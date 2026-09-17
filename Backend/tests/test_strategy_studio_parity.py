from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ctrader_account_context import AccountIdentity
import routes.strategy_studio as route_module
from services import strategy_studio_parity as parity
from services.strategy_engine.market_facts import build_market_facts


class FakeModule:
    SL_BUFFER_POINTS = 50
    MIN_SL_POINTS = 100
    TARGET_RR = 1.9

    @staticmethod
    def candidates(_frame15, frame5, _start, _end, _settings):
        event = {
            "timestamp": frame5.index[0],
            "event_type": "BOS",
            "direction": "BULLISH",
            "broken_level": 1.1000,
            "close": 1.1010,
            "event_invalidation_swing": {"type": "LOW", "price": 1.0950},
        }
        yield event, frame5.index[0], frame5.iloc[:1], "BUY", 0.006, True, {}

    @staticmethod
    def evaluate_event(event, timestamp, _prefix, frame5, side, _leg, _settings, _end, previous_close=None):
        del previous_close
        confirmation_time = frame5.index[1]
        trade = {
            "side": side,
            "event_timestamp": pd.Timestamp(timestamp).isoformat(),
            "m5_confirmation_timestamp": pd.Timestamp(confirmation_time).isoformat(),
            "entry": float(frame5.iloc[1].Close),
            "sl": 1.0945,
            "tp2": 1.11365,
            "broken_level": float(event["broken_level"]),
        }
        return trade, None, {}

    @staticmethod
    def _fixed_levels(side, entry, invalidation):
        stop = float(invalidation["price"]) - 0.0005 if side == "BUY" else float(invalidation["price"]) + 0.0005
        risk = abs(float(entry) - stop)
        sign = 1 if side == "BUY" else -1
        return {"ok": True, "stop_loss": stop, "tp2": float(entry) + sign * 1.9 * risk}


class FakeGoldModule(FakeModule):
    SL_BUFFER_POINTS = 50
    MIN_SL_POINTS = 100
    TARGET_RR = 1.9

    @staticmethod
    def candidates(_frame15, frame5, _start, _end, _settings):
        event = {
            "timestamp": frame5.index[0],
            "event_type": "CHOCH",
            "direction": "BEARISH",
            "broken_level": 4300.0,
            "close": 4290.0,
            "event_invalidation_swing": {"type": "HIGH", "price": 4310.0},
        }
        yield event, frame5.index[0], frame5.iloc[:1], "SELL", 15.0, True, {}

    @staticmethod
    def evaluate_event(event, timestamp, _prefix, frame5, side, _leg, _settings, _end, previous_close=None):
        del previous_close
        confirmation_time = frame5.index[1]
        entry = float(frame5.iloc[1].Close)
        stop = 4310.5
        trade = {
            "side": side,
            "event_timestamp": pd.Timestamp(timestamp).isoformat(),
            "m5_confirmation_timestamp": pd.Timestamp(confirmation_time).isoformat(),
            "entry": entry,
            "sl": stop,
            "tp2": entry - 1.9 * abs(entry - stop),
            "broken_level": float(event["broken_level"]),
        }
        return trade, None, {}

    @staticmethod
    def _fixed_levels(side, entry, invalidation):
        stop = float(invalidation["price"]) + 0.5 if side == "SELL" else float(invalidation["price"]) - 0.5
        risk = abs(float(entry) - stop)
        sign = 1 if side == "BUY" else -1
        return {"ok": True, "stop_loss": stop, "tp2": float(entry) + sign * 1.9 * risk}


def _frame(rows, start="2026-09-14T14:35:00Z"):
    return pd.DataFrame(
        rows,
        columns=["Open", "High", "Low", "Close"],
        index=pd.date_range(start, periods=len(rows), freq="5min", tz="UTC"),
    )


def _client():
    app = FastAPI()
    app.include_router(route_module.router)
    return TestClient(app)


def test_eurusd_buy_bos_entry_parity(monkeypatch):
    frame = _frame([
        (1.0990, 1.1020, 1.0985, 1.1010),
        (1.1010, 1.1040, 1.1005, 1.1030),
    ])
    monkeypatch.setattr(parity, "_legacy_module", lambda _symbol: ("EURUSD", FakeModule, 5))

    class Event:
        timestamp = frame.index[0]
        direction = "BUY"
        event_type = "BOS"
        broken_level = 1.1000
        invalidation_price = 1.0950
        trigger_close = 1.1010

    class Timeline:
        def timestamps(self): return list(frame.index)
        def structure_event(self, timestamp): return Event() if timestamp == frame.index[0] else None
        def candle(self, timestamp):
            row = frame.loc[timestamp]
            span = row.High - row.Low
            return SimpleNamespace(open=row.Open, high=row.High, low=row.Low, close=row.Close, body_percent=abs(row.Close-row.Open)/span*100)
        def trend(self, _timestamp): return SimpleNamespace(bos_choch_direction=None, ema50_direction=None, ema200_direction=None, swing_structure_direction=None)
        def next_timestamp(self, timestamp): return frame.index[1] if timestamp == frame.index[0] else None
        def opposite_swing(self, *_args): return None

    monkeypatch.setattr(parity, "build_market_facts", lambda *_args, **_kwargs: Timeline())
    report = parity.compare_v3b_entry_decisions("EURUSD", frame, account_scope="CTRADER:DEMO:47810571")
    assert report["match"] is True
    assert report["compared_setups"] == 1
    assert report["mismatches"] == []
    assert report["legacy"] == report["studio"]
    assert report["post_entry_management_compared"] is False


def test_xauusd_sell_choch_entry_parity(monkeypatch):
    frame = _frame([
        (4310.0, 4312.0, 4288.0, 4290.0),
        (4290.0, 4292.0, 4278.0, 4280.0),
    ])
    monkeypatch.setattr(parity, "_legacy_module", lambda _symbol: ("XAUUSD", FakeGoldModule, 2))

    class Event:
        timestamp = frame.index[0]
        direction = "SELL"
        event_type = "CHOCH"
        broken_level = 4300.0
        invalidation_price = 4310.0
        trigger_close = 4290.0

    class Timeline:
        def timestamps(self): return list(frame.index)
        def structure_event(self, timestamp): return Event() if timestamp == frame.index[0] else None
        def candle(self, timestamp):
            row = frame.loc[timestamp]
            span = row.High - row.Low
            return SimpleNamespace(open=row.Open, high=row.High, low=row.Low, close=row.Close, body_percent=abs(row.Close-row.Open)/span*100)
        def trend(self, _timestamp): return SimpleNamespace(bos_choch_direction=None, ema50_direction=None, ema200_direction=None, swing_structure_direction=None)
        def next_timestamp(self, timestamp): return frame.index[1] if timestamp == frame.index[0] else None
        def opposite_swing(self, *_args): return None

    monkeypatch.setattr(parity, "build_market_facts", lambda *_args, **_kwargs: Timeline())
    report = parity.compare_v3b_entry_decisions("XAUUSD", frame, account_scope="CTRADER:DEMO:47810571")
    assert report["match"] is True
    assert report["compared_setups"] == 1
    assert report["mismatches"] == []
    assert report["legacy"] == report["studio"]


def test_wait_period_has_no_false_trade(monkeypatch):
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])

    class EmptyModule:
        SL_BUFFER_POINTS = 50
        MIN_SL_POINTS = 100
        TARGET_RR = 1.9
        @staticmethod
        def candidates(*_args, **_kwargs): return []

    monkeypatch.setattr(parity, "_legacy_module", lambda _symbol: ("EURUSD", EmptyModule, 5))
    timeline = build_market_facts({"5m": frame}, "EURUSD", "5m", None)
    monkeypatch.setattr(parity, "build_market_facts", lambda *_args, **_kwargs: timeline)
    report = parity.compare_v3b_entry_decisions("EURUSD", frame, account_scope="CTRADER:DEMO:47810571")
    assert report["compared_setups"] == 0
    assert report["legacy"] == []
    assert report["studio"] == []
    assert report["mismatches"] == []
    assert report["post_entry_management_compared"] is False
    assert "tp1_partial_close" not in report["parity_scope"]


def test_account_scope_is_required():
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])
    with pytest.raises(ValueError, match="ACCOUNT_SCOPE"):
        parity.compare_v3b_entry_decisions("EURUSD", frame, account_scope="")


def test_parity_endpoint_is_authenticated_read_only_and_account_scoped(monkeypatch):
    actor = SimpleNamespace(id="1", email="owner@example.com")
    identity = AccountIdentity("47810571", "demo")
    frame = _frame([
        (1.1000, 1.1005, 1.0995, 1.1001),
        (1.1001, 1.1006, 1.0998, 1.1002),
    ])
    sentinel = {
        "match": True,
        "compared_setups": 0,
        "legacy": [],
        "studio": [],
        "mismatches": [],
        "parity_scope": list(parity.PARITY_SCOPE),
        "post_entry_management_compared": False,
        "account_scope": identity.scope,
        "legacy_only_level_policy": {"sl_buffer_points": 50, "minimum_sl_points": 100, "target_rr": 1.9},
    }

    monkeypatch.setattr(route_module, "current_user", lambda _request: actor)
    monkeypatch.setattr(route_module, "selected_identity", lambda: identity)
    monkeypatch.setattr(route_module, "load_simulation_5m", lambda *args, **kwargs: frame)
    monkeypatch.setattr(route_module, "compare_v3b_entry_decisions", lambda *args, **kwargs: sentinel)
    monkeypatch.setattr(
        route_module,
        "get_studio_live_state",
        lambda owner: {
            "owner_id": owner,
            "enabled": False,
            "enabled_strategy_id": None,
            "enabled_at": None,
            "updated_at": None,
        },
    )

    response = _client().post(
        "/strategy-studio/parity/run",
        json={
            "symbol": "EURUSD",
            "start": "2026-09-14T14:35:00Z",
            "end": "2026-09-14T14:45:00Z",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["account_scope"] == identity.scope
    assert body["post_entry_management_compared"] is False
    assert body["live_handoff_enabled"] is False

    source = Path(route_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "place_market_order",
        "execute_live_order_core",
        "claim_submission(",
        "claim_strategy_submission(",
        "save_auto_trade",
        "set_auto_trade",
    ):
        assert forbidden not in source


def test_live_status_reports_gate_without_enabling_it(monkeypatch):
    actor = SimpleNamespace(id="1", email="owner@example.com")
    monkeypatch.setattr(route_module, "current_user", lambda _request: actor)
    monkeypatch.setattr(
        route_module,
        "get_studio_live_state",
        lambda owner: {
            "owner_id": owner,
            "enabled": False,
            "enabled_strategy_id": None,
            "enabled_at": None,
            "updated_at": None,
        },
    )
    response = _client().get("/strategy-studio/live-status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["parity_status"] == "REQUIRES_VERIFICATION"
    assert body["entry_parity_only"] is True
    assert body["post_entry_management_compared"] is False
