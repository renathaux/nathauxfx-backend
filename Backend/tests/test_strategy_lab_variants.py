from __future__ import annotations

import pandas as pd

from services.strategy_lab import m5_quality_variant
from services.strategy_lab import v2a_m5_quality, v2b_m5_quality, v2c_m15_quality
from services.strategy_lab.replay_engine import _strategy_engine, _strategy_parameters


def _frame(*rows):
    index = pd.date_range("2026-09-01T00:00:00Z", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=index)


def test_v2a_50_30_keeps_moderate_quality_and_rejects_weak_body():
    candles = _frame(
        (1.1000, 1.1010, 1.0999, 1.1002),
        (1.1000, 1.1010, 1.0999, 1.1008),
    )
    filtered = m5_quality_variant._filtered_quality_frame(
        candles,
        "BUY",
        v2a_m5_quality.MIN_BODY_RATIO,
        v2a_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
    )
    assert list(filtered.index) == [candles.index[1]]


def test_v2b_is_looser_than_v2a_without_becoming_baseline():
    # About 46% body with a small close-side wick: V2B yes, V2A no.
    candle = _frame((1.09978, 1.1010, 1.0990, 1.10070))
    quality = m5_quality_variant.candle_quality(candle.iloc[0], "BUY")
    assert quality["body_ratio"] >= v2b_m5_quality.MIN_BODY_RATIO
    assert quality["body_ratio"] < v2a_m5_quality.MIN_BODY_RATIO
    assert quality["close_side_wick_ratio"] <= v2b_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO
    assert m5_quality_variant._filtered_quality_frame(
        candle, "BUY", v2b_m5_quality.MIN_BODY_RATIO,
        v2b_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
    ).shape[0] == 1
    assert m5_quality_variant._filtered_quality_frame(
        candle, "BUY", v2a_m5_quality.MIN_BODY_RATIO,
        v2a_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO,
    ).empty


def test_v2c_m15_quality_60_30_uses_break_candle_body_and_close_side_wick():
    strong_sell = pd.Series({"Open": 1.1625, "High": 1.1626, "Low": 1.1610, "Close": 1.1613})
    weak_sell = pd.Series({"Open": 1.1620, "High": 1.1624, "Low": 1.1610, "Close": 1.1617})
    strong = v2c_m15_quality._m15_quality(strong_sell, "SELL")
    weak = v2c_m15_quality._m15_quality(weak_sell, "SELL")
    assert strong["direction_ok"] is True
    assert strong["body_ratio"] >= v2c_m15_quality.MIN_BODY_RATIO
    assert strong["close_side_wick_ratio"] <= v2c_m15_quality.MAX_CLOSE_SIDE_WICK_RATIO
    assert weak["body_ratio"] < v2c_m15_quality.MIN_BODY_RATIO


def test_replay_registry_has_all_comparison_variants_and_exact_parameters():
    for strategy in (
        "v2a_m5_quality_50_30",
        "v2b_m5_quality_45_35",
        "v2c_m15_quality_60_30",
    ):
        candidate_fn, evaluate_fn, resolve_fn = _strategy_engine(strategy)
        assert callable(candidate_fn) and callable(evaluate_fn) and callable(resolve_fn)

    assert _strategy_parameters("v2a_m5_quality_50_30") == {
        "m5_minimum_body_ratio": 0.50,
        "m5_maximum_close_side_wick_ratio": 0.30,
    }
    assert _strategy_parameters("v2b_m5_quality_45_35") == {
        "m5_minimum_body_ratio": 0.45,
        "m5_maximum_close_side_wick_ratio": 0.35,
    }
    assert _strategy_parameters("v2c_m15_quality_60_30") == {
        "m15_minimum_body_ratio": 0.60,
        "m15_maximum_close_side_wick_ratio": 0.30,
    }
