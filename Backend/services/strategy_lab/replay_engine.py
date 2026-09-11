"""Chronological, in-memory Strategy Lab replay coordinator."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from services.strategy_settings_service import defaults, get_strategy_settings

# Keep these baseline aliases for backwards-compatible tests and callers that
# monkeypatch the Phase 1 replay seams.
from .baseline_v1 import candidates, evaluate_event, resolve_trade
from . import (
    v2_m5_quality,
    v2a_m5_quality,
    v2b_m5_quality,
    v2c_m15_quality,
    v3_m5_two_close,
)
from .data_source import load_candles
from .metrics import summarize_r

MAX_SPAN_DAYS = 120
PURE_5M_STRATEGIES = {"v3_m5_two_close"}
AVAILABLE_STRATEGIES = {
    "baseline_v1",
    "v2_m5_quality",
    "v2a_m5_quality_50_30",
    "v2b_m5_quality_45_35",
    "v2c_m15_quality_60_30",
    "v3_m5_two_close",
}


def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _strategy_engine(strategy):
    if strategy == "baseline_v1":
        return candidates, evaluate_event, resolve_trade
    engines = {
        "v2_m5_quality": v2_m5_quality,
        "v2a_m5_quality_50_30": v2a_m5_quality,
        "v2b_m5_quality_45_35": v2b_m5_quality,
        "v2c_m15_quality_60_30": v2c_m15_quality,
        "v3_m5_two_close": v3_m5_two_close,
    }
    module = engines.get(strategy)
    if module is None:
        raise ValueError(f"unsupported Strategy Lab strategy: {strategy}")
    return module.candidates, module.evaluate_event, module.resolve_trade


def _strategy_parameters(strategy):
    if strategy == "v2_m5_quality":
        return {
            "m5_minimum_body_ratio": v2_m5_quality.MIN_BODY_RATIO,
            "m5_maximum_close_side_wick_ratio": v2_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
        }
    if strategy == "v2a_m5_quality_50_30":
        return {
            "m5_minimum_body_ratio": v2a_m5_quality.MIN_BODY_RATIO,
            "m5_maximum_close_side_wick_ratio": v2a_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
        }
    if strategy == "v2b_m5_quality_45_35":
        return {
            "m5_minimum_body_ratio": v2b_m5_quality.MIN_BODY_RATIO,
            "m5_maximum_close_side_wick_ratio": v2b_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
        }
    if strategy == "v2c_m15_quality_60_30":
        return {
            "m15_minimum_body_ratio": v2c_m15_quality.MIN_BODY_RATIO,
            "m15_maximum_close_side_wick_ratio": v2c_m15_quality.MAX_CLOSE_SIDE_WICK_RATIO,
        }
    if strategy == "v3_m5_two_close":
        return {
            "setup_timeframe": "5m",
            "confirmation_timeframe": "5m",
            "uses_15m": False,
            "event_type": "BOS",
            "confirmation": "immediate next 5m candle closes same direction and stays beyond BOS level",
            "entry_at": "second_5m_close",
        }
    return None


def _parity_rules(strategy):
    if strategy == "v3_m5_two_close":
        return [
            "5m BOS only",
            "immediate next closed 5m candle must close in the BOS direction",
            "second 5m close must remain beyond the broken BOS level",
            "entry at second 5m close",
            "no 15m structure, EMA, consolidation, or confirmation dependency",
            "event-owned 5m structural SL",
            "opposing valid 5m swing TP2 with replay 2R fallback",
            "one active position",
            "previous-position-close freshness",
        ]
    return [
        "EMA 9/21 permission", "ATR/floor BOS buffer", "production consolidation gate",
        "100-point structure qualification", "exact internal two-BOS exception",
        "60-minute remembered-event window", "event-owned structural SL",
        "opposing valid 15m swing TP2 with production 2R fallback",
        "one active position", "previous-position-close freshness",
    ]


def run_replay(symbol, strategy, start, end=None, *, session_factory=None, frames=None, settings=None):
    if symbol != "EURUSD" or strategy not in AVAILABLE_STRATEGIES:
        raise ValueError(
            "Strategy Lab currently supports EURUSD baseline and experiment variants"
        )
    strategy_candidates, strategy_evaluate_event, strategy_resolve_trade = _strategy_engine(strategy)
    start, end = _utc(start), _utc(end or datetime.now(timezone.utc))
    if end <= start or end-start > pd.Timedelta(days=MAX_SPAN_DAYS):
        raise ValueError(f"replay range must be between 1 second and {MAX_SPAN_DAYS} days")
    if settings is None:
        loaded = get_strategy_settings(session_factory) if session_factory else get_strategy_settings()
        settings = {**defaults(), **(loaded.get("current", loaded) if isinstance(loaded, dict) else {})}
        settings_source = "runtime_strategy_settings"
    else:
        settings = {**defaults(), **settings}
        settings_source = "explicit_replay_settings"

    pure_5m = strategy in PURE_5M_STRATEGIES
    if frames is None:
        kwargs = {"session_factory": session_factory} if session_factory else {}
        frame15 = pd.DataFrame()
        if not pure_5m:
            frame15 = load_candles(
                symbol, "15m", start.to_pydatetime(), end.to_pydatetime(), **kwargs
            )
        frame5 = load_candles(
            symbol, "5m", start.to_pydatetime(), end.to_pydatetime(), **kwargs
        )
    else:
        frame15, frame5 = frames

    if frame5.empty or (not pure_5m and frame15.empty):
        if pure_5m:
            raise ValueError("historical EURUSD 5m indicator candles are required")
        raise ValueError("historical EURUSD 15m and 5m indicator candles are required")

    counts = {key: 0 for key in (
        "rejected_by_structure", "rejected_by_m15_buffer", "rejected_by_ema",
        "rejected_by_consolidation", "rejected_by_m5_confirmation_expired",
        "rejected_by_m5_quality", "rejected_by_m15_quality", "rejected_by_second_5m",
        "rejected_by_risk_rr", "skipped_active_trade",
        "skipped_previous_position_close_freshness",
    )}
    events, trades, trace, active, previous_close = [], [], [], None, None
    for event, timestamp, prefix, side, leg, structure_ok, exception in strategy_candidates(
        frame15, frame5, start, end, settings
    ):
        events.append(event)
        qualification = exception.get("reason")
        if strategy != "v3_m5_two_close" and leg is not None and leg >= .001:
            qualification = "external_100_point_leg"
        event_trace = {
            "event_time": timestamp.isoformat(), "event_type": event["event_type"],
            "direction": event["direction"],
            "structural_leg_points": None if leg is None else leg / 0.00001,
            "structure_qualified": bool(structure_ok),
            "structure_qualification": qualification,
            "buffered_m15": None, "ema_allowed": None,
            "consolidation_allowed": None, "m5_confirmation_time": None,
            "risk_result": None, "entry": None, "sl": None, "tp1": None,
            "tp2": None, "rr": None, "skipped_active_position": False,
            "skipped_previous_close_freshness": False, "final_action": None,
        }
        if not structure_ok:
            counts["rejected_by_structure"] += 1
            event_trace["final_action"] = "REJECT_STRUCTURE"
            trace.append(event_trace)
            continue
        event_close_minutes = int(exception.get("event_close_minutes", 15))
        event_close = timestamp + pd.Timedelta(minutes=event_close_minutes)
        active_exit = pd.Timestamp(active["exit_timestamp"]) if active and active.get("exit_timestamp") else None
        if active and (active_exit is None or active_exit > event_close):
            counts["skipped_active_trade"] += 1
            event_trace.update(skipped_active_position=True, final_action="SKIP_ACTIVE_POSITION")
            trace.append(event_trace)
            continue
        if active_exit is not None:
            previous_close = active_exit
            active = None
        trade, rejection, event_trace = strategy_evaluate_event(
            event, timestamp, prefix, frame5, side, leg, settings, end,
            previous_close=previous_close,
        )
        if rejection:
            counts[rejection] += 1
            trace.append(event_trace)
            continue
        active = trade
        strategy_resolve_trade(active, frame5, end)
        trades.append(active)
        trace.append(event_trace)

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
        "candle_counts": {
            "15m": 0 if frame15.empty else int(((frame15.index+pd.Timedelta(minutes=15) >= start)&(frame15.index+pd.Timedelta(minutes=15) <= end)).sum()),
            "5m": int(((frame5.index+pd.Timedelta(minutes=5) >= start)&(frame5.index+pd.Timedelta(minutes=5) <= end)).sum()),
        },
        "summary": summary, "trades": trades, "event_trace": trace,
        "diagnostics": {
            "analysis_only": True, "isolated_in_memory_state": True,
            "data_source": "indicator_candles_read_only", "spread_slippage_included": False,
            "future_candle_access": False,
            "settings_source": settings_source,
            "settings_used": dict(settings),
            "strategy_parameters": _strategy_parameters(strategy),
            "unsupported_or_approximated": [
                "historical spread, slippage, and tick ordering are unavailable",
                "runtime broker position state is represented by isolated simulated trades",
            ],
            "parity_rules": _parity_rules(strategy),
        },
    }
