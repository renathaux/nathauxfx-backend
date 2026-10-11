"""Explicit no-follow file transport. Readers never provision, repair or write.

Strict readers require usable O_NOATIME, or a kernel-reported no-atime mount.
Failure never falls back to an ordinary content/directory read. CopyTarget is
the separate write capability, used only by explicit inventory/copy commands.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from uuid import uuid4

from startup_recovery.cutover_manifest import (
    BINDINGS, KINDS, CutoverError, FileImage, FileStat, path, require, separate,
)
from startup_recovery.checkpoints import _valid_hash


def separate_storage(write_root, protected_paths):
    """Pre-provision separation, including Linux bind-mount/subtree aliases.

    Kernel mount roots map namespace paths to filesystem-relative paths without
    opening native files or traversing excluded cache directories. Device/inode
    equality of the two roots alone would miss a bind of a *child* of source.
    Missing/ambiguous platform evidence fails closed, not lexical-only fallback.
    This write preflight is deliberately NOT used by source-independent verify.
    """
    write_root = Path(path(str(write_root), absolute=True))
    protected_paths = tuple(Path(path(str(p), absolute=True)) for p in protected_paths)
    for protected in protected_paths:
        separate(write_root, protected)
    try:
        with open('/proc/self/mountinfo', 'rb') as handle:
            lines = handle.read().splitlines()
        mounts = []
        for line in lines:
            fields = line.split()
            require(len(fields) >= 10 and b'-' in fields, 'PHYSICAL_SEPARATION_UNAVAILABLE')
            def decoded(value):
                for old,new in ((b'\\040',b' '),(b'\\011',b'\t'),(b'\\012',b'\n'),(b'\\134',b'\\')):
                    value = value.replace(old,new)
                return Path(os.fsdecode(value))
            root, mountpoint = decoded(fields[3]), decoded(fields[4])
            require(root.is_absolute() and mountpoint.is_absolute(), 'PHYSICAL_SEPARATION_UNAVAILABLE')
            mounts.append((fields[2],root,mountpoint))
        def physical(location):
            candidates = [(len(point.parts),device,root/location.relative_to(point))
                          for device,root,point in mounts if location.is_relative_to(point)]
            require(bool(candidates), 'PHYSICAL_SEPARATION_UNAVAILABLE')
            depth = max(c[0] for c in candidates)
            views = {(device,location) for d,device,location in candidates if d==depth}
            require(len(views)==1, 'PHYSICAL_SEPARATION_UNAVAILABLE')
            return views.pop()
        def mounted_subtrees(roots):
            # A stable bind below STATE_ROOT can redirect the later diagnostic
            # lock or generation writes even when STATE_ROOT itself is separate.
            # Inspect kernel topology, never enumerate native/cache contents.
            return {*roots, *(point for _,_,point in mounts
                               if any(point.is_relative_to(root) for root in roots))}
        destinations = {physical(p) for p in mounted_subtrees((write_root,))}
        origins = {physical(p) for p in mounted_subtrees(protected_paths)}
        for device, destination in destinations:
            for other_device, origin in origins:
                if device == other_device:
                    separate(destination, origin)
    except (OSError, ValueError, IndexError):
        raise CutoverError('PHYSICAL_SEPARATION_UNAVAILABLE') from None


def _owned(info, *, regular=False, private=False):
    require(info.st_uid == os.geteuid(), 'UNSAFE_OWNERSHIP')
    require(not info.st_mode & 0o022, 'UNSAFE_MODE')
    if regular:
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'UNSAFE_FILE')
    else:
        require(stat.S_ISDIR(info.st_mode), 'UNSAFE_DIRECTORY')
    if private:
        require(stat.S_IMODE(info.st_mode) == 0o700, 'UNSAFE_MODE')


def _relative(value, *, directory=False):
    if value == '.' and directory:
        return ()
    return path(value, absolute=False).parts


def _directory(root, *, create=False):
    """Pin every component, never resolve a symlink away or trust a path check."""
    parts = path(str(root), absolute=True).parts
    flags = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for name in parts[1:]:
            info = os.fstat(fd)
            require(info.st_uid in (0, os.geteuid()), 'UNSAFE_ANCESTOR')
            require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX), 'UNSAFE_ANCESTOR')
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    syncfd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        os.fsync(syncfd)
                    finally:
                        os.close(syncfd)
                except FileExistsError:
                    pass
                child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        _owned(os.fstat(fd), private=create)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _metadata(info):
    return FileStat(info.st_dev, info.st_ino, info.st_size, stat.S_IMODE(info.st_mode),
                    info.st_uid, info.st_gid, info.st_mtime_ns, info.st_ctime_ns, info.st_atime_ns)


def _readonly(name, parent, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    if hasattr(os, 'O_NOATIME'):
        flags |= os.O_NOATIME
    else:
        noatime = getattr(os, 'ST_NOATIME', 0)
        require(noatime and os.fstatvfs(parent).f_flag & noatime, 'NOATIME_UNAVAILABLE')
    try:
        return os.open(name, flags, dir_fd=parent)
    except PermissionError:
        raise CutoverError('NOATIME_OR_ACCESS_DENIED') from None


class ReadTree:
    _private_directories = False

    def __init__(self, root):
        self.root = Path(root)
        try:
            self._fd = _directory(root)
        except FileNotFoundError:
            raise
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None
        s = os.fstat(self._fd)
        self._identity = (s.st_dev, s.st_ino)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_root(self):
        require(self._fd is not None, 'TREE_CLOSED')
        fresh = _directory(self.root)
        try:
            s = os.fstat(fresh)
            _owned(s, private=self._private_directories)
            require((s.st_dev, s.st_ino) == self._identity, 'ROOT_CHANGED')
        finally:
            os.close(fresh)

    @contextmanager
    def _parent(self, parts, *, create=False):
        self._check_root()
        fd = os.dup(self._fd)
        try:
            for name in parts:
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                        syncfd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            os.fsync(syncfd)
                        finally:
                            os.close(syncfd)
                    except FileExistsError:
                        pass
                child = _readonly(name, fd, directory=True)
                try:
                    _owned(os.fstat(child), private=create or self._private_directories)
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            yield fd
            self._check_root()
        finally:
            os.close(fd)

    def read(self, relative):
        parts = _relative(relative)
        try:
            with self._parent(parts[:-1]) as parent:
                fd = _readonly(parts[-1], parent)
                try:
                    before = os.fstat(fd)
                    _owned(before, regular=True)
                    chunks = []
                    while chunk := os.read(fd, 65536):
                        chunks.append(chunk)
                    after = os.fstat(fd)
                    current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    require(_metadata(before) == _metadata(after) == _metadata(current)
                            and before.st_nlink == after.st_nlink == current.st_nlink == 1, 'FILE_CHANGED')
                    raw = b''.join(chunks)
                    require(len(raw) == before.st_size, 'FILE_CHANGED')
                    return FileImage(raw, _metadata(before))
                finally:
                    os.close(fd)
        except FileNotFoundError:
            raise
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None

    def entries(self, relative):
        parts = _relative(relative, directory=True)
        try:
            with self._parent(parts) as parent:
                fd = _readonly('.', parent, directory=True)
                try:
                    before = os.fstat(fd)
                    names = tuple(sorted(os.listdir(fd)))
                    require(_metadata(before) == _metadata(os.fstat(fd)), 'DIRECTORY_CHANGED')
                    return names
                finally:
                    os.close(fd)
        except FileNotFoundError:
            raise
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None

    @contextmanager
    def shared(self):
        """An existing diagnostic lock only; no provisioning or state publication."""
        try:
            with self._parent(('.cutover',)) as parent:
                fd = _readonly('lock', parent)
                try:
                    _owned(os.fstat(fd), regular=True)
                    require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o600, 'UNSAFE_MODE')
                    try:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise CutoverError('BUSY') from None
                    yield
                finally:
                    os.close(fd)
        except FileNotFoundError:
            raise CutoverError('PACKAGE_INCOMPLETE') from None
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None


def _target(relative, mode):
    parts = _relative(relative)
    if len(parts) == 1 and parts[0] in BINDINGS.values():
        require(mode == 0o600, 'DESTINATION_MODE_INVALID')
    elif (len(parts) in (1, 2) and (len(parts) == 1 or parts[0] == '.cutover')
          and parts[-1].endswith('.json') and _valid_hash(parts[-1][:-5])):
        require(mode == 0o400, 'DESTINATION_MODE_INVALID')
    elif (len(parts) == 3 and parts[0] == '.recovery-generations' and _valid_hash(parts[1])
          and parts[2] in {'manifest.json', *(k+'.json' for k in KINDS)}):
        require(mode == 0o600, 'DESTINATION_MODE_INVALID')
    else:
        raise CutoverError('DESTINATION_NOT_ALLOWED')
    return parts


class CopyTarget(ReadTree):
    _private_directories = True

    def __init__(self, root):
        try:
            fd = _directory(root, create=True)
            os.close(fd)
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None
        super().__init__(root)

    def _existing(self, relative, raw, mode):
        try:
            found = self.read(relative)
        except FileNotFoundError:
            return False
        require(found.raw == raw and found.stat.mode == mode, 'DESTINATION_CONFLICT')
        return True

    def install(self, relative, raw, mode):
        parts = _target(relative, mode)
        require(type(raw) is bytes, 'CONTENT_INVALID')
        try:
            if self._existing(relative, raw, mode):
                return 'IDENTICAL'
            with self._parent(parts[:-1], create=True) as parent:
                stage = '.tmp-' + uuid4().hex
                fd = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fchmod(handle.fileno(), mode)
                        os.fsync(handle.fileno())
                    try:
                        os.link(stage, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                        result = 'CREATED'
                    except FileExistsError:
                        require(self._existing(relative, raw, mode), 'DESTINATION_CONFLICT')
                        result = 'IDENTICAL'
                finally:
                    os.unlink(stage, dir_fd=parent)
                syncfd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    os.fsync(syncfd)
                finally:
                    os.close(syncfd)
                require(self._existing(relative, raw, mode), 'DESTINATION_CONFLICT')
                return result
        except OSError:
            raise CutoverError('COPY_FAILED') from None

    @contextmanager
    def exclusive(self):
        try:
            with self._parent(('.cutover',), create=True) as parent:
                fd = os.open('lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
                try:
                    _owned(os.fstat(fd), regular=True)
                    require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o600, 'UNSAFE_MODE')
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise CutoverError('BUSY') from None
                    yield
                finally:
                    os.close(fd)
        except OSError:
            raise CutoverError('UNSAFE_PATH') from None
