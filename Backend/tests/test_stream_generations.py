from datetime import datetime, timezone
import os
import json
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db import Base
from models import IndicatorStreamState, IndicatorCandle, RuntimeSetting
import stream_generations as generations

SCOPE = "CTRADER:DEMO:48817926"
KEY = "XAUUSD~8EA44F1114"


@pytest.fixture
def Session(tmp_path):
    postgres_url = os.getenv("STREAM_GENERATION_POSTGRES_TEST_URL")
    if postgres_url:
        engine = create_engine(postgres_url, pool_pre_ping=True)
        # The PostgreSQL URL must point to a disposable verification database.
        # Rebuild the schema for every test so the existing SQLite-oriented
        # suite exercises the real PostgreSQL transaction/advisory-lock paths.
        with engine.begin() as conn:
            Base.metadata.drop_all(conn, checkfirst=True)
            Base.metadata.create_all(conn)
    else:
        engine = create_engine(f"sqlite:///{tmp_path}/generations.db")
        Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    t = pd.Timestamp("2026-09-22T03:00:00Z")
    with factory() as s:
        s.add(
            IndicatorStreamState(
                symbol=KEY,
                timeframe="5m",
                configuration_version="legacy-tradingview-smc-v1",
                status="RECONCILIATION_REQUIRED",
                origin_candle=t.to_pydatetime(),
                activation_watermark=t.to_pydatetime(),
                last_processed_candle=(t + pd.Timedelta(minutes=55)).to_pydatetime(),
                updated_at=t.to_pydatetime(),
            )
        )
        s.add(
            RuntimeSetting(
                setting_name="ctrader_active_account",
                setting_value=json.dumps({"account_id": "48817926", "env": "demo"}),
                updated_at=t.to_pydatetime(),
                updated_by="test",
            )
        )
        s.add(
            RuntimeSetting(
                setting_name="live_auto_trade_enabled",
                setting_value="false",
                updated_at=t.to_pydatetime(),
                updated_by="test",
            )
        )
        for i in range(12):
            s.add(
                IndicatorCandle(
                    symbol=KEY,
                    timeframe="5m",
                    candle_timestamp=(t + pd.Timedelta(minutes=5 * i)).to_pydatetime(),
                    open_price=4300 + i,
                    high_price=4302 + i,
                    low_price=4299 + i,
                    close_price=4301 + i,
                    created_at=t.to_pydatetime(),
                )
            )
        s.commit()
    try:
        yield factory
    finally:
        engine.dispose()
        if postgres_url:
            cleanup_engine = create_engine(postgres_url)
            try:
                with cleanup_engine.begin() as conn:
                    Base.metadata.drop_all(conn, checkfirst=True)
            finally:
                cleanup_engine.dispose()


def history():
    return {
        "source": "ctrader_closed_history",
        "scope": SCOPE,
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "fetched_at": "2026-09-22T04:00:00Z",
        "candles": [
            {
                "timestamp": str(t),
                "Open": 4300 + i,
                "High": 4302 + i,
                "Low": 4299 + i,
                "Close": 4301 + i,
            }
            for i, t in enumerate(
                pd.date_range("2026-09-22T03:00:00Z", periods=12, freq="5min")
            )
        ],
    }


def test_atomic_bootstrap_preserves_old_rows(Session):
    h = history()
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    plan = generations.plan(Session, h)
    assert plan["verdict"] == "SAFE_TO_CREATE_NEW_GENERATION"
    result = generations.apply(Session, h, approved_plan=plan)
    assert result["generation"] == 2
    with Session() as s:
        assert generations.snapshot(s, KEY, "5m") == before
        assert generations.resolve(s, KEY, "5m") != KEY
        assert len(generations.audit(s, KEY, "5m", 1)["candles"]) == 12
    assert generations.apply(Session, h, approved_plan=plan)["idempotent"]


from models import (
    IndicatorEvent,
    IndicatorEventLifecycle,
    TradeSubmissionAttempt,
    ExecutionProtocolState,
)


def incident_history():
    candles = []
    for i, t in enumerate(
        pd.date_range("2026-09-22T02:10:00Z", periods=24, freq="5min")
    ):
        o, h, l, c = (
            (4360, 4365, 4350, 4360) if i < 10 else (4342, 4349.28, 4339.9, 4342)
        )
        if i == 10:
            o, h, l, c = 4360, 4361, 4339.9, 4340
        if i == 22:
            o, h, l, c = 4342, 4343, 4335.0, 4336.81
        if i == 23:
            o, h, l, c = 4336.8, 4338.62, 4335.96, 4336.71
        candles.append(dict(timestamp=str(t), Open=o, High=h, Low=l, Close=c))
    return dict(
        source="ctrader_closed_history",
        scope=SCOPE,
        symbol="XAUUSD",
        timeframe="5m",
        fetched_at="2026-09-22T04:10:00Z",
        candles=candles,
    )


@pytest.fixture
def incident(Session):
    from indicators.smc import analyze_structure

    h = incident_history()
    f, _ = generations.history_frame(h)
    old = f.copy()
    old.iloc[-1] = [4336.81, 4336.81, 4336.485000000001, 4336.485000000001]
    analysis = analyze_structure(old, timeframe="5m", point_size=0.01)
    assert analysis["events"][-1]["timestamp"] == "2026-09-22T04:00:00+00:00"
    assert analysis["events"][-1]["event_type"] == "BOS"
    assert analysis == analyze_structure(f, timeframe="5m", point_size=0.01)
    now = f.index[-1].to_pydatetime()
    with Session() as s:
        s.query(IndicatorCandle).delete()
        s.query(IndicatorStreamState).delete()
        s.add(
            IndicatorStreamState(
                symbol=KEY,
                timeframe="5m",
                configuration_version=generations.VERSION,
                status="RECONCILIATION_REQUIRED",
                reconciliation_reason="automatic V3B 5m reconciliation blocked: CONSUMED is irreversible",
                origin_candle=f.index[0].to_pydatetime(),
                activation_watermark=f.index[0].to_pydatetime(),
                last_processed_candle=now,
                updated_at=now,
            )
        )
        for t, r in old.iterrows():
            s.add(
                IndicatorCandle(
                    symbol=KEY,
                    timeframe="5m",
                    candle_timestamp=t.to_pydatetime(),
                    open_price=float(r.Open),
                    high_price=float(r.High),
                    low_price=float(r.Low),
                    close_price=float(r.Close),
                    created_at=now,
                )
            )
        for raw in analysis["events"]:
            eid, identity = generations.event_identity(
                raw, KEY, "5m", 0.01, SCOPE, "XAUUSD"
            )
            s.add(
                IndicatorEvent(
                    event_id=eid,
                    symbol=KEY,
                    timeframe="5m",
                    candle_timestamp=pd.Timestamp(raw["timestamp"]).to_pydatetime(),
                    classification=raw["event_type"],
                    direction=raw["direction"],
                    broken_level=float(raw["broken_level"]),
                    opposite_swing=identity["opposite_swing"],
                    identity=identity,
                    payload=generations.normalized(raw),
                    configuration_version=generations.VERSION,
                    is_historical=False,
                    created_at=now,
                )
            )
        ci = dict(
            source_indicator_event_id=eid,
            candle_open_time=str(f.index[-1]),
            candle_close_time=str(f.index[-1] + pd.Timedelta(minutes=5)),
            close=4336.485000000001,
            symbol="XAUUSD",
            side="SELL",
            timeframe="5m",
            broken_level=4339.9,
        )
        confirmation = "m5v3b_" + generations.digest(ci)
        s.add(
            IndicatorEventLifecycle(
                event_id=eid,
                mode="LIVE",
                owner_id="OWNER",
                account_id="48817926",
                status="CONSUMED",
                m5_confirmation_id=confirmation,
                m5_confirmation_identity=ci,
                updated_at=now,
                consumed_at=now,
            )
        )
        s.add(
            TradeSubmissionAttempt(
                event_id=eid,
                mode="LIVE",
                owner_id="OWNER",
                account_id="48817926",
                symbol="XAUUSD",
                direction="SELL",
                signal_setup_id="incident-setup",
                idempotency_key="incident-claim",
                attempt_status="ACCEPTED",
                claimed_at=now,
                broker_client_order_id="incident-broker-id",
                request_payload_fingerprint="original-fingerprint",
                broker_order_id="319260102",
                broker_position_id="291215079",
                broker_response={"broker_result": "ACCEPTED", "entry": 4336.49},
                reconciliation_status="NOT_REQUIRED",
                updated_at=now,
            )
        )
        s.add(
            ExecutionProtocolState(
                singleton_id=1,
                protocol_version="indicator-event-execution-v2",
                updated_at=now,
            )
        )
        s.commit()
    return Session, h, eid


def test_real_incident_shape_and_destructive_recovery_blocked(incident, monkeypatch):
    Session, h, eid = incident
    from services import v3b_5m_stream_recovery as recovery

    monkeypatch.setattr(recovery, "active_ctrader_stream_scope", lambda: SCOPE)
    f, _ = generations.history_frame(h)
    request = recovery.RecoveryRequest(
        "48817926", "XAUUSD", "5m", KEY, earliest_required_at=f.index[-1]
    )
    assert not recovery.plan_recovery(request, f, session_factory=Session)["safe"]
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    plan = generations.plan(Session, h)
    assert plan["irreversible_event_count"] == 1
    result = generations.apply(Session, h, approved_plan=plan)
    with Session() as s:
        assert generations.snapshot(s, KEY, "5m") == before
        g2 = generations.audit(s, KEY, "5m", 2)
        assert all(e["is_historical"] for e in g2["events"])
        assert not {e["event_id"] for e in g2["events"]} & {
            e["event_id"] for e in before["events"]
        }
        assert s.query(TradeSubmissionAttempt).count() == 1
        assert not generations.event_allowed(s, eid)
        for e in g2["events"]:
            assert not generations.event_allowed(s, e["event_id"])
        assert generations.digest(before) == result["snapshot_hash"]


@pytest.mark.parametrize(
    "case", ["unchanged_confirmation", "changed_confirmation", "consumed_missing"]
)
def test_preserve_irreversible_history_across_replay_variants(incident, case):
    Session, h, eid = incident
    if case == "unchanged_confirmation":
        h["candles"][-1].update(
            Open=4336.81, High=4336.81, Low=4336.485000000001, Close=4336.485000000001
        )
    if case == "consumed_missing":
        for c in h["candles"]:
            c.update(Open=4360, High=4365, Low=4350, Close=4360)
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    with Session() as s:
        assert generations.snapshot(s, KEY, "5m") == before
        if case == "consumed_missing":
            assert not generations.audit(s, KEY, "5m", 2)["events"]


@pytest.mark.parametrize(
    "fault",
    ["gap", "duplicate", "forming", "scope", "prefix", "invalid_ohlc", "wrong_source"],
)
def test_reject_bad_authoritative_evidence(Session, fault):
    h = history()
    if fault == "gap":
        h["candles"].pop(3)
    if fault == "duplicate":
        h["candles"].append(dict(h["candles"][2]))
    if fault == "forming":
        h["fetched_at"] = h["candles"][-1]["timestamp"]
    if fault == "scope":
        h["scope"] = "CTRADER:DEMO:99999"
    if fault == "prefix":
        h["candles"].pop(0)
    if fault == "invalid_ohlc":
        h["candles"][2]["High"] = 1
    if fault == "wrong_source":
        h["source"] = "cache"
    assert generations.plan(Session, h)["verdict"] == "BLOCKED"


def test_atomic_rollback_before_switch(incident):
    Session, h, _ = incident
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    with pytest.raises(generations.GenerationBlocked):
        generations.apply(
            Session,
            h,
            approved_plan=generations.plan(Session, h),
            fail_before_switch=True,
        )
    with Session() as s:
        assert generations.resolve(s, KEY, "5m") == KEY
        assert generations.snapshot(s, KEY, "5m") == before
        assert s.query(generations.Generation).count() == 0


@pytest.mark.parametrize(
    "guard",
    [
        "live_on",
        "missing_live",
        "stale_snapshot",
        "stale_history",
        "no_plan",
        "in_flight",
    ],
)
def test_apply_guards(incident, guard):
    Session, h, eid = incident
    plan = generations.plan(Session, h)
    with Session() as s:
        if guard == "live_on":
            s.get(RuntimeSetting, "live_auto_trade_enabled").setting_value = "true"
        if guard == "missing_live":
            s.query(RuntimeSetting).delete()
        if guard == "stale_snapshot":
            s.query(IndicatorStreamState).first().reconciliation_reason = "changed"
        if guard == "in_flight":
            s.query(TradeSubmissionAttempt).first().attempt_status = "SUBMITTING"
        s.commit()
    if guard == "stale_history":
        h["candles"][-1]["Close"] += 0.01
    if guard == "no_plan":
        plan = {}
    with pytest.raises(generations.GenerationBlocked):
        generations.apply(Session, h, approved_plan=plan)
    with Session() as s:
        assert generations.resolve(s, KEY, "5m") == KEY


def test_full_replay_parity_and_restart(incident):
    from indicators.smc import analyze_structure

    Session, h, _ = incident
    plan = generations.plan(Session, h)
    r = generations.apply(Session, h, approved_plan=plan)
    f, _ = generations.history_frame(h)
    with Session() as s:
        g = s.get(generations.Generation, (KEY, "5m", 2))
        assert g.bootstrap_state == generations.normalized(
            analyze_structure(f, timeframe="5m", point_size=0.01)
        )
        audit = generations.audit(s, KEY, "5m", 2)
        assert (
            audit["state"][0]["last_processed_candle"] == plan["activation_watermark"]
        )
        for raw, stored in zip(
            g.bootstrap_state["events"],
            sorted(audit["events"], key=lambda x: x["candle_timestamp"]),
        ):
            eid, identity = generations.event_identity(
                raw, r["storage_key"], "5m", 0.01, SCOPE, "XAUUSD"
            )
            assert stored["event_id"] == eid and stored["identity"] == identity
    Session.kw["bind"].dispose()
    with Session() as s:
        assert generations.resolve(s, KEY, "5m") == r["storage_key"]
    assert generations.apply(Session, h, approved_plan=plan)["idempotent"]


@pytest.mark.parametrize(
    "previous,following,expected",
    [
        ("2026-09-18T00:00Z", "2026-09-20T22:00Z", False),
        ("2026-09-22T18:00Z", "2026-09-22T22:55Z", False),
        ("2026-09-22T20:55Z", "2026-09-22T22:00Z", True),
        ("2026-09-18T20:55Z", "2026-09-20T22:00Z", True),
        ("2026-12-18T21:55Z", "2026-12-20T23:00Z", True),
    ],
)
def test_precise_market_closures(previous, following, expected):
    assert (
        generations.sparse_gap(
            "XAUUSD", pd.Timestamp(previous), pd.Timestamp(following)
        )
        == expected
    )


def test_xauusd_labor_day_2026_closure_is_narrow_and_15m_only():
    assert generations.sparse_gap(
        "XAUUSD",
        pd.Timestamp("2026-09-07T18:30Z"),
        pd.Timestamp("2026-09-07T22:00Z"),
        15,
    )
    assert not generations.sparse_gap(
        "XAUUSD",
        pd.Timestamp("2026-09-07T18:30Z"),
        pd.Timestamp("2026-09-07T22:15Z"),
        15,
    )
    assert not generations.sparse_gap(
        "XAUUSD",
        pd.Timestamp("2026-09-08T18:30Z"),
        pd.Timestamp("2026-09-08T22:00Z"),
        15,
    )


def add_future_event(Session, key, offset=1):
    with Session() as s:
        state = s.get(IndicatorStreamState, (key, "5m"))
        t = generations.utc(state.activation_watermark) + pd.Timedelta(
            minutes=offset * 5
        )
        raw = dict(
            timestamp=t.isoformat(),
            event_type="BOS",
            direction="BULLISH",
            broken_level=4350,
            event_invalidation_swing=dict(
                type="LOW", price=4300, swing_time=t.isoformat()
            ),
        )
        eid, identity = generations.event_identity(
            raw, key, "5m", 0.01, SCOPE, "XAUUSD"
        )
        s.add(
            IndicatorEvent(
                event_id=eid,
                symbol=key,
                timeframe="5m",
                candle_timestamp=t.to_pydatetime(),
                classification="BOS",
                direction="BULLISH",
                broken_level=4350,
                identity=identity,
                payload=generations.normalized(raw),
                configuration_version=generations.VERSION,
                is_historical=False,
                created_at=t.to_pydatetime(),
            )
        )
        t += pd.Timedelta(minutes=5)
        s.add(
            IndicatorCandle(
                symbol=key,
                timeframe="5m",
                candle_timestamp=t.to_pydatetime(),
                open_price=4350,
                high_price=4352,
                low_price=4349,
                close_price=4351,
                created_at=t.to_pydatetime(),
            )
        )
        state.last_processed_candle = t.to_pydatetime()
        s.commit()
    ci = dict(
        source_indicator_event_id=eid,
        candle_open_time=t.isoformat(),
        candle_close_time=(t + pd.Timedelta(minutes=5)).isoformat(),
        close=4351.0,
        symbol="XAUUSD",
        side="BUY",
        timeframe="5m",
        broken_level=4350.0,
    )
    return eid, ci


@pytest.mark.parametrize("offset,allowed", [(-1, False), (0, False), (1, True)])
def test_activation_boundary_even_if_historical_flag_is_wrong(
    incident, offset, allowed
):
    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    if offset <= 0:
        # Existing candles already cover these confirmations.
        with Session() as s:
            cutoff = generations.utc(
                s.get(
                    IndicatorStreamState, (r["storage_key"], "5m")
                ).activation_watermark
            )
            s.query(IndicatorCandle).filter_by(
                symbol=r["storage_key"],
                timeframe="5m",
                candle_timestamp=(
                    cutoff + pd.Timedelta(minutes=(offset + 1) * 5)
                ).to_pydatetime(),
            ).delete()
            s.commit()
    eid, ci = add_future_event(Session, r["storage_key"], offset)
    with Session() as s:
        assert (
            generations.event_allowed(
                s,
                eid,
                ci,
                require_confirmation=True,
                confirmation_id="m5v3b_" + generations.digest(ci),
            )
            == allowed
        )


def test_first_future_event_claim_and_namespace_isolation(incident):
    from services.indicator_event_stream_service import update_event_lifecycle
    from services.trade_submission_service import (
        claim_submission,
        submission_identity,
        broker_client_order_id,
    )
    from services.paper_v3b_bridge import _confirmation_identity

    Session, h, oldid = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    eid, ci = add_future_event(Session, r["storage_key"])
    confirmation = "m5v3b_" + generations.digest(ci)
    assert update_event_lifecycle(
        eid,
        "LIVE",
        "ELIGIBLE",
        owner_id="OWNER",
        account_id="48817926",
        m5_confirmation_id=confirmation,
        m5_confirmation_identity=ci,
        session_factory=Session,
    )
    result = claim_submission(
        eid,
        "LIVE",
        "48817926",
        "XAUUSD",
        "future-setup",
        dict(
            action="BUY",
            m5_confirmation_identity=ci,
            m5_confirmation_id="m5v3b_" + generations.digest(ci),
        ),
        owner_id="OWNER",
        session_factory=Session,
    )
    assert result["ok"]
    assert not claim_submission(
        eid,
        "LIVE",
        "48817926",
        "XAUUSD",
        "future-setup",
        dict(
            action="BUY",
            m5_confirmation_identity=ci,
            m5_confirmation_id="m5v3b_" + generations.digest(ci),
        ),
        owner_id="OWNER",
        session_factory=Session,
    )["ok"]
    oldci, oldconfirmation = _confirmation_identity(
        "XAUUSD", oldid, "BUY", pd.Timestamp(ci["candle_open_time"]), ci["close"], 4350
    )
    newci, newconfirmation = _confirmation_identity(
        "XAUUSD", eid, "BUY", pd.Timestamp(ci["candle_open_time"]), ci["close"], 4350
    )
    assert oldconfirmation != newconfirmation
    _, oldkey = submission_identity(
        oldid, "LIVE", "OWNER", "48817926", "XAUUSD", "future-setup"
    )
    assert broker_client_order_id(oldkey) != result["broker_client_order_id"]
    with Session() as s:
        assert (
            s.query(IndicatorEventLifecycle).filter_by(event_id=oldid).one().status
            == "CONSUMED"
        )
        assert (
            s.query(IndicatorEventLifecycle).filter_by(event_id=eid).one().status
            == "SUBMITTING"
        )
        assert (
            s.query(TradeSubmissionAttempt)
            .filter_by(event_id=oldid)
            .one()
            .broker_order_id
            == "319260102"
        )


@pytest.mark.parametrize(
    "which",
    [
        "frozen",
        "bootstrap",
        "missing_confirmation",
        "wrong_close",
        "wrong_source",
        "forming",
        "wrong_id",
        "wrong_account",
    ],
)
def test_claim_fences(incident, which):
    from services.indicator_event_stream_service import update_event_lifecycle
    from services.trade_submission_service import claim_submission

    Session, h, oldid = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    eid, ci = add_future_event(Session, r["storage_key"])
    if which == "frozen":
        eid = oldid
    if which == "bootstrap":
        with Session() as s:
            eid = (
                s.query(IndicatorEvent)
                .filter_by(symbol=r["storage_key"])
                .order_by(IndicatorEvent.candle_timestamp)
                .first()
                .event_id
            )
    if which == "missing_confirmation":
        ci = None
    if which == "wrong_close":
        ci["close"] += 1
    if which == "wrong_source":
        ci["source_indicator_event_id"] = oldid
    if which == "forming":
        ci["candle_close_time"] = "2099-01-01T00:00Z"
    confirmation_id = (
        "old-confirmation" if which == "wrong_id" else "m5v3b_" + generations.digest(ci)
    )
    account = "99999" if which == "wrong_account" else "48817926"
    assert not update_event_lifecycle(
        eid,
        "LIVE",
        "ELIGIBLE",
        owner_id="OWNER",
        account_id=account,
        m5_confirmation_id=confirmation_id,
        m5_confirmation_identity=ci,
        session_factory=Session,
    )
    assert not claim_submission(
        eid,
        "LIVE",
        account,
        "XAUUSD",
        "test",
        dict(
            action="BUY",
            m5_confirmation_identity=ci,
            m5_confirmation_id=confirmation_id,
        ),
        owner_id="OWNER",
        session_factory=Session,
    )["ok"]
    with Session() as s:
        assert s.query(TradeSubmissionAttempt).count() == 1


def test_current_reader_and_audit_select_separate_generations(incident):
    from services.indicator_stream_account_scope import (
        account_scoped_read_authoritative_structure,
    )
    from services.indicator_candle_display_reader import load_durable_indicator_candles

    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    frame, _ = generations.history_frame(h)
    result = account_scoped_read_authoritative_structure(
        frame.tail(2), "XAUUSD", "5m", 0.01, session_factory=Session, stream_scope=SCOPE
    )
    with Session() as s:
        active = generations.audit(s, KEY, "5m", 2)
        old = generations.audit(s, KEY, "5m", 1)
        assert {e["event_id"] for e in result["events"]} == {
            e["event_id"] for e in active["events"]
        }
        assert (
            result["bias"]
            == s.get(generations.Generation, (KEY, "5m", 2)).bootstrap_state["bias"]
        )
        assert old["candles"][-1]["close_price"] != active["candles"][-1]["close_price"]
    shown = load_durable_indicator_candles(
        ["XAUUSD"], ["5m"], session_factory=Session, stream_scope=SCOPE
    )
    assert shown["XAUUSD"]["5m"].iloc[-1].Close == 4336.71


def test_head_failure_never_falls_back_and_only_one_active(incident):
    from sqlalchemy.exc import IntegrityError

    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    with Session() as s:
        s.get(generations.Generation, (KEY, "5m", 1)).status = "ACTIVE"
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.get(generations.Head, (KEY, "5m")).active_generation = 999
        s.commit()
        with pytest.raises(generations.GenerationBlocked):
            generations.resolve(s, KEY, "5m")


def test_concurrent_apply_is_idempotent(incident):
    from concurrent.futures import ThreadPoolExecutor

    Session, h, _ = incident
    p = generations.plan(Session, h)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: generations.apply(Session, h, approved_plan=p), range(2))
        )
    assert sorted(r["idempotent"] for r in results) == [False, True]
    with Session() as s:
        assert s.query(generations.Generation).filter_by(status="ACTIVE").count() == 1


def test_admin_import_has_no_broker_or_services_capability(tmp_path):
    import os, subprocess, sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, runpy; runpy.run_path('Backend/scripts/stream_generation_recovery.py',run_name='import_check'); assert not [m for m in sys.modules if m == 'services' or m.startswith('services.') or m.startswith('ctrader')]; print('broker-free imports verified')",
        ],
        env=dict(
            os.environ,
            DATABASE_URL="sqlite:///:memory:",
            PYTHONPATH="Backend",
            PYTHONDONTWRITEBYTECODE="1",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_admin_default_dry_run_then_explicit_apply(
    incident, tmp_path, monkeypatch, capsys
):
    import runpy

    Session, h, _ = incident
    module = runpy.run_path(
        "Backend/scripts/stream_generation_recovery.py", run_name="admin_test"
    )
    main = module["main"]
    monkeypatch.setitem(main.__globals__, "SessionLocal", Session)
    hp = tmp_path / "history.json"
    hp.write_text(json.dumps(h))
    pp = tmp_path / "plan.json"
    sp = tmp_path / "snapshot.json"
    args = ["--history", str(hp), "--plan-file", str(pp), "--snapshot-file", str(sp)]
    assert main(args) == 0
    with Session() as s:
        assert generations.resolve(s, KEY, "5m") == KEY
    assert sp.stat().st_mode & 0o777 == 0o600
    assert main(args + ["--apply"]) == 0
    assert main(args + ["--apply"]) == 0
    with Session() as s:
        assert generations.resolve(s, KEY, "5m") != KEY


def test_additive_migration_backfill_preserves_all_legacy_rows(incident):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    Session, h, _ = incident
    engine = Session.kw["bind"]
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    spec = importlib.util.spec_from_file_location(
        "generation_migration",
        "Backend/migrations/versions/20260925_0026_stream_generations.py",
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    with engine.begin() as conn:
        for table in [
            generations.StrategySetupGeneration.__table__,
            generations.Head.__table__,
            generations.Generation.__table__,
        ]:
            table.drop(conn)
        with Operations.context(MigrationContext.configure(conn)):
            m.upgrade()
    with Session() as s:
        assert generations.snapshot(s, KEY, "5m") == before
        assert generations.resolve(s, KEY, "5m") == KEY
        assert s.get(generations.Generation, (KEY, "5m", 1)).status == "ACTIVE"
    generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="Cannot downgrade"):
                m.downgrade()


def studio_setup(
    Session,
    bindings=(),
    event_time="2026-09-22T04:10Z",
    confirmation_time="2026-09-22T04:15Z",
    setup_id="studio-test",
):
    from models import StrategySetupLifecycle, StrategySetupGeneration

    with Session() as s:
        setup = StrategySetupLifecycle(
            setup_id=setup_id,
            owner_id="OWNER",
            strategy_id="strategy",
            account_id="48817926",
            account_scope=SCOPE,
            symbol="XAUUSD",
            direction="BUY",
            status="ELIGIBLE",
            definition_snapshot={},
            updated_at=datetime.now(timezone.utc),
        )
        s.add(setup)
        s.flush()
        for b in bindings:
            s.add(
                StrategySetupGeneration(
                    setup_id=setup_id,
                    root_key=KEY,
                    timeframe=b["timeframe"],
                    generation=b["generation"],
                    event_time=pd.Timestamp(event_time).to_pydatetime(),
                    confirmation_time=pd.Timestamp(confirmation_time).to_pydatetime(),
                )
            )
        s.commit()
    return setup_id


def test_studio_future_claim_old_cached_setup_and_missing_timeframe_link(incident):
    from models import StrategySetupLifecycle
    from services.trade_submission_service import claim_strategy_submission

    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    add_future_event(Session, r["storage_key"])
    old = studio_setup(Session, setup_id="old-cached")
    new = studio_setup(Session, [dict(timeframe="5m", generation=2)])
    with Session() as s:
        assert not generations.studio_claim_allowed(
            s, s.get(StrategySetupLifecycle, old)
        )
        assert generations.studio_claim_allowed(s, s.get(StrategySetupLifecycle, new))
    result = claim_strategy_submission(
        new,
        "48817926",
        "XAUUSD",
        "BUY",
        {"action": "BUY"},
        owner_id="OWNER",
        strategy_id="strategy",
        session_factory=Session,
    )
    assert result["ok"]
    # A later cutover on another timeframe must invalidate the earlier binding set.
    with Session() as s:
        now = datetime.now(timezone.utc)
        key15 = generations.storage_key(KEY, "15m", 2)
        s.add(
            generations.Generation(
                root_key=KEY,
                timeframe="15m",
                generation=2,
                storage_key=key15,
                public_symbol="XAUUSD",
                scope=SCOPE,
                status="ACTIVE",
                configuration_version=generations.VERSION,
                activation_watermark=pd.Timestamp("2026-09-22T04:00Z").to_pydatetime(),
                created_at=now,
            )
        )
        s.add(generations.Head(root_key=KEY, timeframe="15m", active_generation=2))
        s.commit()
        assert not generations.studio_claim_allowed(
            s, s.get(StrategySetupLifecycle, new)
        )


def test_studio_higher_timeframe_uses_durable_confirmation_timeframe(incident):
    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    add_future_event(Session, r["storage_key"])
    now = datetime.now(timezone.utc)
    key15 = generations.storage_key(KEY, "15m", 2)
    with Session() as s:
        s.add(
            generations.Generation(
                root_key=KEY,
                timeframe="15m",
                generation=2,
                storage_key=key15,
                public_symbol="XAUUSD",
                scope=SCOPE,
                status="ACTIVE",
                configuration_version=generations.VERSION,
                activation_watermark=pd.Timestamp("2026-09-22T03:45Z").to_pydatetime(),
                created_at=now,
            )
        )
        s.add(generations.Head(root_key=KEY, timeframe="15m", active_generation=2))
        s.add(
            IndicatorStreamState(
                symbol=key15,
                timeframe="15m",
                configuration_version=generations.VERSION,
                status="READY",
                origin_candle=pd.Timestamp("2026-09-22T03:45Z").to_pydatetime(),
                activation_watermark=pd.Timestamp("2026-09-22T03:45Z").to_pydatetime(),
                last_processed_candle=pd.Timestamp("2026-09-22T04:00Z").to_pydatetime(),
                updated_at=now,
            )
        )
        s.commit()
        generations.validate_studio_bindings(
            s,
            [dict(root_key=KEY, timeframe="15m", generation=2)],
            "2026-09-22T04:10Z",
            "2026-09-22T04:15Z",
        )
        with pytest.raises(generations.GenerationBlocked):
            generations.validate_studio_bindings(
                s,
                [dict(root_key=KEY, timeframe="15m", generation=2)],
                "2026-09-22T04:10Z",
                "2026-09-22T04:20Z",
            )


def test_studio_bundle_replaces_cache_and_blocks_missing_canonical_input(incident):
    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    frame, _ = generations.history_frame(h)
    wrong = frame.copy()
    wrong["Close"] = 0
    with Session() as s:
        result, bindings = generations.studio_bundle(s, SCOPE, "XAUUSD", {"5m": wrong})
        assert result["5m"].iloc[-1].Close == 4336.71
        assert bindings[0]["generation"] == 2
        with pytest.raises(generations.GenerationBlocked):
            generations.studio_bundle(s, SCOPE, "XAUUSD", {"15m": wrong})


def test_continuation_replays_full_prefix_with_same_generation_ids(incident):
    from services.indicator_stream_account_scope import (
        account_scoped_get_authoritative_structure,
    )
    from indicators.smc import analyze_structure

    Session, h, _ = incident
    r = generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    frame, _ = generations.history_frame(h)
    extra = pd.DataFrame(
        [
            dict(
                Open=4340 + i * 4,
                High=4345 + i * 4,
                Low=4338 + i * 4,
                Close=4343 + i * 4,
            )
            for i in range(12)
        ],
        index=pd.date_range(
            frame.index[-1] + pd.Timedelta(minutes=5), periods=12, freq="5min"
        ),
    )
    full = pd.concat([frame, extra])
    result = account_scoped_get_authoritative_structure(
        extra, "XAUUSD", "5m", 0.01, session_factory=Session, stream_scope=SCOPE
    )
    fresh = analyze_structure(full, timeframe="5m", point_size=0.01)
    assert result["new_event_ids"]
    expected = {
        generations.event_identity(raw, r["storage_key"], "5m", 0.01, SCOPE, "XAUUSD")[
            0
        ]
        for raw in fresh["events"]
    }
    assert {e["event_id"] for e in result["events"]} == expected
    with Session() as s:
        assert all(
            not e.is_historical
            for e in s.query(IndicatorEvent).filter(
                IndicatorEvent.event_id.in_(result["new_event_ids"])
            )
        )
        assert s.get(IndicatorStreamState, (r["storage_key"], "5m")).status == "READY"
        assert (
            s.get(IndicatorStreamState, (KEY, "5m")).status == "RECONCILIATION_REQUIRED"
        )
    # Restart and replaying identical bars cannot regenerate historical events.
    Session.kw["bind"].dispose()
    again = account_scoped_get_authoritative_structure(
        extra, "XAUUSD", "5m", 0.01, session_factory=Session, stream_scope=SCOPE
    )
    assert not again["new_event_ids"]


def test_active_account_change_blocks_even_an_idempotent_retry(incident):
    Session, h, _ = incident
    p = generations.plan(Session, h)
    generations.apply(Session, h, approved_plan=p)
    with Session() as s:
        s.get(RuntimeSetting, "ctrader_active_account").setting_value = json.dumps(
            {"account_id": "999", "env": "demo"}
        )
        s.commit()
    with pytest.raises(generations.GenerationBlocked, match="account changed"):
        generations.apply(Session, h, approved_plan=p)


def test_studio_generation_binding_order_does_not_change_setup_id():
    from services.strategy_studio_live_candidate import _setup_id

    values = dict(
        owner_id="OWNER",
        strategy_id="strategy",
        schema_version=1,
        account_scope=SCOPE,
        symbol="XAUUSD",
        direction="BUY",
        structure_event_time="2026-09-22T04:10Z",
        entry_trigger_time="2026-09-22T04:15Z",
        broken_level=4350,
        evaluator_setup_id="setup",
    )
    bindings = [
        dict(root_key=KEY, timeframe="5m", generation=2),
        dict(root_key=KEY, timeframe="15m", generation=2),
    ]
    assert _setup_id(**values, generation_bindings=bindings) == _setup_id(
        **values, generation_bindings=list(reversed(bindings))
    )
    assert _setup_id(**values, generation_bindings=bindings) != _setup_id(**values)


def test_claim_and_cutover_cannot_both_win(incident):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from services.trade_submission_service import claim_submission

    Session, h, eid = incident
    with Session() as s:
        s.query(TradeSubmissionAttempt).delete()
        lifecycle = s.query(IndicatorEventLifecycle).one()
        lifecycle.status = "ELIGIBLE"
        lifecycle.consumed_at = None
        s.commit()
    p = generations.plan(Session, h)
    barrier = Barrier(2)

    def cutover():
        barrier.wait()
        try:
            return generations.apply(Session, h, approved_plan=p)
        except generations.GenerationBlocked:
            return None

    def claim():
        barrier.wait()
        return claim_submission(
            eid,
            "LIVE",
            "48817926",
            "XAUUSD",
            "race",
            {"action": "SELL"},
            owner_id="OWNER",
            session_factory=Session,
        )

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(cutover)
        b = pool.submit(claim)
        cut, claimed = a.result(), b.result()
    assert bool(cut) != claimed["ok"]
    with Session() as s:
        if cut:
            assert s.query(TradeSubmissionAttempt).count() == 0
            assert generations.resolve(s, KEY, "5m") != KEY
        else:
            assert s.query(TradeSubmissionAttempt).one().attempt_status == "SUBMITTING"
            assert generations.resolve(s, KEY, "5m") == KEY


def test_chart_prefers_canonical_generation_over_usable_old_cache(incident):
    from services.indicator_candle_display_reader import load_dashboard_display_candles

    Session, h, _ = incident
    generations.apply(Session, h, approved_plan=generations.plan(Session, h))
    f, _ = generations.history_frame(h)
    f["Close"] = 1
    shown = load_dashboard_display_candles(
        ["XAUUSD"],
        ["5m"],
        session_factory=Session,
        stream_scope=SCOPE,
        now=pd.Timestamp("2026-09-22T04:11Z"),
        candle_cache={f"{SCOPE}:XAUUSD:5m": {"data": f}},
        cache_health_reader=lambda *_: {"usable": True},
    )
    assert shown["frames"]["XAUUSD"]["5m"].iloc[-1].Close == 4336.71
    assert (
        shown["streams"]["XAUUSD"]["5m"]["source"] == "persisted_ctrader_closed_candles"
    )


def test_readonly_dry_run_before_schema_migration(incident):
    from sqlalchemy import inspect

    Session, h, _ = incident
    with Session() as s:
        before = generations.snapshot(s, KEY, "5m")
    with Session.kw["bind"].begin() as conn:
        for table in [
            generations.StrategySetupGeneration.__table__,
            generations.Head.__table__,
            generations.Generation.__table__,
        ]:
            table.drop(conn)
    result = generations.plan(Session, h)
    assert result["verdict"] == "SAFE_TO_CREATE_NEW_GENERATION"
    assert result["schema_status"] == "MIGRATION_REQUIRED"
    assert not inspect(Session.kw["bind"]).has_table("indicator_stream_generations")
    with Session() as s:
        assert generations.snapshot(s, KEY, "5m") == before
    with pytest.raises(generations.GenerationBlocked, match="migrated schema"):
        generations.apply(Session, h, approved_plan=result)
