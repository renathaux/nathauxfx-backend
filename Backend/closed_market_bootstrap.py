"""Closed-market dashboard compatibility layer.

Keep historical chart/panel data usable when cTrader has no live tick (for
example over the weekend). This only supplies a display price from the latest
stored candle before the existing panel-cache validator runs; trading and
execution freshness gates remain unchanged.
"""
from __future__ import annotations

import math

import api
import app_bootstrap  # noqa: F401 - installs the production bootstrap hooks

_ORIGINAL_PANEL_CACHE_VALIDITY = api._panel_cache_validity


def _positive_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _latest_stored_close(panel_data, symbol):
    candles = panel_data.get("candles") if isinstance(panel_data, dict) else None
    if not isinstance(candles, dict):
        return None
    symbol_candles = candles.get(symbol)
    if not isinstance(symbol_candles, dict):
        return None

    # Prefer the shortest timeframe so the fallback is the most recent known
    # market price, but accept any stored history if one timeframe is missing.
    for timeframe in ("5m", "15m", "1h"):
        rows = symbol_candles.get(timeframe)
        if not isinstance(rows, list):
            continue
        for row in reversed(rows):
            if isinstance(row, dict):
                close = _positive_finite(row.get("close"))
            elif isinstance(row, (list, tuple)) and len(row) >= 5:
                close = _positive_finite(row[4])
            else:
                close = None
            if close is not None:
                return close
    return None


def _panel_cache_validity_with_closed_market_fallback(panel_data):
    if isinstance(panel_data, dict):
        for symbol in ("EURUSD", "XAUUSD"):
            plan = panel_data.get(symbol)
            if not isinstance(plan, dict):
                continue
            if _positive_finite(plan.get("price")) is not None:
                continue
            fallback_price = _latest_stored_close(panel_data, symbol)
            if fallback_price is None:
                continue
            plan["price"] = fallback_price
            plan["price_source"] = "LAST_STORED_CANDLE"
            plan["live_price_available"] = False

    return _ORIGINAL_PANEL_CACHE_VALIDITY(panel_data)


api._panel_cache_validity = _panel_cache_validity_with_closed_market_fallback

# Install the second compatibility layer after the closed-market wrapper so
# partially-open sessions are evaluated per symbol instead of globally. This
# lets EURUSD refresh normally while XAUUSD is still waiting for its own feed.
import production_panel_compat  # noqa: E402,F401

app = api.app
