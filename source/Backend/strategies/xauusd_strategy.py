from . import shared


PAIR_RULES = {
    "recent_pressure_strong_body": 1.5,
    "momentum_strong_body": 1.5,
    "momentum_min_body": 0.8,
    "fake_killer_stretch_ema9": 6.0,
    "fake_killer_stretch_ema21": 9.0,
    "fake_killer_breakout_buffer": 1.5,
    "no_trade_pullback_limit": 2.2,
    "no_trade_stretch_limit": 5.0,
    "no_trade_huge_body": 2.2,
    "no_trade_reentry_buffer": 0.7,
    "fvg_min_gap": 0.80,
    "displacement_min_body": 1.0,
    "fake_breakout_reclaim_buffer": 0.80,
    "signal_decimals": 2,
    "signal_buffer": 1.20,
    "signal_retest_buffer": 2.50,
    "signal_late_entry_distance": 6.00,
    "signal_no_retest_max_distance": 3.50,
    "equal_tolerance": 1.0,
    "breakout_stretch_ema9": 6.0,
    "breakout_stretch_ema21": 9.0,
    "pip_size": 0.01,
    "decimals": 2,
    "broker_min_distance": 1.5,
    "fifteen_m_level_tolerance": 1.00,
    "five_m_tolerance": 0.05,
    "five_m_min_body": 0.80,
    "five_m_max_confirmation_range": 8.00,
    "late_entry_pip_size": 0.1,
    "late_entry_max_setup_distance": 120,
    "freshness_max_distance_pips": 120,
    "market_mode_flat_limit": 1.2,
    "paper_pip_value": 1.0,
    "paper_decimals": 2,
    "late_entry_impulse_range_pips": 140,
    "daily_pause_max_stale_seconds": 190 * 60,
}


def apply_scoring_correction(context):
    final_signal = context["final_signal"]

    if final_signal not in ["WAIT", "BUY", "SELL"]:
        return context

    htf_structure = context["htf_structure"]
    market_state = context["market_state"]
    c1 = context["c1"]
    ema9 = context["ema9"]
    ema21 = context["ema21"]
    bearish_choch = context["bearish_choch"]
    bearish_bos = context["bearish_bos"]
    bearish_displacement = context["bearish_displacement"]
    bearish_fvg = context["bearish_fvg"]
    recent_pressure = context["recent_pressure"]
    bullish_displacement = context["bullish_displacement"]
    bullish_choch = context["bullish_choch"]
    bullish_bos = context["bullish_bos"]
    entry_timing = context["entry_timing"]
    zone_position = context["zone_position"]
    market_condition = context["market_condition"]

    buy_score = context["buy_score"]
    sell_score = context["sell_score"]
    confidence = context["confidence"]
    before_buy_score = buy_score
    before_sell_score = sell_score
    before_confidence = confidence

    bearish_market_states = {
        "BEAR_EXPANSION",
        "BEAR_PULLBACK",
        "TREND_CONTINUATION_BEAR",
        "BREAKOUT_BEAR",
    }

    bearish_regime = (
        htf_structure == "BEARISH"
        or market_state in bearish_market_states
    )

    bearish_ema_alignment = (
        c1 < ema9
        and c1 < ema21
        and ema9 < ema21
    )

    gold_bearish_points = 0
    if htf_structure == "BEARISH":
        gold_bearish_points += 25

    if bearish_choch:
        gold_bearish_points += 20

    if bearish_bos:
        gold_bearish_points += 15

    if bearish_displacement:
        gold_bearish_points += 15

    if bearish_fvg:
        gold_bearish_points += 10

    if recent_pressure == "BEARISH":
        gold_bearish_points += 10

    if bearish_regime:
        gold_bearish_points += 28

    if bearish_bos:
        gold_bearish_points += 18

    if bearish_ema_alignment:
        gold_bearish_points += 22

    if bearish_displacement:
        gold_bearish_points += 8

    if bullish_displacement and bearish_regime:
        gold_bearish_points += 7

    if gold_bearish_points > 0:
        sell_score += gold_bearish_points
        buy_score -= int(gold_bearish_points * 0.8)

    if (
        htf_structure == "BEARISH"
        and (
            bearish_choch
            or bearish_bos
            or "CHOCH SELL" in str(entry_timing or "").upper()
        )
    ):
        sell_score = max(sell_score, 75)
        buy_score = min(buy_score, 25)
        context["reasons"].append("XAUUSD bearish override: 1h bearish + 15m sell setup")

    buy_score = max(5, min(95, int(round(buy_score))))
    sell_score = max(5, min(95, int(round(sell_score))))

    score_total = buy_score + sell_score
    if score_total > 100:
        buy_score = int(round((buy_score / score_total) * 100))
        sell_score = 100 - buy_score

    buy_score = max(5, min(95, buy_score))
    sell_score = max(5, min(95, sell_score))

    if (
        htf_structure == "BEARISH"
        and (
            bearish_choch
            or bearish_bos
        )
    ):
        sell_score = max(sell_score, 75)
        buy_score = min(buy_score, 25)

    if (
        htf_structure == "BULLISH"
        and (
            bullish_choch
            or bullish_bos
        )
    ):
        buy_score = max(buy_score, 75)
        sell_score = min(sell_score, 25)

    corrected_confidence = shared.calculate_confidence(
        buy_score,
        sell_score,
        htf_structure,
        context["choch_signal"],
        zone_position,
        "NONE",
        market_state,
        entry_timing,
        market_condition,
    )

    if final_signal == "BUY" and sell_score > buy_score:
        confidence = min(before_confidence, corrected_confidence)
    else:
        confidence = max(before_confidence, corrected_confidence)

    context.update({
        "buy_score": buy_score,
        "sell_score": sell_score,
        "confidence": confidence,
    })

    print("XAUUSD_SCORING_CORRECTION_DEBUG =", {
        "symbol": context["symbol"],
        "before_buy_score": before_buy_score,
        "after_buy_score": buy_score,
        "before_sell_score": before_sell_score,
        "after_sell_score": sell_score,
        "before_confidence": before_confidence,
        "after_confidence": confidence,
        "gold_bearish_points": gold_bearish_points,
        "htf_structure": htf_structure,
        "market_state": market_state,
        "recent_pressure": recent_pressure,
        "bullish_displacement": bullish_displacement,
        "bearish_displacement": bearish_displacement,
        "bullish_bos": bullish_bos,
        "bearish_bos": bearish_bos,
        "bearish_ema_alignment": bearish_ema_alignment,
    })

    return context


def is_daily_pause(now_utc):
    current_time = now_utc or shared.datetime.now(shared.timezone.utc)
    local_time = current_time.astimezone(shared.MARKET_TIMEZONE)
    local_minutes = local_time.hour * 60 + local_time.minute

    return 17 * 60 <= local_minutes < 19 * 60 + 10


PAIR_RULES["apply_scoring_correction"] = apply_scoring_correction
PAIR_RULES["is_daily_pause"] = is_daily_pause
shared.register_pair_strategy_rules("XAUUSD", PAIR_RULES)


def analyze_xauusd(data_5m, data_15m, data_1h):
    analyzer = getattr(shared, "get_strict_mtf_signal", shared.get_mtf_signal)
    return analyzer(data_5m, data_15m, data_1h, "XAUUSD")
