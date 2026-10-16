"""Pure proof for one historical Gold/news identity transition.

Not an activation API, receipt issuer, recovery admission or execution capability.
Callers must obtain receipts from the durable fenced transition and validate the
referenced original adoption/generation. No dependency hashes are rewritten.
"""
import copy
from datetime import datetime, timedelta, timezone

from live_integrity.canonical_successor import plan
from live_integrity.binding import fingerprint
from startup_recovery import checkpoints as files
from startup_recovery.adoption_evidence import AdoptedEnvelope, checked_adopted_envelope, candidate_has_receipt
from startup_recovery.legacy_semantics import news
from startup_recovery.reconcile import checkpoint_references, digest
from startup_recovery.types import RecoveryError

POLICY = 'gold-three-defaults-expired-news-v1'
PREDECESSOR_SHA = 'b612aa37ac16df3282e41d93a846d354df91a4d477f1673abd002ccc95249f9c'
SUCCESSOR_SHA = '5e3aace73e90832da40d76971601db90c514cad4592c6776d752fd0f623e9a10'
NEWS_SOURCE_SHA = '52c7fb864d28ba1b24ae44d08cda1aec1852179c9a825d32456a3084aa52fc8d'
_FIELDS = {'policy', 'scope_key', 'predecessor_attempt_id', 'predecessor_epoch',
    'handoff_evidence_hash', 'accepted_manifest_hash', 'source_head',
    'adoption_receipt_digest', 'source_envelope', 'predecessor_row', 'successor_row',
    'before_references', 'after_references', 'observed_at', 'receipt_digest',
    'studio_before', 'studio_after', 'transition_authority_sha256', 'maintenance_evidence_sha256'}
_ROW_FIELDS = {'owner_id', 'strategy_id', 'schema_version', 'updated_at', 'definition_json'}


def require(condition):
    if not condition:
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID')


def _time(value):
    require(isinstance(value, str))
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None)
    return parsed.astimezone(timezone.utc)


def _dependencies(references):
    return digest(dict(version='recovery-checkpoint-dependencies-v3',
                       kind='news_trading_state', references=references))


def receipt_digest(document):
    return digest(dict(domain='flowsignal-news-strategy-compatibility-v1',
        receipt={k:v for k,v in document.items() if k != 'receipt_digest'}))


def validate_document(document):
    """Validate immutable proof content; does not assert DB/file authenticity."""
    try:
        require(type(document) is dict and set(document) == _FIELDS)
        d = document
        require(d['policy'] == POLICY and d['receipt_digest'] == receipt_digest(d))
        for field in ('scope_key', 'handoff_evidence_hash', 'accepted_manifest_hash',
                      'adoption_receipt_digest', 'receipt_digest',
                      'transition_authority_sha256', 'maintenance_evidence_sha256'):
            require(files._valid_hash(d[field]))
        require(type(d['predecessor_epoch']) is int and d['predecessor_epoch'] > 0)
        require(isinstance(d['predecessor_attempt_id'], str) and bool(d['predecessor_attempt_id']))
        body = checked_adopted_envelope(AdoptedEnvelope('news_trading_state', files._encoded(d['source_envelope'])))
        binding = body['adoption']['binding']
        require(binding['scope_key'] == d['scope_key'])
        require(binding['source_hashes']['news_trading_state'] == NEWS_SOURCE_SHA)
        head = d['source_head']
        require(type(head) is dict and set(head) == {'scope_key','kind','manifest_hash',
            'file_hash','generation','identity','admission_hash'})
        require(head['scope_key'] == d['scope_key'] and head['kind'] == 'news_trading_state'
            and files._valid_hash(head['manifest_hash']) and head['generation'] == 1
            and type(head['generation']) is int and head['identity'] == body['identity']
            and head['admission_hash'] == body['admission_hash']
            and head['file_hash'] == files._digest(files._encoded(body)))
        require(body['identity']['epoch'] <= d['predecessor_epoch'])
        payload = body['payload']
        news(payload)
        require(bool(payload['opportunities']))
        observed = _time(d['observed_at'])
        for record in payload['opportunities'].values():
            require(observed > _time(record['release_time']) + timedelta(minutes=30))
        old, new = d['predecessor_row'], d['successor_row']
        require(type(old) is dict and type(new) is dict
            and set(old) == _ROW_FIELDS and set(new) == _ROW_FIELDS)
        require(old['owner_id'] == new['owner_id'] == binding['owner_id']
            and old['strategy_id'] != new['strategy_id']
            and all(isinstance(row['strategy_id'],str) and row['strategy_id'] for row in (old,new))
            and type(old['schema_version']) is int and old['schema_version'] == new['schema_version'] == 1)
        _time(old['updated_at']); _time(new['updated_at'])
        studio_old, studio_new = d['studio_before'], d['studio_after']
        studio_fields = {'owner_id','enabled','enabled_strategy_id','enabled_at','updated_at'}
        require(type(studio_old) is dict and type(studio_new) is dict
            and set(studio_old) == set(studio_new) == studio_fields)
        require(studio_old['enabled'] is True and studio_new['enabled'] is True
            and studio_old['owner_id'] == studio_new['owner_id'] == old['owner_id']
            and studio_old['enabled_at'] == studio_new['enabled_at']
            and studio_old['enabled_strategy_id'] == old['strategy_id']
            and studio_new['enabled_strategy_id'] == new['strategy_id']
            and studio_new['updated_at'] == d['observed_at'])
        require(_time(studio_old['enabled_at']) <= _time(studio_old['updated_at']) <= observed)
        require(fingerprint(plan(old['definition_json'], source_sha256=PREDECESSOR_SHA,
            successor_sha256=SUCCESSOR_SHA)) == fingerprint(new['definition_json']))
        before, after = d['before_references'], d['after_references']
        require(type(before) is dict and type(after) is dict)
        require(before['runtime_owners'] == after['runtime_owners'] == [old['owner_id']])
        require(before['strategy_selections'] == [dict(owner_id=old['owner_id'],strategy_id=old['strategy_id'])]
            and after['strategy_selections'] == [dict(owner_id=new['owner_id'],strategy_id=new['strategy_id'])]
            and before['saved_strategies'] == [old] and after['saved_strategies'] == [new])
        require({k:v for k,v in before.items() if k not in {'strategy_selections','saved_strategies'}} ==
                {k:v for k,v in after.items() if k not in {'strategy_selections','saved_strategies'}})
        require(before['selection']['scope'] == body['identity']['account_scope'])
        require(_dependencies(before) == body['identity']['dependencies_hash']
                and _dependencies(before) != _dependencies(after))
        return copy.deepcopy(d)
    except RecoveryError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def _current_pair(d, database):
    old,new = d['predecessor_row'],d['successor_row']
    # Account/environment/revision are authority, not shared-owner telemetry.
    require(database['selection'] == d['after_references']['selection'])
    require(database['studio_live_state'] == [d['studio_after']])
    require(database['runtime_owners'] == [new['owner_id']])
    selected = [r for r in database['strategy_selections'] if r.get('owner_id') == new['owner_id']]
    require(len(selected) == 1 and selected[0].get('strategy_id') == new['strategy_id'])
    for expected in (old,new):
        rows = [r for r in database['saved_strategies'] if r.get('strategy_id') == expected['strategy_id']
                and r.get('owner_id') == expected['owner_id']]
        require(len(rows) == 1 and digest({k:rows[0].get(k) for k in _ROW_FIELDS}) == digest(expected))


def documents_from_rows(rows, expected_scope):
    """Reject split row/document identities and duplicate durable receipts."""
    try:
        require(isinstance(rows,list) and len(rows) <= 1)
        documents=[]
        for row in rows:
            d=validate_document(row['document'])
            expected=dict(scope_key=d['scope_key'],predecessor_attempt_id=d['predecessor_attempt_id'],
                predecessor_epoch=d['predecessor_epoch'],original_checkpoint_hash=d['source_head']['file_hash'],
                predecessor_strategy_id=d['predecessor_row']['strategy_id'],
                successor_strategy_id=d['successor_row']['strategy_id'],receipt_digest=d['receipt_digest'])
            require(d['scope_key']==expected_scope and all(row.get(k)==v for k,v in expected.items()))
            require(type(row['predecessor_epoch']) is int)
            documents.append(d)
        return documents
    except RecoveryError:
        raise
    except (TypeError,ValueError,KeyError,AttributeError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def validate_original(document, database, candidate, expected_scope, *, observed_at):
    """Only the exact original adopted candidate can use historical dependencies."""
    try:
        d = validate_document(document)
        require(d['scope_key'] == expected_scope and candidate.kind == 'news_trading_state'
            and candidate_has_receipt(candidate)
            and candidate.raw == files._encoded(d['source_envelope']))
        _current_pair(d,database)
        require(digest(checkpoint_references(candidate.kind,database,d['source_envelope']['payload']))
                == digest(d['after_references']))
        require(type(observed_at) in (int,float))
        now = datetime.fromtimestamp(observed_at, timezone.utc)
        require(now >= _time(d['observed_at']))
        return d['receipt_digest']
    except RecoveryError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, OSError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def validate_retained_events(document, database, payload):
    """No revived historical work, including in otherwise current descendants."""
    try:
        d = validate_document(document)
        _current_pair(d,database)
        require(type(payload) is dict and set(payload) == set(d['source_envelope']['payload']))
        records = payload['opportunities']
        require(type(records) is dict)
        for key, original in d['source_envelope']['payload']['opportunities'].items():
            current = records[key]
            require(type(current) is dict and set(current) == set(original))
            immutable = set(original) - {'status','reason','consumed','updated_at'}
            require({k:current[k] for k in immutable} == {k:original[k] for k in immutable})
            if current['status'] == 'PENDING':
                require(current == original)
            else:
                require(current['consumed'] is True and (
                    current['status'] == 'EXPIRED' and current['reason'] == 'NEWS_OPPORTUNITY_EXPIRED'
                    or current['status'] == 'CANCELLED' and current['reason'] in {
                        'NEWS_MODE_CHANGED_TO_OFF','NEWS_MODE_CHANGED_TO_BLOCK_ONLY'}))
                require(_time(current['updated_at']) >= _time(d['observed_at']))
        return d['receipt_digest']
    except RecoveryError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None


def validate_lineage(database, projection, payload, expected_scope):
    """Bind an already authenticated ancestor walk to the durable receipt.

    The caller must use the existing generation-wide validation/producer proof,
    not discover a root by directory scanning or trust a supplied JSON lineage.
    Descendants still require ordinary current dependency equality.
    """
    try:
        lineage = projection['lineage']
        require(type(lineage) is list and bool(lineage))
        root = lineage[-1]
        receipts = database.get('news_strategy_compatibility', [])
        require(type(receipts) is list and len(receipts) <= 1)
        selected = {(r['owner_id'], r['strategy_id']) for r in database['strategy_selections']}
        definitions = [r['definition_json'] for r in database['saved_strategies']
                       if (r['owner_id'], r['strategy_id']) in selected]
        historical = root.get('receipt') or {}
        pinned_root = historical.get('source_hashes', {}).get('news_trading_state') == NEWS_SOURCE_SHA
        if not receipts:
            if pinned_root:
                require(len(definitions) == 1 and fingerprint(definitions[0]) == PREDECESSOR_SHA)
            return None
        d = validate_document(receipts[0])
        require(d['scope_key'] == expected_scope and root.get('classification') == 'ADOPTED'
            and root['manifest_hash'] == d['source_head']['manifest_hash']
            and root['file_hash'] == d['source_head']['file_hash']
            and historical.get('receipt_digest') == d['adoption_receipt_digest']
            and historical.get('scope_key') == expected_scope)
        return validate_retained_events(d, database, payload)
    except RecoveryError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        raise RecoveryError('NEWS_STRATEGY_COMPATIBILITY_INVALID') from None
