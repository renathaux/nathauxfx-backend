"""Task 2: actual filesystem invariance, no-follow transport and kernel locks."""
import hashlib
import importlib
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


def api():
    return importlib.import_module('startup_recovery.cutover_files')


def snapshot(root):
    """Independent observer: no reads that change the evidence being measured."""
    result = {}
    def visit(p):
        s = p.lstat()
        raw = None
        if stat.S_ISDIR(s.st_mode):
            fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
            try:
                names = tuple(sorted(os.listdir(fd)))
            finally:
                os.close(fd)
            raw = names
            for name in names:
                visit(p / name)
        elif stat.S_ISREG(s.st_mode):
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
            try:
                raw = b''
                while part := os.read(fd, 65536):
                    raw += part
            finally:
                os.close(fd)
        result[str(p.relative_to(root))] = (raw, *(getattr(s, k) for k in
            ('st_size', 'st_mode', 'st_uid', 'st_gid', 'st_dev', 'st_ino', 'st_nlink',
             'st_mtime_ns', 'st_ctime_ns', 'st_atime_ns')))
    visit(root)
    return result


@pytest.fixture
def source(tmp_path):
    assert sys.platform == 'linux' and hasattr(os, 'O_NOATIME')
    assert os.geteuid() != 0
    root = tmp_path / 'source'
    root.mkdir(mode=0o700)
    (root / 'live_backup.json').write_bytes(b'{"legacy":true}\n')
    (root / 'live_backup.json').chmod(0o640)
    (root / 'nested').mkdir(mode=0o700)
    (root / 'nested' / 'other').write_bytes(b'untouched')
    for p in (root / 'live_backup.json', root / 'nested' / 'other', root / 'nested', root):
        os.utime(p, ns=(946684800000000000, 946684800000000000))
    return root


def test_read_and_enumeration_preserve_all_file_and_directory_metadata(source):
    m = api()
    before = snapshot(source)
    with m.ReadTree(source) as tree:
        assert tree.entries('.') == ('live_backup.json', 'nested')
        image = tree.read('live_backup.json')
        assert image.raw == b'{"legacy":true}\n'
        assert image.stat.mode == 0o640
        assert tree.read('nested/other').raw == b'untouched'
    assert snapshot(source) == before


def test_read_does_not_provision_absent_root(tmp_path):
    m = api()
    with pytest.raises(FileNotFoundError):
        m.ReadTree(tmp_path / 'absent')
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('relative', ['../escape', '/etc/passwd', 'nested/../live_backup.json', 'nested//other', './live_backup.json', ''])
def test_traversal_rejected_without_source_mutation(source, relative):
    m = api()
    before = snapshot(source)
    with m.ReadTree(source) as tree, pytest.raises(m.CutoverError):
        tree.read(relative)
    assert snapshot(source) == before


@pytest.mark.parametrize('hazard', ['leaf_symlink', 'ancestor_symlink', 'fifo', 'hardlink', 'directory', 'world_writable'])
def test_unsafe_source_is_rejected_without_read_or_mutation(source, hazard):
    m = api()
    p = source / 'hazard'
    if hazard == 'leaf_symlink':
        p.symlink_to(source / 'live_backup.json')
    elif hazard == 'ancestor_symlink':
        p.symlink_to(source / 'nested', target_is_directory=True)
    elif hazard == 'fifo':
        os.mkfifo(p)
    elif hazard == 'hardlink':
        os.link(source / 'live_backup.json', p)
    elif hazard == 'directory':
        p.mkdir()
    else:
        p.write_bytes(b'unsafe')
        p.chmod(0o666)
    before = snapshot(source)
    with m.ReadTree(source) as tree, pytest.raises(m.CutoverError):
        tree.read('hazard/other' if hazard == 'ancestor_symlink' else 'hazard')
    assert snapshot(source) == before


def test_symlink_in_absolute_root_rejected(source, tmp_path):
    m = api()
    (tmp_path / 'alias').symlink_to(source, target_is_directory=True)
    with pytest.raises(m.CutoverError):
        m.ReadTree(tmp_path / 'alias')


def test_permission_failure_for_noatime_is_not_silently_retried(source, monkeypatch):
    m = api()
    original = os.open
    def denied(name, flags, *a, **kw):
        if flags & os.O_NOATIME:
            raise PermissionError('synthetic denied')
        return original(name, flags, *a, **kw)
    before = snapshot(source)
    monkeypatch.setattr(os, 'open', denied)
    with pytest.raises(m.CutoverError):
        with m.ReadTree(source) as tree:
            tree.read('live_backup.json')
    monkeypatch.setattr(os, 'open', original)
    assert snapshot(source) == before


def test_copy_success_idempotency_and_failure_preserve_source(source, tmp_path):
    m = api()
    before = snapshot(source)
    with m.ReadTree(source) as tree, m.CopyTarget(tmp_path / 'target') as target:
        raw = tree.read('live_backup.json').raw
        with target.exclusive():
            assert target.install('live_backup.json', raw, 0o600) == 'CREATED'
            installed = snapshot(tmp_path / 'target')
            assert target.install('live_backup.json', raw, 0o600) == 'IDENTICAL'
            assert snapshot(tmp_path / 'target') == installed
            with pytest.raises(m.CutoverError, match='DESTINATION_CONFLICT'):
                target.install('live_backup.json', b'different', 0o600)
            assert snapshot(tmp_path / 'target') == installed
    assert snapshot(source) == before
    assert (tmp_path / 'target' / 'live_backup.json').stat().st_mode & 0o7777 == 0o600


@pytest.mark.parametrize('name', ['.env', 'cache.json', '../live_backup.json', '/escape',
                                 '.cutover/unreviewed', '.recovery-generations/bad/live_backup.json'])
def test_unknown_or_escaping_copy_targets_rejected(tmp_path, name):
    m = api()
    with m.CopyTarget(tmp_path / 'target') as target:
        before = snapshot(tmp_path / 'target')
        with pytest.raises(m.CutoverError):
            target.install(name, b'data', 0o600)
        assert snapshot(tmp_path / 'target') == before


@pytest.mark.parametrize('hazard', ['symlink', 'wrong_mode', 'hardlink', 'fifo'])
def test_existing_destination_never_repaired_or_overwritten(tmp_path, hazard):
    m = api()
    root = tmp_path / 'target'
    root.mkdir(mode=0o700)
    p = root / 'live_backup.json'
    if hazard == 'symlink':
        p.symlink_to(tmp_path / 'outside')
    elif hazard == 'fifo':
        os.mkfifo(p)
    else:
        p.write_bytes(b'data')
        p.chmod(0o644 if hazard == 'wrong_mode' else 0o600)
        if hazard == 'hardlink':
            os.link(p, tmp_path / 'outside')
    before = snapshot(root)
    with m.CopyTarget(root) as target, pytest.raises(m.CutoverError):
        target.install('live_backup.json', b'data', 0o600)
    assert snapshot(root) == before


def test_destination_appearing_at_publication_is_not_overwritten(tmp_path, monkeypatch):
    m = api()
    root = tmp_path / 'target'
    original = os.link
    def race(src, dst, **kw):
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=kw['dst_dir_fd'])
        os.write(fd, b'competitor')
        os.close(fd)
        return original(src, dst, **kw)
    with m.CopyTarget(root) as target:
        monkeypatch.setattr(os, 'link', race)
        with pytest.raises(m.CutoverError, match='DESTINATION_CONFLICT'):
            target.install('live_backup.json', b'ours', 0o600)
    assert snapshot(root)['live_backup.json'][0] == b'competitor'
    assert not list(root.glob('.tmp-*'))


def test_failure_before_publication_removes_only_own_staging(tmp_path, monkeypatch):
    m = api()
    root = tmp_path / 'target'
    with m.CopyTarget(root) as target:
        remnant = root / '.tmp-abandoned'
        remnant.write_bytes(b'untrusted')
        def failure(*a, **kw):
            raise OSError('synthetic fsync failure')
        monkeypatch.setattr(os, 'fsync', failure)
        with pytest.raises(m.CutoverError):
            target.install('live_backup.json', b'data', 0o600)
        assert not (root / 'live_backup.json').exists()
        assert sorted(p.name for p in root.iterdir()) == ['.tmp-abandoned']


def test_manifest_and_generation_bytes_are_not_reserialized(tmp_path):
    m = api()
    raw = b'{ "original bytes": true }\n'
    digest = hashlib.sha256(raw).hexdigest()
    names = [(digest+'.json', 0o400), ('.cutover/'+digest+'.json', 0o400),
             ('.recovery-generations/'+digest+'/manifest.json', 0o600),
             ('.recovery-generations/'+digest+'/live_backup.json', 0o600)]
    with m.CopyTarget(tmp_path / 'target') as target:
        for name, mode in names:
            assert target.install(name, raw, mode) == 'CREATED'
    with m.ReadTree(tmp_path / 'target') as tree:
        for name, mode in names:
            assert tree.read(name).raw == raw
            assert tree.read(name).stat.mode == mode


def test_shared_verification_lock_never_creates_missing_package(tmp_path):
    m = api()
    root = tmp_path / 'target'
    root.mkdir(mode=0o700)
    before = snapshot(root)
    with m.ReadTree(root) as tree, pytest.raises(m.CutoverError):
        with tree.shared():
            pytest.fail('missing lock must block')
    assert snapshot(root) == before


def test_shared_lock_preserves_package_metadata(tmp_path):
    m = api()
    root = tmp_path / 'target'
    with m.CopyTarget(root) as target:
        with target.exclusive():
            pass
    before = snapshot(root)
    with m.ReadTree(root) as tree, tree.shared():
        assert tree.entries('.cutover') == ('lock',)
    assert snapshot(root) == before


@pytest.mark.parametrize('ending', ['success', 'exception', 'crash'])
def test_cross_process_lock_busy_then_released(tmp_path, ending):
    m = api()
    root = tmp_path / 'target'
    program = '''
import sys
from pathlib import Path
from startup_recovery.cutover_files import CopyTarget
with CopyTarget(Path(sys.argv[1])) as target, target.exclusive():
    print('LOCKED', flush=True)
    sys.stdin.readline()
    if sys.argv[2] == 'exception': raise RuntimeError('synthetic')
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', program, str(root), ending],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'LOCKED'
        with m.CopyTarget(root) as target, pytest.raises(m.CutoverError, match='BUSY'):
            with target.exclusive():
                pytest.fail('second process acquired busy lock')
        if ending == 'crash':
            child.kill()
            child.communicate(timeout=10)
        else:
            child.communicate('\n', timeout=10)
        with m.CopyTarget(root) as target, target.exclusive():
            assert target.install('live_backup.json', b'after release', 0o600) == 'CREATED'
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_foreign_owned_source_is_rejected(source):
    m = api()
    p = source / 'foreign'
    p.write_bytes(b'foreign-owned synthetic fixture')
    subprocess.run(['sudo', 'chown', '0:0', str(p)], check=True)
    before = p.stat()
    with m.ReadTree(source) as tree, pytest.raises(m.CutoverError):
        tree.read('foreign')
    after = p.stat()
    assert (before.st_uid, before.st_gid, before.st_atime_ns, before.st_mtime_ns,
            before.st_ctime_ns) == (after.st_uid, after.st_gid, after.st_atime_ns,
                                   after.st_mtime_ns, after.st_ctime_ns)


def test_source_changed_during_read_is_rejected(source, monkeypatch):
    m = api()
    original = os.read
    changed = False
    def racing_read(fd, length):
        nonlocal changed
        raw = original(fd, length)
        if not changed:
            changed = True
            # Synthetic competing writer, not the reader under test.
            (source / 'live_backup.json').write_bytes(b'changed by competitor')
        return raw
    with m.ReadTree(source) as tree:
        monkeypatch.setattr(os, 'read', racing_read)
        with pytest.raises(m.CutoverError, match='FILE_CHANGED'):
            tree.read('live_backup.json')


def test_pinned_root_cannot_be_replaced(source, tmp_path):
    m = api()
    with m.ReadTree(source) as tree:
        source.rename(tmp_path / 'old-source')
        source.mkdir(mode=0o700)
        (source / 'live_backup.json').write_bytes(b'replacement')
        with pytest.raises(m.CutoverError, match='ROOT_CHANGED'):
            tree.read('live_backup.json')


def test_wrong_destination_directory_mode_blocks_identical_reuse(tmp_path):
    m = api()
    root = tmp_path / 'target'
    raw = b'original'
    digest = hashlib.sha256(raw).hexdigest()
    relative = '.recovery-generations/'+digest+'/live_backup.json'
    with m.CopyTarget(root) as target:
        target.install(relative, raw, 0o600)
        (root / '.recovery-generations' / digest).chmod(0o755)
        before = snapshot(root)
        with pytest.raises(m.CutoverError, match='UNSAFE_MODE'):
            target.install(relative, raw, 0o600)
        assert snapshot(root) == before


def test_overlapping_roots_rejected_by_shared_manifest_contract(source):
    from startup_recovery.cutover_manifest import CutoverError, separate
    for a, b in [(source, source), (source, source / 'nested'), (source / 'nested', source)]:
        with pytest.raises(CutoverError, match='ROOT_OVERLAP'):
            separate(a, b)


def test_two_independent_writers_never_overwrite_one_another(tmp_path):
    m = api()
    root = tmp_path / 'target'
    with m.CopyTarget(root):
        pass
    program = '''
import sys
from pathlib import Path
from startup_recovery.cutover_files import CopyTarget, CutoverError
with CopyTarget(Path(sys.argv[1])) as target:
    print('READY', flush=True)
    sys.stdin.readline()
    try: print(target.install('live_backup.json', sys.argv[2].encode(), 0o600), flush=True)
    except CutoverError as e: print(e.code, flush=True)
'''
    workers = [subprocess.Popen([sys.executable, '-B', '-c', program, str(root), content],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for content in ('first', 'second')]
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == 'READY'
        for worker in workers:
            worker.stdin.write('\n')
            worker.stdin.flush()
        results = [worker.communicate(timeout=10)[0].strip() for worker in workers]
        assert results.count('CREATED') == 1
        assert sum(r in ('DESTINATION_CONFLICT', 'UNSAFE_FILE', 'FILE_CHANGED') for r in results) == 1
        with m.ReadTree(root) as tree:
            assert tree.read('live_backup.json').raw in (b'first', b'second')
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=10)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'wrong_mode'])
def test_coordination_lock_cannot_alias_or_repair_untrusted_file(tmp_path, kind):
    m = api()
    root = tmp_path / 'target'
    root.mkdir(mode=0o700)
    (root / '.cutover').mkdir(mode=0o700)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'unchanged')
    outside.chmod(0o600)
    lock = root / '.cutover' / 'lock'
    if kind == 'symlink':
        lock.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, lock)
    else:
        lock.write_bytes(b'')
        lock.chmod(0o644)
    before = snapshot(root)
    with m.CopyTarget(root) as target, pytest.raises(m.CutoverError):
        with target.exclusive():
            pytest.fail('unsafe coordination lock accepted')
    with m.ReadTree(root) as tree, pytest.raises(m.CutoverError):
        with tree.shared():
            pytest.fail('unsafe shared lock accepted')
    assert snapshot(root) == before


def test_process_crash_leaves_staging_untrusted_and_source_unchanged(source, tmp_path):
    m = api()
    before = snapshot(source)
    root = tmp_path / 'target'
    program = '''
import os, sys
from pathlib import Path
from startup_recovery.cutover_files import CopyTarget, ReadTree
def stop_before_publication(*a, **kw):
    print('STAGED', flush=True)
    sys.stdin.readline()
    raise RuntimeError('parent should kill this process')
with ReadTree(Path(sys.argv[1])) as source, CopyTarget(Path(sys.argv[2])) as target, target.exclusive():
    raw = source.read('live_backup.json').raw
    os.link = stop_before_publication
    target.install('live_backup.json', raw, 0o600)
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', program, str(source), str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'STAGED'
        child.kill()
        child.communicate(timeout=10)
        with m.ReadTree(root) as tree:
            remnants = tuple(n for n in tree.entries('.') if n.startswith('.tmp-'))
            assert len(remnants) == 1
        assert not (root / 'live_backup.json').exists()
        assert snapshot(source) == before
        with m.CopyTarget(root) as target, target.exclusive():
            assert target.install('live_backup.json', b'{"legacy":true}\n', 0o600) == 'CREATED'
        with m.ReadTree(root) as tree:
            assert tuple(n for n in tree.entries('.') if n.startswith('.tmp-')) == remnants
        assert snapshot(source) == before
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_readtree_shared_has_no_filesystem_write_operations(tmp_path, monkeypatch):
    m = api()
    root = tmp_path / 'target'
    with m.CopyTarget(root) as target, target.exclusive():
        target.install('live_backup.json', b'data', 0o600)
    before = snapshot(root)
    original = os.open
    def read_only(name, flags, *a, **kw):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original(name, flags, *a, **kw)
    def forbidden(*a, **kw):
        pytest.fail('reader attempted filesystem mutation')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', read_only)
        for name in ('write', 'mkdir', 'unlink', 'rename', 'replace', 'link', 'chmod', 'fchmod', 'chown', 'utime'):
            patch.setattr(os, name, forbidden)
        with m.ReadTree(root) as tree, tree.shared():
            assert tree.read('live_backup.json').raw == b'data'
            assert tree.entries('.') == ('.cutover', 'live_backup.json')
    assert snapshot(root) == before
