"""Independent per-window aggregation/facts are the legacy behavior authority."""
import pickle

import numpy as np
import pandas as pd
import pytest

from services.strategy_engine import market_facts
from services.strategy_fast_window_facts import build_window_facts, window_swings
from services.strategy_simulator_static_data import _aggregate


@pytest.fixture
def frame():
    times = pd.date_range("2025-01-01", periods=12 * 288, freq="5min", tz="UTC")
    rng = np.random.default_rng(832)
    prices = 2000 + rng.normal(0, 3, len(times)).cumsum()
    result = pd.DataFrame(dict(Open=prices, High=prices+3, Low=prices-2, Close=prices+1, Volume=0), index=times)
    # Missing bars must produce the same incomplete aggregate buckets.
    return result.drop(times[510:517])


def bundle_for(frame, end):
    return {"5m": frame, **{tf: _aggregate(frame, tf, end) for tf in ["15m", "1h", "4h"]}}


def windows_for(frame):
    t = frame.index[0]
    return [(t+pd.Timedelta(days=3), t+pd.Timedelta(days=7), t+pd.Timedelta(hours=7, minutes=5)),
            (t+pd.Timedelta(days=7), t+pd.Timedelta(days=11, minutes=2), t+pd.Timedelta(days=4, minutes=5))]


@pytest.mark.parametrize("trading,structure,trend", [("5m", "15m", "4h"), ("5m", "1h", "1h"), ("15m", "15m", "4h"), ("5m", "5m", "5m")])
def test_every_window_fact_matches_independent_legacy_preparation(frame, trading, structure, trend):
    windows = windows_for(frame)
    actual = build_window_facts(bundle_for(frame, windows[-1][1]), "XAUUSD", trading, trend, structure, windows)
    for (start, end, warmup), (actual_start, actual_end, timeline) in zip(windows, actual):
        assert (actual_start, actual_end) == (start, end)
        local = frame.loc[(frame.index >= warmup) & (frame.index < end)]
        reference = market_facts.build_market_facts(bundle_for(local, end), "XAUUSD", trading, trend, structure)
        assert timeline.timestamps() == reference.timestamps()
        for stamp_index, stamp in enumerate(reference.timestamps()):
            assert timeline.candle(stamp) == reference.candle(stamp)
            assert timeline.structure_candle(stamp) == reference.structure_candle(stamp)
            assert timeline.structure_event(stamp) == reference.structure_event(stamp)
            # Includes EMA200 and BOS trend initialization near each warm-up edge.
            assert timeline.trend(stamp) == reference.trend(stamp)
            assert timeline.previous_timestamp(stamp) == reference.previous_timestamp(stamp)
            assert timeline.next_timestamp(stamp) == reference.next_timestamp(stamp)
            if stamp_index % 19 == 0:
                for side in ["BUY", "SELL"]:
                    for entry in [1950, 2000, 2050]:
                        assert timeline.opposite_swing(stamp, side, entry) == reference.opposite_swing(stamp, side, entry)
        assert timeline.candle(end) is None
        assert timeline.candle(warmup-pd.Timedelta(minutes=5)) is None


def test_global_swings_filter_rebase_exactly_and_cannot_see_future(frame):
    swings = market_facts._serialise_confirmed_swings(frame)
    for first, last in [(0, 3), (9, 100), (100, 2000), (1500, len(frame))]:
        expected = market_facts._serialise_confirmed_swings(frame.iloc[first:last])
        actual = window_swings(swings, first, last)
        assert actual == expected
        assert all(s["index"] >= 2 and s["confirmed_index"] < last-first for s in actual)


def test_swings_detected_once_per_required_timeframe_and_backing_shared(frame, monkeypatch):
    windows = windows_for(frame)
    bundle = bundle_for(frame, windows[-1][1])
    calls = []
    original = market_facts._serialise_confirmed_swings
    def record(frame):
        calls.append(len(frame))
        return original(frame)
    monkeypatch.setattr(market_facts, "_serialise_confirmed_swings", record)
    progress = []
    result = build_window_facts(bundle, "XAUUSD", "5m", "4h", "15m", windows, progress.append)
    assert sorted(calls) == sorted([len(bundle["5m"]), len(bundle["4h"])])
    assert progress == [0.5, 1.0]
    assert result[0][2]._candles.parent is result[1][2]._candles.parent
    assert np.shares_memory(result[0][2]._candles.values, result[1][2]._candles.values)
    restored = pickle.loads(pickle.dumps(result))
    assert restored[0][2]._candles.parent is restored[1][2]._candles.parent
    assert np.shares_memory(restored[0][2]._candles.values, restored[1][2]._candles.values)
    assert not restored[0][2]._candles.values.flags.writeable
    stamp = result[1][0]
    assert restored[1][2].candle(stamp) == result[1][2].candle(stamp)


def test_bounded_cache_roundtrip_preserves_window_list(frame, tmp_path):
    from services.strategy_fast_cache import FactsCache
    windows = windows_for(frame)
    value = build_window_facts(bundle_for(frame, windows[-1][1]), "XAUUSD", "5m", "4h", "15m", windows)
    cache = FactsCache(tmp_path, max_bytes=2*1024*1024)
    assert cache.put("a"*64, value)
    loaded = cache.get("a"*64)
    assert len(loaded) == 2
    assert loaded[0][2]._candles.parent is loaded[1][2]._candles.parent
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= cache.max_bytes
