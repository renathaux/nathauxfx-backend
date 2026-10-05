from fundamentals.config import BUY_THRESHOLD, MINIMUM_ACTIVE_COVERAGE, SELL_THRESHOLD

SUPPORTED_PAIRS = {"EURUSD": ("EUR", "USD")}


def synthesize_pair_bias(symbol, currency_strength, minimum_coverage=MINIMUM_ACTIVE_COVERAGE):
    normalized = str(symbol or "").upper().replace("/", "")
    if normalized not in SUPPORTED_PAIRS:
        return {
            "symbol": normalized,
            "pair_score": None,
            "direction": "NEUTRAL",
            "status": "UNSUPPORTED_SYMBOL",
        }
    base, quote = SUPPORTED_PAIRS[normalized]
    base_result = currency_strength.get(base) or {}
    quote_result = currency_strength.get(quote) or {}
    base_score = base_result.get("score")
    quote_score = quote_result.get("score")
    if base_score is None or quote_score is None:
        return {"symbol": normalized, "pair_score": None, "direction": "NEUTRAL", "status": "INSUFFICIENT_DATA"}
    pair_score = max(-100.0, min(100.0, round(float(base_score) - float(quote_score), 2)))
    sufficient = min(
        float(base_result.get("coverage") or 0),
        float(quote_result.get("coverage") or 0),
    ) >= minimum_coverage
    if not sufficient:
        direction = "NEUTRAL"
        status = "INSUFFICIENT_DATA"
    else:
        direction = "BUY" if pair_score >= BUY_THRESHOLD else "SELL" if pair_score <= SELL_THRESHOLD else "NEUTRAL"
        status = "ACTIVE"
    return {
        "symbol": normalized,
        "pair_score": pair_score,
        "direction": direction,
        "status": status,
        "coverage": round(min(
            float(base_result.get("coverage") or 0),
            float(quote_result.get("coverage") or 0),
        ), 4),
    }
