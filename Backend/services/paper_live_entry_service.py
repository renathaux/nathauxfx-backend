from __future__ import annotations

import copy
from datetime import datetime, timezone

import pandas as pd

from indicators.smc import analyze_structure
from services.fundamental_execution_guard import validate_fundamental_entry


PAPER_ENTRY_MODEL = "PAPER_5M_SWING_BOS_TWO_CLOSE"
PAPER_ENTRY_WAIT_REASON = "WAIT_PAPER_5M_SWING_BOS_TWO_CLOSE"
BOS_MIN_BODY_RATIO = 0.65
BOS_MAX_CLOSE_SIDE_WICK_RATIO = 0.20
SECOND_MIN_BODY_RATIO = 0.55
SECOND_MAX_CLOSE_SIDE_WICK_RATIO = 0.25
PAPER_5M_MAX_CLOSED_CANDLE_AGE_SECONDS = 15 * 60
PROTECTED_SL_TP2_FRACTION = 0.50


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


def clear_paper_entry_watch(symbol, reason=None):
    print("PAPER_5M_SWING_WATCH_CLEAR =", {
        "symbol": _normalize_symbol(symbol),
        "reason": reason,
        "legacy_15m_watch_used": False,
    })
    return None


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


def _wait_result(reason=PAPER_ENTRY_WAIT_REASON, details=None):
    return {
        "signal": "WAIT",
        "final_signal": "WAIT",
        "signal_before_filters": "WAIT",
        "signal_after_filters": "WAIT",
        "strategy_setup_complete": False,
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "paper_entry_ready": False,
        "paper_entry_reason": reason,
        "paper_entry_details": copy.deepcopy(details or {}),
        "strategy_setup_timeframe": "5m",
        "strategy_confirmation_timeframe": "5m",
        "setup_timeframe_used": "5m",
        "paper_swing_timeframe": "5m",
        "paper_risk_timeframe": "5m",
    }


def _frame_to_candles(frame, limit=500):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    rows = []
    for index, row in frame.tail(limit).iterrows():
        try:
            timestamp = pd.Timestamp(index)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            else:
                timestamp = timestamp.tz_convert("UTC")
            rows.append({
                "time": timestamp.isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
        except Exception:
            continue
    return rows


def _build_5m_risk_levels(
    strict_trader_module,
    closed_5m,
    symbol,
    side,
    entry,
    bos_event,
):
    try:
        execution_settings = strict_trader_module.get_cached_execution_settings()
    except Exception:
        execution_settings = {}

    minimum_sl_points = (execution_settings or {}).get(
        "minimum_sl_distance_points",
        strict_trader_module.MIN_SL_POINTS,
    )
    event_swing = copy.deepcopy((bos_event or {}).get("event_invalidation_swing"))
    setup_break_time = (bos_event or {}).get("timestamp")

    stop = strict_trader_module.select_structural_stop_loss(
        event_swing,
        side,
        float(entry),
        symbol,
        setup_break_time=setup_break_time,
        minimum_sl_distance_points=minimum_sl_points,
    )
    if not isinstance(stop, dict) or not stop.get("ok"):
        return {
            "ok": False,
            "reason": (stop or {}).get("reason") or "WAIT_PAPER_5M_SWING_SL",
            "stop_debug": copy.deepcopy(stop or {}),
        }

    setup_ts = _utc_timestamp(setup_break_time)
    swing_source = closed_5m.copy()
    if setup_ts is not None:
        try:
            source_index = pd.DatetimeIndex(swing_source.index)
            if source_index.tz is None:
                source_index = source_index.tz_localize("UTC")
            else:
                source_index = source_index.tz_convert("UTC")
            swing_source = swing_source.loc[source_index <= setup_ts]
        except Exception:
            return {"ok": False, "reason": "WAIT_PAPER_5M_SWING_SOURCE"}

    swings = strict_trader_module.detect_valid_swings(swing_source, symbol)
    risk = float(stop["distance"])
    tp2 = strict_trader_module.select_tp2(
        swings,
        side,
        float(entry),
        risk,
        symbol,
        minimum_rr=(execution_settings or {}).get("minimum_rr"),
        maximum_rr=(execution_settings or {}).get("maximum_rr"),
    )
    tp2_price = float(tp2["tp2"])
    try:
        tp1_ratio = strict_trader_module.shared.get_tp1_ratio_of_tp2()
    except Exception:
        tp1_ratio = 0.80

    if side == "BUY":
        tp1 = float(entry) + ((tp2_price - float(entry)) * tp1_ratio)
        protected = float(entry) + (
            (tp2_price - float(entry)) * PROTECTED_SL_TP2_FRACTION
        )
    else:
        tp1 = float(entry) - ((float(entry) - tp2_price) * tp1_ratio)
        protected = float(entry) - (
            (float(entry) - tp2_price) * PROTECTED_SL_TP2_FRACTION
        )

    dec = strict_trader_module.decimals(symbol)
    return {
        "ok": True,
        "entry": round(float(entry), dec),
        "stop_loss": round(float(stop["stop_loss"]), dec),
        "tp1": round(float(tp1), dec),
        "tp2": round(float(tp2_price), dec),
        "protected_sl_price": round(float(protected), dec),
        "risk": round(risk, dec),
        "reward": round(abs(tp2_price - float(entry)), dec),
        "risk_reward_ratio": round(float(tp2["rr"]), 4),
        "risk_reward": f"1:{round(float(tp2['rr']), 2):g}",
        "sl_swing_used": round(float(stop["swing"]["price"]), dec),
        "sl_swing_time": stop["swing"].get("time"),
        "sl_swing_confirmation_time": stop["swing"].get("confirmation_time"),
        "sl_swing_source": stop["swing"].get("source"),
        "sl_structure_source": "event_owned_5m_smc_swing",
        "tp_structure_used": (
            round(float(tp2["swing"]["price"]), dec)
            if tp2.get("swing")
            else None
        ),
        "tp_structure_source": (
            "inverse_5m_swing" if tp2.get("swing") else tp2.get("source")
        ),
        "rejected_tp_candidates": copy.deepcopy(
            tp2.get("rejected_tp_candidates") or []
        ),
    }


def _paper_final_gates(
    candidate,
    symbol,
    side,
    *,
    data_5m,
    data_15m=None,
    bos_close_time=None,
    second_close_time=None,
):
    details = {
        "symbol": symbol,
        "side": side,
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "paper_swing_timeframe": "5m",
        "paper_risk_timeframe": "5m",
        "uses_15m_swing": False,
    }

    fundamental = validate_fundamental_entry(symbol, side)
    details["fundamental_gate"] = copy.deepcopy(fundamental)
    if isinstance(fundamental, dict) and not fundamental.get("ok"):
        return {
            "ok": False,
            "reason": fundamental.get("reason") or "WAIT_FUNDAMENTAL_GATE",
            "details": details,
        }

    last_open = (
        _utc_timestamp(data_5m.index[-1])
        if data_5m is not None and len(data_5m)
        else None
    )
    last_close = last_open + pd.Timedelta(minutes=5) if last_open is not None else None
    now = pd.Timestamp(datetime.now(timezone.utc))
    age_seconds = (
        (now - last_close).total_seconds()
        if last_close is not None
        else None
    )
    details["five_m_feed_age_seconds"] = age_seconds
    if (
        age_seconds is None
        or age_seconds < -60
        or age_seconds > PAPER_5M_MAX_CLOSED_CANDLE_AGE_SECONDS
    ):
        return {
            "ok": False,
            "reason": "WAIT_STALE_5M_MARKET_FEED",
            "details": details,
        }

    try:
        import api as runtime_api

        panel_context = {
            symbol: candidate,
            "candles": {
                symbol: {
                    "5m": _frame_to_candles(data_5m),
                    "15m": _frame_to_candles(data_15m),
                    "1h": [],
                }
            },
        }
        news_state = runtime_api.evaluate_news_entry_state(
            panel_context,
            symbol,
            side=side,
            audit=True,
        )
        details["news_gate"] = copy.deepcopy(news_state)
        if news_state.get("allow_news_entry"):
            return {
                "ok": False,
                "reason": "WAIT_PAPER_LIVE_NEWS_ENTRY_MODE",
                "details": details,
            }
        if not news_state.get("allow_normal_entry", True):
            return {
                "ok": False,
                "reason": (
                    news_state.get("blocking_reason")
                    or news_state.get("authoritative_status")
                    or "NEWS BLOCK"
                ),
                "details": details,
            }

        fresh_after = _utc_timestamp(news_state.get("normal_fresh_after"))
        if fresh_after is not None:
            bos_close = _utc_timestamp(bos_close_time)
            second_close = _utc_timestamp(second_close_time)
            if (
                bos_close is None
                or second_close is None
                or bos_close <= fresh_after
                or second_close <= fresh_after
            ):
                return {
                    "ok": False,
                    "reason": "WAIT_FRESH_5M_SETUP_AFTER_NEWS",
                    "details": details,
                }

        rr = runtime_api.validate_live_trade_risk_reward(
            symbol,
            side,
            candidate.get("entry_price"),
            candidate.get("stop_loss"),
            candidate.get("tp2"),
        )
        details["risk_reward"] = copy.deepcopy(rr)
        if not rr.get("ok"):
            return {
                "ok": False,
                "reason": rr.get("reason") or "WAIT_INVALID_RR",
                "details": details,
            }
    except Exception as exc:
        details["runtime_gate_error"] = str(exc)
        return {
            "ok": False,
            "reason": "WAIT_PAPER_RUNTIME_GATE_UNAVAILABLE",
            "details": details,
        }

    return {"ok": True, "reason": None, "details": details}


def build_paper_entry_result(
    symbol,
    live_plan,
    data_5m,
    data_15m=None,
    *,
    strict_trader_module,
    final_gate=None,
):
    normalized = _normalize_symbol(symbol)
    if normalized not in {"EURUSD", "XAUUSD"}:
        return _wait_result("WAIT_PAPER_UNSUPPORTED_SYMBOL")
    if isinstance(live_plan, dict) and live_plan.get("market_closed"):
        return _wait_result("WAIT_PAPER_MARKET_CLOSED")

    closed_5m = strict_trader_module.closed_frame(data_5m, 5)
    if closed_5m is None or len(closed_5m) < 25:
        return _wait_result("WAIT_PAPER_5M_DATA")

    authority_5m = closed_5m.tail(250).copy()
    if len(authority_5m) < 25:
        return _wait_result("WAIT_PAPER_5M_DATA")

    analysis = analyze_structure(
        authority_5m,
        timeframe="5m",
        point_size=strict_trader_module.point_size(normalized),
    )
    bos_index = len(authority_5m) - 2
    bos_event = next(
        (
            event
            for event in reversed((analysis or {}).get("events") or [])
            if isinstance(event, dict)
            and str(event.get("event_type") or "").upper() == "BOS"
            and str(event.get("direction") or "").upper() in {"BULLISH", "BEARISH"}
            and int(event.get("break_index", -1)) == bos_index
        ),
        None,
    )
    if bos_event is None:
        return _wait_result("WAIT_PAPER_5M_BOS")

    side = (
        "BUY"
        if str(bos_event.get("direction") or "").upper() == "BULLISH"
        else "SELL"
    )
    bos_candle = authority_5m.iloc[-2]
    second_candle = authority_5m.iloc[-1]
    bos_quality = _candle_quality(bos_candle, side)
    second_quality = _candle_quality(second_candle, side)

    if not (
        bos_quality["direction_ok"]
        and bos_quality["body_ratio"] >= BOS_MIN_BODY_RATIO
        and bos_quality["close_side_wick_ratio"] <= BOS_MAX_CLOSE_SIDE_WICK_RATIO
    ):
        return _wait_result("WAIT_PAPER_5M_BOS_CANDLE_QUALITY", {
            "side": side,
            "bos_quality": bos_quality,
            "minimum_body_ratio": BOS_MIN_BODY_RATIO,
            "maximum_close_side_wick_ratio": BOS_MAX_CLOSE_SIDE_WICK_RATIO,
        })

    continuation_extends = (
        side == "BUY"
        and second_quality.get("close", 0) > bos_quality.get("close", 0)
    ) or (
        side == "SELL"
        and second_quality.get("close", 0) < bos_quality.get("close", 0)
    )
    if not (
        second_quality["direction_ok"]
        and second_quality["body_ratio"] >= SECOND_MIN_BODY_RATIO
        and second_quality["close_side_wick_ratio"] <= SECOND_MAX_CLOSE_SIDE_WICK_RATIO
        and continuation_extends
    ):
        return _wait_result("WAIT_PAPER_SECOND_5M_BODY", {
            "side": side,
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
        return _wait_result("WAIT_PAPER_5M_TIME_INVALID")

    trend_5m = strict_trader_module.trend_filter(authority_5m, normalized)
    ema_allowed = (
        side == "BUY" and bool(trend_5m.get("buy_allowed"))
    ) or (
        side == "SELL" and bool(trend_5m.get("sell_allowed"))
    )
    if not ema_allowed:
        return _wait_result("WAIT_PAPER_5M_EMA_NOT_ALLOWED", {
            "side": side,
            "trend_5m": copy.deepcopy(trend_5m),
        })

    entry = float(second_quality["close"])
    levels = _build_5m_risk_levels(
        strict_trader_module,
        authority_5m,
        normalized,
        side,
        entry,
        bos_event,
    )
    if not levels.get("ok"):
        return _wait_result(
            levels.get("reason") or "WAIT_PAPER_5M_RISK_LEVELS",
            {"levels": copy.deepcopy(levels)},
        )

    broken_level = bos_event.get("broken_level")
    candidate = {
        "symbol": normalized,
        "signal": side,
        "final_signal": side,
        "signal_before_filters": side,
        "signal_after_filters": side,
        "signal_text": f"{side} (5m swing BOS + second 5m close)",
        "buy_pct": 85 if side == "BUY" else 15,
        "sell_pct": 85 if side == "SELL" else 15,
        "confidence": 85,
        "market_condition": "STRUCTURE",
        "entry_quality": "STRICT_5M",
        "entry_timing": "SECOND 5M CLOSED CANDLE",
        "strategy_model": "paper_5m_swing_trader",
        "strategy_setup_timeframe": "5m",
        "strategy_confirmation_timeframe": "5m",
        "strategy_trend_timeframe": "5m EMA",
        "setup_timeframe_used": "5m",
        "final_signal_source": "paper_5m_smc",
        "strategy_setup_complete": True,
        "strategy_setup_type": f"PAPER_{side}_5M_SWING_BOS_TWO_CLOSE",
        "plan_type": f"PAPER 5M {side}",
        "plan_reason": (
            f"{side}: 5m BOS closed with strong body/small wick and the next "
            "5m candle closed with a good body in the same direction"
        ),
        "entry_price": levels["entry"],
        "price": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "protected_sl_price": levels["protected_sl_price"],
        "risk_reward": levels["risk_reward"],
        "risk_reward_ratio": levels["risk_reward_ratio"],
        "trend_5m": copy.deepcopy(trend_5m),
        "five_m_swing_break": copy.deepcopy(bos_event),
        "five_m_swing_break_confirmed": True,
        "five_m_bos_level": broken_level,
        "five_m_break_time": bos_event.get("timestamp"),
        "five_m_break_close_time": bos_close_time.isoformat(),
        "five_m_closed_candle_time": second_close_time.isoformat(),
        "setup_candle_time": second_close_time.isoformat(),
        "current_5m_entry_confirmation": True,
        "paper_entry_model": PAPER_ENTRY_MODEL,
        "paper_entry_ready": True,
        "paper_swing_timeframe": "5m",
        "paper_risk_timeframe": "5m",
        "uses_15m_swing": False,
        "swing_sl_debug": copy.deepcopy(levels),
        "structure_resistance": broken_level if side == "BUY" else None,
        "structure_support": broken_level if side == "SELL" else None,
        "paper_entry_details": {
            "side": side,
            "bos_event": copy.deepcopy(bos_event),
            "bos_quality": bos_quality,
            "second_quality": second_quality,
            "bos_close_time": bos_close_time.isoformat(),
            "second_close_time": second_close_time.isoformat(),
            "entry_at": "second_5m_close",
            "swing_timeframe": "5m",
            "risk_timeframe": "5m",
            "uses_15m_swing": False,
        },
    }

    gate = _paper_final_gates(
        candidate,
        normalized,
        side,
        data_5m=authority_5m,
        data_15m=data_15m,
        bos_close_time=bos_close_time,
        second_close_time=second_close_time,
    )
    candidate["paper_live_final_gate"] = copy.deepcopy(gate)
    if not gate.get("ok"):
        return _wait_result(
            gate.get("reason") or "WAIT_PAPER_FINAL_GATE",
            {
                **copy.deepcopy(candidate.get("paper_entry_details") or {}),
                "paper_live_final_gate": copy.deepcopy(gate),
            },
        )

    print("PAPER_5M_SWING_ENTRY_READY =", {
        "symbol": normalized,
        "side": side,
        "bos_level": broken_level,
        "bos_close_time": bos_close_time.isoformat(),
        "second_close_time": second_close_time.isoformat(),
        "entry": candidate["entry_price"],
        "sl": candidate["stop_loss"],
        "tp1": candidate["tp1"],
        "tp2": candidate["tp2"],
        "uses_15m_swing": False,
    })
    return candidate
