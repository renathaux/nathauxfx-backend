from __future__ import annotations

import copy

import pandas as pd
import pytest

from services.strategy_engine.evaluator import EvaluationResult, EvaluationState
from services.strategy_engine.types import CandleFacts
from services import strategy_simulator as simulator


T0 = pd.Timestamp("2026-09-17T10:00:00Z")
T1 = pd.Timestamp("2026-09-17T10:05:00Z")
T2 = pd.Timestamp("2026-09-17T10:10:00Z")
T3 = pd.Timestamp("2026-09-17T10:15:00Z")


class FakeTimeline:
    def __init__(self, rows):
        self._candles = {}
        for timestamp, values in rows.items():
            open_, high, low, close = values
            full_range = max(high - low, 1e-12)
            self._candles[timestamp] = CandleFacts(
                timestamp,
                open_,
                high,
                low,
                close,
                abs(close - open_) / full_range * 100.0,
            )
        self.trading_timestamps = tuple(sorted(self._candles))

    def candle(self, timestamp):
        return self._candles.get(pd.Timestamp(timestamp))


def strategy(*, tp1=False, risk=None):
    return {
        "schema_version": 1,
        "symbols": ["XAUUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {"trigger": "BOS_CHOCH", "break_validation": [], "minimum_body_percent": None, "minimum_distance_pips": None},
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {"method": "FIXED_DISTANCE", "buffer_pips": None, "fixed_distance": 10.0},
        "tp1": {
            "enabled": tp1,
            "target_r": 1.0 if tp1 else None,
            "close_percent": 50.0 if tp1 else None,
            "protection_r": 0.0 if tp1 else None,
        },
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": risk or {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def _market_bundle(rows):
    index = sorted(rows)
    frame = pd.DataFrame(
        [
            {"Open": rows[t][0], "High": rows[t][1], "Low": rows[t][2], "Close": rows[t][3], "Volume": 0.0}
            for t in index
        ],
        index=pd.DatetimeIndex(index),
    )
    return {"5m": frame, "15m": pd.DataFrame(), "1h": pd.DataFrame(), "4h": pd.DataFrame()}


def _install_fake_engine(monkeypatch, rows, signal_times, *, tp1=False):
    timeline = FakeTimeline(rows)
    monkeypatch.setattr(simulator, "build_market_facts", lambda *args, **kwargs: timeline)

    def fake_evaluate(definition, _timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        timestamp = pd.Timestamp(timestamp)
        if timestamp not in signal_times:
            return EvaluationResult(
                signal="WAIT",
                steps={},
                setup_id=None,
                entry=None,
                sl=None,
                tp1=None,
                tp2=None,
                risk_budget=None,
                next_state=EvaluationState("READY", None),
            )
        risk = copy.deepcopy(risk_override or definition["risk"])
        dollars = account_balance * float(risk["value"]) / 100.0 if risk["method"] == "PERCENT_BALANCE" else float(risk["value"])
        return EvaluationResult(
            signal="BUY",
            steps={},
            setup_id=f"setup-{timestamp.isoformat()}",
            entry=100.0,
            sl=90.0,
            tp1=110.0 if tp1 else None,
            tp2=120.0,
            risk_budget={"method": risk["method"], "value": float(risk["value"]), "dollars": dollars},
            next_state=EvaluationState("READY", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)
    return timeline


def test_full_stop_loss_is_minus_one_r(monkeypatch):
    rows = {T0: (99, 101, 98, 100), T1: (100, 101, 89, 90)}
    _install_fake_engine(monkeypatch, rows, {T0})

    result = simulator.run_simulation(strategy(), _market_bundle(rows), "XAUUSD", 10000.0)

    assert result["metrics"]["ending_balance"] == pytest.approx(9900.0)
    assert result["trades"][0]["r"] == pytest.approx(-1.0)
    assert result["trades"][0]["outcome"] == "LOSS"


def test_tp2_without_tp1_realizes_configured_r(monkeypatch):
    rows = {T0: (99, 101, 98, 100), T1: (100, 121, 99, 120)}
    _install_fake_engine(monkeypatch, rows, {T0})

    result = simulator.run_simulation(strategy(), _market_bundle(rows), "XAUUSD", 10000.0)

    assert result["trades"][0]["r"] == pytest.approx(2.0)
    assert result["trades"][0]["dollars"] == pytest.approx(200.0)
    assert result["metrics"]["ending_balance"] == pytest.approx(10200.0)


def test_tp1_partial_then_tp2_realizes_weighted_r(monkeypatch):
    rows = {
        T0: (99, 101, 98, 100),
        T1: (100, 111, 101, 109),
        T2: (109, 121, 105, 120),
    }
    _install_fake_engine(monkeypatch, rows, {T0}, tp1=True)

    result = simulator.run_simulation(strategy(tp1=True), _market_bundle(rows), "XAUUSD", 10000.0)

    trade = result["trades"][0]
    assert trade["tp1_hit"] is True
    assert trade["r"] == pytest.approx(1.5)
    assert trade["dollars"] == pytest.approx(150.0)


def test_protected_stop_arms_only_after_tp1(monkeypatch):
    rows = {
        T0: (99, 101, 98, 100),
        T1: (100, 111, 101, 109),
        T2: (109, 109, 99, 100),
    }
    _install_fake_engine(monkeypatch, rows, {T0}, tp1=True)

    result = simulator.run_simulation(strategy(tp1=True), _market_bundle(rows), "XAUUSD", 10000.0)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "PROTECTED_SL"
    assert trade["r"] == pytest.approx(0.5)
    assert result["metrics"]["ending_balance"] == pytest.approx(10050.0)


def test_pre_tp1_sl_and_tp1_same_candle_is_ambiguous_and_excluded(monkeypatch):
    rows = {T0: (99, 101, 98, 100), T1: (100, 111, 89, 105)}
    _install_fake_engine(monkeypatch, rows, {T0}, tp1=True)

    result = simulator.run_simulation(strategy(tp1=True), _market_bundle(rows), "XAUUSD", 10000.0)

    trade = result["trades"][0]
    assert trade["outcome"] == "AMBIGUOUS_INTRABAR"
    assert trade["r"] is None
    assert result["metrics"]["total_resolved_trades"] == 0
    assert result["metrics"]["ending_balance"] == pytest.approx(10000.0)


def test_fast_and_replay_have_identical_trade_results(monkeypatch):
    rows = {T0: (99, 101, 98, 100), T1: (100, 121, 99, 120)}
    _install_fake_engine(monkeypatch, rows, {T0})
    fast = simulator.run_simulation(strategy(), _market_bundle(rows), "XAUUSD", 10000.0, include_replay=False)
    replay = simulator.run_simulation(strategy(), _market_bundle(rows), "XAUUSD", 10000.0, include_replay=True)

    assert fast["trades"] == replay["trades"]
    assert fast["metrics"] == replay["metrics"]
    assert "replay" not in fast
    assert len(replay["replay"]) == 2


def test_percent_risk_compounds_from_virtual_balance(monkeypatch):
    rows = {
        T0: (99, 101, 98, 100),
        T1: (100, 121, 99, 120),
        T2: (99, 101, 98, 100),
        T3: (100, 121, 99, 120),
    }
    _install_fake_engine(monkeypatch, rows, {T0, T2})

    result = simulator.run_simulation(strategy(), _market_bundle(rows), "XAUUSD", 10000.0)

    assert [trade["risk_dollars"] for trade in result["trades"]] == pytest.approx([100.0, 102.0])
    assert result["metrics"]["ending_balance"] == pytest.approx(10404.0)


def test_fixed_dollar_risk_stays_constant(monkeypatch):
    rows = {
        T0: (99, 101, 98, 100),
        T1: (100, 121, 99, 120),
        T2: (99, 101, 98, 100),
        T3: (100, 121, 99, 120),
    }
    _install_fake_engine(monkeypatch, rows, {T0, T2})
    fixed = strategy(risk={"method": "FIXED_DOLLARS", "value": 100.0})

    result = simulator.run_simulation(fixed, _market_bundle(rows), "XAUUSD", 10000.0)

    assert [trade["risk_dollars"] for trade in result["trades"]] == pytest.approx([100.0, 100.0])
    assert result["metrics"]["ending_balance"] == pytest.approx(10400.0)


def test_risk_override_changes_simulation_only(monkeypatch):
    rows = {T0: (99, 101, 98, 100), T1: (100, 121, 99, 120)}
    definition = strategy()
    original = copy.deepcopy(definition)
    _install_fake_engine(monkeypatch, rows, {T0})

    result = simulator.run_simulation(
        definition,
        _market_bundle(rows),
        "XAUUSD",
        10000.0,
        risk_override={"method": "FIXED_DOLLARS", "value": 50.0},
    )

    assert result["trades"][0]["risk_dollars"] == pytest.approx(50.0)
    assert result["metrics"]["ending_balance"] == pytest.approx(10100.0)
    assert definition == original
