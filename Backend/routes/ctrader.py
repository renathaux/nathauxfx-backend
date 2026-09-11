from datetime import datetime, timedelta, timezone
import time

from fastapi import APIRouter, HTTPException, Query, Request

import ctrader_connector as _ctrader_connector
from services.ctrader_transport_guard import install_ctrader_transport_guard
from services.trade_signal_lifecycle_guard import (
    clone_panel_for_transport,
    install_trade_signal_lifecycle_guard,
)

# Install before importing/using cTrader market-data functions. The guard only
# bounds read/auth requests; broker order/amend/close payloads are untouched.
install_ctrader_transport_guard()

from ctrader_connector import (
    fetch_ctrader_historical_candles,
    get_ctrader_market_data,
    get_live_prices,
    get_symbol_risk_fallback,
    start_ctrader_live_price_stream,
)
from services.customer_forex_guard import _bearer
from services.user_auth_service import require_admin
from indicators.smc import analyze_structure as analyze_xauusd_structure
from indicators.smc.legacy_engine import analyze_structure as analyze_legacy_structure
from services.ctrader_service import get_health_snapshot

router = APIRouter()

_ALLOWED_SYMBOLS = {"EURUSD", "XAUUSD"}
_TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
_CHART_HISTORY_TIMEFRAME_MINUTES = {"1m": 1, **_TIMEFRAME_MINUTES}
_HISTORICAL_EXPORT_TIMEFRAMES = {"15m": "15m", "15min": "15m", "m15": "15m"}
_MAX_HISTORICAL_EXPORT_RANGE = timedelta(days=14)
_MAX_CHART_HISTORY_RANGE = timedelta(days=62)
_MAX_M1_RESEARCH_WINDOW = timedelta(days=1)
_MAX_TICK_RESEARCH_WINDOW = timedelta(minutes=2)
_TICK_REQ = 2145
_TICK_RES = 2146
_TICK_QUOTE_TYPES = {"bid": 1, "ask": 2}


def _enable_read_only_m1_history():
    """Enable cTrader's native M1 trendbar period for observation-only reads.

    The execution strategy and its supported timeframes are intentionally not
    changed. This only teaches the historical market-data helper the native
    cTrader M1 period so Strategy Lab research can resolve intrabar ordering.
    """
    for alias in ("1m", "1min", "m1"):
        _ctrader_connector.CTRADER_TRENDBAR_PERIODS.setdefault(alias, 1)
    _ctrader_connector.CTRADER_TRENDBAR_PERIOD_MINUTES.setdefault(1, 1)


def _normalize_utc(value):
    stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _serialize_closed_candles(frame, start_utc, end_utc, period_minutes):
    if frame is None or frame.empty:
        return []
    data = frame.copy()
    data.index = data.index.map(
        lambda value: value if getattr(value, "tzinfo", None) else value.tz_localize("UTC")
    )
    data = data[~data.index.duplicated(keep="last")].sort_index()
    period = timedelta(minutes=period_minutes)
    data = data[
        (data.index >= start_utc)
        & (data.index <= end_utc)
        & (data.index.map(lambda value: value.to_pydatetime() + period <= end_utc))
    ]
    return [
        {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            **({"volume": float(row["Volume"])} if "Volume" in row.index else {}),
        }
        for timestamp, row in data.iterrows()
    ]


def _fetch_read_only_ticks(symbol, quote, start_utc, end_utc):
    """Fetch a tiny historical tick window directly from cTrader without persistence."""
    config = _ctrader_connector.get_ctrader_config()
    if not config:
        raise HTTPException(status_code=503, detail="cTrader config unavailable")

    account_id = int(config["account_id"])
    host, port = _ctrader_connector.CTRADER_JSON_ENDPOINTS[config["env"]]
    sock = _ctrader_connector.open_ctrader_json_socket(host, port)
    try:
        try:
            sock.settimeout(12)
        except Exception:
            pass
        _ctrader_connector.authorize_ctrader_socket(sock, config, account_id)
        symbol_details = _ctrader_connector.fetch_ctrader_symbol_details(sock, account_id)
        symbol_info = _ctrader_connector.resolve_ctrader_symbol(symbol_details, symbol)
        if not symbol_info:
            raise HTTPException(status_code=503, detail="cTrader symbol unavailable")

        symbol_id = int(symbol_info["symbol_id"])
        digits = int(symbol_info.get("digits") or 2)
        quote_type = _TICK_QUOTE_TYPES[quote]
        start_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        cursor_end_ms = end_ms
        ticks = []
        seen = set()
        complete = True

        for _page in range(20):
            response = _ctrader_connector.send_ctrader_request(
                sock,
                _TICK_REQ,
                {
                    "ctidTraderAccountId": account_id,
                    "symbolId": symbol_id,
                    "type": quote_type,
                    "fromTimestamp": start_ms,
                    "toTimestamp": cursor_end_ms,
                },
                _TICK_RES,
            )
            payload = response.get("payload", {}) if isinstance(response, dict) else {}
            raw_ticks = payload.get("tickData") or []
            if not raw_ticks:
                break

            current_ms = None
            page_times = []
            for index, item in enumerate(raw_ticks):
                if not isinstance(item, dict) or item.get("timestamp") is None or item.get("tick") is None:
                    continue
                raw_timestamp = int(item["timestamp"])
                if index == 0:
                    current_ms = raw_timestamp
                elif current_ms is not None:
                    current_ms -= raw_timestamp
                if current_ms is None:
                    continue
                page_times.append(current_ms)
                if current_ms < start_ms or current_ms > end_ms:
                    continue
                price = round(int(item["tick"]) / 100000.0, digits)
                key = (current_ms, price)
                if key in seen:
                    continue
                seen.add(key)
                stamp = datetime.fromtimestamp(current_ms / 1000.0, tz=timezone.utc)
                ticks.append({
                    "timestamp": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "timestamp_ms": current_ms,
                    "quote": quote,
                    "price": price,
                })

            has_more = bool(payload.get("hasMore"))
            if not has_more:
                break
            if not page_times:
                complete = False
                break
            oldest_ms = min(page_times)
            if oldest_ms <= start_ms:
                break
            next_end = oldest_ms - 1
            if next_end >= cursor_end_ms:
                complete = False
                break
            cursor_end_ms = next_end
        else:
            complete = False

        ticks.sort(key=lambda row: (row["timestamp_ms"], row["price"]))
        return ticks, complete
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _require_candle_export_admin(request: Request):
    """Accept either current database admin auth or the legacy owner token."""
    try:
        return require_admin(request)
    except HTTPException:
        import api

        token = _bearer(request.headers)
        session = api.SESSIONS.get(token) if token else None
        if not isinstance(session, dict) or str(session.get("role") or "").lower() != "admin":
            raise HTTPException(status_code=403, detail="ADMIN_CANDLE_EXPORT_REQUIRED")
        return session


@router.on_event("startup")
def _install_trade_signal_lifecycle_guard():
    # api.py is fully imported by startup time, so the lifecycle can be wrapped
    # without a circular import during module initialization.
    install_trade_signal_lifecycle_guard()


@router.get("/health/ctrader")
def ctrader_health():
    return {
        "ok": True,
        **get_health_snapshot(),
    }


@router.get("/panel-data", include_in_schema=False)
@router.get("/dashboard-feed", include_in_schema=False)
def nonblocking_dashboard_feed(force: int = 0):
    """Serve browser dashboard reads from in-memory cache only.

    Both URLs intentionally use this lightweight route so old cached browsers
    cannot fall through to the heavyweight legacy handler. All cloning here is
    bounded/cycle-safe, so a recursive diagnostic object can never turn the
    dashboard into NO DATA.

    No strategy parameters, signal rules, broker execution, risk, SL/TP, or
    Binary logic are changed.
    """
    import api

    try:
        cached = api.PANEL_CACHE.get("data")
        if not isinstance(cached, dict):
            cached = api.default_panel()
        data = clone_panel_for_transport(cached)
        if not isinstance(data, dict):
            data = api.default_panel()

        now = time.time()
        last_update = float(api.PANEL_CACHE.get("last_update") or 0)
        age = max(now - last_update, 0) if last_update else 0
        refresh_state = clone_panel_for_transport(api.PANEL_REFRESH_STATE or {})
        if not isinstance(refresh_state, dict):
            refresh_state = {}
        live_meta = api.LIVE_PANEL_META_CACHE or {}
        live_pl = clone_panel_for_transport(live_meta.get("live_pl_sync") or {})
        if not isinstance(live_pl, dict):
            live_pl = {}

        for key in (
            "weekly_realized_pl",
            "daily_realized_pl",
            "daily_total_pl",
            "monthly_realized_pl",
            "floating_live_pl",
            "weekly_total_pl",
        ):
            data[key] = live_pl.get(key, data.get(key, 0))

        def _enabled(value):
            if isinstance(value, dict):
                return bool(value.get("enabled", False))
            return bool(value)

        paper_enabled = _enabled(getattr(api, "AUTO_TRADE_ENABLED", False))
        live_enabled = _enabled(getattr(api, "LIVE_AUTO_TRADE_ENABLED", False))
        live_account = clone_panel_for_transport(
            getattr(api, "LIVE_ACCOUNT_STATE", {}) or {}
        )
        live_orders = clone_panel_for_transport(
            getattr(api, "LIVE_ACTIVE_ORDERS", {}) or {}
        )
        live_positions = clone_panel_for_transport(
            live_meta.get("live_positions") or []
        )
        live_recent_history = clone_panel_for_transport(
            live_meta.get("live_recent_history") or []
        )
        live_trade_stats = clone_panel_for_transport(
            live_meta.get("live_trade_stats") or {}
        )
        live_price_status = clone_panel_for_transport(
            live_meta.get("live_price_status") or {}
        )

        if not isinstance(live_positions, list):
            live_positions = []
        if not isinstance(live_recent_history, list):
            live_recent_history = []
        if not isinstance(live_trade_stats, dict):
            live_trade_stats = {}
        if not isinstance(live_price_status, dict):
            live_price_status = {}

        data["_meta"] = {
            "source": "dashboard_feed_cache_only_cycle_safe",
            "cache_age_seconds": round(age, 1),
            "stale_data": bool(refresh_state.get("last_error") or not last_update),
            "last_successful_refresh": refresh_state.get("last_success"),
            "refresh_seconds": getattr(api, "CACHE_SECONDS", 15),
            "error": refresh_state.get("last_error"),
            "brain_refresh": refresh_state,
            "live_meta_last_update": live_meta.get("last_update"),
            "live_meta_error": live_meta.get("last_error"),
            "paper_auto_enabled": paper_enabled,
            "live_auto_enabled": live_enabled,
            "auto_trade_state": {
                "paper_enabled": paper_enabled,
                "live_enabled": live_enabled,
                "source": "memory_cache",
            },
            "live_account": live_account if isinstance(live_account, dict) else {},
            "live_active_orders": live_orders if isinstance(live_orders, dict) else {},
            "broker_open_positions_count": len(live_positions),
            "live_trade_history": live_recent_history,
            "live_trade_stats": {
                **live_trade_stats,
                **live_pl,
            },
            "weekly_realized_pl": live_pl.get("weekly_realized_pl", 0),
            "daily_realized_pl": live_pl.get("daily_realized_pl", 0),
            "daily_total_pl": live_pl.get("daily_total_pl", 0),
            "monthly_realized_pl": live_pl.get("monthly_realized_pl", 0),
            "floating_live_pl": live_pl.get("floating_live_pl", 0),
            "weekly_total_pl": live_pl.get("weekly_total_pl", 0),
            "live_price_status": live_price_status,
            "nonblocking_cache_only": True,
            "cycle_safe_transport": True,
            "legacy_panel_alias": True,
            "force_requested": bool(force),
        }

        safe = getattr(api, "_json_safe_panel_value", None)
        return safe(data) if callable(safe) else data
    except Exception as exc:
        fallback = api.default_panel()
        fallback["_meta"] = {
            "source": "dashboard_feed_failsafe",
            "stale_data": True,
            "error": f"cache read failed: {type(exc).__name__}",
            "nonblocking_cache_only": True,
            "cycle_safe_transport": True,
            "legacy_panel_alias": True,
        }
        return fallback


@router.get("/chart/live-ticks")
def live_chart_ticks():
    """Latest cTrader spot snapshots for visual candle updates only."""
    start_ctrader_live_price_stream()
    status = get_live_prices() or {}
    return {
        "ok": True,
        "source": "ctrader",
        "live_prices": status.get("live_prices", {}),
        "live_price_health": status.get("live_price_health"),
        "live_price_stale_symbols": status.get("live_price_stale_symbols", []),
        "live_price_last_update": status.get("live_price_last_update"),
    }


@router.get("/admin/ctrader/candles")
def export_closed_ctrader_candles(
    request: Request,
    symbol: str = Query(...),
    timeframe: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
):
    """Export bounded native closed cTrader candles for forensic observation."""
    _require_candle_export_admin(request)
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = _HISTORICAL_EXPORT_TIMEFRAMES.get(
        str(timeframe or "").strip().lower()
    )
    if normalized_symbol not in _ALLOWED_SYMBOLS:
        raise HTTPException(status_code=422, detail="symbol must be EURUSD or XAUUSD")
    if normalized_timeframe is None:
        raise HTTPException(status_code=422, detail="timeframe must be M15")

    start_utc = _normalize_utc(start)
    end_utc = _normalize_utc(end)
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end_utc - start_utc > _MAX_HISTORICAL_EXPORT_RANGE:
        raise HTTPException(status_code=422, detail="date range exceeds 14 days")

    frame = fetch_ctrader_historical_candles(
        normalized_symbol,
        normalized_timeframe,
        start_utc,
        end_utc,
    )
    if frame is None or frame.empty:
        raise HTTPException(status_code=503, detail="cTrader returned no historical candles")

    data = frame.copy()
    data.index = data.index.map(
        lambda value: value if getattr(value, "tzinfo", None) else value.tz_localize("UTC")
    )
    data = data[~data.index.duplicated(keep="last")].sort_index()
    closed_before = datetime.now(timezone.utc)
    period = timedelta(minutes=_TIMEFRAME_MINUTES[normalized_timeframe])
    data = data[
        (data.index >= start_utc)
        & (data.index <= end_utc)
        & (data.index.map(lambda value: value.to_pydatetime() + period <= closed_before))
    ]

    candles = []
    for timestamp, row in data.iterrows():
        candle = {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
        if "Volume" in row.index:
            candle["volume"] = float(row["Volume"])
        candles.append(candle)

    return {
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": end_utc.isoformat().replace("+00:00", "Z"),
        "closed_only": True,
        "read_only": True,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/chart/candles-history")
def chart_candle_history(
    request: Request,
    symbol: str = Query(...),
    timeframe: str = Query(...),
    days: int = Query(default=62, ge=1, le=62),
):
    """Return bounded, closed native candles for the visual chart only.

    This uses the historical market-data request and never reads or mutates the
    strategy candle cache, order state, positions, or stops. M1 is observation
    only and is not added to the strategy/SMA/SMC execution timeframe set.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = str(timeframe or "").strip().lower()
    if normalized_symbol not in _ALLOWED_SYMBOLS:
        raise HTTPException(status_code=422, detail="symbol must be EURUSD or XAUUSD")
    if normalized_timeframe not in _CHART_HISTORY_TIMEFRAME_MINUTES:
        raise HTTPException(status_code=422, detail="timeframe must be 1m, 5m, 15m, or 1h")
    if normalized_timeframe == "1m":
        _enable_read_only_m1_history()

    end_utc = datetime.now(timezone.utc)
    start_utc = end_utc - min(timedelta(days=days), _MAX_CHART_HISTORY_RANGE)
    frame = fetch_ctrader_historical_candles(
        normalized_symbol, normalized_timeframe, start_utc, end_utc
    )
    if frame is None or frame.empty:
        raise HTTPException(status_code=503, detail="cTrader returned no historical candles")

    candles = _serialize_closed_candles(
        frame,
        start_utc,
        end_utc,
        _CHART_HISTORY_TIMEFRAME_MINUTES[normalized_timeframe],
    )
    return {
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "days": days,
        "closed_only": True,
        "read_only": True,
        "observation_only": normalized_timeframe == "1m",
        "count": len(candles),
        "candles": candles,
    }


@router.get("/chart/candles-window", include_in_schema=False)
def chart_candle_window(
    symbol: str = Query(...),
    timeframe: str = Query(default="1m"),
    start: datetime = Query(...),
    end: datetime = Query(...),
):
    """Return a tightly bounded M1 window for read-only intrabar research.

    This route exists only to resolve Strategy Lab ordering ambiguity. It does
    not persist candles and cannot place, amend, close, or authorize trades.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = str(timeframe or "").strip().lower()
    if normalized_symbol not in _ALLOWED_SYMBOLS:
        raise HTTPException(status_code=422, detail="symbol must be EURUSD or XAUUSD")
    if normalized_timeframe != "1m":
        raise HTTPException(status_code=422, detail="research window supports 1m only")

    start_utc = _normalize_utc(start)
    end_utc = _normalize_utc(end)
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end_utc - start_utc > _MAX_M1_RESEARCH_WINDOW:
        raise HTTPException(status_code=422, detail="M1 research window exceeds 1 day")

    _enable_read_only_m1_history()
    frame = fetch_ctrader_historical_candles(
        normalized_symbol,
        normalized_timeframe,
        start_utc,
        end_utc,
    )
    if frame is None or frame.empty:
        raise HTTPException(status_code=503, detail="cTrader returned no historical candles")

    candles = _serialize_closed_candles(frame, start_utc, end_utc, 1)
    return {
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": end_utc.isoformat().replace("+00:00", "Z"),
        "closed_only": True,
        "read_only": True,
        "observation_only": True,
        "affects_strategy": False,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/chart/ticks-window", include_in_schema=False)
def chart_tick_window(
    symbol: str = Query(default="XAUUSD"),
    quote: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
):
    """Temporary, tightly bounded cTrader historical tick window for Gold audit."""
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_quote = str(quote or "").strip().lower()
    if normalized_symbol != "XAUUSD":
        raise HTTPException(status_code=422, detail="tick research window supports XAUUSD only")
    if normalized_quote not in _TICK_QUOTE_TYPES:
        raise HTTPException(status_code=422, detail="quote must be bid or ask")

    start_utc = _normalize_utc(start)
    end_utc = _normalize_utc(end)
    if end_utc <= start_utc:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end_utc - start_utc > _MAX_TICK_RESEARCH_WINDOW:
        raise HTTPException(status_code=422, detail="tick research window exceeds 2 minutes")

    ticks, complete = _fetch_read_only_ticks(
        normalized_symbol,
        normalized_quote,
        start_utc,
        end_utc,
    )
    if not ticks:
        raise HTTPException(status_code=503, detail="cTrader returned no historical ticks")
    return {
        "symbol": normalized_symbol,
        "quote": normalized_quote,
        "start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": end_utc.isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "observation_only": True,
        "affects_strategy": False,
        "persisted": False,
        "pagination_complete": complete,
        "count": len(ticks),
        "ticks": ticks,
    }


def _closed_only(frame, timeframe):
    """Remove the currently forming bar before SMC analysis."""
    if frame is None or frame.empty:
        return frame
    minutes = _TIMEFRAME_MINUTES[timeframe]
    cutoff = datetime.now(timezone.utc)
    data = frame.copy()
    data.index = data.index.map(
        lambda value: value if getattr(value, "tzinfo", None) else value.tz_localize("UTC")
    )
    return data[
        data.index.map(lambda value: value.to_pydatetime() + timedelta(minutes=minutes) <= cutoff)
    ]


def chart_smc_structure(
    symbol: str = Query(default="EURUSD"),
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=250, ge=50, le=500),
):
    """Legacy chart calculation helper; it is intentionally not a public route.

    The public route is registered by app_bootstrap and reads persisted events.
    This helper never places, modifies, closes, or authorizes broker orders.
    It deliberately removes the forming candle before calculating swings,
    BOS, and CHoCH so the visual indicator cannot repaint from live ticks.
    """
    normalized_symbol = str(symbol or "").upper()
    normalized_timeframe = str(timeframe or "").lower()
    if normalized_symbol not in _ALLOWED_SYMBOLS:
        raise HTTPException(status_code=422, detail="symbol must be EURUSD or XAUUSD")
    if normalized_timeframe not in _TIMEFRAME_MINUTES:
        raise HTTPException(status_code=422, detail="timeframe must be 5m, 15m, or 1h")

    frame = get_ctrader_market_data(
        normalized_symbol,
        normalized_timeframe,
        limit=limit,
    )
    closed = _closed_only(frame, normalized_timeframe)
    structure_analyzer = (
        analyze_xauusd_structure
        if normalized_symbol == "XAUUSD"
        else analyze_legacy_structure
    )
    point_size = get_symbol_risk_fallback(normalized_symbol).get("tick_size")
    structure = structure_analyzer(
        closed,
        left_bars=2,
        right_bars=2,
        timeframe=normalized_timeframe,
        point_size=point_size,
    )
    structure.update({
        "symbol": normalized_symbol,
        "timeframe": normalized_timeframe,
        "source": "ctrader_closed_candles",
        "closed_candle_count": int(len(closed)) if closed is not None else 0,
        "observation_only": True,
        "affects_strategy": False,
    })
    return structure