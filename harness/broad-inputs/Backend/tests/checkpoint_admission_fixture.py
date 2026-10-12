"""Scoped checkpoint authority through the same store/producer boundary as Task 6."""
from contextlib import ExitStack
from uuid import uuid4

import pytest

from test_recovery_store import store_api, db


@pytest.fixture
def admit_checkpoint(store_api, db):
    from startup_recovery.checkpoint_store import RuntimeWriter, checkpoint_producer
    from startup_recovery.types import AccountScope, HandoffEvidence

    with ExitStack() as stack:
        def admit(kind, path, payload, *, account_id, absent=False):
            scope = AccountScope('ctrader', 'demo', account_id)
            with db.begin() as session:
                token = store_api.begin_attempt(session, scope, str(uuid4()), 'a' * 40)
                store_api.establish_legacy_cutover(session, scope, HandoffEvidence(
                    'operator-termination', 'b' * 64, 'previous-test-release', True, True))
                store_api.acquire_owner(session, token)
            phases = ['BOOTSTRAP', 'DB_READY', 'BROKER_AUTHENTICATED',
                      'STATE_DISCOVERED', 'STATE_RECONCILED', 'POSITION_MANAGEMENT_READY']
            for old, new in zip(phases, phases[1:]):
                with db.begin() as session:
                    store_api.advance(session, token, old, new, 'd' * 64)
            # Explicit fixture facts, never inferred from a legacy file. No
            # entry-ready phase, worker token, or current-strategy authority.
            identity = dict(account_scope=f'CTRADER:DEMO:{account_id}',
                owner_id='checkpoint-fixture', symbol=None, strategy_id=None,
                config_hash=None, position_id=None, epoch=token.epoch,
                boot_id=token.boot_id, build_id='a' * 40,
                dependencies_hash='c' * 64, generation=1)
            writer = RuntimeWriter(db, token, {kind: (path, identity, payload)},
                'd' * 64, absent_kinds={kind} if absent else ())
            stack.enter_context(checkpoint_producer(writer))
            return writer
        yield admit


@pytest.fixture
def account_checkpoint(admit_checkpoint):
    import ctrader_connector
    # These read-only unit paths have no selected account. Reconciled absence
    # remains absence; it does not create a file or authorize broker execution.
    return admit_checkpoint('ctrader_accounts', ctrader_connector.CTRADER_ACCOUNTS_PATH,
        None, account_id='checkpoint-fixture', absent=True)


@pytest.fixture
def risk_checkpoint(admit_checkpoint):
    from services import settings_service
    return admit_checkpoint('app_settings', settings_service.SETTINGS_PATH,
        {'risk': dict(settings_service.DEFAULT_RISK_SETTINGS)},
        account_id='checkpoint-fixture')
