"""Focused offline checks: real DB authentication; no application startup/broker IO."""
import importlib
import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
def service():
    return importlib.import_module("services.ctrader_symbol_metadata")


@pytest.fixture
def api(monkeypatch):
    from services import user_auth_service as auth
    route = importlib.import_module("routes.ctrader_symbol_metadata")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    auth.metadata.create_all(engine)
    monkeypatch.setattr(auth, "default_engine", engine)
    tokens = {}
    for role in ("admin", "user"):
        with engine.begin() as connection:
            connection.execute(auth.users.insert().values(
                id=role, email=f"{role}@example.invalid", full_name=role,
                password_hash="unused", role=role, is_active=True,
                email_verified=True, approval_status="APPROVED",
                created_at=time.time(), updated_at=time.time(),
            ))
        tokens[role] = auth.create_session(role, engine=engine)[0]
    app = FastAPI()
    app.include_router(route.router)
    calls = []
    monkeypatch.setattr(route, "collect_symbol_market_hours", lambda: calls.append(True) or {"symbols": []})
    monkeypatch.setenv("CTRADER_SYMBOL_METADATA_DIAGNOSTIC_ENABLED", "1")
    with TestClient(app) as client:
        yield route, client, tokens, calls
    engine.dispose()


def request(client, token=None, scheme="FlowSignalUser"):
    return client.get("/admin/ctrader/symbol-market-hours", headers={"Authorization": f"{scheme} {token}"} if token else {})


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_disabled_flag_never_reads_broker(api, monkeypatch, flag):
    _, client, tokens, calls = api
    if flag is None:
        monkeypatch.delenv("CTRADER_SYMBOL_METADATA_DIAGNOSTIC_ENABLED")
    else:
        monkeypatch.setenv("CTRADER_SYMBOL_METADATA_DIAGNOSTIC_ENABLED", flag)
    response = request(client, tokens["admin"])
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize("identity,scheme,status", [(None, "FlowSignalUser", 401), ("user", "FlowSignalUser", 403), ("admin", "Bearer", 401)])
def test_db_auth_only_no_legacy_bearer_fallback(api, identity, scheme, status):
    _, client, tokens, calls = api
    response = request(client, tokens.get(identity), scheme)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


def test_valid_db_admin_and_no_store(api):
    _, client, tokens, calls = api
    response = request(client, tokens["admin"])
    assert response.status_code == 200
    assert response.json() == {"symbols": []}
    assert response.headers["cache-control"] == "no-store"
    assert calls == [True]
    assert "/admin/ctrader/symbol-market-hours" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("boundary", ["authentication", "broker"])
def test_errors_never_expose_or_log_secrets(api, monkeypatch, capsys, caplog, boundary):
    route, client, tokens, calls = api
    def fail(*args):
        raise RuntimeError("access_token=SECRET refresh_token=SECRET client_secret=SECRET postgres://SECRET")
    monkeypatch.setattr(route, "require_admin" if boundary == "authentication" else "collect_symbol_market_hours", fail)
    response = request(client, tokens["admin"])
    assert response.status_code == 503
    assert response.json() == {"detail": "SYMBOL_METADATA_UNAVAILABLE"}
    assert response.headers["cache-control"] == "no-store"
    captured = capsys.readouterr()
    assert "SECRET" not in response.text + captured.out + captured.err + caplog.text
    assert calls == []


@pytest.fixture
def broker(service, monkeypatch):
    class Socket:
        closed = False
        def close(self):
            self.closed = True
    sock = Socket()
    sent = []
    full = {"symbol": [
        {"symbolId": 42, "scheduleTimeZone": "Europe/Prague",
         "schedule": [{"startSecond": 86400, "endSecond": 170000, "access_token": "SECRET"}],
         "holiday": [{"holidayId": "7", "name": "Holiday", "holidayDate": "20000", "isRecurring": False,
                      "scheduleTimeZone": "Europe/Prague", "startSecond": 0, "endSecond": 86400, "refresh_token": "SECRET"}],
         "client_secret": "SECRET", "minVolume": 100},
        {"symbolId": 99, "accessToken": "SECRET"},
    ]}
    def send(sock, kind, payload, expected):
        sent.append((kind, payload, expected))
        assert kind in {2100, 2102, 2114}, "Unexpected broker path"
        if kind == 2114:
            return {"payloadType": expected, "payload": {"ctidTraderAccountId": "123", "symbol": [
                {"symbolId": 42, "symbolName": "EURUSD"}, {"symbolId": 99, "symbolName": "XAUUSD"},
                {"symbolId": 777, "symbolName": "UNRELATED"}]}}
        return {"payloadType": expected, "payload": {"ctidTraderAccountId": "123"}}
    def full_symbols(sock, account, ids):
        assert account == 123 and ids == [42, 99]
        sent.append((2116, {"symbolId": ids}, 2117))
        return full
    connector = SimpleNamespace(
        open_ctrader_json_socket=lambda host, port, **kwargs: sock,
        send_ctrader_request=send, fetch_ctrader_full_symbols=full_symbols,
        CTRADER_JSON_ENDPOINTS={"demo": ("demo.ctraderapi.com", 5036)},
        PAYLOAD_APPLICATION_AUTH_REQ=2100, PAYLOAD_APPLICATION_AUTH_RES=2101,
        PAYLOAD_ACCOUNT_AUTH_REQ=2102, PAYLOAD_ACCOUNT_AUTH_RES=2103,
        PAYLOAD_SYMBOLS_LIST_REQ=2114, PAYLOAD_SYMBOLS_LIST_RES=2115,
    )
    monkeypatch.setitem(sys.modules, "ctrader_connector", connector)
    monkeypatch.setattr(service, "_selected_account", lambda: ("123", "demo", "revision"))
    monkeypatch.setattr(service, "_credentials", lambda: {"client_id": "ID", "client_secret": "SECRET", "access_token": "SECRET"})
    return sock, sent, full, connector


def test_allowlist_nulls_and_success_socket_cleanup(service, broker, capsys, caplog):
    sock, sent, _, _ = broker
    result = service.collect_symbol_market_hours()
    assert result == {"symbols": [
        {"symbolId": 42, "symbolName": "EURUSD", "scheduleTimeZone": "Europe/Prague",
         "schedule": [{"startSecond": 86400, "endSecond": 170000}],
         "holiday": [{"holidayId": "7", "name": "Holiday", "holidayDate": "20000", "isRecurring": False,
                      "scheduleTimeZone": "Europe/Prague", "startSecond": 0, "endSecond": 86400}]},
        {"symbolId": 99, "symbolName": "XAUUSD", "scheduleTimeZone": None, "schedule": None, "holiday": None},
    ]}
    assert [item[0] for item in sent] == [2100, 2102, 2114, 2116]
    assert sock.closed
    captured = capsys.readouterr()
    assert "SECRET" not in json.dumps(result) + captured.out + captured.err + caplog.text


@pytest.mark.parametrize("failure", ["auth", "full", "selection_changed", "malformed", "duplicate", "foreign_account"])
def test_failure_closes_socket_without_refresh_or_switch(service, broker, monkeypatch, failure):
    sock, sent, full, connector = broker
    original = connector.send_ctrader_request
    def fail(*args):
        raise RuntimeError("SECRET")
    if failure == "auth":
        connector.send_ctrader_request = fail
    elif failure == "full":
        connector.fetch_ctrader_full_symbols = fail
    elif failure == "selection_changed":
        snapshots = iter([("123", "demo", "revision"), ("456", "demo", "next")])
        monkeypatch.setattr(service, "_selected_account", lambda: next(snapshots))
    elif failure == "malformed":
        full["symbol"][0]["schedule"][0]["startSecond"] = {"secret": "SECRET"}
    else:
        def changed(*args):
            result = original(*args)
            if failure == "foreign_account":
                result["payload"]["ctidTraderAccountId"] = "456"
            elif args[1] == 2114:
                result["payload"]["symbol"].append({"symbolId": 43, "symbolName": "EURUSD"})
            return result
        connector.send_ctrader_request = changed
    with pytest.raises(Exception):
        service.collect_symbol_market_hours()
    assert sock.closed
    assert all(item[0] in {2100, 2102, 2114, 2116} for item in sent)


def test_credential_echo_inside_allowed_field_is_rejected(service, broker):
    sock, _, full, _ = broker
    full["symbol"][0]["holiday"][0]["name"] = "echo SECRET"
    with pytest.raises(ValueError):
        service.collect_symbol_market_hours()
    assert sock.closed


def test_partial_fields_stay_null_and_explicit_empty_lists_stay_empty(service, broker):
    _, _, full, _ = broker
    full["symbol"][0]["holiday"] = [{"holidayDate": "20000"}]
    full["symbol"][1]["schedule"] = []
    result = service.collect_symbol_market_hours()["symbols"]
    assert result[0]["holiday"] == [{"holidayId": None, "name": None, "scheduleTimeZone": None,
        "holidayDate": "20000", "isRecurring": None, "startSecond": None, "endSecond": None}]
    assert result[1]["schedule"] == []


def test_socket_open_failure_releases_admission_lock(service, broker):
    _, _, _, connector = broker
    original = connector.open_ctrader_json_socket
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET")
    connector.open_ctrader_json_socket = fail
    with pytest.raises(RuntimeError):
        service.collect_symbol_market_hours()
    connector.open_ctrader_json_socket = original
    assert len(service.collect_symbol_market_hours()["symbols"]) == 2


def test_real_endpoint_real_collector_filters_upstream(api, service, broker, monkeypatch):
    route, client, tokens, _ = api
    monkeypatch.setattr(route, "collect_symbol_market_hours", service.collect_symbol_market_hours)
    response = request(client, tokens["admin"])
    assert response.status_code == 200
    assert [item["symbolName"] for item in response.json()["symbols"]] == ["EURUSD", "XAUUSD"]
    assert "SECRET" not in response.text and "UNRELATED" not in response.text
    assert broker[0].closed


def test_durable_selected_account_read_only_and_fail_closed(service, monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy.orm import sessionmaker
    import db
    from models import RuntimeSetting
    engine = create_engine("sqlite://")
    RuntimeSetting.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "999")
    with pytest.raises(ValueError):
        service._selected_account()  # No legacy account/environment fallback.
    with factory() as session:
        session.add(RuntimeSetting(setting_name="ctrader_active_account",
            setting_value='{"account_id":"123","env":"demo"}',
            updated_at=datetime.now(timezone.utc), updated_by="fixture"))
        session.commit()
    assert service._selected_account()[:2] == ("123", "demo")
    with factory() as session:
        row = session.get(RuntimeSetting, "ctrader_active_account")
        assert row.setting_value == '{"account_id":"123","env":"demo"}'
        assert row.updated_by == "fixture"
    engine.dispose()


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_invalidated_db_admin_session_cannot_read_metadata(api, state):
    from services import user_auth_service as auth
    _, client, tokens, calls = api
    with auth.default_engine.begin() as connection:
        values = {"expires_at": 0} if state == "expired" else {"revoked_at": time.time()}
        connection.execute(auth.sessions.update().where(auth.sessions.c.user_id == "admin").values(**values))
    assert request(client, tokens["admin"]).status_code == 401
    assert calls == []


def test_existing_cookie_auth_is_supported(api):
    _, client, tokens, _ = api
    client.cookies.set("flowsignal_session", tokens["admin"])
    assert request(client).status_code == 200


def test_existing_encrypted_credentials_are_read_without_persistence(service, monkeypatch, capsys, caplog):
    from sqlalchemy.orm import sessionmaker
    from models import CTraderOAuthToken
    from services import ctrader_token_service as store
    engine = create_engine("sqlite://")
    CTraderOAuthToken.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(store, "SessionLocal", factory)
    monkeypatch.setenv("CTRADER_TOKEN_ENCRYPTION_KEY", "TEST_KEY")
    monkeypatch.setenv("CTRADER_CLIENT_ID", "TEST_ID")
    monkeypatch.setenv("CTRADER_CLIENT_SECRET", "TEST_SECRET")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "STALE_TEST_TOKEN")
    store.save_tokens("CURRENT_TEST_TOKEN", "TEST_REFRESH", session_factory=factory)
    with factory() as session:
        row = session.get(CTraderOAuthToken, "ctrader")
        before = (row.encrypted_access_token, row.encrypted_refresh_token, row.updated_at)
    assert service._credentials() == {"client_id": "TEST_ID", "client_secret": "TEST_SECRET", "access_token": "CURRENT_TEST_TOKEN"}
    with factory() as session:
        row = session.get(CTraderOAuthToken, "ctrader")
        assert (row.encrypted_access_token, row.encrypted_refresh_token, row.updated_at) == before
    captured = capsys.readouterr()
    assert not any(value in captured.out + captured.err + caplog.text
                   for value in ("TEST_SECRET", "CURRENT_TEST_TOKEN", "TEST_REFRESH"))
    engine.dispose()
