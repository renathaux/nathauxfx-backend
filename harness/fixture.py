from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3

DDL = Path(__file__).with_name('legacy-schema.sql')

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
