"""Actual read transactions and a mocked wire, never a trading socket."""
import importlib
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from sqlalchemy import text
from test_recovery_store import store_api, db
from test_recovery_reconciliation import SCOPE
from startup_recovery.types import RecoveryError


def readers_api():
    try:
        return importlib.import_module('startup_recovery.readers')
    except ModuleNotFoundError:
        pytest.fail('Explicit read-only recovery adapters missing')


def test_actual_postgres_reader_does_not_mutate_selection_or_lifecycle(db):
    api = readers_api()
    from models import RuntimeSetting
    with db.begin() as s:
        s.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',
            updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc), updated_by='fixture'))
    engine = db.kw['bind']
    reader = api.DatabaseReader(engine, history_from=1000)
    before = reader(SCOPE)
    assert before['selection']['account_id'] == '7'
    assert before['runtime_owners'] == []
    assert before['lifecycles'] == [] and before['submissions'] == []
    assert reader(SCOPE) == before


def test_reader_binds_studio_handoff_pointer_without_changing_enabled_state(db):
    from models import RuntimeSetting, StrategyStudioLiveState
    stamp=datetime(2026,10,1,tzinfo=timezone.utc)
    with db.begin() as session:
        session.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',updated_at=stamp,updated_by='fixture'))
        session.add(StrategyStudioLiveState(owner_id='synthetic',enabled=True,
            enabled_strategy_id='old',enabled_at=stamp,updated_at=stamp))
    reader=readers_api().DatabaseReader(db.kw['bind'],history_from=1000)
    before=reader(SCOPE)
    assert before['studio_live_state']==[dict(owner_id='synthetic',enabled=True,
        enabled_strategy_id='old',enabled_at=stamp.isoformat(),updated_at=stamp.isoformat())]
    assert reader(SCOPE)==before
    with reader.transaction() as c:
        assert c.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
        with pytest.raises(Exception, match='read-only'):
            c.execute(text("UPDATE runtime_settings SET setting_value='bad'"))
    assert reader(SCOPE) == before


@pytest.mark.parametrize('message', [2106, 2108, 2109, 2110, 2111, 2173, 2149])
def test_broker_reader_rejects_mutation_refresh_and_account_switch_before_send(message):
    api = readers_api()
    reader = api.BrokerReader('7', 'demo', {}, deadline=999999999999)
    reader._socket = Mock()
    with pytest.raises(ValueError, match='BROKER_READ_ONLY_VIOLATION'):
        reader._request(message, {})
    reader._socket.send.assert_not_called()


def test_truncated_history_never_becomes_empty_complete_history():
    api = readers_api()
    reader = api.BrokerReader('7', 'demo', {}, deadline=999999999999)
    reader._request = Mock(return_value={'hasMore': True, 'deal': []})
    with pytest.raises(ValueError, match='RECOVERY_HISTORY_INCOMPLETE'):
        reader.deals(1000, 2000)


def test_protocol_volume_and_prices_are_preserved_exactly():
    api = readers_api()
    raw = dict(positionId=42, price='1.10001', stopLoss='1.09501', takeProfit='1.11001',
               tradeData={'symbolId': 1, 'tradeSide': 1, 'volume': 1234500})
    value = api.normalize_position(raw, {1: 'EURUSD'})
    assert value['volume_units'] == '12345'
    assert value['entry'] == '1.10001'
    assert value['sl'] == '1.09501' and value['tp2'] == '1.11001'


def test_complete_deal_keeps_actual_filled_volume_and_millisecond_time():
    api = readers_api()
    reader = api.BrokerReader('7', 'demo', {}, deadline=999999999999)
    reader._request = Mock(return_value={'hasMore': False, 'deal': [dict(
        dealId=123, orderId=456, positionId=42, volume=1000000, filledVolume=500000,
        symbolId=1, tradeSide=2, executionPrice='1.12345',
        executionTimestamp=1790812800123, dealStatus=2,
        closePositionDetail={'closedVolume': 500000})]})
    result = reader.deals(1790812800, 1790812801)
    assert result[0]['volume_units'] == '5000'
    assert result[0]['execution_timestamp'] == '2026-10-01T00:00:00.123000+00:00'
    assert result[0]['symbol_id'] == 1 and result[0]['side'] == 'SELL'
    assert result[0]['execution_price'] == '1.12345'


def test_composed_readers_close_on_failed_history_and_never_log_credentials(db):
    api = readers_api()
    assert hasattr(api, 'ReadAdapters'), 'Explicit recovery read composition missing'
    from models import RuntimeSetting
    with db.begin() as s:
        s.add(RuntimeSetting(setting_name='ctrader_active_account',
            setting_value='{"account_id":"7","env":"demo"}',
            updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc), updated_by='fixture'))
    closed = []
    class FakeReadSocket:
        def __init__(self, account, environment, credentials, deadline):
            assert account == '7' and environment == 'demo'
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
        def snapshot(self, history_from): raise ValueError('RECOVERY_HISTORY_INCOMPLETE')
    readers = api.ReadAdapters(db.kw['bind'], history_from=1000, broker_factory=FakeReadSocket)
    readers.database.credentials = lambda scope: {'access_token': 'private-fixture'}
    with pytest.raises(RecoveryError, match='RECOVERY_BROKER_READ_FAILED'):
        readers.broker(SCOPE)
    assert closed == [True]


def test_authentication_adapter_is_fixed_account_read_only_and_closes(db):
    api = readers_api()
    closed = []
    class AuthOnly:
        def __init__(self, account, environment, credentials, deadline):
            assert (account, environment) == ('7', 'demo')
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
        def snapshot(self, *_): pytest.fail('authentication must not discover or mutate')
    readers = api.ReadAdapters(db.kw['bind'], history_from=1000, broker_factory=AuthOnly)
    readers.database.credentials = lambda scope: {'access_token': 'private-fixture'}
    assert readers.authenticate(SCOPE) == dict(authenticated=True, account_id='7', environment='demo')
    assert closed == [True]


@pytest.fixture
def timed_readers(monkeypatch):
    """Keep adapter/discovery/deadline enforcement real; replace external IO."""
    import copy
    from test_recovery_reconciliation import empty
    api = readers_api()
    clock = {'now': 100.0, 'auth': 1.0, 'snapshot': 11.0,
             'submission': 11.0, 'snapshots': 0, 'closed': 0}
    monkeypatch.setattr(api.time, 'monotonic', lambda: clock['now'])
    database, broker = empty()

    class TimedBroker(api.BrokerReader):
        def spend(self, duration):
            # Two reads in one operation share Reader.remaining(), not two budgets.
            self.remaining()
            clock['now'] += duration
            self.remaining()

        def __enter__(self):
            assert (self.account, self.environment) == ('7', 'demo')
            self.spend(clock['auth'])
            return self

        def __exit__(self, *_):
            clock['closed'] += 1

        def snapshot(self, history_from):
            assert history_from == 1000
            clock['snapshots'] += 1
            self.spend(clock['snapshot'] / 2)
            self.spend(clock['snapshot'] / 2)
            return copy.deepcopy(broker)

        def submission_records(self, start, end):
            assert start == 1000 and end >= start
            self.spend(clock['submission'])
            return []

    class Database:
        def __call__(self, scope):
            assert scope == SCOPE
            return copy.deepcopy(database)

        def credentials(self, scope):
            assert scope == SCOPE
            return {}

    adapters = api.ReadAdapters(None, history_from=1000, broker_factory=TimedBroker)
    adapters.database = Database()
    return adapters, clock


def test_independent_discovery_snapshots_get_fresh_deadlines(timed_readers):
    from startup_recovery.reconcile import discover, reconcile
    from test_recovery_reconciliation import TOKEN
    adapters, clock = timed_readers
    assert adapters.authenticate(SCOPE)['authenticated'] is True
    # Authentication and two healthy ~11s snapshots must not share 20s.
    result = discover(adapters, SCOPE, TOKEN)
    assert reconcile(result, {}).ok
    assert clock['snapshots'] == 2
    assert clock['now'] == 125.0


def test_submission_reads_and_idle_time_do_not_spend_next_operation_budget(timed_readers):
    adapters, clock = timed_readers
    clock['now'] += 30
    stamp = datetime.fromtimestamp(1000, timezone.utc)
    assert adapters.submissions(SCOPE, stamp) == {'ok': True, 'complete': True, 'records': []}
    assert adapters.submissions(SCOPE, stamp)['complete'] is True
    assert adapters.authenticate(SCOPE)['authenticated'] is True
    assert adapters.broker(SCOPE)['history_complete'] is True


@pytest.mark.parametrize('operation', ['authenticate', 'submissions', 'broker'])
def test_one_broker_operation_cannot_renew_its_twenty_second_budget(timed_readers, operation):
    adapters, clock = timed_readers
    clock[{'authenticate': 'auth', 'submissions': 'submission', 'broker': 'snapshot'}[operation]] = 21
    args = (SCOPE, datetime.fromtimestamp(1000, timezone.utc)) if operation == 'submissions' else (SCOPE,)
    with pytest.raises(RecoveryError, match='^RECOVERY_DISCOVERY_TIMEOUT$'):
        getattr(adapters, operation)(*args)
