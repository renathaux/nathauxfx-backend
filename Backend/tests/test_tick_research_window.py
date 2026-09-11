from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from routes import ctrader as route


class _FakeSocket:
    def settimeout(self, _value):
        return None

    def close(self):
        return None


def test_tick_decoder_rebuilds_relative_timestamps_and_prices(monkeypatch):
    monkeypatch.setattr(route._ctrader_connector, "get_ctrader_config", lambda: {"account_id": "1", "env": "demo"})
    monkeypatch.setattr(route._ctrader_connector, "CTRADER_JSON_ENDPOINTS", {"demo": ("example", 5036)})
    monkeypatch.setattr(route._ctrader_connector, "open_ctrader_json_socket", lambda *_args: _FakeSocket())
    monkeypatch.setattr(route._ctrader_connector, "authorize_ctrader_socket", lambda *_args: {})
    monkeypatch.setattr(route._ctrader_connector, "fetch_ctrader_symbol_details", lambda *_args: {})
    monkeypatch.setattr(
        route._ctrader_connector,
        "resolve_ctrader_symbol",
        lambda *_args: {"symbol_id": 41, "digits": 2},
    )

    newest_ms = 1_700_000_000_000
    monkeypatch.setattr(
        route._ctrader_connector,
        "send_ctrader_request",
        lambda *_args: {
            "payload": {
                # cTrader historical ticks are newest-first. After the first
                # absolute tick, BOTH timestamp and price are deltas from the
                # previous tick.
                "tickData": [
                    {"timestamp": newest_ms, "tick": 434250000},
                    {"timestamp": -250, "tick": -10000},
                    {"timestamp": -500, "tick": -10000},
                ],
                "hasMore": False,
            }
        },
    )

    start = datetime.fromtimestamp((newest_ms - 1000) / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp((newest_ms + 1000) / 1000, tz=timezone.utc)
    ticks, complete = route._fetch_read_only_ticks("XAUUSD", "bid", start, end)

    assert complete is True
    assert [tick["timestamp_ms"] for tick in ticks] == [
        newest_ms - 750,
        newest_ms - 250,
        newest_ms,
    ]
    assert [tick["price"] for tick in ticks] == [4342.3, 4342.4, 4342.5]
    assert all(tick["quote"] == "bid" for tick in ticks)


def test_tick_window_is_xauusd_only_and_tightly_bounded():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)

    with pytest.raises(HTTPException) as wrong_symbol:
        route.chart_tick_window(
            symbol="EURUSD",
            quote="bid",
            start=start,
            end=start + timedelta(minutes=1),
        )
    assert wrong_symbol.value.status_code == 422

    with pytest.raises(HTTPException) as too_wide:
        route.chart_tick_window(
            symbol="XAUUSD",
            quote="ask",
            start=start,
            end=start + timedelta(minutes=3),
        )
    assert too_wide.value.status_code == 422
