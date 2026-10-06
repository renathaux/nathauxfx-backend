"""Fail-closed new-entry identity. Never used by position management.

Saved JSON is hashed from the database's textual representation with exact
Decimal parsing. Runtime floats in a frozen plan use their existing JSON decimal
representation; no new trading arithmetic or broker sizing policy is introduced.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import timezone
from decimal import Decimal, localcontext

from sqlalchemy import Text, cast, text

from db import SessionLocal
from models import StrategySetupLifecycle, TradeSubmissionAttempt
from services.strategy_studio_models import SavedStrategy
from services.strategy_studio_schema import normalize_definition


from live_integrity.binding import BindingError, fingerprint, _emit, _parse, freeze_plan, require_binding


IDENTITY_FIELDS = ('owner_id', 'strategy_id', 'updated_at', 'schema_version', 'config_hash', 'canonical_version')


def reject_studio_source_downgrade(payload):
    """A source label must not erase known Studio provenance on a new entry."""
    source = str(payload.get('execution_source') or '').upper()
    provenance_keys = ('studio_owner_id', 'studio_strategy_id', 'studio_setup_id',
                       'studio_binding', 'studio_account_scope')
    studio_fields = any(payload.get(key) not in (None, '', {}) for key in provenance_keys)
    studio_setup = str(payload.get('signal_setup_id') or '').startswith('sts1_')
    if source == 'STRATEGY_STUDIO' or studio_fields or studio_setup:
        require_binding(payload.get('studio_binding'))
        if source != 'STRATEGY_STUDIO':
            raise BindingError('STRATEGY_EXECUTION_SOURCE_CHANGED')


def saved_identity(session, row):
    if row is None: raise BindingError('STRATEGY_SAVED_ROW_MISSING')
    if not row.updated_at or not row.owner_id or not row.strategy_id or not row.schema_version:
        raise BindingError('STRATEGY_IDENTITY_MISSING')
    raw = session.query(cast(SavedStrategy.definition_json, Text)).filter(
        SavedStrategy.owner_id == row.owner_id, SavedStrategy.strategy_id == row.strategy_id,
    ).scalar()
    if raw is None: raise BindingError('STRATEGY_SAVED_ROW_MISSING')
    # Schema normalization is the existing runtime contract. Old incomplete DB
    # rows must be explicitly resaved, never silently assigned a new identity.
    if fingerprint(normalize_definition(row.definition_json)) != hashlib.sha256(_emit(_parse(raw)).encode()).hexdigest():
        raise BindingError('STRATEGY_DEFINITION_NOT_CANONICAL')
    stamp = row.updated_at
    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)  # SQLite stores UTC without tz
    return dict(owner_id=row.owner_id, strategy_id=row.strategy_id,
                updated_at=stamp.astimezone(timezone.utc).isoformat(timespec='microseconds'),
                schema_version=row.schema_version, canonical_version=1,
                config_hash=hashlib.sha256(_emit(_parse(raw)).encode()).hexdigest())


def make_entry_binding(session, row, plan):
    if int(row.definition_json['risk'].get('max_concurrent_positions', 1)) > 1:
        raise BindingError('STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED')
    return dict(strategy_identity=saved_identity(session,row), frozen_plan=copy.deepcopy(plan),
                frozen_plan_hash=fingerprint(plan))


def lock_saved(session, owner, strategy_id):
    from startup_recovery.unit_of_work import ordered
    query=session.query(SavedStrategy).filter(SavedStrategy.owner_id==owner, SavedStrategy.strategy_id==strategy_id)
    if ordered(session,5,strategy_id): query=query.with_for_update()
    row=query.populate_existing().one_or_none()
    if row is None: raise BindingError('STRATEGY_SAVED_ROW_MISSING')
    return row


def validate_bound_payload(session, lifecycle, payload, owner, *, saved=None):
    supplied=payload.get('studio_binding')
    identity=require_binding(supplied)
    if lifecycle is None: raise BindingError('STRATEGY_SETUP_MISSING')
    persisted=require_binding(lifecycle.entry_binding)
    if identity['owner_id'] != owner or lifecycle.owner_id != owner or payload.get('studio_owner_id') != owner:
        raise BindingError('STRATEGY_OWNER_MISMATCH')
    if identity['strategy_id'] != lifecycle.strategy_id or payload.get('studio_strategy_id') != lifecycle.strategy_id:
        raise BindingError('STRATEGY_VERSION_CHANGED')
    if fingerprint(supplied) != fingerprint(lifecycle.entry_binding): raise BindingError('STRATEGY_PLAN_CHANGED')
    row=saved or lock_saved(session,owner,lifecycle.strategy_id)
    from live_integrity.validation import validate_identity
    validate_identity(supplied,saved_identity(session,row),row.definition_json,owner)
    if fingerprint(lifecycle.definition_snapshot) != persisted['config_hash']: raise BindingError('STRATEGY_VERSION_CHANGED')
    if int(row.definition_json['risk'].get('max_concurrent_positions',1)) > 1:
        raise BindingError('STRATEGY_STUDIO_LIVE_MULTI_POSITION_NOT_SUPPORTED')
    plan=supplied['frozen_plan']
    pairs={'symbol':'symbol','action':'side','entry':'entry','sl':'sl','tp1':'tp1','tp2':'tp2',
           'studio_account_scope':'account_scope','studio_risk_method':'risk_method','studio_risk_value':'risk_value',
           'requested_risk_percent':'requested_risk_percent','risk_percent':'requested_risk_percent',
           'risk_amount':'intended_risk_dollars','account_balance_used':'account_balance',
           'tp1_definition':'tp1_definition','fundamental_policy':'fundamental_policy'}
    for key, frozen in pairs.items():
        if key not in payload or frozen not in plan or fingerprint(payload[key]) != fingerprint(plan[frozen]):
            raise BindingError('STRATEGY_PLAN_CHANGED')
    risk=payload.get('risk') or {}
    if fingerprint(risk.get('broker_metadata')) != fingerprint(plan['broker_metadata']):
        raise BindingError('BROKER_METADATA_PLAN_CHANGED')
    from services.broker_execution_metadata import validate_metadata, MetadataError
    try:
        _, environment, account_id = plan['account_scope'].split(':', 2)
        validate_metadata(plan['broker_metadata'], account_id=account_id, environment=environment.lower(), symbol=plan['symbol'])
    except (ValueError, MetadataError) as exc:
        raise BindingError(str(exc)) from exc
    for key, frozen in [('account_balance','account_balance'),('risk_percent','requested_risk_percent'),('risk_amount','intended_risk_dollars')]:
        if key not in risk or fingerprint(risk[key]) != fingerprint(plan[frozen]): raise BindingError('STRATEGY_PLAN_CHANGED')
    if plan['position_constraints'] != row.definition_json['risk']:
        raise BindingError('STRATEGY_PLAN_CHANGED')
    if payload.get('signal') != plan['side'] or payload.get('studio_tp1_enabled') != bool(plan['tp1_definition']['enabled']):
        raise BindingError('STRATEGY_PLAN_CHANGED')
    if payload.get('studio_setup_id') != lifecycle.setup_id or plan['symbol'] != lifecycle.symbol or plan['side'] != lifecycle.direction or plan['account_scope'] != lifecycle.account_scope:
        raise BindingError('STRATEGY_OWNER_MISMATCH')
    return persisted


def dispatch_fingerprint(payload):
    # Bind every dispatch dependency, including the executable sizing result.
    keys=('symbol','action','signal','entry','sl','tp1','tp2','volume','volume_units','risk','mode',
          'studio_owner_id','studio_strategy_id','studio_setup_id','studio_account_scope','studio_binding',
          'studio_risk_method','studio_risk_value','requested_risk_percent','risk_percent','risk_amount',
          'account_balance_used','studio_tp1_enabled','tp1_definition','fundamental_policy')
    if any(k not in payload for k in keys): raise BindingError('STRATEGY_IDENTITY_MISSING')
    return fingerprint({k:payload[k] for k in keys})


def dispatch_bound_order(payload, key, runtime_owner, submit, *, session_factory=None):
    """Commit unresolved intent before transport; never hold a DB lock over I/O."""
    from services.submission_intent import prepare_entry, finish_entry
    from startup_recovery.runtime import caller_token, operation_context
    from startup_recovery.types import RecoveryError
    factory = session_factory or SessionLocal
    permit = None
    try:
        identity = require_binding(payload.get('studio_binding'))
        if not runtime_owner or identity['owner_id'] != runtime_owner:
            raise BindingError('STRATEGY_OWNER_MISMATCH')
        token = caller_token()
        permit=prepare_entry(factory,token,key,payload,runtime_owner)
        with operation_context(permit):
            result=submit()
        # Transaction B is separate from the network call. A missing observation
        # or failed commit leaves Transaction A intact for reconciliation.
        category=str((result or {}).get('broker_result') or '').upper()
        if category in {'ACCEPTED','ACCEPTED_PROTECTION_FAILED'}:
            from services.accepted_execution import acceptance_observation
            evidence=acceptance_observation(result,permit.material['snapshot'])
            finish_entry(factory,token,key,'ACCEPTED',evidence)
        elif category=='DEFINITELY_REJECTED':
            finish_entry(factory,token,key,category,result)
        else:
            finish_entry(factory,token,key,'AMBIGUOUS',None)
        return result
    except (BindingError, RecoveryError) as exc:
        if permit is not None:
            return {'ok': False, 'reason': str(exc), 'broker_result': 'AMBIGUOUS', 'order_sent': True}
        return {'ok': False, 'reason': str(exc), 'broker_result': 'FAILED_BEFORE_SEND', 'order_sent': False}
    finally:
        if permit is not None: permit.active=False


def _dispatch_bound_order(payload, key, runtime_owner, submit, *, session_factory=None):
    """Compatibility name; all entry sends use the durable transaction split."""
    return dispatch_bound_order(payload,key,runtime_owner,submit,session_factory=session_factory)
