from . import shared


PAIR_RULES = {
    "recent_pressure_strong_body": 0.0004,
    "momentum_strong_body": 0.0004,
    "momentum_min_body": 0.0002,
    "fake_killer_stretch_ema9": 0.0012,
    "fake_killer_stretch_ema21": 0.0018,
    "fake_killer_breakout_buffer": 0.00025,
    "no_trade_pullback_limit": 0.00045,
    "no_trade_stretch_limit": 0.0010,
    "no_trade_huge_body": 0.00065,
    "no_trade_reentry_buffer": 0.00018,
    "fvg_min_gap": 0.00015,
    "displacement_min_body": 0.00025,
    "fake_breakout_reclaim_buffer": 0.00015,
    "signal_decimals": 5,
    "signal_buffer": 0.00020,
    "signal_retest_buffer": 0.00030,
    "signal_late_entry_distance": 0.00120,
    "signal_no_retest_max_distance": 0.00075,
    "equal_tolerance": 0.00015,
    "breakout_stretch_ema9": 0.0012,
    "breakout_stretch_ema21": 0.0018,
    "pip_size": 0.0001,
    "decimals": 5,
    "broker_min_distance": 0.0008,
    "fifteen_m_level_tolerance": 0.00015,
    "five_m_tolerance": 0.00002,
    "five_m_min_body": 0.00015,
    "five_m_max_confirmation_range": 0.00120,
    "late_entry_pip_size": 0.0001,
    "late_entry_max_setup_distance": 8,
    "freshness_max_distance_pips": 8,
    "market_mode_flat_limit": 0.00018,
    "paper_pip_value": 0.0001,
    "paper_decimals": 5,
    "late_entry_impulse_range_pips": 10,
}


def is_recovery_setup(
    htf_structure,
    choch_signal,
    recent_pressure,
    close_price,
    ema21,
):
    return (
        htf_structure == "BEARISH"
        and choch_signal == "BULLISH"
        and recent_pressure == "BULLISH"
        and close_price > ema21
    )


def apply_debug_after_trade_gate(context):
    score_contribution_debug = context["score_contribution_debug"]

    if context["htf_structure"] == "BEARISH" and context["choch_signal"] == "BULLISH":
        score_contribution_debug["penalties"]["htf_bearish_penalty"] = -8

    if context["bearish_fvg"] and not context["bullish_fvg"]:
        score_contribution_debug["penalties"]["bearish_fvg_penalty"] = -6

    if context["bearish_bos"] and not context["bullish_choch"]:
        score_contribution_debug["penalties"]["structure_weighting_penalty"] = -10

    score_contribution_debug.update({
        "buy_score_after_components": context["buy_score"],
        "sell_score_after_components": context["sell_score"],
        "confidence_after_components": context["confidence"],
        "final_signal_after_gate": context["final_signal"],
        "allow_trade": context["allow_trade"],
    })

    print("EURUSD_RECOVERY_SCORE_DEBUG =", score_contribution_debug)


def get_recovery_sell_state(stage):
    states = {
        "sell_ready": {
            "buy_score": 35,
            "sell_score": 50,
            "confidence": 52,
            "branch": "EURUSD_RECOVERY_SOFTENED_SELL_READY",
            "penalty": -15,
            "reason": "EURUSD recovery: reduced sell ready penalty",
        },
        "sell_holding_retest": {
            "buy_score": 35,
            "sell_score": 55,
            "confidence": 58,
            "branch": "EURUSD_RECOVERY_SOFTENED_SELL_HOLDING_RETEST",
            "penalty": -20,
            "reason": "EURUSD recovery: reduced sell holding penalty",
        },
        "sell_holding_memory": {
            "buy_score": 35,
            "sell_score": 55,
            "confidence": 58,
            "branch": "EURUSD_RECOVERY_SOFTENED_SELL_HOLDING_MEMORY",
            "penalty": -20,
            "reason": "EURUSD recovery: reduced sell holding penalty",
        },
    }

    return states.get(stage)


PAIR_RULES.update({
    "is_recovery_setup": is_recovery_setup,
    "apply_debug_after_trade_gate": apply_debug_after_trade_gate,
    "get_recovery_sell_state": get_recovery_sell_state,
})
shared.register_pair_strategy_rules("EURUSD", PAIR_RULES)


def analyze_eurusd(data_5m, data_15m, data_1h):
    analyzer = getattr(shared, "get_strict_mtf_signal", shared.get_mtf_signal)
    return analyzer(data_5m, data_15m, data_1h, "EURUSD")
