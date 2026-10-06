from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import text

from test_recovery_store import store_api, db
from test_recovery_reconciliation import SCOPE
from startup_recovery.types import RecoveryError


def adapter(db):
    from startup_recovery.server_adapter import ProductionDependencies
    return ProductionDependencies(api_module=SimpleNamespace(), engine=db.kw['bind'],
        session_factory=db, scope=SCOPE, build={'backend_git_sha': 'a' * 40})


def test_schema_verification_is_read_only_and_never_creates_missing_schema(db):
    from models import ExecutionProtocolState
    from services.trade_submission_service import EXECUTION_PROTOCOL_VERSION
    server = adapter(db)
    with pytest.raises(RecoveryError, match='RECOVERY_SCHEMA_UNVERIFIED'):
        server.verify_database()
    schema = db.kw['bind'].get_execution_options()['schema_translate_map'][None]
    quoted = db.kw['bind'].dialect.identifier_preparer.quote(schema)
    with db.begin() as session:
        session.execute(text('CREATE TABLE ' + quoted + '.alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
        session.execute(text('INSERT INTO ' + quoted + '.alembic_version VALUES (:version)'),
                        {'version': '20261001_0030'})
        session.add(ExecutionProtocolState(singleton_id=1, protocol_version='wrong',
            updated_at=datetime.now(timezone.utc)))
    with pytest.raises(RecoveryError, match='RECOVERY_PROTOCOL_UNVERIFIED'):
        server.verify_database()
    with db.begin() as session:
        session.get(ExecutionProtocolState, 1).protocol_version = EXECUTION_PROTOCOL_VERSION
        session.execute(text('UPDATE '+quoted+'.alembic_version SET version_num=\'20261001_0029\''))
    # The pre-intent schema cannot authorize the new durable protocol.
    with pytest.raises(RecoveryError,match='RECOVERY_SCHEMA_UNVERIFIED'):
        server.verify_database()
    with db.begin() as session:
        session.execute(text('UPDATE '+quoted+'.alembic_version SET version_num=\'20261001_0030\''))
    with pytest.raises(RecoveryError,match='RECOVERY_RESERVATION_GUARD_MISSING'):
        server.verify_database()
    from services.submission_reservation import install_reservation_guard
    with db.begin() as session: install_reservation_guard(session.connection())
    assert server.verify_database() == dict(schema='20261001_0030', protocol=EXECUTION_PROTOCOL_VERSION)
    with db() as session:
        assert session.query(ExecutionProtocolState).count() == 1


def test_checkpoint_discovery_preserves_absence_and_unversioned_bytes(db, tmp_path, monkeypatch):
    from startup_recovery import server_adapter
    from startup_recovery.checkpoints import Absent, CheckpointCandidate
    from test_recovery_reconciliation import recovery, snapshot
    paths = {'live_backup': tmp_path / 'live_backup.json',
             'app_settings': tmp_path / 'app_settings.json'}
    paths['live_backup'].write_text('{"legacy":"candidate-only"}')
    original = paths['live_backup'].read_bytes()
    monkeypatch.setattr(server_adapter, 'state_paths', lambda api: paths)
    result = adapter(db).checkpoints(snapshot(recovery()))
    assert isinstance(result['live_backup'], CheckpointCandidate)
    assert result['live_backup'].legacy is True
    assert isinstance(result['app_settings'], Absent)
    assert paths['live_backup'].read_bytes() == original
    assert list(tmp_path.iterdir()) == [paths['live_backup']]


@pytest.mark.parametrize('retained_close', [False, True])
@pytest.mark.parametrize('malformed_paper_memory', [False, True])
def test_server_publication_requires_durable_phase_and_does_not_write_checkpoints(store_api, db, tmp_path, monkeypatch, retained_close, malformed_paper_memory):
    import copy
    import api
    from strategies import shared
    from startup_recovery import server_adapter
    from startup_recovery.reconcile import DiscoverySnapshot, reconcile
    from test_recovery_reconciliation import empty
    from test_recovery_startup import prepare_cutover
    prepare_cutover(store_api, db)
    with db.begin() as session:
        token = store_api.begin_attempt(session, SCOPE, 'production-adapter-test', 'a' * 40)
        store_api.acquire_owner(session, token)
    d, b = empty()
    d['runtime_owners'] = ['owner']
    d['settings'] = [dict(setting_name='live_auto_trade_enabled', setting_value='false')]
    candidates = {}
    if retained_close:
        from test_recovery_publication import accepted_candidate
        candidates = accepted_candidate(tmp_path, d, 'live_backup', dict(live_active_orders={},
            account_close_times={'CTRADER:DEMO:7': {'EURUSD': 900}},
            live_last_execution_time={}, last_live_reset=0))
        # Fixture represents a committed predecessor of this first manager.
        from startup_recovery.checkpoints import _parse, _encoded, CheckpointCandidate
        from startup_recovery.reconcile import digest
        body = _parse(candidates['live_backup'].raw)
        body['identity']['epoch'] = token.epoch
        raw = _encoded(body)
        import hashlib
        d['checkpoint_heads'][0].update(identity=body['identity'], file_hash=hashlib.sha256(raw).hexdigest())
        candidates['live_backup'] = CheckpointCandidate('live_backup', raw, False)
    source = DiscoverySnapshot.create(token, d, b)
    result = reconcile(source, candidates)
    server = server_adapter.ProductionDependencies(api_module=api, engine=db.kw['bind'],
        session_factory=db, scope=SCOPE, build={'backend_git_sha': 'a' * 40})
    server.readers = SimpleNamespace(database=lambda scope: copy.deepcopy(d), broker=lambda scope: copy.deepcopy(b))
    monkeypatch.setattr(server, 'checkpoints', lambda snapshot: candidates)
    paths = {kind: tmp_path / (kind + '.json') for kind in server_adapter.state_paths(api)}
    monkeypatch.setattr(server_adapter, 'state_paths', lambda module: paths)
    for name in ('LIVE_ACTIVE_ORDERS', 'LIVE_ACCOUNT_STATE', 'LIVE_TRADE_HISTORY',
                 'LIVE_BROKER_CLOSED_HISTORY', 'LIVE_BROKER_HISTORY_CACHE', 'LIVE_MONTHLY_HISTORY_CACHE',
                 'LIVE_LAST_EXECUTION_TIME', 'LIVE_LAST_POSITION_CLOSED_AT', 'LIVE_ACCOUNT_CLOSE_TIMES',
                 'LIVE_AUTO_TRADE_ENABLED', 'AUTO_TRADE_ENABLED', 'ENGINE_RUNTIME_STATE'):
        monkeypatch.setattr(api, name, copy.deepcopy(getattr(api, name)))
    for name in ('FINAL_SIGNAL_HOLD', 'FIFTEEN_M_SWING_WATCH', 'SOURCE_BOOT_STATE',
                 'MARKET_DATA_RUNTIME', 'MARKET_DATA_SOURCE', 'AUTO_TRADES', 'PAPER_ACTIVE_TRADES',
                 'PAPER_TRADE_HISTORY', 'PAPER_SETUP_LOCKS', 'LAST_PAPER_RESET'):
        monkeypatch.setattr(shared, name, copy.deepcopy(getattr(shared, name)))
    monkeypatch.setattr(api, 'LAST_LIVE_RESET', api.LAST_LIVE_RESET)
    monkeypatch.setattr(shared, 'LAST_POSITION_CLOSED_AT', {'EURUSD': 9999999999, 'XAUUSD': 0})
    if malformed_paper_memory:
        shared.PAPER_ACTIVE_TRADES = [None]
    before = copy.deepcopy(api.LIVE_ACCOUNT_STATE)
    with pytest.raises(RecoveryError, match='RECOVERY_PUBLICATION_NOT_ADMITTED'):
        server.publish_reconciled(result, token, source)
    assert api.LIVE_ACCOUNT_STATE == before
    phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED', 'STATE_DISCOVERED', 'STATE_RECONCILED']
    for previous, target in zip(phases, phases[1:]):
        with db.begin() as session:
            store_api.advance(session, token, previous, target, result.manifest_hash)
    server.publish_reconciled(result, token, source)
    assert api.LIVE_ACCOUNT_STATE['account_id'] == '7'
    assert api.LIVE_AUTO_TRADE_ENABLED['enabled'] is False
    assert api.LIVE_ACTIVE_ORDERS == {'EURUSD': None, 'XAUUSD': None}
    assert shared.LAST_POSITION_CLOSED_AT == {'CTRADER:DEMO:7:EURUSD': 900 if retained_close else 0,
                                            'CTRADER:DEMO:7:XAUUSD': 0}
    from ctrader_account_context import pinned_account, AccountIdentity
    from strategies.strict_trader import last_position_closed_time
    with pinned_account(AccountIdentity('7', 'demo')):
        close = last_position_closed_time('EURUSD')
        assert (close.timestamp() if close is not None else 0) == (900 if retained_close else 0)
    with pinned_account(AccountIdentity('8', 'demo')):
        assert last_position_closed_time('EURUSD') is None
    if not retained_close:
        assert list(tmp_path.iterdir()) == []
    assert server.writer is not None
    if malformed_paper_memory:
        assert 'paper_backup' not in server.writer._bindings
        assert shared.PAPER_ACTIVE_TRADES == [None]  # Never silently convert corruption to empty authority.
    assert api.BACKGROUND_THREAD is None
    from startup_recovery.reconcile import checkpoint_dependencies
    assert server.produced_dependencies('app_settings', {'risk': {}}) == checkpoint_dependencies('app_settings', d)
    d['selection']['revision'] = 'changed-account-selection'
    with pytest.raises(RecoveryError, match='RECOVERY_ACCOUNT_CONFLICT'):
        server.produced_dependencies('app_settings', {'risk': {}})
    d['selection']['revision'] = 'selection-1'
    d['saved_strategies'] = [{'strategy_id': 'new-version'}]
    # Empty state references no saved definition, even if an unrelated row changes.
    assert server.produced_dependencies('final_signal_hold', {})
