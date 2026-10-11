"""Production writers must not rewrite legacy/default state before admission."""
import importlib
import pytest
from test_recovery_store import store_api, db


def test_visits_production_list_publishes_without_rewriting_legacy(store_api, db, tmp_path):
    import json
    from test_recovery_checkpoint_publication import admitted, envelope
    from startup_recovery.checkpoint_store import RuntimeWriter, load
    token = admitted(store_api, db)
    path = tmp_path / 'visits.json'
    path.write_text('[]')
    writer = RuntimeWriter(db, token, {'visits': (path, json.loads(envelope(token).raw)['identity'], [])}, 'd' * 64)
    payload = [{'time': 1790812800.125, 'visitor_id': 'visitor', 'country': 'Canada'}]
    writer.write(path, 'visits', payload)
    assert load(db, token.scope, tmp_path, 'visits').payload == payload
    reloaded = writer.read(path, 'visits')
    reloaded.append({'time': 1790812801.125, 'visitor_id': 'another', 'country': 'Local'})
    writer.write(path, 'visits', reloaded)
    assert load(db, token.scope, tmp_path, 'visits').payload == payload + [reloaded[-1]]
    assert path.read_text() == '[]'


def test_unchanged_numeric_legacy_read_cannot_be_laundered_by_serialization(store_api, db, tmp_path):
    import json
    from test_recovery_checkpoint_publication import admitted, envelope
    from startup_recovery.checkpoint_store import RuntimeWriter
    token = admitted(store_api, db)
    path = tmp_path / 'settings.json'
    path.write_text('{"risk":1.25}')
    writer = RuntimeWriter(db, token, {'app_settings': (
        path, json.loads(envelope(token).raw)['identity'], {'risk': 1.25})}, 'd' * 64)
    same_payload = writer.read(path, 'app_settings')
    assert writer.write(path, 'app_settings', same_payload) is None
    assert path.read_text() == '{"risk":1.25}'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('payload', [{}, [None], [{'time': -1, 'visitor_id': 'v', 'country': 'Canada'}],
    [{'time': 1, 'visitor_id': None, 'country': 'Canada'}],
    [{'time': '1', 'visitor_id': 'v', 'country': 'Canada'}]])
def test_visits_invalid_shape_never_publishes(payload):
    from startup_recovery.publication import validate_produced_checkpoint
    from startup_recovery.types import RecoveryError
    with pytest.raises(RecoveryError):
        validate_produced_checkpoint('visits', payload, {})


def test_approved_directories_only_no_default_files(tmp_path, monkeypatch):
    import paths
    for name, relative in [('DATA_DIR', 'data'), ('DATABASE_DIR', 'database'),
                           ('CACHE_DIR', 'cache'), ('CANDLE_CACHE_DIR', 'cache/candle_cache')]:
        monkeypatch.setattr(paths, name, tmp_path / relative)
    paths.ensure_runtime_dirs()
    paths.ensure_runtime_dirs()
    assert {str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')} == {
        'data', 'database', 'cache', 'cache/candle_cache'}
    assert all(p.is_dir() for p in tmp_path.rglob('*'))


def test_directory_provisioning_does_not_follow_symlink(tmp_path, monkeypatch):
    import paths
    from startup_recovery.types import RecoveryError
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'linked'
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(paths, 'DATA_DIR', link / 'data')
    with pytest.raises(RecoveryError, match='CHECKPOINT_UNSAFE_PATH'):
        paths.ensure_runtime_dirs()
    assert list(outside.iterdir()) == []


def test_watch_transition_requires_admission_and_preserves_evidence_on_failed_commit(store_api, db, tmp_path, monkeypatch):
    import json
    import brain
    from strategies import shared, strict_trader
    from startup_recovery import checkpoint_store as storage
    from startup_recovery.types import RecoveryError
    from test_recovery_checkpoint_publication import admitted, envelope
    token = admitted(store_api, db)
    path = tmp_path / 'watch.json'
    original = {'EURUSD': {'symbol': 'EURUSD', 'status': strict_trader.BLOCKED_BREAKOUT_STATUS},
                'XAUUSD': {'symbol': 'XAUUSD', 'status': 'PENDING'}}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', path)
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH', original.copy())
    with pytest.raises(RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        brain._clear_legacy_consolidation_blocked_watches()
    assert shared.FIFTEEN_M_SWING_WATCH == original
    writer = storage.RuntimeWriter(db, token, {'fifteen_m_swing_watch': (
        path, json.loads(envelope(token).raw)['identity'], original)}, 'd' * 64)
    with storage.checkpoint_producer(writer):
        brain._clear_legacy_consolidation_blocked_watches()
        assert shared.FIFTEEN_M_SWING_WATCH == {'XAUUSD': original['XAUUSD']}
        assert storage.load(db, token.scope, tmp_path, 'fifteen_m_swing_watch').payload == shared.FIFTEEN_M_SWING_WATCH
    assert path.read_bytes() == before


def test_failed_watch_publication_never_removes_runtime_or_candidate_evidence(store_api, db, tmp_path, monkeypatch):
    import json
    import brain
    from strategies import shared, strict_trader
    from startup_recovery import checkpoint_store as storage
    from test_recovery_checkpoint_publication import admitted, envelope
    token = admitted(store_api, db)
    path = tmp_path / 'watch.json'
    original = {'EURUSD': {'status': strict_trader.BLOCKED_BREAKOUT_STATUS}}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', path)
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH', original.copy())
    writer = storage.RuntimeWriter(db, token, {'fifteen_m_swing_watch': (
        path, json.loads(envelope(token).raw)['identity'], original)}, 'd' * 64)
    def fail(*args, **kwargs): raise OSError('durability failure')
    monkeypatch.setattr(storage, 'publish', fail)
    with storage.checkpoint_producer(writer), pytest.raises(OSError):
        brain._clear_legacy_consolidation_blocked_watches()
    assert path.read_bytes() == before
    assert shared.FIFTEEN_M_SWING_WATCH == original


def test_first_admitted_write_provisions_missing_parent_only(store_api, db, tmp_path):
    import json
    from test_recovery_checkpoint_publication import admitted, envelope
    from startup_recovery.checkpoint_store import RuntimeWriter, load
    token = admitted(store_api, db)
    path = tmp_path / 'approved' / 'state' / 'visits.json'
    writer = RuntimeWriter(db, token, {'visits': (path, json.loads(envelope(token).raw)['identity'], [])},
                           'd' * 64, absent_kinds={'visits'})
    assert writer.read(path, 'visits') is None
    assert not path.parent.exists()
    writer.write(path, 'visits', [{'time': 1, 'visitor_id': 'v', 'country': 'Local'}])
    assert not path.exists()
    assert load(db, token.scope, path.parent, 'visits').payload[0]['visitor_id'] == 'v'


def test_watch_does_not_publish_memory_after_epoch_is_revoked(store_api, db, tmp_path, monkeypatch):
    import json
    import brain
    from strategies import shared, strict_trader
    from startup_recovery import checkpoint_store as storage
    from startup_recovery.operation_context import RecoveryOperationContext, bound
    from startup_recovery.types import RecoveryError
    from test_recovery_checkpoint_publication import admitted, envelope
    import db as database
    monkeypatch.setattr(database, 'SessionLocal', db)
    token = admitted(store_api, db)
    path = tmp_path / 'watch.json'
    original = {'EURUSD': {'status': strict_trader.BLOCKED_BREAKOUT_STATUS}}
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH_FILE', path)
    monkeypatch.setattr(shared, 'FIFTEEN_M_SWING_WATCH', original.copy())
    writer = storage.RuntimeWriter(db, token, {'fifteen_m_swing_watch': (
        path, json.loads(envelope(token).raw)['identity'], original)}, 'd' * 64)
    publish = storage.publish
    def revoke_after_commit(*args, **kwargs):
        result = publish(*args, **kwargs)
        from startup_recovery.coordinator import block
        block(db, token, 'revoked-before-local-publication')
        return result
    monkeypatch.setattr(storage, 'publish', revoke_after_commit)
    with bound(RecoveryOperationContext(token, 'request', 'd' * 64)), storage.checkpoint_producer(writer):
        with pytest.raises(RecoveryError):
            brain._clear_legacy_consolidation_blocked_watches()
    assert shared.FIFTEEN_M_SWING_WATCH == original


@pytest.mark.parametrize('module,path_name,writer', [
    ('api', 'LIVE_BACKUP_FILE', 'save_live_backup'),
    ('api', 'LIVE_MONTHLY_HISTORY_FILE', 'save_live_monthly_history_cache'),
    ('strategies.shared', 'FINAL_SIGNAL_HOLD_FILE', 'save_final_signal_hold'),
    ('strategies.shared', 'FIFTEEN_M_SWING_WATCH_FILE', 'save_fifteen_m_swing_watch'),
    ('strategies.shared', 'PAPER_BACKUP_FILE', 'save_paper_backup'),
])
def test_existing_writer_cannot_publish_before_explicit_admission(tmp_path, monkeypatch, module, path_name, writer):
    target = tmp_path / 'legacy.json'
    target.write_text('{"legacy":"retained"}')
    imported = importlib.import_module(module)
    monkeypatch.setattr(imported, path_name, str(target))
    try:
        getattr(imported, writer)()
    except Exception as exc:
        from startup_recovery.types import RecoveryError
        assert isinstance(exc, RecoveryError)
    assert target.read_text() == '{"legacy":"retained"}'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('which', ['news', 'settings'])
def test_lazy_settings_or_news_write_requires_admission(tmp_path, which):
    from startup_recovery.types import RecoveryError
    target = tmp_path / 'state.json'
    if which == 'news':
        from services.news_trading import save_state
        call = lambda: save_state({'opportunities': {}}, path=target)
    else:
        from services.settings_service import _write_json
        call = lambda: _write_json(target, {'risk': {}})
    with pytest.raises(RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        call()
    assert not target.exists()


@pytest.mark.parametrize('which', ['news', 'settings'])
def test_lazy_execution_reader_uses_admitted_state_not_raw_legacy(tmp_path, monkeypatch, which):
    from startup_recovery import checkpoint_store
    target = tmp_path / 'state.json'
    target.write_text('{"legacy":"must-not-load"}')
    calls = []
    def admitted(path, kind):
        calls.append(kind)
        return {'admitted': True}
    monkeypatch.setattr(checkpoint_store, 'read_runtime', admitted)
    if which == 'news':
        from services.news_trading import load_state
        result = load_state(target)
    else:
        from services.settings_service import _read_json
        result = _read_json(target, {})
    assert result.get('admitted') is True
    assert 'legacy' not in result
    assert len(calls) == 1
    assert target.read_text() == '{"legacy":"must-not-load"}'


@pytest.mark.parametrize('module,builder,saver', [
    ('api', 'snapshot_live_backup', 'save_live_backup'),
    ('strategies.shared', 'snapshot_paper_backup', 'save_paper_backup'),
])
def test_checkpoint_snapshot_builder_is_same_payload_as_existing_writer(monkeypatch, module, builder, saver):
    from startup_recovery import checkpoint_store
    imported = importlib.import_module(module)
    assert callable(getattr(imported, builder, None)), 'Explicit pure checkpoint payload builder missing'
    before = getattr(imported, builder)()
    captured = []
    monkeypatch.setattr(checkpoint_store, 'write_runtime', lambda path, kind, payload: captured.append(payload))
    getattr(imported, saver)()
    assert captured == [before]
    captured[0]['test_local_mutation'] = True
    assert 'test_local_mutation' not in getattr(imported, builder)()


@pytest.mark.parametrize('which', ['account_preferences', 'market_source', 'visits'])
def test_remaining_runtime_writers_cannot_modify_unadmitted_files(tmp_path, monkeypatch, which):
    target = tmp_path / 'legacy.json'
    target.write_text('{"legacy":"retained"}')
    if which == 'account_preferences':
        import ctrader_connector as module
        monkeypatch.setattr(module, 'CTRADER_ACCOUNTS_PATH', target)
        call = lambda: module.save_ctrader_account_settings({})
    elif which == 'market_source':
        from strategies import shared as module
        monkeypatch.setattr(module, 'MARKET_DATA_SOURCE_FILE', target)
        call = lambda: module.save_market_data_source('ctrader')
    else:
        import api as module
        monkeypatch.setattr(module, 'VISITS_FILE', target)
        call = lambda: module.save_visits({})
    from startup_recovery.types import RecoveryError
    with pytest.raises(RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        call()
    assert target.read_text() == '{"legacy":"retained"}'


@pytest.mark.parametrize('which', ['account_preferences', 'market_source', 'visits'])
def test_remaining_readers_cannot_reimport_legacy_after_admission(tmp_path, monkeypatch, which):
    from startup_recovery import checkpoint_store
    from startup_recovery.types import RecoveryError
    target = tmp_path / 'legacy.json'
    target.write_text('{"legacy":"retained"}')
    if which == 'account_preferences':
        import ctrader_connector as module
        monkeypatch.setattr(module, 'CTRADER_ACCOUNTS_PATH', target)
        call = module.load_ctrader_account_settings
    elif which == 'market_source':
        from strategies import shared as module
        monkeypatch.setattr(module, 'MARKET_DATA_SOURCE_FILE', target)
        call = module.load_saved_market_data_source
    else:
        import api as module
        monkeypatch.setattr(module, 'VISITS_FILE', target)
        call = module.load_visits
    with pytest.raises(RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        call()


@pytest.mark.parametrize('name,path_name', [
    ('load_final_signal_hold', 'FINAL_SIGNAL_HOLD_FILE'),
    ('load_fifteen_m_swing_watch', 'FIFTEEN_M_SWING_WATCH_FILE'),
])
def test_lazy_signal_restore_cannot_read_unadmitted_checkpoint(name, path_name, tmp_path, monkeypatch):
    from strategies import shared
    from startup_recovery.types import RecoveryError
    path = tmp_path / 'legacy.json'
    path.write_text('{}')
    monkeypatch.setattr(shared, path_name, path)
    with pytest.raises(RecoveryError, match='CHECKPOINT_PRODUCER_NOT_ADMITTED'):
        getattr(shared, name)()


def test_old_live_loader_cannot_launder_or_publish_legacy_checkpoint(tmp_path, monkeypatch):
    import api
    import copy
    from startup_recovery.types import RecoveryError
    path = tmp_path / 'legacy.json'
    raw = '{"live_active_orders":{"EURUSD":{"position_id":"42"}}}'
    path.write_text(raw)
    monkeypatch.setattr(api, 'LIVE_BACKUP_FILE', path)
    before = copy.deepcopy(api.LIVE_ACTIVE_ORDERS)
    with pytest.raises(RecoveryError, match='RECOVERY_USE_COORDINATOR'):
        api.load_live_backup()
    assert path.read_text() == raw
    assert api.LIVE_ACTIVE_ORDERS == before
