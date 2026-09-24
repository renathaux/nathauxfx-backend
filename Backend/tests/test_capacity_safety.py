import subprocess,sys,os
from pathlib import Path


def probe(code):
    env={'PATH':os.environ['PATH'],'CAPACITY_STAGING':'1','PYTHONPATH':str(Path('Backend/capacity_probe').resolve())+os.pathsep+str(Path('Backend').resolve())}
    return subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)


def test_staging_blocks_broker_and_database_network():
    result=probe("import socket\nfor host in ['live.ctraderapi.com','production.neon.tech']:\n try:socket.create_connection((host,443),timeout=.1)\n except PermissionError:continue\n raise AssertionError('network permitted')\nprint('BLOCKED')")
    assert result.returncode==0,result.stderr
    assert 'BLOCKED' in result.stdout


def test_staging_rejects_nonisolated_database_before_import():
    result=probe("import os;from capacity_probe.safety import validate_database;validate_database('postgresql://test:fake@prod.neon.tech/db')")
    assert result.returncode!=0 and 'isolated SQLite' in result.stderr


def test_staging_paths_cannot_reuse_checkout_state():
    result=probe("import paths;assert '/capacity-isolated-' in str(paths.DATA_DIR);assert not (paths.DATA_DIR/'ctrader_accounts.json').exists()")
    assert result.returncode==0,result.stderr
