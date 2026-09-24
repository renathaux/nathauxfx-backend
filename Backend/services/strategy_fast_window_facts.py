"""Immutable FAST facts with exact legacy 31-day initialization boundaries.

History and aggregation are global; confirmed swings are detected once per
required timeframe. EMA seeds and the stateful legacy BOS engine MUST reset at
each old warm-up boundary: global initialization changes actual trade choices.
This deliberate exception to global fact construction preserves legacy results.
"""
from __future__ import annotations

from bisect import bisect_left

import pandas as pd

from services.strategy_engine import market_facts
from services.strategy_engine.market_facts_compact import CandleStore, SwingStore

_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


class CandleStoreView(CandleStore):
    """Readonly slice whose pickle references a single shared global backing."""

    def __init__(self, parent, first, last):
        self.parent, self.first, self.last = parent, first, last
        # NumPy pickle reconstructs writable arrays; restore the immutable bound.
        parent.times.flags.writeable = False
        parent.values.flags.writeable = False
        self.time_scale = parent.time_scale
        self.times = parent.times[first:last]
        self.values = parent.values[first:last]

    def __getstate__(self):
        return self.parent, self.first, self.last

    def __setstate__(self, state):
        self.__init__(*state)


def _slice_bounds(frame, timeframe, history_start, end):
    first = int(frame.index.searchsorted(history_start, side="left"))
    # Legacy 5m input includes open < end, even for non-aligned request ends.
    # Aggregated candles require their complete interval to close by end.
    if timeframe == "5m":
        last = int(frame.index.searchsorted(end, side="left"))
    else:
        last = int(frame.index.searchsorted(end - pd.Timedelta(minutes=_MINUTES[timeframe]), side="right"))
    return first, max(first, last)


def window_swings(swings, first, last, *, pivot_indices=None):
    """Exactly the default two-left/two-right detector on frame[first:last]."""
    indices = pivot_indices if pivot_indices is not None else [s["index"] for s in swings]
    left = bisect_left(indices, first + 2)
    right = bisect_left(indices, last - 2)
    return [dict(s, index=s["index"] - first, confirmed_index=s["confirmed_index"] - first)
            for s in swings[left:right] if s["confirmed_index"] < last]


def iter_window_facts(bundle, symbol, trading_timeframe, trend_timeframe,
                       structure_timeframe, windows, progress=None):
    """Yield each legacy window once, retaining only shared numeric history.

    Consume sequentially and release each timeline before requesting the next.
    Progress runs before yielding, and may raise to cancel preprocessing.
    """
    trading_tf = trading_timeframe
    structure_tf = structure_timeframe or trading_tf
    trend_tf = trend_timeframe or trading_tf
    required = set((trading_tf, structure_tf, trend_tf))
    for tf in required:
        if tf not in _MINUTES or tf not in bundle:
            raise ValueError(f"SIMULATION_TIMEFRAME_UNAVAILABLE: {tf}")
        frame = bundle[tf]
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError("SIMULATION_HISTORY_INDEX_INVALID")
    global_swings = {tf: SwingStore(bundle[tf]) for tf in {trading_tf, trend_tf}}
    trading_store = CandleStore(bundle[trading_tf])
    offset = pd.Timedelta(minutes=_MINUTES[structure_tf] - _MINUTES[trading_tf])
    structure_store = trading_store if structure_tf == trading_tf else CandleStore(bundle[structure_tf], offset)
    windows = list(windows)
    for window_index, (start, end, history_start) in enumerate(windows):
        start, end, history_start = map(market_facts._utc, (start, end, history_start))
        if not history_start <= start < end:
            raise ValueError("SIMULATION_WINDOW_INVALID")
        bounds = {tf: _slice_bounds(bundle[tf], tf, history_start, end) for tf in required}
        sliced = {tf: bundle[tf].iloc[first:last] for tf, (first, last) in bounds.items()}
        swings = {tf: global_swings[tf].window(*bounds[tf]) for tf in global_swings}
        candles = CandleStoreView(trading_store, *bounds[trading_tf])
        structure_candles = (candles if structure_tf == trading_tf else
                             CandleStoreView(structure_store, *bounds[structure_tf]))
        timeline = market_facts.build_market_facts(sliced, symbol, trading_tf, trend_tf, structure_tf,
                                                  compact=True, precomputed_swings_by_tf=swings,
                                                  compact_candle_stores=(candles, structure_candles))
        del sliced, swings
        if progress is not None:
            progress((window_index + 1) / len(windows))
        yield start, end, timeline
        del timeline


def build_window_facts(bundle, symbol, trading_timeframe, trend_timeframe,
                       structure_timeframe, windows, progress=None):
    """Compatibility list API; workers should consume iter_window_facts lazily."""
    return list(iter_window_facts(bundle, symbol, trading_timeframe, trend_timeframe,
                                  structure_timeframe, windows, progress))
