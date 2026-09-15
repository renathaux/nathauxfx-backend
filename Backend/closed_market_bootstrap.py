"""Closed-market dashboard compatibility layer.

Keep historical chart/panel data usable when cTrader has no live tick (for
example over the weekend). This only supplies a display price from the latest
stored candle before the existing panel-cache validator runs; trading and
execution freshness gates remain unchanged.
"""
from __future__ import annotations

import math

from services.indicator_stream_account_scope import install_account_scoped_indicator_stream
from services.monthly_history_window import (
    guarded_import_api,
    install_monthly_history_window,
    trade_is_current_month,
)
from services.v3b_dashboard_state import install_v3b_dashboard_state_middleware

# Install the account/feed stream namespace before api.py imports strategy
# modules that bind indicator stream functions. This keeps cTrader account
# switches from sharing one immutable candle history.
install_account_scoped_indicator_stream()

# Import api through a read-only compatibility guard so the legacy import-time
# weekly LIVE reset cannot prune history before the monthly window is installed.
api = guarded_import_api()
import app_bootstrap  # noqa: F401 - installs the production bootstrap hooks
from strategies import shared as paper_shared


def _start_ctrader_live_price_stream_before_indicator_fences():
    """Start the read-only spot feed before strategy startup can fail closed.

    The strategy bootstrap intentionally returns when any authoritative
    indicator stream is fenced. The live spot subscription is independent of
    that strategy readiness and must still run so the broker/feed status can
    recover while analysis remains safely paused.
    """
    try:
        app_bootstrap._restore_ctrader_selection_before_market_data()
    except Exception as exc:
        print("CTRADER_LIVE_STREAM_ACCOUNT_RESTORE_ERROR =", str(exc))

    try:
        result = api.start_ctrader_live_price_stream()
    except Exception as exc:
        result = {"ok": False, "status": "not_started", "reason": str(exc)}
        print("CTRADER_LIVE_STREAM_START_ERROR =", str(exc))
    else:
        print("CTRADER_LIVE_STREAM_START =", result)
    return result


# Preserve all previously registered startup ordering, but make the read-only
# tick feed start immediately before the strategy bootstrap. The later call in
# app_bootstrap is idempotent and acts as a harmless retry when strategy startup
# reaches it.
if _start_ctrader_live_price_stream_before_indicator_fences not in api.app.router.on_startup:
    try:
        _strategy_startup_index = api.app.router.on_startup.index(
            app_bootstrap._start_forex_background_task
        )
    except ValueError:
        _strategy_startup_index = len(api.app.router.on_startup)
    api.app.router.on_startup.insert(
        _strategy_startup_index,
        _start_ctrader_live_price_stream_before_indicator_fences,
    )


install_monthly_history_window(api, paper_shared)

# The legacy panel fallback still filtered local LIVE history to the current
# week. Keep broker history/month fallback aligned to the calendar-month window.
def _get_live_recent_history_for_panel_monthly():
    api.run_weekly_live_reset()
    broker_history = api.get_live_broker_closed_history()
    if broker_history:
        return broker_history[: api.MAX_LIVE_TRADE_HISTORY]

    active_ids = {
        str(api.get_live_trade_match_key(trade))
        for trade in api.LIVE_ACTIVE_ORDERS.values()
        if trade and api.get_live_trade_match_key(trade)
    }
    cleaned = []
    for trade in api.LIVE_TRADE_HISTORY:
        if str(api.get_live_trade_match_key(trade)) in active_ids:
            continue
        if not api.is_usable_local_live_history_trade(trade):
            continue
        if not trade_is_current_month(trade, api.get_live_month_start_ts()):
            continue
        cleaned.append(trade)
    return cleaned[: api.MAX_LIVE_TRADE_HISTORY]


api.get_live_recent_history_for_panel = _get_live_recent_history_for_panel_monthly

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

# The execution engine already owns the authoritative per-symbol V3B runtime
# status. Expose that status on dashboard JSON responses so the V3B UI does not
# fall back to the legacy 15m block reason. This middleware is display-only.
install_v3b_dashboard_state_middleware(api.app, api)

app = api.app
