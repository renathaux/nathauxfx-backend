from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import IndicatorEvent, IndicatorEventLifecycle, TradeSubmissionAttempt


@pytest.fixture
def rig(tmp_path):
    from services.broker_integration_test_service import BrokerIntegrationTestService, TestRequest, Preflight, Reconciliation
    from models import BrokerIntegrationTestSubmission
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    class Broker:
        sends = 0
        closes = 0
        ambiguous = False
        unavailable = False
        mismatch = False
        preflight = Preflight('47784297', False, 'EURUSD', 1, 1000, 1000, 100000, 1.1, 1.1001, True)

        def fresh_preflight(self, request):
            return self.preflight

        def submit(self, row):
            with factory() as session:
                persisted = session.get(BrokerIntegrationTestSubmission, row.test_id)
                assert persisted.request_started_at is not None
                assert persisted.reference == row.reference
            self.sends += 1
            if self.ambiguous:
                raise TimeoutError('sensitive broker payload must not persist')

        def reconcile(self, row):
            if self.unavailable:
                return Reconciliation(complete=False)
            return Reconciliation(complete=True, account_id='47784297', symbol_id=1,
                side='BUY', reference='unrelated' if self.mismatch else row.reference,
                order_id='17', position_id='18', volume=1000,
                open_volume=0 if self.closes else 1000,
                closed_volume=1000 if self.closes else 0)

        def close(self, row, evidence):
            assert evidence.position_id == '18' and evidence.open_volume == 1000
            self.closes += 1

    broker = Broker()
    return BrokerIntegrationTestService(factory, broker), broker, factory, TestRequest('47784297', 'roundtrip-1', 'EURUSD', True)


@pytest.mark.parametrize('change', [{'account_id':'47810571'}, {'account_id':''}, {'confirmed':False}, {'test_id':''}, {'symbol':'XAUUSD'}])
def test_invalid_request_never_sends(rig, change):
    service, broker, factory, request = rig
    with pytest.raises(ValueError):
        service.run(replace(request, **change))
    assert broker.sends == 0


@pytest.mark.parametrize('change', [{'is_live':True}, {'is_live':None}, {'account_id':'47810571'}, {'cleanup_ready':False}, {'min_volume':0}])
def test_preflight_fails_closed(rig, change):
    service, broker, factory, request = rig
    broker.preflight = replace(broker.preflight, **change)
    with pytest.raises(ValueError):
        service.run(request)
    assert broker.sends == 0


def test_roundtrip_commits_before_send_and_duplicate_is_read_only(rig):
    service, broker, factory, request = rig
    assert service.run(request)['state'] == 'CLOSED'
    assert service.run(request)['state'] == 'CLOSED'
    assert broker.sends == 1 and broker.closes == 1
    result = service.run(request)
    assert result['open_evidence']['open_volume'] == 1000
    assert result['duplicate_evidence']['open_volume'] == 1000
    assert result['reconciliation_evidence']['closed_volume'] == 1000
    with factory() as session:
        for model in (IndicatorEvent, IndicatorEventLifecycle, TradeSubmissionAttempt):
            assert session.query(model).count() == 0


def test_ambiguous_open_is_never_resent_after_restart(rig):
    from services.broker_integration_test_service import BrokerIntegrationTestService
    service, broker, factory, request = rig
    broker.ambiguous = True
    assert service.run(request)['state'] == 'NEEDS_RECOVERY'
    restored = BrokerIntegrationTestService(factory, broker)
    assert restored.run(request, recover=True)['state'] == 'CLOSED'
    assert broker.sends == 1


def test_unresolved_fence_blocks_normal_only_target_account_and_survives_restart(rig):
    from services.account_execution_coordination import run_normal_submission, ExecutionFenced
    from services.broker_integration_test_service import BrokerIntegrationTestService
    service, broker, factory, request = rig
    broker.unavailable = True
    assert service.run(request)['state'] == 'NEEDS_RECOVERY'
    called = []
    with pytest.raises(ExecutionFenced):
        run_normal_submission(factory, '47784297', lambda: called.append('wrong'))
    assert run_normal_submission(factory, '47810571', lambda: 'unchanged') == 'unchanged'
    restored = BrokerIntegrationTestService(factory, broker)
    with pytest.raises(ExecutionFenced):
        restored.run(replace(request, test_id='another'))
    broker.unavailable = False
    assert restored.run(request, recover=True)['state'] == 'CLOSED'
    run_normal_submission(factory, '47784297', lambda: called.append('released'))
    assert called == ['released']


def test_mismatched_position_never_closed(rig):
    service, broker, factory, request = rig
    broker.mismatch = True
    assert service.run(request)['state'] == 'NEEDS_RECOVERY'
    assert broker.closes == 0


def test_preflight_and_unknown_recovery_never_write(rig):
    from models import BrokerIntegrationTestSubmission
    service, broker, factory, request = rig
    assert service.preflight(request)['account_id'] == '47784297'
    with pytest.raises(ValueError):
        service.run(request, recover=True)
    with factory() as session:
        assert session.query(BrokerIntegrationTestSubmission).count() == 0


def test_lock_is_nonblocking_and_normal_callback_cannot_overlap_test(rig):
    from services.account_execution_coordination import account_lock, run_normal_submission, ExecutionFenced
    _, _, factory, _ = rig
    with account_lock(factory, '47784297'):
        with pytest.raises(ExecutionFenced):
            run_normal_submission(factory, '47784297', lambda: pytest.fail('overlapped'))


def test_only_persisted_test_identity_is_excluded_from_position_and_history(rig):
    from services.account_execution_coordination import exclude_test_positions
    service, broker, factory, request = rig
    result = service.run(request)
    positions = [{'position_id':'other','raw':{'tradeData':{'label':'unrelated'}}},
                 {'position_id':'18'}, {'raw':{'label':result['reference']}}]
    assert exclude_test_positions(factory, '47784297', positions) == positions[:1]
    assert exclude_test_positions(factory, '47810571', positions) == positions


def test_normal_execution_context_pins_account_across_selection_switch(rig):
    from services.account_execution_coordination import run_normal_submission, assert_execution_account, ExecutionFenced
    _, _, factory, _ = rig
    with pytest.raises(ExecutionFenced):
        run_normal_submission(factory, '47810571', lambda: assert_execution_account('47784297'))
    run_normal_submission(factory, '47784297', lambda: assert_execution_account('47784297'))


def test_api_normal_wrapper_fails_closed_without_mutating_auto_preference(rig, monkeypatch):
    import api
    import db
    service, broker, factory, request = rig
    broker.unavailable = True
    service.run(request)
    monkeypatch.setattr(db, 'SessionLocal', factory)
    monkeypatch.setattr(api, 'get_active_ctrader_account_id', lambda: '47784297')
    monkeypatch.setattr(api, '_execute_live_order_core_impl', lambda *a, **kw: {'ok':True})
    original = dict(api.LIVE_AUTO_TRADE_ENABLED)
    assert not api.execute_live_order_core({})['ok']
    assert api.LIVE_AUTO_TRADE_ENABLED == original
    monkeypatch.setattr(api, 'get_active_ctrader_account_id', lambda: '47810571')
    assert api.execute_live_order_core({})['ok']


def test_connector_double_normalized_position_excluded_before_position_id_persisted(rig):
    from ctrader_connector import normalize_ctrader_position, normalize_positions
    from services.account_execution_coordination import exclude_test_positions
    service, broker, factory, request = rig
    broker.unavailable = True
    result = service.run(request)
    raw = {'positionId':18,'tradeData':{'symbolId':1,'tradeSide':1,'volume':1000,'label':result['reference']}}
    normalized = normalize_positions([normalize_ctrader_position(raw, {'1':'EURUSD'})])
    assert exclude_test_positions(factory, '47784297', normalized) == []


def test_migration_matches_model_and_preserves_existing_tables(tmp_path):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from models import BrokerIntegrationTestSubmission
    path = Path(__file__).parents[1] / 'migrations/versions/20260916_0022_broker_integration_test.py'
    spec = importlib.util.spec_from_file_location('demo_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE indicator_events (event_id TEXT PRIMARY KEY)'))
        connection.execute(text("INSERT INTO indicator_events VALUES ('untouched')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        actual = {c['name'] for c in inspect(connection).get_columns('broker_integration_test_submissions')}
        assert actual == set(BrokerIntegrationTestSubmission.__table__.columns.keys())
        assert connection.execute(text('SELECT event_id FROM indicator_events')).scalar() == 'untouched'
        migration.downgrade()
        assert 'broker_integration_test_submissions' not in inspect(connection).get_table_names()


def test_cli_preflight_recover_and_roundtrip_use_same_durable_service(rig, monkeypatch, capsys):
    import db
    from scripts.run_broker_integration_test import main
    import services.broker_integration_test_adapter as adapter
    from models import BrokerIntegrationTestSubmission
    service, broker, factory, request = rig
    monkeypatch.setattr(db, 'SessionLocal', factory)
    monkeypatch.setattr(adapter, 'CTraderTestAdapter', lambda: broker)
    args = ['--account-id','47784297','--test-id','cli-one','--symbol','EURUSD','--confirm-demo-broker-test']
    assert main(args + ['--preflight']) == 0
    with factory() as session:
        assert session.query(BrokerIntegrationTestSubmission).count() == 0
    assert main(args) == 0
    assert main(args + ['--recover']) == 0
    assert broker.sends == 1


@pytest.mark.parametrize('crash', [False, True])
def test_ambiguous_close_restart_never_resends_close_on_still_open_snapshot(rig, crash):
    from services.broker_integration_test_service import BrokerIntegrationTestService
    from models import BrokerIntegrationTestSubmission
    service, broker, factory, request = rig
    calls = []
    def ambiguous_close(row, evidence):
        calls.append(evidence.position_id)
        if crash:
            raise SystemExit('process died after durable close marker')
        raise TimeoutError('secret access token must not be emitted')
    broker.close = ambiguous_close
    if crash:
        with pytest.raises(SystemExit):
            service.run(request)
    else:
        assert service.run(request)['state'] == 'NEEDS_RECOVERY'
    restored = BrokerIntegrationTestService(factory, broker)
    result = restored.run(request, recover=True)
    assert calls == ['18']
    assert result['state'] == 'NEEDS_RECOVERY'
    assert result['last_error'] == 'UNRESOLVED_CLOSE'
    with factory() as session:
        assert session.get(BrokerIntegrationTestSubmission, request.test_id).unresolved_account == '47784297'
    broker.closes = 1  # Later authoritative filled closing deal, no second close.
    assert restored.run(request, recover=True)['state'] == 'CLOSED'
    assert calls == ['18']


def test_label_less_closed_deal_is_not_exposed_while_open_identity_unresolved(rig):
    from ctrader_connector import normalize_ctrader_closed_deal
    from services.account_execution_coordination import exclude_test_closed_deals, ExecutionFenced
    service, broker, factory, request = rig
    broker.ambiguous = True
    service.run(request)
    deal = {'dealId':20, 'orderId':19, 'positionId':18, 'symbolId':1,
            'tradeSide':2, 'filledVolume':1000, 'executionPrice':1.101,
            'closePositionDetail':{'closedVolume':1000,'entryPrice':1.1,'grossProfit':0,'swap':0,'commission':1,'balance':10000}}
    history = [normalize_ctrader_closed_deal(deal, {'1':'EURUSD'})]
    with pytest.raises(ExecutionFenced):
        exclude_test_closed_deals(factory, '47784297', history)
    assert exclude_test_closed_deals(factory, '47810571', history) == history
    broker.closes = 1
    assert service.run(request, recover=True)['state'] == 'CLOSED'
    assert exclude_test_closed_deals(factory, '47784297', history) == []


@pytest.mark.parametrize('change,code', [({'is_live':True},'DEMO_PROOF_REQUIRED'),
    ({'min_volume':0},'INVALID_BROKER_VOLUME'), ({'cleanup_ready':False},'CLEANUP_UNAVAILABLE')])
def test_cli_returns_known_safe_blocker_codes(rig, monkeypatch, capsys, change, code):
    import json
    import db
    from scripts.run_broker_integration_test import main
    import services.broker_integration_test_adapter as adapter
    _, broker, factory, _ = rig
    broker.preflight = replace(broker.preflight, **change)
    monkeypatch.setattr(db, 'SessionLocal', factory)
    monkeypatch.setattr(adapter, 'CTraderTestAdapter', lambda: broker)
    args = ['--account-id','47784297','--test-id','codes','--symbol','EURUSD','--confirm-demo-broker-test','--preflight']
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == code


def test_unknown_exception_secret_is_never_persisted(rig):
    service, broker, _, request = rig
    broker.ambiguous = True
    result = service.run(request)
    assert result['last_error'] == 'BROKER_IO_FAILURE'
    assert 'sensitive broker payload' not in str(result)
