"""Controlled PAPER promotion bridge for the frozen V3B candidates.

This module is deliberately not wired into the running PAPER loop yet. It turns
an authoritative, persisted 5m BOS plus its immediately following closed 5m
candle into the PAPER-shaped candidate payload expected by the existing
execution safety layer.

Safety properties:
- EURUSD and XAUUSD use their separately frozen V3B point/risk math.
- Closed source candles advance the existing durable 5m authority before read.
- Only durable, tradable 5m BOS events are eligible.
- No 15m, EMA, consolidation, or wick-quality rule is introduced.
- No broker call is made here.
- No PAPER/LIVE mode state is changed here.
- Indicator candle/event persistence uses the existing immutable stream path;
  lifecycle mutation remains opt-in through an injected updater.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging

import pandas as pd

from indicators.smc import analyze_structure
from services.indicator_event_stream_service import (
    get_authoritative_structure,
    read_authoritative_structure,
)
from services.strategy_lab import v3b_m5_frozen_candidate as eur_v3b
from services.strategy_lab import v3b_xauusd_frozen_candidate as gold_v3b
from services.strategy_lab.v3a_m5_bos_body_50 import _bos_body_ratio


PAPER_V3B_MODEL = "PAPER_V3B_M5_FROZEN"
RECENT_BOS_RECOVERY_CANDLES = 1
logger = logging.getLogger(__name__)
SUPPORTED = {
    "EURUSD": eur_v3b,
    "XAUUSD": gold_v3b,
}


def _normalize_symbol(symbol):
    return str(symbol or "").upper().replace("/", "")


def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _wait(symbol, reason, details=None):
    return {
        "symbol": _normalize_symbol(symbol),
        "signal": "WAIT",
        "final_signal": "WAIT",
        "signal_before_filters": "WAIT",
        "signal_after_filters": "WAIT",
        "strategy_setup_complete": False,
        "paper_entry_model": PAPER_V3B_MODEL,
        "paper_entry_ready": False,
        "paper_entry_reason": reason,
        "paper_entry_details": copy.deepcopy(details or {}),
        "strategy_setup_timeframe": "5m",
        "strategy_confirmation_timeframe": "5m",
        "setup_timeframe_used": "5m",
        "paper_swing_timeframe": "5m",
        "paper_risk_timeframe": "5m",
    }


def _confirmation_identity(symbol, event_id, side, candle_open, candle_close, broken_level):
    payload = {
        "symbol": symbol,
        "timeframe": "5m",
        "source_indicator_event_id": event_id,
        "side": side,
        "candle_open_time": _utc(candle_open).isoformat(),
        "candle_close_time": _utc(candle_open + pd.Timedelta(minutes=5)).isoformat(),
        "close": float(candle_close),
        "broken_level": float(broken_level),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload, "m5v3b_" + digest


def _recent_bos_pairs(events, frame):
    """Return newest-first BOS/next-candle pairs with at most one-cycle recovery.

    The recovery allowance is deliberately one closed 5m candle.  It lets a
    restart or delayed evaluation recover the immediately completed pair, but
    it cannot turn an old BOS into a late entry.
    """
    latest_open = _utc(frame.index[-1])
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "").upper() != "BOS":
            continue
        if event.get("tradable") is not True:
            continue
        try:
            event_time = _utc(event.get("timestamp"))
        except Exception:
            continue
        second_open = event_time + pd.Timedelta(minutes=5)
        if event_time not in frame.index or second_open not in frame.index:
            continue
        lag_candles = int((latest_open - second_open) / pd.Timedelta(minutes=5))
        if 0 <= lag_candles <= RECENT_BOS_RECOVERY_CANDLES:
            rows.append((event_time, second_open, lag_candles, event))
    return sorted(rows, key=lambda item: (item[0], str(item[3].get("event_id") or "")), reverse=True)


def _freshness_details(symbol, latest_source, authority, *, error=None):
    durable_value = (authority or {}).get("stream_last_candle")
    try:
        latest_durable = _utc(durable_value)
    except Exception:
        latest_durable = None
    lag_minutes = (
        max(0.0, float((latest_source - latest_durable) / pd.Timedelta(minutes=1)))
        if latest_durable is not None
        else None
    )
    return {
        "symbol": symbol,
        "latest_source_closed_candle": latest_source.isoformat(),
        "latest_durable_candle": latest_durable.isoformat() if latest_durable is not None else None,
        "lag_minutes": lag_minutes,
        "lag_candles": (int(lag_minutes // 5) if lag_minutes is not None else None),
        "authority_status": (authority or {}).get("stream_status"),
        "error": str(error) if error else None,
    }


def build_paper_v3b_candidate(
    symbol,
    data_5m,
    *,
    strict_trader_module,
    authoritative_reader=None,
    authoritative_updater=None,
    final_gate=None,
    lifecycle_updater=None,
):
    """Build one V3B PAPER candidate without opening a trade.

    Production calls refresh the durable event stream with closed candles before
    reading it.  Injected readers remain observation-only for unit tests.
    ``lifecycle_updater`` defaults to ``None`` so candidate evaluation never
    mutates PAPER/LIVE lifecycle state.
    """
    normalized = _normalize_symbol(symbol)
    strategy = SUPPORTED.get(normalized)
    if strategy is None:
        return _wait(normalized, "WAIT_V3B_PAPER_UNSUPPORTED_SYMBOL")

    try:
        closed = strict_trader_module.closed_frame(data_5m, 5)
    except Exception as exc:
        return _wait(normalized, "WAIT_V3B_PAPER_5M_DATA", {"error": str(exc)})
    if closed is None or len(closed) < 2:
        return _wait(normalized, "WAIT_V3B_PAPER_5M_DATA")

    frame = closed.tail(250).copy()
    frame.index = pd.DatetimeIndex([_utc(value) for value in frame.index])
    latest_source = _utc(frame.index[-1])

    try:
        point_size = float(
            strategy.POINT_SIZE if normalized == "XAUUSD" else strict_trader_module.point_size(normalized)
        )
        reader = authoritative_reader or read_authoritative_structure
        updater = authoritative_updater
        if authoritative_reader is None and updater is None:
            updater = get_authoritative_structure
        if updater is not None:
            authority = updater(
                closed,
                normalized,
                "5m",
                point_size,
                analyzer=analyze_structure,
            )
            logger.info("V3B_5M_AUTHORITY_REFRESH %s", {
                "symbol": normalized,
                "latest_source_closed_candle": latest_source.isoformat(),
                "latest_durable_candle": (authority or {}).get("stream_last_candle"),
                "authority_status": (authority or {}).get("stream_status"),
            })
        else:
            authority = reader(frame, normalized, "5m", point_size)
    except Exception as exc:
        details = _freshness_details(normalized, latest_source, None, error=exc)
        logger.warning("V3B_5M_AUTHORITY_STALE %s", details)
        return _wait(
            normalized,
            "WAIT_V3B_5M_AUTHORITY_STALE",
            details,
        )

    # A production refresh must prove that the durable stream reached the exact
    # latest closed source candle. Injected test readers without stream metadata
    # keep their existing observation-only contract.
    if authoritative_reader is None or (authority or {}).get("stream_last_candle") is not None:
        freshness = _freshness_details(normalized, latest_source, authority)
        if (
            freshness["latest_durable_candle"] is None
            or freshness["lag_minutes"] != 0.0
            or freshness["authority_status"] != "READY"
        ):
            logger.warning("V3B_5M_AUTHORITY_STALE %s", freshness)
            return _wait(normalized, "WAIT_V3B_5M_AUTHORITY_STALE", freshness)

    pairs = _recent_bos_pairs((authority or {}).get("events"), frame)
    logger.info("V3B_RECENT_BOS_SCAN %s", {
        "symbol": normalized,
        "latest_source_closed_candle": latest_source.isoformat(),
        "recovery_candles": RECENT_BOS_RECOVERY_CANDLES,
        "candidate_event_ids": [item[3].get("event_id") for item in pairs],
    })
    if not pairs:
        return _wait(normalized, "WAIT_V3B_PAPER_5M_BOS")
    rejection = None
    selected = None
    for bos_open, second_open, lag_candles, event in pairs:
        try:
            bos_candle = frame.loc[bos_open]
            second = frame.loc[second_open]
            side = "BUY" if str(event.get("direction") or "").upper() == "BULLISH" else "SELL"
            body_ratio = float(_bos_body_ratio(bos_candle))
            second_open_price = float(second["Open"])
            second_close = float(second["Close"])
            broken_level = float(event["broken_level"])
        except Exception:
            rejection = _wait(normalized, "WAIT_V3B_PAPER_5M_DATA_SHAPE")
            continue
        if body_ratio < float(strategy.MIN_BOS_BODY_RATIO):
            rejection = rejection or _wait(normalized, "WAIT_V3B_PAPER_BOS_BODY", {
                "bos_body_ratio": body_ratio,
                "minimum_bos_body_ratio": float(strategy.MIN_BOS_BODY_RATIO),
                "source_indicator_event_id": event.get("event_id"),
            })
            continue
        same_direction = second_close > second_open_price if side == "BUY" else second_close < second_open_price
        stays_beyond = second_close > broken_level if side == "BUY" else second_close < broken_level
        if not (same_direction and stays_beyond):
            rejection = rejection or _wait(normalized, "WAIT_V3B_PAPER_SECOND_5M", {
                "side": side,
                "second_5m_same_direction": bool(same_direction),
                "second_5m_stays_beyond_bos_level": bool(stays_beyond),
                "bos_body_ratio": body_ratio,
                "minimum_bos_body_ratio": float(strategy.MIN_BOS_BODY_RATIO),
                "source_indicator_event_id": event.get("event_id"),
            })
            continue
        selected = (bos_open, second_open, lag_candles, event, side, body_ratio, second_close, broken_level)
        break
    if selected is None:
        return rejection or _wait(normalized, "WAIT_V3B_PAPER_5M_BOS")
    bos_open, second_open, lag_candles, event, side, body_ratio, second_close, broken_level = selected
    logger.info("V3B_CANDIDATE_SELECTED %s", {
        "symbol": normalized,
        "event_id": event.get("event_id"),
        "bos_candle": bos_open.isoformat(),
        "confirmation_candle": second_open.isoformat(),
        "recovery_lag_candles": lag_candles,
    })

    levels = strategy._fixed_levels(
        side,
        second_close,
        event.get("event_invalidation_swing"),
    )
    if not levels.get("ok"):
        return _wait(
            normalized,
            levels.get("reason") or "WAIT_V3B_PAPER_RISK",
            {"levels": copy.deepcopy(levels)},
        )

    event_id = event.get("event_id")
    event_identity = copy.deepcopy(event.get("event_identity") or {})
    broken_swing_time = event.get("broken_swing_timestamp")
    if not event_id or not broken_swing_time:
        return _wait(normalized, "WAIT_V3B_PAPER_DURABLE_IDENTITY")

    confirmation_identity, confirmation_id = _confirmation_identity(
        normalized,
        str(event_id),
        side,
        second_open,
        second_close,
        broken_level,
    )
    second_close_time = second_open + pd.Timedelta(minutes=5)
    bos_close_time = bos_open + pd.Timedelta(minutes=5)
    swing_type = "HIGH" if side == "BUY" else "LOW"
    setup_identity = {
        "symbol": normalized,
        "direction": side,
        "swing_type": swing_type,
        "swing_timestamp": _utc(broken_swing_time).isoformat(),
        "swing_price": broken_level,
        "bos_candle_timestamp": bos_open.isoformat(),
        "bos_level": broken_level,
        "confirmation_timestamp": second_close_time.isoformat(),
        "indicator_event_id": str(event_id),
        "m5_confirmation_id": confirmation_id,
        "setup_timeframe": "5m",
    }

    candidate = {
        "symbol": normalized,
        "signal": side,
        "final_signal": side,
        "signal_before_filters": side,
        "signal_after_filters": side,
        "signal_text": f"{side} (V3B frozen 5m BOS + next 5m close)",
        "strategy_setup_complete": True,
        "strategy_model": "v3b_m5_frozen_candidate",
        "strategy_setup_type": f"PAPER_{side}_V3B_M5",
        "strategy_setup_timeframe": "5m",
        "strategy_confirmation_timeframe": "5m",
        "setup_timeframe_used": "5m",
        "entry_timing": "SECOND 5M CLOSED CANDLE",
        "entry_price": levels["entry"],
        "price": levels["entry"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "protected_sl_price": levels["protected_sl_price"],
        "risk_reward_ratio": float(strategy.TARGET_RR),
        "risk_reward": f"1:{float(strategy.TARGET_RR):g}",
        "paper_entry_model": PAPER_V3B_MODEL,
        "paper_entry_ready": True,
        "paper_entry_reason": None,
        "paper_swing_timeframe": "5m",
        "paper_risk_timeframe": "5m",
        "source_indicator_event_id": str(event_id),
        "indicator_event_identity": event_identity,
        "m5_confirmation_id": confirmation_id,
        "m5_confirmation_identity": confirmation_identity,
        "setup_identity": setup_identity,
        "five_m_swing_break": copy.deepcopy(event),
        "five_m_swing_break_confirmed": True,
        "five_m_bos_level": broken_level,
        "five_m_break_time": bos_open.isoformat(),
        "five_m_break_close_time": bos_close_time.isoformat(),
        "five_m_closed_candle_time": second_close_time.isoformat(),
        "setup_candle_time": second_close_time.isoformat(),
        "confirmation_5m": {
            "confirmation_id": confirmation_id,
            "confirmation_identity": copy.deepcopy(confirmation_identity),
            "confirmation_close_time": second_close_time.isoformat(),
        },
        "protection_trigger_tp2_fraction": float(strategy.PROTECTION_TRIGGER_TP2_FRACTION),
        "protected_stop_tp2_fraction": float(strategy.PROTECTED_STOP_TP2_FRACTION),
        "no_partial_close_at_protection_trigger": True,
        "paper_entry_details": {
            "qualification_source": "authoritative_5m_indicator_event",
            "frozen_v3b": True,
            "side": side,
            "bos_body_ratio": body_ratio,
            "minimum_bos_body_ratio": float(strategy.MIN_BOS_BODY_RATIO),
            "second_5m_same_direction": True,
            "second_5m_stays_beyond_bos_level": True,
            "recovery_lag_candles": lag_candles,
            "entry_at": "second_5m_close",
            "target_rr": float(strategy.TARGET_RR),
            "protection_trigger_tp2_fraction": float(strategy.PROTECTION_TRIGGER_TP2_FRACTION),
            "protected_stop_tp2_fraction": float(strategy.PROTECTED_STOP_TP2_FRACTION),
        },
    }

    if final_gate is not None:
        gate = final_gate(
            candidate,
            normalized,
            side,
            data_5m=frame,
            data_15m=None,
        )
        candidate["paper_live_final_gate"] = copy.deepcopy(gate)
        if not isinstance(gate, dict) or not gate.get("ok"):
            reason = (gate or {}).get("reason") or "WAIT_V3B_PAPER_FINAL_GATE"
            candidate.update({
                "signal": "WAIT",
                "final_signal": "WAIT",
                "signal_after_filters": "WAIT",
                "strategy_setup_complete": False,
                "paper_entry_ready": False,
                "paper_entry_reason": reason,
            })

    if lifecycle_updater is not None:
        lifecycle_updater(
            str(event_id),
            "PAPER",
            "ELIGIBLE" if candidate.get("paper_entry_ready") else "BLOCKED",
            blocking_reason=candidate.get("paper_entry_reason"),
            m5_confirmation_id=confirmation_id,
            m5_confirmation_identity=copy.deepcopy(confirmation_identity),
            signal_setup_id=candidate.get("signal_setup_id"),
            owner_id="OWNER",
            account_id="PAPER",
        )

    return candidate
