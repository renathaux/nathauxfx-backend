"""Bounded SQL reads returning detached values. No models, caches or runtime IO."""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
import pandas as pd
from sqlalchemy import text
from cryptography.fernet import Fernet
from live_integrity.binding import _parse, _emit, fingerprint
from live_integrity.schema import normalize_definition
from live_integrity.market_data import aggregate_closed


def saved(connection,owner,strategy_id):
    row = connection.execute(text('SELECT strategy_id,owner_id,name,schema_version,updated_at,CAST(definition_json AS TEXT) AS raw FROM saved_strategies WHERE owner_id=:owner AND strategy_id=:id'),
        dict(owner=owner,id=strategy_id)).mappings().one_or_none()
    if row is None:
        raise ValueError('STRATEGY_SAVED_ROW_MISSING')
    if not row['schema_version'] or not row['updated_at']:
        raise ValueError('STRATEGY_IDENTITY_MISSING')
    definition = normalize_definition(json.loads(row['raw']))
    config_hash = hashlib.sha256(_emit(_parse(row['raw'])).encode()).hexdigest()
    if fingerprint(definition)!=config_hash:
        raise ValueError('STRATEGY_DEFINITION_NOT_CANONICAL')
    stamp = row['updated_at']
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    identity = dict(owner_id=owner,strategy_id=strategy_id,updated_at=stamp.astimezone(timezone.utc).isoformat(timespec='microseconds'),
        schema_version=row['schema_version'],canonical_version=1,config_hash=config_hash)
    return dict(identity=identity,definition=definition,name=row['name'])


def selected(connection):
    row = connection.execute(text("SELECT setting_value,updated_at FROM runtime_settings WHERE setting_name='ctrader_active_account'")).mappings().one_or_none()
    if row is None:
        raise ValueError('CTRADER_ACCOUNT_NOT_SELECTED')
    value = json.loads(row['setting_value'])
    account,env = value.get('account_id'),value.get('env')
    if type(account) is bool or not str(account).isdigit() or not 0<int(account)<2**63 or env not in ('live','demo'):
        raise ValueError('CTRADER_ACCOUNT_IDENTITY_INVALID')
    return dict(account_id=str(account),environment=env,revision=str(row['updated_at']),scope=f'CTRADER:{env.upper()}:{account}')


def handoff(connection,owner,strategy_id):
    enabled=connection.execute(text('SELECT owner_id FROM strategy_studio_live_state WHERE enabled=true ORDER BY owner_id LIMIT 2')).scalars().all()
    if enabled!=[owner]:
        raise ValueError('STRATEGY_STUDIO_LIVE_OWNER_AMBIGUOUS')
    row = connection.execute(text('SELECT s.strategy_id,l.enabled,l.enabled_strategy_id,l.updated_at FROM strategy_studio_selection s JOIN strategy_studio_live_state l ON l.owner_id=s.owner_id WHERE s.owner_id=:owner'),{'owner':owner}).mappings().one_or_none()
    if row is None or row['enabled'] is not True:
        raise ValueError('WAIT_STUDIO_LIVE_DISABLED')
    if row['strategy_id']!=strategy_id or row['enabled_strategy_id']!=strategy_id:
        raise ValueError('WAIT_STUDIO_LIVE_STRATEGY_MISMATCH')
    pending = connection.execute(text("SELECT COUNT(*) FROM trade_submission_attempts WHERE owner_id=:owner AND (attempt_status IN ('SUBMITTING','RECONCILIATION_REQUIRED','AMBIGUOUS','ACCEPTED_PROTECTION_FAILED') OR reconciliation_status IN ('REQUIRED','PENDING','RECONCILIATION_REQUIRED','AMBIGUOUS'))"),{'owner':owner}).scalar_one()
    pending += connection.execute(text("SELECT COUNT(*) FROM strategy_setup_lifecycle WHERE owner_id=:owner AND status IN ('SUBMITTING','RECONCILIATION_REQUIRED')"),{'owner':owner}).scalar_one()
    if pending:
        raise ValueError('STRATEGY_STUDIO_RECONCILIATION_UNRESOLVED')
    return dict(row)


def admission_state(connection):
    rows=connection.execute(text("SELECT setting_name,setting_value,updated_at FROM runtime_settings WHERE setting_name IN ('live_auto_trade_enabled','news_trading_mode') ORDER BY setting_name")).mappings().all()
    values={r['setting_name']:r['setting_value'] for r in rows}
    # Missing/invalid durable switches are unknown, never a guessed permission.
    return dict(live_enabled=str(values.get('live_auto_trade_enabled','')).strip().lower() in ('true','1','yes','on'),
                news_mode=str(values.get('news_trading_mode','UNKNOWN')).strip().upper(),revisions=[dict(r) for r in rows])


def credentials(connection):
    row = connection.execute(text("SELECT encrypted_access_token FROM ctrader_oauth_tokens WHERE provider='ctrader'")).scalar_one_or_none()
    material = (os.getenv('CTRADER_TOKEN_ENCRYPTION_KEY') or os.getenv('CTRADER_CLIENT_SECRET') or '').strip()
    if not row or not material:
        raise ValueError('BROKER_AUTH_UNAVAILABLE')
    key = base64.urlsafe_b64encode(hashlib.sha256(f'flowsignal:ctrader:{material}'.encode()).digest())
    access = Fernet(key).decrypt(row.encode()).decode()
    client,secret = os.getenv('CTRADER_CLIENT_ID'),os.getenv('CTRADER_CLIENT_SECRET')
    if not client or not secret or not access:
        raise ValueError('BROKER_AUTH_UNAVAILABLE')
    return dict(client_id=client,client_secret=secret,access_token=access)


def existing_setup(connection,setup_id):
    row=connection.execute(text('SELECT owner_id,strategy_id,account_id,account_scope,symbol,direction,status,definition_snapshot,entry_binding,updated_at FROM strategy_setup_lifecycle WHERE setup_id=:id'),{'id':setup_id}).mappings().one_or_none()
    return None if row is None else dict(row)


def existing_generations(connection,setup_id):
    return [dict(r) for r in connection.execute(text('SELECT root_key,timeframe,generation,event_time,confirmation_time FROM strategy_setup_generations WHERE setup_id=:id ORDER BY root_key,timeframe'),{'id':setup_id}).mappings().all()]


def validate_existing_generations(connection,heads,links):
    """Read-only equivalent of the persisted setup's generation claim guard."""
    advanced={(h['root_key'],h['timeframe']):h for h in heads if h['active_generation']>1}
    if len(links)!=len(advanced) or {(b['root_key'],b['timeframe']) for b in links}!=set(advanced):
        raise ValueError('STUDIO_SETUP_GENERATION_INVALID')
    for link in links:
        head=advanced[(link['root_key'],link['timeframe'])]
        if (link['generation']!=head['active_generation'] or head['status']!='READY'
                or head['activation_watermark'] is None or link['event_time'] is None or link['confirmation_time'] is None
                or pd.Timestamp(link['event_time'])<=pd.Timestamp(head['activation_watermark'])
                or pd.Timestamp(link['confirmation_time'])<=pd.Timestamp(head['activation_watermark'])):
            raise ValueError('STUDIO_SETUP_GENERATION_INVALID')
        durable_confirmation(connection,heads,link['confirmation_time'])


def generation_state(connection,scope,symbol):
    root=symbol[:9]+'~'+hashlib.sha256(f'{symbol}|{scope}'.encode()).hexdigest()[:10].upper()
    rows=connection.execute(text('''SELECT h.root_key,h.timeframe,h.active_generation,
        g.storage_key,g.activation_watermark,g.status AS generation_status,
        s.status,s.last_processed_candle
        FROM indicator_stream_heads h LEFT JOIN indicator_stream_generations g
        ON g.root_key=h.root_key AND g.timeframe=h.timeframe AND g.generation=h.active_generation
        LEFT JOIN indicator_stream_state s ON s.symbol=g.storage_key AND s.timeframe=h.timeframe
        WHERE h.root_key=:root ORDER BY h.timeframe'''),{'root':root}).mappings().all()
    if any(r['generation_status']!='ACTIVE' or not r['storage_key'] for r in rows):
        raise ValueError('STUDIO_GENERATION_UNAVAILABLE')
    return [dict(r) for r in rows]


def durable_confirmation(connection,generations,stamp):
    if not any(g['active_generation']>1 for g in generations):
        return
    if any(g['active_generation']>1 and g['status']!='READY' for g in generations):
        raise ValueError('STUDIO_GENERATION_UNAVAILABLE')
    root=generations[0]['root_key']
    five=next((g for g in generations if g['timeframe']=='5m'),None)
    key=five['storage_key'] if five else root
    state=connection.execute(text("SELECT status,last_processed_candle FROM indicator_stream_state WHERE symbol=:key AND timeframe='5m'"),{'key':key}).mappings().one_or_none()
    present=connection.execute(text("SELECT COUNT(*) FROM indicator_candles WHERE symbol=:key AND timeframe='5m' AND candle_timestamp=:stamp"),{'key':key,'stamp':pd.Timestamp(stamp).to_pydatetime()}).scalar_one()
    if (state is None or state['status']!='READY' or state['last_processed_candle'] is None
            or pd.Timestamp(stamp)>pd.Timestamp(state['last_processed_candle']) or present!=1):
        raise ValueError('STUDIO_CONFIRMATION_NOT_DURABLE')


def market(connection,scope,symbol,now):
    root = symbol[:9]+'~'+hashlib.sha256(f'{symbol}|{scope}'.encode()).hexdigest()[:10].upper()
    heads = connection.execute(text('SELECT h.timeframe,h.active_generation,g.storage_key,g.activation_watermark,g.status FROM indicator_stream_heads h JOIN indicator_stream_generations g ON g.root_key=h.root_key AND g.timeframe=h.timeframe AND g.generation=h.active_generation WHERE h.root_key=:root'),{'root':root}).mappings().all()
    keys = {h['timeframe']:h for h in heads}
    key = keys.get('5m',{}).get('storage_key',root)
    end = pd.Timestamp(now)
    start = end-pd.Timedelta(days=35)
    def frame(storage,tf,full=False):
        rows = connection.execute(text('SELECT candle_timestamp,open_price,high_price,low_price,close_price FROM indicator_candles WHERE symbol=:key AND timeframe=:tf AND candle_timestamp<:end'+('' if full else ' AND candle_timestamp>=:start')+' ORDER BY candle_timestamp LIMIT 25001'),
            dict(key=storage,tf=tf,end=end.to_pydatetime(),start=start.to_pydatetime())).mappings().all()
        if not rows or len(rows)>25000:
            raise ValueError('STUDIO_MARKET_SNAPSHOT_UNAVAILABLE')
        data = pd.DataFrame([dict(Open=r['open_price'],High=r['high_price'],Low=r['low_price'],Close=r['close_price'],Volume=0.0) for r in rows],
            index=pd.to_datetime([r['candle_timestamp'] for r in rows],utc=True))
        data.attrs['ctrader_stream_scope']=scope
        return data
    base = frame(key,'5m')
    bundle = {tf:aggregate_closed(base,tf,end_exclusive=end) for tf in ('5m','15m','1h','4h')}
    bindings = []
    for h in heads:
        if h['status']!='ACTIVE':
            raise ValueError('STUDIO_GENERATION_UNAVAILABLE')
        if h['active_generation']<=1:
            continue
        state = connection.execute(text('SELECT status FROM indicator_stream_state WHERE symbol=:key AND timeframe=:tf'),dict(key=h['storage_key'],tf=h['timeframe'])).scalar_one_or_none()
        if state!='READY' or h['timeframe'] not in bundle:
            raise ValueError('STUDIO_GENERATION_UNAVAILABLE')
        bundle[h['timeframe']]=frame(h['storage_key'],h['timeframe'],True)
        bindings.append(dict(root_key=root,timeframe=h['timeframe'],generation=h['active_generation'],activation_watermark=pd.Timestamp(h['activation_watermark']).isoformat()))
    latest = bundle['5m'].index[-1]
    if not pd.Timedelta(minutes=5)<=end-latest<=pd.Timedelta(minutes=10):
        raise ValueError('STUDIO_MARKET_SNAPSHOT_STALE')
    return bundle,bindings
