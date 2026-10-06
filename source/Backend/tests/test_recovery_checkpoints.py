"""Checkpoint candidates are untrusted until an independent DB manifest admits them."""
import importlib
import json
from dataclasses import replace

import pytest


def api():
    try:
        return importlib.import_module('startup_recovery.checkpoints')
    except ModuleNotFoundError:
        pytest.fail('Versioned recovery checkpoint contract missing')


def identity():
    return dict(account_scope='CTRADER:DEMO:123', owner_id='owner', symbol='EURUSD',
                strategy_id='saved', config_hash='a' * 64, position_id='42',
                epoch=2, boot_id='boot', build_id='b' * 40,
                dependencies_hash='c' * 64, generation=1)


def produced(c, payload=None, parent=None):
    return c.produced_envelope('live_backup', payload or {'protected_sl': '1.1025'},
        identity(), parent_hash=parent, admission_hash='d' * 64,
        produced_event_id='new-broker-observation:1', timestamp='2026-10-01T12:00:00Z')


def test_absence_remains_absence_and_does_not_create_directory(tmp_path):
    c = api()
    path = tmp_path / 'missing' / 'state.json'
    assert isinstance(c.read_candidate(path, 'live_backup'), c.Absent)
    assert not path.parent.exists()


def test_reading_legacy_never_rewrites_or_mints_trusted_envelope(tmp_path):
    c = api()
    path = tmp_path / 'state.json'
    original = b'{"protected_sl":1.1025}'
    path.write_bytes(original)
    candidate = c.read_candidate(path, 'live_backup')
    assert candidate.legacy is True
    rejected = c.validate_checkpoint(candidate, identity(), accepted_hash='e' * 64)
    assert rejected.reason == 'LEGACY_RECONCILIATION_REQUIRED'
    assert path.read_bytes() == original
    with pytest.raises(c.RecoveryError, match='CHECKPOINT_NEW_STATE_REQUIRED'):
        c.write_generation(tmp_path, {'live_backup': candidate}, expected_parent=None)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('data', [b'{', b'null', b'{"x":NaN}', b'{"x":Infinity}'])
def test_corrupt_candidate_is_rejected_without_default_write(tmp_path, data):
    c = api()
    path = tmp_path / 'bad.json'
    path.write_bytes(data)
    candidate = c.read_candidate(path, 'live_backup')
    assert c.validate_checkpoint(candidate, identity(), accepted_hash='e' * 64).reason == 'CHECKPOINT_CORRUPT'
    assert path.read_bytes() == data


@pytest.mark.parametrize('field,value', [('account_scope','CTRADER:LIVE:123'), ('symbol','XAUUSD'),
    ('strategy_id','other'), ('config_hash','f' * 64), ('position_id','43'), ('epoch',1),
    ('generation',0), ('dependencies_hash','e' * 64)])
def test_identity_mismatch_never_uses_latest_timestamp(tmp_path, field, value):
    c = api()
    envelope = produced(c)
    manifest = c.write_generation(tmp_path, {'live_backup': envelope}, expected_parent=None)
    candidate = c.read_committed(tmp_path, manifest.manifest_hash)['live_backup']
    expected = identity()
    expected[field] = value
    assert c.validate_checkpoint(candidate, expected,
        accepted_hash=manifest.file_hashes['live_backup']).reason == 'CHECKPOINT_IDENTITY_MISMATCH'


def test_committed_hash_required_and_valid_payload_is_exact(tmp_path):
    c = api()
    envelope = produced(c, {'risk': '98.9651', 'protected_sl': '1.1025'})
    manifest = c.write_generation(tmp_path, {'live_backup': envelope}, expected_parent=None)
    assert isinstance(c.read_committed(tmp_path, None), c.Absent)
    candidate = c.read_committed(tmp_path, manifest.manifest_hash)['live_backup']
    accepted = c.validate_checkpoint(candidate, identity(), accepted_hash=manifest.file_hashes['live_backup'])
    assert accepted.payload == {'risk': '98.9651', 'protected_sl': '1.1025'}
    assert c.validate_checkpoint(candidate, identity(), accepted_hash='f' * 64).reason == 'CHECKPOINT_HASH_MISMATCH'


@pytest.mark.parametrize('boundary', ['file_fsync', 'rename', 'directory_fsync'])
def test_interrupted_publication_never_replaces_accepted_generation(tmp_path, monkeypatch, boundary):
    c = api()
    old = c.write_generation(tmp_path, {'live_backup': produced(c)}, expected_parent=None)
    original_fsync, original_replace = c.os.fsync, c.os.replace
    count = 0
    def fsync(fd):
        nonlocal count
        count += 1
        if (boundary == 'file_fsync' and count == 1) or (boundary == 'directory_fsync' and count == 3):
            raise OSError('injected crash')
        return original_fsync(fd)
    def rename(*a, **k):
        if boundary == 'rename':
            raise OSError('injected crash')
        return original_replace(*a, **k)
    monkeypatch.setattr(c.os, 'fsync', fsync)
    monkeypatch.setattr(c.os, 'replace', rename)
    with pytest.raises(OSError, match='injected crash'):
        c.write_generation(tmp_path, {'live_backup': produced(c, parent=old.manifest_hash)}, expected_parent=old.manifest_hash)
    # The DB pointer is still old; no scan/newest-file fallback is permitted.
    assert c.read_committed(tmp_path, old.manifest_hash)['live_backup'].legacy is False


def test_symlink_and_path_escape_are_rejected(tmp_path):
    c = api()
    source = tmp_path / 'source.json'
    source.write_text('{}')
    link = tmp_path / 'link.json'
    link.symlink_to(source)
    assert c.read_candidate(link, 'live_backup').error == 'CHECKPOINT_UNSAFE_PATH'
    with pytest.raises(c.RecoveryError, match='CHECKPOINT_KIND_INVALID'):
        c.produced_envelope('../source', {}, identity(), parent_hash=None,
            admission_hash='d' * 64, produced_event_id='event', timestamp='2026-10-01T12:00:00Z')


@pytest.mark.parametrize('field,value', [('payload_hash', '0' * 64),
    ('checkpoint_schema', 9), ('produced_event_id', ''), ('admission_hash', None)])
def test_constructed_envelope_cannot_bypass_publication_validation(tmp_path, field, value):
    c = api()
    body = json.loads(produced(c).raw)
    body[field] = value
    forged = c.ProducedEnvelope('live_backup', json.dumps(body).encode())
    with pytest.raises(c.RecoveryError):
        c.write_generation(tmp_path, {'live_backup': forged}, expected_parent=None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['final_signal_hold', 'fifteen_m_swing_watch', 'news_trading_state'])
def test_new_consumption_or_stream_evidence_invalidates_checkpoint(tmp_path, kind):
    c = api()
    envelope = c.produced_envelope(kind, {'event_id': 'event-1', 'consumed': False}, identity(),
        parent_hash=None, admission_hash='d' * 64, produced_event_id='observation-1',
        timestamp='2026-10-01T12:00:00Z')
    manifest = c.write_generation(tmp_path, {kind: envelope}, expected_parent=None)
    current = identity()
    current['dependencies_hash'] = 'e' * 64  # DB consumption/close watermark or stream changed.
    assert c.validate_checkpoint(c.read_committed(tmp_path, manifest.manifest_hash)[kind], current,
        accepted_hash=manifest.file_hashes[kind]).reason == 'CHECKPOINT_IDENTITY_MISMATCH'
