"""Release identity must validate real package bytes, not self-claimed hashes."""
import hashlib
import json
import os
import platform
import socket
import subprocess

import pytest
from live_integrity import build_identity as adapter


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / 'package'
    root.mkdir()
    def put(name, data):
        root.chmod(0o755)
        for directory in root.rglob('*'):
            if directory.is_dir(): directory.chmod(0o755)
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(data)
        path.chmod(0o444)
        for directory in root.rglob('*'):
            if directory.is_dir(): directory.chmod(0o555)
        root.chmod(0o555)
    source = b'VALUE = 1\n'
    put('Backend/sample.py', source)
    lock = b'synthetic==1\n'
    metadata = b'{"synthetic":true}\n'
    put('Backend/requirements.production.lock', lock)
    put('Backend/requirements.production.metadata.json', metadata)
    manifest = dict(schema='flowsignal-certification-source/v1', packaging_source_sha='a'*40,
        files=[dict(path='Backend/sample.py',size=len(source),sha256=digest(source),mode='100644')])
    put('certification-source-manifest.json', encoded(manifest))
    record = dict(schema='flowsignal-immutable-build/v1',packaging_source_sha='a'*40,
        source_manifest_sha256=digest(encoded(manifest)),dependency_lock_sha256=digest(lock),
        dependency_metadata_sha256=digest(metadata),python_version=platform.python_version(),
        base_image_digest='sha256:'+'b'*64,certified_dependency_run_id='37414992580',
        certified_dependency_commit='c'*40)
    put('build-record.json', encoded(record))
    monkeypatch.setattr(adapter, 'PACKAGE_ROOT', root, raising=False)
    # Independent server-side pin, never derived by capture from the record.
    anchor = tmp_path / 'release-anchor'; anchor.mkdir()
    pins = anchor / 'independent-expectations.json'
    pins.write_bytes(encoded(record)); pins.chmod(0o444)
    anchor.chmod(0o555)
    monkeypatch.setattr(adapter, 'EXPECTATIONS_PATH', pins, raising=False)
    return root, record, manifest, put


def test_valid_release_without_git(package):
    root, record, _, _ = package
    result = adapter.capture()
    assert not (root / '.git').exists()
    assert result['backend_git_sha'] == 'a'*40
    assert result['clean_source'] is True
    assert 'source_tree' not in result
    assert result['build_identity'] == digest(encoded(record))


def test_timestamp_and_record_key_order_do_not_change_identity(package):
    _, record, _, put = package
    before = adapter.capture()
    put('build-record.json', json.dumps(dict(reversed(list(record.items()))) | {'build_timestamp':'later'}, indent=2).encode())
    assert adapter.capture() == before


@pytest.mark.parametrize('field', ['schema','packaging_source_sha','source_manifest_sha256',
    'dependency_lock_sha256','dependency_metadata_sha256','python_version','base_image_digest',
    'certified_dependency_run_id','certified_dependency_commit'])
@pytest.mark.parametrize('change', ['missing','mismatch'])
def test_required_independent_identity_pin(package, field, change):
    _, record, _, put = package
    if change == 'missing': del record[field]
    else:
        # Valid formats force the independent-pin comparison, not just parsing.
        replacements = dict(schema='flowsignal-immutable-build/v2',
            packaging_source_sha='d'*40, source_manifest_sha256='d'*64,
            dependency_lock_sha256='d'*64, dependency_metadata_sha256='d'*64,
            python_version='3.14.4',base_image_digest='sha256:'+'d'*64,
            certified_dependency_run_id='37414992581',certified_dependency_commit='d'*40)
        record[field] = replacements[field]
    put('build-record.json', encoded(record))
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()


@pytest.mark.parametrize('name', ['build-record.json','certification-source-manifest.json',
    'Backend/requirements.production.lock','Backend/requirements.production.metadata.json','Backend/sample.py'])
@pytest.mark.parametrize('fault', ['missing','changed','symlink','writable'])
def test_missing_or_tampered_evidence(package, name, fault):
    root, _, _, put = package
    path = root / name
    path.parent.chmod(0o755)
    if fault == 'missing': path.unlink()
    elif fault == 'changed': put(name, b'not valid evidence')
    elif fault == 'symlink':
        root.chmod(0o755)
        target = root / 'external'; target.write_bytes(path.read_bytes()); target.chmod(0o444)
        path.unlink(); path.symlink_to(target)
    else: path.chmod(0o644)
    path.parent.chmod(0o555)
    root.chmod(0o555)
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()


def test_self_consistent_forgery_cannot_replace_independent_pin(package):
    _, record, manifest, put = package
    put('Backend/sample.py', b'forged')
    manifest['files'][0].update(size=6,sha256=digest(b'forged'))
    put('certification-source-manifest.json', encoded(manifest))
    record['source_manifest_sha256'] = digest(encoded(manifest))
    put('build-record.json', encoded(record))
    with pytest.raises(ValueError, match='BUILD_IDENTITY_UNVERIFIED'): adapter.capture()


def test_actual_python_version_is_checked(package, monkeypatch):
    monkeypatch.setattr(platform, 'python_version', lambda: '0.0.0')
    with pytest.raises(ValueError, match='BUILD_IDENTITY_UNVERIFIED'): adapter.capture()


def test_environment_cannot_supply_release_pins(package, monkeypatch):
    _, record, _, _ = package
    adapter.EXPECTATIONS_PATH.parent.chmod(0o755)
    adapter.EXPECTATIONS_PATH.unlink()
    adapter.EXPECTATIONS_PATH.parent.chmod(0o555)
    for key, value in record.items(): monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'a'*40)
    with pytest.raises(ValueError, match='BUILD_IDENTITY_UNVERIFIED'): adapter.capture()


def test_dirty_git_no_executable_network_or_writes(package, monkeypatch):
    root, _, _, _ = package
    root.chmod(0o755)
    (root / '.git').mkdir()
    (root / '.git' / 'dirty').write_text('irrelevant')
    root.chmod(0o555)
    before = {p.relative_to(root): (p.read_bytes(),p.stat().st_mode,p.stat().st_mtime_ns)
              for p in root.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs): raise AssertionError('external side effect')
    monkeypatch.setenv('PATH','')
    monkeypatch.setenv('RENDER_GIT_COMMIT','wrong')
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.setattr(socket,'socket',forbidden)
    assert adapter.capture()['clean_source']
    assert before == {p.relative_to(root): (p.read_bytes(),p.stat().st_mode,p.stat().st_mtime_ns)
                      for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('path', ['Backend/unlisted.py','Backend/unlisted.pyc','Backend/plugin.so','Backend/inject.pth'])
def test_unlisted_importable_code_blocked(package, path):
    _, _, _, put = package
    put(path, b'foreign executable input')
    with pytest.raises(ValueError, match='BUILD_IDENTITY_UNVERIFIED'): adapter.capture()


def test_runtime_data_outside_source_does_not_change_identity(package):
    root, _, _, _ = package
    before = adapter.capture()
    root.chmod(0o755)
    (root / 'runtime').mkdir()
    (root / 'runtime' / 'log.txt').write_text('runtime data')
    root.chmod(0o555)
    assert adapter.capture() == before


@pytest.mark.parametrize('raw', [b'{', b'[]', b'null', b'{"schema":1,"schema":2}', b'{"value":NaN}'])
def test_malformed_json_rejected(package, raw):
    package[3]('build-record.json', raw)
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()


@pytest.mark.parametrize('fault', ['duplicate','escape','absolute','wrong_mode','wrong_size','wrong_schema','empty'])
def test_pinned_but_invalid_manifest_rejected(package, fault):
    _, record, manifest, put = package
    if fault == 'duplicate': manifest['files'] *= 2
    elif fault == 'escape': manifest['files'][0]['path'] = '../outside.py'
    elif fault == 'absolute': manifest['files'][0]['path'] = '/outside.py'
    elif fault == 'wrong_mode': manifest['files'][0]['mode'] = '100755'
    elif fault == 'wrong_size': manifest['files'][0]['size'] = True
    elif fault == 'wrong_schema': manifest['schema'] = 'unknown'
    else: manifest['files'] = []
    record['source_manifest_sha256'] = digest(encoded(manifest))
    put('certification-source-manifest.json', encoded(manifest))
    put('build-record.json', encoded(record))
    adapter.EXPECTATIONS_PATH.chmod(0o644)
    adapter.EXPECTATIONS_PATH.write_bytes(encoded(record))
    adapter.EXPECTATIONS_PATH.chmod(0o444)
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()


def test_hardlink_evidence_rejected(package):
    root = package[0]
    root.chmod(0o755)
    os.link(root / 'Backend/sample.py', root / 'alias')
    root.chmod(0o555)
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()


def test_real_recovery_bootstrap_stops_before_db_without_release_pins(package):
    from startup_recovery.bootstrap import start
    from types import SimpleNamespace
    from unittest.mock import Mock
    adapter.EXPECTATIONS_PATH.parent.chmod(0o755)
    adapter.EXPECTATIONS_PATH.unlink()
    adapter.EXPECTATIONS_PATH.parent.chmod(0o555)
    engine, factory = Mock(), Mock()
    app = SimpleNamespace(ENGINE_RUNTIME_STATE={})
    result = start(app, engine=engine, session_factory=Mock(), dependencies_factory=factory)
    assert not result.ready and result.reason == 'BUILD_IDENTITY_UNVERIFIED'
    engine.connect.assert_not_called()
    factory.assert_not_called()


def test_valid_record_reaches_worker_verifier_with_compatible_identity(package, monkeypatch):
    # Real worker entrypoint and identity adapter; DB/auth/evaluator boundary doubles.
    import runpy
    import sys
    import io
    from pathlib import Path
    from contextlib import contextmanager
    old_meta = list(sys.meta_path)
    try:
        worker = runpy.run_path(str(Path(adapter.__file__).with_name('worker.py')))
    finally:
        sys.meta_path[:] = old_meta
    from unittest.mock import Mock
    @contextmanager
    def scope(*args): yield object()
    observed = []
    def verify(*args):
        observed.append(True)
        return {'decision':'WOULD_BLOCK','REAL_ORDER_DISPATCH_AVAILABLE':False,'block_reasons':['FIXTURE_BLOCK']}
    namespace = worker['main'].__globals__
    monkeypatch.setitem(namespace, 'create_engine', lambda *a, **k: Mock())
    monkeypatch.setitem(namespace, 'read_only', scope)
    monkeypatch.setitem(namespace, 'single_flight', scope)
    monkeypatch.setitem(namespace, 'read_admin', lambda *a: 'fixture-owner')
    monkeypatch.setitem(namespace, 'verify', verify)
    monkeypatch.setenv('STRATEGY_LIVE_INTEGRITY_VERIFY_ENABLED','1')
    monkeypatch.setenv('DATABASE_URL','postgresql://unused-fixture')
    body = dict(deadline=1e20,token='synthetic',csrf='synthetic',strategy_id='fixture',symbol='EURUSD',runtime={})
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(encoded(body))))
    result = worker['main']()
    assert result['status'] == 200 and observed == [True]
    assert result['body']['build']['backend_git_sha'] == 'a'*40
    assert result['body']['build']['clean_source'] is True
    assert result['body']['REAL_ORDER_DISPATCH_AVAILABLE'] is False


@pytest.mark.parametrize('which',['anchor','package','source_parent'])
def test_writable_evidence_directory_rejected(package, which):
    root = package[0]
    path = adapter.EXPECTATIONS_PATH.parent if which == 'anchor' else (root / 'Backend' if which == 'source_parent' else root)
    path.chmod(0o755)
    with pytest.raises(ValueError, match='^BUILD_IDENTITY_UNVERIFIED$'): adapter.capture()
