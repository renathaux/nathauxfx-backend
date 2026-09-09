"""Shared service-package bootstrap policies.

Phase 1.2 keeps the indicator stream strict by default. cTrader Open API is a
provider-specific exception: its OHLCV trendbar history can legitimately omit
a time bucket when no tick arrived, so cTrader-shaped frames are allowed to be
sparse without inventing replacement candles. Duplicate/conflicting candle
checks and all immutable-event safeguards still run normally.

cTrader can also revise a just-closed 15m trendbar for a few minutes after the
nominal close. The authoritative stream therefore waits one 5m confirmation
slot before accepting a 15m provider candle. This keeps immutable candles truly
final without changing BOS/CHoCH, risk, or execution rules.

The connector may append a synthetic current candle for display and strategy
visibility. That synthetic row must never become the durable provider cache:
once a synthetic row survives into a later bucket it can look closed and would
pollute the immutable indicator stream. The cache guard below preserves only
the last provider-fetched frame while still returning the synthetic copy to
legacy callers.
"""
from datetime import datetime, timezone
from functools import wraps


CTRADER_15M_SETTLE_SECONDS = 5 * 60


def _install_ctrader_provider_cache_guard():
    """Keep cTrader's shared cache provider-only while callers see live candles.

    ``get_ctrader_market_data`` intentionally returns a copy with the current
    live-tick candle appended. On a cache hit the legacy implementation also
    writes that returned copy back into ``CTRADER_CANDLE_CACHE``. Preserve the
    provider snapshot whenever no new provider fetch occurred so a synthetic
    candle can never age into the authoritative closed-candle stream.
    """
    try:
        import ctrader_connector as _ctrader
    except Exception:
        return False

    if getattr(_ctrader, "_PROVIDER_ONLY_CANDLE_CACHE_GUARD_INSTALLED", False):
        return True

    original_get = _ctrader.get_ctrader_market_data

    @wraps(original_get)
    def get_ctrader_market_data(symbol, timeframe, *args, **kwargs):
        cache_key = _ctrader.get_ctrader_candle_cache_key(symbol, timeframe)
        cached_before = _ctrader.CTRADER_CANDLE_CACHE.get(cache_key)
        provider_snapshot = None
        fetched_at_before = None
        if isinstance(cached_before, dict):
            before_data = cached_before.get("data")
            if before_data is not None:
                try:
                    provider_snapshot = before_data.copy(deep=True)
                except TypeError:
                    provider_snapshot = before_data.copy()
            fetched_at_before = cached_before.get("fetched_at")

        result = original_get(symbol, timeframe, *args, **kwargs)

        cached_after = _ctrader.CTRADER_CANDLE_CACHE.get(cache_key)
        if isinstance(cached_after, dict) and provider_snapshot is not None:
            # A changed fetched_at means a real provider refresh replaced the
            # cache and must be kept. If it is unchanged, any data mutation came
            # from append_current_forming_candle/cache fallback and is synthetic.
            if cached_after.get("fetched_at") == fetched_at_before:
                cached_after["data"] = provider_snapshot

        return result

    _ctrader.get_ctrader_market_data = get_ctrader_market_data
    _ctrader._PROVIDER_ONLY_CANDLE_CACHE_GUARD_INSTALLED = True
    _ctrader._PROVIDER_ONLY_CANDLE_CACHE_ORIGINAL_GET = original_get

    # api.py imports the connector function by value before app_bootstrap runs.
    # Update that already-bound alias too once api is fully importable.
    try:
        import api as _api
        if getattr(_api, "get_ctrader_market_data", None) is original_get:
            _api.get_ctrader_market_data = get_ctrader_market_data
    except Exception:
        pass

    return True


def _install_ctrader_sparse_trendbar_policy():
    from . import indicator_event_stream_service as _stream

    if getattr(_stream, "_CTRADER_SPARSE_POLICY_INSTALLED", False):
        return

    original_initialize = _stream.initialize_indicator_stream
    original_get = _stream.get_authoritative_structure

    def _ctrader_sparse_frame_allowed(frame, _symbol, timeframe):
        columns = getattr(frame, "columns", None)
        if columns is None:
            return False
        if not {"Open", "High", "Low", "Close", "Volume"}.issubset(set(columns)):
            return False
        normalized_timeframe = _stream._normal_timeframe(timeframe)
        return normalized_timeframe in _stream.SUPPORTED_TIMEFRAMES

    def _ctrader_now():
        return datetime.now(timezone.utc)

    def _ctrader_mature_frame(frame, symbol, timeframe, now=None):
        """Delay only cTrader 15m persistence until the bar is provider-final."""
        if not _ctrader_sparse_frame_allowed(frame, symbol, timeframe):
            return frame
        normalized_timeframe = _stream._normal_timeframe(timeframe)
        if normalized_timeframe != "15m":
            return frame
        try:
            current = _stream._utc(
                now if now is not None else _stream._ctrader_now()
            )
            maturity_delay = _stream.pd.Timedelta(
                minutes=_stream.SUPPORTED_TIMEFRAMES[normalized_timeframe],
                seconds=CTRADER_15M_SETTLE_SECONDS,
            )
            latest_mature_open = current - maturity_delay
            keep = [
                _stream._utc(value) <= latest_mature_open
                for value in frame.index
            ]
            return frame.loc[keep].copy()
        except Exception as exc:
            raise _stream.IndicatorStreamUnavailable(
                f"cTrader 15m settle filter unavailable: {exc}"
            ) from exc

    @wraps(original_initialize)
    def initialize_indicator_stream(frame, symbol, timeframe, point_size, *args, **kwargs):
        # The first startup fetch is force-refreshed provider data. Install the
        # cache guard before any later cache hit can persist a synthetic row.
        _install_ctrader_provider_cache_guard()
        is_ctrader = _ctrader_sparse_frame_allowed(frame, symbol, timeframe)
        if "allow_sparse_trendbars" not in kwargs:
            kwargs["allow_sparse_trendbars"] = is_ctrader
        provider_frame = (
            _ctrader_mature_frame(frame, symbol, timeframe)
            if is_ctrader
            else frame
        )
        return original_initialize(
            provider_frame,
            symbol,
            timeframe,
            point_size,
            *args,
            **kwargs,
        )

    @wraps(original_get)
    def get_authoritative_structure(frame, symbol, timeframe, point_size, *args, **kwargs):
        _install_ctrader_provider_cache_guard()
        is_ctrader = _ctrader_sparse_frame_allowed(frame, symbol, timeframe)
        if "allow_sparse_trendbars" not in kwargs:
            kwargs["allow_sparse_trendbars"] = is_ctrader
        provider_frame = (
            _ctrader_mature_frame(frame, symbol, timeframe)
            if is_ctrader
            else frame
        )
        return original_get(
            provider_frame,
            symbol,
            timeframe,
            point_size,
            *args,
            **kwargs,
        )

    _stream.initialize_indicator_stream = initialize_indicator_stream
    _stream.get_authoritative_structure = get_authoritative_structure
    _stream._CTRADER_SPARSE_POLICY_INSTALLED = True
    _stream._ctrader_sparse_frame_allowed = _ctrader_sparse_frame_allowed
    _stream._ctrader_now = _ctrader_now
    _stream._ctrader_mature_frame = _ctrader_mature_frame
    _stream.CTRADER_15M_SETTLE_SECONDS = CTRADER_15M_SETTLE_SECONDS


def _install_paper_entry_shape_guard():
    """Normalize legacy WAIT strings before PAPER reads nested setup fields."""
    from . import paper_live_entry_service as _paper

    if getattr(_paper, "_PAPER_ENTRY_SHAPE_GUARD_INSTALLED", False):
        return

    original_build = _paper.build_paper_entry_result

    @wraps(original_build)
    def build_paper_entry_result(symbol, live_plan, *args, **kwargs):
        safe_plan = dict(live_plan) if isinstance(live_plan, dict) else live_plan
        if isinstance(safe_plan, dict):
            if not isinstance(safe_plan.get("fifteen_m_swing_break"), dict):
                safe_plan["fifteen_m_swing_break"] = {}
            if not isinstance(safe_plan.get("confirmation_5m"), dict):
                safe_plan["confirmation_5m"] = {}
        return original_build(symbol, safe_plan, *args, **kwargs)

    _paper.build_paper_entry_result = build_paper_entry_result
    _paper._PAPER_ENTRY_SHAPE_GUARD_INSTALLED = True


_install_ctrader_sparse_trendbar_policy()
_install_paper_entry_shape_guard()
