"""Exact LIVE price arithmetic, not a rounding/float-noise repair policy.

Operands are source decimal representations, before arithmetic. An already
noisy input stays noisy and fails the exact broker grid check. JSON numbers
are emitted only when their decimal round trip preserves the exact result.
"""
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext
from functools import wraps

VERSION = 'exact-decimal-v1'


class PriceError(ValueError):
    pass


def decimal(value):
    if isinstance(value, bool) or value is None:
        raise PriceError('EXECUTION_PRICE_INVALID')
    try:
        result = Decimal(str(value))
    except DecimalException as exc:
        raise PriceError('EXECUTION_PRICE_INVALID') from exc
    if not result.is_finite():
        raise PriceError('EXECUTION_PRICE_INVALID')
    return result


def exact_arithmetic(function):
    @wraps(function)
    def run(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 80
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            try:
                return function(*args, **kwargs)
            except DecimalException as exc:
                raise PriceError('EXECUTION_PRICE_ARITHMETIC_INVALID') from exc
    return run


def json_price(value):
    if value is None:
        return None
    precise = decimal(value)
    result = float(precise)
    if decimal(result) != precise:
        raise PriceError('EXECUTION_PRICE_DECIMAL_LOSS')
    return result


@exact_arithmetic
def grid_price(price, metadata):
    if price is None:
        return None
    from live_integrity.metadata import value
    quantum = decimal(value(metadata, 'tick_size'))
    precise = decimal(price)
    if quantum <= 0 or precise % quantum != 0:
        raise PriceError('BROKER_PRICE_PRECISION_INVALID')
    return json_price(precise)


@exact_arithmetic
def protection_prices(definition, entry, sl, tp2, side, metadata):
    """Freeze existing protection formulas; never adjust a result onto a grid."""
    rule = definition['tp1']
    out = {'protected': None, 'step_levels': []}
    if not rule['enabled']:
        return out
    entry, sl, tp2 = map(decimal, (entry, sl, tp2))
    path = tp2-entry
    if rule.get('protection_mode', 'FIXED') == 'TP2_STEPS':
        for step in rule['protection_steps']:
            trigger, secure = decimal(step['trigger_percent']), decimal(step['secure_percent'])
            out['step_levels'].append(dict(trigger_percent=step['trigger_percent'],
                secure_percent=step['secure_percent'],
                trigger=json_price(entry+path*trigger/Decimal(100)),
                protected=grid_price(entry+path*secure/Decimal(100), metadata)))
        out['step_levels'].sort(key=lambda item:item['trigger_percent'])
    elif rule.get('protection_r') is not None:
        distance = path if rule.get('target_basis') == 'TP2_DISTANCE' else abs(entry-sl)*(1 if side=='BUY' else -1)
        out['protected'] = grid_price(entry+distance*decimal(rule['protection_r']), metadata)
    return out
