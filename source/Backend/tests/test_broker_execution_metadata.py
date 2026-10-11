"""Protocol fixtures, never a broker connection or a real order."""
import copy
from decimal import Decimal
from unittest.mock import Mock

import pytest

from services import broker_execution_metadata as metadata

NOW = 1800000000.0


def raw_case(symbol='EURUSD'):
    gold = symbol == 'XAUUSD'
    light = dict(symbolId=41 if gold else 1, symbolName=symbol, enabled=True,
                 baseAssetId=3 if gold else 2, quoteAssetId=1)
    full = dict(symbolId=light['symbolId'], digits=2 if gold else 5,
                pipPosition=2 if gold else 4, minVolume=100 if gold else 100000,
                maxVolume=1000000 if gold else 1000000000,
                stepVolume=100 if gold else 100000, lotSize=10000 if gold else 10000000,
                slDistance=100 if gold else 80, tpDistance=100 if gold else 80,
                distanceSetIn=1, tradingMode=0, enableShortSelling=True)
    trader = dict(ctidTraderAccountId=7, depositAssetId=1, isLimitedRisk=False, accessRights=0, accountType=0)
    assets = [dict(assetId=1, name='USD'), dict(assetId=2, name='EUR'), dict(assetId=3, name='XAU')]
    return light, full, trader, assets


def snapshot(symbol='EURUSD', **changes):
    light, full, trader, assets = raw_case(symbol)
    full.update(changes)
    return metadata.normalize_symbol_metadata('7', 'demo', symbol, light, full, trader, assets, now=NOW)


def plan(symbol='EURUSD'):
    gold = symbol == 'XAUUSD'
    return dict(symbol=symbol, action='BUY', entry=2500.1 if gold else 1.1001,
                sl=2490 if gold else 1.095, tp1=None, tp2=2520 if gold else 1.11,
                volume_units=10 if gold else 20000, volume=.1 if gold else .2)


def quote(symbol='EURUSD', timestamp=NOW):
    gold = symbol == 'XAUUSD'
    return dict(account_id='7', environment='demo', symbol_id=41 if gold else 1,
                symbol_name=symbol, bid='2500' if gold else '1.1',
                ask='2500.1' if gold else '1.1001', server_timestamp=timestamp,
                bid_timestamp=timestamp, ask_timestamp=timestamp, received_at=NOW, source='ProtoOASpotEvent')


@pytest.mark.parametrize('symbol,lot,min_units,step,pip_value',[
    ('EURUSD','100000','1000','1000','10'), ('XAUUSD','100','1','1','1'),
])
def test_authoritative_fields_and_protocol_cents(symbol,lot,min_units,step,pip_value):
    record = snapshot(symbol)
    checked = metadata.validate_metadata(record, account_id='7', environment='demo', symbol=symbol, now=NOW)
    assert checked['fields']['lot_size']['value'] == lot
    assert checked['fields']['min_volume_units']['value'] == min_units
    assert checked['fields']['volume_step_units']['value'] == step
    assert checked['fields']['pip_value_per_lot']['value'] == pip_value
    assert all(v['authoritative'] is True for v in checked['fields'].values())
    result = metadata.validate_new_order(plan(symbol), record, record, quote(symbol), account_id='7', environment='demo', now=NOW)
    assert result['volume_protocol_cents'] == (1000 if symbol=='XAUUSD' else 2000000)


@pytest.mark.parametrize('missing', ['minVolume','maxVolume','stepVolume','lotSize','digits','pipPosition','slDistance','tpDistance','distanceSetIn'])
def test_missing_required_protocol_field_blocks(missing):
    light, full, trader, assets = raw_case()
    del full[missing]
    with pytest.raises(metadata.MetadataError):
        metadata.normalize_symbol_metadata('7','demo','EURUSD',light,full,trader,assets,now=NOW)


@pytest.mark.parametrize('field', ['min_volume_units','max_volume_units','volume_step_units','lot_size','pip_size','tick_size','pip_value_per_lot','digits','sl_distance','tp_distance','distance_unit'])
def test_mixed_authority_never_authorizes_entry(field):
    record = snapshot()
    record['fields'][field]['authoritative'] = False
    record['fields'][field]['source'] = 'fallback'
    with pytest.raises(metadata.MetadataError):
        metadata.validate_metadata(record, account_id='7', environment='demo', symbol='EURUSD', now=NOW)


@pytest.mark.parametrize('record',[None, {}, {'ok':True,'metadata_source':'ctrader'}, {'ok':True,'metadata_source':'fallback'}])
def test_unverified_top_level_label_is_insufficient(record):
    with pytest.raises(metadata.MetadataError):
        metadata.validate_metadata(record, account_id='7', environment='demo', symbol='EURUSD', now=NOW)


@pytest.mark.parametrize('identity',[dict(account_id='8',environment='demo',symbol='EURUSD'),
                                    dict(account_id='7',environment='live',symbol='EURUSD'),
                                    dict(account_id='7',environment='demo',symbol='XAUUSD')])
def test_wrong_account_environment_or_symbol_blocks(identity):
    with pytest.raises(metadata.MetadataError):
        metadata.validate_metadata(snapshot(), now=NOW, **identity)


@pytest.mark.parametrize('units',[999,10000001,1500])
def test_final_volume_min_max_step_violation_blocks(units):
    value = plan(); value['volume_units'] = units; value['volume'] = units/100000
    with pytest.raises(metadata.MetadataError):
        metadata.validate_new_order(value,snapshot(),snapshot(),quote(),account_id='7',environment='demo',now=NOW)


def test_step_rounding_is_exact_and_cents_are_not_units():
    assert metadata.floor_volume_units('12345.67', snapshot()) == Decimal('12000')
    assert metadata.floor_volume_units('10.9', snapshot('XAUUSD')) == Decimal('10')


@pytest.mark.parametrize('field', ['sl','tp2'])
def test_distance_violation_blocks_without_moving_levels(field):
    value = plan(); value[field] = 1.0999 if field == 'sl' else 1.1002
    before = copy.deepcopy(value)
    with pytest.raises(metadata.MetadataError, match='DISTANCE'):
        metadata.validate_new_order(value,snapshot(),snapshot(),quote(),account_id='7',environment='demo',now=NOW)
    assert value == before


def test_typed_percentage_distance_not_interpreted_as_points():
    points = snapshot(slDistance=100, tpDistance=100)
    percentage = snapshot(slDistance=100, tpDistance=100, distanceSetIn=2)
    assert metadata.distance_price(points,'sl_distance',Decimal('1.1')) == Decimal('.001')
    assert metadata.distance_price(percentage,'sl_distance',Decimal('1.1')) == Decimal('.011')


def test_changed_metadata_after_setup_blocks():
    with pytest.raises(metadata.MetadataError,match='CHANGED'):
        metadata.validate_new_order(plan(),snapshot(),snapshot(minVolume=200000),quote(),account_id='7',environment='demo',now=NOW)


@pytest.mark.parametrize('timestamp',[NOW-3,NOW+2])
def test_stale_or_future_quote_blocks(timestamp):
    with pytest.raises(metadata.MetadataError,match='QUOTE'):
        metadata.validate_new_order(plan(),snapshot(),snapshot(),quote(timestamp=timestamp),account_id='7',environment='demo',now=NOW)


def test_stale_metadata_blocks():
    with pytest.raises(metadata.MetadataError,match='STALE'):
        metadata.validate_metadata(snapshot(),account_id='7',environment='demo',symbol='EURUSD',now=NOW+61)


def test_unverified_currency_conversion_is_not_assumed():
    light, full, trader, assets = raw_case()
    trader['depositAssetId'] = 2
    with pytest.raises(metadata.MetadataError,match='CONVERSION'):
        metadata.normalize_symbol_metadata('7','demo','EURUSD',light,full,trader,assets,now=NOW)


def test_price_off_verified_grid_blocks_without_rounding():
    value = plan(); value['sl'] = 1.095001
    with pytest.raises(metadata.MetadataError,match='PRECISION'):
        metadata.validate_new_order(value,snapshot(),snapshot(),quote(),account_id='7',environment='demo',now=NOW)


def wire(monkeypatch, *, wrong_account=False):
    import ctrader_connector as connector
    light, full, trader, assets = raw_case()
    requests = []
    def send(sock, kind, body, expected):
        requests.append(kind)
        assert kind in (2100,2102,2112,2114,2116,2121,2127)
        payload = {'ctidTraderAccountId':8 if wrong_account else 7}
        if kind == connector.PAYLOAD_SYMBOLS_LIST_REQ: payload['symbol'] = [light]
        elif kind == connector.PAYLOAD_SYMBOL_BY_ID_REQ: payload['symbol'] = [full]
        elif kind == connector.PAYLOAD_TRADER_REQ: payload['trader'] = trader
        elif kind == 2112: payload['asset'] = assets
        return {'payloadType':expected, 'payload':payload}
    monkeypatch.setattr(connector,'send_ctrader_request',send)
    return connector, requests


def test_collector_uses_full_symbol_and_closes_socket(monkeypatch):
    from types import SimpleNamespace
    from services import ctrader_symbol_metadata as diagnostic
    connector, requests = wire(monkeypatch)
    sock = Mock()
    monkeypatch.setattr(diagnostic,'_selected_account',lambda:('7','demo','revision1'))
    monkeypatch.setattr(diagnostic,'_credentials',lambda:dict(client_id='mock',client_secret='mock-secret',access_token='mock-token'))
    monkeypatch.setattr(connector,'open_ctrader_json_socket',lambda *a,**k:sock)
    result = metadata.collect_selected_metadata('EURUSD', expected_identity=SimpleNamespace(account_id='7',environment='demo'))
    assert result['fields']['lot_size']['value'] == '100000'
    assert connector.PAYLOAD_SYMBOL_BY_ID_REQ in requests
    sock.close.assert_called_once()


def test_collector_account_mismatch_closes_socket(monkeypatch):
    from services import ctrader_symbol_metadata as diagnostic
    connector, requests = wire(monkeypatch,wrong_account=True)
    sock = Mock()
    monkeypatch.setattr(diagnostic,'_selected_account',lambda:('7','demo','revision1'))
    monkeypatch.setattr(diagnostic,'_credentials',lambda:dict(client_id='mock',client_secret='mock-secret',access_token='mock-token'))
    monkeypatch.setattr(connector,'open_ctrader_json_socket',lambda *a,**k:sock)
    with pytest.raises(metadata.MetadataError): metadata.collect_selected_metadata('EURUSD')
    sock.close.assert_called_once()


def test_quote_requests_timestamp_and_decodes_protocol_price(monkeypatch):
    import json
    connector, requests = wire(monkeypatch)
    payload = dict(ctidTraderAccountId=7,symbolId=1,bid=110000,ask=110010,timestamp=int(NOW*1000))
    monkeypatch.setattr(connector,'websocket_recv_text',lambda sock:json.dumps(dict(payloadType=connector.PAYLOAD_SPOT_EVENT,payload=payload)))
    result = metadata.read_quote(connector,Mock(),snapshot())
    assert result['bid'] == '1.1'
    assert result['ask'] == '1.1001'
    assert result['server_timestamp'] == NOW


def fixture_metadata(symbol='EURUSD', account_id='7', environment='demo'):
    """Fresh test authority for the existing owner/account dummy fixtures."""
    import time
    record = snapshot(symbol)
    record['account_id'] = str(account_id)
    record['environment'] = environment
    record['retrieved_at'] = time.time()
    record['metadata_hash'] = metadata.digest(metadata._hash_material(record))
    record['retrieval_id'] = metadata.digest(dict(metadata_hash=record['metadata_hash'],retrieved_at=record['retrieved_at']))
    return record


def test_strict_getter_cannot_return_fallback(monkeypatch):
    import ctrader_connector as connector
    from ctrader_account_context import AccountIdentity, pinned_account
    monkeypatch.setattr(metadata,'collect_selected_metadata',Mock(side_effect=metadata.MetadataError('BROKER_METADATA_UNAVAILABLE')))
    with pinned_account(AccountIdentity('7', 'demo')):
        result = connector.get_ctrader_symbol_risk_metadata('EURUSD',for_new_entry=True)
    assert result['ok'] is False
    assert 'min_volume_units' not in result


def test_new_order_with_only_fallback_risk_never_opens_order_socket(monkeypatch):
    import ctrader_connector as connector
    opened = Mock(side_effect=AssertionError('fallback must block before socket'))
    monkeypatch.setattr(connector,'open_ctrader_json_socket',opened)
    # Bypass the account lease only in this unit test; the real order function runs.
    send = getattr(connector.place_market_order,'__wrapped__',connector.place_market_order)
    result = send('EURUSD',action='BUY',entry=1.1,sl=1.095,tp2=1.11,volume=.2,volume_units=20000,
                  risk={'metadata_source':'fallback'},mode='demo')
    assert result['ok'] is False
    assert result['broker_order_sent'] is False
    assert result['reason'] == 'BROKER_METADATA_UNVERIFIED'
    opened.assert_not_called()


@pytest.mark.parametrize('symbol', ['EURUSD', 'XAUUSD'])
@pytest.mark.parametrize('failure', [None, 'changed', 'stale_quote', 'wrong_account', 'wrong_symbol', 'mixed', 'distance', 'timeout'])
def test_actual_new_order_boundary_never_sends_invalid_metadata(monkeypatch, symbol, failure):
    import time
    import ctrader_connector as connector
    from services import account_execution_coordination as coordination
    frozen = fixture_metadata(symbol)
    current = copy.deepcopy(frozen)
    price = quote(symbol)
    price.update(server_timestamp=time.time(), bid_timestamp=time.time(), ask_timestamp=time.time(), received_at=time.time())
    order = plan(symbol)
    before = copy.deepcopy(order)
    if failure == 'changed':
        current['fields']['sl_distance']['value'] = '81'
        current['metadata_hash'] = metadata.digest(metadata._hash_material(current))
        current['retrieval_id'] = metadata.digest(dict(metadata_hash=current['metadata_hash'], retrieved_at=current['retrieved_at']))
    elif failure == 'stale_quote': price['server_timestamp'] -= 3
    elif failure == 'wrong_account': current['account_id'] = '8'
    elif failure == 'wrong_symbol': current['symbol_id'] = 99
    elif failure == 'mixed': current['fields']['lot_size']['authoritative'] = False
    elif failure == 'distance': order['sl'] = float(price['bid']) - (0.00001 if symbol == 'EURUSD' else .01)
    sock = Mock()
    monkeypatch.setattr(connector, 'get_ctrader_config', lambda: dict(account_id='7', env='demo'))
    monkeypatch.setattr(coordination, 'assert_execution_account', lambda *a: None)
    monkeypatch.setattr(connector, 'open_ctrader_json_socket', lambda *a, **k: sock)
    monkeypatch.setattr(connector, 'authorize_ctrader_socket', lambda *a: None)
    monkeypatch.setattr(metadata, 'read_symbol_metadata', Mock(return_value=current))
    monkeypatch.setattr(metadata, 'read_quote', Mock(side_effect=TimeoutError() if failure == 'timeout' else None, return_value=price))
    sender = Mock(return_value={'payload': {'errorCode': 'MOCK_REJECTION'}})
    monkeypatch.setattr(connector, 'send_ctrader_request', sender)
    result = connector.place_market_order.__wrapped__(**order, risk={'broker_metadata': frozen}, mode='demo')
    if failure is None:
        sender.assert_called_once()
        assert sender.call_args.args[1] == connector.PAYLOAD_NEW_ORDER_REQ
        assert sender.call_args.args[2]['volume'] == int(metadata.number(order['volume_units']) * 100)
        assert result['broker_order_sent'] is True
        assert order == before
    else:
        sender.assert_not_called()
        assert result['broker_order_sent'] is False
    sock.close.assert_called_once()


def test_quote_future_ask_cannot_hide_behind_fresh_bid():
    record = snapshot()
    prices = quote()
    prices['ask_timestamp'] = NOW + 3600
    with pytest.raises(metadata.MetadataError, match='BROKER_QUOTE_STALE'):
        metadata.validate_new_order(plan(),record,record,prices,account_id='7',environment='demo',now=NOW)


def test_quote_drift_never_rebases_strategy_entry_or_protection():
    record = snapshot()
    order = plan()
    original = copy.deepcopy(order)
    prices = quote()
    prices['ask'] = '1.1002'
    with pytest.raises(metadata.MetadataError, match='BROKER_ENTRY_QUOTE_CHANGED'):
        metadata.validate_new_order(order,record,record,prices,account_id='7',environment='demo',now=NOW)
    assert order == original


def test_named_protocol_enums_normalize_identically_to_numeric_enums():
    light, full, trader, assets = raw_case()
    full.update(distanceSetIn='SYMBOL_DISTANCE_IN_POINTS', tradingMode='ENABLED')
    trader.update(accessRights='FULL_ACCESS', accountType='HEDGED')
    named = metadata.normalize_symbol_metadata('7','demo','EURUSD',light,full,trader,assets,now=NOW)
    assert named == snapshot()


@pytest.mark.parametrize('field', ['lotSize', 'stepVolume', 'digits'])
def test_protocol_integer_fields_do_not_accept_binary_float(field):
    with pytest.raises(metadata.MetadataError):
        snapshot(**{field: 100.0})


@pytest.mark.parametrize('symbol,stop_pips,units', [('EURUSD',50,20000), ('XAUUSD',1000,10)])
def test_api_sizing_uses_verified_lot_and_protocol_cents(monkeypatch,symbol,stop_pips,units):
    import api
    record = fixture_metadata(symbol)
    fallback = Mock(side_effect=AssertionError('no fallback lookup'))
    monkeypatch.setattr(api,'get_default_broker_lot_size',fallback)
    result = api.calculate_position_size(symbol,10000,1,stop_pips,broker_metadata=record)
    assert result['ok'], result
    assert result['volume_units'] == units
    assert result['payload_volume'] == units*100
    assert result['broker_metadata'] == record
    assert result['broker_executable_position_size']['units'] == units
    assert 'theoretical_position_size' in result
    fallback.assert_not_called()


def test_new_entry_adapter_never_rounds_or_uses_hardcoded_distance():
    from services.strategy_studio_execution_adapter import normalize_studio_trade_levels
    record = fixture_metadata()
    # Off-grid strategy levels must be rejected, never rounded into an order.
    result = normalize_studio_trade_levels('EURUSD','BUY',1.100101,1.099,None,1.11,
        tp1_enabled=False,broker_metadata=record)
    assert not result['ok']
    assert result['entry'] == 1.100101
    assert result['adjusted_for_broker_distance'] is False
    # A small valid-price-grid distance is deferred to final broker quote checks,
    # not widened here using legacy 0.0008/1.5 symbol constants.
    result = normalize_studio_trade_levels('EURUSD','BUY',1.1,1.09999,None,1.10001,
        tp1_enabled=False,broker_metadata=record)
    assert result['ok']
    assert (result['sl'],result['tp2']) == (1.09999,1.10001)
    assert result['distance_details']['broker_distance_validation'] == 'REQUIRED_AT_DISPATCH'


def test_legacy_new_entry_preparation_does_not_use_fallback_normalizer(monkeypatch):
    import api
    monkeypatch.setattr(api,'get_tp1_ratio_of_tp2',lambda: .5)
    monkeypatch.setattr(api,'get_configured_rr_window',lambda: (1., 3.))
    monkeypatch.setattr(api,'sync_ctrader_account_state',lambda: None)
    monkeypatch.setattr(api,'get_signal_trade_plan',lambda symbol: {})
    monkeypatch.setattr(api,'LIVE_ACCOUNT_STATE',{'connected':True,'mode':'demo'})
    monkeypatch.setattr(api,'LIVE_ACTIVE_ORDERS',{'EURUSD':None})
    monkeypatch.setattr(api,'get_ctrader_symbol_risk_metadata',lambda symbol, **kw: metadata.risk_metadata(fixture_metadata(symbol)))
    old = Mock(side_effect=AssertionError('legacy broker defaults must not authorize entry'))
    monkeypatch.setattr(api,'normalize_trade_levels',old)
    prepared = api.prepare_ctrader_trade(dict(symbol='EURUSD',action='BUY',signal='BUY',entry=1.1,sl=1.095,tp1=1.105,tp2=1.11))
    assert prepared['ok'], prepared
    assert prepared['sl'] == 1.095
    assert prepared['broker_metadata']['version'] == metadata.VERSION
    old.assert_not_called()
