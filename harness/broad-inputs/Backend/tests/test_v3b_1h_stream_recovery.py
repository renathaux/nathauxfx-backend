from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import IndicatorCandle, IndicatorEvent, IndicatorStreamState
from services import auto_trade_state_service
from services import indicator_event_stream_service as stream
from services import v3b_5m_stream_recovery as recovery


ACCOUNT_ID = "47810571"
SCOPE = "CTRADER:DEMO:47810571"
EUR_KEY = "EURUSD~09C2948873"


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery1h.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(recovery, "active_ctrader_stream_scope", lambda: SCOPE)
    yield Session


def _frame(start, periods, base=1.1):
    index = pd.date_range(start, periods=periods, freq="1h", tz="UTC")
    values = []
    for offset, _stamp in enumerate(index):
        price = base + offset * 0.0001
        values.append({
            "Open": price,
            "High": price + 0.0002,
            "Low": price - 0.0002,
            "Close": price + 0.00005,
        })
    return pd.DataFrame(values, index=index)


def _event(ts, level=1.1111):
    stamp = pd.Timestamp(ts).tz_convert("UTC")
    return {
        "event_type": "BOS",
        "direction": "BULLISH",
        "timestamp": stamp.isoformat(),
        "broken_level": level,
        "event_invalidation_swing": {
            "type": "LOW",
            "price": level - 0.001,
            "swing_time": stamp.isoformat(),
        },
    }


def _analyzer(frame, **_kwargs):
    if len(frame) < 4:
        return {"events": [], "bias": "NEUTRAL"}
    event_time = frame.index[-2]
    return {
        "events": [_event(event_time, level=float(frame.iloc[-2]["Close"]))],
        "bias": "BULLISH",
    }


def _seed_stream(Session, reason, start, periods=8):
    now = datetime.now(timezone.utc)
    frame = _frame(start, periods)
    with Session() as session:
        session.add(IndicatorStreamState(
            symbol=EUR_KEY,
            timeframe="1h",
            configuration_version=stream.CONFIGURATION_VERSION,
            status="RECONCILIATION_REQUIRED",
            origin_candle=frame.index[0].to_pydatetime(),
            activation_watermark=frame.index[-1].to_pydatetime(),
            last_processed_candle=frame.index[-1].to_pydatetime(),
            reconciliation_reason=reason,
            updated_at=now,
        ))
        for ts, candle in frame.iterrows():
            session.add(IndicatorCandle(
                symbol=EUR_KEY,
                timeframe="1h",
                candle_timestamp=ts.to_pydatetime(),
                open_price=float(candle.Open),
                high_price=float(candle.High),
                low_price=float(candle.Low),
                close_price=float(candle.Close),
                created_at=now,
            ))
        raw = _event(frame.index[-2], level=float(frame.iloc[-2]["Close"]))
        identity, event_id = stream.build_event_identity(raw, EUR_KEY, "1h", 0.00001)
        session.add(IndicatorEvent(
            event_id=event_id,
            symbol=EUR_KEY,
            timeframe="1h",
            candle_timestamp=frame.index[-2].to_pydatetime(),
            classification=identity["classification"],
            direction=identity["direction"],
            broken_level=float(raw["broken_level"]),
            opposite_swing=identity["opposite_swing"],
            identity=identity,
            payload={"event_id": event_id, "event_identity": identity},
            configuration_version=stream.CONFIGURATION_VERSION,
            is_historical=False,
            created_at=now,
        ))
        session.commit()
    return frame, event_id


def _request(dry_run):
    return recovery.RecoveryRequest(
        account_id=ACCOUNT_ID,
        symbol="EURUSD",
        timeframe="1h",
        storage_key=EUR_KEY,
        dry_run=dry_run,
    )


def test_1h_plan_supports_current_eurusd_reconciliation_class(session_factory):
    closed, _event_id = _seed_stream(
        session_factory,
        "late candle changed previously accepted structure events",
        "2026-09-15T10:00:00Z",
    )
    result = recovery.plan_recovery(
        _request(True),
        closed,
        session_factory=session_factory,
    )
    assert result["safe"] is True
    assert result["status_before"] == "RECONCILIATION_REQUIRED"
    assert result["timeframe"] == "1h"
    assert result["replacement_suffix_count"] >= 1


def test_1h_apply_rebuilds_ready_stream_with_live_auto_off(
    session_factory, monkeypatch
):
    closed, old_event_id = _seed_stream(
        session_factory,
        "late candle changed previously accepted structure events",
        "2026-09-15T10:00:00Z",
    )
    monkeypatch.setattr(
        auto_trade_state_service,
        "load_state",
        lambda **_kwargs: {"live_enabled": False},
    )
    result = recovery.apply_recovery(
        _request(False),
        closed,
        0.00001,
        analyzer=_analyzer,
        session_factory=session_factory,
    )
    assert result["status_after"] == "READY"
    assert result["timeframe"] == "1h"
    assert old_event_id in result["events_to_remove"]
    with session_factory() as session:
        state = session.query(IndicatorStreamState).filter_by(
            symbol=EUR_KEY, timeframe="1h"
        ).one()
        assert state.status == "READY"
        assert state.reconciliation_reason is None
        assert session.query(IndicatorCandle).filter_by(
            symbol=EUR_KEY, timeframe="1h"
        ).count() == len(closed)


def test_1h_history_start_scales_lookback_by_timeframe():
    start = recovery.history_start_for_recovery(
        "2026-09-15T10:00:00Z",
        "2026-09-15T15:00:00Z",
        lookback_candles=4,
        timeframe="1h",
    )
    assert start == pd.Timestamp("2026-09-15T06:00:00Z")
