def is_expected_loss_oversized(expected_loss_usd, max_risk_usd, tolerance_multiplier=1.10):
    try:
        expected = float(expected_loss_usd)
        maximum = float(max_risk_usd)
    except (TypeError, ValueError):
        return False

    if expected < 0 or maximum <= 0:
        return False

    return expected > maximum * tolerance_multiplier
