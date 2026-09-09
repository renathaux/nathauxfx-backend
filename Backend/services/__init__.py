"""Shared service-package bootstrap policies.

Phase 1.2 keeps the indicator stream strict by default.  The only automatic
exception is for cTrader-shaped OHLCV frames, where Open API legitimately
omits a trendbar when no tick arrived during that period.  Even then, only
small sparse gaps are tolerated; larger unexplained holes still use the
stream's normal fail-closed path.
"""
from functools import wraps


def _install_ctrader_sparse_trendbar_policy():
    from . import indicator_event_stream_service as _stream

    if getattr(_stream, "_CTRADER_SPARSE_POLICY_INSTALLED", False):
        return

    original_initialize = _stream.initialize_indicator_stream
    original_get = _stream.get_authoritative_structure

    def _looks_like_ctrader_frame(frame):
        columns = getattr(frame, "columns", None)
        if columns is None:
            return False
        return {"Open", "High", "Low", "Close", "Volume"}.issubset(set(columns))

    def _small_sparse_gaps_only(frame, symbol, timeframe):
        if not _looks_like_ctrader_frame(frame):
            return False

        normalized_symbol = _stream._normal_symbol(symbol)
        normalized_timeframe = _stream._normal_timeframe(timeframe)
        minutes = _stream.SUPPORTED_TIMEFRAMES.get(normalized_timeframe)
        if not minutes:
            return False

        try:
            ordered = sorted(_stream._utc(value) for value in frame.index)
        except Exception:
            return False

        interval = _stream.pd.Timedelta(minutes=minutes)
        for previous, following in zip(ordered, ordered[1:]):
            if following - previous <= interval:
                continue
            if _stream._known_market_closure(
                normalized_symbol,
                previous,
                following,
            ):
                continue

            expected_missing = 0
            cursor = previous + interval
            while cursor < following:
                if _stream._expected_market_candle(normalized_symbol, cursor):
                    expected_missing += 1
                    if expected_missing > 2:
                        return False
                cursor += interval

        return True

    @wraps(original_initialize)
    def initialize_indicator_stream(frame, symbol, timeframe, point_size, *args, **kwargs):
        if "allow_sparse_trendbars" not in kwargs:
            kwargs["allow_sparse_trendbars"] = _small_sparse_gaps_only(
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
            kwargs["allow_sparse_trendbars"] = _small_sparse_gaps_only(
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
    _stream._ctrader_sparse_frame_allowed = _small_sparse_gaps_only


_install_ctrader_sparse_trendbar_policy()
