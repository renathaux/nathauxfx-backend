"""Field-level source authority for canonical fundamental observations.

Values are selected, never averaged. Official agencies own actuals, revisions,
release timestamps, and policy decisions. JBlanked MQL5 owns consensus and may
supplement impact metadata, without overwriting official values.
"""
from __future__ import annotations

from fundamentals.normalization.indicators import indicator_metadata


RULE_VERSION = "official-v1"
OFFICIAL_PROVIDERS = {"bls", "bea", "eurostat", "federal_reserve", "ecb", "treasury", "fred"}
JBLANKED_PROVIDERS = {
    "jblanked", "jblanked_live", "jblanked_cache", "jblanked_mql5",
    "jblanked_forex_factory", "jblanked_fxstreet",
}


def expected_official_provider(indicator, currency):
    base = indicator_metadata(indicator).get("base_indicator")
    currency = str(currency or "").upper()
    if currency == "USD":
        if base in {"us_10y_treasury_yield", "us_10y_real_yield"}:
            return "treasury"
        if base in {
            "cpi", "core_cpi", "ppi", "nonfarm_payrolls",
            "unemployment_rate", "average_hourly_earnings",
        }:
            return "bls"
        if base in {"pce", "core_pce", "gdp"}:
            return "bea"
        if base in {"fed_interest_rate", "interest_rate"}:
            return "federal_reserve"
    if currency == "EUR":
        if base in {
            "hicp", "core_hicp", "unemployment_rate", "gdp",
            "employment_change",
        }:
            return "eurostat"
        if base in {"ecb_interest_rate", "interest_rate"}:
            return "ecb"
    return None


def authority_rank(field, provider, indicator, currency):
    provider = str(provider or "").lower()
    field = str(field or "").lower()
    expected = expected_official_provider(indicator, currency)
    if field in {"forecast", "impact"}:
        if provider == "jblanked_mql5":
            return 0
        if provider in JBLANKED_PROVIDERS:
            return 5
        if provider in OFFICIAL_PROVIDERS:
            return 30
        return 60
    if field in {"actual", "previous", "revised_previous", "release_time", "policy_decision"}:
        if provider == expected:
            return 0
        if provider in OFFICIAL_PROVIDERS:
            return 10
        if provider in JBLANKED_PROVIDERS:
            return 50
        return 70
    return 50


def choose_field(field, candidates, indicator, currency):
    """Return the best non-null candidate; ties prefer the latest observation."""
    available = [item for item in candidates if item.get("value") not in (None, "")]
    if not available:
        return None
    return min(
        available,
        key=lambda item: (
            authority_rank(field, item.get("provider"), indicator, currency),
            -float(item.get("sequence") or 0),
        ),
    )


def values_disagree(left, right):
    if left in (None, "") or right in (None, ""):
        return False
    return str(left).strip().lower() != str(right).strip().lower()
