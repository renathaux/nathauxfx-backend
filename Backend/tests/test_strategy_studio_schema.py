import math

from services.strategy_studio_schema import normalize_definition, strategy_summary, validation_errors


def valid_definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": ["CLOSE_BEYOND", "MIN_BODY_PERCENT"],
            "minimum_body_percent": 50,
            "minimum_distance_pips": None,
        },
        "confirmation": {
            "rules": ["NEXT_SAME_DIRECTION", "SECOND_CLOSE_BEYOND"],
            "minimum_body_percent": None,
        },
        "entry": {"method": "CONFIRMATION_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 5, "fixed_distance": None},
        "tp1": {"enabled": True, "target_r": 0.75, "close_percent": 80, "protection_r": 0.2},
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
        "fundamentals": {"mode": "BLOCK_OPPOSITE"},
    }


def test_valid_definition_round_trips():
    result = normalize_definition(valid_definition())
    assert result["symbols"] == ["EURUSD"]
    assert result["tp1"]["close_percent"] == 80.0


def test_trend_timeframe_must_be_higher():
    payload = valid_definition()
    payload["trading_timeframe"] = "15m"
    payload["trend"] = {"timeframe": "15m", "methods": ["EMA_50"]}
    assert validation_errors(payload)["trend.timeframe"] == "Trend timeframe must be higher than trading timeframe"


def test_confirmation_entry_requires_confirmation():
    payload = valid_definition()
    payload["confirmation"] = {"rules": [], "minimum_body_percent": None}
    assert "entry.method" in validation_errors(payload)


def test_retest_entry_requires_retest_confirmation():
    payload = valid_definition()
    payload["entry"]["method"] = "RETEST"
    assert "entry.method" in validation_errors(payload)


def test_tp1_enabled_requires_target_close_and_protection():
    payload = valid_definition()
    payload["tp1"]["protection_r"] = None
    assert "tp1.protection_r" in validation_errors(payload)


def test_disabled_tp1_normalizes_values_to_none():
    payload = valid_definition()
    payload["tp1"] = {"enabled": False, "target_r": 1.0, "close_percent": 80, "protection_r": 0.4}
    result = normalize_definition(payload)
    assert result["tp1"] == {
        "enabled": False,
        "target_r": None,
        "target_basis": "SL_DISTANCE",
        "close_percent": None,
        "protection_r": None,
        "protection_mode": "FIXED",
        "protection_steps": [],
    }


def test_min_distance_requires_positive_value():
    payload = valid_definition()
    payload["structure"]["break_validation"] = ["MIN_DISTANCE"]
    payload["structure"]["minimum_body_percent"] = None
    payload["structure"]["minimum_distance_pips"] = 0
    assert "structure.minimum_distance_pips" in validation_errors(payload)


def test_bos_close_cannot_have_future_confirmation_rules():
    payload = valid_definition()
    payload["entry"]["method"] = "BOS_CHOCH_CLOSE"
    assert "entry.method" in validation_errors(payload)


def test_risk_has_no_arbitrary_upper_cap_but_must_be_finite_positive():
    payload = valid_definition()
    payload["risk"] = {"method": "PERCENT_BALANCE", "value": 250.0}
    assert validation_errors(payload) == {}
    payload["risk"]["value"] = math.inf
    assert "risk.value" in validation_errors(payload)


def test_summary_is_deterministic_and_readable():
    text = strategy_summary(valid_definition())
    assert text == (
        "5m BOS/CHOCH -> close beyond level + body >= 50% -> "
        "next candle same direction + second close beyond level -> confirmation close -> "
        "5m swing SL + 5 pip buffer -> TP1 75% of SL distance / close 80% / secure 20% of SL distance -> "
        "TP2 2R -> risk 1% balance -> LIVE fundamentals block opposite bias"
    )


def test_legacy_definition_without_fundamentals_defaults_to_block_opposite():
    payload = valid_definition()
    payload.pop("fundamentals")
    result = normalize_definition(payload)
    assert result["fundamentals"] == {"mode": "BLOCK_OPPOSITE"}


def test_require_alignment_is_valid_and_appears_in_summary():
    payload = valid_definition()
    payload["fundamentals"] = {"mode": "REQUIRE_ALIGNMENT"}
    result = normalize_definition(payload)
    assert result["fundamentals"]["mode"] == "REQUIRE_ALIGNMENT"
    assert "LIVE fundamentals require alignment" in strategy_summary(result)


def test_tp2_based_tp1_and_step_protection_are_valid():
    payload = valid_definition()
    payload["tp1"] = {
        "enabled": True,
        "target_r": 0.70,
        "target_basis": "TP2_DISTANCE",
        "close_percent": 40,
        "protection_r": None,
        "protection_mode": "TP2_STEPS",
        "protection_steps": [
            {"trigger_percent": 70, "secure_percent": 50},
            {"trigger_percent": 80, "secure_percent": 60},
            {"trigger_percent": 90, "secure_percent": 70},
        ],
    }
    assert validation_errors(payload) == {}
    normalized = normalize_definition(payload)
    assert normalized["tp1"]["target_basis"] == "TP2_DISTANCE"
    assert normalized["tp1"]["protection_mode"] == "TP2_STEPS"
    assert normalized["tp1"]["protection_steps"][1] == {
        "trigger_percent": 80.0,
        "secure_percent": 60.0,
    }
    summary = strategy_summary(payload)
    assert "TP1 70% of TP2 distance" in summary
    assert "70%→secure 50%" in summary
    assert "90%→secure 70%" in summary


def test_tp2_based_tp1_cannot_be_beyond_tp2():
    payload = valid_definition()
    payload["tp1"].update({
        "target_basis": "TP2_DISTANCE",
        "target_r": 1.20,
    })
    assert "tp1.target_r" in validation_errors(payload)


def test_step_protection_requires_increasing_safe_steps():
    payload = valid_definition()
    payload["tp1"] = {
        "enabled": True,
        "target_r": 0.70,
        "target_basis": "TP2_DISTANCE",
        "close_percent": 40,
        "protection_r": None,
        "protection_mode": "TP2_STEPS",
        "protection_steps": [
            {"trigger_percent": 70, "secure_percent": 50},
            {"trigger_percent": 80, "secure_percent": 85},
        ],
    }
    errors = validation_errors(payload)
    assert "tp1.protection_steps.1" in errors


def test_remember_bos_entry_option_is_valid_and_readable():
    payload = valid_definition()
    payload["entry"] = {
        "method": "CONFIRMATION_CLOSE",
        "remember_bos_on_confirmation_failure": True,
    }
    assert validation_errors(payload) == {}
    normalized = normalize_definition(payload)
    assert normalized["entry"]["remember_bos_on_confirmation_failure"] is True
    assert "remember BOS on failed next candle" in strategy_summary(normalized)


def test_remember_bos_requires_next_same_direction_confirmation():
    payload = valid_definition()
    payload["confirmation"]["rules"] = ["SECOND_CLOSE_BEYOND"]
    payload["entry"] = {
        "method": "CONFIRMATION_CLOSE",
        "remember_bos_on_confirmation_failure": True,
    }
    errors = validation_errors(payload)
    assert "entry.remember_bos_on_confirmation_failure" in errors


def test_remember_bos_requires_confirmation_close_entry():
    payload = valid_definition()
    payload["confirmation"] = {"rules": [], "minimum_body_percent": None}
    payload["entry"] = {
        "method": "BOS_CHOCH_CLOSE",
        "remember_bos_on_confirmation_failure": True,
    }
    errors = validation_errors(payload)
    assert "entry.remember_bos_on_confirmation_failure" in errors


def test_legacy_entry_defaults_remember_bos_off():
    normalized = normalize_definition(valid_definition())
    assert normalized["entry"]["remember_bos_on_confirmation_failure"] is False
