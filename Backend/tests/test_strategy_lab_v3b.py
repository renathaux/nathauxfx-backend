from __future__ import annotations

import pandas as pd

from services.strategy_lab import replay_engine, v3b_m5_frozen_candidate as v3b


def test_v3b_frozen_parameters_are_exact():
    assert v3b.MIN_BOS_BODY_RATIO == 0.50
    assert v3b.SL_BUFFER_POINTS == 50
    assert v3b.MIN_SL_POINTS == 100
    assert v3b.TARGET_RR == 1.90
    assert v3b.PROTECTION_TRIGGER_TP2_FRACTION == 0.70
    assert v3b.PROTECTED_STOP_TP2_FRACTION == 0.60


def test_v3b_fixed_levels_lock_expected_r_values():
    levels = v3b._fixed_levels(
        "BUY",
        1.10000,
        {"type": "LOW", "price": 1.09850},
    )
    assert levels["ok"] is True
    # SL = 1.09800 -> 20 pip risk. TP2 = 1.10380 (1.9R).
    assert levels["stop_loss"] == 1.09800
    assert levels["tp2"] == 1.10380
    # Trigger = 70% of TP path = 1.33R; protected stop = 1.14R.
    assert levels["tp1"] == 1.10266
    assert levels["protected_sl_price"] == 1.10228
    assert levels["risk_reward_ratio"] == 1.90


def test_v3b_protected_exit_is_1_14r():
    trade = {
        "entry_timestamp": "2026-08-17T10:10:00+00:00",
        "side": "BUY",
        "entry": 1.10000,
        "original_sl": 1.09900,
        "sl": 1.09900,
        "tp1": 1.10133,
        "tp2": 1.10190,
        "protected_sl": 1.10114,
        "protected_sl_price": 1.10114,
        "result": "UNRESOLVED_OPEN",
        "r_result": None,
        "exact_r_before_rounding": None,
        "exit_timestamp": None,
        "exit_price": None,
        "exit_reason": None,
        "tp1_reached": False,
        "protection_armed": False,
    }
    frame = pd.DataFrame(
        [
            (1.1002, 1.10140, 1.10010, 1.10120),
            (1.1012, 1.10130, 1.10110, 1.10115),
        ],
        columns=["Open", "High", "Low", "Close"],
        index=pd.to_datetime(
            ["2026-08-17T10:10:00Z", "2026-08-17T10:15:00Z"]
        ),
    )
    v3b.resolve_trade(trade, frame, pd.Timestamp("2026-08-17T10:30:00Z"))
    assert trade["result"] == "PROTECTED_WIN"
    assert round(trade["r_result"], 2) == 1.14
    assert trade["protection_armed"] is True


def test_v3b_is_registered_as_frozen_pure_5m_strategy():
    candidate_fn, evaluate_fn, resolve_fn = replay_engine._strategy_engine(
        "v3b_m5_frozen_candidate"
    )
    assert callable(candidate_fn)
    assert callable(evaluate_fn)
    assert callable(resolve_fn)
    params = replay_engine._strategy_parameters("v3b_m5_frozen_candidate")
    assert params["frozen_research_candidate"] is True
    assert params["uses_15m"] is False
    assert params["target_rr"] == 1.90
    assert params["protection_trigger_tp2_fraction"] == 0.70
    assert params["protected_stop_tp2_fraction"] == 0.60
