"""Centralized, auditable Phase 2 Fundamental Engine configuration."""

FACTOR_WEIGHTS = {
    "policy_score": 0.30,
    "inflation_score": 0.20,
    "employment_score": 0.20,
    "growth_score": 0.15,
    "surprise_score": 0.15,
}

MINIMUM_ACTIVE_COVERAGE = 0.60
BUY_THRESHOLD = 20.0
SELL_THRESHOLD = -20.0

ORDINARY_EVENT_HORIZON_DAYS = 90
ORDINARY_STALE_AFTER_DAYS = 45
POLICY_EVENT_HORIZON_DAYS = 365
POLICY_STALE_AFTER_DAYS = 180

FUNDAMENTAL_INGEST_INTERVAL_SECONDS = 15 * 60

# Official agencies publish on daily/monthly schedules.  Poll them separately
# from the 15-minute commercial calendar refresh to keep the engine current
# without repeatedly downloading the same archives.
OFFICIAL_INGEST_INTERVAL_SECONDS = 24 * 60 * 60
OFFICIAL_INGEST_LOOKBACK_DAYS = 60
