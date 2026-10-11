"""Retry classification never treats an orphan as committed authority."""
import importlib
from copy import deepcopy

import pytest
from startup_recovery.checkpoints import _parse
from startup_recovery.types import RecoveryError
from test_legacy_adoption_envelopes import bundle


def classify(receipt, heads, generations, binding):
    try:
        api = importlib.import_module('startup_recovery.legacy_adoption')
    except ModuleNotFoundError:
        pytest.fail('Adoption-only retry validator is missing')
    return api.classify_retry(receipt, heads, generations, binding)


def evidence():
    _, binding, envelopes, receipt = bundle()
    heads = [dict(scope_key=receipt['scope_key'], kind=k,
                  manifest_hash=receipt['generation_manifest_hash'], file_hash=receipt['envelope_hashes'][k],
                  generation=1, identity=_parse(e.raw)['identity'], admission_hash=binding.digest)
             for k, e in envelopes.items()]
    return binding, envelopes, receipt, heads


def test_committed_complete_requires_all_five_heads_and_receipt():
    b, e, r, h = evidence()
    assert classify(r, h, {r['generation_manifest_hash']: e}, b) == 'COMMITTED_COMPLETE'


def test_exact_uncommitted_orphan_is_not_authority():
    b, e, r, _ = evidence()
    assert classify(None, [], {r['generation_manifest_hash']: e}, b) == 'UNCOMMITTED_ORPHAN'


@pytest.mark.parametrize('fault', ['partial_heads', 'missing_receipt', 'receipt_only', 'multiple',
    'wrong_epoch', 'wrong_boot', 'wrong_build', 'wrong_scope', 'wrong_source', 'wrong_dependency',
    'wrong_pin', 'missing_envelope', 'extra_envelope', 'head_hash', 'head_owner', 'generation_name'])
def test_every_partial_or_conflicting_state_blocks(fault):
    b, e, r, h = evidence()
    generations = {r['generation_manifest_hash']: e}
    if fault == 'partial_heads': h.pop()
    elif fault == 'missing_receipt': r = None
    elif fault == 'receipt_only': h = []
    elif fault == 'multiple': generations['f'*64] = e
    elif fault == 'missing_envelope': e.pop('live_backup')
    elif fault == 'extra_envelope': e['visits'] = e['live_backup']
    elif fault == 'head_hash': h[0]['file_hash'] = 'f'*64
    elif fault == 'head_owner': h[0]['identity']['owner_id'] = 'other'
    elif fault == 'generation_name': generations = {'f'*64: e}
    else:
        v = b.value
        if fault == 'wrong_epoch': v['epoch'] += 1
        elif fault == 'wrong_boot': v['boot_id'] = 'other'
        elif fault == 'wrong_build': v['build_id'] = 'other'
        elif fault == 'wrong_scope': v['scope_key'] = 'f'*64
        elif fault == 'wrong_pin': v['cutover_manifest_hash'] = 'f'*64
        elif fault == 'wrong_source': v['source_hashes']['live_backup'] = 'f'*64
        elif fault == 'wrong_dependency': v['dependency_hashes']['live_backup'] = 'f'*64
        b = type(b).create(v)
    assert classify(r, h, generations, b) == 'PARTIAL_OR_CONFLICTING'
