"""Pure, conservative snapshot admission; unavailable policy evidence blocks."""
import math
from live_integrity.fundamental_policy import validate_fundamental_entry as fundamental_entry


def parse_live_loss_limit(value):
    if value in [None, '']:
        return None
    try:
        limit=float(value)
    except (TypeError,ValueError):
        return None
    return abs(limit) if math.isfinite(limit) and limit>0 else None


def validate_admission(runtime,*,news_mode,symbol,side,fundamental_policy,now,monotonic_now):
    def block(reason):return {'ok':False,'reason':reason}
    required=('live_auto_enabled','active_orders','inflight','risk_settings','health_fetch_times','fundamentals','fundamental_expiry')
    if not isinstance(runtime,dict) or any(key not in runtime for key in required):
        return block('PRODUCTION_ADMISSION_STATE_UNAVAILABLE')
    if runtime['live_auto_enabled'] is not True:
        return block('LIVE_AUTO_DISABLED')
    if runtime['active_orders'] is not False or runtime['inflight'] is not False:
        return block('LOCAL_EXECUTION_STATE_BLOCKED')
    risk=runtime['risk_settings']
    if not isinstance(risk,dict) or any(k not in risk for k in ('maxDailyLoss','maxWeeklyLoss')):
        return block('PRODUCTION_ADMISSION_STATE_UNAVAILABLE')
    # Never invoke the production history refresher/reset/cache writer. An
    # enabled budget requires additional authoritative read-only evidence.
    if any(parse_live_loss_limit(risk[k]) is not None for k in ('maxDailyLoss','maxWeeklyLoss')):
        return block('LOSS_HISTORY_READ_ONLY_UNAVAILABLE')
    # OFF is production's unconditional normal-entry branch. Enabled news
    # controls need release/calendar state that this diagnostic never refreshes.
    if news_mode!='OFF':
        return block('NEWS_READ_ONLY_EVIDENCE_UNAVAILABLE')
    # Production health can reject the whole feed for either symbol. A recent
    # successful fetch for every stream is sufficient, deliberately stricter
    # than its candle-cache recovery allowances; no cache repair occurs here.
    times=runtime['health_fetch_times']
    for pair in ('EURUSD','XAUUSD'):
        for tf in ('5min','15min','1h'):
            stamp=times.get(pair+':'+tf) if isinstance(times,dict) else None
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or not -.5<=now-stamp<=120:
                return block('WAIT_STALE_MARKET_FEED')
    insight=runtime['fundamentals'].get(symbol)
    expiry=runtime['fundamental_expiry'].get(symbol)
    if not isinstance(insight,dict) or type(expiry) not in (int,float) or not math.isfinite(expiry) or monotonic_now>=expiry:
        return block('FUNDAMENTAL_READ_ONLY_EVIDENCE_UNAVAILABLE')
    result=fundamental_entry(symbol,side,insight=insight,policy=fundamental_policy)
    return {'ok':True,'reason':None} if result['ok'] else block(result['reason'])
