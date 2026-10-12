"""Exercise real request serialization without importing application startup."""
import ast
import copy
import json
from pathlib import Path
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def wire():
    source = Path(__file__).resolve().parents[1] / "ctrader_connector.py"
    tree = ast.parse(source.read_text())
    names = {"fetch_ctrader_full_symbols", "send_ctrader_request"}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    selected += [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id.startswith("PAYLOAD_") for target in node.targets)]
    sent = []
    response = {
        "payloadType": 2117,
        "payload": {"ctidTraderAccountId": "123", "symbol": [
            {"symbolId": 42, "scheduleTimeZone": "Europe/Prague",
             "schedule": [{"startSecond": 86400, "endSecond": 170000}],
             "holiday": [{"holidayDate": 20000, "isRecurring": False}]},
            {"symbolId": 99},
        ]},
    }
    scope = {"json": json, "uuid": uuid, "CTraderApiError": RuntimeError,
             "websocket_send_text": lambda sock, raw: sent.append(json.loads(raw)),
             "websocket_recv_text": lambda sock: json.dumps(response)}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), scope)
    return scope["fetch_ctrader_full_symbols"], sent, response


def test_full_symbol_wire_is_read_only_and_preserves_missing_fields(wire):
    fetch, sent, response = wire
    original = copy.deepcopy(response)
    assert fetch(object(), "123", [42, "99"]) == original["payload"]
    assert len(sent) == 1
    assert sent[0]["payloadType"] == 2116
    assert sent[0]["payload"] == {"ctidTraderAccountId": 123, "symbolId": [42, 99]}
    assert response == original
    assert "schedule" not in response["payload"]["symbol"][1]


@pytest.mark.parametrize("account,ids", [(True, [42]), (123.5, [42]), (123, []), (123, [42, 42]), (123, [42.1])])
def test_invalid_ids_fail_before_any_wire_request(wire, account, ids):
    fetch, sent, _ = wire
    with pytest.raises(ValueError):
        fetch(object(), account, ids)
    assert sent == []


@pytest.mark.parametrize("change", ["foreign_account", "missing", "duplicate", "unexpected"])
def test_full_symbol_response_must_match_account_and_exact_ids(wire, change):
    fetch, _, response = wire
    if change == "foreign_account":
        response["payload"]["ctidTraderAccountId"] = 456
    elif change == "missing":
        response["payload"]["symbol"].pop()
    elif change == "duplicate":
        response["payload"]["symbol"][1]["symbolId"] = 42
    else:
        response["payload"]["symbol"][1]["symbolId"] = 100
    with pytest.raises(ValueError):
        fetch(object(), 123, [42, 99])


@pytest.mark.parametrize("failure", ["tls", "handshake"])
def test_diagnostic_socket_open_failure_closes_owned_resources(failure):
    source = Path(__file__).resolve().parents[1] / "ctrader_connector.py"
    node = next(item for item in ast.parse(source.read_text()).body
                if isinstance(item, ast.FunctionDef) and item.name == "open_ctrader_json_socket")
    class Socket:
        closed = False
        def settimeout(self, value):
            pass
        def sendall(self, value):
            pass
        def recv(self, count):
            return b"HTTP/1.1 400 Bad Request\r\n\r\n"
        def close(self):
            self.closed = True
    raw, tls = Socket(), Socket()
    def wrap(*args, **kwargs):
        if failure == "tls":
            raise RuntimeError("TLS failure")
        return tls
    import base64
    import os
    scope = {"socket": SimpleNamespace(create_connection=lambda *args, **kwargs: raw),
             "ssl": SimpleNamespace(create_default_context=lambda **kwargs: SimpleNamespace(wrap_socket=wrap)),
             "base64": base64, "os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), scope)
    with pytest.raises(RuntimeError):
        scope["open_ctrader_json_socket"]("example.invalid", 5036, close_on_error=True)
    assert raw.closed
    if failure == "handshake":
        assert tls.closed
