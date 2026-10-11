"""Immutable semantic order projection. No IO, DB, services or transport imports."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, Inexact, Rounded, localcontext
import hashlib
import json

VERSION = 'ctrader-order-intent-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def plan_hash(plan):
    return _digest(plan)


def _decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError('ORDER_INTENT_INVALID_NUMBER')
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError('ORDER_INTENT_INVALID_NUMBER')
        return result
    except InvalidOperation:
        raise ValueError('ORDER_INTENT_INVALID_NUMBER') from None


def _text(value):
    value = format(value, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _integer(value):
    if value != value.to_integral_value() or not 0 < value < 2**63:
        raise ValueError('ORDER_INTENT_PROTOCOL_UNREPRESENTABLE')
    return int(value)


@dataclass(frozen=True)
class OrderIntent:
    account_id: str
    environment: str
    symbol_id: int
    symbol_name: str
    side: str
    entry: str
    sl: str
    tp1: str | None
    tp2: str
    digits: int
    tick_size: str
    volume_protocol_cents: int
    relative_stop_loss: int
    relative_take_profit: int
    metadata_hash: str
    retrieval_id: str
    plan_hash: str
    frozen_plan_hash: str | None
    version: str = VERSION
    price_scale: int = 100000
    volume_scale: int = 100
    order_type: str = 'MARKET'

    def canonical_bytes(self):
        return json.dumps(asdict(self),sort_keys=True,separators=(',', ':'),allow_nan=False).encode()

    @property
    def order_intent_hash(self):
        return hashlib.sha256(VERSION.encode()+b'\0'+self.canonical_bytes()).hexdigest()

    def safe_projection(self):
        result = asdict(self)
        del result['account_id']
        result['account_reference'] = hashlib.sha256((self.environment+':'+self.account_id).encode()).hexdigest()
        result['order_intent_hash'] = self.order_intent_hash
        return result


def project_order_intent(plan, metadata, validation, *, expected_plan_hash, frozen_binding=None):
    """Reject changes since validation; never infer/repair a price or volume.

    The complete Studio frozen binding is verified by the shared identity guard.
    This hash additionally seals the final broker-semantic plan at validation.
    """
    if not expected_plan_hash or plan_hash(plan) != expected_plan_hash or validation.get('intent_plan_hash') != expected_plan_hash:
        raise ValueError('STRATEGY_PLAN_CHANGED')
    frozen_hash = None
    if frozen_binding is not None:
        from live_integrity.binding import require_binding
        require_binding(frozen_binding)
        frozen = frozen_binding['frozen_plan']
        if (frozen['symbol'] != plan['symbol'] or frozen['side'] != plan['action']
                or frozen['account_scope'] != f"CTRADER:{metadata['environment'].upper()}:{metadata['account_id']}"
                or any(_decimal(frozen[key]) != _decimal(plan[key]) for key in ('entry','sl','tp1','tp2')
                       if not (key=='tp1' and frozen[key] is None and plan[key] is None))):
            raise ValueError('STRATEGY_PLAN_CHANGED')
        frozen_hash = frozen_binding['frozen_plan_hash']
    material = {key:metadata[key] for key in ('account_id','environment','symbol_id','symbol_name','version','fields')}
    if (_digest(material) != metadata['metadata_hash'] or validation.get('metadata_hash') != metadata['metadata_hash']
            or validation.get('retrieval_id') != metadata['retrieval_id']
            or plan['symbol'] != metadata['symbol_name']):
        raise ValueError('ORDER_INTENT_METADATA_CHANGED')
    if metadata['environment'] not in ('demo','live') or not str(metadata['account_id']).isdigit():
        raise ValueError('ORDER_INTENT_ACCOUNT_INVALID')
    _integer(_decimal(metadata['account_id']))
    _integer(_decimal(metadata['symbol_id']))
    def field(key):
        item = metadata['fields'][key]
        if item.get('authoritative') is not True or item.get('normalization') != metadata['version']:
            raise ValueError('ORDER_INTENT_FIELD_UNVERIFIED')
        return _decimal(item['value'])
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        digits = field('digits')
        if digits != digits.to_integral_value() or not 0 <= digits <= 5:
            raise ValueError('ORDER_INTENT_PRECISION_INVALID')
        quantum = field('tick_size')
        if quantum != Decimal(10) ** -int(digits):
            raise ValueError('ORDER_INTENT_PRECISION_INVALID')
        prices = {}
        for key in ('entry','sl','tp1','tp2'):
            if key == 'tp1' and plan.get(key) is None:
                prices[key] = None
                continue
            value = _decimal(plan[key])
            if value <= 0 or value % quantum:
                raise ValueError('BROKER_PRICE_PRECISION_VIOLATION')
            prices[key] = value
        units = _decimal(plan['volume_units'])
        if (not field('min_volume_units') <= units <= field('max_volume_units') or units % field('volume_step_units')
                or units != _decimal(plan['volume'])*field('lot_size')):
            raise ValueError('ORDER_INTENT_VOLUME_INVALID')
        volume = _integer(units*100)
        if volume != validation['volume_protocol_cents']:
            raise ValueError('ORDER_INTENT_VOLUME_CHANGED')
        side = plan['action']
        if side not in ('BUY','SELL'):
            raise ValueError('ORDER_INTENT_SIDE_INVALID')
        entry,sl,tp = prices['entry'],prices['sl'],prices['tp2']
        if not (sl < entry < tp if side == 'BUY' else tp < entry < sl):
            raise ValueError('ORDER_INTENT_LEVELS_INVALID')
        rel_sl,rel_tp = _integer(abs(entry-sl)*100000),_integer(abs(tp-entry)*100000)
        return OrderIntent(str(metadata['account_id']),metadata['environment'],int(metadata['symbol_id']),
            metadata['symbol_name'],side,_text(entry),_text(sl),
            None if prices['tp1'] is None else _text(prices['tp1']),_text(tp),int(digits),_text(quantum),
            volume,rel_sl,rel_tp,metadata['metadata_hash'],metadata['retrieval_id'],expected_plan_hash,frozen_hash)
