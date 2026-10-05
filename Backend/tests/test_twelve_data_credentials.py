"""Runtime-only Twelve Data credentials; network boundary is always synthetic."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests


@pytest.fixture
def shared(monkeypatch):
    from strategies import shared
    monkeypatch.delenv('TWELVE_DATA_API_KEY', raising=False)
    monkeypatch.setattr(shared.time, 'sleep', lambda _: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected network boundary')
    monkeypatch.setattr(shared.requests, 'get', forbidden)
    return shared


def test_no_embedded_twelve_data_credential_in_source(shared):
    tree = ast.parse(Path(shared.__file__).read_text())
    unsafe = any(isinstance(n, ast.Assign) and
        any(isinstance(t, ast.Name) and t.id == 'TWELVE_DATA_API_KEY' for t in n.targets)
        and isinstance(n.value, ast.Constant) and bool(n.value.value)
        for n in ast.walk(tree))
    assert not unsafe, 'Embedded credential assignment remains'


@pytest.mark.parametrize('value', [None, '', '   ', 'PASTE_YOUR_KEY_HERE'])
def test_missing_key_returns_empty_without_request(shared, monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv('TWELVE_DATA_API_KEY', value)
    calls = []
    def get(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
    monkeypatch.setattr(shared.requests, 'get', get)
    result = shared.safe_download('EUR/USD', '5min', tries=2, pause=0)
    assert result.empty
    assert not calls, 'Unconfigured provider must not contact network'
    assert 'missing API key' in capsys.readouterr().out


def test_runtime_key_is_used_and_normalization_preserved(shared, monkeypatch, capsys):
    # Read configuration at request time; an import-time/default key cannot pass.
    for key in ('synthetic-test-key-one', 'synthetic-test-key-two'):
        monkeypatch.setenv('TWELVE_DATA_API_KEY', key)
        calls = []
        def get(url, *, params, timeout):
            assert params.get('apikey') == key, 'Configured key was not selected'
            safe = {k: v for k, v in params.items() if k != 'apikey'}
            assert safe == dict(symbol='EUR/USD', interval='5min', outputsize=2,
                                format='JSON', timezone='UTC')
            assert url == 'https://api.twelvedata.com/time_series' and timeout == 20
            calls.append(True)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'values': [
                dict(datetime='2026-01-01 00:05:00', open='2', high='3', low='1', close='2.5'),
                dict(datetime='2026-01-01 00:00:00', open='1', high='2', low='0.5', close='1.5')]})
        monkeypatch.setattr(shared.requests, 'get', get)
        result = shared.safe_download('EUR/USD', '5min', outputsize=2, pause=0)
        assert calls == [True]
        assert result.index.tolist() == [pd.Timestamp('2026-01-01 00:00:00'), pd.Timestamp('2026-01-01 00:05:00')]
        assert result.to_dict('list') == dict(Open=[1, 2], High=[2, 3], Low=[0.5, 1], Close=[1.5, 2.5], Volume=[0, 0])
        assert key not in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['transport', 'http', 'provider', 'decode'])
def test_error_paths_do_not_disclose_key_or_provider_payload(shared, monkeypatch, capsys, caplog, failure):
    key = 'synthetic-sensitive-test-key'
    monkeypatch.setenv('TWELVE_DATA_API_KEY', key)
    calls = []
    def error():
        raise requests.RequestException('https://example.invalid/?apikey=' + key)
    def get(*args, **kwargs):
        calls.append(True)
        if failure == 'transport':
            error()
        def status():
            if failure == 'http':
                error()
        def payload():
            if failure == 'decode':
                error()
            return dict(status='error', code=key, message=key)
        return SimpleNamespace(raise_for_status=status, json=payload)
    monkeypatch.setattr(shared.requests, 'get', get)
    assert shared.safe_download('EUR/USD', '5min', tries=2, pause=0).empty
    assert calls == [True, True]  # Existing retry semantics are retained.
    output = capsys.readouterr()
    assert key not in output.out + output.err + caplog.text


def test_empty_success_response_keeps_existing_unavailable_contract(shared, monkeypatch):
    monkeypatch.setenv('TWELVE_DATA_API_KEY', 'synthetic-test-key')
    monkeypatch.setattr(shared.requests, 'get', lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {'values': []}))
    assert shared.safe_download('EUR/USD', '5min', pause=0).empty
