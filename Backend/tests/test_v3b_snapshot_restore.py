from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import ForexExecutionSnapshot
import ctrader_connector as connector


@pytest.fixture
def snapshots(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshots.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


def add_snapshot(factory, *, account="47784297", position="42", direction="BUY",
                 symbol="EURUSD", environment="demo", attempted=None):
    attempted = attempted or datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    payload = {
        "strategy_execution_profile": "V3B_M5_FROZEN", "entry": 1.1,
        "sl": 1.09, "tp1": 1.1133, "tp2": 1.119,
        "protected_sl_price": 1.1114, "event_id": "event-v3b",
        "setup_identity": {"indicator_event_id": "event-v3b"},
    }
    with factory() as session:
        session.add(ForexExecutionSnapshot(
            snapshot_id=f"snapshot-{account}-{position}-{direction}", snapshot_version=2,
            production_sha="test", backend_session_id="test", symbol=symbol,
            account_id=account, broker_environment=environment, direction=direction,
            event_id="event-v3b", order_attempted_at=attempted,
            client_order_id=f"client-{position}", broker_order_id=f"order-{position}",
            position_id=position, broker_response_at=attempted,
            snapshot_json=payload, created_at=attempted,
        ))
        session.commit()


def broker_position(position="42", direction="BUY"):
    opened = datetime(2026, 9, 16, 12, 1, tzinfo=timezone.utc)
    return {"position_id": position, "symbol": "EURUSD", "side": direction,
            "entry": 1.1, "opened_at": opened.timestamp(), "broker_order_id": f"order-{position}"}


def test_exact_account_position_snapshot_restores_v3b(snapshots):
    from services.forex_observability_service import find_v3b_snapshot_for_position
    add_snapshot(snapshots)
    found = find_v3b_snapshot_for_position(
        broker_position(), "47784297", "demo", session_factory=snapshots)
    assert found["strategy_execution_profile"] == "V3B_M5_FROZEN"
    assert found["protected_sl_price"] == 1.1114
    assert found["source_indicator_event_id"] == "event-v3b"


def test_ctrader_millisecond_open_timestamp_restores_exact_snapshot(snapshots):
    from services.forex_observability_service import find_v3b_snapshot_for_position
    add_snapshot(snapshots)
    position = broker_position()
    position["opened_at"] = int(position["opened_at"] * 1000)
    found = find_v3b_snapshot_for_position(
        position, "47784297", "demo", session_factory=snapshots)
    assert found is not None
    assert found["execution_snapshot_id"] == "snapshot-47784297-42-BUY"


@pytest.mark.parametrize("account,position,direction,environment", [
    ("47810571", "42", "BUY", "demo"),
    ("47784297", "43", "BUY", "demo"),
    ("47784297", "42", "SELL", "demo"),
    ("47784297", "42", "BUY", "live"),
])
def test_snapshot_from_other_identity_is_rejected(snapshots, account, position,
                                                   direction, environment):
    from services.forex_observability_service import find_v3b_snapshot_for_position
    add_snapshot(snapshots, account=account, position=position,
                 direction=direction, environment=environment)
    assert find_v3b_snapshot_for_position(
        broker_position(), "47784297", "demo", session_factory=snapshots) is None


def test_snapshot_outside_open_time_is_rejected(snapshots):
    from services.forex_observability_service import find_v3b_snapshot_for_position
    add_snapshot(snapshots, attempted=datetime(2026, 9, 15, 12, tzinfo=timezone.utc))
    assert find_v3b_snapshot_for_position(
        broker_position(), "47784297", "demo", session_factory=snapshots) is None


def test_ambiguous_exact_snapshots_are_rejected(snapshots):
    from services.forex_observability_service import find_v3b_snapshot_for_position
    add_snapshot(snapshots)
    with snapshots() as session:
        row = session.query(ForexExecutionSnapshot).one()
        session.add(ForexExecutionSnapshot(
            snapshot_id="duplicate", snapshot_version=2, production_sha="test",
            backend_session_id="test", symbol=row.symbol, account_id=row.account_id,
            broker_environment=row.broker_environment, direction=row.direction,
            event_id=row.event_id, order_attempted_at=row.order_attempted_at,
            client_order_id=row.client_order_id, broker_order_id=row.broker_order_id,
            position_id=row.position_id, broker_response_at=row.broker_response_at,
            snapshot_json=row.snapshot_json, created_at=row.created_at,
        ))
        session.commit()
    assert find_v3b_snapshot_for_position(
        broker_position(), "47784297", "demo", session_factory=snapshots) is None


def test_broker_response_updates_only_its_exact_snapshot(snapshots):
    from services.forex_observability_service import record_execution_response_safely
    add_snapshot(snapshots, account="47784297", position="41")
    add_snapshot(snapshots, account="47810571", position="42")
    with snapshots() as session:
        assert session.query(ForexExecutionSnapshot).count() == 2
    assert record_execution_response_safely(
        "EURUSD", {"position_id": "new-position", "order_id": "new-order"},
        snapshot_id="snapshot-47784297-41-BUY", session_factory=snapshots)
    with snapshots() as session:
        first = session.query(ForexExecutionSnapshot).filter_by(snapshot_id="snapshot-47784297-41-BUY").one()
        second = session.query(ForexExecutionSnapshot).filter_by(snapshot_id="snapshot-47810571-42-BUY").one()
        assert first.position_id == "new-position"
        assert second.position_id == "42"


def test_empty_worker_without_exact_snapshot_mirrors_position_without_legacy_management(
        monkeypatch, snapshots):
    import api
    from services import account_execution_coordination as coordination
    from services import forex_observability_service as observability
    state = {"active_account_id": "47784297", "active_account_env": "demo",
             "_durable_selection_authoritative": True}
    monkeypatch.setattr(connector, "load_ctrader_account_settings", lambda: dict(state))
    monkeypatch.setattr(observability, "SessionLocal", snapshots)
    add_snapshot(snapshots, account="47810571")
    monkeypatch.setattr(api, "LIVE_ACTIVE_ORDERS", {"EURUSD": None, "XAUUSD": None})
    monkeypatch.setattr(api, "LIVE_TRADE_HISTORY", [])
    monkeypatch.setattr(api, "LIVE_ACCOUNT_STATE", {"connected": True, "mode": "demo", "broker": "ctrader"})
    monkeypatch.setattr(api, "sync_ctrader_account_state", lambda: None)
    monkeypatch.setattr(api, "get_ctrader_position_fetch_error", lambda: None)
    monkeypatch.setattr(api, "get_live_prices", lambda: {})
    monkeypatch.setattr(api, "get_ctrader_symbol_risk_metadata", lambda *a, **kw: {})
    monkeypatch.setattr(connector, "get_ctrader_symbol_risk_metadata", lambda *a, **kw: {})
    monkeypatch.setattr(api, "get_signal_trade_plan", lambda symbol: {})
    monkeypatch.setattr(api, "save_live_backup", lambda: None)
    monkeypatch.setattr(coordination, "exclude_test_positions", lambda session, account, rows: rows)
    monkeypatch.setattr(api, "update_live_trade_tp_protection",
                        lambda row: pytest.fail("unmatched broker position reached legacy management"))
    monkeypatch.setattr(api, "get_open_positions", lambda: [{
        **broker_position(), "volume": 1000, "current_price": 1.101,
        "sl": 1.09, "tp": 1.119, "profit": 1,
    }])
    api.sync_live_positions()
    mirrored = api.LIVE_ACTIVE_ORDERS["EURUSD"]
    assert mirrored["position_id"] == "42"
    assert mirrored["account_scope"] == "CTRADER:DEMO:47784297"
    assert mirrored["management_paused"] is True
