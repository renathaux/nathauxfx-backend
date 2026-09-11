"""Controlled PAPER promotion bridge for the frozen V3B candidates.

This module is deliberately not wired into the running PAPER loop yet. It turns
an authoritative, persisted 5m BOS plus its immediately following closed 5m
candle into the PAPER-shaped candidate payload expected by the existing
execution safety layer.

Safety properties:
- EURUSD and XAUUSD use their separately frozen V3B point/risk math.
- Only durable, tradable 5m BOS events are eligible.
- No 15m, EMA, consolidation, or wick-quality rule is introduced.
- No broker call is made here.
- No PAPER/LIVE mode state is changed here.
- Lifecycle mutation is opt-in through an injected updater; the default is
  observation-only.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pandas as pd

from services.indicator_event_stream_service import read_authoritative_structure
from . import v3b_m5_frozen_candidate as eur_v3b
from . import v3b_xauusd_frozen_candidate as gold_v3b
from .v3a_m5_bos_body_50 import _bos_body_ratio


PAPER_V3B_MODEL = "PAPER_V3B_M5_FROZEN"
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


def _event_at(events, timestamp):
    target = _utc(timestamp)
    matches = []
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
        if event_time == target:
            matches.append(event)
    return matches[-1] if matches else None


def build_paper_v3b_candidate(
    symbol,
    data_5m,
    *,
    strict_trader_module,
    authoritative_reader=read_authoritative_structure,
    final_gate=None,
    lifecycle_updater=None,
):
    """Build one V3B PAPER candidate without opening a trade.

    ``authoritative_reader`` is dependency-injected for tests but defaults to the
    read-only durable indicator event reader. ``lifecycle_updater`` defaults to
    ``None`` so merely evaluating this bridge cannot mutate PAPER lifecycle.
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
    bos_open = _utc(frame.index[-2])
    second_open = _utc(frame.index[-1])
    if second_open != bos_open + pd.Timedelta(minutes=5):
        return _wait(normalized, "WAIT_V3B_PAPER_IMMEDIATE_SECOND_5M")

    try:
        point_size = float(
            strategy.POINT_SIZE if normalized == "XAUUSD" else strict_trader_module.point_size(normalized)
        )
        authority = authoritative_reader(
            frame,
            normalized,
            "5m",
            point_size,
        )
    except Exception as exc:
        return _wait(
            normalized,
            "WAIT_V3B_PAPER_AUTHORITATIVE_5M_EVENT",
            {"error": str(exc)},
        )

    event = _event_at((authority or {}).get("events"), bos_open)
    if event is None:
        return _wait(normalized, "WAIT_V3B_PAPER_5M_BOS")

    side = "BUY" if str(event.get("direction") or "").upper() == "BULLISH" else "SELL"
    try:
        bos_candle = frame.iloc[-2]
        second = frame.iloc[-1]
        body_ratio = float(_bos_body_ratio(bos_candle))
        second_open_price = float(second["Open"])
        second_close = float(second["Close"])
        broken_level = float(event["broken_level"])
    except Exception:
        return _wait(normalized, "WAIT_V3B_PAPER_5M_DATA_SHAPE")

    if body_ratio < float(strategy.MIN_BOS_BODY_RATIO):
        return _wait(
            normalized,
            "WAIT_V3B_PAPER_BOS_BODY",
            {
                "bos_body_ratio": body_ratio,
                "minimum_bos_body_ratio": float(strategy.MIN_BOS_BODY_RATIO),
            },
        )

    same_direction = (
        second_close > second_open_price if side == "BUY" else second_close < second_open_price
    )
    stays_beyond = (
        second_close > broken_level if side == "BUY" else second_close < broken_level
    )
    if not (same_direction and stays_beyond):
        return _wait(
            normalized,
            "WAIT_V3B_PAPER_SECOND_5M",
            {
                "side": side,
                "second_5m_same_direction": bool(same_direction),
                "second_5m_stays_beyond_bos_level": bool(stays_beyond),
            },
        )

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
