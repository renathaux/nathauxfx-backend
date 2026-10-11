def live_prices_match(symbol, left, right, *, normalize_symbol):
    try:
        tolerance = 0.005 if normalize_symbol(symbol) == "XAUUSD" else 0.000005
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def classify_stop_loss_change(
    symbol,
    side,
    broker_sl,
    saved_sl,
    *,
    normalize_symbol,
):
    """Classify a broker SL relative to FlowSignal's last protected SL.

    A manual broker move is considered MORE_PROTECTIVE only when it reduces
    downside risk: higher for BUY, lower for SELL. Missing/invalid values are
    never accepted as manual protection.
    """
    if broker_sl in [None, ""]:
        return "MISSING"
    if saved_sl in [None, ""]:
        return "NO_BASELINE"

    try:
        broker_value = float(broker_sl)
        saved_value = float(saved_sl)
    except (TypeError, ValueError):
        return "INVALID"

    normalized_symbol = normalize_symbol(symbol)
    tolerance = 0.005 if normalized_symbol == "XAUUSD" else 0.000005
    difference = broker_value - saved_value

    if abs(difference) <= tolerance:
        return "MATCH"

    normalized_side = str(side or "").strip().upper()
    if normalized_side in {"BUY", "LONG", "1"}:
        return "MORE_PROTECTIVE" if difference > tolerance else "LESS_PROTECTIVE"
    if normalized_side in {"SELL", "SHORT", "2"}:
        return "MORE_PROTECTIVE" if difference < -tolerance else "LESS_PROTECTIVE"
    return "INVALID_SIDE"


def build_live_protection_audit(
    symbol,
    side,
    *,
    normalize_symbol,
    entry=None,
    saved_sl=None,
    broker_sl=None,
    displayed_sl=None,
    tp2=None,
    broker_tp=None,
    bid=None,
    ask=None,
    lot_size=None,
    expected_loss_usd=None,
    max_risk_usd=None,
    repair_result=None,
    stage=None,
):
    return {
        "stage": stage,
        "symbol": normalize_symbol(symbol),
        "side": str(side or "").upper(),
        "entry": entry,
        "saved_sl": saved_sl,
        "broker_sl": broker_sl,
        "displayed_sl": displayed_sl,
        "tp2": tp2,
        "broker_tp": broker_tp,
        "bid": bid,
        "ask": ask,
        "lot_size": lot_size,
        "expected_loss_usd": expected_loss_usd,
        "max_risk_usd": max_risk_usd,
        "repair_result": repair_result,
    }

