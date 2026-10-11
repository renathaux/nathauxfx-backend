"""Task 5: a delayed worker retains its admitted identity, never a new owner."""
from dataclasses import FrozenInstanceError
import importlib

import pytest
from test_recovery_store import store_api, db, begin

from recovery_fixture import admitted_worker
from startup_recovery.types import RecoveryError


def context_api():
    try:
        return importlib.import_module('startup_recovery.operation_context')
    except ModuleNotFoundError:
        pytest.fail('Explicit immutable worker context is missing')


def test_worker_context_is_immutable_and_contains_no_live_capability(monkeypatch):
    api = context_api()
    with admitted_worker(monkeypatch) as token:
        context = api.capture('management')
        assert context.token == token
        assert context.snapshot_hash == 'e' * 64
        with pytest.raises(FrozenInstanceError):
            context.role = 'entry'
        assert set(vars(context)) == {'token', 'role', 'snapshot_hash', 'selection_revision', 'operation_id', 'position_id'}


def test_management_admitted_without_entry_readiness(monkeypatch):
    api = context_api()
    import db
    from models import RecoveryAccount, RecoveryAttempt
    from startup_recovery.runtime import require_worker_admission
    with admitted_worker(monkeypatch):
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'phase': 'POSITION_MANAGEMENT_READY'})
            session.query(RecoveryAttempt).update({'phase': 'POSITION_MANAGEMENT_READY'})
        context = api.capture('management')
        assert require_worker_admission(context, 'management') == context.token
        with pytest.raises(RecoveryError, match='RECOVERY_NOT_COMPLETE'):
            require_worker_admission(context, 'entry')


def test_delayed_worker_cannot_publish_or_rebind_after_owner_change(monkeypatch):
    api = context_api()
    import db
    from models import RecoveryAccount
    observed = []
    with admitted_worker(monkeypatch):
        context = api.capture('panel')
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'owner_epoch': 2})
        from contextvars import Context
        with pytest.raises(RecoveryError):
            Context().run(api.run, context, lambda: observed.append('published'))
    assert observed == []


def test_worker_does_not_inherit_mutation_permit(monkeypatch):
    api = context_api()
    from startup_recovery.runtime import operation_context, operation_permit
    with admitted_worker(monkeypatch):
        context = api.capture('management')
        def worker():
            with pytest.raises(RecoveryError, match='RECOVERY_MUTATION_UNFENCED'):
                operation_permit()
        with operation_context(object()):
            api.run(context, worker)


@pytest.mark.parametrize('module,name,args', [
    ('api', 'start_background_task', ()),
    ('app_bootstrap', '_start_forex_background_task', ()),
    ('api', 'schedule_panel_cache_refresh', ('test',)),
    ('api', 'schedule_panel_refresh', ()),
    ('ctrader_connector', 'start_ctrader_live_price_stream', ()),
    ('ctrader_connector', 'ctrader_live_price_stream_loop', ()),
    ('fundamentals.ingestion', 'kick_fundamental_ingestion', ()),
    ('fundamentals.ingestion', 'start_fundamental_ingestion_scheduler', ()),
])
def test_stale_constructor_cannot_start_work(monkeypatch, module, name, args):
    api = context_api()
    target = getattr(importlib.import_module(module), name)
    from startup_recovery.runtime import invalidate_token
    with admitted_worker(monkeypatch) as token:
        context = api.capture('management')
        invalidate_token(token)
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            target(*args, context=context)


def test_delayed_tick_is_discarded_after_epoch_changes(monkeypatch):
    api = context_api()
    import db
    import ctrader_connector as connector
    from models import RecoveryAccount
    monkeypatch.setattr(connector, 'LIVE_TICKS', {'EURUSD': {'mid': 1.2}})
    with admitted_worker(monkeypatch):
        context = api.capture('price')
        def delayed():
            with db.SessionLocal.begin() as session:
                session.query(RecoveryAccount).update({'owner_epoch': 2})
            connector.update_live_tick('EURUSD', 110000, 110002)
        with pytest.raises(RecoveryError):
            api.run(context, delayed)
    assert connector.LIVE_TICKS == {'EURUSD': {'mid': 1.2}}


def test_worker_retains_fixed_account_selection_revision(monkeypatch):
    api = context_api()
    from ctrader_account_context import AccountIdentity, pinned_account, current_identity
    with admitted_worker(monkeypatch):
        with pinned_account(AccountIdentity('fixture-worker', 'demo', 'revision-a')):
            context = api.capture('panel')
        seen = api.run(context, current_identity)
        assert seen == AccountIdentity('fixture-worker', 'demo', 'revision-a')


def test_explicit_worker_keeps_only_its_admitted_checkpoint_binding(monkeypatch, tmp_path):
    api = context_api()
    import db
    from startup_recovery import checkpoint_store as checkpoints
    with admitted_worker(monkeypatch) as token:
        context = api.capture('management')
        writer = checkpoints.RuntimeWriter(db.SessionLocal, token,
            {'app_settings': (tmp_path / 'settings.json', {}, {'risk': {'percent': 1}})}, 'e' * 64)
        register = getattr(checkpoints, 'register_worker_producer', None)
        assert callable(register), 'Workers need an exact-token checkpoint binding, not copied authority'
        register(writer)
        result = api.run(context, checkpoints.read_runtime, tmp_path / 'settings.json', 'app_settings')
        assert result == {'risk': {'percent': 1}}


def test_delayed_worker_cannot_open_a_new_broker_socket(monkeypatch):
    api = context_api()
    import ctrader_connector as connector
    from startup_recovery.runtime import invalidate_token
    monkeypatch.setattr(connector.socket, 'create_connection',
        lambda *args, **kwargs: pytest.fail('stale worker reached broker transport'))
    with admitted_worker(monkeypatch) as token:
        context = api.capture('price')
        def delayed():
            invalidate_token(token)
            connector.open_ctrader_json_socket('unused.invalid', 1)
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            api.run(context, delayed)


def test_delayed_panel_result_cannot_overwrite_new_owner_cache(monkeypatch):
    context_module = context_api()
    import api
    import db
    import ctrader_account_context
    from models import RecoveryAccount
    monkeypatch.setattr(api, 'PANEL_CACHE', {'data': {'owner': 'B'}})
    monkeypatch.setattr(api, '_panel_cache_validity', lambda value: {'valid': True, 'candle_counts': {}})
    monkeypatch.setattr(api, 'process_signal_email_alerts', lambda value: None)
    monkeypatch.setattr(ctrader_account_context, 'selected_identity', lambda: None)
    with admitted_worker(monkeypatch):
        context = context_module.capture('panel')
        def delayed():
            with db.SessionLocal.begin() as session:
                session.query(RecoveryAccount).update({'owner_epoch': 2})
            api.update_panel_cache({'owner': 'A'}, 'delayed')
        with pytest.raises(RecoveryError):
            context_module.run(context, delayed)
    assert api.PANEL_CACHE == {'data': {'owner': 'B'}}


def test_postgres_delayed_a_cannot_publish_after_proven_b_handoff(store_api, db, monkeypatch):
    import threading
    import db as database
    import ctrader_connector as connector
    from test_recovery_checkpoint_publication import admitted
    from startup_recovery.types import HandoffEvidence
    from startup_recovery.runtime import manager_context
    api = context_api()
    monkeypatch.setattr(database, 'SessionLocal', db)
    monkeypatch.setattr(connector, 'LIVE_TICKS', {'EURUSD': {'owner': 'B'}})
    a = admitted(store_api, db)
    with manager_context(a):
        context = api.capture('price')
    entered, resume = threading.Event(), threading.Event()
    results = []
    def delayed():
        entered.set()
        assert resume.wait(5)
        connector.update_live_tick('EURUSD', 110000, 110002)
    def worker():
        try:
            api.run(context, delayed)
        except RecoveryError as exc:
            results.append(exc.code)
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    try:
        b = begin(store_api, db, 'boot-b')
        with db.begin() as session:
            store_api.relinquish(session, a, HandoffEvidence(
                'graceful-drain', 'b' * 64, a.boot_id, True, True))
        with db.begin() as session:
            store_api.acquire_owner(session, b)
        with db() as session:
            account, _ = store_api.require_owner(session, b)
            assert account.owner_epoch == b.epoch
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results == ['RECOVERY_TOKEN_STALE']
    assert connector.LIVE_TICKS == {'EURUSD': {'owner': 'B'}}


def test_stale_panel_error_and_finally_do_not_overwrite_successor_state(monkeypatch):
    context_module = context_api()
    import api
    import db
    from models import RecoveryAccount
    monkeypatch.setattr(api, 'PANEL_REFRESH_STATE', {})
    def delayed(*args, **kwargs):
        with db.SessionLocal.begin() as session:
            session.query(RecoveryAccount).update({'owner_epoch': 2})
        api.PANEL_REFRESH_STATE.clear()
        api.PANEL_REFRESH_STATE.update({'owner': 'B', 'running': True})
        raise RecoveryError('RECOVERY_TOKEN_STALE')
    monkeypatch.setattr(api, 'calculate_fresh_panel_data', delayed)
    monkeypatch.setattr(api, 'record_auto_execution_gate',
        lambda *args, **kwargs: pytest.fail('stale execution status publication'))
    with admitted_worker(monkeypatch):
        context = context_module.capture('panel')
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_STALE'):
            context_module.run(context, api.refresh_panel_cache)
    assert api.PANEL_REFRESH_STATE == {'owner': 'B', 'running': True}
    assert not api.PANEL_REFRESH_LOCK.locked()


def test_stale_engine_error_stops_without_publishing(monkeypatch):
    context_module = context_api()
    import api
    from startup_recovery.runtime import invalidate_token
    monkeypatch.setattr(api, 'ENGINE_RUNTIME_STATE', {'loop_iterations': 0})
    monkeypatch.setattr(api, 'forex_weekend_closed', lambda: False)
    monkeypatch.setattr(api, 'start_ctrader_live_price_stream', lambda: {'ok': True})
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: pytest.fail('stale engine kept running'))
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('management')
        def delayed(**kwargs):
            invalidate_token(token)
            api.ENGINE_RUNTIME_STATE.clear()
            api.ENGINE_RUNTIME_STATE['owner'] = 'B'
            raise RecoveryError('RECOVERY_TOKEN_INVALID')
        monkeypatch.setattr(api, 'refresh_panel_cache', delayed)
        result = context_module.run(context, api.background_fetch)
        assert result == {'ok': False, 'reason': 'RECOVERY_TOKEN_INVALID'}
    assert api.ENGINE_RUNTIME_STATE == {'owner': 'B'}


def test_stale_ingestion_cannot_persist_delayed_provider_result(monkeypatch):
    context_module = context_api()
    import db
    from fundamentals.ingestion import collect_official_provider_data
    from models import EconomicProviderFetch
    from startup_recovery.runtime import invalidate_token
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('ingestion')
        def delayed(*args, **kwargs):
            invalidate_token(token)
            return {'normalized_events': []}
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            context_module.run(context, collect_official_provider_data,
                session_factory=db.SessionLocal, fetchers={'bls': delayed})
        with db.SessionLocal() as session:
            assert session.query(EconomicProviderFetch).count() == 0


def test_stale_provider_cannot_publish_calendar_cache(monkeypatch):
    context_module = context_api()
    from services import news_service
    from types import SimpleNamespace
    from startup_recovery.runtime import invalidate_token
    monkeypatch.setattr(news_service, '_CALENDAR_CACHE', {'events': ['new-owner']})
    monkeypatch.setattr(news_service, 'get_jblanked_api_key', lambda: 'fixture-only')
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('ingestion')
        def response(*args, **kwargs):
            invalidate_token(token)
            return SimpleNamespace(status_code=200, json=lambda: [], text='[]')
        monkeypatch.setattr(news_service.requests, 'get', response)
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            context_module.run(context, news_service.fetch_jblanked_calendar_events, force=True)
    assert news_service._CALENDAR_CACHE == {'events': ['new-owner']}


@pytest.mark.parametrize('engine', ['api', 'bootstrap'])
def test_real_engine_constructor_passes_fixed_context_without_duplicate(monkeypatch, engine):
    import api
    import app_bootstrap
    context_module = context_api()
    from startup_recovery.runtime import caller_token
    targets = []
    class Thread:
        ident = 17
        def __init__(self, *, target, **kwargs): targets.append(target)
        def start(self): pass
        def is_alive(self): return True
    monkeypatch.setattr(api.threading, 'Thread', Thread)
    monkeypatch.setattr(api, 'BACKGROUND_THREAD', None)
    monkeypatch.setattr(api, 'warm_panel_cache_from_persisted_candles', lambda: None)
    monkeypatch.setattr(api, 'start_ctrader_live_price_stream', lambda **kwargs: {'ok': True})
    monkeypatch.setattr(api, 'background_fetch', lambda: (caller_token(), context_module.current()))
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('management')
        def start():
            if engine == 'api': return api.start_background_task(context=context)
            return app_bootstrap._start_forex_background_task(context=context, management_only=True)
        start()
        start()
        assert len(targets) == 1
        assert targets[0]() == (token, context)


def test_price_constructor_does_not_adopt_another_selected_account(monkeypatch):
    import ctrader_connector as connector
    context_module = context_api()
    monkeypatch.setattr(connector, 'LIVE_PRICE_THREAD_STARTED', False)
    monkeypatch.setattr(connector, 'get_ctrader_config', lambda: {'account_id': 'other', 'env': 'demo'})
    monkeypatch.setattr(connector, 'verify_ctrader_account_auth',
        lambda *args, **kwargs: pytest.fail('worker adopted different broker account'))
    with admitted_worker(monkeypatch):
        context = context_module.capture('price')
        with pytest.raises(RecoveryError, match='RECOVERY_ACCOUNT_CONFLICT'):
            connector.start_ctrader_live_price_stream(context=context)


def test_authoritative_database_fault_stickily_revokes_worker(monkeypatch):
    context_module = context_api()
    from startup_recovery import store
    from startup_recovery.runtime import require_worker_admission
    with admitted_worker(monkeypatch):
        context = context_module.capture('management')
        def unavailable(*args, **kwargs):
            raise RecoveryError('RECOVERY_DB_UNAVAILABLE')
        with monkeypatch.context() as patch:
            patch.setattr(store, 'require_owner', unavailable)
            with pytest.raises(RecoveryError, match='RECOVERY_DB_UNAVAILABLE'):
                require_worker_admission(context, 'management')
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            require_worker_admission(context, 'management')


@pytest.mark.parametrize('parent', [False, True])
def test_stale_position_sync_does_not_publish_error_into_successor_state(monkeypatch, parent):
    import api
    context_module = context_api()
    from startup_recovery.runtime import invalidate_token
    monkeypatch.setattr(api, 'sync_ctrader_account_state', lambda: None)
    monkeypatch.setattr(api, 'LIVE_ACCOUNT_STATE', {'connected': True})
    monkeypatch.setattr(api, 'LIVE_POSITION_SYNC_STATUS', {})
    monkeypatch.setattr(api, 'repair_pending_original_protection', lambda: [])
    monkeypatch.setattr(api, '_actionable_panel_plans', lambda panel: [])
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('management')
        def delayed():
            invalidate_token(token)
            api.LIVE_POSITION_SYNC_STATUS.clear()
            api.LIVE_POSITION_SYNC_STATUS['owner'] = 'B'
            raise RecoveryError('RECOVERY_TOKEN_INVALID')
        monkeypatch.setattr(api, 'get_open_positions', delayed)
        with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_INVALID'):
            if parent:
                context_module.run(context, api.refresh_live_panel_meta,
                    {'_meta': {'account_scope': 'CTRADER:DEMO:fixture-worker'}})
            else:
                context_module.run(context, api.sync_live_positions)
    assert api.LIVE_POSITION_SYNC_STATUS == {'owner': 'B'}


@pytest.mark.parametrize('name', ['schedule_panel_cache_refresh', 'schedule_panel_refresh'])
def test_panel_constructor_passes_explicit_context(monkeypatch, name):
    import api
    context_module = context_api()
    targets = []
    class Thread:
        def __init__(self, *, target, kwargs, **rest): targets.append((target, kwargs))
        def start(self): pass
    monkeypatch.setattr(api.threading, 'Thread', Thread)
    monkeypatch.setattr(api, 'PANEL_REFRESH_STATE', {})
    monkeypatch.setattr(api, 'refresh_panel_cache', lambda **kw: context_module.current())
    with admitted_worker(monkeypatch) as token:
        context = context_module.capture('panel')
        getattr(api, name)('test', context=context)
        assert len(targets) == 1
        target, kwargs = targets[0]
        assert target(**kwargs) == context


def test_price_constructor_passes_explicit_context(monkeypatch):
    import ctrader_connector as connector
    context_module = context_api()
    targets = []
    class Thread:
        def __init__(self, *, target, **kwargs): targets.append(target)
        def start(self): pass
    monkeypatch.setattr(connector.threading, 'Thread', Thread)
    monkeypatch.setattr(connector, 'LIVE_PRICE_THREAD_STARTED', False)
    monkeypatch.setattr(connector, 'LIVE_PRICE_THREAD', None)
    monkeypatch.setattr(connector, 'get_ctrader_config', lambda: {'account_id': 'fixture-worker', 'env': 'demo'})
    monkeypatch.setattr(connector, 'verify_ctrader_account_auth', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(connector, 'get_ctrader_account_selection_debug', lambda: {})
    monkeypatch.setattr(connector, 'ctrader_live_price_stream_loop', lambda *, context: (context, context_module.current()))
    with admitted_worker(monkeypatch):
        context = context_module.capture('price')
        connector.start_ctrader_live_price_stream(context=context)
        assert len(targets) == 1
        assert targets[0]() == (context, context)


@pytest.mark.parametrize('function,args', [
    ('set_active_ctrader_account', ('other',)),
    ('clear_active_ctrader_account_selection', ('worker authentication error',)),
])
def test_background_worker_cannot_change_account_selection(monkeypatch, function, args):
    import ctrader_connector as connector
    context_module = context_api()
    monkeypatch.setattr(connector, 'load_ctrader_account_settings',
        lambda: pytest.fail('worker entered account selection mutation'))
    with admitted_worker(monkeypatch):
        context = context_module.capture('price')
        with pytest.raises(RecoveryError, match='RECOVERY_ACCOUNT_SWITCH_FORBIDDEN'):
            context_module.run(context, getattr(connector, function), *args)
