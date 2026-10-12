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
XAU_KEY = "XAUUSD~93C0AAE3E8"


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery15.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(recovery, "active_ctrader_stream_scope", lambda: SCOPE)
    yield Session


def _frame(start, periods, *, timeframe="15m", base=1.1):
    freq = "15min" if timeframe == "15m" else "5min"
    index = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
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


def _seed_stream(Session, key, *, timeframe, reason, start, periods=8):
    now = datetime.now(timezone.utc)
    frame = _frame(start, periods, timeframe=timeframe, base=1800.0 if key.startswith("XAU") else 1.1)
    point_size = 0.01 if key.startswith("XAU") else 0.00001
    with Session() as session:
        session.add(IndicatorStreamState(
            symbol=key,
            timeframe=timeframe,
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
                symbol=key,
                timeframe=timeframe,
                candle_timestamp=ts.to_pydatetime(),
                open_price=float(candle.Open),
                high_price=float(candle.High),
                low_price=float(candle.Low),
                close_price=float(candle.Close),
                created_at=now,
            ))
        raw = _event(frame.index[-2], level=float(frame.iloc[-2]["Close"]))
        identity, event_id = stream.build_event_identity(raw, key, timeframe, point_size)
        session.add(IndicatorEvent(
            event_id=event_id,
            symbol=key,
            timeframe=timeframe,
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


def _request(key, symbol, *, timeframe, dry_run):
    return recovery.RecoveryRequest(
        account_id=ACCOUNT_ID,
        symbol=symbol,
        timeframe=timeframe,
        storage_key=key,
        dry_run=dry_run,
    )


@pytest.mark.parametrize(
    "symbol,key,reason,start",
    [
        (
            "EURUSD",
            EUR_KEY,
            "late candle changed previously accepted structure events",
            "2026-09-10T09:30:00Z",
        ),
        (
            "XAUUSD",
            XAU_KEY,
            "conflicting closed candle correction at 2026-09-10T03:00:00+00:00",
            "2026-09-10T03:00:00Z",
        ),
    ],
)
def test_15m_plan_supports_current_reconciliation_classes(
    session_factory, symbol, key, reason, start
):
    closed, _event_id = _seed_stream(
        session_factory,
        key,
        timeframe="15m",
        reason=reason,
        start=start,
    )
    result = recovery.plan_recovery(
        _request(key, symbol, timeframe="15m", dry_run=True),
        closed,
        session_factory=session_factory,
    )
    assert result["safe"] is True
    assert result["status_before"] == "RECONCILIATION_REQUIRED"
    assert result["replacement_suffix_count"] >= 1


@pytest.mark.parametrize(
    "symbol,key,reason,start,point_size",
    [
        (
            "EURUSD",
            EUR_KEY,
            "late candle changed previously accepted structure events",
            "2026-09-10T09:30:00Z",
            0.00001,
        ),
        (
            "XAUUSD",
            XAU_KEY,
            "conflicting closed candle correction at 2026-09-10T03:00:00+00:00",
            "2026-09-10T03:00:00Z",
            0.01,
        ),
    ],
)
def test_15m_apply_rebuilds_ready_stream_with_live_auto_off(
    session_factory, monkeypatch, symbol, key, reason, start, point_size
):
    closed, old_event_id = _seed_stream(
        session_factory,
        key,
        timeframe="15m",
        reason=reason,
        start=start,
    )
    monkeypatch.setattr(
        auto_trade_state_service,
        "load_state",
        lambda **_kwargs: {"live_enabled": False},
    )
    result = recovery.apply_recovery(
        _request(key, symbol, timeframe="15m", dry_run=False),
        closed,
        point_size,
        analyzer=_analyzer,
        session_factory=session_factory,
    )
    assert result["status_after"] == "READY"
    assert result["timeframe"] == "15m"
    assert old_event_id in result["events_to_remove"]
    with session_factory() as session:
        state = session.query(IndicatorStreamState).filter_by(
            symbol=key, timeframe="15m"
        ).one()
        assert state.status == "READY"
        assert state.reconciliation_reason is None
        assert session.query(IndicatorCandle).filter_by(
            symbol=key, timeframe="15m"
        ).count() == len(closed)


def test_apply_refuses_when_live_auto_is_enabled(session_factory, monkeypatch):
    closed, _event_id = _seed_stream(
        session_factory,
        XAU_KEY,
        timeframe="5m",
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        start="2026-09-14T00:00:00Z",
        periods=6,
    )
    monkeypatch.setattr(
        auto_trade_state_service,
        "load_state",
        lambda **_kwargs: {"live_enabled": True},
    )
    with pytest.raises(recovery.V3B5MRecoveryBlocked, match="LIVE Auto"):
        recovery.apply_recovery(
            _request(XAU_KEY, "XAUUSD", timeframe="5m", dry_run=False),
            closed,
            0.01,
            analyzer=_analyzer,
            session_factory=session_factory,
        )


def test_15m_history_start_scales_lookback_by_timeframe():
    start = recovery.history_start_for_recovery(
        "2026-09-10T09:30:00Z",
        "2026-09-14T21:00:00Z",
        lookback_candles=4,
        timeframe="15m",
    )
    assert start == pd.Timestamp("2026-09-10T08:30:00Z")
