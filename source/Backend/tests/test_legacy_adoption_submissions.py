"""Adoption and normal reconciliation share the real submission safety boundary."""
from copy import deepcopy

import pytest

from startup_recovery.reconcile import DiscoverySnapshot, _submission_pending, reconcile
from startup_recovery.types import RecoveryError
from test_legacy_adoption_semantics import inputs, validate
from test_recovery_reconciliation import TOKEN


# Literal expectations derived from the producer and existing reconciliation
# contract, not from the predicate under test. Historical status alone is not
# sufficient: send intent, acceptance evidence and companion states matter.
CASES = [
    *[(dict(attempt_status=status, reconciliation_status=companion), False)
      for status in ('FAILED_BEFORE_SEND', 'DEFINITELY_REJECTED',
                     'ACCEPTED_PROTECTION_FAILED', 'ACCEPTED')
      for companion in ('NOT_REQUIRED', 'RECONCILED', 'MATCHED')],
    # These spellings are not terminal statuses in certified reconciliation.
    *[(dict(attempt_status=status), True)
      for status in ('CANCELLED', 'REJECTED', 'FAILED', 'SUBMITTING', 'UNKNOWN', None)],
    *[(dict(reconciliation_status=status), True)
      for status in ('RECONCILIATION_REQUIRED', 'PENDING', 'PROTECTION_FAILED', None)],
    (dict(send_intent={'state': 'DEFINITELY_REJECTED'}), False),
    *[(dict(send_intent={'state': state}), True)
      for state in ('UNRESOLVED', 'SENT_UNKNOWN', 'ACCEPTANCE_AMBIGUOUS',
                    'REQUEST_STARTED', 'PENDING', 'FAILED', 'UNKNOWN')],
    # Accepted intent without original accepted-execution proof is not settled.
    (dict(send_intent={'state': 'ACCEPTED'}), True),
    *[(dict(initial_protection={'state': state}), True)
      for state in ('UNRESOLVED', 'REQUEST_STARTED', 'RECONCILIATION_REQUIRED')],
    (dict(initial_protection={'state': 'CONFIRMED'}), False),
]


@pytest.mark.parametrize('overrides,pending', CASES)
def test_submission_decision_matches_normal_reconciliation(overrides, pending):
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
    else:
        assert validate(payloads, database, broker).payloads == payloads
    assert (payloads, database, broker) == before


@pytest.mark.parametrize('lifecycle', [
    {'status': 'RECONCILIATION_REQUIRED'},
    {'tp1_requested_at': '2026-10-01T00:00:00Z', 'tp1_completed_at': None},
    {'management_state': {'protection_state': 'PENDING'}},
    {'management_state': {'protection_state': 'FAILED'}},
])
def test_settled_submission_cannot_bypass_lifecycle_blockers(lifecycle):
    payloads, database, broker = inputs()
    database['submissions'] = [dict(attempt_status='FAILED_BEFORE_SEND',
                                   reconciliation_status='NOT_REQUIRED')]
    database['lifecycles'] = [lifecycle]
    with pytest.raises(RecoveryError, match='LEGACY_ADOPTION_SEMANTICS_INVALID'):
        validate(payloads, database, broker)
