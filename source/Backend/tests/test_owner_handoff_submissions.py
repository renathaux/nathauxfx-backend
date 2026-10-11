"""Canonical submission decisions also fence administrative relinquishment."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from models import TradeSubmissionAttempt
from startup_recovery import store
from startup_recovery.owner_handoff import safety
from startup_recovery.reconcile import DiscoverySnapshot, _submission_pending, reconcile
from startup_recovery.types import RecoveryError
from test_legacy_adoption_semantics import inputs, validate
from test_legacy_adoption_submissions import CASES
from test_owner_handoff import request_args, prepared, db, migrated_database, release
from test_recovery_reconciliation import TOKEN


@pytest.mark.parametrize('overrides,pending', CASES)
def test_three_way_submission_equivalence(overrides, pending):
    payloads, database, broker = inputs()
    row = dict(id=1, attempt_status='ACCEPTED', reconciliation_status='NOT_REQUIRED',
               send_intent=None, initial_protection=None)
    row.update(overrides)
    database['submissions'] = [row]
    before = deepcopy((payloads, database, broker))
    assert _submission_pending(row) is pending
    result = reconcile(DiscoverySnapshot.create(TOKEN, database, broker), {})
    assert ('RECOVERY_OPERATIONS_UNRESOLVED' in result.conflicts) is pending
    if pending:
        with pytest.raises(RecoveryError, match='LEGACY_ADOPTION_SEMANTICS_INVALID'):
            validate(payloads, database, broker)
        with pytest.raises(RecoveryError, match='RECOVERY_TERMINATION_EVIDENCE_INVALID'):
            safety(dict(database=database, broker=broker), TOKEN.scope)
    else:
        assert validate(payloads, database, broker).payloads == payloads
        hashes = safety(dict(database=database, broker=broker), TOKEN.scope)
        assert len(hashes) == 2 and all(len(value) == 64 for value in hashes)
    assert (payloads, database, broker) == before


def submission_reader(args, overrides):
    """Seed/read a real disposable-PG row; never mock the settlement predicate."""
    factory = args['session_factory']
    now = datetime.now(timezone.utc)
    with factory.begin() as session:
        row = TradeSubmissionAttempt(event_id='synthetic-event', mode='LIVE',
            owner_id='owner', account_id=args['scope'].account_id, symbol='EURUSD',
            direction='BUY', signal_setup_id='synthetic-setup', idempotency_key='synthetic-key',
            attempt_status='ACCEPTED', reconciliation_status='NOT_REQUIRED',
            claimed_at=now, updated_at=now, broker_client_order_id='synthetic-client',
            request_payload_fingerprint='a'*64)
        for name, value in overrides.items():
            setattr(row, name, value)
        session.add(row)
    original = args['safety_reader']
    class Reader:
        def database(self, session, scope):
            state = original.database(session, scope)
            state['submissions'] = [dict(id=row.id, attempt_status=row.attempt_status,
                reconciliation_status=row.reconciliation_status, send_intent=row.send_intent,
                initial_protection=row.initial_protection)
                for row in session.scalars(select(TradeSubmissionAttempt).where(
                    TradeSubmissionAttempt.account_id == scope.account_id,
                    TradeSubmissionAttempt.mode == 'LIVE'))]
            return state

        def __call__(self, scope):
            with factory() as session:
                return dict(database=self.database(session, scope), broker=original(scope)['broker'])
    args['safety_reader'] = Reader()
    return args['safety_reader'](args['scope'])


@pytest.mark.parametrize('overrides', [
    pytest.param(dict(send_intent={'state': 'REQUEST_STARTED'}), id='request-started'),
    pytest.param(dict(send_intent={'state': 'ACCEPTANCE_AMBIGUOUS'}), id='ambiguous-send'),
    pytest.param(dict(send_intent={'state': 'UNRESOLVED'}), id='unresolved-send'),
    pytest.param(dict(send_intent={'state': 'ACCEPTED'}), id='missing-acceptance-proof'),
    pytest.param(dict(reconciliation_status='RECONCILIATION_REQUIRED'), id='reconciliation-required'),
    pytest.param(dict(reconciliation_status='PENDING'), id='pending-reconciliation'),
    pytest.param(dict(initial_protection={'state': 'UNRESOLVED'}), id='unresolved-protection'),
    pytest.param(dict(initial_protection={'state': 'REQUEST_STARTED'}), id='protection-started'),
])
def test_unresolved_pg_submission_never_calls_relinquish_or_changes_owner(request_args, monkeypatch, overrides):
    args = request_args
    state = submission_reader(args, overrides)
    assert _submission_pending(state['database']['submissions'][0]) is True
    calls = []
    original = store.relinquish
    def observed(*positional, **keywords):
        calls.append(True)
        return original(*positional, **keywords)
    monkeypatch.setattr(store, 'relinquish', observed)
    with args['session_factory']() as session:
        account = store.account_state(session, args['scope'])
        before = (account.owner_attempt_id, account.owner_epoch, account.phase,
                  deepcopy(account.handoff_evidence))
    with pytest.raises(RecoveryError, match='RECOVERY_TERMINATION_EVIDENCE_INVALID'):
        release(args)
    assert calls == []
    with args['session_factory']() as session:
        account = store.account_state(session, args['scope'])
        assert (account.owner_attempt_id, account.owner_epoch, account.phase,
                account.handoff_evidence) == before


@pytest.mark.parametrize('status', [
    'FAILED_BEFORE_SEND', 'DEFINITELY_REJECTED', 'ACCEPTED_PROTECTION_FAILED', 'ACCEPTED',
])
def test_settled_pg_history_still_allows_only_relinquishment(request_args, status):
    args = request_args
    state = submission_reader(args, dict(attempt_status=status))
    assert _submission_pending(state['database']['submissions'][0]) is False
    safety(state, args['scope'])
    assert release(args).status == 'RELINQUISHED'
    with args['session_factory']() as session:
        account = store.account_state(session, args['scope'])
        assert account.owner_attempt_id is None
        assert account.owner_epoch == args['expected_epoch']
        assert account.phase == 'RELINQUISHED'
