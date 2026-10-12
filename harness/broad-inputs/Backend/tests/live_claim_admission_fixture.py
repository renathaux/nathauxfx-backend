"""Scoped LIVE claim admission via the real recovery coordinator, never tokens."""
from contextlib import ExitStack
import json

import pytest

from test_recovery_store import db, store_api
from recovery_integration_fixture import prepare_database, recovery_server, admit


@pytest.fixture
def live_claim_admission(db, monkeypatch, tmp_path):
    initialized = False
    with ExitStack() as stack:
        def admitted(account_id):
            nonlocal initialized
            if not initialized:
                prepare_database(db, account_id)
                initialized = True
            else:
                # Selected-account input, not manager ownership/readiness.
                from models import RuntimeSetting
                with db.begin() as session:
                    row = session.query(RuntimeSetting).filter_by(
                        setting_name='ctrader_active_account').one()
                    row.setting_value = json.dumps(dict(account_id=account_id, env='demo'))
            harness = recovery_server(db, monkeypatch, tmp_path / account_id,
                account_id=account_id)
            admit(harness, boot='claim-fixture-' + account_id)
            stack.enter_context(harness.server.runtime_context(entries=True))
            return db
        yield admitted
