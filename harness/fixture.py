from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3

DDL = Path(__file__).with_name('legacy-schema.sql')
SCRIPT = Path(__file__).parent/'test-only-inputs/migrate_sqlite_to_neon.py'
SCRIPT_PIN = '944d6cfd065d9f893344d0a28860813c694655706177f7b2c261de245f68480a'
SCRIPT_PROVENANCE = {
    'source_commit': '63b42795ca26bdecb1be6d7758c8ae757eee90d4',
    'original_path': 'Backend/scripts/migrate_sqlite_to_neon.py',
    'git_blob': 'cc969a9814e3ee3d6c7c8ebbc34df7eb567be7f4',
    'bytes': 5788, 'sha256': SCRIPT_PIN,
}

@contextmanager
def temporary_script(path):
    raw = SCRIPT.read_bytes()
    assert len(raw) == SCRIPT_PROVENANCE['bytes'] and hashlib.sha256(raw).hexdigest() == SCRIPT_PIN
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        assert path.read_bytes() == raw
        yield
    finally:
        path.unlink()
        assert not path.exists()

def fingerprint():
    return hashlib.sha256(DDL.read_bytes()).hexdigest()

@contextmanager
def temporary_database(path):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(DDL.read_text())
            connection.commit()
            assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        finally:
            connection.close()
        path.chmod(0o400)
        yield
    finally:
        path.unlink()
        assert not path.exists()
        assert all(not Path(str(path)+suffix).exists() for suffix in ('-wal', '-shm', '-journal'))
