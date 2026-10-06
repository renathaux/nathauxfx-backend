"""Exercise launch and lazy imports in fresh interpreters, no allowed side effects."""
import json
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('modules', [
    ['closed_market_bootstrap'],
    ['api', 'app_bootstrap', 'closed_market_bootstrap'],
    ['strategies.shared', 'brain', 'api'],
    ['services.news_service', 'ctrader_connector', 'closed_market_bootstrap'],
    ['closed_market_bootstrap', '--factory'],
])
def test_imports_do_not_restore_mutate_or_start_workers(modules):
    probe = Path(__file__).with_name('recovery_import_probe.py')
    result = subprocess.run([sys.executable, '-B', str(probe), *modules],
        cwd=probe.parents[1], capture_output=True, text=True, timeout=30,
        env={'PATH': '/usr/bin:/bin', 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1',
             'PYTHONPATH': str(probe.parents[1]), 'DATABASE_URL': 'sqlite:///:memory:'})
    report = json.loads(result.stdout)
    assert report == {'violations': [], 'error_type': None}, report
    assert result.returncode == 0
