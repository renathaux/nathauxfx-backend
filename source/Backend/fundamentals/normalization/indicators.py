import re


INDICATOR_RULES = (
    ("us_10y_real_yield", ("us 10y real yield", "10-year real yield", "10 year real yield", "dfii10"), "yields", False),
    ("us_10y_treasury_yield", ("us 10y treasury yield", "10-year treasury yield", "10 year treasury yield", "dgs10"), "yields", False),
    ("financial_stress_index", ("financial stress index", "stlfsi"), "risk_sentiment", False),
    ("average_hourly_earnings", ("average hourly earnings", "wage growth", "earnings"), "employment", True),
    ("core_cpi", ("core cpi", "core consumer price"), "inflation", True),
    ("cpi", ("cpi", "consumer price"), "inflation", True),
    ("ppi", ("ppi", "producer price", "final demand"), "inflation", True),
    ("core_hicp", ("core hicp", "core harmonised", "core harmonized"), "inflation", True),
    ("hicp", ("hicp", "harmonised index", "harmonized index"), "inflation", True),
    ("core_pce", ("core pce",), "inflation", True),
    ("pce", ("pce", "personal consumption expenditure"), "inflation", True),
    ("nonfarm_payrolls", ("nonfarm payroll", "non-farm payroll", "nfp"), "employment", True),
    ("unemployment_rate", ("unemployment rate",), "employment", False),
    ("employment_change", ("employment change", "change in employment"), "employment", True),
    ("jobless_claims", ("jobless claims", "unemployment claims"), "employment", False),
    ("retail_sales", ("retail sales",), "growth", True),
    ("industrial_production", ("industrial production",), "growth", True),
    ("services_pmi", ("services pmi",), "growth", True),
    ("manufacturing_pmi", ("manufacturing pmi",), "growth", True),
    ("gdp", ("gdp", "gross domestic product"), "growth", True),
    ("pmi", ("pmi", "purchasing managers"), "growth", True),
    ("fed_interest_rate", (
        "federal reserve interest rate", "federal funds rate", "fed funds target",
        "fed interest rate decision", "fomc rate decision"
    ), "policy", True),
    ("ecb_interest_rate", (
        "ecb interest rate", "ecb rate decision", "deposit facility rate",
        "main refinancing rate", "ecb policy rate"
    ), "policy", True),
    ("interest_rate", (
        "interest rate", "rate decision", "federal funds rate", "deposit facility rate",
        "main refinancing rate", "ecb policy rate", "fed funds target"
    ), "policy", True),
)


# The EURUSD engine consumes currency policy, inflation, employment, growth,
# and surprise evidence. Yield and risk-sentiment series belong to the XAUUSD
# engine and must not be hydrated by the EURUSD read path.
EURUSD_ENGINE_CATEGORIES = frozenset({
    "policy",
    "inflation",
    "employment",
    "growth",
})
EURUSD_ENGINE_INDICATOR_BASES = frozenset(
    indicator
    for indicator, _patterns, category, _higher_bullish in INDICATOR_RULES
    if category in EURUSD_ENGINE_CATEGORIES
)


FREQUENCY_QUALIFIERS = (
    ("m_m", (r"\bm\s*/\s*m\b", r"\bmom\b", r"month(?:ly|\s+over\s+month)")),
    ("q_q", (r"\bq\s*/\s*q\b", r"\bqoq\b", r"quarter(?:ly|\s+over\s+quarter)")),
    ("y_y", (r"\by\s*/\s*y\b", r"\byoy\b", r"year(?:ly|\s+over\s+year)")),
)

RELEASE_STAGE_QUALIFIERS = (
    ("preliminary", (r"\bpreliminary\b", r"\bprelim\b", r"\bflash\b", r"\badvance\b")),
    ("final", (r"\bfinal\b",)),
)


def _matching_rule(title):
    for rule in INDICATOR_RULES:
        _indicator, patterns, _category, _higher_bullish = rule
        if any(pattern in title for pattern in patterns):
            return rule
    return None


def _qualifier(title, rules):
    for qualifier, patterns in rules:
        if any(re.search(pattern, title) for pattern in patterns):
            return qualifier
    return None


def normalize_indicator(name):
    title = re.sub(r"\s+", " ", str(name or "").strip().lower())
    canonical = re.sub(r"[^a-z0-9]+", "_", title).strip("_")
    known_bases = {rule[0] for rule in INDICATOR_RULES}
    if any(
        canonical == base
        or canonical.startswith(f"{base}_m_m")
        or canonical.startswith(f"{base}_q_q")
        or canonical.startswith(f"{base}_y_y")
        for base in known_bases
    ):
        return canonical
    rule = _matching_rule(title)
    if rule:
        indicator = rule[0]
        qualifiers = [
            value for value in (
                _qualifier(title, FREQUENCY_QUALIFIERS),
                _qualifier(title, RELEASE_STAGE_QUALIFIERS),
            ) if value
        ]
        return "_".join((indicator, *qualifiers))
    slug = re.sub(r"[^a-z0-9]+", "_", title).strip("_")
    return slug or "unknown"


def indicator_metadata(indicator_or_name):
    normalized = normalize_indicator(indicator_or_name)
    for indicator, _patterns, category, higher_bullish in INDICATOR_RULES:
        if normalized == indicator or normalized.startswith(f"{indicator}_"):
            return {
                "indicator": normalized,
                "base_indicator": indicator,
                "category": category,
                "higher_is_currency_bullish": higher_bullish,
                "recognized": True,
            }
    return {
        "indicator": normalized,
        "base_indicator": normalized,
        "category": "other",
        "higher_is_currency_bullish": None,
        "recognized": False,
    }
