from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import (
    IndicatorCandle,
    IndicatorEvent,
    IndicatorEventLifecycle,
    IndicatorStreamState,
    TradeSubmissionAttempt,
)
from services import indicator_event_stream_service as stream
from services import v3b_5m_stream_recovery as recovery


ACCOUNT_ID = "47810571"
SCOPE = "CTRADER:DEMO:47810571"
EUR_KEY = "EURUSD~09C2948873"
XAU_KEY = "XAUUSD~93C0AAE3E8"


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recovery.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(recovery, "active_ctrader_stream_scope", lambda: SCOPE)
    yield Session


def _frame(start, periods, *, base=1.1, missing=None, duplicate=None):
    index = list(pd.date_range(start, periods=periods, freq="5min", tz="UTC"))
    missing = set(pd.Timestamp(value).tz_convert("UTC") for value in (missing or []))
    index = [value for value in index if value not in missing]
    if duplicate is not None:
        stamp = pd.Timestamp(duplicate).tz_convert("UTC")
        index.insert(index.index(stamp), stamp)
    values = []
    for offset, _stamp in enumerate(index):
        price = base + offset * 0.0001
        values.append({
            "Open": price,
            "High": price + 0.0002,
            "Low": price - 0.0002,
            "Close": price + 0.00005,
        })
    return pd.DataFrame(values, index=pd.DatetimeIndex(index))


def _event(ts, level=1.1111):
    return {
        "event_type": "BOS",
        "direction": "BULLISH",
        "timestamp": pd.Timestamp(ts).tz_convert("UTC").isoformat(),
        "broken_level": level,
        "event_invalidation_swing": {
            "type": "LOW",
            "price": level - 0.001,
            "swing_time": pd.Timestamp(ts).tz_convert("UTC").isoformat(),
        },
    }


def _analyzer(frame, **_kwargs):
    events = []
    if len(frame) >= 4:
        events.append(_event(frame.index[-2], level=float(frame.iloc[-2]["Close"])))
    return {"events": events, "bias": "BULLISH"}


def _late_analyzer(frame, **_kwargs):
    event_time = pd.Timestamp("2026-09-14T00:10:00Z")
    if event_time not in frame.index:
        return {"events": [_event("2026-09-14T00:20:00Z", 1.2)]}
    return {"events": [_event("2026-09-14T00:25:00Z", 1.3)]}


def _seed_stream(Session, key, *, reason, start="2026-09-14T00:00:00Z", periods=8):
    now = datetime.now(timezone.utc)
    frame = _frame(start, periods)
    with Session() as session:
        state = IndicatorStreamState(
            symbol=key,
            timeframe="5m",
            configuration_version=stream.CONFIGURATION_VERSION,
            status="RECONCILIATION_REQUIRED",
            origin_candle=frame.index[0].to_pydatetime(),
            activation_watermark=frame.index[-1].to_pydatetime(),
            last_processed_candle=frame.index[-1].to_pydatetime(),
            reconciliation_reason=reason,
            updated_at=now,
        )
        session.add(state)
        for ts, candle in frame.iterrows():
            session.add(IndicatorCandle(
                symbol=key,
                timeframe="5m",
                candle_timestamp=ts.to_pydatetime(),
                open_price=float(candle.Open),
                high_price=float(candle.High),
                low_price=float(candle.Low),
                close_price=float(candle.Close),
                created_at=now,
            ))
        identity, event_id = stream.build_event_identity(
            _event(frame.index[-2]), key, "5m", 0.00001
        )
        session.add(IndicatorEvent(
            event_id=event_id,
            symbol=key,
            timeframe="5m",
            candle_timestamp=frame.index[-2].to_pydatetime(),
            classification=identity["classification"],
            direction=identity["direction"],
            broken_level=1.1111,
            opposite_swing=identity["opposite_swing"],
            identity=identity,
            payload={"event_id": event_id, "event_identity": identity},
            configuration_version=stream.CONFIGURATION_VERSION,
            is_historical=False,
            created_at=now,
        ))
        session.commit()
        return frame, event_id


def _request(key, symbol="EURUSD", *, dry_run=False, earliest=None):
    return recovery.RecoveryRequest(
        account_id=ACCOUNT_ID,
        symbol=symbol,
        timeframe="5m",
        storage_key=key,
        dry_run=dry_run,
        earliest_required_at=earliest,
    )


def test_complete_historical_backfill_repair_and_idempotent_rerun(session_factory):
    old, old_event_id = _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    closed = old.copy()
    closed.iloc[0, closed.columns.get_loc("Close")] += 0.25
    result = recovery.apply_recovery(
        _request(XAU_KEY, "XAUUSD"),
        closed,
        0.01,
        analyzer=_analyzer,
        session_factory=session_factory,
    )
    assert result["status_after"] == "READY"
    assert result["old_watermark"] == "2026-09-14T00:25:00+00:00"
    assert result["new_watermark"] == "2026-09-14T00:25:00+00:00"
    assert old_event_id in result["events_to_remove"]

    second = recovery.apply_recovery(
        _request(XAU_KEY, "XAUUSD"),
        closed,
        0.01,
        analyzer=_analyzer,
        session_factory=session_factory,
    )
    assert second["idempotent"] is True
    with session_factory() as session:
        state = session.query(IndicatorStreamState).filter_by(symbol=XAU_KEY).one()
        assert state.status == "READY"
        assert state.reconciliation_reason is None
        assert session.query(IndicatorCandle).filter_by(symbol=XAU_KEY).count() == 6


def test_paginated_history_spanning_beyond_normal_window_succeeds(session_factory):
    _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=260,
    )
    closed = _frame("2026-09-13T23:00:00Z", 320, base=1800.0)
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        closed,
        session_factory=session_factory,
    )
    assert result["safe"] is True
    assert result["replacement_suffix_count"] > 250


@pytest.mark.parametrize(
    "closed, blocked",
    [
        (_frame("2026-09-14T00:00:00Z", 3), "does not reach old durable watermark"),
        (_frame("2026-09-14T00:00:00Z", 6, missing=["2026-09-14T00:10:00Z"]), "gap"),
        (_frame("2026-09-14T00:00:00Z", 6, duplicate="2026-09-14T00:10:00Z"), "duplicate"),
    ],
)
def test_invalid_backfill_fails_closed(session_factory, closed, blocked):
    original, _ = _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        closed,
        session_factory=session_factory,
    )
    assert result["safe"] is False
    assert blocked in result["blocked_reason"]
    with session_factory() as session:
        assert session.query(IndicatorCandle).filter_by(symbol=XAU_KEY).count() == len(original)
        assert session.query(IndicatorStreamState).filter_by(symbol=XAU_KEY).one().status == "RECONCILIATION_REQUIRED"


def test_forming_synthetic_candle_exclusion_must_not_satisfy_coverage(session_factory):
    _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    # Simulates caller already excluded the forming candle; without the old
    # watermark closed bar this remains blocked.
    closed_only = _frame("2026-09-14T00:00:00Z", 5)
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        closed_only,
        session_factory=session_factory,
    )
    assert result["safe"] is False
    assert "does not reach old durable watermark" in result["blocked_reason"]


def test_irreversible_lifecycle_and_submission_block(session_factory):
    _old, event_id = _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        session.add(IndicatorEventLifecycle(
            event_id=event_id,
            mode="LIVE",
            owner_id="SYSTEM",
            account_id=ACCOUNT_ID,
            status="SUBMITTING",
            updated_at=now,
        ))
        session.commit()
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        _frame("2026-09-14T00:00:00Z", 6),
        session_factory=session_factory,
    )
    assert result["safe"] is False
    assert "irreversible" in result["blocked_reason"]

    with session_factory() as session:
        session.query(IndicatorEventLifecycle).delete()
        session.add(TradeSubmissionAttempt(
            event_id=event_id,
            mode="LIVE",
            owner_id="SYSTEM",
            account_id=ACCOUNT_ID,
            symbol="XAUUSD",
            direction="BUY",
            signal_setup_id="setup-1",
            idempotency_key="idem-1",
            attempt_status="CLAIMED",
            claimed_at=now,
            broker_client_order_id="client-1",
            request_payload_fingerprint="fp",
            reconciliation_status="NOT_REQUIRED",
            updated_at=now,
        ))
        session.commit()
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        _frame("2026-09-14T00:00:00Z", 6),
        session_factory=session_factory,
    )
    assert result["safe"] is False
    assert "trade_submission_attempt" in result["blocked_reason"]


def test_account_and_symbol_isolation(session_factory):
    _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    with pytest.raises(recovery.V3B5MRecoveryBlocked, match="storage key mismatch"):
        recovery.resolve_verified_storage_key(ACCOUNT_ID, "EURUSD", XAU_KEY)
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        _frame("2026-09-14T00:00:00Z", 6),
        session_factory=session_factory,
    )
    assert result["resolved_scoped_key"] == XAU_KEY


def test_eurusd_late_event_set_mismatch_recovery(session_factory):
    old, old_event_id = _seed_stream(
        session_factory,
        EUR_KEY,
        reason="late candle changed previously accepted structure events",
        periods=7,
    )
    closed = old.copy()
    result = recovery.apply_recovery(
        _request(EUR_KEY, "EURUSD"),
        closed,
        0.00001,
        analyzer=_late_analyzer,
        session_factory=session_factory,
    )
    assert result["status_after"] == "READY"
    assert old_event_id in result["events_to_remove"]
    assert result["rebuilt_event_ids"]


def test_dry_run_causes_zero_db_mutation(session_factory):
    _old, event_id = _seed_stream(
        session_factory,
        XAU_KEY,
        reason="conflicting closed candle correction at 2026-09-14T00:00:00+00:00",
        periods=6,
    )
    result = recovery.plan_recovery(
        _request(XAU_KEY, "XAUUSD", dry_run=True),
        _frame("2026-09-14T00:00:00Z", 6),
        session_factory=session_factory,
    )
    assert result["safe"] is True
    with session_factory() as session:
        assert session.query(IndicatorStreamState).filter_by(symbol=XAU_KEY).one().status == "RECONCILIATION_REQUIRED"
        assert session.query(IndicatorEvent).filter_by(event_id=event_id).count() == 1


def test_recovery_module_cannot_call_broker_submission_code():
    import services.v3b_5m_stream_recovery as module

    assert "trade_submission_service" not in module.__dict__
    assert "live_v3b_execution_adapter" not in module.__dict__
