from types import SimpleNamespace
from datetime import datetime, timezone
import pytest


@pytest.fixture
def network():
    class Network:
        calls = []
        is_live = False
        truncated = False
        volume = 1000
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, kind, payload, response):
            self.calls.append((kind, payload))
            common = {'ctidTraderAccountId': 47784297}
            if kind == 2149: return {'ctidTraderAccount':[{'ctidTraderAccountId':47784297, 'isLive': self.is_live}]}
            if kind == 2121: return {**common, 'trader':{'ctidTraderAccountId':47784297, 'accountType':0, 'accessRights':0, 'isLimitedRisk':False}}
            if kind == 2114: return {**common,'symbol':[{'symbolId':1, 'symbolName':'EURUSD'}]}
            if kind == 2116: return {**common,'symbol':[{'symbolId':1,'minVolume': self.volume,'stepVolume':1000,'maxVolume':100000,'digits':5,'slDistance':10,'tpDistance':10,'distanceSetIn':1,'tradingMode':0}]}
            if kind == 2124: return {**common, 'position': [], 'order': []}
            if kind == 2175: return {**common, 'order': [], 'hasMore': self.truncated}
            if kind == 2133: return {**common, 'deal': [], 'hasMore': self.truncated}
            return common
        def quote(self, symbol_id): return {'ctidTraderAccountId':47784297,'symbolId':symbol_id,'bid':110000,'ask':110010,'timestamp':int(datetime.now(timezone.utc).timestamp()*1000)}
    return Network()


def test_pinned_adapter_uses_raw_full_metadata_cents_and_protection(network):
    from services.broker_integration_test_adapter import CTraderTestAdapter
    from services.broker_integration_test_service import TestRequest
    adapter = CTraderTestAdapter(lambda: network)
    evidence = adapter.fresh_preflight(TestRequest('47784297','one','EURUSD',True))
    assert evidence.min_volume == 1000
    row = SimpleNamespace(account_id='47784297', symbol_id=1, volume=1000, reference='bit-unique')
    adapter.submit(row)
    order = [payload for kind, payload in network.calls if kind == 2106][0]
    assert order['volume'] == 1000 and order['ctidTraderAccountId'] == 47784297
    assert order['clientOrderId'] == 'bit-unique' and order['label'] == 'bit-unique'
    assert order['relativeStopLoss'] > 0 and order['relativeTakeProfit'] > 0


@pytest.mark.parametrize('change', [{'is_live':True}, {'is_live':None}, {'volume':None}, {'truncated':True}])
def test_adapter_refuses_missing_authority_or_cleanup(network, change):
    from services.broker_integration_test_adapter import CTraderTestAdapter
    from services.broker_integration_test_service import TestRequest
    for key, value in change.items(): setattr(network, key, value)
    with pytest.raises((ValueError, TypeError)):
        CTraderTestAdapter(lambda: network).fresh_preflight(TestRequest('47784297','one','EURUSD',True))
    assert not any(kind == 2106 for kind, _ in network.calls)


def test_complete_empty_history_is_not_closure(network):
    from services.broker_integration_test_adapter import CTraderTestAdapter
    row = SimpleNamespace(account_id='47784297', symbol_id=1, volume=1000,
        reference='bit-unique', created_at=datetime.now(timezone.utc))
    assert not CTraderTestAdapter(lambda: network).reconcile(row).complete


@pytest.mark.parametrize('change', ['none', 'wrong_reference', 'wrong_side', 'added_volume', 'partial_history', 'wrong_close_volume', 'pending_close'])
def test_reconciliation_requires_exact_open_and_close_evidence(network, change):
    from services.broker_integration_test_adapter import CTraderTestAdapter
    row = SimpleNamespace(account_id='47784297', symbol_id=1, volume=1000,
        reference='bit-unique', created_at=datetime.now(timezone.utc), broker_position_id='18')
    data = {'symbolId':1,'tradeSide':1,'volume':1000,'label':'bit-unique'}
    order = {'orderId':17,'positionId':18,'clientOrderId':'bit-unique','tradeData':data}
    opening = {'dealId':20,'orderId':17,'positionId':18,'symbolId':1,'tradeSide':1,'filledVolume':1000}
    closing = {'dealId':21,'orderId':19,'positionId':18,'symbolId':1,'tradeSide':2,'filledVolume':1000,'closePositionDetail':{'closedVolume':1000}}
    if change == 'wrong_reference': order['clientOrderId'] = 'other'
    if change == 'wrong_side': data['tradeSide'] = 2
    if change == 'added_volume': opening['filledVolume'] = 2000
    if change == 'wrong_close_volume': closing['closePositionDetail']['closedVolume'] = 999
    original = network.request
    def request(kind, payload, expected):
        if kind == 2175: return {'order':[order],'hasMore':change == 'partial_history'}
        if kind == 2133: return {'deal':[opening,closing],'hasMore':False}
        if kind == 2124: return {'position':[],'order':[{'positionId':18}] if change == 'pending_close' else []}
        return original(kind,payload,expected)
    network.request = request
    adapter = CTraderTestAdapter(lambda: network)
    if change == 'partial_history':
        with pytest.raises(ValueError): adapter.reconcile(row)
    else:
        result = adapter.reconcile(row)
        assert result.complete is (change == 'none')
        if result.complete:
            assert result.closed_volume == 1000 and result.open_volume == 0


def test_transport_ignores_unrelated_response_and_enforces_account(monkeypatch):
    import json
    from services.broker_integration_test_adapter import DemoSocket
    transport = object.__new__(DemoSocket)
    sent = []
    transport.sock = SimpleNamespace(settimeout=lambda seconds: None)
    transport.connector = SimpleNamespace(websocket_send_text=lambda sock, msg: sent.append(json.loads(msg)))
    calls = []
    def receive(deadline):
        calls.append(1)
        return {'clientMsgId':'unrelated' if len(calls) == 1 else sent[0]['clientMsgId'],
                'payloadType':2125,'payload':{'ctidTraderAccountId':47784297}}
    transport._receive = receive
    assert transport.request(2124, {'ctidTraderAccountId':47784297}, 2125)['ctidTraderAccountId'] == 47784297
    assert len(calls) == 2
    transport._receive = lambda deadline: {'clientMsgId':sent[-1]['clientMsgId'], 'payloadType':2125,'payload':{'ctidTraderAccountId':47810571}}
    with pytest.raises(ValueError): transport.request(2124, {'ctidTraderAccountId':47784297}, 2125)


def test_transport_deadline_bounds_repeated_heartbeat_or_fragment_reads(monkeypatch):
    import services.broker_integration_test_adapter as module
    now = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    class Socket:
        def settimeout(self, seconds): pass
        def recv(self, count):
            now[0] += 6
            if now[0] > 20:
                raise AssertionError('Read loop escaped total deadline')
            return b'x'
    transport = object.__new__(module.DemoSocket)
    transport.sock = Socket()
    def streaming_reader(sock):
        while True: sock.recv(1)
    transport.connector = SimpleNamespace(websocket_recv_text=streaming_reader)
    with pytest.raises(TimeoutError): transport._receive(10)
