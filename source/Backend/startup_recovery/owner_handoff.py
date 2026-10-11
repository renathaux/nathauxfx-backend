"""Trusted administrative termination adapter: relinquish only, never takeover.

The caller must independently approve/pin retained platform termination evidence.
This function validates its binding, not the truth of a caller-invented document.
It is not an HTTP endpoint or an automatic recovery callback. Run it in a fresh
administrative process after the predecessor is proven terminated; never reset
the predecessor process's revoked-token set.

store.relinquish remains the lower-level fenced mutation primitive. It checks
owner/proof and its existing DB guards, but does not obtain this adapter's fresh
broker/database observations. Canonical submission safety is enforced here,
including on the locked database reread, before calling that unchanged primitive.
"""
from dataclasses import asdict, dataclass
from startup_recovery import store
from startup_recovery.checkpoints import _parse, _encoded, _digest, _valid_hash
from startup_recovery.reconcile import digest, _database_comparison, _broker_comparison, _submission_pending
from startup_recovery.types import RecoveryError, ManagerToken, HandoffEvidence, Phase
from startup_recovery.unit_of_work import reconciliation_uow


@dataclass(frozen=True)
class RelinquishmentResult:
    status: str
    evidence_sha256: str
    predecessor_epoch: int


def require(value):
    if not value:
        raise RecoveryError('RECOVERY_TERMINATION_EVIDENCE_INVALID')


def safety(value, scope):
    d,b=value['database'],value['broker']
    require(d['selection']['account_id']==scope.account_id
            and d['selection']['environment']==scope.environment)
    require(b['account_id']==scope.account_id and b['environment']==scope.environment)
    require(b['complete'] is True and b['authenticated'] is True)
    require(b['positions']==[] and b['orders']==[])
    settings=[r for r in d['settings'] if r.get('setting_name')=='live_auto_trade_enabled']
    require(len(settings)==1 and settings[0]['setting_value']=='false')
    for row in d['submissions']:
        require(not _submission_pending(row))
    for row in d['lifecycles']:
        require(row.get('status') not in {'SUBMITTING','RECONCILIATION_REQUIRED'})
        require(not row.get('broker_position_id'))
        require(not (row.get('tp1_requested_at') and not row.get('tp1_completed_at')))
        require((row.get('management_state') or {}).get('protection_state') not in {'PENDING','FAILED'})
    return digest(_database_comparison(d)),digest(_broker_comparison(b))


def relinquish_terminated_owner(*,session_factory,scope,expected_attempt_id,expected_epoch,
        predecessor_boot_id,predecessor_build_id,predecessor_deployment_id,
        successor_release_identity,evidence_document,expected_evidence_sha256,safety_reader):
    mutation_started=False
    try:
        require(isinstance(evidence_document,bytes) and _valid_hash(expected_evidence_sha256))
        require(_digest(evidence_document)==expected_evidence_sha256)
        doc=_parse(evidence_document)
        require(_encoded(doc)==evidence_document)
        require(set(doc)=={'schema','kind','scope','attempt_id','epoch','boot_id','build_id',
            'deployment_id','successor_release_identity','predecessor_status',
            'operations_reconciled','termination_record_sha256'})
        require(type(doc['schema']) is int and doc['schema']==1)
        require(doc['kind']=='operator-termination' and doc['predecessor_status']=='TERMINATED'
            and doc['operations_reconciled'] is True and _valid_hash(doc['termination_record_sha256']))
        require(type(expected_epoch) is int and expected_epoch>0 and type(doc['epoch']) is int)
        require(_valid_hash(successor_release_identity))
        for value in (expected_attempt_id,predecessor_boot_id,predecessor_build_id,predecessor_deployment_id):
            require(isinstance(value,str) and bool(value))
        require(doc['scope']==asdict(scope) and doc['attempt_id']==expected_attempt_id
            and doc['epoch']==expected_epoch and doc['boot_id']==predecessor_boot_id
            and doc['build_id']==predecessor_build_id and doc['deployment_id']==predecessor_deployment_id
            and doc['successor_release_identity']==successor_release_identity)
        require(callable(safety_reader) and callable(getattr(safety_reader,'database',None)))
        first=safety_reader(scope)
        expected=safety(first,scope)
        require(safety(safety_reader(scope),scope)==expected)
        token=ManagerToken(scope,expected_attempt_id,expected_epoch,predecessor_boot_id)
        with reconciliation_uow(session_factory,scope) as uow:
            account,attempt=store.require_owner(uow.session,token)
            require(account.phase==Phase.BLOCKED and attempt.phase==Phase.BLOCKED
                and attempt.build_id==predecessor_build_id)
            require((account.handoff_evidence or {}).get('evidence_hash')!=expected_evidence_sha256)
            current=safety_reader.database(uow.session,scope)
            require(safety(dict(database=current,broker=first['broker']),scope)[0]==expected[0])
            require(not store._unsettled(uow.session,scope))
            proof=HandoffEvidence('operator-termination',expected_evidence_sha256,
                predecessor_boot_id,True,True)
            mutation_started=True
            store.relinquish(uow.session,token,proof)
        return RelinquishmentResult('RELINQUISHED',expected_evidence_sha256,expected_epoch)
    except RecoveryError:
        raise
    except Exception:
        code='RECOVERY_HANDOFF_COMMIT_INSPECT_REQUIRED' if mutation_started else 'RECOVERY_TERMINATION_EVIDENCE_INVALID'
        raise RecoveryError(code) from None
