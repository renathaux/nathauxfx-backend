from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi import HTTPException

import routes.ctrader as ctrader_routes


def _frame():
    return pd.DataFrame(
        [
            (4400.0, 4401.0, 4399.0, 4400.5),
            (4400.5, 4402.0, 4400.0, 4401.5),
            (4401.5, 4403.0, 4401.0, 4402.5),
        ],
        columns=["Open", "High", "Low", "Close"],
        index=pd.to_datetime(
            [
                "2026-09-10T12:00:00Z",
                "2026-09-10T12:01:00Z",
                "2026-09-10T12:02:00Z",
            ]
        ),
    )


def test_m1_enablement_is_historical_only():
    ctrader_routes._enable_read_only_m1_history()
    assert ctrader_routes._ctrader_connector.CTRADER_TRENDBAR_PERIODS["1m"] == 1
    assert ctrader_routes._ctrader_connector.CTRADER_TRENDBAR_PERIOD_MINUTES[1] == 1
    assert "1m" in ctrader_routes._CHART_HISTORY_TIMEFRAME_MINUTES
    assert "1m" not in ctrader_routes._TIMEFRAME_MINUTES


def test_m1_research_window_is_read_only_and_bounded(monkeypatch):
    calls = []

    def fake_fetch(symbol, timeframe, start, end):
        calls.append((symbol, timeframe, start, end))
        return _frame()

    monkeypatch.setattr(ctrader_routes, "fetch_ctrader_historical_candles", fake_fetch)
    start = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 10, 12, 3, tzinfo=timezone.utc)
    result = ctrader_routes.chart_candle_window(
        symbol="XAUUSD",
        timeframe="1m",
        start=start,
        end=end,
    )

    assert calls and calls[0][0:2] == ("XAUUSD", "1m")
    assert result["read_only"] is True
    assert result["observation_only"] is True
    assert result["affects_strategy"] is False
    assert result["count"] == 3

    with pytest.raises(HTTPException) as exc:
        ctrader_routes.chart_candle_window(
            symbol="XAUUSD",
            timeframe="1m",
            start=start,
            end=start + timedelta(days=1, seconds=1),
        )
    assert exc.value.status_code == 422


def test_m1_is_not_accepted_by_strategy_structure_helper():
    with pytest.raises(HTTPException) as exc:
        ctrader_routes.chart_smc_structure(
            symbol="XAUUSD",
            timeframe="1m",
            limit=250,
        )
    assert exc.value.status_code == 422
