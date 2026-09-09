"""Durable LIVE submission claims and conservative broker reconciliation."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from db import SessionLocal
from models import ExecutionProtocolState, IndicatorEventLifecycle, TradeSubmissionAttempt


logger = logging.getLogger(__name__)


BLOCKING_ATTEMPT_STATUSES = {
    "SUBMITTING", "RECONCILIATION_REQUIRED", "ACCEPTED", "REJECTED",
}
EXECUTION_PROTOCOL_VERSION = "indicator-event-execution-v2"
DEFAULT_OWNER_ID = "OWNER"

ACCEPTED = "ACCEPTED"
DEFINITELY_REJECTED = "DEFINITELY_REJECTED"
FAILED_BEFORE_SEND = "FAILED_BEFORE_SEND"
AMBIGUOUS = "AMBIGUOUS"
ACCEPTED_PROTECTION_FAILED = "ACCEPTED_PROTECTION_FAILED"


def _json(value):
    return json.loads(json.dumps(value or {}, sort_keys=True, default=str))


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def submission_identity(event_id, mode, owner_id, account_id, symbol, signal_setup_id):
    identity = {
        "event_id": str(event_id), "mode": str(mode).upper(),
        "owner_id": str(owner_id),
        "account_id": str(account_id), "symbol": str(symbol).upper().replace("/", ""),
        "signal_setup_id": str(signal_setup_id),
    }
    return identity, "fs1_" + _hash(identity)


def broker_client_order_id(internal_key):
    """Stable 160-bit broker reference within cTrader's 50-char limit."""
    return "fsc1_" + hashlib.sha256(str(internal_key).encode("utf-8")).hexdigest()[:40]


def verify_execution_protocol(*, session_factory=None, session=None):
    factory = session_factory or SessionLocal
    owns_session = session is None
    current = session or factory()
    try:
        row = current.get(ExecutionProtocolState, 1)
        return bool(row and row.protocol_version == EXECUTION_PROTOCOL_VERSION)
    except Exception:
        return False
    finally:
        if owns_session:
            current.close()


def claim_submission(event_id, mode, account_id, symbol, signal_setup_id, payload, *, owner_id=DEFAULT_OWNER_ID, direction=None, session_factory=None):
    direction = str(direction or (payload or {}).get("action") or (payload or {}).get("signal") or "").upper()
    if not all([event_id, owner_id, account_id, symbol, signal_setup_id, direction]):
        return {"ok": False, "reason": "missing durable submission identity"}
    factory = session_factory or SessionLocal
    identity, key = submission_identity(event_id, mode, owner_id, account_id, symbol, signal_setup_id)
    client_id = broker_client_order_id(key)
    now = datetime.now(timezone.utc)
    session = factory()
    try:
        if not verify_execution_protocol(session=session):
            session.rollback()
            return {"ok": False, "reason": "execution protocol fence absent or incompatible"}
        lifecycle = session.query(IndicatorEventLifecycle).filter(
            IndicatorEventLifecycle.event_id == str(event_id),
            IndicatorEventLifecycle.mode == str(mode).upper(),
            IndicatorEventLifecycle.owner_id == str(owner_id),
            IndicatorEventLifecycle.account_id == str(account_id),
        ).with_for_update().one_or_none()
        if lifecycle is None or str(lifecycle.status).upper() != "ELIGIBLE":
            session.rollback()
            return {"ok": False, "reason": "event is not atomically claimable", "status": getattr(lifecycle, "status", None), "idempotency_key": key}
        changed = session.query(IndicatorEventLifecycle).filter(
            IndicatorEventLifecycle.event_id == str(event_id),
            IndicatorEventLifecycle.mode == str(mode).upper(),
            IndicatorEventLifecycle.owner_id == str(owner_id),
            IndicatorEventLifecycle.account_id == str(account_id),
            IndicatorEventLifecycle.status == "ELIGIBLE",
        ).update({
            IndicatorEventLifecycle.status: "SUBMITTING",
            IndicatorEventLifecycle.signal_setup_id: str(signal_setup_id),
            IndicatorEventLifecycle.updated_at: now,
        }, synchronize_session=False)
        if changed != 1:
            session.rollback()
            return {"ok": False, "reason": "event claim lost", "idempotency_key": key}
        existing = session.query(TradeSubmissionAttempt).filter_by(idempotency_key=key).with_for_update().one_or_none()
        if existing is not None:
            if existing.attempt_status != "FAILED_BEFORE_SEND" or existing.request_started_at is not None:
                session.rollback()
                return {"ok": False, "reason": "submission already claimed", "status": existing.attempt_status, "idempotency_key": key}
            existing.attempt_status = "SUBMITTING"
            existing.claimed_at = now
            existing.updated_at = now
            existing.last_error = None
            session.commit()
            return {"ok": True, "idempotency_key": key, "broker_request_id": key, "broker_client_order_id": client_id, "attempt_id": existing.id}
        attempt = TradeSubmissionAttempt(
            event_id=str(event_id), mode=str(mode).upper(), account_id=str(account_id),
            owner_id=str(owner_id), direction=direction,
            symbol=identity["symbol"], signal_setup_id=str(signal_setup_id),
            idempotency_key=key, attempt_status="SUBMITTING", claimed_at=now,
            broker_request_id=key, broker_client_order_id=client_id,
            request_payload_fingerprint=_hash(_json(payload)),
            reconciliation_status="NOT_REQUIRED", updated_at=now,
        )
        session.add(attempt)
        session.commit()
        return {"ok": True, "idempotency_key": key, "broker_request_id": key, "broker_client_order_id": client_id, "attempt_id": attempt.id}
    except IntegrityError:
        session.rollback()
        return {"ok": False, "reason": "submission already claimed", "idempotency_key": key}
    except Exception as exc:
        session.rollback()
        return {"ok": False, "reason": f"submission claim failed: {exc}", "idempotency_key": key}
    finally:
        session.close()


def mark_request_started(key, *, session_factory=None):
    return _transition_attempt(key, {"SUBMITTING"}, "SUBMITTING", request_started=True, session_factory=session_factory)


def complete_submission(key, result, *, session_factory=None):
    category = str((result or {}).get("broker_result") or "").upper()
    if category == ACCEPTED:
        return _transition_attempt(key, {"SUBMITTING", "RECONCILIATION_REQUIRED"}, "ACCEPTED", result=result, lifecycle_status="CONSUMED", session_factory=session_factory)
    if category == ACCEPTED_PROTECTION_FAILED:
        return _transition_attempt(key, {"SUBMITTING", "RECONCILIATION_REQUIRED"}, "ACCEPTED_PROTECTION_FAILED", result=result, lifecycle_status="CONSUMED", reconciliation_status="PROTECTION_FAILED", session_factory=session_factory)
    if category == DEFINITELY_REJECTED:
        return _transition_attempt(key, {"SUBMITTING"}, "REJECTED", result=result, lifecycle_status="BLOCKED", session_factory=session_factory)
    if category == FAILED_BEFORE_SEND:
        return _transition_attempt(key, {"SUBMITTING"}, "FAILED_BEFORE_SEND", result=result, lifecycle_status="ELIGIBLE", reconciliation_status="NOT_REQUIRED", session_factory=session_factory)
    return require_reconciliation(key, (result or {}).get("reason") or "ambiguous broker result", result=result, session_factory=session_factory)


def require_reconciliation(key, error, *, result=None, session_factory=None):
    return _transition_attempt(
        key, {"SUBMITTING"}, "RECONCILIATION_REQUIRED", error=error,
        result=result,
        lifecycle_status="RECONCILIATION_REQUIRED", reconciliation_status="PENDING",
        session_factory=session_factory,
    )


def recover_unsent_claim(key, *, session_factory=None):
    """Release only a claim proven never to have entered broker dispatch."""
    factory = session_factory or SessionLocal
    session = factory()
    try:
        unlocked = session.query(TradeSubmissionAttempt).filter_by(idempotency_key=str(key)).one_or_none()
        if unlocked is None:
            session.rollback(); return False
        session.query(IndicatorEventLifecycle).filter_by(
            event_id=unlocked.event_id, mode=unlocked.mode,
            owner_id=unlocked.owner_id, account_id=unlocked.account_id,
        ).with_for_update().one()
        row = session.query(TradeSubmissionAttempt).filter_by(idempotency_key=str(key)).with_for_update().one_or_none()
        if row is None or row.attempt_status != "SUBMITTING" or row.request_started_at is not None:
            session.rollback(); return False
        now = datetime.now(timezone.utc)
        attempt_changed = session.query(TradeSubmissionAttempt).filter(
            TradeSubmissionAttempt.id == row.id,
            TradeSubmissionAttempt.attempt_status == "SUBMITTING",
            TradeSubmissionAttempt.request_started_at.is_(None),
        ).update({
            TradeSubmissionAttempt.attempt_status: "FAILED_BEFORE_SEND",
            TradeSubmissionAttempt.reconciliation_status: "NOT_REQUIRED",
            TradeSubmissionAttempt.updated_at: now,
        }, synchronize_session=False)
        lifecycle_changed = session.query(IndicatorEventLifecycle).filter(
            IndicatorEventLifecycle.event_id == row.event_id,
            IndicatorEventLifecycle.mode == row.mode,
            IndicatorEventLifecycle.owner_id == row.owner_id,
            IndicatorEventLifecycle.account_id == row.account_id,
            IndicatorEventLifecycle.status == "SUBMITTING",
        ).update({
            IndicatorEventLifecycle.status: "ELIGIBLE",
            IndicatorEventLifecycle.updated_at: now,
        }, synchronize_session=False)
        if attempt_changed != 1 or lifecycle_changed != 1:
            logger.error("Failed closed while recovering unsent claim %s", key)
            session.rollback(); return False
        session.commit(); return True
    except Exception:
        logger.exception("Failed to recover unsent submission claim %s", key)
        session.rollback(); return False
    finally:
        session.close()


def reconcile_known_broker_order(key, broker_record, *, session_factory=None):
    """Consume an ambiguous attempt when the stable client reference is found."""
    return _transition_attempt(
        key, {"RECONCILIATION_REQUIRED", "SUBMITTING"}, "ACCEPTED",
        result=broker_record, lifecycle_status="CONSUMED", reconciliation_status="MATCHED",
        session_factory=session_factory,
    )


def reconcile_incomplete_submissions(broker_records=None, *, record_provider=None, session_factory=None):
    """Recover unsent claims and fail closed for any possibly-sent request."""
    factory = session_factory or SessionLocal
    session = factory()
    try:
        rows = session.query(TradeSubmissionAttempt).filter(
            TradeSubmissionAttempt.attempt_status.in_({
                "SUBMITTING", "RECONCILIATION_REQUIRED",
            })
        ).all()
        pending = [{
            "key": row.idempotency_key, "request_started_at": row.request_started_at,
            "account_id": row.account_id, "symbol": row.symbol,
            "direction": row.direction, "claimed_at": row.claimed_at,
            "client_order_id": row.broker_client_order_id,
        } for row in rows]
    finally:
        session.close()
    matched = []
    recovered = []
    unresolved = []
    account_results = {}
    for item in pending:
        key = item["key"]
        request_started_at = item["request_started_at"]
        if request_started_at is None:
            if recover_unsent_claim(key, session_factory=factory):
                recovered.append(key)
            else:
                unresolved.append(key)
            continue
        if record_provider:
            account_id = item["account_id"]
            if account_id not in account_results:
                try:
                    account_results[account_id] = record_provider(account_id, item["claimed_at"])
                except Exception as exc:
                    account_results[account_id] = {"ok": False, "complete": False, "reason": str(exc), "records": []}
            response = account_results[account_id]
            if not isinstance(response, dict) or not response.get("ok") or not response.get("complete"):
                require_reconciliation(key, "broker reconciliation query incomplete", session_factory=factory)
                unresolved.append(key)
                continue
            candidates = response.get("records") or []
        else:
            candidates = broker_records or []
        record = next((candidate for candidate in candidates if _record_matches_attempt(candidate, item)), None)
        if record is not None and reconcile_known_broker_order(key, record, session_factory=factory):
            matched.append(key)
        else:
            require_reconciliation(key, "broker submission outcome is ambiguous", session_factory=factory)
            unresolved.append(key)
    return {"ok": not unresolved, "recovered_unsent": recovered, "matched_broker_orders": matched, "unresolved": unresolved}


def _broker_reference_values(record):
    values = set()
    stack = [record]
    while stack:
        value = stack.pop()
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            if key in {"client_order_id", "clientOrderId", "label", "comment"} and item not in (None, ""):
                values.add(str(item))
            elif isinstance(item, dict):
                stack.append(item)
    return values


def _record_matches_attempt(record, attempt):
    if not isinstance(record, dict):
        return False
    references = _broker_reference_values(record)
    internal_key = str(attempt["key"])
    client_id = str(attempt.get("client_order_id") or "")
    reference_match = internal_key in references or client_id in references or any(
        internal_key in value for value in references if value.startswith("NathauxFX ")
    )
    if not reference_match:
        return False
    account = str(record.get("account_id") or record.get("ctidTraderAccountId") or "")
    if account != str(attempt["account_id"]):
        return False
    symbol = str(record.get("symbol") or "").upper().replace("/", "")
    direction = str(record.get("side") or record.get("direction") or "").upper()
    if symbol != str(attempt["symbol"]) or direction != str(attempt["direction"]):
        return False
    if not any(record.get(field) not in (None, "") for field in ("order_id", "orderId", "position_id", "positionId", "deal_id", "dealId")):
        return False
    return True


def _transition_attempt(key, allowed, status, *, result=None, error=None, request_started=False, lifecycle_status=None, reconciliation_status=None, session_factory=None):
    factory = session_factory or SessionLocal
    session = factory()
    try:
        unlocked = session.query(TradeSubmissionAttempt).filter_by(idempotency_key=str(key)).one_or_none()
        if unlocked is None:
            session.rollback(); return False
        session.query(IndicatorEventLifecycle).filter_by(
            event_id=unlocked.event_id, mode=unlocked.mode,
            owner_id=unlocked.owner_id, account_id=unlocked.account_id,
        ).with_for_update().one()
        row = session.query(TradeSubmissionAttempt).filter_by(idempotency_key=str(key)).with_for_update().one_or_none()
        if row is None or row.attempt_status not in allowed:
            session.rollback(); return False
        now = datetime.now(timezone.utc)
        values = {
            TradeSubmissionAttempt.attempt_status: status,
            TradeSubmissionAttempt.updated_at: now,
        }
        if request_started and row.request_started_at is None:
            values[TradeSubmissionAttempt.request_started_at] = now
        if result is not None:
            values[TradeSubmissionAttempt.broker_response] = _json(result)
            values[TradeSubmissionAttempt.broker_order_id] = str((result or {}).get("order_id") or "") or None
            values[TradeSubmissionAttempt.broker_position_id] = str((result or {}).get("position_id") or "") or None
        if error:
            values[TradeSubmissionAttempt.last_error] = str(error)
        if reconciliation_status:
            values[TradeSubmissionAttempt.reconciliation_status] = reconciliation_status
            if reconciliation_status == "MATCHED":
                values[TradeSubmissionAttempt.reconciled_at] = now
        attempt_changed = session.query(TradeSubmissionAttempt).filter(
            TradeSubmissionAttempt.id == row.id,
            TradeSubmissionAttempt.attempt_status.in_(allowed),
        ).update(values, synchronize_session=False)
        if attempt_changed != 1:
            logger.error("Invalid or lost submission transition %s -> %s for %s", sorted(allowed), status, key)
            session.rollback(); return False
        if lifecycle_status:
            lifecycle_values = {
                IndicatorEventLifecycle.status: lifecycle_status,
                IndicatorEventLifecycle.updated_at: now,
            }
            if lifecycle_status == "CONSUMED":
                lifecycle_values[IndicatorEventLifecycle.consumed_at] = now
            lifecycle_changed = session.query(IndicatorEventLifecycle).filter(
                IndicatorEventLifecycle.event_id == row.event_id,
                IndicatorEventLifecycle.mode == row.mode,
                IndicatorEventLifecycle.owner_id == row.owner_id,
                IndicatorEventLifecycle.account_id == row.account_id,
                IndicatorEventLifecycle.status.in_(allowed),
            ).update(lifecycle_values, synchronize_session=False)
            if lifecycle_changed != 1:
                logger.error("Failed closed while transitioning lifecycle for submission %s", key)
                session.rollback(); return False
        session.commit(); return True
    except Exception:
        logger.exception("Failed submission transition for %s", key)
        session.rollback(); return False
    finally:
        session.close()
