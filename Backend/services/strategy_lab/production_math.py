"""Mutation-free equivalents of strict_trader's deterministic EURUSD math."""
from __future__ import annotations

import math
import pandas as pd

POINT_SIZE = 0.00001
MIN_SWING_POINTS = 100
PROTECTED_SL_TP2_FRACTION = 0.50


def atr14(data):
    if data is None or len(data) < 14:
        return None
    high, low, close = data.High.astype(float), data.Low.astype(float), data.Close.astype(float)
    previous = close.shift(1)
    value = pd.concat([high-low, (high-previous).abs(), (low-previous).abs()], axis=1).max(axis=1).tail(14).mean()
    return float(value) if pd.notna(value) and value > 0 else None


def bos_buffer(data, configured_floor_points):
    try:
        floor = float(configured_floor_points)
        if not math.isfinite(floor) or floor < 0: raise ValueError
    except (TypeError, ValueError):
        floor = 10.0
    return max(floor*POINT_SIZE, .10*(atr14(data) or 0.0))


def trend_filter(data):
    if data is None or len(data) < 21:
        return {"trend":"NEUTRAL","buy_allowed":False,"sell_allowed":False}
    close=data.Close.astype(float); fast=close.ewm(span=9,adjust=False).mean().iloc[-1]; slow=close.ewm(span=21,adjust=False).mean().iloc[-1]; last=close.iloc[-1]
    bullish=last>slow and fast>slow; bearish=last<slow and fast<slow
    return {"trend":"BULLISH" if bullish else "BEARISH" if bearish else "NEUTRAL","buy_allowed":bool(bullish),"sell_allowed":bool(bearish),"ema_fast":round(float(fast),5),"ema_slow":round(float(slow),5),"close":round(float(last),5)}


def classify_consolidation(data):
    result={"is_consolidation":False,"conditions_met":0}
    atr=atr14(data)
    if data is None or len(data)<21 or atr is None: return result
    recent=data.tail(8); overlaps=0
    for pos in range(1,len(recent)):
        previous,current=recent.iloc[pos-1],recent.iloc[pos]
        denominator=min(float(previous.High-previous.Low),float(current.High-current.Low)); overlap=min(float(previous.High),float(current.High))-max(float(previous.Low),float(current.Low))
        overlaps += bool(denominator>0 and max(0.0,overlap)/denominator>=.60)
    close=data.Close.astype(float); ema9=close.ewm(span=9,adjust=False).mean(); ema21=close.ewm(span=21,adjust=False).mean()
    checks=[overlaps>=5,float(recent.High.max()-recent.Low.min())<=3.0*atr,abs(float(ema9.iloc[-1]-ema21.iloc[-1]))<=.20*atr and abs(float(ema9.iloc[-1]-ema9.iloc[-4]))<=.15*atr]
    result.update(is_consolidation=sum(checks)>=2,conditions_met=sum(checks),high_overlap=checks[0],compressed_range=checks[1],ema_compressed=checks[2]); return result


def event_leg(event):
    try: return abs(float(event["broken_level"])-float(event["event_invalidation_swing"]["price"]))
    except (KeyError,TypeError,ValueError): return None


def internal_two_bos(analysis,current):
    direction=str(current.get("direction","")).upper(); kind=str(current.get("event_type","")).upper(); current_leg=event_leg(current)
    details={"qualified":False,"pattern":None,"reason":None}
    if kind!="BOS" or direction not in {"BULLISH","BEARISH"}: details["reason"]="current_event_is_not_internal_bos"; return details
    prior=[e for e in analysis.get("events",[]) if int(e.get("break_index",-1))<int(current.get("break_index",-1))]
    if not prior: details["reason"]="no_previous_structure_event"; return details
    previous=prior[-1]; previous_leg=event_leg(previous)
    if str(previous.get("event_type","")).upper()!="BOS" or str(previous.get("direction","")).upper()!=direction: details["reason"]="previous_event_is_not_same_direction_bos"; return details
    if previous_leg is None or previous_leg>=MIN_SWING_POINTS*POINT_SIZE or current_leg is None or current_leg>=MIN_SWING_POINTS*POINT_SIZE: details["reason"]="bos_is_not_small_internal_structure"; return details
    try:
        pl,cl=float(previous["broken_level"]),float(current["broken_level"]); pi,ci=previous["event_invalidation_swing"],current["event_invalidation_swing"]; pp,cp=float(pi["price"]),float(ci["price"])
    except (KeyError,TypeError,ValueError): details["reason"]="internal_structure_prices_missing"; return details
    if direction=="BULLISH": ok=pi.get("type")==ci.get("type")=="LOW" and cl>pl and cp>pp; details["pattern"]="HH_HL"
    else: ok=pi.get("type")==ci.get("type")=="HIGH" and cl<pl and cp<pp; details["pattern"]="LH_LL"
    details.update(qualified=bool(ok),reason="second_small_bos_confirms_internal_structure" if ok else "second_small_bos_did_not_confirm_internal_structure"); return details


def utc(value):
    stamp=pd.Timestamp(value); return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def detect_valid_swings(data,left=2,right=2):
    if data is None or len(data)<left+right+3: return []
    highs,lows,index,raw=data.High.astype(float).tolist(),data.Low.astype(float).tolist(),list(data.index),[]
    for pos in range(left,len(data)-right):
        high,low=highs[pos],lows[pos]
        if high==max(highs[pos-left:pos+right+1]) and high>max(highs[pos-left:pos]+highs[pos+1:pos+right+1]): raw.append({"type":"HIGH","price":high,"index":pos,"time":pd.Timestamp(index[pos]).isoformat()})
        if low==min(lows[pos-left:pos+right+1]) and low<min(lows[pos-left:pos]+lows[pos+1:pos+right+1]): raw.append({"type":"LOW","price":low,"index":pos,"time":pd.Timestamp(index[pos]).isoformat()})
    raw.sort(key=lambda x:x["index"]); accepted=[]
    for swing in raw:
        opposite=next((x for x in reversed(accepted) if x["type"]!=swing["type"]),None); size=abs(swing["price"]-opposite["price"]) if opposite else abs(float(data.iloc[swing["index"]].High-data.iloc[swing["index"]].Low))
        swing["valid"]=size>=MIN_SWING_POINTS*POINT_SIZE
        if swing["valid"]: accepted.append(swing)
    return [x for x in raw if x["valid"]]


def select_tp2(swings, side, entry, risk, minimum, maximum):
    """Mirror strict_trader.select_tp2 without settings or runtime imports."""
    inverse="HIGH" if side=="BUY" else "LOW"
    candidates=[s for s in swings if s["type"]==inverse and (s["price"]>entry if side=="BUY" else s["price"]<entry)]
    candidates.sort(key=lambda s:abs(float(s["price"])-entry))
    for swing in candidates:
        rr=abs(float(swing["price"])-entry)/risk
        if minimum<=rr<=maximum:
            return {"tp2":float(swing["price"]),"rr":rr,"swing":swing,"source":"inverse_15m_swing"}
    rr=min(max(2.0,minimum),maximum)
    return {"tp2":entry+risk*rr if side=="BUY" else entry-risk*rr,"rr":rr,"swing":None,"source":f"fallback_{rr:g}r"}


def build_risk_levels(data,side,entry,setup_time,settings,invalidation,tp1_ratio):
    required="LOW" if side=="BUY" else "HIGH"
    if not isinstance(invalidation,dict) or invalidation.get("price") is None or str(invalidation.get("type","")).upper()!=required: return {"ok":False,"reason":"WAIT_NO_STRUCTURAL_SL_SWING"}
    setup=utc(setup_time)
    for key in ("swing_time","confirmation_time"):
        if invalidation.get(key) and utc(invalidation[key])>setup: return {"ok":False,"reason":"WAIT_SL_SWING_AFTER_SETUP"}
    swing_price=float(invalidation["price"]); stop=swing_price-50*POINT_SIZE if side=="BUY" else swing_price+50*POINT_SIZE; distance=abs(float(entry)-stop)
    if not (stop<entry if side=="BUY" else stop>entry): return {"ok":False,"reason":"WAIT_15M_SWING_WRONG_SIDE"}
    if distance<float(settings.get("minimum_sl_distance_points",100))*POINT_SIZE: return {"ok":False,"reason":"WAIT_SL_TOO_SMALL"}
    source=data.loc[pd.DatetimeIndex(data.index)<=setup]; swings=detect_valid_swings(source); inverse="HIGH" if side=="BUY" else "LOW"
    minimum,maximum=float(settings["minimum_rr"]),float(settings["maximum_rr"])
    selected=select_tp2(swings,side,entry,distance,minimum,maximum)
    tp2,rr,swing,source_name=selected["tp2"],selected["rr"],selected["swing"],selected["source"]
    tp1=entry+(tp2-entry)*tp1_ratio; protected=entry+(tp2-entry)*PROTECTED_SL_TP2_FRACTION
    return {"ok":True,"entry":round(entry,5),"stop_loss":round(stop,5),"tp1":round(tp1,5),"tp2":round(tp2,5),"protected_sl_price":round(protected,5),"risk_reward_ratio":round(rr,4),"tp_structure_source":source_name,"tp_structure_used":round(swing["price"],5) if swing else None,"sl_structure_source":"event_owned_15m_smc_swing"}
