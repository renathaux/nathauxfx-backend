"""Read-only virtual trade simulator for Strategy Studio.

No broker execution, order mutation, LIVE Auto state, or Strategy Studio
activation state is imported or modified here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import pandas as pd

from services.strategy_engine.evaluator import evaluate_strategy
from services.strategy_engine.market_facts import build_market_facts
from services.strategy_engine.types import EvaluationState
from services.strategy_studio_schema import normalize_definition


DIAGNOSTIC_STAGE_ORDER = [
    "trend", "structure", "break_validation", "confirmation", "entry",
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

        if original_sl_hit and tp1_hit:
            return _closed_result(trade, candle, "AMBIGUOUS_INTRABAR", 0.0, resolved=False)
        if original_sl_hit:
            return _closed_result(trade, candle, "SL", -1.0, exit_price=trade.sl)

        if tp1_hit:
            fraction = min(max(float(trade.tp1_close_fraction), 0.0), 1.0)
            trade.tp1_hit = True
            trade.realized_r += fraction * trade.r_at(float(trade.tp1))
            trade.remaining_fraction = 1.0 - fraction
            trade.protected_sl = (
                float(trade.entry) + trade.sign * trade.risk_distance * float(trade.protection_r)
            )

            # Reaching TP2 from entry necessarily crosses TP1 on the profit side.
            # With no original SL touch, no unknowable reversal is required.
            if tp2_hit:
                total_r = trade.realized_r + trade.remaining_fraction * trade.r_at(trade.tp2)
                return _closed_result(trade, candle, "TP2", total_r, exit_price=trade.tp2)
            if trade.remaining_fraction <= 0:
                return _closed_result(trade, candle, "TP1_FULL", trade.realized_r, exit_price=trade.tp1)
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
                   include_replay=False) -> dict:
    value = normalize_definition(definition)
    timeline = build_market_facts(
        market_bundle,
        symbol,
        value["trading_timeframe"],
        value["trend"]["timeframe"],
    )
    balance = float(start_balance)
    if balance <= 0:
        raise ValueError("SIMULATION_BALANCE_INVALID")

    state = EvaluationState()
    active: VirtualTrade | None = None
    trades: list[dict] = []
    replay: list[dict] = []
    ordinal = 0

    # Fast Backtest must explain *why* a strategy produced few or zero trades.
    # Keep diagnostics independent from replay frames so FAST and REPLAY return
    # the same funnel/rejection summary without storing every candle decision.
    diagnostic_setups: dict[str, dict] = {}
    no_setup_reasons: dict[str, int] = {}
    candles_analyzed = 0
    evaluations = 0
    signals_emitted = 0

    for timestamp in timeline.timestamps():
        candle = timeline.candle(timestamp)
        if candle is None:
            continue
        candles_analyzed += 1

        if active is not None:
            closed = resolve_virtual_trade(active, candle)
            if include_replay:
                replay.append(_replay_frame(timestamp, candle, None, active, closed))
            if closed is not None:
                trades.append(closed)
                if closed.get("resolved") is True:
                    balance += float(closed.get("pnl_dollars") or 0.0)
                active = None
            # Never invent an intrabar exit-then-reentry order on the same candle.
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

        if evaluation.signal in {"BUY", "SELL"}:
            signals_emitted += 1
            if evaluation.setup_id:
                diagnostic_setups[str(evaluation.setup_id)]["signaled"] = True
            ordinal += 1
            tp1 = value["tp1"]
            active = VirtualTrade(
                trade_id=_trade_id(evaluation.setup_id, timestamp, ordinal),
                entry_time=pd.Timestamp(timestamp),
                entry=float(evaluation.entry),
                sl=float(evaluation.sl),
                tp1=float(evaluation.tp1) if evaluation.tp1 is not None else None,
                tp2=float(evaluation.tp2),
                side=evaluation.signal,
                risk_dollars=float(evaluation.risk_budget["dollars"]),
                tp1_close_fraction=float(tp1["close_percent"] or 0.0) / 100.0 if tp1["enabled"] else 0.0,
                protection_r=float(tp1["protection_r"] or 0.0) if tp1["enabled"] else 0.0,
            )
        if include_replay:
            replay.append(_replay_frame(timestamp, candle, evaluation, active, None))

    if active is not None:
        trades.append({
            "trade_id": active.trade_id,
            "side": active.side,
            "entry_time": pd.Timestamp(active.entry_time).isoformat(),
            "exit_time": None,
            "entry": active.entry,
            "sl": active.sl,
            "tp1": active.tp1,
            "tp2": active.tp2,
            "exit_price": None,
            "risk_dollars": active.risk_dollars,
            "r": None,
            "pnl_dollars": 0.0,
            "outcome": "OPEN_AT_END",
            "resolved": False,
            "tp1_hit": active.tp1_hit,
        })

    metrics = simulation_metrics(float(start_balance), trades)

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

    diagnostics = {
        "candles_analyzed": candles_analyzed,
        "evaluations": evaluations,
        "setups_detected": len(diagnostic_setups),
        "signals_emitted": signals_emitted,
        "trades_opened": signals_emitted,
        "resolved_trades": metrics["total_resolved_trades"],
        "open_trades_at_end": sum(1 for item in trades if item.get("outcome") == "OPEN_AT_END"),
        "blocked_setups": blocked_setups,
        "waiting_setups": waiting_setups,
        "stage_pass_counts": stage_pass_counts,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "no_setup_reasons": dict(sorted(no_setup_reasons.items())),
    }

    output = {
        "metrics": {key: value for key, value in metrics.items() if key != "equity_curve"},
        "trades": trades,
        "equity_curve": metrics["equity_curve"],
        "diagnostics": diagnostics,
    }
    if include_replay:
        output["replay"] = replay
    return output
