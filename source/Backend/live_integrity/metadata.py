"""New-entry authority only. Never used to authorize closing an open position.

Protocol v1: volume/lot fields are cents; spot prices are 1e-5; symbol digits
define the supported price quantum. Distance points are 10^-digits; percentage
distances are hundredths of a percent. No symbol-specific numeric defaults.
Only quote=deposit USD valuation is supported here; other FX conversions block.

Freshness policy v1: frozen and revalidated metadata <=60s old; each bid/ask
server timestamp and receipt <=2s old, at most 0.5s future clock skew. Metadata
is explicitly refetched on the order socket; changed constraints BLOCK, never
silently replace the frozen snapshot. These are safety TTLs, not trading rules.
"""
import copy
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

VERSION = 'ctrader-execution-metadata-v1'
METADATA_MAX_AGE = 60
QUOTE_MAX_AGE = 2
FIELDS = ('digits', 'pip_size', 'tick_size', 'lot_size', 'min_volume_units',
          'max_volume_units', 'volume_step_units', 'pip_value_per_lot',
          'tick_value_per_lot', 'sl_distance', 'tp_distance', 'distance_unit',
          'trading_mode', 'short_selling', 'limited_risk', 'quote_asset_id',
          'deposit_asset_id', 'quote_to_deposit_rate', 'access_rights', 'account_type')


class MetadataError(ValueError):
    pass


def number(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise MetadataError('BROKER_METADATA_INVALID_NUMBER') from exc


def decimal_text(value):
    return format(number(value).normalize(), 'f')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _hash_material(record):
    return {k: record[k] for k in ('account_id', 'environment', 'symbol_id', 'symbol_name', 'version', 'fields')}


def _integer(record, key, *, minimum=0):
    if type(record.get(key)) not in (int, str):
        raise MetadataError('BROKER_METADATA_INVALID_' + key)
    value = number(record.get(key))
    if value != value.to_integral_value() or value < minimum:
        raise MetadataError('BROKER_METADATA_INVALID_' + key)
    return int(value)


def _enum(record, key, names):
    raw = record.get(key)
    if isinstance(raw, str) and raw in names:
        return names.index(raw)
    return _integer(record, key)


def normalize_symbol_metadata(account_id, environment, symbol, light, full, trader, assets, *, now=None):
    account = str(account_id)
    if environment not in ('demo', 'live') or not account.isdigit():
        raise MetadataError('BROKER_METADATA_ACCOUNT_MISMATCH')
    symbol_id = _integer(light, 'symbolId', minimum=1)
    if (symbol not in ('EURUSD', 'XAUUSD') or light.get('symbolName') != symbol
            or _integer(full, 'symbolId', minimum=1) != symbol_id):
        raise MetadataError('BROKER_METADATA_SYMBOL_MISMATCH')
    if str(trader.get('ctidTraderAccountId')) != account:
        raise MetadataError('BROKER_METADATA_ACCOUNT_MISMATCH')
    if light.get('enabled') is not True:
        raise MetadataError('BROKER_SYMBOL_NOT_ENABLED')
    digits = _integer(full, 'digits')
    pip_position = _integer(full, 'pipPosition')
    if not 0 <= pip_position <= digits <= 5:
        raise MetadataError('BROKER_PRICE_PRECISION_UNSUPPORTED')
    quote_asset = _integer(light, 'quoteAssetId', minimum=1)
    deposit_asset = _integer(trader, 'depositAssetId', minimum=1)
    quote_assets = [a for a in assets if str(a.get('assetId')) == str(quote_asset)]
    if quote_asset != deposit_asset or len(quote_assets) != 1 or quote_assets[0].get('name') != 'USD':
        raise MetadataError('BROKER_CURRENCY_CONVERSION_UNVERIFIED')
    for obj, key in ((full,'enableShortSelling'), (trader,'isLimitedRisk')):
        if type(obj.get(key)) is not bool:
            raise MetadataError('BROKER_METADATA_MISSING_' + key)
    fields = {}
    def put(key, value, source):
        fields[key] = dict(value=value, authoritative=True, source=source, normalization=VERSION)
    for field, raw in (('lot_size','lotSize'), ('min_volume_units','minVolume'),
                       ('max_volume_units','maxVolume'), ('volume_step_units','stepVolume')):
        put(field, decimal_text(Decimal(_integer(full,raw,minimum=1))/100), 'ProtoOASymbol.'+raw+'/100')
    if number(fields['min_volume_units']['value']) > number(fields['max_volume_units']['value']):
        raise MetadataError('BROKER_VOLUME_RANGE_INVALID')
    quantum = Decimal(10) ** -digits
    pip = Decimal(10) ** -pip_position
    lot = number(fields['lot_size']['value'])
    put('digits', str(digits), 'ProtoOASymbol.digits')
    put('tick_size', decimal_text(quantum), 'ProtoOASymbol.digits:price-quantum')
    put('pip_size', decimal_text(pip), 'ProtoOASymbol.pipPosition')
    put('pip_value_per_lot', decimal_text(pip*lot), 'ProtoOASymbol.lotSize+pipPosition;quote=depositUSD')
    put('tick_value_per_lot', decimal_text(quantum*lot), 'ProtoOASymbol.lotSize+digits;quote=depositUSD')
    for name, raw in (('sl_distance','slDistance'), ('tp_distance','tpDistance')):
        put(name, str(_integer(full,raw)), 'ProtoOASymbol.'+raw)
    put('distance_unit', str(_enum(full,'distanceSetIn',('', 'SYMBOL_DISTANCE_IN_POINTS','SYMBOL_DISTANCE_IN_PERCENTAGE'))), 'ProtoOASymbol.distanceSetIn')
    put('trading_mode', str(_enum(full,'tradingMode',('ENABLED','DISABLED_WITHOUT_PENDINGS_EXECUTION','DISABLED_WITH_PENDINGS_EXECUTION','CLOSE_ONLY_MODE'))), 'ProtoOASymbol.tradingMode')
    if fields['distance_unit']['value'] not in ('1','2'):
        raise MetadataError('BROKER_DISTANCE_UNIT_UNSUPPORTED')
    put('short_selling', full['enableShortSelling'], 'ProtoOASymbol.enableShortSelling')
    put('limited_risk', trader['isLimitedRisk'], 'ProtoOATrader.isLimitedRisk')
    put('access_rights', str(_enum(trader,'accessRights',('FULL_ACCESS','CLOSE_ONLY','NO_TRADING','NO_LOGIN'))), 'ProtoOATrader.accessRights')
    put('account_type', str(_enum(trader,'accountType',('HEDGED','NETTED','SPREAD_BETTING'))), 'ProtoOATrader.accountType')
    put('quote_asset_id', str(quote_asset), 'ProtoOALightSymbol.quoteAssetId')
    put('deposit_asset_id', str(deposit_asset), 'ProtoOATrader.depositAssetId')
    put('quote_to_deposit_rate', '1', 'ProtoOAAssetList+Trader:identical-USD-assets')
    result = dict(account_id=account, environment=environment, symbol_id=symbol_id,
                  symbol_name=symbol, version=VERSION, retrieved_at=time.time() if now is None else now, fields=fields)
    result['metadata_hash'] = digest(_hash_material(result))
    result['retrieval_id'] = digest(dict(metadata_hash=result['metadata_hash'], retrieved_at=result['retrieved_at']))
    return result


def validate_metadata(record, *, account_id, environment, symbol, now=None):
    if not isinstance(record,dict) or record.get('version') != VERSION:
        raise MetadataError('BROKER_METADATA_UNVERIFIED')
    if record.get('account_id') != str(account_id) or record.get('environment') != environment:
        raise MetadataError('BROKER_METADATA_ACCOUNT_MISMATCH')
    if record.get('symbol_name') != symbol or not record.get('symbol_id'):
        raise MetadataError('BROKER_METADATA_SYMBOL_MISMATCH')
    for key in FIELDS:
        field = (record.get('fields') or {}).get(key) or {}
        if field.get('authoritative') is not True or field.get('normalization') != VERSION or not str(field.get('source','')).startswith('ProtoOA') or field.get('value') is None:
            raise MetadataError('BROKER_METADATA_FIELD_UNVERIFIED:' + key)
    if record.get('metadata_hash') != digest(_hash_material(record)):
        raise MetadataError('BROKER_METADATA_HASH_CHANGED')
    if record.get('retrieval_id') != digest(dict(metadata_hash=record['metadata_hash'], retrieved_at=record.get('retrieved_at'))):
        raise MetadataError('BROKER_METADATA_RETRIEVAL_CHANGED')
    age = number(time.time() if now is None else now)-number(record.get('retrieved_at'))
    if age < Decimal('-.5') or age > METADATA_MAX_AGE:
        raise MetadataError('BROKER_METADATA_STALE')
    if value(record,'trading_mode') != '0' or value(record,'limited_risk') is not False:
        raise MetadataError('BROKER_TRADING_MODE_UNSUPPORTED')
    if value(record,'access_rights') != '0' or value(record,'account_type') not in ('0','1'):
        raise MetadataError('BROKER_ACCOUNT_MODE_UNSUPPORTED')
    return record


def value(record, key):
    return record['fields'][key]['value']


def risk_metadata(record):
    """Compatibility projection only after validation, not an authority label."""
    validate_metadata(record,account_id=record['account_id'],environment=record['environment'],symbol=record['symbol_name'])
    names = ('lot_size','min_volume_units','max_volume_units','volume_step_units','pip_size','tick_size','pip_value_per_lot')
    # The legacy risk policy uses whole units. Do not truncate fractional broker
    # steps into a different constraint; unsupported fractional sizing blocks.
    for name in ('min_volume_units','max_volume_units','volume_step_units'):
        if number(value(record,name)) != number(value(record,name)).to_integral_value():
            raise MetadataError('BROKER_FRACTIONAL_UNIT_SIZING_UNSUPPORTED')
    return dict(ok=True, symbol=record['symbol_name'], symbol_id=record['symbol_id'],
                metadata_source=VERSION, broker_metadata=copy.deepcopy(record),
                **{name:float(value(record,name)) for name in names})


def floor_volume_units(units, record):
    step = number(value(record,'volume_step_units'))
    return (number(units)/step).to_integral_value(rounding=ROUND_FLOOR)*step


def distance_price(record, field, reference):
    distance = number(value(record,field))
    if value(record,'distance_unit') == '1':
        return distance*number(value(record,'tick_size'))
    if value(record,'distance_unit') == '2':
        return number(reference)*distance/10000
    raise MetadataError('BROKER_DISTANCE_UNIT_UNSUPPORTED')


def validate_new_order(plan, frozen, current, quote, *, account_id, environment, now=None):
    now = time.time() if now is None else now
    for record in (frozen,current):
        validate_metadata(record,account_id=account_id,environment=environment,symbol=plan['symbol'],now=now)
    if frozen['metadata_hash'] != current['metadata_hash']:
        raise MetadataError('BROKER_METADATA_CHANGED')
    if (quote.get('source') != 'ProtoOASpotEvent' or quote.get('account_id') != str(account_id)
            or quote.get('environment') != environment or quote.get('symbol_id') != frozen['symbol_id']
            or quote.get('symbol_name') != frozen['symbol_name']):
        raise MetadataError('BROKER_QUOTE_IDENTITY_MISMATCH')
    for key in ('server_timestamp','bid_timestamp','ask_timestamp','received_at'):
        age = number(now)-number(quote.get(key))
        if not Decimal('-.5') <= age <= QUOTE_MAX_AGE:
            raise MetadataError('BROKER_QUOTE_STALE')
    bid, ask = number(quote['bid']), number(quote['ask'])
    if not 0 < bid <= ask:
        raise MetadataError('BROKER_QUOTE_INVALID')
    units = number(plan['volume_units'])
    if not number(value(frozen,'min_volume_units')) <= units <= number(value(frozen,'max_volume_units')):
        raise MetadataError('BROKER_VOLUME_RANGE_VIOLATION')
    if units % number(value(frozen,'volume_step_units')) or units*100 != (units*100).to_integral_value():
        raise MetadataError('BROKER_VOLUME_STEP_VIOLATION')
    if number(plan['volume'])*number(value(frozen,'lot_size')) != units:
        raise MetadataError('BROKER_LOT_CONVERSION_MISMATCH')
    quantum = number(value(frozen,'tick_size'))
    for key in ('entry','sl','tp1','tp2'):
        if key == 'tp1' and plan.get(key) is None:
            continue
        if number(plan.get(key)) <= 0 or number(plan[key]) % quantum:
            raise MetadataError('BROKER_PRICE_PRECISION_VIOLATION')
    side = plan['action']
    if side not in ('BUY','SELL') or (side == 'SELL' and value(frozen,'short_selling') is not True):
        raise MetadataError('BROKER_SIDE_NOT_ALLOWED')
    # MARKET requests express protection relatively. Do not rebase an old
    # strategy entry onto a different quote. Fill slippage still uses the
    # existing post-fill protection verification/repair, never new-entry defaults.
    if number(plan['entry']) != (ask if side == 'BUY' else bid):
        raise MetadataError('BROKER_ENTRY_QUOTE_CHANGED')
    sl, tp = number(plan['sl']), number(plan['tp2'])
    sl_ref, tp_ref = (bid,ask) if side == 'BUY' else (ask,bid)
    sl_gap, tp_gap = (sl_ref-sl,tp-tp_ref) if side == 'BUY' else (sl-sl_ref,tp_ref-tp)
    if sl_gap <= 0 or tp_gap <= 0 or sl_gap < distance_price(frozen,'sl_distance',sl_ref) or tp_gap < distance_price(frozen,'tp_distance',tp_ref):
        raise MetadataError('BROKER_DISTANCE_VIOLATION')
    from live_integrity.order_intent import plan_hash
    return dict(intent_plan_hash=plan_hash(plan), metadata_hash=frozen['metadata_hash'], retrieval_id=frozen['retrieval_id'],
                revalidated_retrieval_id=current['retrieval_id'], quote_identity=digest(quote),
                quote=copy.deepcopy(quote), validated_at=now, volume_protocol_cents=int(units*100),
                version=VERSION)
