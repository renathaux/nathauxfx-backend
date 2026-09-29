"""Read-only virtual trade simulator for Strategy Studio.

No broker execution, order mutation, LIVE Auto state, or Strategy Studio
activation state is imported or modified here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

import pandas as pd

from services.strategy_engine.evaluator import evaluate_strategy_normalized as evaluate_strategy
from services.strategy_engine.market_facts import build_market_facts
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_schema import normalize_definition


DIAGNOSTIC_STAGE_ORDER = [
    "trend", "structure", "break_validation", "confirmation", "session", "seasonal", "entry",
    "stop_loss", "tp1", "tp2", "risk",
]


@dataclass
class VirtualTrade:
    trade_id: str
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp1: float | None
    tp2: float
    side: str
    risk_dollars: float
    tp1_close_fraction: float = 0.0
    protection_r: float = 0.0
    protection_basis: str = "SL_DISTANCE"
    protection_mode: str = "FIXED"
    protection_trigger_method: str = "CANDLE_CLOSE"
    protection_steps: list[dict] = field(default_factory=list)
    tp1_hit: bool = False
    remaining_fraction: float = 1.0
    protected_sl: float | None = None
    realized_r: float = 0.0

    @property
    def risk_distance(self) -> float:
        return abs(float(self.entry) - float(self.sl))

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "BUY" else -1.0

    def r_at(self, price: float) -> float:
        if self.risk_distance <= 0:
            return 0.0
        return self.sign * (float(price) - float(self.entry)) / self.risk_distance


def _value(candle, name: str) -> float:
    if isinstance(candle, dict):
        return float(candle[name])
    return float(getattr(candle, name))


def _timestamp(candle) -> pd.Timestamp:
    if isinstance(candle, dict):
        return pd.Timestamp(candle["timestamp"])
    return pd.Timestamp(getattr(candle, "timestamp"))


def _touch_profit(trade: VirtualTrade, high: float, low: float, level: float | None) -> bool:
    if level is None:
        return False
    return high >= level if trade.side == "BUY" else low <= level


def _touch_stop(trade: VirtualTrade, high: float, low: float, level: float) -> bool:
    return low <= level if trade.side == "BUY" else high >= level


def _fixed_protection_level(trade: VirtualTrade) -> float:
    fraction = float(trade.protection_r)
    if trade.protection_basis == "TP2_DISTANCE":
        return float(trade.entry) + (float(trade.tp2) - float(trade.entry)) * fraction
    return float(trade.entry) + trade.sign * trade.risk_distance * fraction


def _step_protection_level(trade: VirtualTrade, progress_price: float) -> float | None:
    if trade.protection_mode != "TP2_STEPS" or not trade.protection_steps:
        return None
    path = float(trade.tp2) - float(trade.entry)
    if path == 0:
        return None
    progress = (float(progress_price) - float(trade.entry)) / path * 100.0
    if progress < 0:
        return None
    reached = [
        item for item in trade.protection_steps
        if progress >= float(item.get("trigger_percent", 0.0))
    ]
    if not reached:
        return None
    step = max(reached, key=lambda item: float(item.get("trigger_percent", 0.0)))
    secure_fraction = float(step.get("secure_percent", 0.0)) / 100.0
    return float(trade.entry) + path * secure_fraction


def _better_stop(trade: VirtualTrade, candidate: float | None) -> bool:
    if candidate is None:
        return False
    current = trade.protected_sl
    if current is None:
        current = trade.sl
    return candidate > current if trade.side == "BUY" else candidate < current


def _update_step_protection_on_close(trade: VirtualTrade, close: float) -> None:
    candidate = _step_protection_level(trade, close)
    if _better_stop(trade, candidate):
        trade.protected_sl = float(candidate)


def _touch_step_candidate(trade: VirtualTrade, high: float, low: float) -> float | None:
    favorable_extreme = high if trade.side == "BUY" else low
    candidate = _step_protection_level(trade, favorable_extreme)
    return float(candidate) if _better_stop(trade, candidate) else None


def _touch_step_is_intrabar_ambiguous(
    trade: VirtualTrade, high: float, low: float, candidate: float | None
) -> bool:
    return candidate is not None and _touch_stop(trade, high, low, float(candidate))


def _virtual_trade_payload(trade: VirtualTrade | None) -> dict | None:
    if trade is None:
        return None
    return {
        "trade_id": trade.trade_id,
        "entry_time": pd.Timestamp(trade.entry_time).isoformat(),
        "entry": float(trade.entry),
        "sl": float(trade.sl),
        "tp1": float(trade.tp1) if trade.tp1 is not None else None,
        "tp2": float(trade.tp2),
        "side": trade.side,
        "risk_dollars": float(trade.risk_dollars),
        "tp1_close_fraction": float(trade.tp1_close_fraction),
        "protection_r": float(trade.protection_r),
        "protection_basis": trade.protection_basis,
        "protection_mode": trade.protection_mode,
        "protection_trigger_method": trade.protection_trigger_method,
        "protection_steps": list(trade.protection_steps or []),
        "tp1_hit": bool(trade.tp1_hit),
        "remaining_fraction": float(trade.remaining_fraction),
        "protected_sl": (
            float(trade.protected_sl) if trade.protected_sl is not None else None
        ),
        "realized_r": float(trade.realized_r),
    }


def _virtual_trade_from_payload(payload: dict | None) -> VirtualTrade | None:
    if not isinstance(payload, dict):
        return None
    return VirtualTrade(
        trade_id=str(payload["trade_id"]),
        entry_time=pd.Timestamp(payload["entry_time"]),
        entry=float(payload["entry"]),
        sl=float(payload["sl"]),
        tp1=float(payload["tp1"]) if payload.get("tp1") is not None else None,
        tp2=float(payload["tp2"]),
        side=str(payload["side"]),
        risk_dollars=float(payload["risk_dollars"]),
        tp1_close_fraction=float(payload.get("tp1_close_fraction") or 0.0),
        protection_r=float(payload.get("protection_r") or 0.0),
        protection_basis=str(payload.get("protection_basis") or "SL_DISTANCE"),
        protection_mode=str(payload.get("protection_mode") or "FIXED"),
        protection_trigger_method=str(
            payload.get("protection_trigger_method") or "CANDLE_CLOSE"
        ),
        protection_steps=list(payload.get("protection_steps") or []),
        tp1_hit=bool(payload.get("tp1_hit")),
        remaining_fraction=float(payload.get("remaining_fraction", 1.0)),
        protected_sl=(
            float(payload["protected_sl"])
            if payload.get("protected_sl") is not None else None
        ),
        realized_r=float(payload.get("realized_r") or 0.0),
    )


def _closed_result(trade: VirtualTrade, candle, outcome: str, total_r: float,
                   *, resolved: bool = True, exit_price: float | None = None) -> dict:
    pnl = float(total_r) * float(trade.risk_dollars) if resolved else 0.0
    return {
        "trade_id": trade.trade_id,
        "side": trade.side,
        "entry_time": pd.Timestamp(trade.entry_time).isoformat(),
        "exit_time": _timestamp(candle).isoformat(),
        "entry": float(trade.entry),
        "sl": float(trade.sl),
        "tp1": float(trade.tp1) if trade.tp1 is not None else None,
        "tp2": float(trade.tp2),
        "exit_price": float(exit_price) if exit_price is not None else None,
        "risk_dollars": float(trade.risk_dollars),
        "r": float(total_r) if resolved else None,
        "pnl_dollars": pnl,
        "outcome": outcome,
        "resolved": bool(resolved),
        "tp1_hit": bool(trade.tp1_hit),
    }


def resolve_virtual_trade(trade: VirtualTrade, candle) -> dict | None:
    """Advance one virtual trade through one closed OHLC candle.

    OHLC cannot reveal intrabar path.  Any outcome requiring an assumed reversal
    order is explicitly excluded as AMBIGUOUS_INTRABAR.
    """
    high = _value(candle, "high")
    low = _value(candle, "low")
    original_sl_hit = _touch_stop(trade, high, low, float(trade.sl))
    tp2_hit = _touch_profit(trade, high, low, float(trade.tp2))

    if not trade.tp1_hit:
        tp1_hit = _touch_profit(trade, high, low, trade.tp1)

        if trade.tp1 is None:
            if original_sl_hit and tp2_hit:
                return _closed_result(trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False)
            if original_sl_hit:
                return _closed_result(trade, candle, "SL", -1.0, exit_price=trade.sl)
            if tp2_hit:
                final_r = trade.r_at(trade.tp2)
                return _closed_result(trade, candle, "TP2", final_r, exit_price=trade.tp2)
            return None

        tp1_r = trade.r_at(float(trade.tp1))
        tp2_r = trade.r_at(float(trade.tp2))
        tp2_is_nearer = tp2_r <= tp1_r

        # TP2 is broker-side in the real flow. If a dynamic opposite-swing TP2
        # is closer than TP1, reaching TP2 closes the whole trade before the
        # app-managed partial TP1 can occur. The old simulator ignored this
        # case and could later count a false full SL.
        if tp2_is_nearer:
            if original_sl_hit and tp2_hit:
                return _closed_result(trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False)
            if tp2_hit:
                return _closed_result(
                    trade, candle, "TP2", tp2_r, exit_price=trade.tp2
                )
            if original_sl_hit:
                return _closed_result(trade, candle, "SL", -1.0, exit_price=trade.sl)
            return None

        if original_sl_hit and tp1_hit:
            return _closed_result(trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False)
        if original_sl_hit:
            return _closed_result(trade, candle, "SL", -1.0, exit_price=trade.sl)

        if tp1_hit:
            fraction = min(max(float(trade.tp1_close_fraction), 0.0), 1.0)
            trade.tp1_hit = True
            trade.realized_r += fraction * trade.r_at(float(trade.tp1))
            trade.remaining_fraction = 1.0 - fraction

            if trade.protection_mode == "FIXED":
                trade.protected_sl = _fixed_protection_level(trade)

            # Reaching TP2 from entry necessarily crosses TP1 on the profit side.
            # With no original SL touch, no unknowable reversal is required.
            if tp2_hit:
                total_r = trade.realized_r + trade.remaining_fraction * trade.r_at(trade.tp2)
                return _closed_result(trade, candle, "TP2", total_r, exit_price=trade.tp2)
            if trade.remaining_fraction <= 0:
                return _closed_result(trade, candle, "TP1_FULL", trade.realized_r, exit_price=trade.tp1)

            if trade.protection_mode == "TP2_STEPS":
                if trade.protection_trigger_method == "PRICE_TOUCH":
                    candidate = _touch_step_candidate(trade, high, low)
                    if _touch_step_is_intrabar_ambiguous(trade, high, low, candidate):
                        return _closed_result(
                            trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False
                        )
                    if candidate is not None:
                        trade.protected_sl = float(candidate)
                else:
                    # Close-triggered protection becomes active after this
                    # closed candle, preserving the existing deterministic model.
                    _update_step_protection_on_close(trade, _value(candle, "close"))
            return None
        return None

    protected = float(trade.protected_sl if trade.protected_sl is not None else trade.sl)
    protected_hit = _touch_stop(trade, high, low, protected)
    if protected_hit and tp2_hit:
        return _closed_result(trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False)
    if protected_hit:
        total_r = trade.realized_r + trade.remaining_fraction * trade.r_at(protected)
        return _closed_result(trade, candle, "PROTECTED_SL", total_r, exit_price=protected)
    if tp2_hit:
        total_r = trade.realized_r + trade.remaining_fraction * trade.r_at(trade.tp2)
        return _closed_result(trade, candle, "TP2", total_r, exit_price=trade.tp2)
    if trade.protection_mode == "TP2_STEPS":
        if trade.protection_trigger_method == "PRICE_TOUCH":
            candidate = _touch_step_candidate(trade, high, low)
            if _touch_step_is_intrabar_ambiguous(trade, high, low, candidate):
                return _closed_result(
                    trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False
                )
            if candidate is not None:
                trade.protected_sl = float(candidate)
        else:
            _update_step_protection_on_close(trade, _value(candle, "close"))
    return None


def simulation_metrics(starting_balance: float, trades: list[dict]) -> dict:
    start = float(starting_balance)
    balance = start
    peak = start
    max_drawdown = 0.0
    equity_curve = [{"trade": 0, "balance": start}]

    resolved = [item for item in trades if item.get("resolved") is True]
    for index, item in enumerate(trades, start=1):
        if item.get("resolved") is True:
            balance += float(item.get("pnl_dollars") or 0.0)
        peak = max(peak, balance)
        max_drawdown = max(max_drawdown, peak - balance)
        equity_curve.append({"trade": index, "balance": balance})

    wins = [item for item in resolved if float(item.get("pnl_dollars") or 0.0) > 0]
    losses = [item for item in resolved if float(item.get("pnl_dollars") or 0.0) < 0]
    gross_profit = sum(float(item.get("pnl_dollars") or 0.0) for item in wins)
    gross_loss = sum(float(item.get("pnl_dollars") or 0.0) for item in losses)
    resolved_count = len(resolved)
    r_values = [float(item["r"]) for item in resolved if item.get("r") is not None]

    return {
        "starting_balance": start,
        "ending_balance": balance,
        "net_pl": balance - start,
        "win_rate": (len(wins) / resolved_count * 100.0) if resolved_count else 0.0,
        "total_resolved_trades": resolved_count,
        "wins": len(wins),
        "losses": len(losses),
        "ambiguous_trades": sum(1 for item in trades if item.get("outcome") == "AMBIGUOUS_INTRABAR"),
        "average_r": (sum(r_values) / len(r_values)) if r_values else 0.0,
        "max_drawdown_dollars": max_drawdown,
        "max_drawdown_percent": (max_drawdown / peak * 100.0) if peak > 0 else 0.0,
        "profit_factor": (gross_profit / abs(gross_loss)) if gross_loss < 0 else None,
        "equity_curve": equity_curve,
    }


def _trade_id(setup_id: str | None, timestamp, ordinal: int) -> str:
    raw = f"{setup_id or 'setup'}|{pd.Timestamp(timestamp).isoformat()}|{ordinal}"
    return "sim_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _replay_frame(timestamp, candle, evaluation, open_trade: VirtualTrade | None, result: dict | None) -> dict:
    payload = {
        "timestamp": pd.Timestamp(timestamp).isoformat(),
        "candle": {
            "open": float(candle.open), "high": float(candle.high),
            "low": float(candle.low), "close": float(candle.close),
        },
        "signal": evaluation.signal if evaluation is not None else "WAIT",
        "steps": evaluation.steps if evaluation is not None else {},
        "trade_id": open_trade.trade_id if open_trade is not None else (result or {}).get("trade_id"),
        "outcome": (result or {}).get("outcome"),
    }
    return payload


def _evaluation_reason(evaluation) -> tuple[str | None, str | None]:
    """Return the first blocking/waiting evaluator reason for diagnostics."""
    for stage in DIAGNOSTIC_STAGE_ORDER:
        detail = (evaluation.steps or {}).get(stage) or {}
        state = detail.get("state")
        reason = detail.get("reason")
        if state in {"BLOCKED", "WAITING"} and reason:
            return str(state), str(reason)
    return None, None


def run_simulation(definition, market_bundle, symbol, start_balance, *, risk_override=None,
                   include_replay=False, evaluation_start=None, evaluation_end=None,
                   continuation=None, finalize_open_trade=True, timeline=None, progress=None, is_cancelled=None,
                   max_concurrent_positions=None, max_combined_open_risk_percent=None) -> dict:
    value = normalize_definition(definition)
    timeline = timeline or build_market_facts(
        market_bundle,
        symbol,
        value["trading_timeframe"],
        value["trend"]["timeframe"],
        value["structure_timeframe"],
    )
    base_balance = float(start_balance)
    if base_balance <= 0:
        raise ValueError("SIMULATION_BALANCE_INVALID")

    # Position stacking is a saved Strategy Studio rule. Request-level values
    # are retained only as a backwards-compatible fallback for clients that
    # have not reloaded the strategy yet.
    risk_definition = value.get("risk") or {}
    configured_positions = risk_definition.get("max_concurrent_positions")
    if configured_positions is None:
        configured_positions = max_concurrent_positions if max_concurrent_positions is not None else 1
    try:
        max_positions = int(configured_positions)
    except (TypeError, ValueError):
        raise ValueError("SIMULATION_MAX_CONCURRENT_POSITIONS_INVALID")
    if max_positions < 1 or max_positions > 3:
        raise ValueError("SIMULATION_MAX_CONCURRENT_POSITIONS_INVALID")

    configured_cap = risk_definition.get("max_combined_open_risk_percent")
    if configured_cap is None and max_concurrent_positions is not None:
        configured_cap = max_combined_open_risk_percent
    risk_cap_percent = None
    if configured_cap is not None:
        try:
            risk_cap_percent = float(configured_cap)
        except (TypeError, ValueError):
            raise ValueError("SIMULATION_MAX_COMBINED_OPEN_RISK_INVALID")
        if not 0 < risk_cap_percent <= 10:
            raise ValueError("SIMULATION_MAX_COMBINED_OPEN_RISK_INVALID")
    if max_positions > 1 and risk_cap_percent is None:
        raise ValueError("SIMULATION_MAX_COMBINED_OPEN_RISK_REQUIRED")

    continuation_value = continuation if isinstance(continuation, dict) else {}
    balance = float(continuation_value.get("balance", base_balance))
    if balance <= 0:
        raise ValueError("SIMULATION_BALANCE_INVALID")
    chunk_start_balance = balance

    state = EvaluationState(
        str(continuation_value.get("evaluator_status") or "WAITING"),
        continuation_value.get("pending_setup"),
    )

    active_payloads = continuation_value.get("active_trades")
    if isinstance(active_payloads, list):
        active = [
            trade for trade in
            (_virtual_trade_from_payload(item) for item in active_payloads)
            if trade is not None
        ]
    else:
        legacy_active = _virtual_trade_from_payload(
            continuation_value.get("active_trade")
        )
        active = [legacy_active] if legacy_active is not None else []

    if len(active) > max_positions:
        raise ValueError("SIMULATION_CONTINUATION_EXCEEDS_MAX_CONCURRENT_POSITIONS")

    trades: list[dict] = []
    replay: list[dict] = []
    ordinal = int(continuation_value.get("ordinal") or 0)

    diagnostic_setups: dict[str, dict] = {}
    no_setup_reasons: dict[str, int] = {}
    candles_analyzed = 0
    evaluations = 0
    signals_emitted = 0
    trades_opened = 0
    capacity_blocked_candles = 0
    combined_risk_blocked_signals = 0
    max_simultaneous_positions = len(active)
    max_open_risk_dollars = sum(float(item.risk_dollars) for item in active)
    overlapping_entries_opened = 0
    window_start = pd.Timestamp(evaluation_start) if evaluation_start is not None else None
    window_end = pd.Timestamp(evaluation_end) if evaluation_end is not None else None

    timestamps = timeline.timestamps()
    for candle_index, timestamp in enumerate(timestamps):
        if candle_index % 2048 == 0:
            if is_cancelled and is_cancelled():
                raise InterruptedError("Backtest cancelled")
            if progress:
                progress(candle_index, len(timestamps))
        stamp = pd.Timestamp(timestamp)
        if window_start is not None and stamp < window_start:
            continue
        if window_end is not None and stamp >= window_end:
            continue
        candle = timeline.candle(timestamp)
        if candle is None:
            continue
        candles_analyzed += 1

        # Every open position is managed independently. If any position closes on
        # this candle, do not also invent a same-candle re-entry ordering.
        closed_on_candle = False
        if active:
            survivors = []
            for trade in active:
                closed = resolve_virtual_trade(trade, candle)
                if closed is not None:
                    closed_on_candle = True
                    trades.append(closed)
                    if closed.get("resolved") is True:
                        balance += float(closed.get("pnl_dollars") or 0.0)
                else:
                    survivors.append(trade)
            active = survivors

            if include_replay:
                latest = active[-1] if active else None
                replay.append(
                    _replay_frame(timestamp, candle, None, latest, None)
                )
            if closed_on_candle:
                continue

        # Preserve the legacy one-position behavior exactly: when capacity is
        # full, the evaluator is not advanced and setups during that interval
        # are intentionally ignored.
        if len(active) >= max_positions:
            capacity_blocked_candles += 1
            continue

        evaluation = evaluate_strategy(
            value,
            timeline,
            timestamp,
            state,
            symbol=symbol,
            account_balance=balance,
            risk_override=risk_override,
        )
        evaluations += 1
        state = evaluation.next_state

        reason_state, reason = _evaluation_reason(evaluation)
        if evaluation.setup_id:
            setup_diag = diagnostic_setups.setdefault(
                str(evaluation.setup_id),
                {
                    "passed_stages": set(),
                    "last_state": None,
                    "last_reason": None,
                    "signaled": False,
                },
            )
            for stage in DIAGNOSTIC_STAGE_ORDER:
                detail = (evaluation.steps or {}).get(stage) or {}
                if detail.get("state") == "PASSED":
                    setup_diag["passed_stages"].add(stage)
            if reason:
                setup_diag["last_state"] = reason_state
                setup_diag["last_reason"] = reason
        elif reason:
            no_setup_reasons[reason] = no_setup_reasons.get(reason, 0) + 1

        opened_trade = None
        if evaluation.signal in {"BUY", "SELL"}:
            signals_emitted += 1
            candidate_risk = float(evaluation.risk_budget["dollars"])
            open_risk = sum(float(item.risk_dollars) for item in active)
            cap_dollars = (
                balance * risk_cap_percent / 100.0
                if risk_cap_percent is not None
                else None
            )
            if (
                cap_dollars is not None
                and open_risk + candidate_risk > cap_dollars + 1e-9
            ):
                combined_risk_blocked_signals += 1
                if evaluation.setup_id:
                    diagnostic_setups[str(evaluation.setup_id)]["last_state"] = "BLOCKED"
                    diagnostic_setups[str(evaluation.setup_id)]["last_reason"] = (
                        "MAX_COMBINED_OPEN_RISK"
                    )
            else:
                if evaluation.setup_id:
                    diagnostic_setups[str(evaluation.setup_id)]["signaled"] = True
                ordinal += 1
                tp1 = value["tp1"]
                opened_trade = VirtualTrade(
                    trade_id=_trade_id(evaluation.setup_id, timestamp, ordinal),
                    entry_time=pd.Timestamp(timestamp),
                    entry=float(evaluation.entry),
                    sl=float(evaluation.sl),
                    tp1=float(evaluation.tp1) if evaluation.tp1 is not None else None,
                    tp2=float(evaluation.tp2),
                    side=evaluation.signal,
                    risk_dollars=candidate_risk,
                    tp1_close_fraction=float(tp1["close_percent"] or 0.0) / 100.0 if tp1["enabled"] else 0.0,
                    protection_r=float(tp1.get("protection_r") or 0.0) if tp1["enabled"] else 0.0,
                    protection_basis=str(tp1.get("target_basis") or "SL_DISTANCE"),
                    protection_mode=str(tp1.get("protection_mode") or "FIXED"),
                    protection_trigger_method=str(
                        tp1.get("protection_trigger_method") or "CANDLE_CLOSE"
                    ),
                    protection_steps=list(tp1.get("protection_steps") or []),
                )
                had_open_position = bool(active)
                active.append(opened_trade)
                trades_opened += 1
                if had_open_position:
                    overlapping_entries_opened += 1
                max_simultaneous_positions = max(
                    max_simultaneous_positions, len(active)
                )
                max_open_risk_dollars = max(
                    max_open_risk_dollars,
                    sum(float(item.risk_dollars) for item in active),
                )

        if include_replay:
            replay.append(
                _replay_frame(
                    timestamp,
                    candle,
                    evaluation,
                    opened_trade or (active[-1] if active else None),
                    None,
                )
            )

    if active and finalize_open_trade:
        for trade in active:
            trades.append({
                "trade_id": trade.trade_id,
                "side": trade.side,
                "entry_time": pd.Timestamp(trade.entry_time).isoformat(),
                "exit_time": None,
                "entry": trade.entry,
                "sl": trade.sl,
                "tp1": trade.tp1,
                "tp2": trade.tp2,
                "exit_price": None,
                "risk_dollars": trade.risk_dollars,
                "r": None,
                "pnl_dollars": 0.0,
                "outcome": "OPEN_AT_END",
                "resolved": False,
                "tp1_hit": trade.tp1_hit,
            })

    metrics = simulation_metrics(chunk_start_balance, trades)

    stage_pass_counts = {
        stage: sum(
            1 for setup in diagnostic_setups.values()
            if stage in setup["passed_stages"]
        )
        for stage in DIAGNOSTIC_STAGE_ORDER
    }
    rejection_reasons: dict[str, int] = {}
    blocked_setups = 0
    waiting_setups = 0
    for setup in diagnostic_setups.values():
        if setup["signaled"]:
            continue
        final_state = setup.get("last_state")
        if final_state == "BLOCKED":
            blocked_setups += 1
        elif final_state == "WAITING":
            waiting_setups += 1
        reason = setup.get("last_reason") or "NO_VALID_ENTRY"
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    all_timestamps = timestamps
    history_start = (
        pd.Timestamp(all_timestamps[0]).isoformat()
        if all_timestamps else None
    )
    warmup_candles = 0
    if window_start is not None:
        warmup_candles = sum(
            1 for item in all_timestamps
            if pd.Timestamp(item) < window_start
        )

    diagnostics = {
        "candles_analyzed": candles_analyzed,
        "warmup_candles": warmup_candles,
        "history_start": history_start,
        "evaluations": evaluations,
        "setups_detected": len(diagnostic_setups),
        "signals_emitted": signals_emitted,
        "trades_opened": trades_opened,
        "resolved_trades": metrics["total_resolved_trades"],
        "open_trades_at_end": len(active),
        "blocked_setups": blocked_setups,
        "waiting_setups": waiting_setups,
        "capacity_blocked_candles": capacity_blocked_candles,
        "combined_risk_blocked_signals": combined_risk_blocked_signals,
        "max_simultaneous_positions": max_simultaneous_positions,
        "max_open_risk_dollars": max_open_risk_dollars,
        "overlapping_entries_opened": overlapping_entries_opened,
        "stage_pass_counts": stage_pass_counts,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "no_setup_reasons": dict(sorted(no_setup_reasons.items())),
        "setup_details": [
            {
                "setup_id": setup_id,
                "passed_stages": sorted(item["passed_stages"]),
                "last_state": item.get("last_state"),
                "last_reason": item.get("last_reason"),
                "signaled": bool(item.get("signaled")),
            }
            for setup_id, item in diagnostic_setups.items()
        ],
    }

    output = {
        "metrics": {key: value for key, value in metrics.items() if key != "equity_curve"},
        "trades": trades,
        "equity_curve": metrics["equity_curve"],
        "diagnostics": diagnostics,
        "execution_options": {
            "max_concurrent_positions": max_positions,
            "max_combined_open_risk_percent": risk_cap_percent,
        },
        "continuation": {
            "balance": float(balance),
            "evaluator_status": str(state.status),
            "pending_setup": state.pending_setup,
            "active_trades": [_virtual_trade_payload(item) for item in active],
            # Backward-compatible single-position field for older clients.
            "active_trade": (
                _virtual_trade_payload(active[0]) if len(active) == 1 else None
            ),
            "ordinal": int(ordinal),
        },
    }
    if include_replay:
        output["replay"] = replay
    return output
