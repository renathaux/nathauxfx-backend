from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from test_recovery_store import store_api, db
from test_recovery_checkpoint_publication import admitted
from startup_recovery.types import Phase, RecoveryError


def test_server_activation_uses_fixed_admitted_context_and_one_existing_loop(store_api, db, monkeypatch, tmp_path):
    from startup_recovery.server_adapter import ProductionDependencies
    from startup_recovery.checkpoint_store import RuntimeWriter
    from startup_recovery.runtime import caller_token
    import app_bootstrap
    from fundamentals import ingestion
    import db as database
    monkeypatch.setattr(database, 'SessionLocal', db)
    token = admitted(store_api, db)
    api = SimpleNamespace(BACKGROUND_THREAD=None)
    server = ProductionDependencies(api_module=api, engine=db.kw['bind'],
        session_factory=db, scope=token.scope, build={'backend_git_sha': 'a' * 40})
    from strategies import shared, strict_trader
    from test_recovery_checkpoint_publication import envelope
    import json
    path = tmp_path / 'watch.json'
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', path)
    watches = {'EURUSD': {'symbol': 'EURUSD', 'status': strict_trader.BLOCKED_BREAKOUT_STATUS}}
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH', watches)
    path.write_text(json.dumps(watches))
    original_bytes = path.read_bytes()
    server.writer = RuntimeWriter(db, token, {'fifteen_m_swing_watch': (
        path, json.loads(envelope(token).raw)['identity'], watches)}, 'd' * 64)
    server.token = token
    original = Mock()
    def existing_loop(*, context, management_only):
        assert caller_token() == token
        assert context.token == token and management_only is True
        assert shared.FIFTEEN_M_SWING_WATCH == {}
        assert path.read_bytes() == original_bytes
        original()
        api.BACKGROUND_THREAD = SimpleNamespace(is_alive=lambda: True, recovery_context=context)
    monkeypatch.setattr(app_bootstrap, '_start_forex_background_task', existing_loop)
    scheduler = Mock(side_effect=lambda **kwargs: caller_token())
    monkeypatch.setattr(ingestion, 'start_fundamental_ingestion_scheduler', scheduler)
    server.start_management(token)
    original.assert_called_once()
    scheduler.assert_called_once()
    with pytest.raises(RecoveryError, match='RECOVERY_NOT_COMPLETE'):
        server.start_entry_evaluation(token)
    with db.begin() as session:
        store_api.advance(session, token, Phase.POSITION_MANAGEMENT_READY, Phase.NEW_ENTRIES_READY, 'd' * 64)
    server.start_entry_evaluation(token)
    server.start_entry_evaluation(token)
    original.assert_called_once()
    scheduler.assert_called_once()
    with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_MISSING'):
        caller_token()


def test_request_context_never_adopts_a_new_owner_and_does_not_leak_context(store_api, db):
    import asyncio
    from startup_recovery.asgi import RecoveryContextMiddleware
    from startup_recovery.server_adapter import ProductionDependencies
    from startup_recovery.checkpoint_store import RuntimeWriter
    from startup_recovery.runtime import caller_token
    from startup_recovery.coordinator import block
    token = admitted(store_api, db)
    runtime = ProductionDependencies(api_module=SimpleNamespace(), engine=db.kw['bind'],
        session_factory=db, scope=token.scope, build={'backend_git_sha': 'a' * 40})
    runtime.token, runtime.writer = token, RuntimeWriter(db, token, {}, 'd' * 64)
    observed = []
    async def app(scope, receive, send):
        try:
            observed.append(caller_token())
        except RecoveryError as exc:
            observed.append(exc.code)
    middleware = RecoveryContextMiddleware(app, runtime_api=SimpleNamespace(_recovery_runtime=runtime))
    asyncio.run(middleware({'type': 'http'}, None, None))
    assert observed == [token]
    with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_MISSING'):
        caller_token()
    block(db, token, 'fixture revocation')
    asyncio.run(middleware({'type': 'http'}, None, None))
    assert observed == [token, 'RECOVERY_TOKEN_MISSING']


def test_request_context_database_failure_is_sanitized_and_never_grants_capability(db):
    import asyncio
    from startup_recovery.asgi import RecoveryContextMiddleware
    from startup_recovery.server_adapter import ProductionDependencies
    from startup_recovery.runtime import caller_token
    class Unavailable:
        def __call__(self): raise OSError('private connection detail')
    runtime = ProductionDependencies(api_module=SimpleNamespace(), engine=db.kw['bind'],
        session_factory=Unavailable(), scope=SimpleNamespace(), build={})
    runtime.token, runtime.writer = object(), object()
    observed = []
    async def app(scope, receive, send):
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_MISSING'):
            caller_token()
        observed.append('no capability')
    middleware = RecoveryContextMiddleware(app, runtime_api=SimpleNamespace(_recovery_runtime=runtime))
    asyncio.run(middleware({'type': 'http'}, None, None))
    assert observed == ['no capability']


def test_fundamental_threads_keep_fixed_epoch_and_stale_scheduler_stops(monkeypatch):
    from fundamentals import ingestion
    from recovery_fixture import admitted_worker
    from startup_recovery.runtime import caller_token, invalidate_token
    targets = []
    class Thread:
        ident = 1
        def __init__(self, *, target, **kwargs): targets.append(target)
        def start(self): pass
        def is_alive(self): return False
    monkeypatch.setattr(ingestion.threading, 'Thread', Thread)
    monkeypatch.setattr(ingestion, '_SCHEDULER_THREAD', None)
    monkeypatch.setattr(ingestion, '_WORKER_THREAD', None)
    monkeypatch.setattr(ingestion, '_LAST_KICK_MONOTONIC', 0)
    observed = []
    monkeypatch.setattr(ingestion, 'run_fundamental_ingestion_if_due',
                        lambda: observed.append(caller_token()))
    with admitted_worker(monkeypatch) as token:
        ingestion.kick_fundamental_ingestion()
        ingestion.start_fundamental_ingestion_scheduler()
        from contextvars import Context
        Context().run(targets[0])
        assert observed == [token]
        invalidate_token(token)
        # No sleep, provider fetch, or replacement epoch lookup after revocation.
        monkeypatch.setattr(ingestion.time, 'sleep', lambda _: pytest.fail('stale scheduler continued'))
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            Context().run(targets[1])
        assert observed == [token]
