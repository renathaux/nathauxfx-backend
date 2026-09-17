from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import Base
from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def valid_definition():
    return {
        "schema_version": 1,
        "symbols": ["EURUSD"],
        "trading_timeframe": "5m",
        "trend": {"timeframe": None, "methods": []},
        "structure": {"trigger": "BOS_CHOCH", "break_validation": [], "minimum_body_percent": None, "minimum_distance_pips": None},
        "confirmation": {"rules": [], "minimum_body_percent": None},
        "entry": {"method": "BOS_CHOCH_CLOSE"},
        "stop_loss": {"method": "LAST_SWING", "buffer_pips": None, "fixed_distance": None},
        "tp1": {"enabled": False, "target_r": None, "close_percent": None, "protection_r": None},
        "tp2": {"method": "FIXED_R", "value": 2.0},
        "risk": {"method": "PERCENT_BALANCE", "value": 1.0},
    }


def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def test_saved_strategy_definition_is_durable():
    engine, sessions = session_factory()
    row = SavedStrategy(
        strategy_id="strat-test-1", owner_id="user:abc", name="Breakout",
        schema_version=1, definition_json=valid_definition(), created_at=NOW, updated_at=NOW,
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
        session.add(SavedStrategy(
            strategy_id="strat-test-1", owner_id="user:abc", name="Breakout",
            schema_version=1, definition_json=valid_definition(), created_at=NOW, updated_at=NOW,
        ))
        session.add(StrategyStudioSelection(
            owner_id="user:abc", strategy_id="strat-test-1", activated_at=NOW, updated_at=NOW,
        ))
        session.commit()
    with sessions() as session:
        assert session.get(StrategyStudioSelection, "user:abc").strategy_id == "strat-test-1"
    engine.dispose()
