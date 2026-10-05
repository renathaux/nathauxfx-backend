"""No final permission without complete, current, detached admission evidence."""
import copy
import importlib
import time
import pytest


def module():
    try:return importlib.import_module('live_integrity.admission')
    except ModuleNotFoundError:pytest.fail('Pure production admission boundary missing')


def evidence():
    return dict(live_auto_enabled=True,active_orders=False,inflight=False,
        risk_settings={'maxDailyLoss':None,'maxWeeklyLoss':None},
        health_fetch_times={s+':'+tf:time.time() for s in ('EURUSD','XAUUSD') for tf in ('5min','15min','1h')},
        fundamentals={'EURUSD':{'overall_bias':{'status':'ACTIVE','direction':'BUY'},'data_quality':{}}},fundamental_expiry={'EURUSD':time.monotonic()+120})


def test_complete_detached_admission_and_production_fundamental_policy_agree():
    m=module(); runtime=evidence(); before=copy.deepcopy(runtime)
    result=m.validate_admission(runtime,news_mode='OFF',symbol='EURUSD',side='BUY',fundamental_policy='BLOCK_OPPOSITE',now=time.time(),monotonic_now=time.monotonic())
    assert result['ok'] is True
    from services.fundamental_execution_guard import validate_fundamental_entry
    assert m.fundamental_entry('EURUSD','BUY',insight=runtime['fundamentals']['EURUSD'])==validate_fundamental_entry('EURUSD','BUY',insight=runtime['fundamentals']['EURUSD'])
    assert runtime==before


@pytest.mark.parametrize('fault,reason',[
    ('missing','PRODUCTION_ADMISSION_STATE_UNAVAILABLE'),
    ('loss','LOSS_HISTORY_READ_ONLY_UNAVAILABLE'),
    ('news','NEWS_READ_ONLY_EVIDENCE_UNAVAILABLE'),
    ('opposite','WAIT_FUNDAMENTAL_BIAS_OPPOSES_ENTRY'),
    ('fundamental_missing','FUNDAMENTAL_READ_ONLY_EVIDENCE_UNAVAILABLE'),
    ('stale_health','WAIT_STALE_MARKET_FEED'),
    ('orders','LOCAL_EXECUTION_STATE_BLOCKED'),
    ('inflight','LOCAL_EXECUTION_STATE_BLOCKED'),
])
def test_incomplete_or_blocked_admission_never_allows(fault,reason):
    runtime=evidence(); mode='OFF'
    if fault=='missing':runtime={}
    if fault=='loss':runtime['risk_settings']['maxDailyLoss']=100
    if fault=='news':mode='BLOCK_ONLY'
    if fault=='opposite':runtime['fundamentals']['EURUSD']['overall_bias']['direction']='SELL'
    if fault=='fundamental_missing':runtime['fundamentals']={}
    if fault=='stale_health':runtime['health_fetch_times']['EURUSD:5min']=time.time()-10000
    if fault=='orders':runtime['active_orders']=True
    if fault=='inflight':runtime['inflight']=True
    result=module().validate_admission(runtime,news_mode=mode,symbol='EURUSD',side='BUY',fundamental_policy='BLOCK_OPPOSITE',now=time.time(),monotonic_now=time.monotonic())
    assert result=={'ok':False,'reason':reason}


def test_server_runtime_capture_is_detached_and_does_not_refresh_caches(monkeypatch):
    import api
    from fundamentals import insight_cache
    from strategies import shared
    from datetime import datetime,timezone
    cache={'EURUSD':{'stored_at':time.monotonic(),'response':{'overall_bias':{'status':'ACTIVE','direction':'BUY'},'secret_extra':'must-not-cross-boundary'}}}
    times={s+':'+tf:datetime.now(timezone.utc) for s in ('EURUSD','XAUUSD') for tf in ('5min','15min','1h')}
    monkeypatch.setattr(insight_cache,'_ENTRIES',cache)
    monkeypatch.setattr(shared,'CTRADER_LAST_SUCCESS_TIMES',times)
    monkeypatch.setattr(api,'LIVE_ACTIVE_ORDERS',{'EURUSD':None,'XAUUSD':None})
    monkeypatch.setattr(api,'LIVE_ORDER_IN_FLIGHT',set())
    monkeypatch.setattr(api,'LIVE_AUTO_TRADE_ENABLED',{'enabled':True})
    def no_disk():pytest.fail('Parent capture performed settings-file IO outside hard timeout')
    monkeypatch.setattr(api,'load_risk_settings',no_disk)
    before=copy.deepcopy((cache,times,api.LIVE_AUTO_TRADE_ENABLED,api.LIVE_ACTIVE_ORDERS,api.LIVE_ORDER_IN_FLIGHT))
    assert hasattr(api,'capture_integrity_runtime'),'Read-only server runtime capture missing'
    result=api.capture_integrity_runtime()
    assert result['fundamentals']['EURUSD']['overall_bias']['direction']=='BUY'
    assert 'secret_extra' not in repr(result)
    result['fundamentals']['EURUSD']['overall_bias']['direction']='SELL'
    assert (cache,times,api.LIVE_AUTO_TRADE_ENABLED,api.LIVE_ACTIVE_ORDERS,api.LIVE_ORDER_IN_FLIGHT)==before


def test_sizing_preserves_production_stop_distance_input_without_changing_levels():
    from live_integrity import sizing
    assert hasattr(sizing,'stop_distance_pips'),'Shared production stop-distance input missing'
    assert sizing.stop_distance_pips('1.1001','1.0951','0.0001')==50.00000000000115
