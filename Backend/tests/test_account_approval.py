from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from services.user_auth_service import (
    create_session,
    issue_email_verification,
    list_access_requests,
    review_access_request,
    session_snapshot,
    signup,
    verify_email_code,
)
from routes import admin_access


def make_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )


def verify(user_email, engine):
    sent = []
    issue_email_verification(
        user_email,
        engine=engine,
        sender=lambda email, code: sent.append((email, code)),
    )
    return verify_email_code(user_email, sent[0][1], engine=engine)


def test_verified_account_stays_locked_until_admin_approval():
    engine = make_engine()
    user = signup("person@example.com", "very-secure-password", "Test Person", engine=engine)
    verified = verify(user["email"], engine)
    token, _csrf, _expires = create_session(user["id"], engine=engine)

    assert verified["approval_status"] == "PENDING_ADMIN"
    assert session_snapshot(token, engine=engine) is None
    assert list_access_requests(engine=engine)[0]["full_name"] == "Test Person"


def test_approved_account_can_create_an_authenticated_session():
    engine = make_engine()
    user = signup("person@example.com", "very-secure-password", "Test Person", engine=engine)
    verify(user["email"], engine)
    reviewed = review_access_request(user["id"], "APPROVED", "admin@example.com", engine=engine)
    token, _csrf, _expires = create_session(user["id"], engine=engine)

    snapshot = session_snapshot(token, engine=engine)
    assert reviewed["approval_status"] == "APPROVED"
    assert snapshot is not None
    assert snapshot[0].email == "person@example.com"


def test_denied_account_remains_locked():
    engine = make_engine()
    user = signup("person@example.com", "very-secure-password", "Test Person", engine=engine)
    verify(user["email"], engine)
    review_access_request(user["id"], "DENIED", "admin@example.com", engine=engine)
    token, _csrf, _expires = create_session(user["id"], engine=engine)

    assert session_snapshot(token, engine=engine) is None
    assert list_access_requests(engine=engine)[0]["approval_status"] == "DENIED"


def test_denied_unverified_account_can_verify_and_be_reconsidered():
    engine = make_engine()
    user = signup("person@example.com", "very-secure-password", "Test Person", engine=engine)
    review_access_request(user["id"], "DENIED", "admin@example.com", engine=engine)

    verified = verify(user["email"], engine)
    assert verified["email_verified"] is True
    assert verified["approval_status"] == "DENIED"

    review_access_request(user["id"], "APPROVED", "admin@example.com", engine=engine)
    token, _csrf, _expires = create_session(user["id"], engine=engine)
    assert session_snapshot(token, engine=engine) is not None


def test_legacy_customer_signup_and_login_are_closed():
    source = (__import__("pathlib").Path(__file__).parents[1] / "api.py").read_text()
    signup_block = source[source.index('@app.post("/signup")'):source.index('@app.post("/login")')]
    login_block = source[source.index('@app.post("/login")'):source.index('@app.post("/session/access-code")')]

    assert "USE_AUTH_SIGNUP" in signup_block
    assert "save_users" not in signup_block
    assert 'role": role' not in login_block


def test_unverified_account_cannot_be_approved():
    engine = make_engine()
    user = signup("person@example.com", "very-secure-password", "Test Person", engine=engine)

    try:
        review_access_request(user["id"], "APPROVED", "admin@example.com", engine=engine)
        raised = False
    except RuntimeError as exc:
        raised = str(exc) == "EMAIL_VERIFICATION_REQUIRED"

    assert raised


def test_approval_sends_email_but_denial_does_not(monkeypatch):
    sent = []
    monkeypatch.setattr(admin_access, "_admin", lambda _request, mutation=False: {"email": "admin@example.com"})
    monkeypatch.setattr(
        admin_access,
        "review_access_request",
        lambda user_id, decision, reviewer: {
            "id": user_id,
            "full_name": "Test Person",
            "email": "person@example.com",
            "approval_status": decision,
        },
    )
    monkeypatch.setattr(admin_access, "send_account_approved_email", lambda email, name: sent.append((email, name)))

    denied = admin_access.decide("user-1", admin_access.AccessDecision(decision="DENIED"), object())
    approved = admin_access.decide("user-1", admin_access.AccessDecision(decision="APPROVED"), object())

    assert denied["email_sent"] is False
    assert sent == [("person@example.com", "Test Person")]
    assert approved["email_sent"] is True
