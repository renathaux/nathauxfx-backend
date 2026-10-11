"""Backend-authoritative, opt-in post-news entry controls.

This module is deliberately independent from the normal strict strategy.  It
can block entries around live high-impact releases and, only when explicitly
enabled, construct one confirmed post-news plan per event and symbol.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paths import state_file


MODES = {"OFF", "BLOCK_ONLY", "TRADE_CONFIRMED"}
PRE_NEWS_MINUTES = 30
OPPORTUNITY_MINUTES = 30
BLOCK_ONLY_AFTER_MINUTES = 15
ACTUAL_STABILIZATION_SECONDS = 60
MAX_DATA_AGE_SECONDS = 15 * 60
CONFIRMATION_BODY_ATR = 0.50
CONFIRMATION_CLOSE_OUTER_FRACTION = 0.25
CONFIRMATION_BUFFER_ATR = 0.10
MAX_SPIKE_ATR = 3.0
MAX_CHASE_ATR = 0.50
TARGET_RR = 2.0
MIN_CONFIRMATION_POINTS = 20
BUFFER_POINTS = 10
SL_BUFFER_POINTS = 50

LIVE_SOURCES = {
    "jblanked_live", "jblanked_cache", "jblanked", "fmp", "finnhub"
}
SYMBOL_CURRENCIES = {"EURUSD": {"EUR", "USD"}, "XAUUSD": {"USD"}}

# Thresholds are absolute normalized-surprise magnitudes.  The denominator
# floor prevents near-zero forecasts from producing unbounded scores.
EVENT_RULES = {
    "core_cpi": {"patterns": ("core cpi",), "higher_bullish": True, "threshold": 0.20, "minimum_denominator": 0.10, "family": "inflation"},
    "cpi": {"patterns": ("cpi", "consumer price"), "higher_bullish": True, "threshold": 0.20, "minimum_denominator": 0.10, "family": "inflation"},
    "core_pce": {"patterns": ("core pce",), "higher_bullish": True, "threshold": 0.20, "minimum_denominator": 0.10, "family": "inflation"},
    "pce": {"patterns": ("pce", "personal consumption expenditures"), "higher_bullish": True, "threshold": 0.20, "minimum_denominator": 0.10, "family": "inflation"},
    "nfp": {"patterns": ("nonfarm payroll", "non-farm payroll", "nfp"), "higher_bullish": True, "threshold": 0.20, "minimum_denominator": 50_000.0, "family": "labor"},
    "unemployment": {"patterns": ("unemployment rate",), "higher_bullish": False, "threshold": 0.02, "minimum_denominator": 0.10, "family": "labor"},
    "fomc_rate": {"patterns": ("fomc rate", "fed interest rate", "federal funds rate"), "higher_bullish": True, "threshold": 0.04, "minimum_denominator": 0.25, "family": "usd_rate"},
    "ecb_rate": {"patterns": ("ecb rate", "ecb interest rate"), "higher_bullish": True, "threshold": 0.04, "minimum_denominator": 0.25, "family": "eur_rate"},
    "retail_sales": {"patterns": ("retail sales",), "higher_bullish": True, "threshold": 0.25, "minimum_denominator": 0.10, "family": "growth"},
    "gdp": {"patterns": ("gdp", "gross domestic product"), "higher_bullish": True, "threshold": 0.15, "minimum_denominator": 0.10, "family": "growth"},
}

STATE_FILE = state_file('news_trading_state.json', override='NEWS_TRADING_STATE_FILE')
AUDIT_FILE = state_file('news_trading_audit.jsonl', override='NEWS_TRADING_AUDIT_FILE')
_LOCK = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc)


def get_mode():
    # Imported lazily to keep this strategy module easy to unit test and avoid
    # making database initialization part of module import ordering.
    from services.news_mode_service import get_effective_mode

    return get_effective_mode()["mode"]


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else value


def parse_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if value in (None, "", "--", "N/A"):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # live authorization requires an unambiguous timestamp
    return parsed.astimezone(timezone.utc)


def parse_number(value):
    if value in (None, "", "--", "N/A"):
        return None
    text = str(value).strip().replace(",", "")
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier, text = 1_000.0, text[:-1]
    elif text.upper().endswith("M"):
        multiplier, text = 1_000_000.0, text[:-1]
    elif text.upper().endswith("B"):
        multiplier, text = 1_000_000_000.0, text[:-1]
    try:
        return float(text) * multiplier
    except (TypeError, ValueError):
        return None


def normalize_symbol(symbol):
    value = str(symbol or "").upper().replace("/", "").replace("_", "")
    return "XAUUSD" if value in {"GOLD", "XAUUSD"} else value


def point_size(symbol):
    return 0.01 if normalize_symbol(symbol) == "XAUUSD" else 0.00001


def price_digits(symbol):
    return 2 if normalize_symbol(symbol) == "XAUUSD" else 5


def classify_event(event):
    title = str(event.get("event_name") or event.get("event") or "").lower()
    for key, rule in EVENT_RULES.items():
        if any(pattern in title for pattern in rule["patterns"]):
            return key, rule
    return None, None


def event_time(event):
    return parse_time(event.get("release_time") or event.get("time_utc") or event.get("time"))


def event_id(event):
    supplied = event.get("event_id") or event.get("id")
    if supplied not in (None, ""):
        return str(supplied)
    basis = "|".join([
        str(event.get("currency") or "").upper(),
        str(event.get("event_name") or event.get("event") or "").lower(),
        _iso(event_time(event)) or "invalid-time",
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _empty_state():
    return {
        "version": 1,
        "opportunities": {},
        "normal_fresh_after": {},
        "audit": [],
        "mode_enabled_at": None,
    }


def load_state(path=None):
    target = Path(path or STATE_FILE)
    from startup_recovery.checkpoint_store import read_runtime
    from startup_recovery.types import RecoveryError
    data = read_runtime(target, 'news_trading_state')
    if data is None:
        return _empty_state()  # Only admitted, proven absence reaches this branch.
    if not isinstance(data, dict):
        raise RecoveryError('CHECKPOINT_SCHEMA_INVALID')
    return {**_empty_state(), **data}


def save_state(state, path=None):
    from startup_recovery.checkpoint_store import write_runtime
    write_runtime(Path(path or STATE_FILE), 'news_trading_state', state)


def opportunity_key(event, symbol):
    return f"{event_id(event)}|{normalize_symbol(symbol)}"


def get_opportunity(event, symbol, path=None):
    return load_state(path)["opportunities"].get(opportunity_key(event, symbol))


def mark_opportunity(event, symbol, status, reason, details=None, path=None):
    with _LOCK:
        state = load_state(path)
        key = opportunity_key(event, symbol)
        previous = state["opportunities"].get(key) or {}
        record = {
            **previous,
            "event_id": event_id(event),
            "event_name": event.get("event_name") or event.get("event"),
            "symbol": normalize_symbol(symbol),
            "currency": str(event.get("currency") or "").upper(),
            "release_time": _iso(event_time(event)),
            "status": status,
            "reason": reason,
            "consumed": status in {
                "EXECUTED", "EXPIRED", "REJECTED", "INVALID", "MIXED", "CANCELLED"
            },
            "updated_at": _iso(utc_now()),
            "details": details or previous.get("details") or {},
        }
        state["opportunities"][key] = record
        if status == "EXPIRED" and record.get("release_time"):
            state["normal_fresh_after"][normalize_symbol(symbol)] = _iso(
                parse_time(record["release_time"]) + timedelta(minutes=OPPORTUNITY_MINUTES)
            )
        save_state(state, path)
        return record


def record_broker_result(event, symbol, result, path=None):
    status = "EXECUTED" if result.get("ok") else "REJECTED"
    details = {
        "broker_order_id": result.get("order_id") or result.get("broker_order_id"),
        "broker_position_id": result.get("position_id") or result.get("broker_position_id"),
        "broker_reason": result.get("reason") or result.get("message"),
    }
    return mark_opportunity(event, symbol, status, details["broker_reason"] or status, details, path)


def apply_mode_transition(previous_mode, new_mode, enabled_at=None, path=None):
    """Apply mode side effects without touching any broker position.

    Pending/ready opportunities are consumed when leaving confirmed trading so
    they can never be revived by toggling the setting later.
    """
    previous = str(previous_mode or "OFF").upper()
    current = str(new_mode or "OFF").upper()
    if current not in MODES:
        raise ValueError("Invalid news trading mode")
    changed_at = parse_time(enabled_at) or utc_now()
    cancelled = []
    with _LOCK:
        state = load_state(path)
        if current in {"OFF", "BLOCK_ONLY"}:
            for key, record in list(state["opportunities"].items()):
                if record.get("status") in {"PENDING", "READY"} and not record.get("consumed"):
                    record = {
                        **record,
                        "status": "CANCELLED",
                        "reason": f"NEWS_MODE_CHANGED_TO_{current}",
                        "consumed": True,
                        "updated_at": _iso(changed_at),
                    }
                    state["opportunities"][key] = record
                    cancelled.append(key)
        if current == "TRADE_CONFIRMED" and previous != current:
            state["mode_enabled_at"] = _iso(changed_at)
        elif current != "TRADE_CONFIRMED":
            state["mode_enabled_at"] = None
        save_state(state, path)
    result = {
        "previous_mode": previous,
        "new_mode": current,
        "changed_at": _iso(changed_at),
        "cancelled_opportunities": cancelled,
        "existing_positions_unchanged": True,
    }
    print("NEWS_TRADING_MODE_TRANSITION =", result)
    return result


def set_normal_fresh_after(symbol, timestamp, path=None):
    with _LOCK:
        state = load_state(path)
        state["normal_fresh_after"][normalize_symbol(symbol)] = _iso(timestamp)
        save_state(state, path)
    return _iso(timestamp)


def mark_running_trade_closed(symbol, closed_at=None, path=None):
    with _LOCK:
        state = load_state(path)
        changed = False
        for record in state["opportunities"].values():
            if record.get("symbol") == normalize_symbol(symbol) and record.get("status") == "EXECUTED":
                record["status"] = "COMPLETED"
                record["closed_at"] = _iso(parse_time(closed_at) or utc_now())
                record["updated_at"] = _iso(utc_now())
                changed = True
        if changed:
            save_state(state, path)
        return changed


def _audit(decision, path=None):
    payload = {**decision, "audited_at": _iso(utc_now())}
    target = Path(path or AUDIT_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return payload


def _event_is_relevant(event, symbol):
    return (
        str(event.get("impact") or "").upper() == "HIGH"
        and str(event.get("currency") or "").upper() in SYMBOL_CURRENCIES.get(normalize_symbol(symbol), set())
        and classify_event(event)[0] is not None
    )


def select_active_event(events, symbol, now):
    candidates = [event for event in (events or []) if _event_is_relevant(event, symbol) and event_time(event)]
    if not candidates:
        return None
    in_window = [event for event in candidates if event_time(event) - timedelta(minutes=PRE_NEWS_MINUTES) <= now <= event_time(event) + timedelta(minutes=OPPORTUNITY_MINUTES)]
    if in_window:
        return min(in_window, key=lambda item: abs((event_time(item) - now).total_seconds()))
    upcoming = [event for event in candidates if event_time(event) > now]
    return min(upcoming, key=event_time) if upcoming else max(candidates, key=event_time)


def evaluate_surprise(event):
    event_type, rule = classify_event(event)
    actual = parse_number(event.get("actual"))
    forecast = parse_number(event.get("forecast"))
    if not rule or actual is None or forecast is None:
        return {"ok": False, "reason": "WAIT_NEWS_DATA_MISSING", "event_type": event_type}
    raw = actual - forecast
    normalized = raw / max(abs(forecast), rule["minimum_denominator"])
    currency_direction = 1 if normalized > 0 else -1 if normalized < 0 else 0
    if not rule["higher_bullish"]:
        currency_direction *= -1
    threshold = float(rule["threshold"])
    if abs(normalized) < threshold:
        return {"ok": False, "reason": "WAIT_NEWS_SURPRISE_TOO_SMALL", "event_type": event_type, "raw_surprise": raw, "normalized_surprise": normalized, "threshold": threshold, "currency_direction": currency_direction}
    return {
        "ok": True,
        "reason": "NEWS_SURPRISE_HIGH_CONFIDENCE",
        "event_type": event_type,
        "family": rule["family"],
        "raw_surprise": raw,
        "normalized_surprise": normalized,
        "threshold": threshold,
        "currency_direction": currency_direction,
        "news_confidence": "HIGH",
    }


def symbol_direction(currency, currency_direction, symbol):
    currency = str(currency or "").upper()
    symbol = normalize_symbol(symbol)
    if currency == "USD" and symbol in {"EURUSD", "XAUUSD"}:
        return "SELL" if currency_direction > 0 else "BUY"
    if currency == "EUR" and symbol == "EURUSD":
        return "BUY" if currency_direction > 0 else "SELL"
    return None


def evaluate_related_results(events, selected, symbol):
    release = event_time(selected)
    primary = evaluate_surprise(selected)
    if not primary.get("ok"):
        return primary
    directions = []
    components = []
    for item in events or [selected]:
        if not _event_is_relevant(item, symbol) or str(item.get("currency") or "").upper() != str(selected.get("currency") or "").upper():
            continue
        item_time = event_time(item)
        if not item_time or abs((item_time - release).total_seconds()) > 90:
            continue
        result = evaluate_surprise(item)
        if result.get("reason") == "WAIT_NEWS_DATA_MISSING":
            return {
                "ok": False,
                "reason": "WAIT_NEWS_DATA_MISSING",
                "components": components + [{
                    "event": item.get("event_name") or item.get("event"),
                    **result,
                }],
            }
        if result.get("currency_direction"):
            directions.append(result["currency_direction"])
            components.append({"event": item.get("event_name") or item.get("event"), **result})
    if directions and min(directions) < 0 < max(directions):
        return {"ok": False, "reason": "WAIT_NEWS_MIXED_RESULT", "components": components}
    return {**primary, "components": components}


def _candle(candle):
    try:
        opened = parse_time(candle.get("time") or candle.get("datetime") or candle.get("Datetime"))
        return {
            "open_time": opened,
            "close_time": opened + timedelta(minutes=5) if opened else None,
            "open": float(candle.get("open", candle.get("Open"))),
            "high": float(candle.get("high", candle.get("High"))),
            "low": float(candle.get("low", candle.get("Low"))),
            "close": float(candle.get("close", candle.get("Close"))),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def atr(candles, period=14):
    values = []
    previous_close = None
    for item in candles:
        high, low, close = item["high"], item["low"], item["close"]
        tr = high - low if previous_close is None else max(high - low, abs(high - previous_close), abs(low - previous_close))
        values.append(tr)
        previous_close = close
    if len(values) < period:
        return None
    value = sum(values[-period:]) / period
    return value if math.isfinite(value) and value > 0 else None


def _ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for current in values[period:]:
        value = current * multiplier + value * (1.0 - multiplier)
    return value


def evaluate_post_news_15m_conflict(candles, event, side, now):
    parsed = []
    for raw in candles or []:
        try:
            opened = parse_time(raw.get("time") or raw.get("datetime") or raw.get("Datetime"))
            if not opened:
                continue
            parsed.append({
                "open_time": opened,
                "close_time": opened + timedelta(minutes=15),
                "open": float(raw.get("open", raw.get("Open"))),
                "close": float(raw.get("close", raw.get("Close"))),
            })
        except (AttributeError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item["open_time"])
    release = event_time(event)
    post = [item for item in parsed if item["open_time"] > release and item["close_time"] <= now]
    if not post:
        return {"conflict": False, "checked": False}
    first = post[0]
    closes = [item["close"] for item in parsed if item["close_time"] <= first["close_time"]]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    if ema9 is None or ema21 is None:
        return {"conflict": False, "checked": False, "reason": "POST_NEWS_15M_EMA_UNAVAILABLE"}
    candle_opposes = first["close"] < first["open"] if side == "BUY" else first["close"] > first["open"]
    ema_opposes = ema9 < ema21 if side == "BUY" else ema9 > ema21
    return {
        "conflict": bool(candle_opposes and ema_opposes),
        "checked": True,
        "first_post_news_15m": {key: _iso(value) for key, value in first.items()},
        "ema9": ema9,
        "ema21": ema21,
        "candle_opposes": candle_opposes,
        "ema_opposes": ema_opposes,
    }


def evaluate_confirmation(candles, event, side, symbol, now, entry_price=None):
    parsed = [item for item in (_candle(raw) for raw in (candles or [])) if item and item["open_time"]]
    parsed.sort(key=lambda item: item["open_time"])
    release = event_time(event)
    before = [item for item in parsed if item["close_time"] <= release]
    post = [item for item in parsed if item["open_time"] > release and item["close_time"] <= now]
    if len(before) < 14 or not post:
        return {"ok": False, "reason": "WAITING_FOR_5M_CONFIRMATION"}
    confirmation = post[0]
    atr_value = atr([item for item in parsed if item["close_time"] <= confirmation["open_time"]])
    if atr_value is None:
        return {"ok": False, "reason": "WAIT_NEWS_ATR_UNAVAILABLE"}
    pre_range = before[-4:]
    pre_high = max(item["high"] for item in pre_range)
    pre_low = min(item["low"] for item in pre_range)
    body = abs(confirmation["close"] - confirmation["open"])
    candle_range = confirmation["high"] - confirmation["low"]
    minimum_body = max(CONFIRMATION_BODY_ATR * atr_value, MIN_CONFIRMATION_POINTS * point_size(symbol))
    buffer_value = max(BUFFER_POINTS * point_size(symbol), CONFIRMATION_BUFFER_ATR * atr_value)
    direction_ok = confirmation["close"] > confirmation["open"] if side == "BUY" else confirmation["close"] < confirmation["open"]
    outer_ok = (
        confirmation["close"] >= confirmation["high"] - CONFIRMATION_CLOSE_OUTER_FRACTION * candle_range
        if side == "BUY"
        else confirmation["close"] <= confirmation["low"] + CONFIRMATION_CLOSE_OUTER_FRACTION * candle_range
    )
    break_ok = confirmation["close"] > pre_high + buffer_value if side == "BUY" else confirmation["close"] < pre_low - buffer_value
    details = {
        "pre_news_high": pre_high,
        "pre_news_low": pre_low,
        "confirmation_candle": {key: _iso(value) for key, value in confirmation.items()},
        "atr5": atr_value,
        "body": body,
        "minimum_body": minimum_body,
        "confirmation_buffer": buffer_value,
        "direction_ok": direction_ok,
        "outer_close_ok": outer_ok,
        "pre_news_break_ok": break_ok,
    }
    if candle_range > MAX_SPIKE_ATR * atr_value:
        return {"ok": False, "reason": "WAIT_NEWS_SPIKE_TOO_LARGE", **details}
    if not direction_ok:
        return {"ok": False, "reason": "WAIT_NEWS_CONFIRMATION_DIRECTION", **details}
    if body < minimum_body or not outer_ok:
        return {"ok": False, "reason": "WAIT_NEWS_CONFIRMATION_WEAK", **details}
    if not break_ok:
        return {"ok": False, "reason": "WAIT_NEWS_RANGE_NOT_BROKEN", **details}
    executable_entry = float(entry_price if entry_price is not None else confirmation["close"])
    chase = abs(executable_entry - confirmation["close"])
    details.update({"entry_price": executable_entry, "chase_distance": chase, "maximum_chase": MAX_CHASE_ATR * atr_value})
    if chase > MAX_CHASE_ATR * atr_value:
        return {"ok": False, "reason": "WAIT_NEWS_ENTRY_TOO_LATE", **details}
    point = point_size(symbol)
    if side == "BUY":
        stop = min(confirmation["low"], pre_low) - SL_BUFFER_POINTS * point
        risk = executable_entry - stop
        tp2 = executable_entry + TARGET_RR * risk
    else:
        stop = max(confirmation["high"], pre_high) + SL_BUFFER_POINTS * point
        risk = stop - executable_entry
        tp2 = executable_entry - TARGET_RR * risk
    if risk <= 0 or risk > MAX_SPIKE_ATR * atr_value:
        return {"ok": False, "reason": "WAIT_NEWS_SL_TOO_WIDE", **details, "stop_loss": stop, "risk": risk}
    tp1 = executable_entry + (tp2 - executable_entry) * 0.80
    digits = price_digits(symbol)
    return {
        "ok": True,
        "reason": f"NEWS_{side}_READY",
        **details,
        "side": side,
        "entry": round(executable_entry, digits),
        "stop_loss": round(stop, digits),
        "tp1": round(tp1, digits),
        "tp2": round(tp2, digits),
        "risk_reward_ratio": TARGET_RR,
    }


def _base_decision(mode, symbol, event, phase, status, reason):
    release = event_time(event) if event else None
    return {
        "mode": mode,
        "symbol": normalize_symbol(symbol),
        "phase": phase,
        "authoritative_status": status,
        "allow_normal_entry": phase == "NORMAL",
        "allow_news_entry": False,
        "manage_existing_positions": True,
        "blocking_reason": reason,
        "event_id": event_id(event) if event else None,
        "event_name": (event.get("event_name") or event.get("event")) if event else None,
        "provider": event.get("source") if event else None,
        "provider_timestamp": event.get("provider_timestamp") if event else None,
        "currency": event.get("currency") if event else None,
        "impact": event.get("impact") if event else None,
        "actual": event.get("actual") if event else None,
        "forecast": event.get("forecast") if event else None,
        "previous": event.get("previous") if event else None,
        "event_time": _iso(release),
        "opportunity_expiration": _iso(release + timedelta(minutes=OPPORTUNITY_MINUTES)) if release else None,
    }


def evaluate(events, symbol, candles_5m=None, candles_15m=None, entry_price=None, now=None, data_age_seconds=0, state_path=None, audit=False):
    now = parse_time(now) or utc_now()
    symbol = normalize_symbol(symbol)
    mode = get_mode()
    selected = select_active_event(events, symbol, now)
    if mode == "OFF":
        return _base_decision(mode, symbol, selected, "NORMAL", "NORMAL", "NEWS_TRADING_MODE_OFF")
    if not selected:
        return _base_decision(mode, symbol, None, "NORMAL", "NORMAL", "NO_RELEVANT_MAJOR_EVENT")
    release = event_time(selected)
    if now < release - timedelta(minutes=PRE_NEWS_MINUTES):
        return _base_decision(mode, symbol, selected, "NORMAL", "NORMAL", "OUTSIDE_NEWS_WINDOW")
    if now < release:
        decision = _base_decision(mode, symbol, selected, "PRE_NEWS", "NEWS BLOCK", "PRE_NEWS_BLOCK")
        decision["allow_normal_entry"] = False
        return _audit(decision) if audit else decision
    if mode == "BLOCK_ONLY":
        block_end = release + timedelta(minutes=BLOCK_ONLY_AFTER_MINUTES)
        if now <= block_end:
            decision = _base_decision(mode, symbol, selected, "RELEASE_LOCK", "NEWS BLOCK", "BLOCK_ONLY_POST_RELEASE_SAFETY")
            decision["allow_normal_entry"] = False
            return _audit(decision) if audit else decision
        return _base_decision(mode, symbol, selected, "NORMAL", "NORMAL", "BLOCK_ONLY_WINDOW_COMPLETE")
    expires = release + timedelta(minutes=OPPORTUNITY_MINUTES)
    existing = get_opportunity(selected, symbol, state_path)
    if existing and existing.get("consumed"):
        normal_fresh_after = None
        if now > expires:
            normal_fresh_after = set_normal_fresh_after(
                symbol,
                expires,
                state_path,
            )
        status = "NEWS TRADE RUNNING" if existing.get("status") == "EXECUTED" else "NEWS OPPORTUNITY EXPIRED" if existing.get("status") == "EXPIRED" else "NEWS BLOCK"
        decision = _base_decision(mode, symbol, selected, "POST_NEWS_EVALUATION", status, existing.get("reason") or "NEWS_EVENT_CONSUMED")
        decision.update({"consumed": True, "opportunity_state": existing})
        decision["allow_normal_entry"] = now > expires
        decision["normal_fresh_after"] = normal_fresh_after
        return decision
    if now > expires:
        record = mark_opportunity(selected, symbol, "EXPIRED", "NEWS_OPPORTUNITY_EXPIRED", path=state_path)
        decision = _base_decision(mode, symbol, selected, "NORMAL", "NEWS OPPORTUNITY EXPIRED", "NEWS_OPPORTUNITY_EXPIRED")
        decision.update({"consumed": True, "opportunity_state": record, "normal_fresh_after": _iso(expires)})
        return _audit(decision) if audit else decision
    source = str(selected.get("source") or "").lower()
    actual = parse_number(selected.get("actual"))
    forecast = parse_number(selected.get("forecast"))
    if source not in LIVE_SOURCES or actual is None or forecast is None or not release or data_age_seconds > MAX_DATA_AGE_SECONDS:
        reason = "WAIT_NEWS_DATA_MISSING" if actual is None or forecast is None else "WAIT_NEWS_DATA_STALE" if data_age_seconds > MAX_DATA_AGE_SECONDS else "WAIT_NEWS_LIVE_PROVIDER_REQUIRED"
        status = "WAITING FOR ACTUAL" if actual is None else "NEWS BLOCK"
        decision = _base_decision(mode, symbol, selected, "RELEASE_LOCK", status, reason)
        decision.update({"is_live_news": source in LIVE_SOURCES, "data_age_seconds": data_age_seconds})
        decision["allow_normal_entry"] = False
        return _audit(decision) if audit else decision
    if now < release + timedelta(seconds=ACTUAL_STABILIZATION_SECONDS):
        decision = _base_decision(mode, symbol, selected, "RELEASE_LOCK", "WAITING FOR ACTUAL", "WAIT_NEWS_ACTUAL_STABILIZING")
        decision["allow_normal_entry"] = False
        return decision
    surprise = evaluate_related_results(events, selected, symbol)
    if not surprise.get("ok"):
        if surprise.get("reason") == "WAIT_NEWS_DATA_MISSING":
            decision = _base_decision(
                mode,
                symbol,
                selected,
                "RELEASE_LOCK",
                "WAITING FOR ACTUAL",
                "WAIT_NEWS_DATA_MISSING",
            )
            decision.update(surprise)
            decision["allow_normal_entry"] = False
            return _audit(decision) if audit else decision
        status = "MIXED RESULT" if surprise.get("reason") == "WAIT_NEWS_MIXED_RESULT" else "NEWS BLOCK"
        record = mark_opportunity(selected, symbol, "MIXED" if status == "MIXED RESULT" else "REJECTED", surprise.get("reason"), surprise, state_path)
        decision = _base_decision(mode, symbol, selected, "POST_NEWS_EVALUATION", status, surprise.get("reason"))
        decision.update({**surprise, "consumed": True, "opportunity_state": record})
        decision["allow_normal_entry"] = False
        return _audit(decision) if audit else decision
    side = symbol_direction(selected.get("currency"), surprise["currency_direction"], symbol)
    post_news_15m = evaluate_post_news_15m_conflict(
        candles_15m,
        selected,
        side,
        now,
    )
    if post_news_15m.get("conflict"):
        reason = "WAIT_NEWS_POST_15M_CONFLICT"
        record = mark_opportunity(
            selected,
            symbol,
            "INVALID",
            reason,
            post_news_15m,
            state_path,
        )
        decision = _base_decision(
            mode,
            symbol,
            selected,
            "POST_NEWS_EVALUATION",
            "NEWS BLOCK",
            reason,
        )
        decision.update({
            **surprise,
            "expected_symbol_direction": side,
            "post_news_15m": post_news_15m,
            "consumed": True,
            "opportunity_state": record,
        })
        decision["allow_normal_entry"] = False
        return _audit(decision) if audit else decision
    confirmation = evaluate_confirmation(candles_5m, selected, side, symbol, now, entry_price)
    if not confirmation.get("ok"):
        waiting = confirmation.get("reason") in {"WAITING_FOR_5M_CONFIRMATION", "WAIT_NEWS_ATR_UNAVAILABLE"}
        if not waiting:
            record = mark_opportunity(selected, symbol, "REJECTED", confirmation.get("reason"), confirmation, state_path)
        else:
            record = mark_opportunity(selected, symbol, "PENDING", confirmation.get("reason"), confirmation, state_path)
        decision = _base_decision(mode, symbol, selected, "POST_NEWS_EVALUATION", "WAITING FOR 5M CONFIRMATION", confirmation.get("reason"))
        decision.update({**surprise, **confirmation, "expected_symbol_direction": side, "post_news_15m": post_news_15m, "consumed": not waiting, "opportunity_state": record})
        decision["allow_normal_entry"] = False
        return _audit(decision) if audit else decision
    plan = {
        "symbol": symbol,
        "signal": side,
        "final_signal": side,
        "signal_before_filters": side,
        "signal_after_filters": side,
        "signal_text": f"{side} (confirmed post-news reaction)",
        "entry_price": confirmation["entry"],
        "price": confirmation["entry"],
        "stop_loss": confirmation["stop_loss"],
        "tp1": confirmation["tp1"],
        "tp2": confirmation["tp2"],
        "risk_reward_ratio": confirmation["risk_reward_ratio"],
        "risk_reward": "1:2",
        "confidence": 90,
        "buy_pct": 90 if side == "BUY" else 10,
        "sell_pct": 90 if side == "SELL" else 10,
        "strategy_setup_complete": True,
        "strategy_setup_type": f"{side}_CONFIRMED_POST_NEWS",
        "strategy_model": "confirmed_post_news",
        "plan_type": f"NEWS {side}",
        "plan_reason": confirmation["reason"],
        "entry_timing": "FIRST CLOSED POST-RELEASE 5M",
        "final_signal_source": "news_trading",
        "signal_setup_id": f"NEWS|{event_id(selected)}|{symbol}",
        "news_event_id": event_id(selected),
        "news_event": selected,
        "news_confirmation": confirmation,
        "blocked_by": None,
        "blocked_reason": None,
    }
    record = mark_opportunity(selected, symbol, "READY", confirmation["reason"], {**surprise, **confirmation}, state_path)
    decision = _base_decision(mode, symbol, selected, "POST_NEWS_EVALUATION", f"NEWS {side} READY", confirmation["reason"])
    decision.update({**surprise, **confirmation, "expected_symbol_direction": side, "post_news_15m": post_news_15m, "allow_news_entry": True, "allow_normal_entry": False, "news_plan": plan, "opportunity_state": record, "consumed": False})
    return _audit(decision) if audit else decision


def final_gate(expected, events, symbol, candles_5m, entry_price, active_position=False, cooldown_active=False, feed_fresh=True, spread_ok=True, now=None, state_path=None, audit=False, data_age_seconds=0):
    current = evaluate(events, symbol, candles_5m=candles_5m, entry_price=entry_price, now=now, state_path=state_path, audit=audit, data_age_seconds=data_age_seconds)
    reasons = []
    if not current.get("allow_news_entry"):
        reasons.append(current.get("blocking_reason") or "NEWS_NOT_READY")
    if current.get("event_id") != expected.get("event_id") or current.get("expected_symbol_direction") != expected.get("expected_symbol_direction"):
        reasons.append("WAIT_NEWS_DATA_CHANGED_BEFORE_EXECUTION")
    if active_position:
        reasons.append("WAIT_ACTIVE_BROKER_POSITION")
    if cooldown_active:
        reasons.append("WAIT_POST_CLOSE_COOLDOWN")
    if not feed_fresh:
        reasons.append("WAIT_STALE_MARKET_FEED")
    if not spread_ok:
        reasons.append("WAIT_SPREAD_TOO_WIDE")
    if not reasons:
        expected_plan = expected.get("news_plan") or {}
        current_plan = current.get("news_plan") or {}
        for field in ("signal", "entry_price", "stop_loss", "tp1", "tp2", "signal_setup_id"):
            if current_plan.get(field) != expected_plan.get(field):
                reasons.append("WAIT_NEWS_DATA_CHANGED_BEFORE_EXECUTION")
                break
    return {"ok": not reasons, "reason": reasons[0] if reasons else "NEWS_FINAL_GATE_OK", "reasons": reasons, "decision": current}
