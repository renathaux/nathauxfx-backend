import pandas as pd
import pytest

from services.strategy_engine.types import EvaluationResult, EvaluationState
from services.strategy_simulator import (
    VirtualTrade,
    resolve_virtual_trade,
    run_simulation,
    simulation_metrics,
)


def bar(timestamp, open_, high, low, close):
    return {
        "timestamp": pd.Timestamp(timestamp),
        "open": open_, "high": high, "low": low, "close": close,
    }


def trade(*, tp1=None, tp2=120.0, close_fraction=0.0, protection_r=0.0,
          protection_basis="SL_DISTANCE", protection_mode="FIXED",
          protection_trigger_method="CANDLE_CLOSE", protection_steps=None):
    return VirtualTrade(
        trade_id="trade_1",
        entry_time=pd.Timestamp("2026-09-17T10:00:00Z"),
        entry=100.0,
        sl=90.0,
        tp1=tp1,
        tp2=tp2,
        side="BUY",
        risk_dollars=100.0,
        tp1_close_fraction=close_fraction,
        protection_r=protection_r,
        protection_basis=protection_basis,
        protection_mode=protection_mode,
        protection_trigger_method=protection_trigger_method,
        protection_steps=list(protection_steps or []),
    )


def test_full_sl_is_minus_one_r():
    result = resolve_virtual_trade(trade(), bar("2026-09-17T10:05:00Z", 100, 105, 89, 91))
    assert result["outcome"] == "SL"
    assert result["r"] == pytest.approx(-1.0)
    assert result["pnl_dollars"] == pytest.approx(-100.0)


def test_tp2_without_tp1_returns_final_r():
    result = resolve_virtual_trade(trade(), bar("2026-09-17T10:05:00Z", 100, 121, 99, 119))
    assert result["outcome"] == "TP2"
    assert result["r"] == pytest.approx(2.0)
    assert result["pnl_dollars"] == pytest.approx(200.0)


def test_tp1_partial_then_tp2_combines_realized_r():
    value = trade(tp1=110.0, close_fraction=0.8, protection_r=0.4)
    result = resolve_virtual_trade(value, bar("2026-09-17T10:05:00Z", 100, 121, 99, 119))
    assert result["outcome"] == "TP2"
    assert result["r"] == pytest.approx(1.2)
    assert result["pnl_dollars"] == pytest.approx(120.0)


def test_nearer_tp2_closes_full_trade_before_tp1():
    value = trade(tp1=170.0, tp2=101.0, close_fraction=0.4, protection_r=0.2)
    result = resolve_virtual_trade(value, bar("2026-09-17T10:05:00Z", 100, 101.2, 99.5, 101.0))
    assert result["outcome"] == "TP2"
    assert result["tp1_hit"] is False
    assert result["r"] == pytest.approx(0.1)
    assert result["pnl_dollars"] == pytest.approx(10.0)


def test_nearer_tp2_and_sl_same_candle_is_ambiguous():
    value = trade(tp1=170.0, tp2=101.0, close_fraction=0.4, protection_r=0.2)
    result = resolve_virtual_trade(value, bar("2026-09-17T10:05:00Z", 100, 101.2, 89.0, 100.0))
    assert result["outcome"] == "AMBIGUOUS_INTRABAR"
    assert result["resolved"] is False


def test_fixed_protection_can_use_tp2_distance():
    value = trade(
        tp1=114.0, tp2=120.0, close_fraction=0.4,
        protection_r=0.5, protection_basis="TP2_DISTANCE",
    )
    result = resolve_virtual_trade(
        value, bar("2026-09-17T10:05:00Z", 100, 115, 99.5, 114)
    )
    assert result is None
    assert value.tp1_hit is True
    assert value.protected_sl == pytest.approx(110.0)


def test_step_protection_follows_tp2_progress_after_tp1():
    steps = [
        {"trigger_percent": 70, "secure_percent": 50},
        {"trigger_percent": 80, "secure_percent": 60},
        {"trigger_percent": 90, "secure_percent": 70},
    ]
    value = trade(
        tp1=114.0, tp2=120.0, close_fraction=0.4,
        protection_mode="TP2_STEPS", protection_steps=steps,
    )

    first = resolve_virtual_trade(
        value, bar("2026-09-17T10:05:00Z", 100, 115, 99.5, 114)
    )
    assert first is None
    assert value.tp1_hit is True
    assert value.protected_sl == pytest.approx(110.0)

    second = resolve_virtual_trade(
        value, bar("2026-09-17T10:10:00Z", 114, 117, 111, 116)
    )
    assert second is None
    assert value.protected_sl == pytest.approx(112.0)

    third = resolve_virtual_trade(
        value, bar("2026-09-17T10:15:00Z", 116, 119, 113, 118)
    )
    assert third is None
    assert value.protected_sl == pytest.approx(114.0)

    closed = resolve_virtual_trade(
        value, bar("2026-09-17T10:20:00Z", 118, 118.5, 113.5, 114)
    )
    assert closed["outcome"] == "PROTECTED_SL"
    assert closed["exit_price"] == pytest.approx(114.0)
    assert closed["r"] == pytest.approx(1.4)


def test_price_touch_step_protection_arms_without_waiting_for_close():
    steps = [
        {"trigger_percent": 70, "secure_percent": 50},
        {"trigger_percent": 80, "secure_percent": 60},
        {"trigger_percent": 90, "secure_percent": 70},
    ]
    value = trade(
        tp1=114.0, tp2=120.0, close_fraction=0.1,
        protection_mode="TP2_STEPS",
        protection_trigger_method="PRICE_TOUCH",
        protection_steps=steps,
    )

    result = resolve_virtual_trade(
        value, bar("2026-09-17T10:05:00Z", 100, 115, 111, 112)
    )
    assert result is None
    assert value.tp1_hit is True
    assert value.protected_sl == pytest.approx(110.0)


def test_price_touch_new_protected_stop_same_candle_is_ambiguous():
    steps = [
        {"trigger_percent": 70, "secure_percent": 50},
        {"trigger_percent": 80, "secure_percent": 60},
        {"trigger_percent": 90, "secure_percent": 70},
    ]
    value = trade(
        tp1=114.0, tp2=120.0, close_fraction=0.1,
        protection_mode="TP2_STEPS",
        protection_trigger_method="PRICE_TOUCH",
        protection_steps=steps,
    )

    result = resolve_virtual_trade(
        value, bar("2026-09-17T10:05:00Z", 100, 115, 109.5, 112)
    )
    assert result["outcome"] == "AMBIGUOUS_INTRABAR"
    assert result["resolved"] is False


def test_price_touch_later_step_can_advance_on_favorable_extreme():
    steps = [
        {"trigger_percent": 70, "secure_percent": 50},
        {"trigger_percent": 80, "secure_percent": 60},
        {"trigger_percent": 90, "secure_percent": 70},
    ]
    value = trade(
        tp1=114.0, tp2=120.0, close_fraction=0.1,
        protection_mode="TP2_STEPS",
        protection_trigger_method="PRICE_TOUCH",
        protection_steps=steps,
    )
    first = resolve_virtual_trade(
        value, bar("2026-09-17T10:05:00Z", 100, 115, 111, 112)
    )
    assert first is None
    assert value.protected_sl == pytest.approx(110.0)

    second = resolve_virtual_trade(
        value, bar("2026-09-17T10:10:00Z", 112, 117, 113, 114)
    )
    assert second is None
    assert value.protected_sl == pytest.approx(112.0)


def test_pre_tp1_sl_and_tp1_same_candle_is_ambiguous():
    value = trade(tp1=110.0, close_fraction=0.8, protection_r=0.4)
    result = resolve_virtual_trade(value, bar("2026-09-17T10:05:00Z", 100, 111, 89, 100))
    assert result["outcome"] == "AMBIGUOUS_INTRABAR"
    assert result["resolved"] is False


def test_newly_armed_protected_sl_and_tp2_same_later_candle_is_ambiguous():
    value = trade(tp1=110.0, close_fraction=0.8, protection_r=0.4)
    first = resolve_virtual_trade(value, bar("2026-09-17T10:05:00Z", 100, 111, 99, 110))
    assert first is None
    assert value.tp1_hit is True
    assert value.protected_sl == pytest.approx(104.0)
    second = resolve_virtual_trade(value, bar("2026-09-17T10:10:00Z", 110, 121, 103, 118))
    assert second["outcome"] == "AMBIGUOUS_INTRABAR"
    assert second["resolved"] is False


def test_metrics_exclude_ambiguous_from_win_loss_counts():
    trades = [
        {"resolved": True, "r": 2.0, "pnl_dollars": 200.0, "outcome": "TP2"},
        {"resolved": True, "r": -1.0, "pnl_dollars": -100.0, "outcome": "SL"},
        {"resolved": False, "r": None, "pnl_dollars": 0.0, "outcome": "AMBIGUOUS_INTRABAR"},
    ]
    metrics = simulation_metrics(10000.0, trades)
    assert metrics["total_resolved_trades"] == 2
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1
    assert metrics["win_rate"] == pytest.approx(50.0)
    assert metrics["ending_balance"] == pytest.approx(10100.0)
    assert metrics["profit_factor"] == pytest.approx(2.0)


def _definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {"trigger": "BOS_CHOCH", "break_validation": [], "minimum_body_percent": None, "minimum_distance_pips": None},
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {"method": "FIXED_DISTANCE", "buffer_pips": None, "fixed_distance": 100000},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 2},
        "risk": {"method": "PERCENT_BALANCE", "value": 1},
    }


def _bundle():
    index = pd.date_range("2026-09-17T10:00:00Z", periods=4, freq="5min")
    frame = pd.DataFrame({
        "Open": [100, 100, 100, 100],
        "High": [101, 121, 101, 121],
        "Low": [99, 99, 99, 99],
        "Close": [100, 119, 100, 119],
        "Volume": [0, 0, 0, 0],
    }, index=index)
    return {"5m": frame, "15m": frame.iloc[:0], "1h": frame.iloc[:0], "4h": frame.iloc[:0]}


def test_fast_run_and_replay_have_identical_trades_and_metrics(monkeypatch):
    import services.strategy_simulator as simulator

    class Timeline:
        def timestamps(self):
            return list(_bundle()["5m"].index)
        def candle(self, timestamp):
            row = _bundle()["5m"].loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {"timestamp": pd.Timestamp(timestamp), "open": row.Open, "high": row.High, "low": row.Low, "close": row.Close})()

    monkeypatch.setattr(simulator, "build_market_facts", lambda *args, **kwargs: Timeline())

    emitted = set()
    def fake_evaluate(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        stamp = pd.Timestamp(timestamp)
        # Emit at the first candle only for each independent run.
        if stamp.minute == 0:
            return EvaluationResult(
                signal="BUY", steps={}, setup_id="setup_1", entry=100.0, sl=90.0,
                tp1=None, tp2=120.0,
                risk_budget={"method": "PERCENT_BALANCE", "value": 1.0, "dollars": account_balance * 0.01},
                next_state=EvaluationState("READY", None),
            )
        return EvaluationResult("WAIT", {}, None, None, None, None, None, None, EvaluationState())

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)
    fast = run_simulation(_definition(), _bundle(), "EURUSD", 10000.0, include_replay=False)
    replay = run_simulation(_definition(), _bundle(), "EURUSD", 10000.0, include_replay=True)
    assert fast["trades"] == replay["trades"]
    assert fast["metrics"] == replay["metrics"]
    assert fast["diagnostics"] == replay["diagnostics"]
    assert fast["diagnostics"]["candles_analyzed"] == 4
    assert fast["diagnostics"]["setups_detected"] == 1
    assert fast["diagnostics"]["signals_emitted"] == 1
    assert fast["diagnostics"]["trades_opened"] == 1
    assert fast["diagnostics"]["resolved_trades"] == 1
    assert "replay" not in fast
    assert replay["replay"]


def test_diagnostics_explain_zero_trade_run(monkeypatch):
    import services.strategy_simulator as simulator

    class Timeline:
        def timestamps(self):
            return list(_bundle()["5m"].index)
        def candle(self, timestamp):
            row = _bundle()["5m"].loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {
                "timestamp": pd.Timestamp(timestamp), "open": row.Open,
                "high": row.High, "low": row.Low, "close": row.Close,
            })()

    monkeypatch.setattr(simulator, "build_market_facts", lambda *args, **kwargs: Timeline())

    def fake_wait(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        steps = {
            "trend": {"state": "NOT_APPLICABLE"},
            "structure": {"state": "WAITING", "reason": "BOS_CHOCH_REQUIRED"},
        }
        return EvaluationResult(
            "WAIT", steps, None, None, None, None, None, None,
            EvaluationState("WAITING", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_wait)
    result = run_simulation(_definition(), _bundle(), "EURUSD", 10000.0)

    assert result["metrics"]["total_resolved_trades"] == 0
    assert result["diagnostics"]["candles_analyzed"] == 4
    assert result["diagnostics"]["evaluations"] == 4
    assert result["diagnostics"]["setups_detected"] == 0
    assert result["diagnostics"]["trades_opened"] == 0
    assert result["diagnostics"]["no_setup_reasons"] == {"BOS_CHOCH_REQUIRED": 4}


def test_warmup_candles_seed_facts_but_are_not_evaluated(monkeypatch):
    import services.strategy_simulator as simulator

    index = pd.date_range("2026-09-17T09:00:00Z", periods=6, freq="5min")
    frame = pd.DataFrame({
        "Open": [100] * 6,
        "High": [101] * 6,
        "Low": [99] * 6,
        "Close": [100] * 6,
        "Volume": [0] * 6,
    }, index=index)
    bundle = {"5m": frame, "15m": frame.iloc[:0], "1h": frame.iloc[:0], "4h": frame.iloc[:0]}

    class Timeline:
        def timestamps(self):
            return list(index)
        def candle(self, timestamp):
            row = frame.loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {
                "timestamp": pd.Timestamp(timestamp), "open": row.Open,
                "high": row.High, "low": row.Low, "close": row.Close,
            })()

    monkeypatch.setattr(simulator, "build_market_facts", lambda *args, **kwargs: Timeline())

    called = []
    def fake_evaluate(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        called.append(pd.Timestamp(timestamp))
        return EvaluationResult(
            "WAIT",
            {"structure": {"state": "WAITING", "reason": "BOS_CHOCH_REQUIRED"}},
            None, None, None, None, None, None,
            EvaluationState("WAITING", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)
    start = pd.Timestamp("2026-09-17T09:15:00Z")
    end = pd.Timestamp("2026-09-17T09:30:00Z")
    result = run_simulation(
        _definition(), bundle, "EURUSD", 10000.0,
        evaluation_start=start, evaluation_end=end,
    )

    assert called == list(index[3:6])
    assert result["diagnostics"]["candles_analyzed"] == 3
    assert result["diagnostics"]["warmup_candles"] == 3
    assert result["diagnostics"]["history_start"] == index[0].isoformat()


def test_simulation_continuation_carries_open_trade_across_chunks(monkeypatch):
    import services.strategy_simulator as simulator

    def frame(start, highs, lows, closes):
        index = pd.date_range(start, periods=len(highs), freq="5min")
        data = pd.DataFrame({
            "Open": [100.0] * len(highs),
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": [0.0] * len(highs),
        }, index=index)
        return data

    first_frame = frame(
        "2026-09-17T10:00:00Z",
        [101.0, 105.0],
        [99.0, 95.0],
        [100.0, 102.0],
    )
    second_frame = frame(
        "2026-09-17T10:10:00Z",
        [121.0, 121.0],
        [99.0, 99.0],
        [119.0, 119.0],
    )

    class Timeline:
        def __init__(self, data):
            self.data = data
        def timestamps(self):
            return list(self.data.index)
        def candle(self, timestamp):
            row = self.data.loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {
                "timestamp": pd.Timestamp(timestamp),
                "open": row.Open, "high": row.High,
                "low": row.Low, "close": row.Close,
            })()

    monkeypatch.setattr(
        simulator,
        "build_market_facts",
        lambda market_bundle, *args, **kwargs: Timeline(market_bundle["5m"]),
    )

    def fake_evaluate(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        if pd.Timestamp(timestamp) == first_frame.index[0]:
            return EvaluationResult(
                signal="BUY", steps={}, setup_id="setup_chunked",
                entry=100.0, sl=90.0, tp1=None, tp2=120.0,
                risk_budget={
                    "method": "PERCENT_BALANCE",
                    "value": 1.0,
                    "dollars": account_balance * 0.01,
                },
                next_state=EvaluationState("READY", None),
            )
        return EvaluationResult(
            "WAIT", {}, None, None, None, None, None, None,
            EvaluationState("WAITING", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)

    first_bundle = {
        "5m": first_frame,
        "15m": first_frame.iloc[:0],
        "1h": first_frame.iloc[:0],
        "4h": first_frame.iloc[:0],
    }
    first = run_simulation(
        _definition(), first_bundle, "EURUSD", 10000.0,
        finalize_open_trade=False,
    )

    assert first["trades"] == []
    assert first["continuation"]["balance"] == pytest.approx(10000.0)
    assert first["continuation"]["active_trade"]["trade_id"].startswith("sim_")
    assert first["continuation"]["ordinal"] == 1

    second_bundle = {
        "5m": second_frame,
        "15m": second_frame.iloc[:0],
        "1h": second_frame.iloc[:0],
        "4h": second_frame.iloc[:0],
    }
    second = run_simulation(
        _definition(), second_bundle, "EURUSD", 10000.0,
        continuation=first["continuation"],
        finalize_open_trade=True,
    )

    assert len(second["trades"]) == 1
    assert second["trades"][0]["outcome"] == "TP2"
    assert second["trades"][0]["pnl_dollars"] == pytest.approx(200.0)
    assert second["continuation"]["balance"] == pytest.approx(10200.0)
    assert second["continuation"]["active_trade"] is None
    assert second["continuation"]["ordinal"] == 1


def _stacking_definition(max_positions=2, risk_cap=2.0):
    value = _definition()
    value["risk"].update({
        "max_concurrent_positions": max_positions,
        "max_combined_open_risk_percent": risk_cap,
    })
    return value


def _concurrency_bundle():
    index = pd.date_range("2026-09-17T10:00:00Z", periods=4, freq="5min")
    frame = pd.DataFrame({
        "Open": [100.0, 100.0, 100.0, 100.0],
        "High": [101.0, 105.0, 121.0, 121.0],
        "Low": [99.0, 99.0, 99.0, 99.0],
        "Close": [100.0, 102.0, 119.0, 119.0],
        "Volume": [0.0, 0.0, 0.0, 0.0],
    }, index=index)
    return frame, {"5m": frame, "15m": frame.iloc[:0], "1h": frame.iloc[:0], "4h": frame.iloc[:0]}


def _install_concurrency_fakes(monkeypatch, simulator):
    frame, bundle = _concurrency_bundle()

    class Timeline:
        def timestamps(self):
            return list(frame.index)

        def candle(self, timestamp):
            row = frame.loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {
                "timestamp": pd.Timestamp(timestamp),
                "open": row.Open,
                "high": row.High,
                "low": row.Low,
                "close": row.Close,
            })()

    monkeypatch.setattr(simulator, "build_market_facts", lambda *args, **kwargs: Timeline())

    def fake_evaluate(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        stamp = pd.Timestamp(timestamp)
        if stamp in {frame.index[0], frame.index[1]}:
            suffix = "1" if stamp == frame.index[0] else "2"
            return EvaluationResult(
                signal="BUY",
                steps={},
                setup_id=f"setup_concurrent_{suffix}",
                entry=100.0,
                sl=90.0,
                tp1=None,
                tp2=120.0,
                risk_budget={
                    "method": "PERCENT_BALANCE",
                    "value": 1.0,
                    "dollars": account_balance * 0.01,
                },
                next_state=EvaluationState("READY", None),
            )
        return EvaluationResult(
            "WAIT", {}, None, None, None, None, None, None,
            EvaluationState("WAITING", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)
    return bundle


def test_default_simulator_still_allows_only_one_position(monkeypatch):
    import services.strategy_simulator as simulator

    bundle = _install_concurrency_fakes(monkeypatch, simulator)
    result = run_simulation(_definition(), bundle, "EURUSD", 10000.0)

    resolved = [item for item in result["trades"] if item.get("resolved") is True]
    assert len(resolved) == 1
    assert result["diagnostics"]["trades_opened"] == 1
    assert result["diagnostics"]["max_simultaneous_positions"] == 1
    assert result["metrics"]["ending_balance"] == pytest.approx(10200.0)
    assert result["execution_options"]["max_concurrent_positions"] == 1


def test_simulator_can_open_two_independent_concurrent_positions(monkeypatch):
    import services.strategy_simulator as simulator

    bundle = _install_concurrency_fakes(monkeypatch, simulator)
    result = run_simulation(
        _stacking_definition(2, 2.0),
        bundle,
        "EURUSD",
        10000.0,
    )

    resolved = [item for item in result["trades"] if item.get("resolved") is True]
    assert len(resolved) == 2
    assert len({item["trade_id"] for item in resolved}) == 2
    assert all(item["outcome"] == "TP2" for item in resolved)
    assert result["diagnostics"]["trades_opened"] == 2
    assert result["diagnostics"]["signals_emitted"] == 2
    assert result["diagnostics"]["max_simultaneous_positions"] == 2
    assert result["diagnostics"]["max_open_risk_dollars"] == pytest.approx(200.0)
    assert result["diagnostics"]["overlapping_entries_opened"] == 1
    assert result["metrics"]["ending_balance"] == pytest.approx(10400.0)
    assert result["execution_options"] == {
        "max_concurrent_positions": 2,
        "max_combined_open_risk_percent": 2.0,
    }


def test_combined_open_risk_cap_blocks_second_position(monkeypatch):
    import services.strategy_simulator as simulator

    bundle = _install_concurrency_fakes(monkeypatch, simulator)
    result = run_simulation(
        _stacking_definition(2, 1.5),
        bundle,
        "EURUSD",
        10000.0,
    )

    resolved = [item for item in result["trades"] if item.get("resolved") is True]
    assert len(resolved) == 1
    assert result["diagnostics"]["signals_emitted"] == 2
    assert result["diagnostics"]["trades_opened"] == 1
    assert result["diagnostics"]["combined_risk_blocked_signals"] == 1
    assert result["diagnostics"]["max_simultaneous_positions"] == 1
    assert result["metrics"]["ending_balance"] == pytest.approx(10200.0)


def test_multiple_open_positions_survive_chunk_continuation(monkeypatch):
    import services.strategy_simulator as simulator

    frame, bundle = _concurrency_bundle()

    class Timeline:
        def __init__(self, data):
            self.data = data
        def timestamps(self):
            return list(self.data.index)
        def candle(self, timestamp):
            row = self.data.loc[pd.Timestamp(timestamp)]
            return type("Candle", (), {
                "timestamp": pd.Timestamp(timestamp),
                "open": row.Open,
                "high": row.High,
                "low": row.Low,
                "close": row.Close,
            })()

    monkeypatch.setattr(
        simulator,
        "build_market_facts",
        lambda market_bundle, *args, **kwargs: Timeline(market_bundle["5m"]),
    )

    emitted = set()
    def fake_evaluate(definition, timeline, timestamp, prior_state, *, symbol, account_balance, risk_override=None):
        stamp = pd.Timestamp(timestamp)
        if stamp.minute in {0, 5} and stamp not in emitted:
            emitted.add(stamp)
            return EvaluationResult(
                "BUY", {}, f"setup_{stamp.minute}", 100.0, 90.0, None, 120.0,
                {"method": "PERCENT_BALANCE", "value": 1.0, "dollars": account_balance * 0.01},
                EvaluationState("READY", None),
            )
        return EvaluationResult(
            "WAIT", {}, None, None, None, None, None, None,
            EvaluationState("WAITING", None),
        )

    monkeypatch.setattr(simulator, "evaluate_strategy", fake_evaluate)

    first_frame = frame.iloc[:2]
    first_bundle = {"5m": first_frame, "15m": first_frame.iloc[:0], "1h": first_frame.iloc[:0], "4h": first_frame.iloc[:0]}
    first = run_simulation(
        _stacking_definition(2, 2.0), first_bundle, "EURUSD", 10000.0,
        finalize_open_trade=False,
    )
    assert len(first["continuation"]["active_trades"]) == 2
    assert first["continuation"]["active_trade"] is None

    second_frame = frame.iloc[2:]
    second_bundle = {"5m": second_frame, "15m": second_frame.iloc[:0], "1h": second_frame.iloc[:0], "4h": second_frame.iloc[:0]}
    second = run_simulation(
        _stacking_definition(2, 2.0), second_bundle, "EURUSD", 10000.0,
        continuation=first["continuation"],
        finalize_open_trade=True,
    )
    assert len([item for item in second["trades"] if item.get("resolved") is True]) == 2
    assert second["continuation"]["active_trades"] == []
    assert second["continuation"]["balance"] == pytest.approx(10400.0)
