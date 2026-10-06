"""The real server startup obtains identity only from server-side DB/build sources."""
from datetime import datetime, timezone
import importlib
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from test_recovery_store import store_api, db


def bootstrap():
    try:
        return importlib.import_module('startup_recovery.bootstrap')
    except ModuleNotFoundError:
        pytest.fail('Server-side recovery bootstrap missing')


def test_server_first_rollout_records_cutover_block_without_broker_or_workers(db, monkeypatch):
    from models import RuntimeSetting, RecoveryAccount
    from test_recovery_startup import dependencies
    from live_integrity import build_identity
    with db.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',
            updated_at=datetime.now(timezone.utc), updated_by='fixture'))
    build = dict(backend_git_sha='a' * 40, source_tree='b' * 40,
                 build_identity='c' * 64, clean_source=True)
    monkeypatch.setattr(build_identity, 'capture', lambda: build)
    trace = []
    deps = dependencies(db, trace)
    factory = Mock(return_value=deps)
    api = SimpleNamespace(ENGINE_RUNTIME_STATE={})
    outcome = bootstrap().start(api, engine=db.kw['bind'], session_factory=db,
                                dependencies_factory=factory)
    assert not outcome.ready and outcome.reason == 'LEGACY_CUTOVER_REQUIRED'
    assert trace == []
    assert api.ENGINE_RUNTIME_STATE['recovery']['ready'] is False
    assert api.ENGINE_RUNTIME_STATE['recovery']['build_identity'] == build
    with db() as session:
        account = session.query(RecoveryAccount).one()
        assert account.owner_attempt_id is None
        assert account.phase == 'LEGACY_CUTOVER_REQUIRED'


def test_server_unverified_build_stops_before_any_db_or_factory(monkeypatch):
    from live_integrity import build_identity
    def unverified(): raise ValueError('BUILD_IDENTITY_UNVERIFIED')
    monkeypatch.setattr(build_identity, 'capture', unverified)
    engine, factory = Mock(), Mock()
    api = SimpleNamespace(ENGINE_RUNTIME_STATE={})
    outcome = bootstrap().start(api, engine=engine, session_factory=Mock(),
                                dependencies_factory=factory)
    assert outcome.reason == 'BUILD_IDENTITY_UNVERIFIED' and not outcome.ready
    engine.connect.assert_not_called()
    factory.assert_not_called()


def test_real_server_adapter_first_release_never_restores_or_contacts_broker(db, monkeypatch):
    import copy
    import api
    from models import RuntimeSetting
    from live_integrity import build_identity
    from startup_recovery.readers import ReadAdapters
    with db.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',
            updated_at=datetime.now(timezone.utc), updated_by='fixture'))
    monkeypatch.setattr(build_identity, 'capture', lambda: dict(
        backend_git_sha='a' * 40, source_tree='b' * 40, build_identity='c' * 64, clean_source=True))
    read = Mock(side_effect=AssertionError('Legacy cutover must precede broker IO'))
    monkeypatch.setattr(ReadAdapters, 'authenticate', read)
    monkeypatch.setattr(ReadAdapters, 'broker', read)
    before = copy.deepcopy(api.LIVE_ACTIVE_ORDERS)
    outcome = bootstrap().start(api, engine=db.kw['bind'], session_factory=db)
    assert outcome.reason == 'LEGACY_CUTOVER_REQUIRED'
    assert api.LIVE_ACTIVE_ORDERS == before
    read.assert_not_called()
