"""Full phase ordering against PostgreSQL; broker/discovery IO is fixture-only."""
import copy
import importlib
from types import SimpleNamespace
import pytest
from test_recovery_store import store_api, db, begin, cutover
from test_recovery_reconciliation import empty, SCOPE
from startup_recovery.types import RecoveryError


def coordinator():
    try:
        return importlib.import_module('startup_recovery.coordinator')
    except ModuleNotFoundError:
        pytest.fail('Explicit recovery coordinator missing')


def dependencies(db, trace):
    d, b = empty()
    class Readers:
        def database(self, scope):
            assert scope == SCOPE
            return copy.deepcopy(d)
        def broker(self, scope):
            assert scope == SCOPE
            return copy.deepcopy(b)
        def authenticate(self, scope):
            trace.append('authenticate')
            return {'account_id': '7', 'environment': 'demo', 'authenticated': True}
    def publish(result, token, snapshot):
        with db() as s:
            from startup_recovery.store import require_owner
            row, _ = require_owner(s, token, lock=False)
            assert row.phase == 'STATE_RECONCILED'
        trace.append('publish')
    return SimpleNamespace(session_factory=db, readers=Readers(), verify_database=lambda: {'schema': '0029'},
        checkpoints=lambda snapshot: {}, publish_reconciled=publish,
        start_management=lambda token: trace.append('management'),
        start_entry_evaluation=lambda token: trace.append('entries'))


def prepare_cutover(store, db):
    from startup_recovery.types import HandoffEvidence
    with db.begin() as s:
        store.begin_attempt(s, SCOPE, 'prepare-only', 'a' * 40)
        store.establish_legacy_cutover(s, SCOPE, HandoffEvidence(
            'operator-termination', 'b' * 64, 'legacy-release', True, True))


def test_first_rollout_cannot_seed_owner_or_start_any_worker(store_api, db):
    api = coordinator()
    trace = []
    outcome = api.recover(dependencies(db, trace), SCOPE, 'new-boot', 'a' * 40)
    assert outcome.ready is False and outcome.reason == 'LEGACY_CUTOVER_REQUIRED'
    assert trace == []
    with db() as s:
        assert store_api.account_state(s, SCOPE).owner_attempt_id is None


def test_recovery_orders_all_phases_and_starts_one_manager(store_api, db):
    api = coordinator()
    prepare_cutover(store_api, db)
    trace = []
    outcome = api.recover(dependencies(db, trace), SCOPE, 'new-boot', 'a' * 40)
    assert outcome.ready is True
    assert outcome.phases == ('BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED',
        'STATE_DISCOVERED', 'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY')
    assert trace == ['authenticate', 'publish', 'management', 'entries']
    with db() as s:
        assert store_api.entries_ready(s, outcome.token)
    second = api.recover(dependencies(db, trace), SCOPE, 'standby', 'a' * 40)
    assert not second.ready and second.reason == 'RECOVERY_OWNER_BUSY'
    assert trace.count('management') == 1


@pytest.mark.parametrize('boundary', ['verify_database', 'authenticate', 'discovery', 'checkpoints', 'publication'])
def test_crash_before_readiness_does_not_start_workers_or_leave_entry_gate_true(store_api, db, boundary):
    api = coordinator()
    prepare_cutover(store_api, db)
    trace = []
    deps = dependencies(db, trace)
    def crash(*args): raise RuntimeError('private upstream detail not returned')
    if boundary == 'verify_database': deps.verify_database = crash
    if boundary == 'authenticate': deps.readers.authenticate = crash
    if boundary == 'discovery': deps.readers.broker = crash
    if boundary == 'checkpoints': deps.checkpoints = crash
    if boundary == 'publication': deps.publish_reconciled = crash
    outcome = api.recover(deps, SCOPE, 'new-boot', 'a' * 40)
    assert not outcome.ready and outcome.reason == 'RECOVERY_STARTUP_FAILED'
    assert 'management' not in trace and 'entries' not in trace
    if boundary == 'checkpoints': assert outcome.phases[-1] == 'STATE_DISCOVERED'
    if boundary == 'publication': assert outcome.phases[-1] == 'STATE_RECONCILED'
    with db() as s:
        assert not store_api.entries_ready(s, outcome.token)


def test_reconciliation_conflict_never_publishes_or_starts_management(store_api, db):
    api = coordinator()
    prepare_cutover(store_api, db)
    trace = []
    deps = dependencies(db, trace)
    d, b = empty()
    b['complete'] = False
    deps.readers.broker = lambda scope: b
    outcome = api.recover(deps, SCOPE, 'new-boot', 'a' * 40)
    assert not outcome.ready and outcome.reason == 'RECOVERY_RECONCILIATION_BLOCKED'
    assert trace == ['authenticate']


def test_unverified_build_identity_cannot_allocate_recovery_authority(db):
    api = coordinator()
    for build in ('latest', '', None):
        with pytest.raises(RecoveryError, match='BUILD_IDENTITY_UNVERIFIED'):
            api.recover(dependencies(db, []), SCOPE, 'boot', build)


def test_legacy_bootstrap_callback_cannot_restore_account_or_start_feed_without_recovery(monkeypatch):
    from unittest.mock import Mock
    import app_bootstrap
    restore, feed, worker = Mock(), Mock(), Mock()
    monkeypatch.setattr(app_bootstrap, '_restore_ctrader_selection_before_market_data', restore)
    monkeypatch.setattr(app_bootstrap.api, 'start_ctrader_live_price_stream', feed)
    monkeypatch.setattr(app_bootstrap.threading, 'Thread', worker)
    monkeypatch.setattr(app_bootstrap, 'verify_execution_protocol', lambda: False)
    app_bootstrap._start_forex_background_task()
    restore.assert_not_called()
    feed.assert_not_called()
    worker.assert_not_called()


def test_legacy_auto_preference_read_does_not_migrate_or_authorize(db, tmp_path):
    from services.auto_trade_state_service import load_state
    from models import RuntimeSetting, AutoTradeStateAudit
    legacy = tmp_path / 'auto_trade_state.json'
    legacy.write_text('{"live_auto_enabled":true,"paper_auto_enabled":true}')
    before = legacy.read_bytes()
    with pytest.raises(RecoveryError, match='LEGACY_CHECKPOINT_RECONCILIATION_REQUIRED'):
        load_state(legacy_path=str(legacy), session_factory=db, force_refresh=True)
    with db() as session:
        assert session.query(RuntimeSetting).count() == 0
        assert session.query(AutoTradeStateAudit).count() == 0
    assert legacy.read_bytes() == before


def test_late_feed_callback_cannot_restore_or_start_without_admission():
    from unittest.mock import Mock
    from services.ctrader_live_stream_startup import start_ctrader_live_stream
    api, restore = Mock(), Mock()
    result = start_ctrader_live_stream(api, restore)
    assert result['ok'] is False
    assert result['reason'] == 'RECOVERY_TOKEN_MISSING'
    restore.assert_not_called()
    api.start_ctrader_live_price_stream.assert_not_called()


def test_direct_api_startup_cannot_start_workers_without_admission(monkeypatch):
    from unittest.mock import Mock
    import api
    warm, feed, worker = Mock(), Mock(), Mock()
    monkeypatch.setattr(api, 'warm_panel_cache_from_persisted_candles', warm)
    monkeypatch.setattr(api, 'start_ctrader_live_price_stream', feed)
    monkeypatch.setattr(api.threading, 'Thread', worker)
    assert api.start_background_task()['ok'] is False
    warm.assert_not_called()
    feed.assert_not_called()
    worker.assert_not_called()


def test_fundamental_startup_keeps_security_guard_but_does_not_start_worker(monkeypatch):
    from unittest.mock import Mock
    import routes.trading as trading
    import services.customer_forex_guard as guards
    scheduler, guard = Mock(), Mock()
    monkeypatch.setattr(trading, 'start_fundamental_ingestion_scheduler', scheduler)
    monkeypatch.setattr(guards, 'install_owner_forex_mutation_guard', guard)
    result = trading.start_fundamental_collection()
    scheduler.assert_not_called()
    guard.assert_called_once()
    assert result == {'ok': False, 'reason': 'RECOVERY_TOKEN_MISSING'}


def test_background_loop_exits_before_any_refresh_when_epoch_is_not_admitted(monkeypatch):
    from unittest.mock import Mock
    import api
    feed, panel, manage = Mock(), Mock(), Mock()
    monkeypatch.setattr(api, 'forex_weekend_closed', lambda: False)
    monkeypatch.setattr(api, 'start_ctrader_live_price_stream', feed)
    monkeypatch.setattr(api, 'refresh_panel_cache', panel)
    monkeypatch.setattr(api, 'refresh_live_panel_meta', manage)
    def cannot_sleep(*args): raise AssertionError('unadmitted loop must exit, not poll')
    monkeypatch.setattr(api.time, 'sleep', cannot_sleep)
    result = api.background_fetch()
    assert result == {'ok': False, 'reason': 'RECOVERY_TOKEN_MISSING'}
    feed.assert_not_called()
    panel.assert_not_called()
    manage.assert_not_called()
