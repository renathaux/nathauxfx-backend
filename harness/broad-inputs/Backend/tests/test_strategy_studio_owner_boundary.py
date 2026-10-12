from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import routes.strategy_studio as studio


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer expired-owner",
        "Bearer",
        "Bearer   ",
        "bEaReR expired-owner",
        "Bearer\texpired-owner",
    ],
)
def test_rejected_explicit_owner_token_never_falls_through_to_customer_cookie(
    monkeypatch, authorization
):
    monkeypatch.setattr(studio, "_legacy_actor", lambda *a, **k: None)
    monkeypatch.setattr(
        studio,
        "current_user",
        lambda r: SimpleNamespace(
            id="customer", role="user", email="customer@example.test"
        ),
    )
    owners = []
    monkeypatch.setattr(
        studio, "list_strategies", lambda owner: owners.append(owner) or []
    )
    app = FastAPI()
    app.include_router(studio.router)
    response = TestClient(app).get(
        "/strategy-studio/strategies",
        headers={
            "Authorization": authorization,
            "Cookie": "flowsignal_session=valid-customer",
        },
    )
    assert response.status_code == 401
    assert owners == []


def test_valid_owner_token_overrides_customer_cookie(monkeypatch):
    monkeypatch.setattr(
        studio,
        "_legacy_actor",
        lambda *a, **k: {"role": "admin", "email": "FLOWSIGNAL.CONTACT@GMAIL.COM"},
    )
    monkeypatch.setattr(
        studio,
        "current_user",
        lambda r: (_ for _ in ()).throw(AssertionError("Customer fallback used")),
    )
    owners = []
    monkeypatch.setattr(
        studio, "list_strategies", lambda owner: owners.append(owner) or []
    )
    app = FastAPI()
    app.include_router(studio.router)
    response = TestClient(app).get(
        "/strategy-studio/strategies", headers={"Authorization": "Bearer owner"}
    )
    assert response.status_code == 200
    assert owners == ["owner:flowsignal.contact@gmail.com"]


def test_canonical_admin_and_customer_identity():
    from services.strategy_studio_owner import canonical_strategy_owner

    for actor in [
        {"role": "admin", "email": " FLOWSIGNAL.CONTACT@GMAIL.COM "},
        {
            "role": "admin",
            "email": "flowsignal.contact@gmail.com",
            "auth_method": "persistent_owner_session",
        },
        SimpleNamespace(
            id="typed-admin", role="admin", email="flowsignal.contact@gmail.com"
        ),
    ]:
        assert canonical_strategy_owner(actor) == "owner:flowsignal.contact@gmail.com"
    for uid in ["customer-1", "test-account"]:
        assert (
            canonical_strategy_owner(
                {"id": uid, "role": "user", "email": "flowsignal.contact@gmail.com"}
            )
            == f"user:{uid}"
        )


def test_placeholder_admin_and_access_code_have_no_strategy_namespace():
    import pytest
    from fastapi import HTTPException
    from services.strategy_studio_owner import canonical_strategy_owner

    for actor in [
        {"role": "admin"},
        {"role": "admin", "email": "legacy-admin"},
        {"role": "user", "email": "flowsignal-access-user"},
    ]:
        with pytest.raises(HTTPException):
            canonical_strategy_owner(actor)


def test_diagnostics_report_owner_and_counts_without_credentials(monkeypatch, caplog):
    import logging, json

    monkeypatch.setattr(
        studio,
        "_legacy_actor",
        lambda *a, **kw: {
            "role": "admin",
            "email": "flowsignal.contact@gmail.com",
            "token": "never-log-secret",
        },
    )
    monkeypatch.setattr(studio, "list_strategies", lambda owner: [])
    monkeypatch.setattr(
        studio,
        "owner_debug_snapshot",
        lambda owner: {
            "strategy_count": 14,
            "active_strategy_id": "gold18",
            "live_strategy_id": "gold18",
        },
    )
    app = FastAPI()
    app.include_router(studio.router)
    with caplog.at_level(logging.INFO):
        response = TestClient(app).get(
            "/strategy-studio/strategies",
            headers={"Authorization": "Bearer never-log-secret"},
        )
    assert response.json()["owner_id"] == "owner:flowsignal.contact@gmail.com"
    assert "STRATEGY_STUDIO_OWNER_DEBUG" in caplog.text
    assert "never-log-secret" not in caplog.text
    assert '"strategy_count": 14' in caplog.text


def test_create_clone_reload_and_customer_isolation(monkeypatch):
    import db
    from test_strategy_studio_service import session_factory, valid_definition
    from services import strategy_studio_service as service
    from services import strategy_studio_live_state as live

    engine, sessions = session_factory()
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(live, "SessionLocal", sessions)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr(db, "engine", engine)

    def legacy(request, **kwargs):
        if request.headers.get("authorization") in {
            "Bearer first-admin",
            "Bearer restored-admin",
        }:
            return {"role": "admin", "email": "flowsignal.contact@gmail.com"}

    monkeypatch.setattr(studio, "_legacy_actor", legacy)
    monkeypatch.setattr(
        studio,
        "current_user",
        lambda r: SimpleNamespace(
            id=r.headers.get("x-test-user", "customer"),
            email="flowsignal.contact@gmail.com",
            role="user",
        ),
    )
    app = FastAPI()
    app.include_router(studio.router)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer first-admin"}
        created = client.post(
            "/strategy-studio/strategies",
            headers=headers,
            json={"name": "Gold 931", "definition": valid_definition()},
        )
        assert created.status_code == 201
        sid = created.json()["strategy"]["strategy_id"]
        cloned = client.post(
            f"/strategy-studio/strategies/{sid}/clone",
            headers=headers,
            json={"name": "Gold clone"},
        )
        assert cloned.status_code == 201
        clone_id = cloned.json()["strategy"]["strategy_id"]
        assert (
            client.post(
                f"/strategy-studio/strategies/{sid}/activate",
                headers=headers,
                json={"confirm": True},
            ).status_code
            == 200
        )
    # A new client / alternate valid owner session must see the same durable rows.
    with TestClient(app) as client:
        rows = client.get(
            "/strategy-studio/strategies",
            headers={"Authorization": "Bearer restored-admin"},
        ).json()["strategies"]
        assert {r["strategy_id"] for r in rows} == {sid, clone_id}
        assert next(r for r in rows if r["strategy_id"] == sid)["state"] == "ACTIVE"
        for user in ["customer", "test-account"]:
            assert (
                client.get(
                    "/strategy-studio/strategies", headers={"x-test-user": user}
                ).json()["strategies"]
                == []
            )
            assert (
                client.get(
                    f"/strategy-studio/strategies/{sid}", headers={"x-test-user": user}
                ).status_code
                == 404
            )
    engine.dispose()


def test_diagnostic_failure_does_not_replace_successful_library(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(
        studio,
        "_legacy_actor",
        lambda *a, **kw: {"role": "admin", "email": "flowsignal.contact@gmail.com"},
    )
    monkeypatch.setattr(studio, "list_strategies", lambda owner: [{"name": "Gold 931"}])

    def unavailable(owner):
        raise RuntimeError("private database detail must not be logged")

    monkeypatch.setattr(studio, "owner_debug_snapshot", unavailable)
    app = FastAPI()
    app.include_router(studio.router)
    with caplog.at_level(logging.INFO):
        response = TestClient(app).get(
            "/strategy-studio/strategies", headers={"Authorization": "Bearer owner"}
        )
    assert response.status_code == 200
    assert response.json()["strategies"] == [{"name": "Gold 931"}]
    assert '"snapshot_available": false' in caplog.text
    assert "private database detail" not in caplog.text
