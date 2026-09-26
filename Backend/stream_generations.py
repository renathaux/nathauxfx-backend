"""Broker-free generation administration and shared runtime fences.

Never import the services package here: its __init__ installs trading hooks.
Only a scoped, timestamped CLOSED-history export is accepted by administration.
The export transport is outside this process. There is no broker API capability.
"""

from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import text, inspect
from models import (
    IndicatorStreamGeneration as Generation,
    IndicatorStreamHead as Head,
    IndicatorStreamState as State,
    IndicatorCandle as Candle,
    IndicatorEvent as Event,
    IndicatorEventLifecycle as Lifecycle,
    TradeSubmissionAttempt as Attempt,
    RuntimeSetting,
    StrategySetupGeneration,
)
from indicators.smc import analyze_structure

VERSION = "legacy-tradingview-smc-v1"
MINUTES = {"5m": 5, "15m": 15, "1h": 60}
MAX_BOOTSTRAP_CANDLES = 25000


class GenerationBlocked(RuntimeError):
    pass


def utc(value):
    t = pd.Timestamp(value)
    if pd.isna(t):
        raise GenerationBlocked("missing timestamp")
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def normalized(value):
    return json.loads(
        json.dumps(value, sort_keys=True, default=lambda v: utc(v).isoformat())
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(
            normalized(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def root_key(symbol, scope):
    return (
        symbol[:9]
        + "~"
        + hashlib.sha256(f"{symbol}|{scope}".encode()).hexdigest()[:10].upper()
    )


def storage_key(root, timeframe, number):
    if number == 1:
        return root
    return (
        root.split("~")[0][:6]
        + "~"
        + hashlib.sha256(f"{root}|{timeframe}|generation:{number}".encode())
        .hexdigest()[:12]
        .upper()
    )


def lock(session, root, timeframe):
    if session.bind.dialect.name == "postgresql":
        key = int(hashlib.sha256(f"generation:{root}".encode()).hexdigest()[:15], 16)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    elif session.bind.dialect.name == "sqlite":
        # Acquire the SQLite writer lock before the first read; no deferred upgrade race.
        if not session.in_transaction():
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.execute(
                text(
                    "UPDATE indicator_stream_heads SET active_generation=active_generation WHERE root_key=:r AND timeframe=:t"
                ),
                {"r": root, "t": timeframe},
            )


def generation_for_storage(session, key, timeframe):
    return (
        session.query(Generation)
        .filter_by(storage_key=key, timeframe=timeframe)
        .one_or_none()
    )


def resolve(session, key, timeframe, *, for_update=False):
    generation = generation_for_storage(session, key, timeframe)
    root = generation.root_key if generation else key
    if for_update:
        lock(session, root, timeframe)
    head = session.get(Head, (root, timeframe), populate_existing=True)
    if head is None:
        if generation:
            raise GenerationBlocked("generation head missing")
        return key  # an as-yet unregistered legacy/new stream; migration registers existing rows
    active = session.get(
        Generation, (root, timeframe, head.active_generation), populate_existing=True
    )
    if active is None or active.status != "ACTIVE":
        raise GenerationBlocked("active generation inconsistent")
    return active.storage_key


def register_initial(session, key, timeframe, state):
    """Register newly initialized streams in the same transaction as their state."""
    if generation_for_storage(session, key, timeframe) is None:
        if session.get(Head, (key, timeframe)) is not None:
            raise GenerationBlocked("initial generation head collision")
        session.add(
            Generation(
                root_key=key,
                timeframe=timeframe,
                generation=1,
                storage_key=key,
                public_symbol=key.split("~")[0],
                status="ACTIVE",
                configuration_version=state.configuration_version,
                activation_watermark=state.activation_watermark,
                created_at=state.updated_at,
            )
        )
        session.add(Head(root_key=key, timeframe=timeframe, active_generation=1))


def require_active(session, key, timeframe, *, for_update=False):
    if resolve(session, key, timeframe, for_update=for_update) != key:
        raise GenerationBlocked("stream generation is frozen")


def event_allowed(
    session,
    event_id,
    confirmation=None,
    *,
    for_update=False,
    require_confirmation=False,
    confirmation_id=None,
    account_id=None,
):
    event = session.get(Event, str(event_id))
    if event is None:
        return False
    try:
        require_active(session, event.symbol, event.timeframe, for_update=for_update)
    except GenerationBlocked:
        return False
    generation = generation_for_storage(session, event.symbol, event.timeframe)
    if generation and generation.generation > 1:
        if (
            account_id is not None
            and str(account_id) != generation.scope.split(":")[-1]
        ):
            return False
        if require_confirmation and confirmation_id != "m5v3b_" + digest(confirmation):
            return False
        state = session.get(State, (event.symbol, event.timeframe))
        if not state or state.status != "READY" or event.is_historical:
            return False
        cutoff = utc(generation.activation_watermark)
        if utc(event.candle_timestamp) <= cutoff:
            return False
        if require_confirmation and not confirmation:
            return False
        if confirmation is not None:
            try:
                opened = utc(confirmation["candle_open_time"])
                closed = utc(confirmation["candle_close_time"])
                if confirmation["source_indicator_event_id"] != event.event_id:
                    return False
                if (
                    confirmation.get("symbol") != generation.public_symbol
                    or confirmation.get("timeframe") != "5m"
                ):
                    return False
                side = "BUY" if event.direction == "BULLISH" else "SELL"
                if confirmation.get("side") != side or float(
                    confirmation["broken_level"]
                ) != float(event.broken_level):
                    return False
                if (
                    opened <= cutoff
                    or closed <= cutoff
                    or opened < utc(event.candle_timestamp)
                ):
                    return False
                if closed != opened + pd.Timedelta(minutes=5):
                    return False
                # Confirm the claimed source is actually durable CLOSED data.
                confirmation_key = resolve(session, generation.root_key, "5m")
                confirmation_state = session.get(State, (confirmation_key, "5m"))
                if (
                    not confirmation_state
                    or confirmation_state.status != "READY"
                    or opened > utc(confirmation_state.last_processed_candle)
                ):
                    return False
                if closed > pd.Timestamp.now(tz="UTC"):
                    return False
                candle = (
                    session.query(Candle)
                    .filter_by(
                        symbol=confirmation_key,
                        timeframe="5m",
                        candle_timestamp=opened.to_pydatetime(),
                    )
                    .one_or_none()
                )
                if candle is None or float(candle.close_price) != float(
                    confirmation["close"]
                ):
                    return False
            except (KeyError, TypeError, ValueError, GenerationBlocked):
                return False
    return True


def rows(query):
    return [
        normalized({c.name: getattr(r, c.name) for c in r.__table__.columns})
        for r in query.all()
    ]


def snapshot(session, key, timeframe):
    events = rows(
        session.query(Event)
        .filter_by(symbol=key, timeframe=timeframe)
        .order_by(Event.event_id)
    )
    ids = [e["event_id"] for e in events]
    return {
        "state": rows(session.query(State).filter_by(symbol=key, timeframe=timeframe)),
        "candles": rows(
            session.query(Candle)
            .filter_by(symbol=key, timeframe=timeframe)
            .order_by(Candle.candle_timestamp)
        ),
        "events": events,
        "lifecycles": rows(
            session.query(Lifecycle)
            .filter(Lifecycle.event_id.in_(ids))
            .order_by(Lifecycle.id)
        ),
        "attempts": rows(
            session.query(Attempt)
            .filter(Attempt.event_id.in_(ids))
            .order_by(Attempt.id)
        ),
    }


def audit(session, root, timeframe, number):
    g = session.get(Generation, (root, timeframe, number))
    if not g:
        raise GenerationBlocked("unknown audit generation")
    return snapshot(session, g.storage_key, timeframe)


def sparse_gap(symbol, previous, following, minutes=5):
    """Every absent bar must belong to a known closure; unknown gaps block."""

    def closed(t):
        local = t.tz_convert("America/New_York")
        minute = local.hour * 60 + local.minute
        if local.dayofweek == 5:
            return True
        if local.dayofweek == 4 and minute >= 17 * 60:
            return True
        reopen = 18 if symbol == "XAUUSD" else 17
        if local.dayofweek == 6 and minute < reopen * 60:
            return True
        if symbol == "XAUUSD" and 17 * 60 <= minute < 18 * 60:
            return True
        # Exact authoritative cTrader XAUUSD Labor Day 2026 omission. The
        # broker history closes after the 18:30 UTC bar and resumes at the
        # normal 22:00 UTC reopen. Keep this exception date-bounded so an
        # unrelated weekday gap still fails closed.
        if (
            symbol == "XAUUSD"
            and t.date() == pd.Timestamp("2026-09-07", tz="UTC").date()
            and pd.Timestamp("2026-09-07T18:45:00Z") <= t
            < pd.Timestamp("2026-09-07T22:00:00Z")
        ):
            return True
        # Exact previously observed cTrader EURUSD daily rollover omission.
        if symbol == "EURUSD" and t.dayofweek < 5 and t.hour == 20 and t.minute >= 30:
            return True
        return False

    return all(
        closed(t)
        for t in pd.date_range(
            previous + pd.Timedelta(minutes=minutes),
            following,
            freq=f"{minutes}min",
            inclusive="left",
        )
    )


def history_frame(history):
    if history.get("source") != "ctrader_closed_history":
        raise GenerationBlocked("authoritative cTrader source required")
    scope = history.get("scope", "")
    symbol = history.get("symbol")
    tf = history.get("timeframe")
    if not re.fullmatch(r"CTRADER:(DEMO|LIVE):[0-9]+", scope):
        raise GenerationBlocked("invalid account scope")
    if symbol not in {"XAUUSD", "EURUSD"} or tf not in MINUTES:
        raise GenerationBlocked("unsupported stream")
    candles = history.get("candles", [])
    if not 2 <= len(candles) <= MAX_BOOTSTRAP_CANDLES:
        raise GenerationBlocked("history size outside safe bootstrap bounds")
    f = pd.DataFrame(candles).set_index("timestamp")
    f.index = pd.DatetimeIndex([utc(t) for t in f.index])
    if f.index.duplicated().any():
        raise GenerationBlocked("duplicate timestamps")
    f = f.sort_index()[["Open", "High", "Low", "Close"]].astype(float)
    step = pd.Timedelta(minutes=MINUTES[tf])
    fetched = utc(history["fetched_at"])
    if fetched > pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=30):
        raise GenerationBlocked("future provenance time")
    if f.index[-1] + step > fetched:
        raise GenerationBlocked("forming candle is not CLOSED")
    if any(t.value % step.value for t in f.index):
        raise GenerationBlocked("off-grid candle")
    current_open = fetched.floor(f"{MINUTES[tf]}min")
    if current_open - f.index[-1] > step and not sparse_gap(
        symbol, f.index[-1], current_open, MINUTES[tf]
    ):
        raise GenerationBlocked(
            "history does not reach latest CLOSED coverage at fetch time"
        )
    for _, r in f.iterrows():
        if (
            not all(math.isfinite(v) for v in r)
            or r.Low > min(r.Open, r.Close)
            or r.High < max(r.Open, r.Close)
            or r.Low > r.High
        ):
            raise GenerationBlocked("invalid OHLC")
    gaps = []
    for a, b in zip(f.index, f.index[1:]):
        if b - a > step:
            if not sparse_gap(symbol, a, b, MINUTES[tf]):
                raise GenerationBlocked(f"unrecognized history gap after {a}")
            gaps.append([a.isoformat(), b.isoformat()])
    return f, gaps


def event_identity(raw, key, tf, point, scope, symbol):
    # Same immutable v1 hash schema; independent storage key namespaces G2+.
    opposite = raw.get("event_invalidation_swing") or {}
    decimals = 2 if point == 0.01 else 5
    swing_time = opposite.get("swing_time") or opposite.get("time")
    identity = {
        "symbol": key,
        "timeframe": tf,
        "candle_timestamp": utc(raw["timestamp"]).isoformat(),
        "classification": raw["event_type"].upper(),
        "direction": raw["direction"].upper(),
        "broken_level": f"{float(raw['broken_level']):.{decimals}f}",
        "opposite_swing": {
            "type": str(opposite.get("type") or "").upper(),
            "price": (
                f"{float(opposite['price']):.{decimals}f}"
                if opposite.get("price") is not None
                else None
            ),
            "swing_time": utc(swing_time).isoformat() if swing_time else None,
        },
    }
    eid = "smc1_" + digest(identity)
    identity.update(symbol=symbol, stream_scope=scope)
    return eid, identity


def live_off(session, *, locked=False):
    q = session.query(RuntimeSetting).filter_by(setting_name="live_auto_trade_enabled")
    if locked:
        q = q.with_for_update()
    r = q.one_or_none()
    return bool(r and str(r.setting_value).lower() in {"false", "0", "off", "no"})


def _plan(session, history, *, legacy_schema=False):
    f, gaps = history_frame(history)
    symbol = history["symbol"]
    tf = history["timeframe"]
    scope = history["scope"]
    root = root_key(symbol, scope)
    selection = session.get(RuntimeSetting, "ctrader_active_account")
    if selection is None:
        raise GenerationBlocked("durable active account required")
    selected = json.loads(selection.setting_value)
    if (
        scope
        != f"CTRADER:{str(selected.get('env','')).upper()}:{selected.get('account_id')}"
    ):
        raise GenerationBlocked("active account does not match history scope")
    key = root if legacy_schema else resolve(session, root, tf)
    old = session.get(State, (key, tf))
    if not old or old.origin_candle is None or old.last_processed_candle is None:
        raise GenerationBlocked("initialized predecessor required")
    if old.configuration_version != VERSION:
        raise GenerationBlocked("configuration mismatch")
    if f.index[0] != utc(old.origin_candle) or f.index[-1] < utc(
        old.last_processed_candle
    ):
        raise GenerationBlocked(
            "history must cover entire original prefix and watermark"
        )
    if (
        session.query(Candle).filter_by(symbol=key, timeframe=tf).count()
        > MAX_BOOTSTRAP_CANDLES
    ):
        raise GenerationBlocked("predecessor too large")
    snap = snapshot(session, key, tf)
    if any(
        x["attempt_status"] in {"SUBMITTING", "RECONCILIATION_REQUIRED"}
        for x in snap["attempts"]
    ) or any(
        x["status"] in {"SUBMITTING", "RECONCILIATION_REQUIRED"}
        for x in snap["lifecycles"]
    ):
        raise GenerationBlocked("in-flight or unresolved submission")
    # Studio has independent setup rows; no in-flight account/symbol handoff may cross cutover.
    account = scope.split(":")[-1]
    if (
        session.query(Attempt)
        .filter(
            Attempt.account_id == account,
            Attempt.symbol == symbol,
            Attempt.attempt_status.in_(["SUBMITTING", "RECONCILIATION_REQUIRED"]),
        )
        .first()
    ):
        raise GenerationBlocked("account has an in-flight submission")
    g = None if legacy_schema else generation_for_storage(session, key, tf)
    number = g.generation if g else 1
    newkey = storage_key(root, tf, number + 1)
    analysis = normalized(
        analyze_structure(
            f, timeframe=tf, point_size=0.01 if symbol == "XAUUSD" else 0.00001
        )
    )
    events = []
    for raw in analysis["events"]:
        eid, identity = event_identity(
            raw, newkey, tf, 0.01 if symbol == "XAUUSD" else 0.00001, scope, symbol
        )
        events.append({"event_id": eid, "identity": identity, "raw": raw})
    return {
        "verdict": "SAFE_TO_CREATE_NEW_GENERATION",
        "schema_status": (
            "MIGRATION_REQUIRED" if legacy_schema else "GENERATION_SCHEMA_PRESENT"
        ),
        "account": account,
        "scope": scope,
        "symbol": symbol,
        "timeframe": tf,
        "root_key": root,
        "old_key": key,
        "old_generation": number,
        "old_status": old.status,
        "old_watermark": utc(old.last_processed_candle).isoformat(),
        "new_rows_to_create": {
            "candles": len(f),
            "events": len(events),
            "stream_state": 1,
            "generation_registry": 1 if g else 2,
            "head": 0 if g else 1,
        },
        "new_generation": number + 1,
        "new_key": newkey,
        "history_start": f.index[0].isoformat(),
        "history_end": f.index[-1].isoformat(),
        "activation_watermark": f.index[-1].isoformat(),
        "coverage": "COMPLETE_FROM_ORIGIN",
        "gaps": gaps,
        "duplicates": 0,
        "bootstrap_candles": len(f),
        "bootstrap_events": len(events),
        "irreversible_event_count": len(
            {
                x["event_id"]
                for x in snap["lifecycles"]
                if x["status"] in {"CONSUMED", "SUBMITTING"} or x["consumed_at"]
            }
        ),
        "submission_attempt_count": len(snap["attempts"]),
        "snapshot_hash": digest(snap),
        "history_hash": digest(history),
        "analysis_hash": digest(analysis),
        "event_hash": digest(events),
        "old_rows_to_modify": ["generation registry status and active head only"],
        "historical_execution_rows_to_modify": 0,
        "broker_capable_code_imported": False,
        "broker_calls_possible": False,
        "live_auto_status": "OFF" if live_off(session) else "ON_OR_UNKNOWN",
    }


def plan(factory, history):
    with factory() as s:
        try:
            inspector = inspect(s.connection())
            present = [
                inspector.has_table(name)
                for name in (
                    Generation.__tablename__,
                    Head.__tablename__,
                    StrategySetupGeneration.__tablename__,
                )
            ]
            if any(present) and not all(present):
                raise GenerationBlocked("partial generation schema")
            return _plan(s, history, legacy_schema=not any(present))
        except (GenerationBlocked, KeyError, ValueError, TypeError) as e:
            return {"verdict": "BLOCKED", "reason": str(e)}


def apply(factory, history, *, approved_plan, fail_before_switch=False):
    if approved_plan.get("verdict") != "SAFE_TO_CREATE_NEW_GENERATION":
        raise GenerationBlocked("successful dry-run required")
    if approved_plan.get("schema_status") != "GENERATION_SCHEMA_PRESENT":
        raise GenerationBlocked("apply requires migrated schema and a fresh dry-run")
    root = approved_plan["root_key"]
    tf = approved_plan["timeframe"]
    with factory() as s:
        try:
            lock(s, root, tf)
            selection = (
                s.query(RuntimeSetting)
                .filter_by(setting_name="ctrader_active_account")
                .with_for_update()
                .one_or_none()
            )
            if not selection:
                raise GenerationBlocked("durable account unavailable")
            selected = json.loads(selection.setting_value)
            if (
                history.get("scope")
                != f"CTRADER:{str(selected.get('env','')).upper()}:{selected.get('account_id')}"
            ):
                raise GenerationBlocked("active account changed")
            if not live_off(s, locked=True):
                raise GenerationBlocked("LIVE Auto must be OFF")
            active = resolve(s, root, tf)
            g = generation_for_storage(s, active, tf)
            if (
                g
                and g.generation == approved_plan["new_generation"]
                and g.history_hash == digest(history)
            ):
                if (
                    g.predecessor_snapshot is None
                    or digest(g.predecessor_snapshot) != approved_plan["snapshot_hash"]
                ):
                    raise GenerationBlocked("retry does not match predecessor")
                return {
                    "generation": g.generation,
                    "storage_key": g.storage_key,
                    "idempotent": True,
                }
            current = _plan(s, history)
            # LIVE may legitimately have been turned OFF after the dry-run.
            comparable = lambda p: {
                k: v for k, v in p.items() if k != "live_auto_status"
            }
            if comparable(current) != comparable(approved_plan):
                raise GenerationBlocked("dry-run is stale; rerun it")
            oldkey = current["old_key"]
            snap = snapshot(s, oldkey, tf)
            old = s.get(State, (oldkey, tf))
            now = datetime.now(timezone.utc)
            if g is None:
                g = Generation(
                    root_key=root,
                    timeframe=tf,
                    generation=1,
                    storage_key=root,
                    scope=history["scope"],
                    public_symbol=history["symbol"],
                    status="ACTIVE",
                    configuration_version=VERSION,
                    activation_watermark=old.activation_watermark,
                    created_at=now,
                )
                s.add(g)
                s.add(Head(root_key=root, timeframe=tf, active_generation=1))
                s.flush()
            key = current["new_key"]
            number = current["new_generation"]
            f, _ = history_frame(history)
            analysis = normalized(
                analyze_structure(
                    f,
                    timeframe=tf,
                    point_size=0.01 if history["symbol"] == "XAUUSD" else 0.00001,
                )
            )
            s.add(
                State(
                    symbol=key,
                    timeframe=tf,
                    configuration_version=VERSION,
                    status="READY",
                    origin_candle=f.index[0].to_pydatetime(),
                    activation_watermark=f.index[-1].to_pydatetime(),
                    last_processed_candle=f.index[-1].to_pydatetime(),
                    updated_at=now,
                )
            )
            for t, r in f.iterrows():
                s.add(
                    Candle(
                        symbol=key,
                        timeframe=tf,
                        candle_timestamp=t.to_pydatetime(),
                        open_price=float(r.Open),
                        high_price=float(r.High),
                        low_price=float(r.Low),
                        close_price=float(r.Close),
                        created_at=now,
                    )
                )
            for raw in analysis["events"]:
                eid, identity = event_identity(
                    raw,
                    key,
                    tf,
                    0.01 if history["symbol"] == "XAUUSD" else 0.00001,
                    history["scope"],
                    history["symbol"],
                )
                payload = dict(
                    raw,
                    event_id=eid,
                    event_identity=identity,
                    public_symbol=history["symbol"],
                    stream_scope=history["scope"],
                )
                s.add(
                    Event(
                        event_id=eid,
                        symbol=key,
                        timeframe=tf,
                        candle_timestamp=utc(raw["timestamp"]).to_pydatetime(),
                        classification=identity["classification"],
                        direction=identity["direction"],
                        broken_level=raw["broken_level"],
                        opposite_swing=identity["opposite_swing"],
                        identity=identity,
                        payload=payload,
                        configuration_version=VERSION,
                        is_historical=True,
                        created_at=now,
                    )
                )
            s.flush()
            stored = snapshot(s, key, tf)
            stored_frame = pd.DataFrame(
                [
                    {
                        "Open": c["open_price"],
                        "High": c["high_price"],
                        "Low": c["low_price"],
                        "Close": c["close_price"],
                    }
                    for c in stored["candles"]
                ],
                index=pd.DatetimeIndex(
                    [utc(c["candle_timestamp"]) for c in stored["candles"]]
                ),
            )
            if (
                not stored_frame.equals(f)
                or normalized(
                    analyze_structure(
                        stored_frame,
                        timeframe=tf,
                        point_size=0.01 if history["symbol"] == "XAUUSD" else 0.00001,
                    )
                )
                != analysis
            ):
                raise GenerationBlocked("stored bootstrap replay parity failed")
            expected_ids = {
                event_identity(
                    raw,
                    key,
                    tf,
                    0.01 if history["symbol"] == "XAUUSD" else 0.00001,
                    history["scope"],
                    history["symbol"],
                )[0]
                for raw in analysis["events"]
            }
            if {e["event_id"] for e in stored["events"]} != expected_ids or any(
                not e["is_historical"] for e in stored["events"]
            ):
                raise GenerationBlocked("stored bootstrap event parity failed")
            g.status = "FROZEN"
            s.flush()
            s.add(
                Generation(
                    root_key=root,
                    timeframe=tf,
                    generation=number,
                    storage_key=key,
                    scope=history["scope"],
                    public_symbol=history["symbol"],
                    status="ACTIVE",
                    configuration_version=VERSION,
                    activation_watermark=f.index[-1].to_pydatetime(),
                    history_hash=digest(history),
                    predecessor_snapshot=snap,
                    bootstrap_state=analysis,
                    created_at=now,
                )
            )
            s.flush()
            if fail_before_switch:
                raise GenerationBlocked("injected interruption before cutover")
            head = s.get(Head, (root, tf))
            head.active_generation = number
            s.flush()
            if snapshot(s, oldkey, tf) != snap:
                raise GenerationBlocked("historical rows changed")
            if not live_off(s, locked=True):
                raise GenerationBlocked("LIVE changed during preparation")
            s.commit()
            return {
                "generation": number,
                "storage_key": key,
                "idempotent": False,
                "snapshot_hash": digest(snap),
            }
        except Exception:
            s.rollback()
            raise


def studio_bundle(session, scope, symbol, bundle):
    """Canonical generation bars replace transient cache input; no prior-state reuse."""
    root = root_key(symbol, scope)
    output = dict(bundle)
    bindings = []
    lock(session, root, "5m")
    for head in session.query(Head).filter_by(root_key=root).all():
        key = resolve(session, root, head.timeframe)
        g = generation_for_storage(session, key, head.timeframe)
        if g.generation <= 1:
            continue
        state = session.get(State, (key, head.timeframe))
        if not state or state.status != "READY":
            raise GenerationBlocked("Studio active stream is not READY")
        if head.timeframe not in output:
            raise GenerationBlocked("Studio bundle lacks an active canonical timeframe")
        candles = (
            session.query(Candle)
            .filter_by(symbol=key, timeframe=head.timeframe)
            .order_by(Candle.candle_timestamp)
            .all()
        )
        if not candles:
            raise GenerationBlocked("Studio canonical history missing")
        frame = pd.DataFrame(
            [
                {
                    "Open": c.open_price,
                    "High": c.high_price,
                    "Low": c.low_price,
                    "Close": c.close_price,
                    "Volume": 0.0,
                }
                for c in candles
            ],
            index=pd.DatetimeIndex([utc(c.candle_timestamp) for c in candles]),
        )
        frame.attrs.update(ctrader_stream_scope=scope, stream_generation=g.generation)
        output[head.timeframe] = frame
        bindings.append(
            {
                "root_key": root,
                "timeframe": head.timeframe,
                "generation": g.generation,
                "activation_watermark": utc(g.activation_watermark).isoformat(),
            }
        )
    return output, sorted(
        bindings, key=lambda b: (b["root_key"], b["timeframe"], b["generation"])
    )


def validate_studio_bindings(session, bindings, event_time, confirmation_time):
    for b in sorted(bindings, key=lambda b: (b["root_key"], b["timeframe"])):
        lock(session, b["root_key"], b["timeframe"])
        key = resolve(session, b["root_key"], b["timeframe"])
        g = generation_for_storage(session, key, b["timeframe"])
        if not g or g.generation != b["generation"]:
            raise GenerationBlocked("Studio generation changed")
        if utc(event_time) <= utc(g.activation_watermark) or utc(
            confirmation_time
        ) <= utc(g.activation_watermark):
            raise GenerationBlocked("Studio bootstrap setup is historical")
        state = session.get(State, (key, b["timeframe"]))
        if not state or state.status != "READY":
            raise GenerationBlocked("Studio stream is not READY")
        confirmation_key = resolve(session, b["root_key"], "5m")
        confirmation_state = session.get(State, (confirmation_key, "5m"))
        candle = (
            session.query(Candle)
            .filter_by(
                symbol=confirmation_key,
                timeframe="5m",
                candle_timestamp=utc(confirmation_time).to_pydatetime(),
            )
            .one_or_none()
        )
        if (
            not confirmation_state
            or confirmation_state.status != "READY"
            or candle is None
            or utc(confirmation_time) > utc(confirmation_state.last_processed_candle)
        ):
            raise GenerationBlocked("Studio confirmation is not durable")


def studio_claim_allowed(session, setup):
    root = root_key(setup.symbol, setup.account_scope)
    lock(session, root, "5m")
    heads = session.query(Head).filter_by(root_key=root).populate_existing().all()
    # Lock every stream for this symbol, including G1, before cutover/claim decision.
    for h in sorted(heads, key=lambda h: h.timeframe):
        lock(session, root, h.timeframe)
    links = (
        session.query(StrategySetupGeneration).filter_by(setup_id=setup.setup_id).all()
    )
    advanced = [
        h
        for h in heads
        if session.get(
            Head, (root, h.timeframe), populate_existing=True
        ).active_generation
        > 1
    ]
    if {(h.root_key, h.timeframe) for h in advanced} != {
        (b.root_key, b.timeframe) for b in links
    }:
        return False
    try:
        for link in links:
            validate_studio_bindings(
                session,
                [
                    {
                        "root_key": link.root_key,
                        "timeframe": link.timeframe,
                        "generation": link.generation,
                    }
                ],
                link.event_time,
                link.confirmation_time,
            )
    except GenerationBlocked:
        return False
    return True
