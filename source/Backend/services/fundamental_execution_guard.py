from __future__ import annotations

from fundamentals.gold_insight_service import get_xauusd_fundamental_insight
from fundamentals.insight_cache import get_or_calculate
from fundamentals.insight_service import get_fundamental_insight


from live_integrity.fundamental_policy import *
from live_integrity.fundamental_policy import _normalize_symbol, validate_fundamental_entry as _evaluate_snapshot

def _load_insight(symbol):
    normalized = _normalize_symbol(symbol)

    def calculate():
        if normalized == "XAUUSD":
            return get_xauusd_fundamental_insight()
        return get_fundamental_insight(normalized, persist=False)

    return get_or_calculate(
        normalized,
        calculate,
        ttl_seconds=EXECUTION_CACHE_TTL_SECONDS,
    )


def validate_fundamental_entry(symbol, side, *, insight=None, policy=DEFAULT_FUNDAMENTAL_POLICY):
    normalized = _normalize_symbol(symbol)
    if (normalize_fundamental_policy(policy) is None or normalized not in SUPPORTED_SYMBOLS
            or str(side or "").upper() not in {"BUY", "SELL"}):
        return _evaluate_snapshot(symbol, side, insight=insight, policy=policy)
    try:
        current = insight if insight is not None else _load_insight(normalized)
    except Exception as exc:
        return _evaluate_snapshot(symbol, side, policy=policy, load_error=str(exc))
    return _evaluate_snapshot(symbol, side, insight=current, policy=policy)
