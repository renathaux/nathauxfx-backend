"""A pinned digest never authorizes an invalid transport policy."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import importlib
import json

import pytest


def api():
    return importlib.import_module('startup_recovery.cutover_manifest')


def body():
    return dict(schema=1, policy_version='native-cutover-v1', source_root='/private/tmp/native',
        state_root='/private/tmp/destination', scope=dict(schema=1, binding_overrides={},
        family_locations={str(n): [] for n in range(13, 28)}, operator_outputs=[]),
        db_references=[], db_reference_sha256=hashlib.sha256(b'[]').hexdigest(),
        records=[dict(family=1, kind='live_backup', source_path='/private/tmp/native/live_backup.json',
        presence='ABSENT', size=None, sha256=None, source_mode=None, destination_mode=None,
        classification='COPY_CANDIDATE', target_path='live_backup.json',
        trust_status='LEGACY_UNTRUSTED', db_reference_keys=[], reason=None)], blockers=['INCOMPLETE_COVERAGE'])


def test_manifest_canonical_bytes_and_independent_pin():
    m = api()
    value = body()
    doc = m.canonical_manifest(value)
    assert doc.raw == json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    assert doc.digest == hashlib.sha256(doc.raw).hexdigest()
    assert m.decode_manifest(doc.raw, doc.digest) == doc
    with pytest.raises(m.CutoverError):
        m.decode_manifest(doc.raw, '0' * 64)
    with pytest.raises(Exception):
        doc.raw = b'changed'


@pytest.mark.parametrize('pin', ['', 'A'*64, '0'*63, None, 'sha256:'+'0'*64])
def test_pin_is_mandatory_exact_lowercase_hex(pin):
    m = api()
    doc = m.canonical_manifest(body())
    with pytest.raises(m.CutoverError):
        m.decode_manifest(doc.raw, pin)


@pytest.mark.parametrize('field,value', [
    ('schema', True), ('schema', 2), ('policy_version', 'future'), ('extra', 'secret'),
    ('source_root', '/private/tmp/native/../native'), ('state_root', '/private/tmp/native/sub'),
    ('source_root', 'relative'), ('db_reference_sha256', '0'*64)])
def test_rejects_invalid_schema_paths_and_reference_digest(field, value):
    m = api()
    raw = body()
    raw[field] = value
    with pytest.raises(m.CutoverError):
        m.canonical_manifest(raw)


@pytest.mark.parametrize('field,value', [('size', 1), ('sha256', '0'*64),
    ('source_mode', 420), ('destination_mode', 384), ('extra', None),
    ('target_path', '../escape'), ('target_path', 'app_settings.json'),
    ('trust_status', 'TRUSTED'), ('source_path', '/private/tmp/native/.env')])
def test_absence_and_canonical_target_cannot_be_forged(field, value):
    m = api()
    raw = body()
    raw['records'][0][field] = value
    with pytest.raises(m.CutoverError):
        m.canonical_manifest(raw)


@pytest.mark.parametrize('family,kind,name', [(17, 'credentials', '.env'),
    (19, 'candle_cache', 'candle_cache'), (15, 'users', 'users.json'),
    (27, 'operator_outputs', 'mystery.json')])
def test_well_hashed_document_cannot_reclassify_excluded_or_unknown(family, kind, name):
    m = api()
    value = body()
    value['records'][0].update(family=family, kind=kind,
        source_path='/private/tmp/native/'+name, target_path=name)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    with pytest.raises(m.CutoverError):
        m.decode_manifest(raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize('raw', [b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'{"schema":Infinity}'])
def test_duplicate_keys_and_nonfinite_input_rejected(raw):
    m = api()
    with pytest.raises(m.CutoverError):
        m.decode_manifest(raw, hashlib.sha256(raw).hexdigest())


def test_present_candidate_hash_and_modes_required():
    m = api()
    value = body()
    record = value['records'][0]
    record.update(presence='PRESENT', size=2, sha256=hashlib.sha256(b'{}').hexdigest(),
        source_mode=0o644, destination_mode=0o600)
    assert m.decode_manifest(m.canonical_manifest(value).raw, m.canonical_manifest(value).digest)
    for field, invalid in [('size', -1), ('size', True), ('sha256', None),
                           ('source_mode', -1), ('destination_mode', 0o777)]:
        changed = deepcopy(value)
        changed['records'][0][field] = invalid
        with pytest.raises(m.CutoverError):
            m.canonical_manifest(changed)


def test_exact_decimal_reference_identity_changes_digest_without_float():
    m = api()
    from startup_recovery.checkpoints import _encoded, _digest
    value = body()
    ref = dict(scope_key='scope', kind='live_backup', manifest_hash='1'*64, file_hash='2'*64,
        generation=1, identity={'risk': Decimal('1.00000000000000000001')}, admission_hash='3'*64,
        broker='ctrader', environment='demo', account_id='synthetic')
    value['db_references'] = [ref]
    value['db_reference_sha256'] = _digest(_encoded([ref]))
    first = m.canonical_manifest(value)
    assert b'1.00000000000000000001' in first.raw
    ref['identity']['risk'] = Decimal('1.00000000000000000002')
    value['db_reference_sha256'] = _digest(_encoded([ref]))
    assert first.digest != m.canonical_manifest(value).digest


def test_policy_covers_all_27_families_without_trust_labels():
    m = api()
    assert set(m.POLICY) == set(range(1, 28))
    assert all(p.trust_status not in ('TRUSTED', 'ADMITTED') for p in m.POLICY.values())
