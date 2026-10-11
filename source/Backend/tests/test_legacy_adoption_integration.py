"""The bridge supplies evidence; the unchanged reconciler remains the gate."""
from types import SimpleNamespace
import pytest
from test_legacy_adoption_postgres import prepared, db, migrated_database
from startup_recovery.types import RecoveryError
from startup_recovery.reconcile import DiscoverySnapshot, reconcile
from startup_recovery import server_adapter
from models import RecoveryCheckpointHead


def configured(prepared, monkeypatch):
    p = prepared
    monkeypatch.setattr(server_adapter, 'state_paths', lambda api: {
        k: p['state_root']/(k+'.json') for k in ('live_backup','final_signal_hold',
            'fifteen_m_swing_watch','news_trading_state','ctrader_accounts','paper_backup','visits')})
    deps = server_adapter.ProductionDependencies(api_module=SimpleNamespace(),
        engine=p['session_factory'].kw['bind'], session_factory=p['session_factory'],
        scope=p['token'].scope, build={'backend_git_sha':p['build_id']})
    deps.adoption_configuration = dict(manifest_path=p['manifest_path'],
        expected_manifest_sha256=p['expected_manifest_sha256'],state_root=p['state_root'])
    original = p['snapshot_reader']
    class Database:
        def read_connection(self, connection, scope):
            # Synthetic reader only; production must use the same transaction.
            with p['session_factory']() as session:
                value = original.database(session, p['token'])
                value['checkpoint_heads'] = [{c.name:getattr(h,c.name) for c in h.__table__.columns}
                    for h in session.query(RecoveryCheckpointHead)]
                return value
        def __call__(self, scope):
            return self.read_connection(None, scope)
    deps.readers = SimpleNamespace(database=Database(), broker=lambda scope:original(p['token']).broker)
    return deps


def test_bridge_commits_then_unchanged_reconcile_accepts_and_quarantines(prepared, monkeypatch):
    deps = configured(prepared, monkeypatch)
    token = prepared['token']
    def fresh():
        return DiscoverySnapshot.create(token, deps.readers.database(token.scope), deps.readers.broker(token.scope))
    before = fresh()
    assert not reconcile(before, deps.checkpoints(before)).ok
    result = deps.adopt_legacy_checkpoints(token, before)
    assert result.status == 'COMMITTED'
    after = fresh()
    reconciled = reconcile(after, deps.checkpoints(after))
    assert reconciled.ok, reconciled.conflicts
    assert set(reconciled.quarantined) == {'paper_backup','visits'}
    assert deps.writer is None
    # Normal compatible heads bypass adoption even without configuration.
    deps.adoption_configuration = None
    assert deps.adopt_legacy_checkpoints(token, after).status == 'SKIPPED'


def test_missing_independent_configuration_blocks_legacy_only(prepared, monkeypatch):
    deps = configured(prepared, monkeypatch)
    deps.adoption_configuration = None
    token = prepared['token']
    snap = DiscoverySnapshot.create(token,deps.readers.database(token.scope),deps.readers.broker(token.scope))
    with pytest.raises(RecoveryError, match='MANIFEST_PIN_REQUIRED'):
        deps.adopt_legacy_checkpoints(token,snap)


def test_coordinator_hook_is_after_discovery_and_refreshes_before_reconcile(db):
    from test_recovery_startup import dependencies, prepare_cutover
    from test_recovery_reconciliation import SCOPE
    from startup_recovery import coordinator, store
    prepare_cutover(store,db)
    trace=[]
    deps=dependencies(db,trace)
    read=deps.readers.database
    def tracked(scope):
        trace.append('discovery')
        return read(scope)
    deps.readers.database=tracked
    def adopt(token,snapshot):
        with db() as s:
            assert store.account_state(s,token.scope).phase == 'STATE_DISCOVERED'
        trace.append('adopt')
    deps.adopt_legacy_checkpoints=adopt
    outcome=coordinator.recover(deps,SCOPE,'hook-boot','a'*40)
    assert outcome.ready
    assert trace[:6] == ['authenticate','discovery','discovery','adopt','discovery','discovery']


def test_server_configuration_requires_both_independent_pin_and_explicit_path(monkeypatch):
    from startup_recovery import bootstrap
    monkeypatch.delenv('FLOWSIGNAL_ADOPTION_MANIFEST',raising=False)
    monkeypatch.delenv('FLOWSIGNAL_ADOPTION_MANIFEST_SHA256',raising=False)
    assert bootstrap.adoption_configuration() is None
    monkeypatch.setenv('FLOWSIGNAL_ADOPTION_MANIFEST','/state/.cutover/'+('a'*64)+'.json')
    with pytest.raises(RecoveryError,match='MANIFEST_PIN_REQUIRED'):
        bootstrap.adoption_configuration()
    monkeypatch.setenv('FLOWSIGNAL_ADOPTION_MANIFEST_SHA256','a'*64)
    assert bootstrap.adoption_configuration()['expected_manifest_sha256'] == 'a'*64
