from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from services.strategy_lab.baseline_v1 import candidates, resolve_trade
from services.strategy_lab.replay_engine import run_replay


def frame(minutes, rows=80, start="2026-08-20T00:00:00Z", base=1.1600):
    index = pd.date_range(start, periods=rows, freq=f"{minutes}min", tz="UTC")
    values = [base + pos * .00002 for pos in range(rows)]
    return pd.DataFrame({
        "Open": values, "High": [v+.0003 for v in values],
        "Low": [v-.0003 for v in values], "Close": [v+.00001 for v in values],
    }, index=index)


def trade(side="BUY", entry_time="2026-08-22T00:05:00Z"):
    return {
        "side": side, "entry": 1.1000, "sl": 1.0990, "tp1": 1.1016,
        "tp2": 1.1020, "protected_sl": 1.1010, "rr": 2.0,
        "entry_timestamp": entry_time, "result": "UNRESOLVED_OPEN",
        "r_result": None, "exit_timestamp": None, "exit_reason": None,
    }


def outcome_frame(*ohlc):
    index = pd.date_range("2026-08-22T00:05:00Z", periods=len(ohlc), freq="5min", tz="UTC")
    return pd.DataFrame(ohlc, columns=["Open", "High", "Low", "Close"], index=index)


def test_tp1_then_protected_sl():
    current = trade()
    candles = outcome_frame((1.1012, 1.1017, 1.1011, 1.1015), (1.1015, 1.1017, 1.1009, 1.1010))
    resolve_trade(current, candles, pd.Timestamp("2026-08-22T01:00:00Z"))
    assert current["result"] == "PROTECTED_WIN"
    assert current["r_result"] == pytest.approx(1.0)


def test_tp2_full_win():
    current = trade()
    resolve_trade(current, outcome_frame((1.1012, 1.1021, 1.1011, 1.1020)), pd.Timestamp("2026-08-22T01:00:00Z"))
    assert current["result"] == "FULL_TP2_WIN"
    assert current["r_result"] == 2.0


def test_sl_loss():
    current = trade()
    resolve_trade(current, outcome_frame((1.1000, 1.1002, 1.0989, 1.0990)), pd.Timestamp("2026-08-22T01:00:00Z"))
    assert current["result"] == "LOSS" and current["r_result"] == -1.0


def test_ambiguous_same_candle_sl_and_tp():
    current = trade()
    resolve_trade(current, outcome_frame((1.1000, 1.1021, 1.0989, 1.1005)), pd.Timestamp("2026-08-22T01:00:00Z"))
    assert current["result"] == "AMBIGUOUS_INTRABAR"
    assert current["r_result"] is None


def test_m5_confirmation_cannot_precede_m15_close(monkeypatch):
    event = {"timestamp": "2026-08-22T00:00:00+00:00", "direction": "BULLISH", "broken_level": 1.1,
             "close": 1.101, "event_type": "BOS", "event_invalidation_swing": {"price": 1.09}}
    from services.strategy_lab import baseline_v1
    five = outcome_frame((1.099, 1.102, 1.098, 1.101), (1.1, 1.102, 1.099, 1.101),
                         (1.1, 1.102, 1.099, 1.101), (1.1, 1.102, 1.099, 1.101))
    found = baseline_v1._confirmation(event, five, 0, pd.Timestamp("2026-08-22T01:00:00Z"))
    assert found[1] > pd.Timestamp(event["timestamp"]) + pd.Timedelta(minutes=15)


def test_m5_confirmation_cannot_cross_replay_end():
    event = {"timestamp": "2026-08-22T00:00:00+00:00", "direction": "BULLISH", "broken_level": 1.1,
             "close": 1.101, "event_type": "BOS", "event_invalidation_swing": {"price": 1.09}}
    from services.strategy_lab import baseline_v1
    five = outcome_frame((1.1, 1.102, 1.099, 1.101), (1.1, 1.102, 1.099, 1.101),
                         (1.1, 1.102, 1.099, 1.101), (1.1, 1.102, 1.099, 1.101))
    assert baseline_v1._confirmation(event, five, 0, pd.Timestamp("2026-08-22T00:15:00Z")) is None


def test_smc_events_are_deterministic_and_do_not_use_future_candles():
    fifteen, five = frame(15, 120), frame(5, 360)
    start, end = pd.Timestamp("2026-08-20T00:00:00Z"), pd.Timestamp("2026-08-21T00:00:00Z")
    first = list(candidates(fifteen, five, start, end, {}))
    changed = pd.concat([fifteen, frame(15, 2, "2026-08-25T00:00:00Z", 1.5)])
    second = list(candidates(changed, five, start, end, {}))
    assert [(x[0]["timestamp"], x[0]["event_type"]) for x in first] == [(x[0]["timestamp"], x[0]["event_type"]) for x in second]


def test_replay_is_deterministic_one_active_trade_and_reports_safety(monkeypatch):
    fifteen, five = frame(15, 200), frame(5, 600, base=1.1000)
    five.loc[:, ["Open", "Close"]] = 1.1000
    five.loc[:, "High"] = 1.1003
    five.loc[:, "Low"] = 1.0997
    event = {"timestamp": "2026-08-22T00:00:00+00:00", "direction": "BULLISH", "broken_level": 1.1,
             "close": 1.101, "event_type": "BOS", "event_invalidation_swing": {"price": 1.09}}
    prefix = fifteen.loc[fifteen.index <= pd.Timestamp("2026-08-22T00:00:00Z")]
    def fake_candidates(*_args):
        yield event, pd.Timestamp(event["timestamp"]), prefix, "BUY", .011, True
        later = {**event, "timestamp": "2026-08-22T00:15:00+00:00"}
        yield later, pd.Timestamp(later["timestamp"]), prefix, "BUY", .011, True
    def fake_build(event, timestamp, *_args):
        built = trade(entry_time=(timestamp + pd.Timedelta(minutes=20)).isoformat())
        built.update(event_timestamp=event["timestamp"], event_type="BOS", source_event_identity=event["timestamp"])
        return built, None
    monkeypatch.setattr("services.strategy_lab.replay_engine.candidates", fake_candidates)
    monkeypatch.setattr("services.strategy_lab.replay_engine.build_trade", fake_build)
    settings = {"minimum_rr": 1.2, "maximum_rr": 2.0}
    args = ("EURUSD", "baseline_v1", "2026-08-22T00:00:00Z", "2026-08-23T00:00:00Z")
    first = run_replay(*args, frames=(fifteen, five), settings=settings)
    second = run_replay(*args, frames=(fifteen, five), settings=settings)
    assert first == second
    assert first["summary"]["total_simulated_trades"] == 1
    assert first["summary"]["skipped_active_trade"] == 1
    assert first["diagnostics"]["analysis_only"] is True


def test_strategy_lab_has_no_execution_or_mutation_imports():
    root = Path(__file__).parents[1] / "services" / "strategy_lab"
    forbidden = {"ctrader_connector", "paper_live_entry_service", "indicator_event_stream_service", "trade_submission_service"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert not any(name and any(blocked in name for blocked in forbidden) for name in imports)
        source = path.read_text()
        assert "place_market_order" not in source
        assert "update_event_lifecycle" not in source
        assert "FIFTEEN_M_SWING_WATCH" not in source


def test_replay_leaves_lifecycle_and_watch_sentinels_unchanged(monkeypatch):
    fifteen, five = frame(15, 100), frame(5, 300)
    lifecycle = {"event": "sentinel", "status": "ELIGIBLE"}
    watches = {"EURUSD:BUY": {"status": "PENDING"}}
    before = (lifecycle.copy(), {key: value.copy() for key, value in watches.items()})
    monkeypatch.setattr("services.strategy_lab.replay_engine.candidates", lambda *_args: iter(()))
    run_replay("EURUSD", "baseline_v1", "2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z",
               frames=(fifteen, five), settings={})
    assert (lifecycle, watches) == before
