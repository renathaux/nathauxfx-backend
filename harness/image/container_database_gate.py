"""Container dry run: certified migration runner and real entrypoint, disposable PG only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from sqlalchemy import create_engine,text
from alembic import command
from alembic.config import Config

assert os.getuid()==501 and os.getgid()==1000
assert os.environ['DATABASE_URL'].startswith('postgresql+psycopg2://cert_user@/verifier_test?host=/tmp/fs-verifier-pg.')
assert os.environ['MIGRATION_DATABASE_URL']==os.environ['DATABASE_URL']
os.chdir('/app/Backend');sys.path.insert(0,'/app/Backend')
cfg=Config('alembic.ini')
command.upgrade(cfg,'20261001_0030')
engine=create_engine(os.environ['DATABASE_URL'])
tables=['recovery_accounts','recovery_attempts','recovery_mutations','recovery_checkpoint_heads']
def snapshot():
    with engine.connect() as conn:
        return dict(revision=conn.scalar(text('SELECT version_num FROM alembic_version')),
            counts={t:conn.scalar(text('SELECT count(*) FROM '+t)) for t in tables})
before=snapshot();assert before['revision']=='20261001_0030' and not any(before['counts'].values())
assert list(Path('/state').iterdir())==[]
mode=sys.argv[1]
if mode=='maintenance':
    subprocess.run([sys.executable,'-B','-m','startup_recovery.migrate'],check=True)
elif mode=='production':
    # Actual unmodified entrypoint performs migration, then starts Uvicorn.
    log=Path('/cache/entrypoint.log')
    with log.open('w') as out:
        child=subprocess.Popen(['/usr/local/bin/flowsignal-start'],stdout=out,stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(150):
            assert child.poll() is None, 'PRODUCTION_ENTRYPOINT_EXITED'
            try:
                with urllib.request.urlopen('http://127.0.0.1:10000/openapi.json',timeout=1) as response:
                    assert response.status==200
                break
            except OSError: time.sleep(.2)
        else: raise AssertionError('PRODUCTION_STARTUP_TIMEOUT')
    finally:
        child.terminate()
        try: child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill();child.wait();raise
else: raise AssertionError('UNKNOWN_MODE')
after=snapshot();assert after['revision']=='20261010_0031' and after['counts']==before['counts']
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM recovery_legacy_adoptions'))==0
subprocess.run([sys.executable,'-B','-m','startup_recovery.migrate'],check=True)
assert snapshot()==after
assert list(Path('/state').iterdir())==[]
engine.dispose()
print(json.dumps({'container_mode':mode,'migration':True,'noop':True,'recovery_counts_unchanged':True,
    'state_empty':True,'actual_production_entrypoint':mode=='production'},sort_keys=True))
