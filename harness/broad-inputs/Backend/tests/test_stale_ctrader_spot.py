"""A delayed cTrader spot must not masquerade as a current chart candle."""

import copy
import json
import socket as socket_module
import time
from types import SimpleNamespace

import pandas as pd
import pytest

import api
import ctrader_connector as connector
from test_simple_account_switch import selected
from ctrader_account_context import AccountIdentity
from test_recovery_store import db, store_api
from recovery_integration_fixture import prepare_database, recovery_server, admit
from startup_recovery.operation_context import capture, run


@pytest.fixture
def spot_context(db, monkeypatch, tmp_path):
    prepare_database(db, '47784297')
    harness = recovery_server(db, monkeypatch, tmp_path, account_id='47784297')
    admit(harness, entries=False)
    with harness.server.runtime_context():
        context = capture('price')
    # A burst represents one periodic-selection interval regardless of local
    # PostgreSQL latency. Do not replace the shared time module's DB timers.
    monkeypatch.setattr(connector, 'time', SimpleNamespace(
        time=time.time, sleep=time.sleep, monotonic=lambda: 100.0))
    # Only the immutable context crosses the worker boundary. No ambient token,
    # DB session, broker permit, or new-entry authority is supplied to the test.
    return context


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


def test_stream_discards_spots_older_than_live_price_limit_before_account_db_lookup(monkeypatch, spot_context):
    """A quote unusable by /chart/live-ticks must not slow socket drainage."""
    account = AccountIdentity("47784297", "demo", spot_context.selection_revision)
    socket = type("FakeSocket", (), {"settimeout": lambda self, _: None,
                                      "close": lambda self: None})()
    now_ms = int(time.time() * 1000)
    events = iter([
        {"payloadType": connector.PAYLOAD_SPOT_EVENT,
         "payload": {"symbolId": 1, "bid": 114628, "ask": 114630,
                     "timestamp": now_ms - 30 * 1000}},
        {"payloadType": connector.PAYLOAD_SPOT_EVENT,
         "payload": {"symbolId": 1, "bid": 114634, "ask": 114636,
                     "timestamp": now_ms}},
    ])
    selections = []
    accepted = []
    real_update_live_tick = connector.update_live_tick

    def recv(_socket):
        try:
            return json.dumps(next(events))
        except StopIteration:
            raise KeyboardInterrupt

    def select():
        selections.append(account)
        return account

    monkeypatch.setattr(connector, "get_ctrader_config", lambda: {"account_id": "47784297", "env": "demo"})
    monkeypatch.setattr(connector, "open_ctrader_json_socket", lambda *_: socket)
    monkeypatch.setattr(connector, "authorize_ctrader_socket", lambda *_: None)
    monkeypatch.setattr(connector, "fetch_ctrader_symbol_details", lambda *_: [{"symbolId": 1, "symbolName": "EURUSD"}])
    monkeypatch.setattr(connector, "resolve_ctrader_symbol", lambda _details, symbol: {"symbol_id": 1} if symbol == "EURUSD" else None)
    monkeypatch.setattr(connector, "send_ctrader_request", lambda *_: {})
    monkeypatch.setattr(connector, "websocket_recv_text", recv)
    monkeypatch.setattr(connector, "selected_identity", select)
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": {}})

    def record_tick(symbol, bid, ask, timestamp, **kw):
        accepted.append((symbol, timestamp, kw["account_scope"]))
        return real_update_live_tick(symbol, bid, ask, timestamp, **kw)

    monkeypatch.setattr(connector, "update_live_tick", record_tick)

    try:
        run(spot_context, connector.ctrader_live_price_stream_loop, context=spot_context)
    except KeyboardInterrupt:
        pass

    assert accepted == [("EURUSD", now_ms, "CTRADER:DEMO:47784297")]
    assert connector.LIVE_TICKS["EURUSD"]["bid"] == 1.14634
    # The burst uses the periodic account check; neither the old quote nor
    # the new quote causes another durable lookup in the same second.
    assert len(selections) == 1


def test_stream_notices_account_switch_without_a_fresh_spot(monkeypatch, spot_context):
    """Periodic selection checks still stop an old account's idle stream."""
    account_a = AccountIdentity("47784297", "demo", spot_context.selection_revision)
    account_b = AccountIdentity("47810571", "demo")
    closed = []
    socket = type("FakeSocket", (), {"settimeout": lambda self, _: None,
                                      "close": lambda self: closed.append(True)})()
    opens = []
    selections = iter([account_a, account_b])
    observed_selections = []
    clock = iter([1.0, 2.0, 4.0])

    def open_socket(*_args):
        opens.append(True)
        if len(opens) > 1:
            pytest.fail('old worker must not reconnect after an account switch')
        return socket

    def select():
        identity = next(selections)
        observed_selections.append(identity)
        return identity

    monkeypatch.setattr(connector, "get_ctrader_config", lambda: {"account_id": "47784297", "env": "demo"})
    monkeypatch.setattr(connector, "open_ctrader_json_socket", open_socket)
    monkeypatch.setattr(connector, "authorize_ctrader_socket", lambda *_: None)
    monkeypatch.setattr(connector, "fetch_ctrader_symbol_details", lambda *_: [])
    monkeypatch.setattr(connector, "resolve_ctrader_symbol", lambda _details, symbol: {"symbol_id": 1} if symbol == "EURUSD" else None)
    monkeypatch.setattr(connector, "send_ctrader_request", lambda *_: {})
    monkeypatch.setattr(connector, "websocket_recv_text", lambda _: (_ for _ in ()).throw(socket_module.timeout()))
    monkeypatch.setattr(connector, "selected_identity", select)
    # Only the socket-loop clock is controlled, not PostgreSQL/pool timers.
    monkeypatch.setattr(connector, "time", SimpleNamespace(
        time=time.time, sleep=time.sleep, monotonic=lambda: next(clock)))
    monkeypatch.setattr(connector, 'LIVE_TICKS', {'EURUSD': {'unchanged': True}})

    run(spot_context, connector.ctrader_live_price_stream_loop, context=spot_context)

    assert len(opens) == 1
    assert closed == [True]
    assert observed_selections == [account_a, account_b]
    assert connector.LIVE_TICKS == {'EURUSD': {'unchanged': True}}


def test_fresh_spot_burst_does_not_query_durable_selection_per_tick(monkeypatch, spot_context):
    """A burst of usable spots must not recreate the account-DB bottleneck."""
    account = AccountIdentity("47784297", "demo", spot_context.selection_revision)
    socket = type("FakeSocket", (), {"settimeout": lambda self, _: None,
                                      "close": lambda self: None})()
    now_ms = int(time.time() * 1000)
    events = iter([
        {"payloadType": connector.PAYLOAD_SPOT_EVENT,
         "payload": {"symbolId": 1, "bid": 114634 + i, "ask": 114636 + i,
                     "timestamp": now_ms + i}}
        for i in range(20)
    ])
    selections = []

    def recv(_socket):
        try:
            return json.dumps(next(events))
        except StopIteration:
            raise KeyboardInterrupt

    def select():
        selections.append(account)
        return account

    monkeypatch.setattr(connector, "get_ctrader_config", lambda: {"account_id": "47784297", "env": "demo"})
    monkeypatch.setattr(connector, "open_ctrader_json_socket", lambda *_: socket)
    monkeypatch.setattr(connector, "authorize_ctrader_socket", lambda *_: None)
    monkeypatch.setattr(connector, "fetch_ctrader_symbol_details", lambda *_: [])
    monkeypatch.setattr(connector, "resolve_ctrader_symbol", lambda _details, symbol: {"symbol_id": 1} if symbol == "EURUSD" else None)
    monkeypatch.setattr(connector, "send_ctrader_request", lambda *_: {})
    monkeypatch.setattr(connector, "websocket_recv_text", recv)
    monkeypatch.setattr(connector, "selected_identity", select)
    monkeypatch.setattr(connector, "LIVE_TICKS", {"EURUSD": {}})

    try:
        run(spot_context, connector.ctrader_live_price_stream_loop, context=spot_context)
    except KeyboardInterrupt:
        pass

    assert connector.LIVE_TICKS["EURUSD"]["bid"] == 1.14653
    assert connector.LIVE_TICKS["EURUSD"]["account_scope"] == "CTRADER:DEMO:47784297"
    assert len(selections) == 1


def test_spot_worker_without_admitted_context_has_no_authoritative_effect(monkeypatch):
    from contextvars import Context
    from unittest.mock import Mock
    from startup_recovery.types import RecoveryError

    monkeypatch.setattr(connector, 'LIVE_TICKS', {'EURUSD': {'unchanged': True}})
    monkeypatch.setattr(connector, 'LIVE_PRICE_LAST_ERROR', 'preserved')
    forbidden = {}
    for name in ('open_ctrader_json_socket', 'send_ctrader_request',
                 'websocket_recv_text', 'update_live_tick', 'selected_identity'):
        forbidden[name] = Mock(side_effect=AssertionError('unadmitted worker reached ' + name))
        monkeypatch.setattr(connector, name, forbidden[name])
    with pytest.raises(RecoveryError, match='^RECOVERY_TOKEN_MISSING$'):
        Context().run(connector.ctrader_live_price_stream_loop)
    for boundary in forbidden.values():
        boundary.assert_not_called()
    assert connector.LIVE_TICKS == {'EURUSD': {'unchanged': True}}
    assert connector.LIVE_PRICE_LAST_ERROR == 'preserved'
