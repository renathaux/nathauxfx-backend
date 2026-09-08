from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from indicators.smc import analyze_structure
from paths import DATA_DIR


PAPER_ENTRY_MODEL = "LIVE_SETUP_5M_BOS_TWO_CLOSE"
PAPER_ENTRY_WAIT_REASON = "WAIT_PAPER_5M_BOS_TWO_CLOSE"
PAPER_ENTRY_WATCH_FILE = os.path.join(DATA_DIR, "paper_live_entry_watch.json")
PAPER_SETUP_MAX_AGE_SECONDS = 4 * 15 * 60
BOS_MIN_BODY_RATIO = 0.65
BOS_MAX_CLOSE_SIDE_WICK_RATIO = 0.20
SECOND_MIN_BODY_RATIO = 0.55
SECOND_MAX_CLOSE_SIDE_WICK_RATIO = 0.25


def _normalize_symbol(symbol):
    return str(symbol or "").upper().replace("/", "")


def _utc_timestamp(value):
    if value in [None, "", "--"]:
        return None
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp
    except Exception:
        return None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    return value


def _load_watches():
    try:
        if not os.path.exists(PAPER_ENTRY_WATCH_FILE):
            return {}
        with open(PAPER_ENTRY_WATCH_FILE, "r", encoding="utf-8") as file:
            loaded = json.load(file)
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        print("PAPER_5M_ENTRY_WATCH_LOAD_ERROR =", str(exc))
        return {}


PAPER_ENTRY_WATCHES = _load_watches()


def _save_watches():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PAPER_ENTRY_WATCH_FILE, "w", encoding="utf-8") as file:
            json.dump(_json_safe(PAPER_ENTRY_WATCHES), file, indent=2)
    except Exception as exc:
        print("PAPER_5M_ENTRY_WATCH_SAVE_ERROR =", str(exc))


def clear_paper_entry_watch(symbol, reason=None):
    normalized = _normalize_symbol(symbol)
    removed = PAPER_ENTRY_WATCHES.pop(normalized, None)
    if removed is not None:
        _save_watches()
        print("PAPER_5M_ENTRY_WATCH_CLEARED =", {
            "symbol": normalized,
            "reason": reason,
        })
    return removed


def _watch_expired(watch, now=None):
    anchor = _utc_timestamp((watch or {}).get("fifteen_m_break_close_time"))
    if anchor is None:
        return True
    current = pd.Timestamp(now or datetime.now(timezone.utc))
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    return (current - anchor).total_seconds() > PAPER_SETUP_MAX_AGE_SECONDS


def _capture_live_setup(symbol, live_plan):
    if not isinstance(live_plan, dict):
        return None
    breakout = live_plan.get("fifteen_m_swing_break")
    if not isinstance(breakout, dict):
        return None
    side = str(breakout.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return None

    break_close_time = (
        breakout.get("break_close_time")
        or live_plan.get("fifteen_m_break_close_time")
    )
    if _utc_timestamp(break_close_time) is None:
        return None

    normalized = _normalize_symbol(symbol)
    watch = {
        "symbol": normalized,
        "side": side,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "fifteen_m_break_time": (
            breakout.get("break_time")
            or live_plan.get("fifteen_m_break_time")
        ),
        "fifteen_m_break_close_time": break_close_time,
        "fifteen_m_level": breakout.get("level"),
        "fifteen_m_break_type": breakout.get("break_type"),
        "fifteen_m_break_close": breakout.get("break_close"),
        "fifteen_m_swing": copy.deepcopy(breakout.get("swing")),
        "event_invalidation_swing": copy.deepcopy(
            breakout.get("event_invalidation_swing")
        ),
        "trend_15m": copy.deepcopy(live_plan.get("trend_15m") or {}),
        "source_setup_type": live_plan.get("strategy_setup_type"),
        "source_plan_type": live_plan.get("plan_type"),
    }
    existing = PAPER_ENTRY_WATCHES.get(normalized)
    identity_changed = not isinstance(existing, dict) or any(
        existing.get(key) != watch.get(key)
        for key in [
            "side",
            "fifteen_m_break_time",
            "fifteen_m_break_close_time",
            "fifteen_m_level",
        ]
    )
    PAPER_ENTRY_WATCHES[normalized] = watch
    if identity_changed:
        _save_watches()
        print("PAPER_5M_ENTRY_WATCH_CAPTURED =", {
            "symbol": normalized,
            "side": side,
            "fifteen_m_break_type": watch.get("fifteen_m_break_type"),
            "fifteen_m_level": watch.get("fifteen_m_level"),
            "fifteen_m_break_close_time": break_close_time,
        })
    return watch


def _current_watch(symbol, live_plan):
    normalized = _normalize_symbol(symbol)
    captured = _capture_live_setup(normalized, live_plan)
    watch = captured or PAPER_ENTRY_WATCHES.get(normalized)
    if not isinstance(watch, dict):
        return None
    if _watch_expired(watch):
        clear_paper_entry_watch(normalized, "15m setup expired before paper trigger")
        return None
    return watch


def _candle_quality(candle, side):
    try:
        open_price = float(candle["Open"])
        close_price = float(candle["Close"])
        high_price = float(candle["High"])
        low_price = float(candle["Low"])
    except Exception:
        return {
            "direction_ok": False,
            "body_ratio": 0.0,
            "close_side_wick_ratio": 1.0,
            "body": 0.0,
            "range": 0.0,
        }

    candle_range = high_price - low_price
    body = abs(close_price - open_price)
    if candle_range <= 0:
        return {
            "direction_ok": False,
            "body_ratio": 0.0,
            "close_side_wick_ratio": 1.0,
            "body": body,
            "range": candle_range,
        }

    direction_ok = (
        side == "BUY" and close_price > open_price
    ) or (
        side == "SELL" and close_price < open_price
    )
    upper_wick = max(0.0, high_price - max(open_price, close_price))
    lower_wick = max(0.0, min(open_price, close_price) - low_price)
    close_side_wick = upper_wick if side == "BUY" else lower_wick
    return {
        "direction_ok": direction_ok,
        "body_ratio": body / candle_range,
        "close_side_wick_ratio": close_side_wick / candle_range,
        "body": body,
        "range": candle_range,
        "open": open_price,
        "close": close_price,
        "high": high_price,
        "low": low_price,
    }


def _wait_copy(live_plan, reason=PAPER_ENTRY_WAIT_REASON, details=None):
    result = copy.deepcopy(live_plan) if isinstance(live_plan, dict) else {}
    result.update({
        "signal": "WAIT",
        "final_signal": "WAIT",
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "paper_entry_ready": False,
        "paper_entry_reason": reason,
        "paper_entry_details": copy.deepcopy(details or {}),
    })
    return result


def build_paper_entry_result(
    symbol,
    live_plan,
    data_5m,
    data_15m,
    *,
    strict_trader_module,
    final_gate=None,
):
    """Return a PAPER-only entry result built on the current LIVE setup.

    All 15m setup, trend, structural SL/TP and final macro/news checks stay tied
    to the live V1 model. The only intentional difference is entry timing:
    a same-direction closed 5m BOS candle with a strong body and small closing
    wick, followed immediately by another strong same-direction closed 5m
    candle. Entry is the second candle close.
    """
    normalized = _normalize_symbol(symbol)
    if not isinstance(live_plan, dict):
        return _wait_copy(live_plan, "WAIT_PAPER_LIVE_PLAN_MISSING")
    if live_plan.get("market_closed"):
        return _wait_copy(live_plan, "WAIT_PAPER_MARKET_CLOSED")

    watch = _current_watch(normalized, live_plan)
    if not isinstance(watch, dict):
        return _wait_copy(live_plan, "WAIT_PAPER_LIVE_15M_SETUP")

    side = str(watch.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return _wait_copy(live_plan, "WAIT_PAPER_LIVE_15M_SETUP")

    closed_5m = strict_trader_module.closed_frame(data_5m, 5)
    closed_15m = strict_trader_module.closed_frame(data_15m, 15)
    if closed_5m is None or len(closed_5m) < 12:
        return _wait_copy(live_plan, "WAIT_PAPER_5M_DATA")
    if closed_15m is None or len(closed_15m) < 25:
        return _wait_copy(live_plan, "WAIT_PAPER_15M_DATA")

    authority_5m = closed_5m.tail(250).copy()
    if len(authority_5m) < 2:
        return _wait_copy(live_plan, "WAIT_PAPER_5M_DATA")

    analysis = analyze_structure(
        authority_5m,
        timeframe="5m",
        point_size=strict_trader_module.point_size(normalized),
    )
    bos_index = len(authority_5m) - 2
    expected_direction = "BULLISH" if side == "BUY" else "BEARISH"
    bos_event = next(
        (
            event
            for event in reversed((analysis or {}).get("events") or [])
            if isinstance(event, dict)
            and str(event.get("event_type") or "").upper() == "BOS"
            and str(event.get("direction") or "").upper() == expected_direction
            and int(event.get("break_index", -1)) == bos_index
        ),
        None,
    )
    if bos_event is None:
        return _wait_copy(live_plan, "WAIT_PAPER_5M_BOS", {
            "required_direction": expected_direction,
        })

    bos_candle = authority_5m.iloc[-2]
    second_candle = authority_5m.iloc[-1]
    bos_quality = _candle_quality(bos_candle, side)
    second_quality = _candle_quality(second_candle, side)

    if not (
        bos_quality["direction_ok"]
        and bos_quality["body_ratio"] >= BOS_MIN_BODY_RATIO
        and bos_quality["close_side_wick_ratio"] <= BOS_MAX_CLOSE_SIDE_WICK_RATIO
    ):
        return _wait_copy(live_plan, "WAIT_PAPER_5M_BOS_CANDLE_QUALITY", {
            "bos_quality": bos_quality,
            "minimum_body_ratio": BOS_MIN_BODY_RATIO,
            "maximum_close_side_wick_ratio": BOS_MAX_CLOSE_SIDE_WICK_RATIO,
        })

    continuation_extends = (
        side == "BUY" and second_quality.get("close", 0) > bos_quality.get("close", 0)
    ) or (
        side == "SELL" and second_quality.get("close", 0) < bos_quality.get("close", 0)
    )
    if not (
        second_quality["direction_ok"]
        and second_quality["body_ratio"] >= SECOND_MIN_BODY_RATIO
        and second_quality["close_side_wick_ratio"] <= SECOND_MAX_CLOSE_SIDE_WICK_RATIO
        and continuation_extends
    ):
        return _wait_copy(live_plan, "WAIT_PAPER_SECOND_5M_BODY", {
            "bos_quality": bos_quality,
            "second_quality": second_quality,
            "minimum_body_ratio": SECOND_MIN_BODY_RATIO,
            "maximum_close_side_wick_ratio": SECOND_MAX_CLOSE_SIDE_WICK_RATIO,
            "continuation_extends_bos_close": continuation_extends,
        })

    try:
        bos_open_time = pd.Timestamp(authority_5m.index[-2])
        second_open_time = pd.Timestamp(authority_5m.index[-1])
        if bos_open_time.tzinfo is None:
            bos_open_time = bos_open_time.tz_localize("UTC")
        else:
            bos_open_time = bos_open_time.tz_convert("UTC")
        if second_open_time.tzinfo is None:
            second_open_time = second_open_time.tz_localize("UTC")
        else:
            second_open_time = second_open_time.tz_convert("UTC")
        bos_close_time = bos_open_time + pd.Timedelta(minutes=5)
        second_close_time = second_open_time + pd.Timedelta(minutes=5)
    except Exception:
        return _wait_copy(live_plan, "WAIT_PAPER_5M_TIME_INVALID")

    anchor = _utc_timestamp(watch.get("fifteen_m_break_close_time"))
    if anchor is None or bos_close_time <= anchor or second_close_time <= bos_close_time:
        return _wait_copy(live_plan, "WAIT_PAPER_5M_AFTER_15M_SETUP", {
            "fifteen_m_break_close_time": watch.get("fifteen_m_break_close_time"),
            "bos_close_time": bos_close_time.isoformat(),
            "second_close_time": second_close_time.isoformat(),
        })

    entry = float(second_quality["close"])
    try:
        execution_settings = strict_trader_module.get_cached_execution_settings()
    except Exception:
        execution_settings = None
    levels = strict_trader_module.build_risk_levels(
        closed_15m,
        side,
        entry,
        normalized,
        setup_break_time=watch.get("fifteen_m_break_time"),
        execution_settings=execution_settings,
        event_invalidation_swing=copy.deepcopy(
            watch.get("event_invalidation_swing")
        ),
    )
    if not isinstance(levels, dict) or not levels.get("ok"):
        return _wait_copy(live_plan, (
            (levels or {}).get("reason") or "WAIT_PAPER_LIVE_RISK_LEVELS"
        ), {
            "levels": copy.deepcopy(levels or {}),
        })

    swing = watch.get("fifteen_m_swing") or {}
    setup_identity = {
        "symbol": normalized,
        "direction": side,
        "swing_type": swing.get("type"),
        "swing_timestamp": swing.get("time"),
        "swing_price": swing.get("price"),
        "bos_candle_timestamp": watch.get("fifteen_m_break_time"),
        "bos_level": watch.get("fifteen_m_level"),
        "confirmation_timestamp": second_close_time.isoformat(),
    }
    candidate = copy.deepcopy(live_plan)
    stage_states = dict(candidate.get("strategy_stage_states") or {})
    stage_states.update({
        "market_data": "PASSED",
        "swing_detection": "PASSED",
        "fifteen_m_bos": "PASSED",
        "fifteen_m_close": "PASSED",
        "ema": "PASSED",
        "five_m_confirmation": "PASSED",
        "swing_sl": "PASSED",
        "tp_rr": "PASSED",
        "execution": "PASSED",
    })
    confirmation = {
        "side": side,
        "close_confirmed": True,
        "closed_candle_time": second_open_time.isoformat(),
        "confirmation_close_time": second_close_time.isoformat(),
        "close": entry,
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "bos_event": copy.deepcopy(bos_event),
        "bos_candle": bos_quality,
        "continuation_candle": second_quality,
    }
    candidate.update({
        "symbol": normalized,
        "signal": side,
        "final_signal": side,
        "signal_before_filters": side,
        "signal_after_filters": side,
        "signal_text": f"{side} (paper 5m BOS + second body close)",
        "entry_price": levels.get("entry"),
        "price": levels.get("entry"),
        "stop_loss": levels.get("stop_loss"),
        "tp1": levels.get("tp1"),
        "tp2": levels.get("tp2"),
        "protected_sl_price": levels.get("protected_sl_price"),
        "risk_reward": levels.get("risk_reward"),
        "risk_reward_ratio": levels.get("risk_reward_ratio"),
        "entry_timing": "PAPER 5M BOS + SECOND BODY CLOSE",
        "entry_quality": "STRICT",
        "strategy_setup_complete": True,
        "strategy_setup_type": f"{side}_LIVE_SETUP_PAPER_5M_BOS_2_CLOSE",
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "paper_entry_ready": True,
        "paper_entry_reason": "PAPER_5M_BOS_TWO_CLOSE_CONFIRMED",
        "paper_entry_details": {
            "bos_event": copy.deepcopy(bos_event),
            "bos_quality": bos_quality,
            "second_quality": second_quality,
            "fifteen_m_watch": copy.deepcopy(watch),
        },
        "fifteen_m_swing_break": {
            **copy.deepcopy(live_plan.get("fifteen_m_swing_break") or {}),
            "side": side,
            "level": watch.get("fifteen_m_level"),
            "break_time": watch.get("fifteen_m_break_time"),
            "break_close_time": watch.get("fifteen_m_break_close_time"),
            "break_type": watch.get("fifteen_m_break_type"),
            "swing": copy.deepcopy(watch.get("fifteen_m_swing")),
            "event_invalidation_swing": copy.deepcopy(
                watch.get("event_invalidation_swing")
            ),
        },
        "fifteen_m_swing_level": watch.get("fifteen_m_level"),
        "fifteen_m_break_time": watch.get("fifteen_m_break_time"),
        "fifteen_m_break_close_time": watch.get("fifteen_m_break_close_time"),
        "trend_15m": copy.deepcopy(watch.get("trend_15m") or {}),
        "confirmation_5m": confirmation,
        "five_m_closed_candle_time": second_close_time.isoformat(),
        "setup_candle_time": second_close_time.isoformat(),
        "setup_identity": setup_identity,
        "strategy_stage_states": stage_states,
        "swing_sl_debug": copy.deepcopy(levels),
        "blocked_by": None,
        "blocked_reason": None,
        "block_reason": None,
        "blocker_rule_name": None,
    })

    if callable(final_gate):
        gate = final_gate(
            candidate,
            normalized,
            side,
            data_5m=closed_5m,
            data_15m=closed_15m,
        )
        if not isinstance(gate, dict) or not gate.get("ok"):
            reason = (
                (gate or {}).get("reason")
                or "WAIT_PAPER_LIVE_FINAL_GATE"
            )
            return _wait_copy(live_plan, reason, {
                "paper_candidate": {
                    "side": side,
                    "entry": levels.get("entry"),
                    "sl": levels.get("stop_loss"),
                    "tp2": levels.get("tp2"),
                    "setup_identity": setup_identity,
                },
                "final_gate": copy.deepcopy(gate or {}),
            })
        candidate["paper_live_final_gate"] = copy.deepcopy(gate)

    print("PAPER_5M_BOS_TWO_CLOSE_READY =", {
        "symbol": normalized,
        "side": side,
        "entry": candidate.get("entry_price"),
        "sl": candidate.get("stop_loss"),
        "tp1": candidate.get("tp1"),
        "tp2": candidate.get("tp2"),
        "bos_close_time": bos_close_time.isoformat(),
        "second_close_time": second_close_time.isoformat(),
        "bos_body_ratio": round(bos_quality["body_ratio"], 4),
        "second_body_ratio": round(second_quality["body_ratio"], 4),
    })
    return candidate
