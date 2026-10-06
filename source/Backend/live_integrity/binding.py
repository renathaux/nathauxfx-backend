"""Pure frozen-plan and canonical binding primitives. No persistence or dispatch."""
import copy
import hashlib
import json
from decimal import Decimal, localcontext
from live_integrity.schema import normalize_definition

class BindingError(ValueError):
    pass

IDENTITY_FIELDS = ('owner_id','strategy_id','updated_at','schema_version','config_hash','canonical_version')

def fingerprint(value):
    # Values here are persisted JSON/runtime plans, not arbitrary objects.
    try:
        canonical = _emit(_fingerprint_values(value, set()))
    except (ValueError, TypeError) as exc:
        raise BindingError('STRATEGY_IDENTITY_INVALID') from exc
    return hashlib.sha256(canonical.encode()).hexdigest()


def _fingerprint_values(value, active):
    """Preserve canonical-v1 JSON semantics while retaining exact DB decimals.

    Native scalar/key conversion deliberately uses the original JSON codec:
    float spelling, tuple/list equivalence and stringified-key collisions must
    retain their existing hashes. Decimal values go straight to the shared
    canonical emitter, never through float or a quoted string representation.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise BindingError('STRATEGY_IDENTITY_INVALID')
        return value
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in active:
            raise BindingError('STRATEGY_IDENTITY_INVALID')
        active.add(marker)
        try:
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    encoded_key = next(iter(_parse(json.dumps(
                        {key: None}, allow_nan=False, ensure_ascii=False))))
                    result[encoded_key] = _fingerprint_values(item, active)
                return result
            return [_fingerprint_values(item, active) for item in value]
        finally:
            active.remove(marker)
    return _parse(json.dumps(value, allow_nan=False, ensure_ascii=False))


def _emit(value):
    if value is None: return 'null'
    if value is True: return 'true'
    if value is False: return 'false'
    if isinstance(value, Decimal):
        if not value.is_finite(): raise BindingError('STRATEGY_IDENTITY_INVALID')
        if not value: return '0'
        with localcontext() as ctx:
            ctx.prec = max(28, len(value.as_tuple().digits))
            token = format(value.normalize(), 'f')
        return token.rstrip('0').rstrip('.') if '.' in token else token
    if isinstance(value, int): return str(value)
    if isinstance(value, str): return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list): return '[' + ','.join(_emit(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(_emit(k)+':'+_emit(value[k]) for k in sorted(value)) + '}'
    raise BindingError('STRATEGY_IDENTITY_INVALID')


def _parse(raw):
    def reject(value): raise BindingError('STRATEGY_IDENTITY_INVALID')
    return json.loads(raw, parse_float=Decimal, parse_int=Decimal, parse_constant=reject)


def freeze_plan(definition, result, *, symbol, account_balance, account_scope, broker_metadata=None):
    from live_integrity.metadata import validate_metadata, MetadataError
    try:
        _, environment, account_id = account_scope.split(':', 2)
        validate_metadata(broker_metadata, account_id=account_id, environment=environment.lower(), symbol=symbol)
    except (ValueError, MetadataError) as exc:
        raise BindingError(str(exc)) from exc
    from live_integrity.prices import grid_price, protection_prices, VERSION
    from live_integrity.evaluator import PIP_SIZE
    from live_integrity.metadata import number, value
    try:
        if number(value(broker_metadata, 'pip_size')) != number(PIP_SIZE[symbol]):
            raise ValueError('BROKER_PIP_SEMANTICS_MISMATCH')
        prices = {key:grid_price(getattr(result,key),broker_metadata) for key in ('entry','sl','tp1','tp2')}
        protection = protection_prices(definition,prices['entry'],prices['sl'],prices['tp2'],result.signal,broker_metadata)
    except ValueError as exc:
        raise BindingError(str(exc)) from exc
    risk = result.risk_budget
    if risk['method'] != definition['risk']['method'] or fingerprint(risk['value']) != fingerprint(definition['risk']['value']):
        raise BindingError('STRATEGY_PLAN_CHANGED')
    return dict(symbol=symbol, side=result.signal, account_scope=account_scope,
                **prices, price_arithmetic_version=VERSION, protection_prices=protection,
                broker_metadata=copy.deepcopy(broker_metadata),
                risk_method=risk['method'], risk_value=risk['value'],
                requested_risk_percent=risk['value'] if risk['method']=='PERCENT_BALANCE' else risk['dollars']/account_balance*100,
                intended_risk_dollars=round(risk['dollars'], 2), account_balance=account_balance,
                position_constraints=copy.deepcopy(definition['risk']),
                combined_risk_inputs={'requires_no_existing_symbol_position': True},
                tp1_definition=copy.deepcopy(definition['tp1']),
                fundamental_policy=definition['fundamentals']['mode'])


def require_binding(binding):
    if not isinstance(binding, dict) or any(not binding.get(k) for k in ('strategy_identity','frozen_plan','frozen_plan_hash')):
        raise BindingError('STRATEGY_IDENTITY_MISSING')
    identity=binding['strategy_identity']
    if not isinstance(identity,dict) or any(identity.get(k) in (None,'') for k in IDENTITY_FIELDS):
        raise BindingError('STRATEGY_IDENTITY_MISSING')
    if identity['canonical_version'] != 1 or fingerprint(binding['frozen_plan']) != binding['frozen_plan_hash']:
        raise BindingError('STRATEGY_PLAN_CHANGED')
    plan = binding['frozen_plan']
    required = ('symbol','side','account_scope','entry','sl','tp1','tp2','risk_method','risk_value',
                'requested_risk_percent','intended_risk_dollars','account_balance',
                'position_constraints','combined_risk_inputs','tp1_definition','fundamental_policy','broker_metadata')
    if not isinstance(plan, dict) or any(k not in plan for k in required):
        raise BindingError('STRATEGY_IDENTITY_MISSING')
    return identity
