from __future__ import annotations

import pandas as pd
import pytest

from services.strategy_lab import v2_m5_quality
from services.strategy_lab.replay_engine import run_replay


def _event():
    return {
        "timestamp": "2026-08-22T00:00:00+00:00",
        "direction": "BULLISH",
        "broken_level": 1.1000,
        "close": 1.1005,
        "event_type": "CHOCH",
        "event_invalidation_swing": {
            "type": "LOW",
            "price": 1.0980,
            "swing_time": "2026-08-21T23:45:00+00:00",
            "confirmation_time": "2026-08-22T00:00:00+00:00",
        },
    }


def _m5(*rows):
    index = pd.date_range("2026-08-22T00:15:00Z", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=index)


def _flat_frame(minutes, rows=100, start="2026-08-20T00:00:00Z"):
    index = pd.date_range(start, periods=rows, freq=f"{minutes}min", tz="UTC")
    values = [1.1000 + i * 0.00001 for i in range(rows)]
    return pd.DataFrame(
        {
            "Open": values,
            "High": [v + 0.0003 for v in values],
            "Low": [v - 0.0003 for v in values],
            "Close": [v + 0.00001 for v in values],
        },
        index=index,
    )


def test_v2_quality_confirmation_waits_for_later_strong_candle():
    candles = _m5(
        (1.10010, 1.10050, 1.10000, 1.10020),  # baseline-valid, weak body
        (1.10020, 1.10090, 1.10010, 1.10080),  # strong body, small close-side wick
    )
    result = v2_m5_quality._quality_confirmation(
        _event(), candles, 0.00010, pd.Timestamp("2026-08-22T01:00:00Z")
    )
    assert result["found"] is True
    assert result["candle_time"] == pd.Timestamp("2026-08-22T00:20:00Z")
    assert result["entry_time"] == pd.Timestamp("2026-08-22T00:25:00Z")
    assert result["body_ratio"] >= v2_m5_quality.MIN_BODY_RATIO
    assert result["close_side_wick_ratio"] <= v2_m5_quality.MAX_CLOSE_SIDE_WICK_RATIO


def test_v2_quality_rejects_only_weak_baseline_confirmation():
    candles = _m5((1.10010, 1.10050, 1.10000, 1.10020))
    result = v2_m5_quality._quality_confirmation(
        _event(), candles, 0.00010, pd.Timestamp("2026-08-22T01:00:00Z")
    )
    assert result["found"] is False
    assert result["baseline_confirmation_seen"] is True
    assert result["last_rejected_quality"]["body_ratio"] < v2_m5_quality.MIN_BODY_RATIO


def test_v2_candle_quality_uses_close_side_wick():
    buy = pd.Series({"Open": 1.1000, "High": 1.1010, "Low": 1.0999, "Close": 1.1008})
    sell = pd.Series({"Open": 1.1008, "High": 1.1009, "Low": 1.0998, "Close": 1.1000})
    buy_quality = v2_m5_quality._candle_quality(buy, "BUY")
    sell_quality = v2_m5_quality._candle_quality(sell, "SELL")
    assert buy_quality["direction_ok"] is True
    assert sell_quality["direction_ok"] is True
    assert buy_quality["close_side_wick_ratio"] == pytest.approx((1.1010 - 1.1008) / (1.1010 - 1.0999))
    assert sell_quality["close_side_wick_ratio"] == pytest.approx((1.1000 - 1.0998) / (1.1009 - 1.0998))


def test_v2_replay_reports_exact_settings_used(monkeypatch):
    fifteen = _flat_frame(15, 120)
    five = _flat_frame(5, 360)
    monkeypatch.setattr(v2_m5_quality, "candidates", lambda *_args: iter(()))
    result = run_replay(
        "EURUSD",
        "v2_m5_quality",
        "2026-08-20T00:00:00Z",
        "2026-08-21T00:00:00Z",
        frames=(fifteen, five),
        settings={"minimum_rr": 1.5, "maximum_rr": 2.0},
    )
    assert result["strategy_version"] == "v2_m5_quality"
    assert result["diagnostics"]["settings_source"] == "explicit_replay_settings"
    assert result["diagnostics"]["settings_used"]["minimum_rr"] == 1.5
    assert result["diagnostics"]["settings_used"]["maximum_rr"] == 2.0
    assert result["diagnostics"]["strategy_parameters"] == {
        "m5_minimum_body_ratio": 0.55,
        "m5_maximum_close_side_wick_ratio": 0.25,
    }
