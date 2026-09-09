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
"""
from datetime import datetime, timezone
from functools import wraps


CTRADER_15M_SETTLE_SECONDS = 5 * 60


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
