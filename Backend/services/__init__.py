"""Shared service-package bootstrap policies.

Phase 1.2 keeps the indicator stream strict by default. cTrader Open API is a
provider-specific exception: its OHLCV trendbar history can legitimately omit
a time bucket when no tick arrived, so cTrader-shaped frames are allowed to be
sparse without inventing replacement candles. Duplicate/conflicting candle
checks and all immutable-event safeguards still run normally.
"""
from functools import wraps


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

    @wraps(original_initialize)
    def initialize_indicator_stream(frame, symbol, timeframe, point_size, *args, **kwargs):
        if "allow_sparse_trendbars" not in kwargs:
            kwargs["allow_sparse_trendbars"] = _ctrader_sparse_frame_allowed(
                frame,
                symbol,
                timeframe,
            )
        return original_initialize(
            frame,
            symbol,
            timeframe,
            point_size,
            *args,
            **kwargs,
        )

    @wraps(original_get)
    def get_authoritative_structure(frame, symbol, timeframe, point_size, *args, **kwargs):
        if "allow_sparse_trendbars" not in kwargs:
            kwargs["allow_sparse_trendbars"] = _ctrader_sparse_frame_allowed(
                frame,
                symbol,
                timeframe,
            )
        return original_get(
            frame,
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


_install_ctrader_sparse_trendbar_policy()
