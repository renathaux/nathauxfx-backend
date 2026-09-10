"""Production-parity, analysis-only EURUSD baseline."""
from __future__ import annotations
import hashlib, json
import pandas as pd
from indicators.smc.legacy_engine import analyze_structure
from . import production_math as calc

MAX_EVENT_AGE = pd.Timedelta(minutes=60)

def _iso(value):
    stamp=pd.Timestamp(value); stamp=stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"); return stamp.isoformat()

def _identity(event):
    payload={"symbol":"EURUSD","timeframe":"15m","timestamp":event["timestamp"],"event_type":event["event_type"],"direction":event["direction"],"broken_level":event["broken_level"]}
    return "lab_smc1_"+hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def _confirmation(event,frame5,buffer,end,not_before=None):
    side="BUY" if event["direction"]=="BULLISH" else "SELL"; anchor=pd.Timestamp(event["timestamp"])
    anchor=(anchor.tz_localize("UTC") if anchor.tzinfo is None else anchor.tz_convert("UTC"))+pd.Timedelta(minutes=15); floor=pd.Timestamp(not_before) if not_before is not None else None
    for timestamp,candle in frame5.iterrows():
        close_time=timestamp+pd.Timedelta(minutes=5)
        if close_time<=anchor or close_time>anchor+MAX_EVENT_AGE or close_time>end or (floor is not None and close_time<=floor): continue
        directional=candle.Close>candle.Open if side=="BUY" else candle.Close<candle.Open
        beyond=candle.Close>=event["broken_level"]+buffer if side=="BUY" else candle.Close<=event["broken_level"]-buffer
        if directional and beyond: return timestamp,close_time,float(candle.Close)
    return None

def candidates(frame15,frame5,start,end,settings):
    analysis=analyze_structure(frame15.loc[frame15.index<=end],timeframe="15m",point_size=calc.POINT_SIZE)
    for event in analysis.get("events",[]):
        timestamp=pd.Timestamp(event["timestamp"]); timestamp=timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC"); close_time=timestamp+pd.Timedelta(minutes=15)
        if close_time<start or close_time>end: continue
        prefix=frame15.loc[frame15.index<=timestamp]; side="BUY" if event["direction"]=="BULLISH" else "SELL"; leg=calc.event_leg(event); exception=calc.internal_two_bos(analysis,event)
        structure_ok=bool(leg is not None and leg>=100*calc.POINT_SIZE or exception["qualified"])
        yield event,timestamp,prefix,side,leg,structure_ok,exception

def evaluate_event(event,timestamp,prefix15,frame5,side,leg,settings,end,*,previous_close=None):
    trace={"event_time":_iso(timestamp),"event_type":event["event_type"],"direction":event["direction"],"structural_leg_points":None if leg is None else leg/calc.POINT_SIZE,"structure_qualified":True,"buffered_m15":None,"ema_allowed":None,"consolidation_allowed":None,"m5_confirmation_time":None,"risk_result":None,"entry":None,"sl":None,"tp1":None,"tp2":None,"rr":None,"skipped_active_position":False,"skipped_previous_close_freshness":False,"final_action":None}
    buffer=calc.bos_buffer(prefix15,settings["bos_buffer_points"]); buffered=float(event["close"])>=float(event["broken_level"])+buffer if side=="BUY" else float(event["close"])<=float(event["broken_level"])-buffer; trace["buffered_m15"]=bool(buffered)
    if not buffered: trace["final_action"]="REJECT_M15_BUFFER"; return None,"rejected_by_m15_buffer",trace
    trend=calc.trend_filter(prefix15); allowed=trend["buy_allowed"] if side=="BUY" else trend["sell_allowed"]; trace["ema_allowed"]=bool(allowed)
    if settings.get("ema_filter_enabled",True) and not allowed: trace["final_action"]="REJECT_EMA"; return None,"rejected_by_ema",trace
    consolidation=calc.classify_consolidation(prefix15); trace["consolidation_allowed"]=not consolidation["is_consolidation"]; close_time=timestamp+pd.Timedelta(minutes=15)
    if previous_close is not None and close_time<=previous_close: trace.update(skipped_previous_close_freshness=True,final_action="SKIP_SETUP_BEFORE_PREVIOUS_CLOSE"); return None,"skipped_previous_position_close_freshness",trace
    confirmation=_confirmation(event,frame5,buffer,end,previous_close)
    if not confirmation: trace["final_action"]="REJECT_M5_CONFIRMATION_EXPIRED"; return None,"rejected_by_m5_confirmation_expired",trace
    candle_time,entry_time,entry=confirmation; trace["m5_confirmation_time"]=_iso(candle_time)
    if settings.get("consolidation_filter_enabled",True) and consolidation["is_consolidation"]: trace["final_action"]="REJECT_CONSOLIDATION"; return None,"rejected_by_consolidation",trace
    levels=calc.build_risk_levels(prefix15,side,entry,timestamp,settings,event.get("event_invalidation_swing"),float(settings.get("tp1_percent_of_tp2",80))/100); trace["risk_result"]=levels.get("reason") if not levels.get("ok") else levels["tp_structure_source"]
    if not levels.get("ok"): trace["final_action"]="REJECT_RISK_RR"; return None,"rejected_by_risk_rr",trace
    trace.update(entry=levels["entry"],sl=levels["stop_loss"],tp1=levels["tp1"],tp2=levels["tp2"],rr=levels["risk_reward_ratio"],final_action="SIMULATED_TRADE")
    trade={"event_timestamp":_iso(timestamp),"event_type":event["event_type"],"side":side,"broken_level":float(event["broken_level"]),"event_structural_leg_size":leg,"m15_break_close":float(event["close"]),"m5_confirmation_timestamp":_iso(candle_time),"entry":levels["entry"],"sl":levels["stop_loss"],"original_sl":levels["stop_loss"],"tp1":levels["tp1"],"tp2":levels["tp2"],"protected_sl":levels["protected_sl_price"],"protected_sl_price":levels["protected_sl_price"],"rr":levels["risk_reward_ratio"],"tp_structure_source":levels["tp_structure_source"],"tp_structure_used":levels["tp_structure_used"],"entry_timestamp":_iso(entry_time),"exit_timestamp":None,"exit_price":None,"exit_reason":None,"result":"UNRESOLVED_OPEN","r_result":None,"exact_r_before_rounding":None,"tp1_reached":False,"source_event_identity":_identity(event),"filters_passed":["structure","m15_buffer","ema","consolidation","m5_confirmation","risk_rr"],"filters_failed_or_skipped":[]}
    return trade,None,trace

def build_trade(event,timestamp,prefix15,frame5,side,leg,settings,end):
    trade,rejection,_=evaluate_event(event,timestamp,prefix15,frame5,side,leg,settings,end); return trade,rejection

def resolve_trade(trade,frame5,end):
    entry_time=pd.Timestamp(trade["entry_timestamp"]); tp1_reached=False; original_sl=float(trade["original_sl"])
    def realized(price): return ((price-trade["entry"])/(trade["entry"]-original_sl) if trade["side"]=="BUY" else (trade["entry"]-price)/(original_sl-trade["entry"]))
    for timestamp,candle in frame5.iterrows():
        close_time=timestamp+pd.Timedelta(minutes=5)
        if close_time<=entry_time or close_time>end: continue
        stop=trade["protected_sl"] if tp1_reached else trade["sl"]; stop_hit=candle.Low<=stop if trade["side"]=="BUY" else candle.High>=stop; protected_touched=candle.Low<=trade["protected_sl"] if trade["side"]=="BUY" else candle.High>=trade["protected_sl"]; tp1_hit=candle.High>=trade["tp1"] if trade["side"]=="BUY" else candle.Low<=trade["tp1"]; tp2_hit=candle.High>=trade["tp2"] if trade["side"]=="BUY" else candle.Low<=trade["tp2"]
        if (stop_hit and (tp2_hit or (tp1_hit and not tp1_reached))) or (not tp1_reached and protected_touched and (tp1_hit or tp2_hit)): result,price,r="AMBIGUOUS_INTRABAR",None,None
        elif tp2_hit: result,price="FULL_TP2_WIN",float(trade["tp2"]); r=realized(price)
        elif stop_hit: result="PROTECTED_WIN" if tp1_reached else "LOSS"; price=float(stop); r=realized(price)
        elif tp1_hit: tp1_reached=True; trade["tp1_reached"]=True; continue
        else: continue
        trade.update(result=result,exit_price=price,r_result=r,exact_r_before_rounding=r,exit_reason=result,exit_timestamp=_iso(close_time),tp1_reached=tp1_reached); return
