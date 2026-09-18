"""A delayed cTrader spot must not masquerade as a current chart candle."""

import copy
import time

import pandas as pd

import api
import ctrader_connector as connector
from test_simple_account_switch import selected


def _tick(received_at, broker_at):
    return {
        "bid": 1.14828,
        "ask": 1.14830,
        "mid": 1.14829,
        "timestamp": received_at,
        "server_timestamp": broker_at * 1000,
        "account_scope": "CTRADER:DEMO:47784297",
    }


def test_old_broker_spot_cannot_become_fresh_chart_candle(selected, monkeypatch):
    now = time.time()
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": _tick(now, now - 4 * 3600)})
    status = connector.get_ctrader_live_price_status()
    assert "EURUSD" in status["live_price_stale_symbols"]
    assert "EURUSD" not in status["live_prices"]
    assert connector.get_live_tick_snapshot("EURUSD") is None

    bucket = int(now // 300) * 300
    closed_frame = pd.DataFrame({
        "Open": [1.14650], "High": [1.14660], "Low": [1.14635],
        "Close": [1.14640], "Volume": [597],
    }, index=pd.to_datetime([bucket - 300], unit="s", utc=True))
    pd.testing.assert_frame_equal(
        connector.append_current_forming_candle(closed_frame, "EURUSD", "5m"),
        closed_frame,
    )

    panel = {
        "candles": {"EURUSD": {"5m": [{
            "time": bucket - 300, "open": 1.14650, "high": 1.14660,
            "low": 1.14635, "close": 1.14640,
        }]}},
        "EURUSD": {"signal_data_source": {"latest_5m_time": "closed"}},
    }
    original = copy.deepcopy(panel)
    assert api.overlay_live_forming_candles(panel, status, now=now) == original


def test_current_broker_spot_still_forms_current_candle(selected, monkeypatch):
    now = time.time()
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": _tick(now, now - 1)})
    status = connector.get_ctrader_live_price_status()
    assert "EURUSD" not in status["live_price_stale_symbols"]
    assert connector.get_live_tick_snapshot("EURUSD")["price"] == 1.14829

    closed_bucket = int(now // 300) * 300 - 300
    frame = pd.DataFrame({
        "Open": [1.14650], "High": [1.14660], "Low": [1.14635],
        "Close": [1.14640], "Volume": [1],
    }, index=pd.to_datetime([closed_bucket], unit="s", utc=True))
    assert len(connector.append_current_forming_candle(frame, "EURUSD", "5m")) == 2
