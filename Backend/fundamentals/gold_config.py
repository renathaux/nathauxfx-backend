"""Central configuration for the read-only XAUUSD macro model."""

from fundamentals.normalization.indicators import EURUSD_ENGINE_INDICATOR_BASES


GOLD_FACTOR_WEIGHTS = {
    "policy": 0.25,
    "real_yields": 0.25,
    "inflation": 0.15,
    "employment": 0.15,
    "growth": 0.10,
    "risk_sentiment": 0.10,
}

GOLD_BUY_THRESHOLD = 20.0
GOLD_SELL_THRESHOLD = -20.0
GOLD_MINIMUM_ACTIVE_COVERAGE = 0.65
GOLD_REQUIRED_FACTORS = {"real_yields"}

GOLD_CONFIDENCE_CAP = 85.0
GOLD_PROVISIONAL_CONFIDENCE_CAP = 70.0
GOLD_MISSING_YIELD_CONFIDENCE_CAP = 25.0

# Yield trend: compare the latest daily value with up to five preceding market
# observations. At least three prior observations are required. Changes within
# three basis points are noise; twenty basis points is a full-strength signal.
GOLD_YIELD_BASELINE_OBSERVATIONS = 5
GOLD_YIELD_MINIMUM_PRIOR_OBSERVATIONS = 3
GOLD_YIELD_DEADBAND_PERCENTAGE_POINTS = 0.03
GOLD_YIELD_FULL_SIGNAL_PERCENTAGE_POINTS = 0.20
GOLD_YIELD_MAX_MISSING_MARKET_DAYS = 2

GOLD_RELEVANT_EVENT_INDICATORS = {
    "cpi", "core_cpi", "pce", "core_pce", "ppi",
    "nonfarm_payrolls", "unemployment_rate", "average_hourly_earnings",
    "fed_interest_rate", "interest_rate", "gdp", "retail_sales",
    "manufacturing_pmi", "services_pmi", "pmi",
}

# Complete SQL allowlist for the XAUUSD engine. The calendar subset above is
# intentionally narrower because daily yields and financial-stress series are
# evidence inputs, not scheduled high-impact calendar events.
XAUUSD_ENGINE_INDICATOR_BASES = frozenset({
    *(EURUSD_ENGINE_INDICATOR_BASES - {
        "core_hicp",
        "hicp",
        "ecb_interest_rate",
    }),
    "us_10y_real_yield",
    "us_10y_treasury_yield",
    "financial_stress_index",
})
