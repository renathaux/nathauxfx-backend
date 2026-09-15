import pandas as pd

import app_bootstrap


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
        app_bootstrap,
        "load_persisted_ctrader_candle_cache",
        lambda symbol, timeframe: {"data": persisted.copy()},
        raising=False,
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
