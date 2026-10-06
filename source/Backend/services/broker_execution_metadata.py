"""Broker IO wrappers; pure metadata rules shared with read-only verification."""
import copy
import json
import time
from decimal import Decimal
from live_integrity.metadata import *
from live_integrity.metadata import _integer, _hash_material, _enum

def _account_reply(connector, sock, request, response, account_id, **payload):
    reply = connector.send_ctrader_request(sock, request,
        dict(ctidTraderAccountId=int(account_id), **payload), response)
    body = reply.get('payload', {})
    if reply.get('payloadType') != response or str(body.get('ctidTraderAccountId')) != str(account_id):
        raise MetadataError('BROKER_METADATA_ACCOUNT_MISMATCH')
    return body


def read_symbol_metadata(connector, sock, account_id, environment, symbol):
    """Caller owns an exclusively read authenticated socket; never selects accounts."""
    listed = _account_reply(connector,sock,connector.PAYLOAD_SYMBOLS_LIST_REQ,
        connector.PAYLOAD_SYMBOLS_LIST_RES,account_id,includeArchivedSymbols=False)
    matches = [s for s in listed.get('symbol',[]) if s.get('symbolName') == symbol]
    if len(matches) != 1:
        raise MetadataError('BROKER_SYMBOL_UNAVAILABLE_OR_AMBIGUOUS')
    light = matches[0]
    full = connector.fetch_ctrader_full_symbols(sock,int(account_id),[light['symbolId']])['symbol'][0]
    trader = _account_reply(connector,sock,connector.PAYLOAD_TRADER_REQ,connector.PAYLOAD_TRADER_RES,account_id)['trader']
    assets = _account_reply(connector,sock,2112,2113,account_id)['asset']
    record = normalize_symbol_metadata(account_id,environment,symbol,light,full,trader,assets)
    return validate_metadata(record,account_id=account_id,environment=environment,symbol=symbol)


def collect_selected_metadata(symbol, *, expected_identity=None):
    """No cache, refresh, selection write, strategy call, or broker order."""
    import ctrader_connector as connector
    from services.ctrader_symbol_metadata import _selected_account, _credentials
    sock = None
    try:
        selected = _selected_account()
        account, environment, _revision = selected
        if expected_identity is not None and (str(expected_identity.account_id),expected_identity.environment) != (account,environment):
            raise MetadataError('BROKER_METADATA_ACCOUNT_MISMATCH')
        credentials = _credentials()
        sock = connector.open_ctrader_json_socket(*connector.CTRADER_JSON_ENDPOINTS[environment],close_on_error=True)
        connector.send_ctrader_request(sock,connector.PAYLOAD_APPLICATION_AUTH_REQ,
            dict(clientId=credentials['client_id'],clientSecret=credentials['client_secret']),connector.PAYLOAD_APPLICATION_AUTH_RES)
        _account_reply(connector,sock,connector.PAYLOAD_ACCOUNT_AUTH_REQ,connector.PAYLOAD_ACCOUNT_AUTH_RES,account,accessToken=credentials['access_token'])
        result = read_symbol_metadata(connector,sock,account,environment,symbol)
        if selected != _selected_account():
            raise MetadataError('BROKER_METADATA_ACCOUNT_CHANGED')
        return result
    except MetadataError:
        raise
    except Exception:
        # Neither error text nor arbitrary upstream payloads may expose credentials.
        raise MetadataError('BROKER_METADATA_UNAVAILABLE') from None
    finally:
        if sock is not None:
            try: sock.close()
            except Exception: pass


def read_quote(connector, sock, record):
    _account_reply(connector,sock,connector.PAYLOAD_SUBSCRIBE_SPOTS_REQ,
        connector.PAYLOAD_SUBSCRIBE_SPOTS_RES,record['account_id'],
        symbolId=[record['symbol_id']],subscribeToSpotTimestamp=True)
    old_timeout = sock.gettimeout()
    prices = {}
    deadline = time.monotonic()+2
    try:
        for _ in range(100):
            remaining = deadline-time.monotonic()
            if remaining <= 0: break
            sock.settimeout(remaining)
            event = json.loads(connector.websocket_recv_text(sock))
            if event.get('payloadType') == connector.PAYLOAD_ERROR_RES:
                raise MetadataError('BROKER_QUOTE_UNAVAILABLE')
            if event.get('payloadType') != connector.PAYLOAD_SPOT_EVENT: continue
            body = event.get('payload',{})
            if str(body.get('ctidTraderAccountId')) != record['account_id'] or body.get('symbolId') != record['symbol_id']:
                raise MetadataError('BROKER_QUOTE_IDENTITY_MISMATCH')
            stamp = number(body.get('timestamp'))/1000
            for side in ('bid','ask'):
                if side in body:
                    prices[side] = (Decimal(_integer(body,side,minimum=1))/100000,stamp)
            if len(prices) == 2:
                return dict(account_id=record['account_id'],environment=record['environment'],
                    symbol_id=record['symbol_id'],symbol_name=record['symbol_name'],
                    bid=decimal_text(prices['bid'][0]),ask=decimal_text(prices['ask'][0]),
                    server_timestamp=float(min(prices['bid'][1],prices['ask'][1])),
                    bid_timestamp=float(prices['bid'][1]),ask_timestamp=float(prices['ask'][1]),
                    received_at=time.time(),source='ProtoOASpotEvent')
        raise MetadataError('BROKER_QUOTE_UNAVAILABLE')
    finally:
        sock.settimeout(old_timeout)


def build_order_payload(plan, record, validation, *, client_order_id, broker_label, broker_comment, frozen_binding=None):
    """No sizing, rounding, distance widening, or symbol defaults at serialization."""
    from live_integrity.order_intent import project_order_intent
    intent = project_order_intent(plan,record,validation,expected_plan_hash=validation.get('intent_plan_hash'),frozen_binding=frozen_binding)
    return dict(ctidTraderAccountId=int(intent.account_id),symbolId=intent.symbol_id,
                orderType=intent.order_type,tradeSide=intent.side,volume=intent.volume_protocol_cents,
                label=broker_label,comment=broker_comment,clientOrderId=client_order_id,
                relativeStopLoss=intent.relative_stop_loss,relativeTakeProfit=intent.relative_take_profit)
