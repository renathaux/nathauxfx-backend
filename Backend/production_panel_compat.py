"""Production panel compatibility for partially open/stale markets.

The dashboard must be able to refresh one healthy symbol even when another
symbol is temporarily unavailable (for example EURUSD live while XAUUSD has
not started its Sunday session yet). This module only relaxes panel *display
cache* validation. It does not change execution freshness, risk, or broker
submission gates.
"""
from __future__ import annotations

import math

import api

_ORIGINAL_PANEL_CACHE_VALIDITY = api._panel_cache_validity


def _positive_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _symbol_is_explicitly_unavailable(plan):
    plan = plan if isinstance(plan, dict) else {}
    market_condition = str(plan.get("market_condition") or "").upper()
    blocked_by = str(plan.get("blocked_by") or "").lower()
    source_state = plan.get("signal_data_source") or {}
    source_unavailable = (
        isinstance(source_state, dict)
        and source_state.get("available") is False
    )
    return (
        market_condition in {"UNKNOWN", "CTRADER_UNAVAILABLE"}
        or blocked_by == "ctrader_data"
        or source_unavailable
    )


def _panel_cache_validity_per_symbol(data):
    """Allow a WAIT/no-data symbol without rejecting a healthy sibling symbol.

    A symbol marked unavailable remains non-actionable. We still reject missing
    candle history, malformed payloads, or any unavailable symbol that somehow
    carries an actionable BUY/SELL decision.
    """
    if not api._is_valid_panel_payload(data):
        return _ORIGINAL_PANEL_CACHE_VALIDITY(data)

    candle_counts = api._panel_candle_counts(data)
    problems = []

    for symbol in ("EURUSD", "XAUUSD"):
        plan = data.get(symbol) or {}
        signal = str(plan.get("signal") or "WAIT").upper()
        has_candles = any(candle_counts[symbol].values())
        unavailable = _symbol_is_explicitly_unavailable(plan)

        if not has_candles:
            problems.append(f"{symbol} candles missing")
            continue

        if unavailable:
            if signal in {"BUY", "SELL"}:
                problems.append(
                    f"{symbol} unavailable market data cannot be actionable"
                )
            continue

        try:
            price = float(plan.get("price"))
            has_price = math.isfinite(price) and price > 0
        except (TypeError, ValueError):
            has_price = False

        market_condition = str(
            plan.get("market_condition") or "UNKNOWN"
        ).upper()
        has_market_condition = market_condition not in {
            "",
            "UNKNOWN",
            "CTRADER_UNAVAILABLE",
        }
        scores = [
            plan.get("buy_pct", plan.get("buy_percentage", 0)),
            plan.get("sell_pct", plan.get("sell_percentage", 0)),
            plan.get("confidence", 0),
        ]
        wait_reason = plan.get("blocked_reason") or plan.get("blocked_by")
        try:
            has_scores = (
                signal == "WAIT" and bool(wait_reason)
            ) or any(float(value or 0) > 0 for value in scores)
        except (TypeError, ValueError):
            has_scores = signal == "WAIT" and bool(wait_reason)

        if not has_price:
            problems.append(f"{symbol} price missing")
        if not has_market_condition:
            problems.append(f"{symbol} market condition unavailable")
        if not has_scores:
            problems.append(f"{symbol} scores are all zero")

    return {
        "valid": not problems,
        "reason": "; ".join(problems) if problems else None,
        "candle_counts": candle_counts,
    }


api._panel_cache_validity = _panel_cache_validity_per_symbol
