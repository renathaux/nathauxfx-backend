"""Disabled-by-default LIVE handoff for the frozen V3B candidates.

This module prepares an execution payload that can be consumed by the existing
LIVE execution core, but it never calls the broker or turns LIVE on itself.

Promotion safety:
- the feature gate defaults OFF;
- EURUSD and XAUUSD reuse the separately frozen V3B PAPER bridge/risk math;
- only durable/tradable 5m BOS events can qualify;
- the setup must carry the durable 5m event identity and deterministic 5m
  confirmation identity expected by the execution-safety layer;
- no EMA, 15m, consolidation, session, wick, or fundamental filter is added;
- no order submission, position mutation, lifecycle mutation, or mode toggle is
  performed by this module.

The actual broker-facing `execute_live_order_core(..., source="auto")` handoff
must remain a separate explicitly authorized wiring step.
"""
from __future__ import annotations

import copy
import os

from services.paper_v3b_bridge import (
    PAPER_V3B_MODEL,
    build_paper_v3b_candidate,
)


LIVE_V3B_MODEL = "LIVE_V3B_M5_FROZEN"
LIVE_V3B_ENV = "V3B_LIVE_STRATEGY_ENABLED"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def live_v3b_enabled(environ=None):
    """Return True only for an explicit opt-in environment value."""
    source = os.environ if environ is None else environ
    return str(source.get(LIVE_V3B_ENV, "")).strip().lower() in _TRUE_VALUES


def _wait(symbol, reason, details=None):
    return {
        "symbol": str(symbol or "").upper().replace("/", ""),
        "signal": "WAIT",
        "final_signal": "WAIT",
        "signal_after_filters": "WAIT",
        "strategy_setup_complete": False,
        "live_strategy_model": LIVE_V3B_MODEL,
        "live_v3b_ready": False,
        "live_v3b_reason": reason,
        "live_v3b_details": copy.deepcopy(details or {}),
    }


def build_live_v3b_candidate(
    symbol,
    data_5m,
    *,
    strict_trader_module,
    setup_id_builder,
    authoritative_reader=None,
    final_gate=None,
    enabled=None,
):
    """Build a LIVE-shaped V3B candidate without submitting an order.

    `enabled` is dependency-injected for tests. Runtime callers should omit it,
    which keeps the feature OFF unless V3B_LIVE_STRATEGY_ENABLED is explicitly
    enabled in the process environment.

    `final_gate`, when supplied, is for execution-safety checks only. Strategy
    filters must not be added here because V3B is frozen.
    """
    is_enabled = live_v3b_enabled() if enabled is None else bool(enabled)
    if not is_enabled:
        return _wait(symbol, "WAIT_V3B_LIVE_DISABLED")

    kwargs = {
        "strict_trader_module": strict_trader_module,
        "final_gate": None,
        "lifecycle_updater": None,
    }
    if authoritative_reader is not None:
        kwargs["authoritative_reader"] = authoritative_reader

    candidate = build_paper_v3b_candidate(symbol, data_5m, **kwargs)
    if not isinstance(candidate, dict) or not candidate.get("paper_entry_ready"):
        reason = (
            (candidate or {}).get("paper_entry_reason")
            or "WAIT_V3B_LIVE_QUALIFICATION"
        )
        return _wait(
            symbol,
            reason,
            {"source_candidate": copy.deepcopy(candidate or {})},
        )

    side = str(candidate.get("signal") or "").upper()
    if side not in {"BUY", "SELL"}:
        return _wait(symbol, "WAIT_V3B_LIVE_SIDE")

    candidate = copy.deepcopy(candidate)
    candidate.update({
        "action": side,
        "side": side,
        "entry": candidate.get("entry_price"),
        "sl": candidate.get("stop_loss"),
        "mode": "LIVE",
        "live_strategy_model": LIVE_V3B_MODEL,
        "live_v3b_ready": True,
        "live_v3b_reason": None,
        "live_v3b_source_model": PAPER_V3B_MODEL,
        "live_v3b_feature_gate": LIVE_V3B_ENV,
    })

    try:
        setup_id = setup_id_builder(candidate, side)
    except Exception as exc:
        return _wait(
            symbol,
            "WAIT_V3B_LIVE_SETUP_ID",
            {"error": str(exc)},
        )
    if not setup_id:
        return _wait(symbol, "WAIT_V3B_LIVE_SETUP_ID")
    candidate["signal_setup_id"] = str(setup_id)

    identity = candidate.get("setup_identity") or {}
    required = {
        "symbol",
        "direction",
        "swing_type",
        "swing_timestamp",
        "swing_price",
        "bos_candle_timestamp",
        "bos_level",
        "confirmation_timestamp",
        "indicator_event_id",
        "m5_confirmation_id",
        "setup_timeframe",
    }
    missing = sorted(
        field for field in required if identity.get(field) in {None, ""}
    )
    if missing or str(identity.get("setup_timeframe") or "").lower() != "5m":
        return _wait(
            symbol,
            "WAIT_V3B_LIVE_DURABLE_IDENTITY",
            {"missing_fields": missing},
        )

    if final_gate is not None:
        try:
            gate = final_gate(candidate, side)
        except Exception as exc:
            return _wait(
                symbol,
                "WAIT_V3B_LIVE_FINAL_GATE_UNAVAILABLE",
                {"error": str(exc)},
            )
        candidate["live_v3b_final_gate"] = copy.deepcopy(gate)
        if not isinstance(gate, dict) or not gate.get("ok"):
            reason = (gate or {}).get("reason") or "WAIT_V3B_LIVE_FINAL_GATE"
            candidate.update({
                "signal": "WAIT",
                "final_signal": "WAIT",
                "signal_after_filters": "WAIT",
                "strategy_setup_complete": False,
                "live_v3b_ready": False,
                "live_v3b_reason": reason,
            })

    return candidate


def build_live_v3b_execution_payload(candidate):
    """Return the broker-core input shape without calling the broker.

    Risk sizing, account verification, one-position-per-symbol checks,
    idempotency claims, broker reconciliation and actual submission remain owned
    by the existing LIVE execution core.
    """
    if not isinstance(candidate, dict) or not candidate.get("live_v3b_ready"):
        return {
            "ok": False,
            "reason": (
                (candidate or {}).get("live_v3b_reason")
                or "WAIT_V3B_LIVE_NOT_READY"
            ),
            "payload": None,
        }

    side = str(candidate.get("side") or candidate.get("signal") or "").upper()
    if side not in {"BUY", "SELL"}:
        return {"ok": False, "reason": "WAIT_V3B_LIVE_SIDE", "payload": None}

    payload = {
        "symbol": candidate.get("symbol"),
        "side": side,
        "action": side,
        "signal": side,
        "entry": candidate.get("entry"),
        "sl": candidate.get("sl"),
        "tp1": candidate.get("tp1"),
        "tp2": candidate.get("tp2"),
        "protected_sl_price": candidate.get("protected_sl_price"),
        "risk_reward": candidate.get("risk_reward"),
        "risk_reward_ratio": candidate.get("risk_reward_ratio"),
        "signal_setup_id": candidate.get("signal_setup_id"),
        "setup_identity": copy.deepcopy(candidate.get("setup_identity") or {}),
        "source_indicator_event_id": candidate.get("source_indicator_event_id"),
        "indicator_event_identity": copy.deepcopy(
            candidate.get("indicator_event_identity") or {}
        ),
        "m5_confirmation_id": candidate.get("m5_confirmation_id"),
        "m5_confirmation_identity": copy.deepcopy(
            candidate.get("m5_confirmation_identity") or {}
        ),
        "confirmation_5m": copy.deepcopy(candidate.get("confirmation_5m") or {}),
        "five_m_break_time": candidate.get("five_m_break_time"),
        "five_m_break_close_time": candidate.get("five_m_break_close_time"),
        "five_m_closed_candle_time": candidate.get("five_m_closed_candle_time"),
        "setup_candle_time": candidate.get("setup_candle_time"),
        "strategy_setup_type": candidate.get("strategy_setup_type"),
        "strategy_setup_complete": True,
        "live_strategy_model": LIVE_V3B_MODEL,
        "mode": "LIVE",
    }

    required_payload = {
        "symbol",
        "entry",
        "sl",
        "tp1",
        "tp2",
        "signal_setup_id",
        "source_indicator_event_id",
        "m5_confirmation_id",
    }
    missing = sorted(
        field for field in required_payload if payload.get(field) in {None, ""}
    )
    if missing:
        return {
            "ok": False,
            "reason": "WAIT_V3B_LIVE_PAYLOAD_INCOMPLETE",
            "details": {"missing_fields": missing},
            "payload": None,
        }

    return {"ok": True, "reason": None, "payload": payload}
