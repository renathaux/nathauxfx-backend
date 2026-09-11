from __future__ import annotations

import pandas as pd

from services.strategy_lab import replay_engine, v3_m5_two_close


def _frame(rows, start="2026-08-22T00:00:00Z"):
    index = pd.date_range(start, periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=index)


def _event(direction="BULLISH", broken_level=1.1000):
    return {
        "timestamp": "2026-08-22T00:00:00+00:00",
        "direction": direction,
        "event_type": "BOS",
        "broken_level": broken_level,
        "close": 1.1002 if direction == "BULLISH" else 1.0998,
        "event_invalidation_swing": {
            "type": "LOW" if direction == "BULLISH" else "HIGH",
            "price": 1.0980 if direction == "BULLISH" else 1.1020,
            "swing_time": "2026-08-21T23:45:00+00:00",
            "confirmation_time": "2026-08-22T00:00:00+00:00",
        },
    }


def test_second_5m_must_be_same_direction_and_stay_beyond_bos_level():
    candles = _frame([
        (1.0998, 1.1003, 1.0997, 1.1002),
        (1.1001, 1.1006, 1.1000, 1.1005),
    ])
    result = v3_m5_two_close._second_candle(
        _event(), candles, pd.Timestamp("2026-08-22T00:20:00Z")
    )
    assert result["accepted"] is True
    assert result["same_direction"] is True
    assert result["stays_beyond_bos_level"] is True
    assert result["entry_time"] == pd.Timestamp("2026-08-22T00:10:00Z")
    assert result["entry"] == 1.1005


def test_second_5m_rejects_reentry_below_broken_level_even_if_green():
    candles = _frame([
        (1.0998, 1.1003, 1.0997, 1.1002),
        (1.0997, 1.1000, 1.0996, 1.0999),
    ])
    result = v3_m5_two_close._second_candle(
        _event(), candles, pd.Timestamp("2026-08-22T00:20:00Z")
    )
    assert result["same_direction"] is True
    assert result["stays_beyond_bos_level"] is False
    assert result["accepted"] is False


def test_pure_5m_replay_does_not_load_15m(monkeypatch):
    five = _frame(
        [(1.1000, 1.1003, 1.0997, 1.1001)] * 40,
        start="2026-08-21T21:00:00Z",
    )
    calls = []

    def fake_load(symbol, timeframe, start, end, **kwargs):
        calls.append(timeframe)
        if timeframe != "5m":
            raise AssertionError("pure 5m strategy must not load 15m candles")
        return five

    monkeypatch.setattr(replay_engine, "load_candles", fake_load)
    monkeypatch.setattr(v3_m5_two_close, "candidates", lambda *_args: iter(()))
    result = replay_engine.run_replay(
        "EURUSD",
        "v3_m5_two_close",
        "2026-08-22T00:00:00Z",
        "2026-08-22T00:30:00Z",
        settings={"minimum_rr": 1.5, "maximum_rr": 2.0},
    )
    assert calls == ["5m"]
    assert result["candle_counts"]["15m"] == 0
    assert result["diagnostics"]["strategy_parameters"]["uses_15m"] is False
    assert "no 15m structure, EMA, consolidation, or confirmation dependency" in result["diagnostics"]["parity_rules"]


def test_strategy_registry_exposes_v3():
    candidate_fn, evaluate_fn, resolve_fn = replay_engine._strategy_engine("v3_m5_two_close")
    assert callable(candidate_fn)
    assert callable(evaluate_fn)
    assert callable(resolve_fn)
    assert replay_engine._strategy_parameters("v3_m5_two_close")["entry_at"] == "second_5m_close"
