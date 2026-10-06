"""A fresh interpreter resolves committed heads from the same external root.

Only the disposable PostgreSQL fixture is used. No broker calls or file migration.
"""
import json

from test_recovery_store import db, store_api
from test_recovery_checkpoint_publication import admitted, envelope
from test_runtime_paths import child, roots, readonly_source


def test_generation_restart_with_readonly_application_source(db, store_api, tmp_path, readonly_source):
    from startup_recovery.checkpoint_store import RuntimeWriter, load
    from startup_recovery.checkpoints import provision_directory
    from models import RecoveryCheckpointHead
    state = tmp_path/'state'
    provision_directory(state)
    token = admitted(store_api, db)
    fields = json.loads(envelope(token).raw)['identity']
    target = state/'live_backup.json'
    writer = RuntimeWriter(db, token, {'live_backup': (target, fields, {})},
                           'd'*64, absent_kinds={'live_backup'})
    writer.write(target, 'live_backup', {'protected_sl':'1.1025'})
    assert not target.exists()
    with db() as session:
        row = session.query(RecoveryCheckpointHead).one()
        manifest, fingerprint = row.manifest_hash, row.file_hash
        engine = session.get_bind()
        schema = engine.get_execution_options()['schema_translate_map'][None]
    assert (state/'.recovery-generations'/manifest/'live_backup.json').is_file()
    # New process, fresh path import and DB session, no inherited writer/cache.
    code = '''
import os, paths
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from startup_recovery.checkpoint_store import load, RuntimeWriter, checkpoint_producer, write_runtime
from startup_recovery.types import AccountScope, ManagerToken
engine = create_engine(os.environ['VERIFIER_TEST_PG_DSN']).execution_options(schema_translate_map={None: %r})
factory = sessionmaker(engine)
accepted = load(factory, AccountScope('ctrader','demo','123'), paths.DATA_DIR, 'live_backup')
assert accepted.payload == {'protected_sl':'1.1025'}
assert accepted.checkpoint_hash == %r
scope = AccountScope('ctrader','demo','123')
token = ManagerToken(scope, %r, %r, %r)
target = paths.DATA_DIR/'live_backup.json'
writer = RuntimeWriter(factory, token, {'live_backup': (target, %r, accepted.payload)}, 'd'*64)
with checkpoint_producer(writer):
    write_runtime(target, 'live_backup', {'protected_sl':'1.1030'})
assert load(factory, scope, paths.DATA_DIR, 'live_backup').payload == {'protected_sl':'1.1030'}
assert not target.exists()
# Actual settings/account/analytics consumers retain their canonical bindings.
# These are disposable test records, never a user's selected account/settings.
import api, ctrader_connector
from services import settings_service
from startup_recovery.server_adapter import state_paths
bindings = state_paths(api)
fields = %r
local = RuntimeWriter(factory, token, {
    kind: (bindings[kind], fields, {} if kind != 'visits' else [])
    for kind in ('app_settings','feature_flags','ctrader_accounts','visits')
}, 'd'*64)
with checkpoint_producer(local):
    settings_service.save_risk_settings({'riskPerTradePct':0.5})
    settings_service.save_feature_flags({'healthPage':False})
    ctrader_connector.save_ctrader_account_settings({'forgotten_account_ids':['test-only']})
    api.save_visits([{'time':1,'visitor_id':'test-only','country':'Local'}])
assert load(factory, scope, paths.DATA_DIR, 'app_settings').payload['risk']['riskPerTradePct'] == 0.5
assert load(factory, scope, paths.DATA_DIR, 'ctrader_accounts').payload['forgotten_account_ids'] == ['test-only']
assert load(factory, scope, paths.DATA_DIR, 'visits').payload[0]['visitor_id'] == 'test-only'
assert all(not bindings[kind].exists() for kind in ('app_settings','feature_flags','ctrader_accounts','visits'))
from ctrader_account_context import pinned_account, AccountIdentity
import pandas as pd
with pinned_account(AccountIdentity('123','demo')):
    assert ctrader_connector.persist_ctrader_candle_cache('EURUSD','5m',pd.DataFrame([{'close':1}]))
assert (paths.CANDLE_CACHE_DIR/'demo_123_EURUSD_5m.json').is_file()
engine.dispose()
''' % (schema, fingerprint, token.attempt_id, token.epoch, token.boot_id, fields, fields)
    result = child(code, roots(tmp_path), source=readonly_source)
    assert result.returncode == 0, result.stderr
    # Changing to an empty root cannot select retained source files or manufacture trust.
    empty = tmp_path/'empty-state'
    provision_directory(empty)
    from startup_recovery.types import RecoveryError
    import pytest
    with pytest.raises(RecoveryError):
        load(db, token.scope, empty, 'live_backup')
    assert list(empty.iterdir()) == []
