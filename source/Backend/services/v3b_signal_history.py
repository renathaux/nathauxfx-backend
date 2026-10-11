"""Durable V3B signal transitions; no broker or strategy evaluation imports."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from sqlalchemy import text

from db import SessionLocal
from models import V3BSignalTransition


_LOCAL_LOCK = threading.RLock()
_SCOPE = re.compile(r"^CTRADER:(?:DEMO|LIVE):[0-9]+$")
_SYMBOLS = {"EURUSD", "XAUUSD"}
_SIGNALS = {"WAIT", "BUY", "SELL"}
_IRREVERSIBLE_EXECUTION = {"EXECUTED", "RUNNING", "SUBMITTED", "RECONCILIATION_REQUIRED"}


def _valid_scope(value):
    scope = str(value or "").strip().upper()
    return scope if _SCOPE.fullmatch(scope) else None


def _utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _serialize(row):
    return {
        "id": row.id,
        "account_scope": row.account_scope,
        "symbol": row.symbol,
        "signal": row.signal,
        "timestamp": _utc(row.signal_timestamp).isoformat().replace("+00:00", "Z"),
        "strategy_profile": row.strategy_profile,
        "event_id": row.event_id,
        "m5_confirmation_id": row.confirmation_id,
        "signal_setup_id": row.setup_id,
        "signal_creation_state": row.signal_creation_state,
        "execution_status": row.execution_status,
        "result": row.execution_status,
        "reason": row.reason,
        "confidence": row.confidence,
        "entry": row.entry,
    }


def record_v3b_transition(
    account_scope, symbol, signal, observed_at, *, event_id=None,
    confirmation_id=None, setup_id=None, execution_status=None, reason=None,
    confidence=None, entry=None, session_factory=None,
):
    """Create only state changes; update a matching BUY/SELL with its gate outcome.

    The Postgres transaction advisory lock serializes writers across workers.
    A local lock also makes SQLite/unit-test writes deterministic.
    """
    scope = _valid_scope(account_scope)
    symbol = str(symbol or "").upper().replace("/", "")
    signal = str(signal or "").upper()
    if not scope or symbol not in _SYMBOLS or signal not in _SIGNALS:
        return None
    timestamp = _utc(observed_at)
    factory = session_factory or SessionLocal
    status = str(execution_status or ("CANDIDATE" if signal != "WAIT" else "WAIT")).upper()
    with _LOCAL_LOCK, factory() as session:
        with session.begin():
            if session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:stream, 0))"),
                    {"stream": f"v3b-signal:{scope}:{symbol}"},
                )
            latest = (
                session.query(V3BSignalTransition)
                .filter_by(account_scope=scope, symbol=symbol)
                .order_by(V3BSignalTransition.ordinal.desc())
                .first()
            )
            same_setup = (
                latest is not None and latest.signal == signal
                and (signal == "WAIT" or not setup_id or latest.setup_id == str(setup_id))
            )
            if same_setup:
                if signal == "WAIT" and event_id and latest.event_id != str(event_id):
                    # A developing BOS remains a WAIT transition, but its
                    # identity and closed-candle time must not be lost behind
                    # an earlier generic WAIT poll.
                    latest.event_id = str(event_id)
                    latest.signal_timestamp = timestamp
                    latest.reason = str(reason)[:255] if reason else None
                    latest.updated_at = datetime.now(timezone.utc)
                if (
                    signal != "WAIT"
                    and setup_id
                    and latest.setup_id == str(setup_id)
                    and status != "CANDIDATE"
                    and not (
                        latest.execution_status in _IRREVERSIBLE_EXECUTION
                        and status not in _IRREVERSIBLE_EXECUTION
                    )
                ):
                    latest.execution_status = status
                    latest.reason = str(reason)[:255] if reason else None
                    latest.updated_at = datetime.now(timezone.utc)
            else:
                latest = V3BSignalTransition(
                    account_scope=scope,
                    symbol=symbol,
                    ordinal=(latest.ordinal + 1 if latest else 1),
                    signal=signal,
                    signal_timestamp=timestamp,
                    strategy_profile="V3B_M5_FROZEN",
                    event_id=str(event_id) if event_id else None,
                    confirmation_id=str(confirmation_id) if confirmation_id else None,
                    setup_id=str(setup_id) if setup_id else None,
                    signal_creation_state="CREATED" if signal != "WAIT" else "WAIT",
                    execution_status=status,
                    reason=str(reason)[:255] if reason else None,
                    confidence=float(confidence) if confidence is not None else None,
                    entry=float(entry) if entry is not None else None,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(latest)
            session.flush()
            result = _serialize(latest)
    return result


def list_v3b_transitions(account_scope, *, limit=10, session_factory=None):
    scope = _valid_scope(account_scope)
    if not scope:
        return []
    count = min(max(int(limit), 1), 10)
    factory = session_factory or SessionLocal
    with factory() as session:
        rows = (
            session.query(V3BSignalTransition)
            .filter_by(account_scope=scope)
            .order_by(V3BSignalTransition.signal_timestamp.desc(), V3BSignalTransition.id.desc())
            .limit(count)
            .all()
        )
        return [_serialize(row) for row in rows]
