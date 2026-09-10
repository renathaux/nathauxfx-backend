"""Chronological, in-memory Strategy Lab replay coordinator."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from services.strategy_settings_service import defaults, get_strategy_settings

from .baseline_v1 import build_trade, candidates, resolve_trade
from .data_source import load_candles
from .metrics import summarize_r

MAX_SPAN_DAYS = 120


def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def run_replay(symbol, strategy, start, end=None, *, session_factory=None, frames=None, settings=None):
    if symbol != "EURUSD" or strategy != "baseline_v1":
        raise ValueError("Phase 1 supports only EURUSD baseline_v1")
    start, end = _utc(start), _utc(end or datetime.now(timezone.utc))
    if end <= start or end-start > pd.Timedelta(days=MAX_SPAN_DAYS):
        raise ValueError(f"replay range must be between 1 second and {MAX_SPAN_DAYS} days")
    if settings is None:
        loaded = get_strategy_settings(session_factory) if session_factory else get_strategy_settings()
        settings = {**defaults(), **(loaded.get("current", loaded) if isinstance(loaded, dict) else {})}
    else:
        settings = {**defaults(), **settings}
    if frames is None:
        kwargs = {"session_factory": session_factory} if session_factory else {}
        frame15 = load_candles(symbol, "15m", start.to_pydatetime(), end.to_pydatetime(), **kwargs)
        frame5 = load_candles(symbol, "5m", start.to_pydatetime(), end.to_pydatetime(), **kwargs)
    else:
        frame15, frame5 = frames
    if frame15.empty or frame5.empty:
        raise ValueError("historical EURUSD 15m and 5m indicator candles are required")

    counts = {key: 0 for key in (
        "rejected_by_structure", "rejected_by_m15_buffer", "rejected_by_ema",
        "rejected_by_consolidation", "rejected_by_m5_confirmation_expired",
        "rejected_by_risk_rr", "skipped_active_trade",
    )}
    events, trades, active = [], [], None
    for event, timestamp, prefix, side, leg, structure_ok in candidates(frame15, frame5, start, end, settings):
        events.append(event)
        if not structure_ok:
            counts["rejected_by_structure"] += 1
            continue
        trade, rejection = build_trade(event, timestamp, prefix, frame5, side, leg, settings, end)
        if rejection:
            counts[rejection] += 1
            continue
        if active and active["result"] == "UNRESOLVED_OPEN":
            resolve_trade(active, frame5, pd.Timestamp(trade["entry_timestamp"]))
        if active and active["result"] == "UNRESOLVED_OPEN":
            counts["skipped_active_trade"] += 1
            continue
        active = trade
        trades.append(active)

    if active and active["result"] == "UNRESOLVED_OPEN":
        resolve_trade(active, frame5, end)

    summary = {
        "total_smc_events": len(events),
        "bos_count": sum(e["event_type"] == "BOS" for e in events),
        "choch_count": sum(e["event_type"] == "CHOCH" for e in events),
        **counts,
        "total_simulated_trades": len(trades),
        "full_tp2_wins": sum(t["result"] == "FULL_TP2_WIN" for t in trades),
        "protected_wins": sum(t["result"] == "PROTECTED_WIN" for t in trades),
        "losses": sum(t["result"] == "LOSS" for t in trades),
        "ambiguous": sum(t["result"] == "AMBIGUOUS_INTRABAR" for t in trades),
        "unresolved_open": sum(t["result"] == "UNRESOLVED_OPEN" for t in trades),
        **summarize_r(trades),
    }
    return {
        "strategy_version": strategy, "symbol": symbol,
        "start": start.isoformat(), "end": end.isoformat(),
        "candle_counts": {"15m": int(((frame15.index+pd.Timedelta(minutes=15) >= start)&(frame15.index+pd.Timedelta(minutes=15) <= end)).sum()),
                          "5m": int(((frame5.index+pd.Timedelta(minutes=5) >= start)&(frame5.index+pd.Timedelta(minutes=5) <= end)).sum())},
        "summary": summary, "trades": trades,
        "diagnostics": {
            "analysis_only": True, "isolated_in_memory_state": True,
            "data_source": "indicator_candles_read_only", "spread_slippage_included": False,
            "future_candle_access": False,
            "unsupported_or_approximated": [
                "two sub-minimum BOS structure exception uses chronological same-direction approximation",
                "historical spread, slippage, and tick ordering are unavailable",
                "TP2 uses the production 2R fallback rather than a mutable runtime broker context",
            ],
        },
    }
