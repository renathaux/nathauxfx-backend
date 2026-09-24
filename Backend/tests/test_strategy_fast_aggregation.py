import numpy as np
import pandas as pd
import pytest

from services.strategy_fast_aggregation import aggregate_fast, build_fast_market_bundle
from services.strategy_simulator_static_data import _aggregate


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h", "4h"])
@pytest.mark.parametrize("unit", ["us", "ns"])
@pytest.mark.parametrize("shape", ["complete", "gaps", "off_grid", "unordered", "empty"])
def test_exact_static_parity_with_gaps_boundaries_and_index_units(timeframe, unit, shape):
    index = pd.date_range("2024-01-01T00:05Z", periods=159, freq="5min").as_unit(unit)
    rng = np.random.default_rng(728)
    values = rng.normal(size=(len(index), 5)) * [1, 1, 1, 1, 1e8]
    frame = pd.DataFrame(values, index=index, columns=["Open", "High", "Low", "Close", "Volume"])
    if shape == "gaps":
        frame = frame.drop(frame.index[[20, 43, 95]])
    elif shape == "off_grid":
        # Extra off-grid rows must not replace a missing expected timestamp.
        extra = frame.iloc[[20, 43]].copy()
        extra.index += pd.Timedelta(minutes=1)
        frame = pd.concat([frame.drop(frame.index[43]), extra]).sort_index()
    elif shape == "unordered":
        frame = frame.iloc[::-1]
    elif shape == "empty":
        frame = frame.iloc[:0]
    for end in ["2024-01-01T12:00Z", "2024-01-01T12:03Z", "2024-01-02T00:00Z"]:
        pd.testing.assert_frame_equal(aggregate_fast(frame, timeframe, end), _aggregate(frame, timeframe, end), check_exact=True)


def test_only_requested_frames_and_base_frame_reused():
    index = pd.date_range("2024-01-01", periods=60, freq="5min", tz="UTC")
    frame = pd.DataFrame(np.ones((60, 5)), index=index, columns=["Open", "High", "Low", "Close", "Volume"])
    bundle = build_fast_market_bundle(frame, ["5m", "1h", "1h"], "2024-01-02")
    assert list(bundle) == ["5m", "1h"]
    assert bundle["5m"] is frame
    pd.testing.assert_frame_equal(bundle["1h"], _aggregate(frame, "1h", "2024-01-02"), check_exact=True)


@pytest.mark.parametrize("timeframe", ["15m", "1h", "4h"])
@pytest.mark.parametrize("shape", ["complete", "gaps", "off_grid"])
def test_chunked_aggregation_exact_boundary_parity(timeframe, shape):
    index = pd.date_range("2024-01-01T00:05Z", periods=22000, freq="5min").as_unit("us")
    values = np.random.default_rng(401).normal(size=(len(index), 5)) * [1, 1, 1, 1, 1e8]
    frame = pd.DataFrame(values, index=index, columns=["Open", "High", "Low", "Close", "Volume"])
    if shape == "gaps":
        frame = frame.drop(frame.index[[8927, 8928, 13000]])
    elif shape == "off_grid":
        extra = frame.iloc[[8927, 8928]].copy()
        extra.index += pd.Timedelta(minutes=1)
        frame = pd.concat([frame, extra]).sort_index()
    for end in ["2024-02-01T00:03Z", "2024-04-01T00:00Z"]:
        pd.testing.assert_frame_equal(aggregate_fast(frame, timeframe, end), _aggregate(frame, timeframe, end), check_exact=True)
