"""No cached admission survives an epoch transition or incomplete phase."""
import importlib
import pytest
from test_recovery_store import store_api, db, begin, cutover


def test_entry_gate_rechecks_every_phase(store_api, db):
    try:
        gate = importlib.import_module('startup_recovery.admission').entry_gate
    except ModuleNotFoundError:
        pytest.fail('Entry recovery gate missing')
    t = begin(store_api, db)
    cutover(store_api, db, t)
    with db.begin() as s:
        store_api.acquire_owner(s, t)
    phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED', 'STATE_DISCOVERED',
              'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY', 'NEW_ENTRIES_READY']
    with db.begin() as s:
        with pytest.raises(store_api.RecoveryError, match='LIVE_RECOVERY_INCOMPLETE'):
            gate(s, t)
    for old, new in zip(phases, phases[1:]):
        with db.begin() as s:
            store_api.advance(s, t, old, new, 'e' * 64)
        with db.begin() as s:
            if new == 'NEW_ENTRIES_READY':
                gate(s, t)
            else:
                with pytest.raises(store_api.RecoveryError, match='LIVE_RECOVERY_INCOMPLETE'):
                    gate(s, t)


def test_live_claim_rejects_missing_token_before_lifecycle_or_claim_write(db):
    from services.trade_submission_service import claim_submission
    from models import TradeSubmissionAttempt
    result = claim_submission('evt', 'LIVE', '123', 'EURUSD', 'setup',
                              {'action': 'BUY'}, session_factory=db)
    assert result['ok'] is False
    assert result['reason'] == 'RECOVERY_TOKEN_MISSING'
    with db() as s:
        assert s.query(TradeSubmissionAttempt).count() == 0


def test_nonstudio_final_dispatch_also_requires_current_epoch(store_api, db):
    from startup_recovery import admission
    from startup_recovery.runtime import manager_context
    from test_recovery_fencing import ready, create_submission
    from unittest.mock import Mock
    dispatch = getattr(admission, 'dispatch_claimed_order', None)
    assert callable(dispatch), 'Non-Studio final dispatcher must use the same recovery guard'
    t = begin(store_api, db)
    ready(store_api, db, t)
    create_submission(db)
    submit = Mock(return_value={'ok': True})
    with pytest.raises(store_api.RecoveryError, match='RECOVERY_TOKEN_MISSING'):
        dispatch(db, 'claim', submit)
    submit.assert_not_called()
    with manager_context(t):
        with pytest.raises(store_api.RecoveryError,match='RECOVERY_EXECUTION_IDENTITY_UNVERIFIED'):
            dispatch(db, 'claim', submit)
    submit.assert_not_called()  # epoch alone cannot create missing immutable intent


def test_studio_setup_creation_is_fenced_before_saved_lookup_or_lifecycle_write(db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from services import strategy_studio_live_candidate as candidate
    from startup_recovery.types import RecoveryError
    from models import StrategySetupLifecycle
    lookup = Mock(side_effect=AssertionError('Saved lookup must follow recovery admission'))
    monkeypatch.setattr(candidate, 'lock_saved', lookup)
    with pytest.raises(RecoveryError, match='RECOVERY_TOKEN_MISSING'):
        candidate._persist_eligible_setup(db, setup_id='s', owner_id='owner', strategy_id='strategy',
            account_identity=SimpleNamespace(account_id='123', environment='demo'),
            account_scope='CTRADER:DEMO:123', symbol='EURUSD', direction='BUY',
            definition={}, entry_binding={})
    lookup.assert_not_called()
    with db() as session:
        assert session.query(StrategySetupLifecycle).count() == 0
