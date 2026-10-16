"""Administrative Gold/news transition gates; never a startup or trading API.

Relinquished database state is necessary, not proof of maintenance isolation.
This module does not acquire ownership, allocate epochs or modify checkpoints.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json

from sqlalchemy import select, cast, Text
from sqlalchemy.exc import DBAPIError

from models import RecoveryAttempt
from startup_recovery import store
from startup_recovery.news_strategy_compatibility import require, validate_document
from startup_recovery.types import HandoffEvidence, ManagerToken, Phase, RecoveryError, scope_key


def validate_isolation(raw, expected_sha256, document, *, maintenance_image_digest, maintenance_build_identity):
    """Bind independently collected trusted platform evidence, not invent it.

    As with termination evidence, the trusted operator must prove the claims
    externally and pin the exact bytes before invocation. This is not a Render
    client and cannot turn a caller's unverified assertion into platform proof.
    """
    from startup_recovery.checkpoints import _parse, _encoded, _digest, _valid_hash
    try:
        d=validate_document(document)
        require(type(raw) is bytes and _valid_hash(expected_sha256) and _digest(raw)==expected_sha256)
        proof=_parse(raw)
        require(type(proof) is dict and _encoded(proof)==raw)
        require(isinstance(maintenance_image_digest,str) and maintenance_image_digest.startswith('sha256:')
            and _valid_hash(maintenance_image_digest[7:]) and _valid_hash(maintenance_build_identity))
        for field in ('predecessor_boot_id','predecessor_build_id','maintenance_deployment_id'):
            require(isinstance(proof.get(field),str) and bool(proof[field]))
        require(_valid_hash(proof.get('operator_record_sha256')))
        expected=dict(schema=1,kind='news-strategy-maintenance-isolation',scope_key=d['scope_key'],
            predecessor_attempt_id=d['predecessor_attempt_id'],predecessor_epoch=d['predecessor_epoch'],
            predecessor_boot_id=proof['predecessor_boot_id'],predecessor_build_id=proof['predecessor_build_id'],
            handoff_evidence_hash=d['handoff_evidence_hash'],
            maintenance_deployment_id=proof['maintenance_deployment_id'],
            maintenance_image_digest=maintenance_image_digest,maintenance_build_identity=maintenance_build_identity,
            predecessor_terminated=True,maintenance_only=True,live_auto=False,
            operator_record_sha256=proof['operator_record_sha256'])
        require(_encoded(proof)==_encoded(expected))
        return proof
    except RecoveryError:
        raise
    except (TypeError,ValueError,KeyError,AttributeError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def require_relinquished(session, scope, document, *, predecessor_boot_id, predecessor_build_id):
    """Lock and re-read exact predecessor proof without changing authority."""
    d = validate_document(document)
    require(d['scope_key'] == scope_key(scope))
    require(isinstance(predecessor_boot_id, str) and bool(predecessor_boot_id)
        and isinstance(predecessor_build_id, str) and bool(predecessor_build_id))
    require(session.get_bind().dialect.name == 'postgresql')
    with session.no_autoflush:
        account = store.account_state(session, scope, lock=True)
        require(account is not None and account.owner_attempt_id is None
            and account.phase == Phase.RELINQUISHED
            and account.accepted_manifest_hash is None
            and account.allocated_epoch == account.owner_epoch == d['predecessor_epoch']
            and account.broker == scope.broker and account.environment == scope.environment
            and account.account_id == scope.account_id)
        expected = HandoffEvidence('graceful-drain', d['handoff_evidence_hash'],
            predecessor_boot_id, True, True)
        require(account.handoff_evidence == asdict(expected))
        try:
            attempt = session.scalar(select(RecoveryAttempt).where(
                RecoveryAttempt.attempt_id == d['predecessor_attempt_id'],
                RecoveryAttempt.scope_key == d['scope_key']
            ).with_for_update(nowait=True).execution_options(populate_existing=True))
        except DBAPIError as exc:
            raise RecoveryError('RECOVERY_OWNER_BUSY' if getattr(exc.orig, 'pgcode', None) == '55P03'
                                else 'RECOVERY_DB_UNAVAILABLE') from None
        require(attempt is not None and attempt.epoch == d['predecessor_epoch']
            and attempt.boot_id == predecessor_boot_id and attempt.build_id == predecessor_build_id
            and attempt.phase == Phase.RELINQUISHED and attempt.conflict_reason is None
            and attempt.phase_evidence_hash == d['accepted_manifest_hash'])
        return ManagerToken(scope, attempt.attempt_id, attempt.epoch, attempt.boot_id)


def lock_strategy_identity(session, document, *, committed=False):
    """Saved rows (ordered), selection, Studio pointer; no waiting or edits.

    Must run only after the account lock within proven maintenance isolation.
    Existing normal CRUD does not share an account lock, so each actual row is
    locked and freshly compared, not trusted from an earlier ORM identity map.
    """
    from services.strategy_studio_models import SavedStrategy, StrategyStudioSelection
    from models import StrategyStudioLiveState
    d=validate_document(document)
    old,new=d['predecessor_row'],d['successor_row']
    def projected(row):
        return {k:(v.astimezone(timezone.utc).isoformat() if isinstance(v,datetime) else v)
                for k,v in row.items()}
    try:
        with session.no_autoflush:
            columns=[c if c.name!='definition_json' else cast(c,Text).label(c.name)
                     for c in SavedStrategy.__table__.columns if c.name in old]
            rows=session.execute(select(*columns).where(
                SavedStrategy.strategy_id.in_([old['strategy_id'],new['strategy_id']])
            ).order_by(SavedStrategy.strategy_id).with_for_update(nowait=True)).mappings().all()
            require(len(rows)==2)
            actual={}
            for row in rows:
                r=projected(row)
                r['definition_json']=json.loads(r['definition_json'],parse_float=Decimal)
                actual[r['strategy_id']]=r
            from startup_recovery.reconcile import digest
            require(all(digest(actual[r['strategy_id']])==digest(r) for r in (old,new)))
            selection=session.scalar(select(StrategyStudioSelection).where(
                StrategyStudioSelection.owner_id==old['owner_id']
            ).with_for_update(nowait=True).execution_options(populate_existing=True))
            require(selection is not None and selection.strategy_id==(new if committed else old)['strategy_id'])
            studio=session.scalar(select(StrategyStudioLiveState).where(
                StrategyStudioLiveState.owner_id==old['owner_id']
            ).with_for_update(nowait=True).execution_options(populate_existing=True))
            require(studio is not None)
            expected_studio=d['studio_after' if committed else 'studio_before']
            actual_studio=projected({k:getattr(studio,k) for k in expected_studio})
            require(digest(actual_studio)==digest(expected_studio))
            return selection,studio
    except DBAPIError as exc:
        raise RecoveryError('NEWS_STRATEGY_TRANSITION_BUSY' if getattr(exc.orig,'pgcode',None)=='55P03'
                            else 'RECOVERY_DB_UNAVAILABLE') from None


def validate_current_authority(tree, session, token, build_id, document, observation):
    """Read the complete DB-selected set after relinquishment, without admission.

    Historical predecessor identity is a read context only. No require_owner,
    acquisition, phase change, reconciliation mutation or checkpoint publication.
    """
    from models import RecoveryCheckpointHead
    from startup_recovery.checkpoints import _KINDS, _parse, _encoded
    from startup_recovery.checkpoint_lineage import read_lineage
    from startup_recovery.news_strategy_compatibility import validate_lineage
    from startup_recovery.reconcile import DiscoverySnapshot, reconcile, checkpoint_references, digest
    from startup_recovery.owner_handoff import _runtime_safety
    d=validate_document(document)
    require(d['scope_key']==scope_key(token.scope)
        and d['predecessor_attempt_id']==token.attempt_id and d['predecessor_epoch']==token.epoch)
    database=observation['database']
    require(database.get('news_strategy_compatibility')==[])
    safety=_runtime_safety(observation,token.scope)
    rows=session.scalars(select(RecoveryCheckpointHead).where(
        RecoveryCheckpointHead.scope_key==scope_key(token.scope)
    ).execution_options(populate_existing=True)).all()
    heads=[{c.name:getattr(row,c.name) for c in row.__table__.columns} for row in rows]
    kinds=[h['kind'] for h in heads]
    require(bool(heads) and len(kinds)==len(set(kinds)) and set(kinds)<=_KINDS)
    require(digest(sorted(heads,key=lambda h:h['kind']))==
            digest(sorted(database['checkpoint_heads'],key=lambda h:h['kind'])))
    require(next((h for h in heads if h['kind']=='news_trading_state'),None)==d['source_head'])
    candidates, projections={},[]
    for head in sorted(heads,key=lambda h:h['kind']):
        require(head['identity']['owner_id'] in database['runtime_owners'])
        candidate,projection=read_lineage(tree,session,token,build_id,d['accepted_manifest_hash'],head)
        if head['kind']=='news_trading_state':
            validate_lineage(database,projection,_parse(candidate.raw)['payload'],scope_key(token.scope))
            require(candidate.raw==_encoded(d['source_envelope'])
                and projection['receipt']['receipt_digest']==d['adoption_receipt_digest'])
        candidates[head['kind']]=candidate
        projections.append(projection)
    result=reconcile(DiscoverySnapshot.create(token,database,observation['broker']),candidates)
    require(result.ok and result.conflicts==() and set(result.checkpoints)==set(kinds)
        and not set(kinds).intersection(result.quarantined))
    require(checkpoint_references('news_trading_state',database,d['source_envelope']['payload'])
        ==d['before_references'])
    return candidates,dict(heads=projections,
        current_reconciliation_hash=result.manifest_hash,current_dependencies_hash=result.dependencies_hash,
        database_safety_sha256=safety[0],broker_safety_sha256=safety[1])


def project_successor(document, observation, candidates):
    """Prove the proposed identity delta in detached data, not a new epoch.

    Normal reconciliation of the real old authority already passed. Every other
    checkpoint retains ordinary dependency equality. Only the exact historical
    news candidate gets the explicit compatibility proof; no envelope is edited.
    """
    from copy import deepcopy
    from startup_recovery.checkpoints import _parse
    from startup_recovery.reconcile import checkpoint_dependencies
    from startup_recovery.news_strategy_compatibility import validate_original
    d=validate_document(document)
    after=deepcopy(observation['database'])
    require(after.get('news_strategy_compatibility')==[])
    selections=after['strategy_selections']
    require(len(selections)==1 and selections[0]['owner_id']==d['predecessor_row']['owner_id']
        and selections[0]['strategy_id']==d['predecessor_row']['strategy_id'])
    selections[0]['strategy_id']=d['successor_row']['strategy_id']
    for field in ('activated_at','updated_at'):
        if field in selections[0]: selections[0][field]=d['observed_at']
    require(after['studio_live_state']==[d['studio_before']])
    after['studio_live_state']=[deepcopy(d['studio_after'])]
    after['news_strategy_compatibility']=[deepcopy(d)]
    require(set(candidates)=={h['kind'] for h in after['checkpoint_heads']})
    for kind,candidate in candidates.items():
        if kind=='news_trading_state':
            validate_original(d,after,candidate,d['scope_key'],
                              observed_at=observation['broker']['observed_at'])
        else:
            body=_parse(candidate.raw)
            require(checkpoint_dependencies(kind,after,body['payload'])==body['identity']['dependencies_hash'])
    return after


def apply_transition(*, session_factory, scope, document, expected_receipt_digest,
        expected_authority_digest, isolation_document, expected_isolation_sha256,
        maintenance_image_digest, maintenance_build_identity, state_root, safety_reader):
    """One fenced maintenance-only identity transaction; never start recovery.

    A failed/ambiguous commit is inspection-only, not retry authorization. This
    entrypoint does not create the saved successor: both independently reviewed
    immutable rows must already exist, with only the predecessor selected.
    """
    from pathlib import Path
    from time import monotonic
    from sqlalchemy import insert, literal, JSON
    from models import RecoveryNewsStrategyCompatibility
    from startup_recovery.checkpoints import _valid_hash, _encoded
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.unit_of_work import reconciliation_uow
    from startup_recovery.owner_handoff import _runtime_safety, _runtime_fresh
    from startup_recovery.reconcile import freeze, thaw, digest, _decimal_values, _database_comparison
    mutation_started=False
    try:
        d=validate_document(thaw(freeze(document)))
        require(_valid_hash(expected_receipt_digest) and d['receipt_digest']==expected_receipt_digest
            and _valid_hash(expected_authority_digest) and d['scope_key']==scope_key(scope)
            and d['transition_authority_sha256']==expected_authority_digest
            and d['maintenance_evidence_sha256']==expected_isolation_sha256)
        isolation=validate_isolation(isolation_document,expected_isolation_sha256,d,
            maintenance_image_digest=maintenance_image_digest,maintenance_build_identity=maintenance_build_identity)
        require(callable(safety_reader) and callable(getattr(safety_reader,'database',None)))
        started=monotonic()
        first=thaw(freeze(safety_reader(scope)))
        _runtime_fresh(first['broker'],started)
        comparisons=_runtime_safety(first,scope)
        second=thaw(freeze(safety_reader(scope)))
        _runtime_fresh(second['broker'],started)
        require(second['broker']['observed_at']>=first['broker']['observed_at']
            and _runtime_safety(second,scope)==comparisons)
        root=Path(state_root)
        require(root.is_absolute())
        with _VerificationTree(root) as tree,tree.shared():
            with reconciliation_uow(session_factory,scope) as uow:
                session=uow.session
                token=require_relinquished(session,scope,d,
                    predecessor_boot_id=isolation['predecessor_boot_id'],
                    predecessor_build_id=isolation['predecessor_build_id'])
                selection,studio=lock_strategy_identity(session,d)
                require(session.scalar(select(RecoveryNewsStrategyCompatibility.scope_key).where(
                    RecoveryNewsStrategyCompatibility.scope_key==d['scope_key'])) is None)
                require(not store._unsettled(session,scope))
                current=safety_reader.database(session,scope)
                observation=dict(database=current,broker=second['broker'])
                candidates,authority=validate_current_authority(tree,session,token,
                    isolation['predecessor_build_id'],d,observation)
                require((authority['database_safety_sha256'],authority['broker_safety_sha256'])==comparisons
                    and digest(authority)==expected_authority_digest)
                projected=project_successor(d,observation,candidates)
                _runtime_fresh(second['broker'],started)
                stamp=datetime.fromisoformat(d['observed_at'].replace('Z','+00:00'))
                mutation_started=True
                session.execute(insert(RecoveryNewsStrategyCompatibility).values(
                    scope_key=d['scope_key'],predecessor_attempt_id=d['predecessor_attempt_id'],
                    predecessor_epoch=d['predecessor_epoch'],original_checkpoint_hash=d['source_head']['file_hash'],
                    predecessor_strategy_id=d['predecessor_row']['strategy_id'],
                    successor_strategy_id=d['successor_row']['strategy_id'],receipt_digest=d['receipt_digest'],
                    document=cast(literal(_encoded(_decimal_values(d)).decode(),type_=Text),JSON),created_at=stamp))
                selection.strategy_id=d['successor_row']['strategy_id']
                selection.activated_at=selection.updated_at=stamp
                studio.enabled_strategy_id=d['successor_row']['strategy_id']
                studio.updated_at=stamp
                session.flush()
                actual=safety_reader.database(session,scope)
                require(digest(_database_comparison(actual))==digest(_database_comparison(projected)))
                _runtime_fresh(second['broker'],started)
        return dict(status='COMMITTED',receipt_digest=d['receipt_digest'])
    except Exception as exc:
        if mutation_started:
            raise RecoveryError('NEWS_STRATEGY_TRANSITION_COMMIT_INSPECT_REQUIRED') from None
        if isinstance(exc,RecoveryError): raise
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def inspect_transition(*, session_factory, scope, document, expected_receipt_digest,
        expected_authority_digest, isolation_document, expected_isolation_sha256,
        maintenance_image_digest, maintenance_build_identity, state_root, safety_reader):
    """Read-only commit classification while the same maintenance isolation holds.

    The detached predecessor projection proves historical compatibility only;
    it grants no recovery/epoch/admission authority. Actual successor startup
    still runs normal reconciliation with its own allocated token.
    """
    from copy import deepcopy
    from pathlib import Path
    from time import monotonic
    from startup_recovery.cutover_evidence import _VerificationTree
    from startup_recovery.unit_of_work import reconciliation_uow
    from startup_recovery.owner_handoff import _runtime_fresh
    from startup_recovery.reconcile import freeze, thaw, digest, _database_comparison
    d=validate_document(thaw(freeze(document)))
    require(d['receipt_digest']==expected_receipt_digest and d['scope_key']==scope_key(scope)
        and d['transition_authority_sha256']==expected_authority_digest
        and d['maintenance_evidence_sha256']==expected_isolation_sha256)
    isolation=validate_isolation(isolation_document,expected_isolation_sha256,d,
        maintenance_image_digest=maintenance_image_digest,maintenance_build_identity=maintenance_build_identity)
    started=monotonic()
    observed=thaw(freeze(safety_reader(scope)))
    _runtime_fresh(observed['broker'],started)
    with _VerificationTree(Path(state_root)) as tree,tree.shared():
        with reconciliation_uow(session_factory,scope) as uow:
            session=uow.session
            token=require_relinquished(session,scope,d,
                predecessor_boot_id=isolation['predecessor_boot_id'],predecessor_build_id=isolation['predecessor_build_id'])
            current=safety_reader.database(session,scope)
            receipts=current.get('news_strategy_compatibility')
            require(type(receipts) is list and len(receipts)<=1 and not store._unsettled(session,scope))
            committed=bool(receipts)
            lock_strategy_identity(session,d,committed=committed)
            before=deepcopy(current)
            if committed:
                require(digest(receipts)==digest([d]))
                before['news_strategy_compatibility']=[]
                require(len(before['strategy_selections'])==1)
                before['strategy_selections'][0]['strategy_id']=d['predecessor_row']['strategy_id']
                before['studio_live_state']=[deepcopy(d['studio_before'])]
            observation=dict(database=before,broker=observed['broker'])
            candidates,authority=validate_current_authority(tree,session,token,
                isolation['predecessor_build_id'],d,observation)
            if committed:
                projected=project_successor(d,observation,candidates)
                require(digest(_database_comparison(projected))==digest(_database_comparison(current)))
            else:
                require(digest(authority)==expected_authority_digest)
            _runtime_fresh(observed['broker'],started)
    return dict(status='COMMITTED_COMPLETE' if committed else 'NOT_COMMITTED',receipt_digest=d['receipt_digest'])
