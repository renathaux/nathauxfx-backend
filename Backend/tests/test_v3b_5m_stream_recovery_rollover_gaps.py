import pandas as pd
import pytest

from services import v3b_5m_stream_recovery as recovery


def _frame(times):
    index = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    return pd.DataFrame(
        {
            "Open": [1.1] * len(index),
            "High": [1.2] * len(index),
            "Low": [1.0] * len(index),
            "Close": [1.15] * len(index),
        },
        index=index,
    )


def test_eurusd_short_rollover_no_tick_gap_is_allowed():
    frame = _frame([
        "2026-09-14T20:40:00Z",
        "2026-09-14T20:45:00Z",
        "2026-09-14T20:50:00Z",
        "2026-09-14T21:05:00Z",
        "2026-09-14T21:10:00Z",
    ])

    result = recovery.validate_closed_history_coverage(
        frame,
        "2026-09-14T20:40:00Z",
        "2026-09-14T21:10:00Z",
        public_symbol="EURUSD",
    )

    assert result["allowed_sparse_gaps"] == [
        "2026-09-14T20:55:00+00:00",
        "2026-09-14T21:00:00+00:00",
    ]


def test_xauusd_daily_market_close_gap_is_allowed():
    frame = _frame([
        "2026-09-14T20:40:00Z",
        "2026-09-14T20:45:00Z",
        "2026-09-14T22:00:00Z",
        "2026-09-14T22:05:00Z",
    ])

    result = recovery.validate_closed_history_coverage(
        frame,
        "2026-09-14T20:40:00Z",
        "2026-09-14T20:45:00Z",
        public_symbol="XAUUSD",
    )

    assert result["allowed_sparse_gaps"][0] == "2026-09-14T20:50:00+00:00"
    assert result["allowed_sparse_gaps"][-1] == "2026-09-14T21:55:00+00:00"


def test_mid_session_gap_still_blocks():
    frame = _frame([
        "2026-09-14T00:00:00Z",
        "2026-09-14T00:05:00Z",
        "2026-09-14T00:15:00Z",
        "2026-09-14T00:20:00Z",
    ])

    with pytest.raises(recovery.V3B5MRecoveryBlocked, match="missing 2026-09-14T00:10:00"):
        recovery.validate_closed_history_coverage(
            frame,
            "2026-09-14T00:00:00Z",
            "2026-09-14T00:20:00Z",
            public_symbol="EURUSD",
        )


def test_off_grid_timestamp_still_blocks():
    frame = _frame([
        "2026-09-14T20:40:00Z",
        "2026-09-14T20:46:00Z",
        "2026-09-14T20:50:00Z",
    ])

    with pytest.raises(recovery.V3B5MRecoveryBlocked, match="off-grid timestamp"):
        recovery.validate_closed_history_coverage(
            frame,
            "2026-09-14T20:40:00Z",
            "2026-09-14T20:50:00Z",
            public_symbol="EURUSD",
        )
