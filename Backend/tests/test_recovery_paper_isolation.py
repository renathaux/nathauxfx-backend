from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from test_recovery_store import store_api, db
from test_recovery_checkpoint_publication import admitted, envelope


@pytest.mark.parametrize('field', ['paper_active_trades', 'paper_trade_history'])
def test_malformed_paper_entries_are_rejected_not_silently_dropped(field):
    from strategies.shared import stage_admitted_paper_payload
    from startup_recovery.types import RecoveryError
    payload = dict(paper_active_trades=[], paper_trade_history=[], paper_setup_locks={}, last_paper_reset=0)
    payload[field] = [None]
    with pytest.raises(RecoveryError):
        stage_admitted_paper_payload(payload)


def test_semantically_malformed_committed_paper_is_quarantined_before_live_publication(tmp_path):
    from test_recovery_publication import accepted_candidate
    from test_recovery_reconciliation import empty, recovery, snapshot
    api = recovery()
    d, b = empty()
    candidates = accepted_candidate(tmp_path, d, 'paper_backup', dict(
        paper_active_trades=[None], paper_trade_history=[], paper_setup_locks={}, last_paper_reset=0))
    result = api.reconcile(snapshot(api, d, b), candidates)
    assert result.ok
    assert 'paper_backup' in result.quarantined
    assert 'paper_backup' not in result.checkpoints


def test_corrupt_committed_paper_manifest_does_not_abort_live_discovery(db, tmp_path, monkeypatch):
    from startup_recovery import server_adapter
    from test_recovery_server_adapter import adapter
    from test_recovery_reconciliation import empty, recovery, snapshot
    d, b = empty()
    paths = {'paper_backup': tmp_path / 'paper.json', 'live_backup': tmp_path / 'live.json'}
    monkeypatch.setattr(server_adapter, 'state_paths', lambda api: paths)
    d['checkpoint_heads'] = [dict(kind='paper_backup', manifest_hash='a' * 64)]
    source = snapshot(recovery(), d, b)
    candidates = adapter(db).checkpoints(source)
    result = recovery().reconcile(source, candidates)
    assert result.ok and 'paper_backup' in result.quarantined
    assert list(tmp_path.iterdir()) == []


def test_unadmitted_paper_callbacks_cannot_touch_state_or_read_broker():
    from startup_recovery.paper import install
    callbacks = [Mock() for _ in range(3)]
    shared = SimpleNamespace(PAPER_BACKUP_FILE='/reviewed/paper.json',
        update_paper_trade=callbacks[0], run_weekly_paper_reset=callbacks[1],
        run_monthly_paper_reset=callbacks[2])
    install(shared)
    assert shared.update_paper_trade('EURUSD') is None
    assert shared.run_weekly_paper_reset() is None
    assert shared.run_monthly_paper_reset(force=True) is None
    for callback in callbacks: callback.assert_not_called()
    assert shared.PAPER_RECOVERY_STATE['ready'] is False


def test_admitted_paper_runs_existing_policy_without_writing_or_enabling_preference(store_api, db, tmp_path):
    import json
    from startup_recovery.paper import install
    from startup_recovery.checkpoint_store import RuntimeWriter, checkpoint_producer
    token = admitted(store_api, db)
    path = tmp_path / 'paper.json'
    callback = Mock(return_value='unchanged-policy-result')
    shared = SimpleNamespace(PAPER_BACKUP_FILE=path, update_paper_trade=callback,
        run_weekly_paper_reset=Mock(), run_monthly_paper_reset=Mock())
    install(shared)
    writer = RuntimeWriter(db, token, {'paper_backup': (
        path, json.loads(envelope(token).raw)['identity'], {})}, 'd' * 64,
        absent_kinds={'paper_backup'})
    with checkpoint_producer(writer):
        assert shared.update_paper_trade('EURUSD', price=1) == 'unchanged-policy-result'
    callback.assert_called_once_with('EURUSD', price=1)
    assert list(tmp_path.iterdir()) == []
