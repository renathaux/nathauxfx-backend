"""Disposable schema fixture; writes here are setup only, never cutover operations."""
from datetime import datetime, timezone
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from startup_recovery.checkpoints import _digest, _encoded


def generation(account='synthetic-a', kinds=('live_backup', 'paper_backup')):
    scope = dict(broker='ctrader', environment='demo', account_id=account)
    scope_key = _digest(_encoded(scope))
    identity = dict(account_scope='CTRADER:DEMO:'+account, owner_id='synthetic-owner',
        symbol=None, strategy_id=None, config_hash=None, position_id=None,
        epoch=1, boot_id='synthetic-boot', build_id='a'*40,
        dependencies_hash='d'*64, generation=1)
    payloads = {}
    for kind in kinds:
        payload = {'exact': Decimal('1.000000000000000000000000001')}
        payloads[kind] = _encoded(dict(checkpoint_schema=1, kind=kind, identity=identity,
            parent_hash='e'*64, admission_hash='b'*64, produced_event_id='synthetic-event',
            persisted_at='2026-10-03T00:00:00Z', payload=payload,
            payload_hash=_digest(_encoded(payload))))
    manifest = _encoded(dict(manifest_schema=1, parent_hash='e'*64,
        files={k: _digest(v) for k, v in payloads.items()}))
    refs = [dict(scope_key=scope_key, **scope, kind=k, manifest_hash=_digest(manifest),
        file_hash=_digest(v), generation=1, identity=dict(identity), admission_hash='b'*64)
        for k, v in payloads.items()]
    return manifest, payloads, refs


def snapshot_db(engine, schema):
    """Application rows + sequence state, not PG WAL/statistics."""
    with engine.connect() as c:
        names = c.execute(text('SELECT tablename FROM pg_tables WHERE schemaname=:s ORDER BY tablename'), {'s': schema}).scalars().all()
        rows = {n: tuple(c.execute(text(f'SELECT row_to_json(t)::text FROM "{schema}"."{n}" t ORDER BY row_to_json(t)::text')).scalars()) for n in names}
        sequences = c.execute(text('SELECT sequencename FROM pg_sequences WHERE schemaname=:s ORDER BY sequencename'), {'s': schema}).scalars().all()
        seq = {n: tuple(c.execute(text(f'SELECT last_value,is_called FROM "{schema}"."{n}"')).one()) for n in sequences}
    return rows, seq


@pytest.fixture
def pg(monkeypatch):
    # Same unique-schema/actual model registry isolation as test_recovery_store.db.
    # Required gate: missing disposable PG is a failure, never a skip.
    dsn = os.environ.get('VERIFIER_TEST_PG_DSN', '')
    assert 'host=/tmp/fs-verifier-pg.' in dsn and '/verifier_test?' in dsn
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')  # model registry only
    monkeypatch.setenv('CUTOVER_DATABASE_URL', dsn)
    from models import Base, RecoveryAccount, RecoveryCheckpointHead, RecoveryAttempt, User, StrategySetupLifecycle, TradeSubmissionAttempt
    from services.strategy_studio_models import SavedStrategy
    engine = create_engine(dsn)
    schema = 'cutover_' + uuid4().hex
    with engine.begin() as c:
        c.execute(text('CREATE SCHEMA '+schema))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(scoped)
        bundles = [generation(), generation('synthetic-b', ('app_settings',))]
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        with scoped.begin() as c:
            for _, _, refs in bundles:
                r = refs[0]
                c.execute(RecoveryAccount.__table__.insert().values(**{k: r[k] for k in ('scope_key', 'broker', 'environment', 'account_id')}))
                c.execute(RecoveryAttempt.__table__.insert().values(attempt_id=r['account_id'], scope_key=r['scope_key'], epoch=1, boot_id='synthetic-boot', build_id='a'*40, phase='BOOTSTRAP', created_at=now))
                for r in refs:
                    c.execute(RecoveryCheckpointHead.__table__.insert().values(**{k: v for k,v in r.items() if k not in ('broker','environment','account_id')}))
            c.execute(User.__table__.insert().values(email='synthetic@example.invalid', hashed_password='not-a-credential'))
            c.execute(SavedStrategy.__table__.insert().values(strategy_id='synthetic-strategy',owner_id='synthetic-owner',name='synthetic',definition_json={'untouched': True},created_at=now,updated_at=now))
            c.execute(StrategySetupLifecycle.__table__.insert().values(setup_id='synthetic-setup',owner_id='synthetic-owner',strategy_id='synthetic-strategy',account_id='synthetic-a',account_scope='CTRADER:DEMO:synthetic-a',symbol='EURUSD',direction='BUY',status='SYNTHETIC',definition_snapshot={'untouched':True},updated_at=now))
            c.execute(TradeSubmissionAttempt.__table__.insert().values(event_id='synthetic-event',mode='LIVE',owner_id='synthetic-owner',account_id='synthetic-a',symbol='EURUSD',direction='BUY',signal_setup_id='synthetic-setup',idempotency_key='synthetic-only',attempt_status='UNRESOLVED',claimed_at=now,broker_client_order_id='synthetic-only',request_payload_fingerprint='f'*64,updated_at=now))
            # Auth-like audit canary and independent sequence exercise read-only proof.
            c.execute(text(f'CREATE TABLE "{schema}".auth_canary (id serial PRIMARY KEY, last_seen_at timestamptz)'))
            c.execute(text(f'INSERT INTO "{schema}".auth_canary(last_seen_at) VALUES (NULL)'))
        yield scoped, schema, bundles
    finally:
        with engine.begin() as c:
            c.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        engine.dispose()


@pytest.fixture
def native_inventory(pg, tmp_path):
    """All paths synthetic and declared; no repository/runtime scan."""
    source, external = tmp_path/'native', tmp_path/'external'
    source.mkdir(mode=0o700)
    external.mkdir(mode=0o700)
    for name in ('live_backup.json','paper_backup.json','final_signal_hold.json',
                 'fifteen_m_swing_watch.json','app_settings.json','feature_flags.json',
                 'market_data_source.json','ctrader_accounts.json','news_trading_state.json',
                 'visits.json','live_monthly_history.json','news_trading_audit.jsonl'):
        (source/name).write_bytes(b'{"synthetic_legacy":true}\n')
    for raw,payloads,refs in pg[2]:
        directory = source/'.recovery-generations'/refs[0]['manifest_hash']
        directory.mkdir(parents=True,mode=0o700)
        (directory/'manifest.json').write_bytes(raw)
        for kind,value in payloads.items(): (directory/(kind+'.json')).write_bytes(value)
    locations = {str(n): [] for n in range(13,28)}
    for family,name in ((18,'flowsignal.db'),(19,'candle_cache'),(20,'fast-history'),
                        (21,'fast-jobs'),(22,'scratch'),(23,'nathauxfx-heavy-replay.lock'),
                        (24,'account-coordination.lock'),(25,'facts.pkl'),(26,'__pycache__')):
        locations[str(family)] = [str(external/name)]
    scope = dict(schema=1,binding_overrides={},family_locations=locations,operator_outputs=[])
    return dict(source=source,external=external,state=tmp_path/'state',evidence=tmp_path/'evidence',scope=scope)
