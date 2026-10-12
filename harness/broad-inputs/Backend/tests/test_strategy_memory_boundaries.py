"""Independent global-history oracle and explicit lifecycle boundary scenarios."""
import json

import numpy as np
import pandas as pd
import pytest

from services.strategy_engine.market_facts import MarketFactsTimeline, build_market_facts
from services.strategy_engine.types import CandleFacts, StructureEventFacts
from services.strategy_fast_results import aggregate_results
from services.strategy_simulator import run_simulation
from services.strategy_simulator_static_data import _aggregate
from services.strategy_studio_schema import normalize_definition


def definition():
    return {
        "schema_version": 1, "symbols": ["EURUSD"], "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {"trigger": "BOS_CHOCH", "break_validation": [],
                      "minimum_body_percent": None, "minimum_distance_pips": None},
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0, "fixed_distance": None},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 2},
        "risk": {"method": "PERCENT_BALANCE", "value": 1},
    }


@pytest.fixture
def history_files(tmp_path):
    # Source begins inside a bucket, later than requested warmup; missing and
    # off-grid bars stress completeness independently of FAST aggregation.
    stamps = pd.date_range("2024-11-28T00:10Z", "2025-02-02T00:05Z", freq="5min")
    stamps = stamps.difference(pd.DatetimeIndex([
        "2024-12-31T23:55Z", "2025-01-01T00:10Z", "2025-01-31T23:50Z",
    ]))
    stamps = stamps.union(pd.DatetimeIndex(["2024-12-31T23:57Z", "2025-01-01T00:01Z"]))
    rng = np.random.default_rng(928)
    prices = 1.1 + rng.normal(0, .0003, len(stamps)).cumsum()
    frame = pd.DataFrame({"Open": prices, "High": prices + .0004,
                          "Low": prices - .0003, "Close": prices + .0001,
                          "Volume": rng.uniform(0, 100, len(stamps))}, index=stamps)
    directory = tmp_path / "history"
    (directory / "EURUSD").mkdir(parents=True)
    symbol = {"months": []}
    for month, monthly in frame.groupby(frame.index.strftime("%Y-%m")):
        symbol["months"].append(month)
        symbol[month] = {"first_timestamp": monthly.index[0].isoformat(), "count": len(monthly)}
        rows = [dict(timestamp=row.Index.isoformat(), open=row.Open, high=row.High,
                     low=row.Low, close=row.Close, volume=row.Volume)
                for row in monthly.itertuples()]
        (directory / "EURUSD" / f"{month}.json").write_text(json.dumps(
            {"symbol": "EURUSD", "timeframe": "5m", "candles": rows}))
    (directory / "manifest.json").write_text(json.dumps(
        {"version": 1, "base_timeframe": "5m", "symbols": {"EURUSD": symbol}}))
    return directory, frame


def assert_same_facts(actual, expected):
    stamps = expected.timestamps()
    assert actual.timestamps() == stamps
    for index, stamp in enumerate(stamps):
        assert actual.candle(stamp) == expected.candle(stamp)
        assert actual.structure_candle(stamp) == expected.structure_candle(stamp)
        assert actual.structure_event(stamp) == expected.structure_event(stamp)
        assert actual.trend(stamp) == expected.trend(stamp)
        assert actual.previous_timestamp(stamp) == expected.previous_timestamp(stamp)
        assert actual.next_timestamp(stamp) == expected.next_timestamp(stamp)
        if index % 101 == 0:
            for direction in ("BUY", "SELL"):
                assert actual.opposite_swing(stamp, direction, 1.1) == expected.opposite_swing(stamp, direction, 1.1)


@pytest.mark.parametrize("trading,structure,trend", [("5m", "15m", "1h"), ("15m", "1h", "4h")])
def test_worker_windows_match_global_noncompact_oracle(history_files, tmp_path, monkeypatch,
                                                      trading, structure, trend):
    from services import strategy_fast_worker as worker

    directory, raw = history_files
    value = definition()
    value.update(trading_timeframe=trading, structure_timeframe=structure)
    value["trend"] = {"timeframe": trend, "methods": ["EMA_200", "BOS_CHOCH"]}
    start, end = pd.Timestamp("2024-12-01T00:05Z"), pd.Timestamp("2025-02-02T00:07Z")
    warmup = worker.warmup_days(normalize_definition(value))
    durations = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
    # No FAST aggregation, slicing or swing stores in this reference.
    global_bundle = {tf: raw if tf == "5m" else _aggregate(raw, tf, end)
                     for tf in {trading, structure, trend}}
    original_load, original_run = worker.load_fast_history, worker.run_simulation
    loaded_ranges, reference_results = [], []
    reference_continuation = None

    def load(symbol, first, last, days, **kwargs):
        assert last - first <= pd.Timedelta(days=31)
        assert days == warmup
        loaded_ranges.append((first, last))
        return original_load(symbol, first, last, days, **kwargs)

    def simulate(*args, **kwargs):
        nonlocal reference_continuation
        first, last = kwargs["evaluation_start"], kwargs["evaluation_end"]
        history_start = first - pd.Timedelta(days=warmup)
        sliced = {}
        for tf, frame in global_bundle.items():
            keep = frame.index >= history_start
            keep &= frame.index < last if tf == "5m" else frame.index + pd.Timedelta(minutes=durations[tf]) <= last
            sliced[tf] = frame.loc[keep]
        reference = build_market_facts(sliced, "EURUSD", trading, trend, structure)
        assert_same_facts(kwargs["timeline"], reference)
        expected_kwargs = dict(kwargs, timeline=reference, continuation=reference_continuation)
        expected = original_run(*args, **expected_kwargs)
        reference_continuation = expected["continuation"]
        reference_results.append(expected)
        actual = original_run(*args, **kwargs)
        assert actual == expected
        return actual

    monkeypatch.setattr(worker, "load_fast_history", load)
    monkeypatch.setattr(worker, "run_simulation", simulate)
    actual = worker.execute({"strategy_definition": value, "symbol": "EURUSD",
                             "start": start.isoformat(), "end": end.isoformat(),
                             "starting_balance": 10000, "strategy_id": "boundary",
                             "account_scope": "test"}, history_dir=directory, scratch_parent=tmp_path)
    assert loaded_ranges == [(start, pd.Timestamp("2025-01-01T00:05Z")),
                             (pd.Timestamp("2025-01-01T00:05Z"), pd.Timestamp("2025-02-01T00:05Z")),
                             (pd.Timestamp("2025-02-01T00:05Z"), end)]
    expected = aggregate_results(reference_results, 10000)
    for key in ("trades", "metrics", "diagnostics", "continuation", "equity_curve"):
        assert actual[key] == expected[key]
    assert actual["diagnostics"]["candles_analyzed"] > 1000


def timeline(rows, events=()):
    candles = {}
    for stamp, open_, high, low, close in rows:
        stamp = pd.Timestamp(stamp)
        candles[stamp] = CandleFacts(stamp, open_, high, low, close,
                                    abs(close - open_) / (high - low) * 100)
    return MarketFactsTimeline(candles=candles, events={e.timestamp: e for e in events},
                               trends={}, timestamps=candles, trading_swings=[])


def test_partial_tp1_and_protected_stop_survive_year_boundary():
    value = definition()
    value["tp1"] = {"enabled": True, "target_r": 1, "close_percent": 80, "protection_r": .4}
    before, hit, after = map(pd.Timestamp, ["2024-12-31T23:50Z", "2024-12-31T23:55Z", "2025-01-01T00:00Z"])
    rows = [(before, 1.099, 1.1015, 1.098, 1.1),
            (hit, 1.1, 1.111, 1.099, 1.11),
            (after, 1.11, 1.112, 1.103, 1.105)]
    event = StructureEventFacts(before, "BUY", "BOS", 1.099, 1.09, 1.1)
    whole = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(rows, [event]))
    first = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(rows[:2], [event]),
                           evaluation_end=after, finalize_open_trade=False)
    active = first["continuation"]["active_trade"]
    assert first["trades"] == []
    assert active["tp1_hit"] is True
    assert active["remaining_fraction"] == pytest.approx(.2)
    assert active["protected_sl"] == pytest.approx(1.104)
    assert active["realized_r"] == pytest.approx(.8)
    second = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(rows, [event]),
                            evaluation_start=after, continuation=first["continuation"])
    assert second["trades"] == whole["trades"]
    assert second["continuation"] == whole["continuation"]
    assert len(second["trades"]) == 1
    assert second["trades"][0]["r"] == pytest.approx(.88)
    assert second["continuation"]["balance"] == pytest.approx(10088)
    assert second["diagnostics"]["evaluations"] == 0  # No exit-then-reentry.


@pytest.mark.parametrize("remembered", [False, True])
def test_pending_confirmation_survives_month_boundary_with_real_evaluator(remembered):
    value = definition()
    value["confirmation"].update(rules=["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
                                  max_setup_age_bars=3)
    value["entry"] = {"method": "CONFIRMATION_CLOSE", "remember_bos_on_confirmation_failure": remembered}
    t0, t1, t2, t3 = map(pd.Timestamp, ["2025-01-31T23:50Z", "2025-01-31T23:55Z",
                                       "2025-02-03T00:00Z", "2025-02-03T00:05Z"])
    event = StructureEventFacts(t0, "BUY", "BOS", 1.1, 1.095, 1.101)
    rows = [(t0, 1.099, 1.102, 1.098, 1.101)]
    if remembered:
        rows += [(t1, 1.101, 1.1015, 1.097, 1.098),
                 (t2, 1.098, 1.1, 1.0975, 1.099),
                 (t3, 1.099, 1.103, 1.0985, 1.102)]
    else:
        rows += [(t2, 1.101, 1.105, 1.1005, 1.104)]
    boundary = pd.Timestamp("2025-02-01T00:00Z")
    first_rows = [row for row in rows if row[0] < boundary]
    first = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(first_rows, [event]),
                           evaluation_end=boundary, finalize_open_trade=False)
    pending = first["continuation"]["pending_setup"]
    assert pending["event_timestamp"] == t0.isoformat()
    assert pending["age_bars"] == (1 if remembered else 0)
    if remembered:
        assert pending["remember_bos"] and pending["remember_rearmed"]
        assert pending["broken_level"] == 1.1
    # Warmup includes the original event, but must not evaluate it again.
    second = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(rows, [event]),
                            evaluation_start=boundary, continuation=first["continuation"])
    whole = run_simulation(value, {}, "EURUSD", 10000, timeline=timeline(rows, [event]))
    assert second["trades"] == whole["trades"]
    assert second["continuation"] == whole["continuation"]
    assert second["diagnostics"]["signals_emitted"] == 1
    assert second["trades"][0]["entry_time"] == (t3 if remembered else t2).isoformat()
    assert second["trades"][0]["entry"] == (1.102 if remembered else 1.104)
    assert second["continuation"]["ordinal"] == 1
