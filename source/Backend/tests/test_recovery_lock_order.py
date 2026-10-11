"""Lock topology is enforced before SQL, not recovered by timeout/retry."""
import importlib
import pytest
from test_recovery_store import store_api, db, begin
from test_recovery_fencing import ready


def module():
    try: return importlib.import_module('startup_recovery.unit_of_work')
    except ModuleNotFoundError: pytest.fail('Canonical operation unit of work missing')


def test_lock_inversion_rejected_before_query(store_api, db):
    m = module(); token = begin(store_api,db); ready(store_api,db,token)
    with m.operation_uow(db,token) as uow:
        uow.acquire(4,'owner')
        uow.acquire(6,'lifecycle')
        with pytest.raises(RuntimeError,match='RECOVERY_LOCK_ORDER'):
            uow.acquire(3,'selection')


def test_nested_competing_transaction_is_rejected(store_api,db):
    m = module(); token = begin(store_api,db); ready(store_api,db,token)
    with m.operation_uow(db,token) as outer:
        assert m.current_session() is outer.session
        with pytest.raises(RuntimeError,match='RECOVERY_NESTED_TRANSACTION'):
            with m.operation_uow(db,token): pass
        with db() as competing:
            with pytest.raises(RuntimeError,match='RECOVERY_SESSION_CONFLICT'):
                m.ordered(competing,4,'owner')


def test_current_owner_read_reuses_transaction_without_self_lock(store_api,db):
    m = module(); token = begin(store_api,db); ready(store_api,db,token)
    with m.operation_uow(db,token) as uow:
        store_api.require_owner(uow.session,token)
        with m.admitted_read_session(db) as same:
            assert same is uow.session
            owner,_ = store_api.require_owner(same,token,lock=False)
            assert owner.owner_epoch == token.epoch
        with pytest.raises(RuntimeError,match='RECOVERY_NETWORK_IN_TRANSACTION'):
            m.require_network_boundary()
    m.require_network_boundary()


def test_independent_account_lock_is_busy_and_releases(store_api,db):
    from concurrent.futures import ThreadPoolExecutor
    m = module(); token = begin(store_api,db); ready(store_api,db,token)
    def other():
        with m.operation_uow(db,token): return 'acquired'
    with ThreadPoolExecutor(max_workers=1) as worker:
        with m.operation_uow(db,token):
            with pytest.raises(RuntimeError,match='RECOVERY_ACCOUNT_BUSY'):
                worker.submit(other).result(timeout=3)
        assert worker.submit(other).result(timeout=3) == 'acquired'


def test_unsorted_same_rank_rejected(store_api,db):
    m = module(); token = begin(store_api,db); ready(store_api,db,token)
    with m.operation_uow(db,token) as uow:
        uow.acquire(6,'z')
        with pytest.raises(RuntimeError,match='RECOVERY_LOCK_ORDER'):
            uow.acquire(6,'a')


def test_real_authorization_reads_admitted_checkpoint_without_owner_self_conflict(store_api,db,tmp_path,monkeypatch):
    import ctrader_connector as connector
    from startup_recovery.checkpoint_store import RuntimeWriter, checkpoint_producer
    from unittest.mock import Mock
    token = begin(store_api,db); ready(store_api,db,token)
    path = tmp_path/'accounts.json'
    monkeypatch.setattr(connector,'CTRADER_ACCOUNTS_PATH',path)
    writer = RuntimeWriter(db,token,{'ctrader_accounts':(path,{}, {'active_account_id':'123','active_account_env':'demo'})},'e'*64)
    def exchange(sock,kind,payload,expected):
        if kind == connector.PAYLOAD_GET_ACCOUNT_LIST_BY_ACCESS_TOKEN_REQ:
            return {'payloadType':expected,'payload':{'ctidTraderAccount':[{'ctidTraderAccountId':123}]}}
        assert kind == connector.PAYLOAD_ACCOUNT_AUTH_REQ
        return {'payloadType':expected,'payload':{'ctidTraderAccountId':123}}
    monkeypatch.setattr(connector,'send_ctrader_request',exchange)
    with db.begin() as held:
        store_api.require_owner(held,token)
        with checkpoint_producer(writer):
            connector.authorize_ctrader_account(Mock(),{'access_token':'fixture-only','env':'demo'},'123')
    with db() as s: assert store_api.entries_ready(s,token)
