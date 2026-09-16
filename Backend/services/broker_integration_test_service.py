"""Durable one-shot DEMO broker proof, isolated from strategy persistence."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import re
from typing import Protocol

from sqlalchemy import select, update
from models import BrokerIntegrationTestSubmission as Submission
from services.account_execution_coordination import TEST_ACCOUNT, ExecutionFenced, account_lock
from services.broker_integration_test_errors import BlockerCode as Code, BrokerTestBlocked, safe_error_code


@dataclass(frozen=True)
class TestRequest:
    account_id: str
    test_id: str
    symbol: str
    confirmed: bool


@dataclass(frozen=True)
class Preflight:
    account_id: str
    is_live: bool
    symbol: str
    symbol_id: int
    min_volume: int
    step_volume: int
    max_volume: int
    bid: float
    ask: float
    cleanup_ready: bool


@dataclass(frozen=True)
class Reconciliation:
    complete: bool
    account_id: str = ''
    symbol_id: int = 0
    side: str = ''
    reference: str = ''
    order_id: str = ''
    position_id: str = ''
    volume: int = 0
    open_volume: int = 0
    closed_volume: int = 0


class BrokerAdapter(Protocol):
    def fresh_preflight(self, request: TestRequest) -> Preflight: ...
    def submit(self, row: Submission) -> None: ...
    def reconcile(self, row: Submission) -> Reconciliation: ...
    def close(self, row: Submission, evidence: Reconciliation) -> None: ...


def validate_request(request):
    if (request.account_id != TEST_ACCOUNT or request.symbol != 'EURUSD'
            or request.confirmed is not True
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request.test_id)):
        raise BrokerTestBlocked(Code.INVALID_REQUEST)


class BrokerIntegrationTestService:
    def __init__(self, session_factory, adapter: BrokerAdapter):
        self.sessions = session_factory
        self.adapter = adapter

    def preflight(self, request):
        validate_request(request)
        evidence = self.adapter.fresh_preflight(request)
        if evidence.account_id != TEST_ACCOUNT or evidence.is_live is not False:
            raise BrokerTestBlocked(Code.DEMO_PROOF_REQUIRED)
        if evidence.symbol != 'EURUSD' or evidence.symbol_id <= 0:
            raise BrokerTestBlocked(Code.SYMBOL_METADATA_INVALID)
        if evidence.cleanup_ready is not True:
            raise BrokerTestBlocked(Code.CLEANUP_UNAVAILABLE)
        if (evidence.min_volume <= 0 or evidence.step_volume <= 0
                or evidence.min_volume % evidence.step_volume
                or evidence.max_volume < evidence.min_volume):
            raise BrokerTestBlocked(Code.INVALID_BROKER_VOLUME)
        if not (0 < evidence.bid < evidence.ask):
            raise BrokerTestBlocked(Code.QUOTE_INVALID)
        return asdict(evidence)

    def run(self, request, recover=False):
        validate_request(request)
        with account_lock(self.sessions, request.account_id):
            with self.sessions() as session:
                row = session.get(Submission, request.test_id)
                if row and (row.account_id != request.account_id or row.symbol != request.symbol):
                    raise BrokerTestBlocked(Code.IDENTITY_MISMATCH)
                if row and row.state == 'CLOSED':
                    return self._result(row)
                if row is None:
                    if recover:
                        raise BrokerTestBlocked(Code.RECOVERY_ID_UNKNOWN)
                    if session.scalar(select(Submission.test_id).where(Submission.unresolved_account == TEST_ACCOUNT)):
                        raise ExecutionFenced('Another unresolved DEMO broker integration test')
                    evidence = self.preflight(request)
                    row = Submission(test_id=request.test_id, account_id=request.account_id,
                        unresolved_account=request.account_id, environment='demo', symbol='EURUSD',
                        symbol_id=evidence['symbol_id'], side='BUY', volume=evidence['min_volume'],
                        reference='bit-' + hashlib.sha256(request.test_id.encode()).hexdigest()[:40],
                        state='PREPARED', created_at=datetime.now(timezone.utc), preflight_evidence=evidence)
                    session.add(row)
                    session.commit()
                try:
                    if row.request_started_at is None:
                        if recover:
                            # No dispatch marker means no opening call occurred.
                            row.state = 'CLOSED'
                            row.unresolved_account = None
                            row.reconciled_at = datetime.now(timezone.utc)
                            session.commit()
                            return self._result(row)
                        # Revalidate after any PREPARED crash; persist marker before network.
                        fresh = self.preflight(request)
                        if fresh['symbol_id'] != row.symbol_id or fresh['min_volume'] != row.volume:
                            raise BrokerTestBlocked(Code.SYMBOL_METADATA_INVALID)
                        marked = session.execute(update(Submission).where(
                            Submission.test_id == row.test_id,
                            Submission.request_started_at.is_(None),
                            Submission.unresolved_account == TEST_ACCOUNT,
                        ).values(request_started_at=datetime.now(timezone.utc), state='REQUEST_STARTED'))
                        if marked.rowcount != 1:
                            raise ExecutionFenced('Opening marker already claimed')
                        session.commit()
                        self.adapter.submit(row)
                    evidence = self.adapter.reconcile(row)
                    self._verify(row, evidence)
                    if evidence.open_volume and row.close_started_at is not None:
                        # A still-open snapshot and empty pending orders do not prove
                        # that a previous close request is terminal. Never replay it.
                        raise BrokerTestBlocked(Code.UNRESOLVED_CLOSE)
                    row.broker_order_id = evidence.order_id
                    row.broker_position_id = evidence.position_id
                    row.reconciliation_evidence = asdict(evidence)
                    if evidence.open_volume:
                        row.state = 'OPEN'
                        row.open_evidence = asdict(evidence)
                        session.commit()
                        # Resume the committed same identity through the read-only path.
                        # The request marker remains authoritative; no opening call here.
                        session.refresh(row)
                        evidence = self.adapter.reconcile(row)
                        self._verify(row, evidence)
                        row.duplicate_evidence = asdict(evidence)
                        session.commit()
                    if evidence.open_volume:
                        marked = session.execute(update(Submission).where(
                            Submission.test_id == row.test_id,
                            Submission.close_started_at.is_(None),
                            Submission.unresolved_account == TEST_ACCOUNT,
                        ).values(close_started_at=datetime.now(timezone.utc), state='CLOSING'))
                        if marked.rowcount != 1:
                            raise BrokerTestBlocked(Code.UNRESOLVED_CLOSE)
                        session.commit()
                        self.adapter.close(row, evidence)
                        evidence = self.adapter.reconcile(row)
                        self._verify(row, evidence)
                    if evidence.open_volume or evidence.closed_volume != row.volume:
                        raise BrokerTestBlocked(Code.UNRESOLVED_CLOSE)
                    row.state = 'CLOSED'
                    row.unresolved_account = None
                    row.reconciliation_evidence = asdict(evidence)
                    row.reconciled_at = datetime.now(timezone.utc)
                    row.last_error = None
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    row = session.get(Submission, request.test_id)
                    row.state = 'NEEDS_RECOVERY'
                    # Deliberately never persist exception text, raw credentials or responses.
                    row.last_error = safe_error_code(exc)
                    session.commit()
                return self._result(row)

    @staticmethod
    def _verify(row, evidence):
        if not evidence.complete:
            raise BrokerTestBlocked(Code.HISTORY_INCOMPLETE)
        if (evidence.account_id != row.account_id
                or evidence.symbol_id != row.symbol_id or evidence.side != row.side
                or evidence.reference != row.reference or evidence.volume != row.volume
                or not evidence.order_id or not evidence.position_id
                or (row.broker_position_id and row.broker_position_id != evidence.position_id)
                or evidence.open_volume < 0 or evidence.closed_volume < 0
                or evidence.open_volume + evidence.closed_volume != row.volume):
            raise BrokerTestBlocked(Code.IDENTITY_MISMATCH)

    @staticmethod
    def _result(row):
        return {'test_id': row.test_id, 'account_id': row.account_id, 'state': row.state,
                'reference': row.reference, 'broker_order_id': row.broker_order_id,
                'broker_position_id': row.broker_position_id, 'last_error': row.last_error,
                'open_evidence': row.open_evidence, 'duplicate_evidence': row.duplicate_evidence,
                'reconciliation_evidence': row.reconciliation_evidence}
