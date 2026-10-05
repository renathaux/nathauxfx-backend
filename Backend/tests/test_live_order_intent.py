"""Pure projection catches post-validation mutations and protocol scaling errors."""
import copy
from dataclasses import FrozenInstanceError
import importlib

import pytest

from services import broker_execution_metadata as metadata
from test_broker_execution_metadata import snapshot, plan, quote, NOW


def projection_module():
    try:
        return importlib.import_module('live_integrity.order_intent')
    except ModuleNotFoundError:
        pytest.fail('Shared pure order-intent projection is not implemented')


def validated(symbol='EURUSD'):
    p, m, q = plan(symbol), snapshot(symbol), quote(symbol)
    if symbol == 'EURUSD':
        p.update(sl='1.0951', tp2='1.1101')
    v = metadata.validate_new_order(p,m,m,q,account_id='7',environment='demo',now=NOW)
    return p,m,v


@pytest.mark.parametrize('symbol,volume,sl,tp',[('EURUSD',2000000,500,1000),('XAUUSD',1000,1010000,1990000)])
def test_exact_protocol_intent_and_production_payload_parity(symbol,volume,sl,tp):
    mod = projection_module()
    p,m,v = validated(symbol)
    before = copy.deepcopy((p,m,v))
    intent = mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash'])
    payload = metadata.build_order_payload(p,m,v,client_order_id='fixture',broker_label='fixture',broker_comment='fixture')
    assert intent.volume_protocol_cents == volume == payload['volume']
    assert intent.relative_stop_loss == sl == payload['relativeStopLoss']
    assert intent.relative_take_profit == tp == payload['relativeTakeProfit']
    assert intent.symbol_id == payload['symbolId']
    assert intent.side == payload['tradeSide']
    assert len(intent.order_intent_hash) == 64
    assert mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash']).canonical_bytes() == intent.canonical_bytes()
    assert (p,m,v) == before
    with pytest.raises(FrozenInstanceError):
        intent.side = 'SELL'
    safe = intent.safe_projection()
    assert 'account_id' not in safe
    assert 'access_token' not in safe


@pytest.mark.parametrize('field,value',[('sl','1.0952'),('tp2','1.1102'),('entry','1.1002'),('volume_units',21000),('action','SELL')])
def test_any_post_validation_plan_mutation_blocks(field,value):
    mod = projection_module()
    p,m,v = validated()
    p[field] = value
    with pytest.raises(ValueError,match='PLAN_CHANGED'):
        mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash'])


def test_validation_volume_cannot_override_exact_units():
    mod = projection_module()
    p,m,v = validated()
    v['volume_protocol_cents'] = 20000
    with pytest.raises(ValueError,match='VOLUME'):
        mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash'])


def test_wrong_metadata_identity_blocks_projection():
    mod = projection_module()
    p,m,v = validated()
    m['account_id'] = '8'
    with pytest.raises(ValueError):
        mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash'])


@pytest.mark.parametrize('value',['1.095001','1.0951000000000002','NaN','Infinity'])
def test_no_rounding_of_off_grid_or_nonfinite_prices(value):
    mod = projection_module()
    p,m,v = validated()
    p['sl'] = value
    v['intent_plan_hash'] = mod.plan_hash(p)
    with pytest.raises(ValueError):
        mod.project_order_intent(p,m,v,expected_plan_hash=v['intent_plan_hash'])
