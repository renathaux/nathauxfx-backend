from datetime import datetime, timezone

import pandas as pd

import app_bootstrap
import ctrader_connector
from services import indicator_candle_display_reader as display_reader


def _closed_frame():
    index = pd.date_range(
        "2026-09-14T18:00:00Z",
        periods=4,
        freq="15min",
    )
    frame = pd.DataFrame(
        {
            "Open": [1.10, 1.11, 1.12, 1.13],
            "High": [1.11, 1.12, 1.13, 1.14],
            "Low": [1.09, 1.10, 1.11, 1.12],
            "Close": [1.105, 1.115, 1.125, 1.135],
            "Volume": [10, 11, 12, 13],
        },
        index=index,
    )
    frame.index.name = "Datetime"
    return frame


def test_chart_uses_persisted_closed_candles_when_live_market_data_unavailable(monkeypatch):
    persisted = _closed_frame()

    monkeypatch.setattr(
        app_bootstrap.api,
        "get_ctrader_market_data",
        lambda *args, **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(
        ctrader_connector,
        "load_persisted_ctrader_candle_cache",
        lambda symbol, timeframe: {"data": persisted.copy()},
    )

    captured = {}

    def fake_build_chart_structure(
        closed,
        symbol,
        timeframe,
        strict_trader_module=None,
        display_limit=250,
    ):
        captured["rows"] = len(closed)
        captured["symbol"] = symbol
        captured["timeframe"] = timeframe
        return {"events": []}

    monkeypatch.setattr(
        app_bootstrap,
        "build_chart_structure",
        fake_build_chart_structure,
    )

    result = app_bootstrap.chart_smc_structure(
        symbol="EURUSD",
        timeframe="15m",
        limit=250,
    )

    assert captured == {
        "rows": 4,
        "symbol": "EURUSD",
        "timeframe": "15m",
    }
    assert result["display_data_source"] == "persisted_ctrader_closed_candles"
    assert result["display_closed_candles_available"] is True


def test_panel_display_reader_prefers_memory_and_strips_forming_candle(monkeypatch):
    monkeypatch.setattr(display_reader, "load_durable_indicator_candles", lambda *_args, **_kwargs: {})
    cached = pd.DataFrame(
        {
            "Open": [1.10, 1.11, 1.12, 1.13],
            "High": [1.11, 1.12, 1.13, 1.14],
            "Low": [1.09, 1.10, 1.11, 1.12],
            "Close": [1.105, 1.115, 1.125, 1.135],
            "Volume": [10, 11, 12, 13],
        },
        index=pd.date_range("2026-09-15T12:00:00Z", periods=4, freq="5min"),
    )
    before = cached.copy(deep=True)

    result = display_reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope="CTRADER:DEMO:47810571",
        candle_cache={"CTRADER:DEMO:47810571:EURUSD:5m": {"data": cached}},
        cache_health_reader=lambda *_args: {
            "usable": True,
            "last_candle_age_seconds": 30.0,
            "max_recovery_age_seconds": 15 * 60,
            "recovery_mode": False,
        },
        now=datetime(2026, 9, 15, 12, 17, tzinfo=timezone.utc),
    )

    frame = result["frames"]["EURUSD"]["5m"]
    assert list(frame.index) == list(pd.to_datetime([
        "2026-09-15T12:00:00Z",
        "2026-09-15T12:05:00Z",
        "2026-09-15T12:10:00Z",
    ]))
    assert result["streams"]["EURUSD"]["5m"]["source"] == "in_memory_ctrader_closed_candles"
    pd.testing.assert_frame_equal(cached, before)


def test_panel_display_reader_rechecks_freshness_after_forming_candle_removed(monkeypatch):
    cached = pd.DataFrame(
        {
            "Open": [1.10, 1.11],
            "High": [1.11, 1.12],
            "Low": [1.09, 1.10],
            "Close": [1.105, 1.115],
            "Volume": [10, 0],
        },
        index=pd.to_datetime([
            "2026-09-15T10:00:00Z",
            "2026-09-15T12:15:00Z",
        ]),
    )
    monkeypatch.setattr(
        display_reader,
        "load_durable_indicator_candles",
        lambda *_args, **_kwargs: {},
    )

    result = display_reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("5m",),
        stream_scope="CTRADER:DEMO:47810571",
        candle_cache={"CTRADER:DEMO:47810571:EURUSD:5m": {"data": cached}},
        cache_health_reader=lambda *_args: {
            # The raw cache looks fresh only because the 12:15 forming bucket is present.
            "usable": True,
            "last_candle_age_seconds": 120.0,
            "max_recovery_age_seconds": 15 * 60,
            "recovery_mode": False,
        },
        now=datetime(2026, 9, 15, 12, 17, tzinfo=timezone.utc),
    )

    assert result["frames"] == {}
    assert result["streams"] == {}


def test_panel_display_reader_never_uses_another_accounts_memory_frame(monkeypatch):
    monkeypatch.setattr(display_reader, "load_durable_indicator_candles", lambda *_args, **_kwargs: {})
    other_account_frame = _closed_frame()
    result = display_reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("15m",),
        stream_scope="CTRADER:DEMO:47784297",
        candle_cache={"CTRADER:DEMO:47810571:EURUSD:15m": {"data": other_account_frame}},
        cache_health_reader=lambda *_args: {"usable": True},
        now=datetime(2026, 9, 14, 19, tzinfo=timezone.utc),
    )
    assert result["frames"] == {}
    assert result["streams"] == {}


def test_panel_display_reader_rejects_health_from_previous_account(monkeypatch):
    monkeypatch.setattr(display_reader, "load_durable_indicator_candles", lambda *_args, **_kwargs: {})
    result = display_reader.load_dashboard_display_candles(
        ("EURUSD",),
        ("15m",),
        stream_scope="CTRADER:DEMO:47784297",
        candle_cache={"CTRADER:DEMO:47784297:EURUSD:15m": {"data": _closed_frame()}},
        cache_health_reader=lambda *_args: {
            "cache_key": "CTRADER:DEMO:47810571:EURUSD:15m",
            "usable": True,
        },
        now=datetime(2026, 9, 14, 19, tzinfo=timezone.utc),
    )
    assert result["frames"] == {}
    assert result["streams"] == {}
