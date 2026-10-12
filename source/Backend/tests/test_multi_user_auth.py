import os
import time

from sqlalchemy import create_engine, select, update

from services.user_auth_service import (
    authenticate,
    change_password,
    create_session,
    hash_password,
    session_snapshot,
    signup,
    verify_password,
    sessions,
    users,
)


def memory_engine():
    return create_engine("sqlite:///:memory:")


def approve_user(engine, user_id):
    """Disposable fixture setup for a verified, approved customer (not admin)."""
    with engine.begin() as connection:
        connection.execute(
            update(users).where(users.c.id == user_id).values(
                email_verified=True, approval_status="APPROVED", is_active=True,
            )
        )


def test_password_hash_is_salted_and_pbkdf2():
    first = hash_password("StrongPassword123!")
    second = hash_password("StrongPassword123!")
    assert first.startswith("pbkdf2_sha256$600000$")
    assert second.startswith("pbkdf2_sha256$600000$")
    assert first != second
    assert verify_password("StrongPassword123!", first)
    assert not verify_password("wrong-password", first)


def test_signup_login_and_duplicate_email():
    engine = memory_engine()
    user = signup("User@Example.com", "StrongPassword123!", engine=engine)
    assert user["email"] == "user@example.com"
    authenticated = authenticate("user@example.com", "StrongPassword123!", engine=engine)
    assert authenticated["id"] == user["id"]
    try:
        signup("USER@example.com", "AnotherPassword123!", engine=engine)
        assert False, "duplicate email should fail"
    except RuntimeError as exc:
        assert str(exc) == "EMAIL_ALREADY_REGISTERED"


def test_sessions_are_opaque_and_bound_to_user():
    engine = memory_engine()
    user = signup("a@example.com", "StrongPassword123!", "A User", engine=engine)
    approve_user(engine, user["id"])
    token, csrf, expires = create_session(user["id"], engine=engine)
    assert user["id"] not in token
    snapshot = session_snapshot(token, engine=engine)
    assert snapshot is not None
    assert snapshot[0].id == user["id"]
    assert snapshot[0].role == "user"
    assert snapshot[1] == csrf
    assert expires > time.time()


def test_change_password_keeps_current_session_and_revokes_others():
    engine = memory_engine()
    user = signup("password@example.com", "StrongPassword123!", engine=engine)
    approve_user(engine, user["id"])
    current, _csrf, _ = create_session(user["id"], engine=engine)
    other, _csrf2, _ = create_session(user["id"], engine=engine)
    assert session_snapshot(current, engine=engine)[0].role == "user"
    assert session_snapshot(other, engine=engine) is not None
    result = change_password(user["id"], "StrongPassword123!", "NewStrongPassword456!", current, engine=engine)
    assert result["other_sessions_revoked"] is True
    assert session_snapshot(current, engine=engine) is not None
    assert session_snapshot(other, engine=engine) is None
    try:
        authenticate("password@example.com", "StrongPassword123!", engine=engine)
        assert False, "old password must be rejected"
    except RuntimeError as exc:
        assert str(exc) == "INVALID_EMAIL_OR_PASSWORD"
    assert authenticate("password@example.com", "NewStrongPassword456!", engine=engine)["id"] == user["id"]
