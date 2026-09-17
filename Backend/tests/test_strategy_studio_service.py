from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
from services.strategy_studio_service import (
    StrategyStudioConflict,
    StrategyStudioNotFound,
    activate_strategy,
    clone_strategy,
    create_strategy,
    deactivate_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    update_strategy,
)

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def valid_definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {
            "trigger": "BOS_CHOCH",
            "break_validation": [],
            "minimum_body_percent": None,
            "minimum_distance_pips": None,
        },
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {
            "method": "LAST_SWING",
            "buffer_pips": None,
            "fixed_distance": None,
        },
        "tp1": {
            "enabled": False,
            "target_r": None,
            "close_percent": None,
            "protection_r": None,
        },
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_saved_strategy_definition_is_durable():
    engine, sessions = session_factory()
    row = SavedStrategy(
        strategy_id="strat-test-1",
        owner_id="user:abc",
        name="Breakout",
        schema_version=1,
        definition_json=valid_definition(),
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions() as session:
        session.add(row)
        session.commit()
    with sessions() as session:
        assert session.get(SavedStrategy, "strat-test-1").definition_json["trading_timeframe"] == "5m"
    engine.dispose()


def test_one_selection_per_owner():
    engine, sessions = session_factory()
    with sessions() as session:
        session.add(
            SavedStrategy(
                strategy_id="strat-test-1",
                owner_id="user:abc",
                name="Breakout",
                schema_version=1,
                definition_json=valid_definition(),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            StrategyStudioSelection(
                owner_id="user:abc",
                strategy_id="strat-test-1",
                activated_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    with sessions() as session:
        assert session.get(StrategyStudioSelection, "user:abc").strategy_id == "strat-test-1"
    engine.dispose()


def test_owner_cannot_read_another_owners_strategy():
    engine, sessions = session_factory()
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    with pytest.raises(StrategyStudioNotFound):
        get_strategy("user:b", created["strategy_id"], sessions)
    engine.dispose()


def test_clone_is_exact_inactive_copy():
    engine, sessions = session_factory()
    source = create_strategy("user:a", "Original", valid_definition(), sessions)
    activate_strategy("user:a", source["strategy_id"], True, sessions)
    clone = clone_strategy("user:a", source["strategy_id"], "Copy", sessions)
    assert clone["definition"] == source["definition"]
    assert clone["state"] == "INACTIVE"
    assert clone["strategy_id"] != source["strategy_id"]
    engine.dispose()


def test_activation_replaces_only_studio_selection():
    engine, sessions = session_factory()
    first = create_strategy("user:a", "A", valid_definition(), sessions)
    second = create_strategy("user:a", "B", valid_definition(), sessions)
    activate_strategy("user:a", first["strategy_id"], True, sessions)
    current = activate_strategy("user:a", second["strategy_id"], True, sessions)
    assert current["live_handoff_enabled"] is False
    assert current["state"] == "ACTIVE"
    assert get_strategy("user:a", first["strategy_id"], sessions)["state"] == "INACTIVE"
    engine.dispose()


def test_active_strategy_must_be_deactivated_before_delete():
    engine, sessions = session_factory()
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    activate_strategy("user:a", created["strategy_id"], True, sessions)
    with pytest.raises(StrategyStudioConflict, match="deactivate"):
        delete_strategy("user:a", created["strategy_id"], True, sessions)
    engine.dispose()


def test_delete_requires_confirmation_and_is_permanent():
    engine, sessions = session_factory()
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    with pytest.raises(StrategyStudioConflict, match="confirmation"):
        delete_strategy("user:a", created["strategy_id"], False, sessions)
    assert delete_strategy("user:a", created["strategy_id"], True, sessions) is True
    with pytest.raises(StrategyStudioNotFound):
        get_strategy("user:a", created["strategy_id"], sessions)
    engine.dispose()


def test_update_preserves_identity_and_normalizes_definition():
    engine, sessions = session_factory()
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    changed = valid_definition()
    changed["symbols"] = ["EURUSD", "XAUUSD"]
    updated = update_strategy("user:a", created["strategy_id"], "Renamed", changed, sessions)
    assert updated["strategy_id"] == created["strategy_id"]
    assert updated["name"] == "Renamed"
    assert updated["definition"]["symbols"] == ["EURUSD", "XAUUSD"]
    engine.dispose()


def test_list_is_owner_scoped_and_newest_first():
    engine, sessions = session_factory()
    create_strategy(
        "user:a",
        "A1",
        valid_definition(),
        sessions,
        now=datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc),
    )
    create_strategy(
        "user:b",
        "B1",
        valid_definition(),
        sessions,
        now=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
    )
    create_strategy(
        "user:a",
        "A2",
        valid_definition(),
        sessions,
        now=datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc),
    )
    items = list_strategies("user:a", sessions)
    assert [item["name"] for item in items] == ["A2", "A1"]
    assert all(item["live_handoff_enabled"] is False for item in items)
    engine.dispose()


def test_deactivate_requires_current_selection():
    engine, sessions = session_factory()
    first = create_strategy("user:a", "A", valid_definition(), sessions)
    second = create_strategy("user:a", "B", valid_definition(), sessions)
    activate_strategy("user:a", first["strategy_id"], True, sessions)
    with pytest.raises(StrategyStudioConflict, match="not the active"):
        deactivate_strategy("user:a", second["strategy_id"], True, sessions)
    result = deactivate_strategy("user:a", first["strategy_id"], True, sessions)
    assert result["state"] == "INACTIVE"
    engine.dispose()


def test_active_strategy_cannot_be_edited_until_deactivated():
    engine, sessions = session_factory()
    created = create_strategy("user:a", "A", valid_definition(), sessions)
    activate_strategy("user:a", created["strategy_id"], True, sessions)
    changed = valid_definition()
    changed["symbols"] = ["XAUUSD"]
    with pytest.raises(StrategyStudioConflict, match="deactivate"):
        update_strategy("user:a", created["strategy_id"], "Changed", changed, sessions)
    engine.dispose()
