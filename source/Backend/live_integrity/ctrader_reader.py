"""Dedicated fixed-account read-only JSON socket. No order messages supported."""
import json
import logging
import time
import uuid
from decimal import Decimal
from live_integrity.metadata import normalize_symbol_metadata, validate_metadata, decimal_text

READ_RESPONSES = {2100:2101,2102:2103,2112:2113,2114:2115,2116:2117,2121:2122,2124:2125,2127:2128}


def permit(kind,body):
    if kind not in READ_RESPONSES:
        raise ValueError('BROKER_READ_ONLY_VIOLATION')


class Reader:
    def __init__(self,account,environment,credentials,deadline):
        if environment not in ('demo','live') or not str(account).isdigit():
            raise ValueError('BROKER_IDENTITY_INVALID')
        self.account,self.environment,self.deadline = str(account),environment,deadline
        self._socket = None
        self._credentials = credentials

    def remaining(self):
        value = self.deadline-time.monotonic()
        if value <= 0:
            raise TimeoutError('DIAGNOSTIC_TIMEOUT')
        return value

    def __enter__(self):
        from websockets.sync.client import connect
        logger = logging.Logger('private-diagnostic-transport'); logger.disabled = True
        try:
            self._socket = connect(f'wss://{self.environment}.ctraderapi.com:5036/',
                open_timeout=min(2,self.remaining()),close_timeout=.1,max_size=262144,logger=logger)
            self._request(2100,dict(clientId=self._credentials['client_id'],clientSecret=self._credentials['client_secret']))
            self._request(2102,dict(accessToken=self._credentials['access_token']))
            self._credentials = None
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self._credentials = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __exit__(self,*exc):
        self.close()

    def _receive(self):
        raw = self._socket.recv(timeout=min(2,self.remaining()))
        if len(raw)>262144:
            raise ValueError('BROKER_RESPONSE_TOO_LARGE')
        event = json.loads(raw)
        if event.get('payloadType') in (2142,2132):
            raise ValueError('BROKER_READ_UNAVAILABLE')
        return event

    def _request(self,kind,body):
        permit(kind,body)
        self.remaining()
        body = dict(body)
        if kind != 2100:
            body['ctidTraderAccountId'] = int(self.account)
        tag = str(uuid.uuid4())
        self._socket.send(json.dumps(dict(clientMsgId=tag,payloadType=kind,payload=body)))
        for _ in range(100):
            event = self._receive()
            if event.get('payloadType') != READ_RESPONSES[kind] or event.get('clientMsgId') != tag:
                continue
            payload = event.get('payload',{})
            if kind != 2100 and str(payload.get('ctidTraderAccountId')) != self.account:
                raise ValueError('BROKER_ACCOUNT_MISMATCH')
            return payload
        raise ValueError('BROKER_READ_UNAVAILABLE')

    def metadata(self,symbol):
        if symbol not in ('EURUSD','XAUUSD'):
            raise ValueError('BROKER_SYMBOL_UNSUPPORTED')
        listed = self._request(2114,{'includeArchivedSymbols':False})['symbol']
        matches = [s for s in listed if s.get('symbolName')==symbol]
        if len(matches)!=1:
            raise ValueError('BROKER_SYMBOL_AMBIGUOUS')
        light = matches[0]
        full = self._request(2116,{'symbolId':[light['symbolId']]})['symbol']
        if len(full)!=1:
            raise ValueError('BROKER_SYMBOL_AMBIGUOUS')
        trader = self._request(2121,{})['trader']
        assets = self._request(2112,{})['asset']
        record = normalize_symbol_metadata(self.account,self.environment,symbol,light,full[0],trader,assets)
        return validate_metadata(record,account_id=self.account,environment=self.environment,symbol=symbol)

    def account_state(self):
        trader = self._request(2121,{})['trader']
        if str(trader.get('ctidTraderAccountId')) != self.account:
            raise ValueError('BROKER_ACCOUNT_MISMATCH')
        digits = trader.get('moneyDigits')
        if type(digits) is not int or not 0 <= digits <= 8 or 'balance' not in trader:
            raise ValueError('BROKER_BALANCE_UNVERIFIED')
        balance = decimal_text(Decimal(str(trader['balance']))/(Decimal(10)**digits))
        exposure = self._request(2124,{})
        if not isinstance(exposure,dict) or any(type(exposure[key]) is not list for key in ('position','order') if key in exposure):
            raise ValueError('BROKER_EXPOSURE_UNVERIFIED')
        return dict(balance=balance,positions=exposure.get('position',[]),orders=exposure.get('order',[]))

    def quote(self,record):
        self._request(2127,dict(symbolId=[record['symbol_id']],subscribeToSpotTimestamp=True))
        values = {}
        for _ in range(100):
            event = self._receive()
            if event.get('payloadType') != 2131:
                continue
            p = event.get('payload',{})
            if str(p.get('ctidTraderAccountId')) != self.account or p.get('symbolId') != record['symbol_id']:
                raise ValueError('BROKER_QUOTE_IDENTITY_MISMATCH')
            if 'timestamp' not in p:
                raise ValueError('BROKER_QUOTE_TIMESTAMP_UNAVAILABLE')
            stamp = float(Decimal(str(p['timestamp']))/1000)
            for side in ('bid','ask'):
                if side in p:
                    values[side] = decimal_text(Decimal(str(p[side]))/100000)
                    values[side+'_timestamp'] = stamp
            if 'bid' in values and 'ask' in values:
                return dict(**values,server_timestamp=min(values['bid_timestamp'],values['ask_timestamp']),
                    received_at=time.time(),source='ProtoOASpotEvent',account_id=self.account,
                    environment=self.environment,symbol_id=record['symbol_id'],symbol_name=record['symbol_name'])
        raise ValueError('BROKER_QUOTE_UNAVAILABLE')
