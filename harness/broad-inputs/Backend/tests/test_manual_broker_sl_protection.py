from risk_management.broker_protection import classify_stop_loss_change


def normalize_symbol(value):
    return str(value or "").upper()


def classify(symbol, side, broker_sl, saved_sl):
    return classify_stop_loss_change(symbol, side, broker_sl, saved_sl, normalize_symbol=normalize_symbol)


def test_buy_tighter_manual_sl_is_more_protective():
    assert classify("EURUSD", "BUY", 1.10100, 1.10000) == "MORE_PROTECTIVE"


def test_buy_wider_manual_sl_is_less_protective():
    assert classify("EURUSD", "BUY", 1.09900, 1.10000) == "LESS_PROTECTIVE"


def test_sell_tighter_manual_sl_is_more_protective():
    assert classify("EURUSD", "SELL", 1.09900, 1.10000) == "MORE_PROTECTIVE"


def test_sell_wider_manual_sl_is_less_protective():
    assert classify("EURUSD", "SELL", 1.10100, 1.10000) == "LESS_PROTECTIVE"


def test_missing_sl_is_never_adopted():
    assert classify("EURUSD", "BUY", None, 1.10000) == "MISSING"


def test_small_price_noise_counts_as_match():
    assert classify("EURUSD", "BUY", 1.100003, 1.100000) == "MATCH"


def test_gold_tolerance_avoids_false_manual_change():
    assert classify("XAUUSD", "SELL", 4350.003, 4350.000) == "MATCH"
