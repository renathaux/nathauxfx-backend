"""Pinned DEMO protocol adapter; no connector selection or strategy side effects.

Protocol units: https://help.ctrader.com/open-api/model-messages/
Requests: https://help.ctrader.com/open-api/messages/
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
import time
import uuid

from services.broker_integration_test_service import Preflight, Reconciliation, BrokerIntegrationTestService
from services.broker_integration_test_errors import BlockerCode as Code, BrokerTestBlocked

ACCOUNT = 47784297


class _DeadlineSocket:
    """Enforce the same deadline inside websocket ping/fragment loops."""
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def _budget(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Broker deadline')
        self.sock.settimeout(min(remaining, 8))

    def recv(self, count):
        self._budget()
        return self.sock.recv(count)

    def sendall(self, data):
        self._budget()
        return self.sock.sendall(data)


class DemoSocket:
    """Fresh, bounded, correlated connection; token refresh is intentionally absent."""
    def __init__(self):
        import ctrader_connector as connector
        from services.ctrader_token_service import load_tokens
        self.connector = connector
        self.token = load_tokens().get('access_token') or os.getenv('CTRADER_ACCESS_TOKEN')
        self.sock = None
        if not self.token or not os.getenv('CTRADER_CLIENT_ID') or not os.getenv('CTRADER_CLIENT_SECRET'):
            raise BrokerTestBlocked(Code.CREDENTIALS_UNAVAILABLE)

    def __enter__(self):
        self.sock = self.connector.open_ctrader_json_socket('demo.ctraderapi.com', 5036)
        try:
            self.request(2100, {'clientId': os.environ['CTRADER_CLIENT_ID'],
                              'clientSecret': os.environ['CTRADER_CLIENT_SECRET']}, 2101)
            self.prove_demo()
            self.request(2102, {'ctidTraderAccountId': ACCOUNT, 'accessToken': self.token}, 2103)
            return self
        except BaseException:
            self.sock.close()
            raise

    def __exit__(self, *args):
        self.sock.close()

    def prove_demo(self):
        result = self.request(2149, {'accessToken': self.token}, 2150)
        accounts = [a for a in result.get('ctidTraderAccount', []) if str(a.get('ctidTraderAccountId')) == str(ACCOUNT)]
        if len(accounts) != 1 or accounts[0].get('isLive') is not False:
            raise BrokerTestBlocked(Code.DEMO_PROOF_REQUIRED)

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Broker deadline')
        self.sock.settimeout(min(remaining, 8))
        raw = self.connector.websocket_recv_text(_DeadlineSocket(self.sock, deadline))
        return json.loads(raw) if raw else {}

    def request(self, kind, payload, expected):
        client_id = str(uuid.uuid4())
        self.connector.websocket_send_text(self.sock, json.dumps({
            'clientMsgId': client_id, 'payloadType': kind, 'payload': payload}))
        deadline = time.monotonic() + 15
        while True:
            data = self._receive(deadline)
            if data.get('clientMsgId') != client_id:
                continue
            if data.get('payloadType') in (2142, 2132):
                raise BrokerTestBlocked(Code.BROKER_REJECTED)
            if data.get('payloadType') != expected:
                continue
            result = data.get('payload')
            if not isinstance(result, dict) or result.get('errorCode'):
                raise BrokerTestBlocked(Code.BROKER_RESPONSE_INVALID)
            if 'ctidTraderAccountId' in payload and str(result.get('ctidTraderAccountId')) != str(ACCOUNT):
                raise BrokerTestBlocked(Code.IDENTITY_MISMATCH)
            return result

    def quote(self, symbol_id):
        self.request(2127, {'ctidTraderAccountId':ACCOUNT,'symbolId':[symbol_id], 'subscribeToSpotTimestamp':True}, 2128)
        deadline = time.monotonic() + 15
        quote = {}
        while True:
            event = self._receive(deadline)
            payload = event.get('payload') or {}
            if (event.get('payloadType') == 2131 and payload.get('symbolId') == symbol_id
                    and str(payload.get('ctidTraderAccountId')) == str(ACCOUNT)):
                quote.update(payload)
                if quote.get('bid') and quote.get('ask') and quote.get('timestamp'):
                    return quote


class CTraderTestAdapter:
    def __init__(self, transport_factory=DemoSocket):
        self.transport_factory = transport_factory
        self.protection = None

    @staticmethod
    def _assert_selected_account():
        from ctrader_connector import get_active_ctrader_account_id
        try:
            selected = get_active_ctrader_account_id()
        except Exception as exc:
            raise BrokerTestBlocked(Code.SELECTED_ACCOUNT_MISMATCH) from exc
        if str(selected) != str(ACCOUNT):
            raise BrokerTestBlocked(Code.SELECTED_ACCOUNT_MISMATCH)

    @contextmanager
    def _connection(self):
        with self.transport_factory() as transport:
            # Verify on every operation even with injected transports.
            token = getattr(transport, 'token', None)
            accounts = transport.request(2149, {'accessToken':token}, 2150).get('ctidTraderAccount', [])
            matched = [a for a in accounts if str(a.get('ctidTraderAccountId')) == str(ACCOUNT)]
            if len(matched) != 1 or matched[0].get('isLive') is not False:
                raise BrokerTestBlocked(Code.DEMO_PROOF_REQUIRED)
            yield transport

    @staticmethod
    def _history(transport, since):
        now = int(time.time() * 1000)
        start = int(since.replace(tzinfo=since.tzinfo or timezone.utc).timestamp() * 1000) - 60000
        payload = {'ctidTraderAccountId':ACCOUNT,'fromTimestamp':start,'toTimestamp':now}
        orders = transport.request(2175, payload, 2176)
        deals = transport.request(2133, {**payload, 'maxRows':1000}, 2134)
        if orders.get('hasMore') is not False or deals.get('hasMore') is not False:
            raise BrokerTestBlocked(Code.HISTORY_INCOMPLETE)
        return orders.get('order', []), deals.get('deal', [])

    def fresh_preflight(self, request):
        try:
            return self._fresh_preflight(request)
        except BrokerTestBlocked:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerTestBlocked(Code.BROKER_RESPONSE_INVALID) from exc

    def _fresh_preflight(self, request):
        if request.account_id != str(ACCOUNT) or request.symbol != 'EURUSD':
            raise BrokerTestBlocked(Code.INVALID_REQUEST)
        self._assert_selected_account()
        with self._connection() as transport:
            trader = transport.request(2121, {'ctidTraderAccountId':ACCOUNT}, 2122)['trader']
            if (str(trader.get('ctidTraderAccountId')) != str(ACCOUNT)
                    or trader.get('accountType') not in (0, 'HEDGED')
                    or trader.get('accessRights') not in (0, 'FULL_ACCESS')
                    or trader.get('isLimitedRisk') is not False):
                raise BrokerTestBlocked(Code.ACCOUNT_PERMISSIONS_UNSUPPORTED)
            symbols = transport.request(2114, {'ctidTraderAccountId':ACCOUNT,'includeArchivedSymbols':False}, 2115)['symbol']
            candidates = [s for s in symbols if s.get('symbolName') == 'EURUSD']
            if len(candidates) != 1:
                raise BrokerTestBlocked(Code.SYMBOL_METADATA_INVALID)
            symbol_id = int(candidates[0]['symbolId'])
            details = transport.request(2116, {'ctidTraderAccountId':ACCOUNT,'symbolId':[symbol_id]}, 2117)['symbol']
            if len(details) != 1 or int(details[0]['symbolId']) != symbol_id:
                raise BrokerTestBlocked(Code.SYMBOL_METADATA_INVALID)
            symbol = details[0]
            try:
                volume, step, maximum = (int(symbol[k]) for k in ('minVolume','stepVolume','maxVolume'))
            except (KeyError, TypeError, ValueError) as exc:
                raise BrokerTestBlocked(Code.INVALID_BROKER_VOLUME) from exc
            if volume <= 0 or step <= 0 or volume % step or maximum < volume:
                raise BrokerTestBlocked(Code.INVALID_BROKER_VOLUME)
            if symbol.get('tradingMode') not in (0, 'ENABLED'):
                raise BrokerTestBlocked(Code.SYMBOL_METADATA_INVALID)
            # Test-only 50-pip protection, widened to broker minimum + spread.
            # Unsupported distance metadata fails closed; no guessed unit conversion.
            if symbol.get('distanceSetIn') not in (1, 'SYMBOL_DISTANCE_IN_POINTS'):
                raise BrokerTestBlocked(Code.PROTECTION_UNSUPPORTED)
            quote = transport.quote(symbol_id)
            try:
                quote_account = str(quote['ctidTraderAccountId'])
                quote_symbol = int(quote['symbolId'])
                quote_time = int(quote['timestamp'])
                bid, ask = int(quote['bid']) / 100000, int(quote['ask']) / 100000
            except (KeyError, TypeError, ValueError) as exc:
                raise BrokerTestBlocked(Code.QUOTE_INVALID) from exc
            if (quote_account != str(ACCOUNT) or quote_symbol != symbol_id
                    or abs(time.time()*1000 - quote_time) > 30000):
                raise BrokerTestBlocked(Code.QUOTE_INVALID)
            if not 0 < bid < ask:
                raise BrokerTestBlocked(Code.QUOTE_INVALID)
            digits = int(symbol['digits'])
            distances = [max(0.005, int(symbol[k]) / 10**digits + 2*(ask-bid)) for k in ('slDistance','tpDistance')]
            if max(distances) > 0.02:
                raise BrokerTestBlocked(Code.PROTECTION_UNSUPPORTED)
            self.protection = tuple(math.ceil(d*100000) for d in distances)
            existing = transport.request(2124, {'ctidTraderAccountId':ACCOUNT,'returnProtectionOrders':False}, 2125)
            if existing.get('position') or existing.get('order'):
                raise BrokerTestBlocked(Code.EXISTING_EXPOSURE)
            self._history(transport, datetime.now(timezone.utc))
            return Preflight(str(ACCOUNT), False, 'EURUSD', symbol_id, volume, step, maximum, bid, ask, True)

    def submit(self, row):
        if row.account_id != str(ACCOUNT) or not self.protection:
            raise BrokerTestBlocked(Code.INVALID_REQUEST)
        with self._connection() as transport:
            self._assert_selected_account()
            transport.request(2106, {'ctidTraderAccountId':ACCOUNT, 'symbolId':row.symbol_id,
                'orderType':1, 'tradeSide':1, 'volume':row.volume, 'label':row.reference,
                'clientOrderId':row.reference, 'comment':'Dedicated DEMO broker integration test',
                'relativeStopLoss':self.protection[0], 'relativeTakeProfit':self.protection[1]}, 2126)

    def reconcile(self, row):
        if row.account_id != str(ACCOUNT):
            raise BrokerTestBlocked(Code.INVALID_REQUEST)
        with self._connection() as transport:
            orders, deals = self._history(transport, row.created_at)
            current = transport.request(2124, {'ctidTraderAccountId':ACCOUNT,'returnProtectionOrders':False}, 2125)
        matched = [o for o in orders if o.get('clientOrderId') == row.reference
                   and (o.get('tradeData') or {}).get('label') == row.reference
                   and not o.get('closingOrder')]
        if len(matched) != 1:
            return Reconciliation(False)
        order = matched[0]
        trade = order.get('tradeData') or {}
        position_id, order_id = str(order.get('positionId') or ''), str(order.get('orderId') or '')
        if (int(trade.get('symbolId', 0)) != row.symbol_id or trade.get('tradeSide') not in (1,'BUY')
                or int(trade.get('volume', 0)) != row.volume or not position_id or not order_id):
            return Reconciliation(False)
        related = [d for d in deals if str(d.get('positionId')) == position_id]
        opening = [d for d in related if not d.get('closePositionDetail')]
        if (not opening or any(str(d.get('orderId')) != order_id or int(d.get('symbolId',0)) != row.symbol_id
                               or d.get('tradeSide') not in (1,'BUY') for d in opening)
                or sum(int(d.get('filledVolume',0)) for d in opening) != row.volume):
            return Reconciliation(False)
        if any(str(o.get('positionId')) == position_id for o in current.get('order', [])):
            return Reconciliation(False)
        closing = [d for d in related if d.get('closePositionDetail')]
        if any(int(d.get('symbolId',0)) != row.symbol_id or d.get('tradeSide') not in (2,'SELL')
               or int(d.get('filledVolume',0)) != int(d['closePositionDetail'].get('closedVolume',-1)) for d in closing):
            return Reconciliation(False)
        closed = sum(int(d['closePositionDetail']['closedVolume']) for d in closing)
        positions = [p for p in current.get('position',[]) if str(p.get('positionId')) == position_id]
        if len(positions) > 1:
            return Reconciliation(False)
        open_volume = 0
        if positions:
            data = positions[0].get('tradeData') or {}
            if (data.get('label') != row.reference or int(data.get('symbolId',0)) != row.symbol_id
                    or data.get('tradeSide') not in (1,'BUY')):
                return Reconciliation(False)
            open_volume = int(data.get('volume',0))
        if open_volume + closed != row.volume:
            return Reconciliation(False)
        try:
            prices = [float(d['executionPrice']) for d in opening]
            opened_times = [int(d['executionTimestamp']) for d in opening]
            closed_times = [int(d['executionTimestamp']) for d in closing]
            since_ms = int(row.created_at.replace(tzinfo=row.created_at.tzinfo or timezone.utc).timestamp()*1000)-60000
            now_ms = int(time.time()*1000)+30000
            if (any(not math.isfinite(price) or price <= 0 for price in prices)
                    or any(not since_ms <= timestamp <= now_ms for timestamp in opened_times+closed_times)
                    or any(timestamp < min(opened_times) for timestamp in closed_times)):
                return Reconciliation(False)
            fill_price = sum(price*int(deal['filledVolume']) for price,deal in zip(prices,opening))/row.volume
            opened_at = min(opened_times)
            closed_at = max(closed_times) if not open_volume and closed_times else None
            if not open_volume and closed_at is None:
                return Reconciliation(False)
        except (KeyError, TypeError, ValueError, OverflowError):
            return Reconciliation(False)
        return Reconciliation(True, str(ACCOUNT), row.symbol_id, 'BUY', row.reference,
                              order_id, position_id, row.volume, open_volume, closed,
                              fill_price, opened_at, closed_at)

    def close(self, row, evidence):
        fresh = self.reconcile(row)
        BrokerIntegrationTestService._verify(row, fresh)
        if fresh != evidence or fresh.open_volume <= 0:
            raise BrokerTestBlocked(Code.UNRESOLVED_CLOSE)
        with self._connection() as transport:
            transport.request(2111, {'ctidTraderAccountId':ACCOUNT,
                'positionId':int(fresh.position_id),'volume':fresh.open_volume}, 2126)
