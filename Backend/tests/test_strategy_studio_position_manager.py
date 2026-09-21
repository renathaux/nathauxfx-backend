from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ctrader_account_context import AccountIdentity
from db import Base
from models import StrategySetupLifecycle, StrategyStudioLiveState
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection


@pytest.fixture
def db_session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def definition(*, tp1_enabled=True, close_percent=50.0, protection_r=0.5, target_r=1.0,
               target_basis="SL_DISTANCE", protection_mode="FIXED", protection_steps=None):
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": ["CLOSE_BEYOND"],
            "minimum_body_percent": None,
            "minimum_distance_pips": None,
        },
        "confirmation": {"rules": ["NEXT_SAME_DIRECTION"], "minimum_body_percent": None},
        "entry": {"method": "CONFIRMATION_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": 0.0, "fixed_distance": None},
        "tp1": {
            "enabled": tp1_enabled,
            "target_r": target_r if tp1_enabled else None,
            "target_basis": target_basis if tp1_enabled else "SL_DISTANCE",
            "close_percent": close_percent if tp1_enabled else None,
            "protection_r": protection_r if (tp1_enabled and protection_mode == "FIXED") else None,
            "protection_mode": protection_mode if tp1_enabled else "FIXED",
            "protection_steps": list(protection_steps or []) if tp1_enabled else [],
        },
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def seed_lifecycle(factory, *, account_id="acct-a", account_scope="CTRADER:DEMO:acct-a",
                   strategy_id="strat-live", direction="BUY", tp1_enabled=True,
                   suspended=False, tp1_done=False, broker_position_id="pos-1",
                   strategy_definition=None):
    now = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
    with factory() as session:
        session.add(StrategySetupLifecycle(
            setup_id="setup-1",
            owner_id="owner-1",
            strategy_id=strategy_id,
            account_id=account_id,
            account_scope=account_scope,
            symbol="EURUSD",
            direction=direction,
            status="CONSUMED",
            definition_snapshot=(strategy_definition or definition(tp1_enabled=tp1_enabled)),
            initial_volume_units=10000,
            broker_position_id=broker_position_id,
            tp1_completed_at=(now if tp1_done else None),
            protection_applied_at=None,
            management_suspended_at=(now if suspended else None),
            updated_at=now,
        ))
        session.commit()


def open_position(*, price=1.1060, side="BUY", volume=10000, position_id="pos-1",
                  entry=1.1000, sl=1.0950, tp2=1.1100):
    return {
        "position_id": position_id,
        "broker_position_id": position_id,
        "symbol": "EURUSD",
        "side": side,
        "entry": entry,
        "stop_loss": sl,
        "sl": sl,
        "take_profit": tp2,
        "tp2": tp2,
        "volume": volume,
        "volume_units": volume,
        "current_price": price,
    }


def prices(*, bid=1.1060, ask=1.1062):
    return {"EURUSD": {"bid": bid, "ask": ask}}


def test_switching_away_suspends_management_without_broker_mutation(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory)
    close = monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("must not close on suspend"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("must not modify SL on suspend"))

    result = manager.suspend_account_management(
        "owner-1",
        AccountIdentity("acct-a", "demo"),
        [open_position()],
        session_factory=db_session_factory,
    )
    assert result["suspended"] == 1
    with db_session_factory() as session:
        row = session.get(StrategySetupLifecycle, "setup-1")
        assert row.management_suspended_at is not None
        assert row.status == "CONSUMED"


def test_inactive_account_is_never_managed(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory, account_scope="CTRADER:DEMO:acct-b", account_id="acct-b")
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("inactive account cannot mutate broker"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("inactive account cannot mutate broker"))

    result = manager.manage_selected_account_positions(
        "owner-1",
        AccountIdentity("acct-a", "demo"),
        [open_position()],
        prices(),
        session_factory=db_session_factory,
    )
    assert result["actions"] == []
    assert result["status"] == "INACTIVE_ACCOUNT_NOT_MANAGED"


def test_resume_beyond_tp1_closes_partial_once_without_retroactive_protection(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory, suspended=True)
    closed = []
    protected = []
    monkeypatch.setattr(manager, "close_position", lambda position_id, volume=None: closed.append((position_id, volume)) or {"ok": True})
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: protected.append((a, k)) or {"ok": True})

    result = manager.resume_account_management(
        "owner-1",
        AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1060)],
        prices(bid=1.1060),
        session_factory=db_session_factory,
    )
    assert result["actions"][0]["action"] == "TP1_CATCHUP_PARTIAL_CLOSE"
    assert closed == [("pos-1", 5000)]
    assert protected == []
    with db_session_factory() as session:
        row = session.get(StrategySetupLifecycle, "setup-1")
        assert row.tp1_completed_at is not None
        assert row.protection_applied_at is None
        assert row.management_suspended_at is None

    manager.resume_account_management(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1070)],
        prices(bid=1.1070), session_factory=db_session_factory,
    )
    assert closed == [("pos-1", 5000)]


def test_resume_below_tp1_does_nothing_and_clears_suspension(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory, suspended=True)
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("TP1 not reached"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("TP1 not reached"))
    result = manager.resume_account_management(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1030)],
        prices(bid=1.1030), session_factory=db_session_factory,
    )
    assert result["actions"] == []
    with db_session_factory() as session:
        assert session.get(StrategySetupLifecycle, "setup-1").management_suspended_at is None


def test_continuous_tp1_hit_closes_partial_then_applies_configured_protection(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory)
    sequence = []
    monkeypatch.setattr(manager, "close_position", lambda position_id, volume=None: sequence.append(("close", position_id, volume)) or {"ok": True})
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda position_id, stop_loss, take_profit_price=None: sequence.append(("protect", position_id, stop_loss, take_profit_price)) or {"ok": True})

    result = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1060)],
        prices(bid=1.1060), session_factory=db_session_factory,
    )
    assert [item[0] for item in sequence] == ["close", "protect"]
    assert sequence[0] == ("close", "pos-1", 5000)
    assert sequence[1][2] == pytest.approx(1.1025)
    assert result["actions"][0]["action"] == "TP1_PARTIAL_CLOSE_AND_PROTECT"
    with db_session_factory() as session:
        row = session.get(StrategySetupLifecycle, "setup-1")
        assert row.tp1_completed_at is not None
        assert row.protection_applied_at is not None


def test_tp2_based_tp1_waits_for_percentage_of_tp2_path(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    strategy = definition(
        close_percent=40.0,
        target_r=0.70,
        target_basis="TP2_DISTANCE",
        protection_r=0.50,
    )
    seed_lifecycle(db_session_factory, strategy_definition=strategy)
    closed = []
    protected = []
    monkeypatch.setattr(
        manager, "close_position",
        lambda position_id, volume=None: closed.append((position_id, volume)) or {"ok": True},
    )
    monkeypatch.setattr(
        manager, "modify_position_stop_loss",
        lambda *a, **k: protected.append((a, k)) or {"ok": True},
    )

    before = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1060, entry=1.1000, sl=1.0950, tp2=1.1100)],
        prices(bid=1.1060), session_factory=db_session_factory,
    )
    assert before["actions"] == []
    assert closed == []

    hit = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1070, entry=1.1000, sl=1.0950, tp2=1.1100)],
        prices(bid=1.1070), session_factory=db_session_factory,
    )
    assert closed == [("pos-1", 4000)]
    assert protected[0][0][1] == pytest.approx(1.1050)
    assert hit["actions"][0]["protected_sl"] == pytest.approx(1.1050)


def test_step_protection_advances_70_50_80_60_90_70(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    steps = [
        {"trigger_percent": 70, "secure_percent": 50},
        {"trigger_percent": 80, "secure_percent": 60},
        {"trigger_percent": 90, "secure_percent": 70},
    ]
    strategy = definition(
        close_percent=40.0,
        target_r=0.70,
        target_basis="TP2_DISTANCE",
        protection_mode="TP2_STEPS",
        protection_steps=steps,
    )
    seed_lifecycle(db_session_factory, strategy_definition=strategy)
    closed = []
    protected = []
    monkeypatch.setattr(
        manager, "close_position",
        lambda position_id, volume=None: closed.append((position_id, volume)) or {"ok": True},
    )
    monkeypatch.setattr(
        manager, "modify_position_stop_loss",
        lambda position_id, stop_loss, take_profit_price=None:
            protected.append((position_id, stop_loss, take_profit_price)) or {"ok": True},
    )

    first = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1070, entry=1.1000, sl=1.0950, tp2=1.1100)],
        prices(bid=1.1070), session_factory=db_session_factory,
    )
    assert closed == [("pos-1", 4000)]
    assert protected[-1][1] == pytest.approx(1.1050)
    assert first["actions"][0]["trigger_percent"] == 70
    assert first["actions"][0]["secure_percent"] == 50

    second = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1080, entry=1.1000, sl=1.1050, tp2=1.1100)],
        prices(bid=1.1080), session_factory=db_session_factory,
    )
    assert second["actions"][0]["action"] == "TP2_STEP_PROTECTION"
    assert second["actions"][0]["secure_percent"] == 60
    assert protected[-1][1] == pytest.approx(1.1060)

    third = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"),
        [open_position(price=1.1090, entry=1.1000, sl=1.1060, tp2=1.1100)],
        prices(bid=1.1090), session_factory=db_session_factory,
    )
    assert third["actions"][0]["secure_percent"] == 70
    assert protected[-1][1] == pytest.approx(1.1070)


def test_tp1_completed_prevents_duplicate_partial_close(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory, tp1_done=True)
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("TP1 already completed"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("no duplicate protection action"))
    result = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1080)],
        prices(bid=1.1080), session_factory=db_session_factory,
    )
    assert result["actions"] == []


def test_tp1_disabled_has_no_manager_action(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory, tp1_enabled=False)
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("TP1 disabled"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("TP1 disabled"))
    result = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1090)],
        prices(bid=1.1090), session_factory=db_session_factory,
    )
    assert result["actions"] == []


def test_manual_broker_close_terminalizes_lifecycle_without_new_trade(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory)
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: pytest.fail("manager must not reopen/close absent position"))
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("manager must not mutate absent position"))
    result = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"), [], prices(),
        session_factory=db_session_factory,
    )
    assert result["terminalized"] == 1
    with db_session_factory() as session:
        assert session.get(StrategySetupLifecycle, "setup-1").status == "CLOSED"


def test_ambiguous_partial_close_fails_closed(db_session_factory, monkeypatch):
    from services import strategy_studio_position_manager as manager

    seed_lifecycle(db_session_factory)
    monkeypatch.setattr(manager, "close_position", lambda *a, **k: {"ok": False, "broker_result": "AMBIGUOUS", "broker_order_sent": True})
    monkeypatch.setattr(manager, "modify_position_stop_loss", lambda *a, **k: pytest.fail("no protection after ambiguous close"))
    result = manager.manage_selected_account_positions(
        "owner-1", AccountIdentity("acct-a", "demo"), [open_position(price=1.1060)],
        prices(bid=1.1060), session_factory=db_session_factory,
    )
    assert result["actions"][0]["status"] == "RECONCILIATION_REQUIRED"
    with db_session_factory() as session:
        row = session.get(StrategySetupLifecycle, "setup-1")
        assert row.status == "RECONCILIATION_REQUIRED"
        assert row.tp1_completed_at is None


def test_live_active_strategy_is_locked_while_selected_account_position_is_open(db_session_factory, monkeypatch):
    from services import strategy_studio_service as studio
    import ctrader_account_context

    now = datetime(2026, 9, 17, 15, 0, tzinfo=timezone.utc)
    strategy = definition()
    with db_session_factory() as session:
        session.add(SavedStrategy(
            strategy_id="strat-live", owner_id="owner-1", name="Live Strategy",
            schema_version=1, definition_json=strategy, created_at=now, updated_at=now,
        ))
        session.add(StrategyStudioSelection(
            owner_id="owner-1", strategy_id="strat-live", activated_at=now, updated_at=now,
        ))
        session.add(StrategyStudioLiveState(
            owner_id="owner-1", enabled=True, enabled_strategy_id="strat-live",
            enabled_at=now, updated_at=now,
        ))
        session.commit()
    seed_lifecycle(db_session_factory, strategy_id="strat-live")
    monkeypatch.setattr(ctrader_account_context, "selected_identity", lambda: AccountIdentity("acct-a", "demo"))

    item = studio.get_strategy("owner-1", "strat-live", db_session_factory)
    assert item["locked"] is True
    assert item["live_handoff_enabled"] is True
    with pytest.raises(studio.StrategyStudioConflict, match="locked"):
        studio.deactivate_strategy("owner-1", "strat-live", True, db_session_factory)
    clone = studio.clone_strategy("owner-1", "strat-live", "Clone", db_session_factory)
    assert clone["locked"] is False
