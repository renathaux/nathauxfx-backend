"""Legacy endpoint credential sourcing only; no real order/network calls."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def endpoint(monkeypatch):
    import api
    import ctrader_connector
    monkeypatch.delenv('ADMIN_TOKEN', raising=False)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Broker boundary must not be reached')
    for mod, names in [(api, ('place_market_order', '_execute_live_order_core_impl')),
                       (ctrader_connector, ('place_market_order',))]:
        for name in names:
            monkeypatch.setattr(mod, name, forbidden)
    return api, calls


def test_admin_credential_is_not_embedded(endpoint):
    api, _ = endpoint
    tree = ast.parse(Path(api.__file__).read_text())
    unsafe = any(isinstance(n, ast.Assign) and
        any(isinstance(t, ast.Name) and t.id == 'ADMIN_TOKEN' for t in n.targets)
        and isinstance(n.value, ast.Constant) and bool(n.value.value)
        for n in ast.walk(tree))
    assert not unsafe, 'Embedded admin credential remains'


def test_runtime_admin_key_authorizes_unchanged_acknowledgement(endpoint, monkeypatch, capsys, caplog):
    api, calls = endpoint
    for key in ('synthetic-admin-one', 'synthetic-admin-two', 'synthetic-unicode-\u00e9'):
        monkeypatch.setenv('ADMIN_TOKEN', key)
        result = api.execute_trade(api.TradeRequest(symbol='EURUSD', action='BUY', token=key))
        assert result == dict(ok=True, message='Trade request received for EURUSD BUY', symbol='EURUSD', action='BUY')
        output = capsys.readouterr()
        assert key not in repr(result) + output.out + output.err + caplog.text
    assert calls == []  # This legacy endpoint acknowledges; it never creates orders.


@pytest.mark.parametrize('configured,supplied', [
    (None, ''), (None, 'synthetic-admin'), ('', ''), ('', 'synthetic-admin'),
    ('   ', '   '), ('synthetic-admin', ''), ('synthetic-admin', 'wrong'),
    ('synthetic-admin', None), ('synthetic-admin', 123),
    ('synthetic-admin', {'token': 'synthetic-admin'}), ('synthetic-admin', ['synthetic-admin']),
])
def test_invalid_or_missing_credential_blocks_before_order(endpoint, monkeypatch, capsys, caplog, configured, supplied):
    api, calls = endpoint
    if configured is not None:
        monkeypatch.setenv('ADMIN_TOKEN', configured)
    result = api.execute_trade(SimpleNamespace(symbol='EURUSD', action='BUY', token=supplied))
    assert result == dict(ok=False, message='Unauthorized')
    assert calls == []
    output = capsys.readouterr()
    assert 'synthetic-admin' not in repr(result) + output.out + output.err + caplog.text


def test_http_valid_and_wrong_key_preserve_existing_contract(endpoint, monkeypatch):
    api, calls = endpoint
    monkeypatch.setenv('ADMIN_TOKEN', 'synthetic-admin')
    # No lifespan context: do not start recovery or background workers.
    client = TestClient(api.app)
    for supplied, allowed in [('synthetic-admin', True), ('wrong', False), ('', False)]:
        response = client.post('/execute-trade', json=dict(symbol='EURUSD', action='BUY', token=supplied))
        assert response.status_code == 200 and response.json()['ok'] is allowed
        assert 'synthetic-admin' not in response.text
    assert calls == []


@pytest.mark.parametrize('body', [
    dict(symbol='EURUSD', action='BUY', token={'secret': 'synthetic-admin'}),
    dict(symbol='EURUSD', action='BUY', token=['synthetic-admin']),
    dict(symbol='EURUSD', action='BUY', token=None),
    dict(symbol='EURUSD', token='synthetic-admin'),
])
def test_http_validation_errors_never_echo_supplied_credentials(endpoint, monkeypatch, capsys, caplog, body):
    api, calls = endpoint
    monkeypatch.setenv('ADMIN_TOKEN', 'synthetic-admin')
    response = TestClient(api.app).post('/execute-trade', json=body)
    assert response.status_code == 422
    output = capsys.readouterr()
    assert 'synthetic-admin' not in response.text + output.out + output.err + caplog.text
    assert calls == []
