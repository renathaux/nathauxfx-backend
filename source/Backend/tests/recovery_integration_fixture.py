"""Disposable PostgreSQL admission through the production recovery coordinator.

No recovery rows, phases, tokens or accepted executions are fabricated here.
The broker discovery boundary and continuous worker launch are controlled test
dependencies; database discovery, reconciliation and publication remain real.
"""
import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text


def prepare_database(factory, account_id='7'):
    from models import RuntimeSetting, ExecutionProtocolState, StrategyStudioLiveState
    from services.trade_submission_service import EXECUTION_PROTOCOL_VERSION
    from services.submission_reservation import install_reservation_guard
    engine = factory.kw['bind']
    schema = engine.get_execution_options()['schema_translate_map'][None]
    quoted = engine.dialect.identifier_preparer.quote(schema)
    with factory.begin() as session:
        session.execute(text('CREATE TABLE ' + quoted + '.alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
        session.execute(text('INSERT INTO ' + quoted + '.alembic_version VALUES (:v)'), {'v': '20261001_0030'})
        if session.get(ExecutionProtocolState, 1) is None:
            session.add(ExecutionProtocolState(singleton_id=1, protocol_version=EXECUTION_PROTOCOL_VERSION,
                updated_at=datetime.now(timezone.utc)))
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value=json.dumps(dict(account_id=account_id, env='demo')),
            updated_at=datetime.now(timezone.utc), updated_by='isolated-test'))
        if session.query(StrategyStudioLiveState).count() == 0:
            session.add(StrategyStudioLiveState(owner_id='verification-owner', enabled=True,
                updated_at=datetime.now(timezone.utc)))
        install_reservation_guard(session.connection())


def recovery_server(factory, monkeypatch, tmp_path, *, account_id='7', broker=None):
    import api
    import db
    from strategies import shared
    from startup_recovery import server_adapter
    from startup_recovery.types import AccountScope
    monkeypatch.setattr(db, 'SessionLocal', factory)
    scope = AccountScope('ctrader', 'demo', account_id)
    # Isolate every shared publication target; production publication itself runs.
    for module, names in ((api, ('LIVE_ACTIVE_ORDERS', 'LIVE_ACCOUNT_STATE', 'LIVE_TRADE_HISTORY',
        'LIVE_BROKER_CLOSED_HISTORY', 'LIVE_BROKER_HISTORY_CACHE', 'LIVE_MONTHLY_HISTORY_CACHE',
        'LIVE_ACCOUNT_CLOSE_TIMES', 'LIVE_LAST_EXECUTION_TIME', 'LIVE_LAST_POSITION_CLOSED_AT',
        'LAST_LIVE_RESET', 'LIVE_AUTO_TRADE_ENABLED', 'AUTO_TRADE_ENABLED')),
        (shared, ('FINAL_SIGNAL_HOLD', 'FIFTEEN_M_SWING_WATCH', 'SOURCE_BOOT_STATE',
        'MARKET_DATA_SOURCE', 'MARKET_DATA_RUNTIME', 'LAST_POSITION_CLOSED_AT', 'AUTO_TRADES',
        'PAPER_ACTIVE_TRADES', 'PAPER_TRADE_HISTORY', 'PAPER_SETUP_LOCKS', 'LAST_PAPER_RESET'))):
        for name in names:
            monkeypatch.setattr(module, name, copy.deepcopy(getattr(module, name)))
    paths = {kind: tmp_path / (kind + '.json') for kind in server_adapter.state_paths(api)}
    monkeypatch.setattr(server_adapter, 'state_paths', lambda module: paths)
    from services import settings_service
    import ctrader_connector
    monkeypatch.setattr(settings_service, 'SETTINGS_PATH', paths['app_settings'])
    monkeypatch.setattr(settings_service, 'FEATURE_FLAGS_PATH', paths['feature_flags'])
    monkeypatch.setattr(ctrader_connector, 'CTRADER_ACCOUNTS_PATH', paths['ctrader_accounts'])
    server = server_adapter.ProductionDependencies(api_module=api, engine=factory.kw['bind'],
        session_factory=factory, scope=scope, build={'backend_git_sha': 'a' * 40})
    observed_at = time.time()
    state = broker if broker is not None else dict(account_id=account_id, environment='demo',
        complete=True, authenticated=True, balance='10000', equity='10000', positions=[],
        orders=[], deals=[], history_from=0, history_to=observed_at, history_complete=True,
        observed_at=observed_at, metadata={})
    monkeypatch.setattr(server.readers, 'authenticate', lambda target: dict(
        authenticated=target == scope, account_id=account_id, environment='demo'))
    monkeypatch.setattr(server.readers, 'broker', lambda target: copy.deepcopy(state))
    trace = []
    # Do not start infinite workers in a test. Each launch still verifies the
    # actual phase committed by coordinator, rather than granting admission.
    def management(token):
        with server.runtime_context():
            from startup_recovery.runtime import require_worker_admission
            require_worker_admission(None, 'management')
            trace.append(('management', token))
    def entries(token):
        with server.runtime_context(entries=True):
            trace.append(('entries', token))
    monkeypatch.setattr(server, 'start_management', management)
    monkeypatch.setattr(server, 'start_entry_evaluation', entries)
    return SimpleNamespace(server=server, scope=scope, broker=state, trace=trace, paths=paths)


def admit(harness, *, boot='integration-a', entries=True, cutover=True):
    from startup_recovery import coordinator, store
    from startup_recovery.types import HandoffEvidence
    server, scope = harness.server, harness.scope
    if cutover:
        with server.session_factory.begin() as session:
            store.begin_attempt(session, scope, 'cutover-evidence', 'a' * 40)
            store.establish_legacy_cutover(session, scope,
                HandoffEvidence('operator-termination', 'b' * 64, 'test-predecessor', True, True))
    # Explicit availability of the external indicator feed; no owner/readiness
    # rows are assigned. The coordinator alone performs the phase transition.
    server.entry_readiness = lambda token: ({'ready': True} if entries else
        {'ready': False, 'reason': 'INDICATOR_STREAM_STARTUP_BLOCKED'})
    outcome = coordinator.recover(server, scope, boot, 'a' * 40)
    assert outcome.management_ready, (outcome.reason, outcome.result)
    assert outcome.entries_ready is entries
    return outcome
