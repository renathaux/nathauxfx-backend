import pandas as pd

import api
import ctrader_connector
from routes import ctrader


def _closed_frame(base):
    index = pd.date_range("2026-09-15T12:00:00Z", periods=3, freq="5min")
    return pd.DataFrame(
        {
            "Open": [base, base + 1, base + 2],
            "High": [base + 2, base + 3, base + 4],
            "Low": [base - 1, base, base + 1],
            "Close": [base + 1, base + 2, base + 3],
            "Volume": [10, 11, 12],
        },
        index=index,
    )


def test_startup_panel_uses_persisted_candles_for_display_only(monkeypatch):
    expected_streams = {
        (symbol, timeframe)
        for symbol in ("EURUSD", "XAUUSD")
        for timeframe in ("5m", "15m", "1h")
    }
    # This mirrors the post-restart Render filesystem. The local cache has only
    # two streams, while the durable reader has all six account-scoped streams.
    local_frames = {
        ("EURUSD", "5m"): _closed_frame(1.0),
        ("EURUSD", "15m"): _closed_frame(1.0),
    }
    durable_frames = {
        symbol: {
            timeframe: _closed_frame(1.0 if symbol == "EURUSD" else 3600.0)
            for timeframe in ("5m", "15m", "1h")
        }
        for symbol in ("EURUSD", "XAUUSD")
    }
    monkeypatch.setattr(api, "PANEL_CACHE", {"data": api.default_panel(), "last_update": 0})
    monkeypatch.setattr(
        api,
        "PANEL_REFRESH_STATE",
        {
            "running": False,
            "last_started": None,
            "last_success": None,
            "last_error": None,
            "last_duration_seconds": None,
            "reason": None,
            "last_source": None,
        },
    )
    monkeypatch.setattr(api, "LIVE_PANEL_META_CACHE", {})

    monkeypatch.setattr(
        ctrader_connector,
        "load_persisted_ctrader_candle_cache",
        lambda symbol, timeframe: {"data": local_frames[(symbol, timeframe)]},
    )
    monkeypatch.setattr(
        ctrader,
        "load_closed_indicator_candles",
        lambda *_args, **_kwargs: durable_frames,
    )
    monkeypatch.setattr(
        ctrader_connector,
        "place_market_order",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker execution must not run during display fallback")
        ),
    )
    for forbidden in (
        "get_ctrader_market_data",
        "fetch_ctrader_historical_candles",
        "get_live_prices",
        "start_ctrader_live_price_stream",
    ):
        monkeypatch.setattr(
            ctrader,
            forbidden,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{forbidden} must not run during display fallback")
            ),
        )

    result = ctrader.nonblocking_dashboard_feed()

    assert result["_meta"]["stale_data"] is True
    assert result["_meta"]["analysis_available"] is False
    assert result["_meta"]["display_data_source"] == "persisted_ctrader_closed_candles"
    assert result["_meta"]["display_only_fallback"] is True
    assert result["EURUSD"]["signal"] == "WAIT"
    assert result["EURUSD"]["market_condition"] == "DISPLAY_ONLY"
    assert result["XAUUSD"]["signal"] == "WAIT"
    assert {
        (symbol, timeframe)
        for symbol, timeframes in result["candles"].items()
        for timeframe in timeframes
    } == expected_streams
    assert all(
        len(result["candles"][symbol][timeframe]) == 3
        for symbol, timeframe in expected_streams
    )
    assert result["candles"]["EURUSD"]["5m"][-1]["time"] == 1789474200


def test_ready_panel_does_not_load_display_fallback(monkeypatch):
    panel = api.default_panel()
    panel["candles"] = {"EURUSD": {"5m": [{"time": 1}]}}
    monkeypatch.setattr(api, "PANEL_CACHE", {"data": panel, "last_update": 123.0})
    monkeypatch.setattr(api, "PANEL_REFRESH_STATE", {"last_success": 123.0})
    monkeypatch.setattr(api, "LIVE_PANEL_META_CACHE", {})

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("durable display fallback must not replace a warmed panel")

    monkeypatch.setattr(
        ctrader,
        "load_closed_indicator_candles",
        fail_if_called,
    )

    result = ctrader.nonblocking_dashboard_feed()

    assert result["candles"] == panel["candles"]
    assert result["_meta"]["display_only_fallback"] is False
    assert result["_meta"]["analysis_available"] is True
