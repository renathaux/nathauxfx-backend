"""Management starts before entry readiness and never creates a second manager."""
from types import SimpleNamespace
import pytest
from recovery_fixture import admitted_worker
from test_recovery_store import store_api, db


def test_existing_engine_starts_at_management_ready_once(monkeypatch, tmp_path):
    import db
    import app_bootstrap
    from fundamentals import ingestion
    from models import RecoveryAccount, RecoveryAttempt
    from startup_recovery.server_adapter import ProductionDependencies
    from startup_recovery.checkpoint_store import RuntimeWriter
    from startup_recovery.runtime import require_worker_admission
    from startup_recovery.types import RecoveryError
    with admitted_worker(monkeypatch) as token:
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'phase': 'POSITION_MANAGEMENT_READY'})
            session.query(RecoveryAttempt).update({'phase': 'POSITION_MANAGEMENT_READY'})
        api = SimpleNamespace(BACKGROUND_THREAD=None)
        server = ProductionDependencies(api_module=api, engine=db.SessionLocal.kw['bind'],
            session_factory=db.SessionLocal, scope=token.scope, build={'backend_git_sha': 'a' * 40})
        server.token = token
        from strategies import shared
        from test_recovery_checkpoints import identity
        path = tmp_path / 'watch.json'
        monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', path)
        server.writer = RuntimeWriter(db.SessionLocal, token, {'fifteen_m_swing_watch': (
            path, identity(), {})}, 'e' * 64, absent_kinds={'fifteen_m_swing_watch'})
        calls = []
        def start(context=None, *, management_only=False):
            assert management_only is True
            assert require_worker_admission(context, 'management') == token
            with pytest.raises(RecoveryError, match='RECOVERY_NOT_COMPLETE'):
                require_worker_admission(context, 'entry')
            calls.append(context)
            api.BACKGROUND_THREAD = SimpleNamespace(is_alive=lambda: True, recovery_context=context)
        monkeypatch.setattr(app_bootstrap, '_start_forex_background_task', start)
        monkeypatch.setattr(ingestion, 'start_fundamental_ingestion_scheduler', lambda **kw: None)
        server.start_management(token)
        assert len(calls) == 1
        server.start_management(token)
        assert len(calls) == 1
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'phase': 'NEW_ENTRIES_READY'})
            session.query(RecoveryAttempt).update({'phase': 'NEW_ENTRIES_READY'})
        server.start_entry_evaluation(token)
        assert len(calls) == 1


def test_entry_only_startup_block_preserves_management(store_api, db):
    from test_recovery_startup import dependencies, prepare_cutover, SCOPE
    from startup_recovery.coordinator import recover
    trace = []
    prepare_cutover(store_api, db)
    deps = dependencies(db, trace)
    deps.entry_readiness = lambda token: {'ready': False, 'reason': 'INDICATOR_STREAM_STARTUP_BLOCKED'}
    outcome = recover(deps, SCOPE, 'management-only', 'a' * 40)
    assert outcome.management_ready is True
    assert outcome.entries_ready is False
    assert outcome.entry_block_reasons == ('INDICATOR_STREAM_STARTUP_BLOCKED',)
    assert trace == ['authenticate', 'publish', 'management']
    with db() as session:
        account, _ = store_api.require_owner(session, outcome.token)
        assert account.phase == 'POSITION_MANAGEMENT_READY'


def test_database_fault_is_not_an_entry_only_block(store_api, db):
    from test_recovery_startup import dependencies, prepare_cutover, SCOPE
    from startup_recovery.coordinator import recover
    from startup_recovery.types import RecoveryError
    trace = []
    prepare_cutover(store_api, db)
    deps = dependencies(db, trace)
    def unavailable(token):
        raise RecoveryError('RECOVERY_DATABASE_UNAVAILABLE')
    deps.entry_readiness = unavailable
    outcome = recover(deps, SCOPE, 'database-fault', 'a' * 40)
    assert outcome.management_ready is False
    assert outcome.entries_ready is False
    assert outcome.reason == 'RECOVERY_DATABASE_UNAVAILABLE'


def test_management_only_cycle_does_not_evaluate_new_setups(monkeypatch):
    import api
    import db
    from models import RecoveryAccount, RecoveryAttempt
    from startup_recovery.operation_context import capture, run
    calls = []
    class Stop(BaseException): pass
    monkeypatch.setattr(api, 'forex_weekend_closed', lambda: False)
    monkeypatch.setattr(api, 'start_ctrader_live_price_stream', lambda **kw: {'ok': True})
    monkeypatch.setattr(api, 'refresh_panel_cache', lambda **kw: pytest.fail('entry evaluation ran'))
    monkeypatch.setattr(api, 'refresh_live_panel_meta', lambda panel: calls.append('management'))
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: (_ for _ in ()).throw(Stop()))
    with admitted_worker(monkeypatch):
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'phase': 'POSITION_MANAGEMENT_READY'})
            session.query(RecoveryAttempt).update({'phase': 'POSITION_MANAGEMENT_READY'})
        context = capture('management')
        with pytest.raises(Stop):
            run(context, api.background_fetch)
    assert calls == ['management']


def test_management_dispatches_only_never_sent_original_repairs(monkeypatch):
    import db
    from models import TradeSubmissionAttempt
    from startup_recovery.operation_context import capture, run
    from services import accepted_position_repair
    import api
    function = getattr(api, 'repair_pending_original_protection', None)
    assert callable(function), 'Accepted original repair is not scheduled by management'
    with admitted_worker(monkeypatch, account_id='123'):
        context = capture('management')
        from test_recovery_fencing import create_submission
        attempt_id = create_submission(db.SessionLocal)
        with db.SessionLocal.begin() as session:
            row = session.get(TradeSubmissionAttempt, attempt_id)
            row.accepted_execution = {'position_id': '77'}
            row.initial_protection = {'state': 'UNRESOLVED'}
        invoked = []
        monkeypatch.setattr(accepted_position_repair, 'repair_original_protection',
            lambda factory, operation_context, attempt: invoked.append(attempt))
        run(context, function)
        assert invoked == []  # Polling cannot replay an uncertain amendment.
        with db.SessionLocal.begin() as session:
            row = session.get(TradeSubmissionAttempt, attempt_id)
            row.initial_protection = {'state': 'UNASSESSED'}
        run(context, function)
        assert invoked == [attempt_id]


def test_request_context_preserves_canonical_selection_revision(monkeypatch):
    import db
    from startup_recovery.server_adapter import ProductionDependencies
    from startup_recovery.checkpoint_store import RuntimeWriter
    from ctrader_account_context import current_identity
    with admitted_worker(monkeypatch) as token:
        server = ProductionDependencies(api_module=SimpleNamespace(), engine=db.SessionLocal.kw['bind'],
            session_factory=db.SessionLocal, scope=token.scope, build={'backend_git_sha': 'a' * 40})
        server.token = token
        server.writer = RuntimeWriter(db.SessionLocal, token, {}, 'e' * 64)
        server._baseline_database = {'selection': {'revision': '2026-10-01 00:00:00+00:00'}}
        with server.runtime_context() as context:
            assert current_identity().selection_revision == '2026-10-01T00:00:00+00:00'
            assert context.selection_revision == '2026-10-01T00:00:00+00:00'
