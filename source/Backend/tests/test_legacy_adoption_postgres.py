"""Atomic adoption on disposable PostgreSQL and strict Linux filesystem storage."""
import importlib
import os
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy import event
from dataclasses import replace
from tests.test_cutover_files import snapshot
from test_legacy_adoption_migration import database as migrated_database
from sqlalchemy.orm import sessionmaker
from alembic import command
from test_legacy_adoption_semantics import inputs
from startup_recovery import store
from startup_recovery.checkpoints import _encoded, _digest, _parse
from startup_recovery.cutover_files import ReadTree, CopyTarget
from startup_recovery.cutover_service import inventory_state, copy_state
from startup_recovery.reconcile import DiscoverySnapshot
from startup_recovery.types import AccountScope, HandoffEvidence, RecoveryError
from models import RecoveryCheckpointHead, RecoveryLegacyAdoption, RecoveryMutation, RuntimeSetting


@pytest.fixture
def db(migrated_database):
    engine, cfg = migrated_database
    command.upgrade(cfg, '20261010_0031')
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def prepared(db, tmp_path, monkeypatch):
    assert hasattr(os, 'O_NOATIME'), 'Required strict Linux filesystem environment'
    payloads, database, broker = inputs()
    with db.begin() as s:
        token = store.begin_attempt(s, AccountScope('ctrader', 'demo', '7'), 'boot', 'build')
    with db.begin() as s:
        store.establish_legacy_cutover(s, token.scope, HandoffEvidence('operator-termination', 'c'*64, 'predecessor', True, True))
        store.acquire_owner(s, token)
        for old, new in [('BOOTSTRAP','DB_READY'), ('DB_READY','BROKER_AUTHENTICATED'), ('BROKER_AUTHENTICATED','STATE_DISCOVERED')]:
            store.advance(s, token, old, new, 'd'*64)
        s.add(RuntimeSetting(setting_name='live_auto_trade_enabled', setting_value='false', updated_at=datetime.now(timezone.utc), updated_by='synthetic'))
    source, state, evidence, external = [tmp_path/n for n in ('source','state','evidence','external')]
    source.mkdir(mode=0o700)
    external.mkdir(mode=0o700)
    for k, p in {**payloads, 'paper_backup': {}, 'visits': []}.items():
        (source/(k+'.json')).write_bytes(_encoded(p))
    scope = dict(schema=1,binding_overrides={},family_locations={str(n): [] for n in range(13,28)},operator_outputs=[])
    scope['family_locations']['18'] = [str(external/'absent.sqlite')]
    engine = db.kw['bind']
    monkeypatch.setenv('CUTOVER_DATABASE_URL', engine.url.render_as_string(hide_password=False))
    with ReadTree(source) as tree, CopyTarget(evidence) as out:
        document, report = inventory_state(tree, state, scope, engine, out)
    assert not report.blockers
    assert copy_state(document,engine,expected_digest=document.digest).copy_verified
    class Reader:
        def database(self, session, passed_token):
            fresh = deepcopy(database)
            row = session.get(RuntimeSetting, 'live_auto_trade_enabled', populate_existing=True)
            fresh['settings'] = [dict(setting_name=row.setting_name, setting_value=row.setting_value)]
            return fresh
        def __call__(self, passed_token):
            with db() as s:
                return DiscoverySnapshot.create(passed_token, self.database(s, passed_token), deepcopy(broker))
    return dict(session_factory=db, token=token, build_id='build', snapshot_reader=Reader(),
                manifest_path=state/'.cutover'/(document.digest+'.json'),
                expected_manifest_sha256=document.digest, state_root=state)


def adopt(prepared):
    module = importlib.import_module('startup_recovery.legacy_adoption')
    assert hasattr(module, 'adopt_verified_cutover'), 'Atomic adoption entrypoint missing'
    return module.adopt_verified_cutover(**prepared)


def test_five_heads_and_receipt_commit_without_readiness_or_input_mutation(prepared):
    p = prepared
    before = {name: snapshot(p['state_root']/name) for name in os.listdir(p['state_root'])}
    result = adopt(p)
    assert result.status == 'COMMITTED'
    with p['session_factory']() as s:
        assert s.query(RecoveryLegacyAdoption).count() == 1
        assert s.query(RecoveryCheckpointHead).count() == 5
        assert {h.kind for h in s.query(RecoveryCheckpointHead)} == {'live_backup','final_signal_hold','fifteen_m_swing_watch','news_trading_state','ctrader_accounts'}
        assert s.query(RecoveryMutation).count() == 0
        assert store.account_state(s, p['token'].scope).phase == 'STATE_DISCOVERED'
        assert not store.entries_ready(s,p['token'])
    assert before == {name: snapshot(p['state_root']/name) for name in before}
    assert adopt(p).status == 'ALREADY_COMMITTED'


@pytest.mark.parametrize('fault', ['pin','build','live_on','missing','extra','changed'])
def test_failed_gate_publishes_no_authority(prepared, fault):
    p = prepared
    if fault == 'pin': p['expected_manifest_sha256'] = 'f'*64
    elif fault == 'build': p['build_id'] = 'other'
    elif fault == 'live_on':
        with p['session_factory'].begin() as s:
            s.get(RuntimeSetting,'live_auto_trade_enabled').setting_value = 'true'
    elif fault == 'missing': (p['state_root']/'live_backup.json').unlink()
    elif fault == 'extra': (p['state_root']/'unexplained.json').write_bytes(b'{}')
    elif fault == 'changed': (p['state_root']/'live_backup.json').write_bytes(b'{}')
    with pytest.raises(RecoveryError):
        adopt(p)
    with p['session_factory']() as s:
        assert s.query(RecoveryLegacyAdoption).count() == 0
        assert s.query(RecoveryCheckpointHead).count() == 0


@pytest.mark.parametrize('kind', ['live_backup','final_signal_hold','fifteen_m_swing_watch','news_trading_state','ctrader_accounts'])
def test_each_source_hash_mismatch_blocks(prepared, kind):
    (prepared['state_root']/(kind+'.json')).write_bytes(b'{"changed":true}')
    with pytest.raises(RecoveryError):
        adopt(prepared)


@pytest.mark.parametrize('field,value', [('epoch',99), ('boot_id','other'), ('attempt_id','other')])
def test_stale_owner_cannot_adopt(prepared, field, value):
    prepared['token'] = replace(prepared['token'], **{field:value})
    with pytest.raises(RecoveryError):
        adopt(prepared)


def test_partial_receipt_head_set_blocks_after_commit(prepared):
    adopt(prepared)
    with prepared['session_factory'].begin() as s:
        head = s.execute(select(RecoveryCheckpointHead).limit(1)).scalar_one()
        s.delete(head)
    with pytest.raises(RecoveryError, match='RETRY_CONFLICT'):
        adopt(prepared)


def test_post_generation_commit_failure_leaves_no_heads_and_reuses_exact_orphan(prepared):
    factory = prepared['session_factory']
    def fail_commit(session):
        if any(isinstance(obj, RecoveryLegacyAdoption) for obj in session.identity_map.values()) or session.info.get('adoption_pending'):
            raise RuntimeError('synthetic commit failure')
    # Target the transaction that actually flushes adoption authority, not setup.
    def mark(session, context, instances):
        if any(isinstance(obj, RecoveryLegacyAdoption) for obj in session.new):
            session.info['adoption_pending'] = True
    event.listen(factory.class_, 'before_flush', mark)
    event.listen(factory.class_, 'before_commit', fail_commit)
    try:
        with pytest.raises(RecoveryError, match='COMMIT'):
            adopt(prepared)
    finally:
        event.remove(factory.class_, 'before_flush', mark)
        event.remove(factory.class_, 'before_commit', fail_commit)
    with factory() as s:
        assert s.query(RecoveryLegacyAdoption).count() == s.query(RecoveryCheckpointHead).count() == 0
    directory = prepared['state_root']/'.recovery-generations'
    before = snapshot(directory)
    assert adopt(prepared).status == 'COMMITTED'
    assert snapshot(directory) == before


@pytest.mark.parametrize('write_number', range(1,7))
def test_interrupted_generation_never_publishes_partial_heads(prepared, monkeypatch, write_number):
    from startup_recovery import checkpoints
    original = checkpoints._write_at
    calls = 0
    def interrupted(fd, name, raw):
        nonlocal calls
        calls += 1
        if calls == write_number:
            raise OSError('synthetic interruption')
        return original(fd, name, raw)
    monkeypatch.setattr(checkpoints, '_write_at', interrupted)
    with pytest.raises(RecoveryError):
        adopt(prepared)
    with prepared['session_factory']() as s:
        assert s.query(RecoveryCheckpointHead).count() == s.query(RecoveryLegacyAdoption).count() == 0
    with pytest.raises(RecoveryError):
        adopt(prepared)  # a partial temporary generation is never guessed/repaired


def test_lost_commit_acknowledgement_is_inspected_not_blindly_retried(prepared):
    factory = prepared['session_factory']
    def lost_ack(session):
        raise RuntimeError('synthetic lost acknowledgement')
    event.listen(factory.class_, 'after_commit', lost_ack)
    try:
        with pytest.raises(RecoveryError, match='COMMIT.*COMMITTED_COMPLETE'):
            adopt(prepared)
    finally:
        event.remove(factory.class_, 'after_commit', lost_ack)
    with factory() as s:
        assert s.query(RecoveryCheckpointHead).count() == 5
        assert s.query(RecoveryLegacyAdoption).count() == 1
    assert adopt(prepared).status == 'ALREADY_COMMITTED'


def test_foreign_scope_head_means_generation_is_not_an_orphan(prepared):
    from models import RecoveryAccount
    adopt(prepared)
    with prepared['session_factory'].begin() as s:
        heads = s.query(RecoveryCheckpointHead).all()
        old = {c.name: getattr(heads[0], c.name) for c in RecoveryCheckpointHead.__table__.columns}
        s.query(RecoveryLegacyAdoption).delete()
        s.query(RecoveryCheckpointHead).delete()
        # The migrated fixture's separate scope exists, with no authority before this injection.
        old['scope_key'] = 'scope'
        s.add(RecoveryCheckpointHead(**old))
    with pytest.raises(RecoveryError):
        adopt(prepared)


@pytest.mark.parametrize('fault', ['phase', 'handoff', 'owner_build', 'manifest_missing', 'manifest_extra'])
def test_identity_and_manifest_gates(prepared, fault):
    from models import RecoveryAccount, RecoveryAttempt
    p = prepared
    if fault in {'phase','handoff','owner_build'}:
        with p['session_factory'].begin() as s:
            account = s.get(RecoveryAccount, store.scope_key(p['token'].scope))
            attempt = s.get(RecoveryAttempt, p['token'].attempt_id)
            if fault == 'phase': account.phase = attempt.phase = 'RECOVERY_BLOCKED'
            elif fault == 'handoff': account.handoff_evidence = None
            else: attempt.build_id = 'other'
    elif fault == 'manifest_missing': p['manifest_path'].unlink()
    else: (p['state_root']/'.cutover'/('f'*64+'.json')).write_bytes(b'{}')
    with pytest.raises(RecoveryError):
        adopt(p)


def test_five_heads_not_visible_before_commit(prepared):
    factory = prepared['session_factory']
    observations = []
    def inspect(session, context):
        with factory() as independent:
            observations.append((independent.query(RecoveryCheckpointHead).count(),
                                 independent.query(RecoveryLegacyAdoption).count()))
    event.listen(factory.class_, 'after_flush', inspect)
    try:
        adopt(prepared)
    finally:
        event.remove(factory.class_, 'after_flush', inspect)
    assert observations and set(observations) == {(0,0)}


@pytest.mark.parametrize('target,field,value', [
    ('broker','account_id','other'), ('broker','environment','live'),
    ('broker','positions',[{'position_id':'synthetic'}]),
    ('broker','orders',[{'order_id':'synthetic'}]),
    ('broker','complete',False), ('broker','authenticated',False),
    ('database','submissions',[{'attempt_status':'SUBMITTING'}]),
    ('database','runtime_owners',[]),
])
def test_fresh_discovery_gate_blocks_before_generation(prepared, target, field, value):
    original = prepared['snapshot_reader']
    class Reader:
        database = original.database
        def __call__(self, token):
            snapshot = original(token)
            db, broker = snapshot.database, snapshot.broker
            (db if target == 'database' else broker)[field] = value
            return DiscoverySnapshot.create(token, db, broker)
    prepared['snapshot_reader'] = Reader()
    with pytest.raises(RecoveryError):
        adopt(prepared)
    assert not (prepared['state_root']/'.recovery-generations').exists()


def test_transaction_database_drift_blocks_before_generation(prepared):
    original = prepared['snapshot_reader']
    class Reader:
        def __call__(self, token):
            return original(token)
        def database(self, session, token):
            db = original.database(session, token)
            db['runtime_owners'] = ['different-owner']
            return db
    prepared['snapshot_reader'] = Reader()
    with pytest.raises(RecoveryError, match='DATABASE_DRIFT'):
        adopt(prepared)
    assert not (prepared['state_root']/'.recovery-generations').exists()


@pytest.mark.parametrize('fsync_number', range(1,10))
def test_each_generation_fsync_failure_has_no_database_authority(prepared, monkeypatch, fsync_number):
    original = os.fsync
    calls = 0
    def interrupted(fd):
        nonlocal calls
        calls += 1
        if calls == fsync_number:
            raise OSError('synthetic fsync failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', interrupted)
    with pytest.raises(RecoveryError):
        adopt(prepared)
    assert calls >= fsync_number
    with prepared['session_factory']() as s:
        assert s.query(RecoveryLegacyAdoption).count() == s.query(RecoveryCheckpointHead).count() == 0


def test_failure_before_directory_creation_has_no_side_effect(prepared, monkeypatch):
    from startup_recovery import checkpoints
    before = snapshot(prepared['state_root'])
    def fail(*args, **kwargs):
        raise OSError('synthetic mkdir failure')
    monkeypatch.setattr(checkpoints.os, 'mkdir', fail)
    with pytest.raises(RecoveryError):
        adopt(prepared)
    assert snapshot(prepared['state_root']) == before
