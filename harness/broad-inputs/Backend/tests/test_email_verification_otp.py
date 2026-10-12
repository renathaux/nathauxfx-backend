import time

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from services.user_auth_service import (
    email_verification_codes,
    issue_email_verification,
    session_snapshot,
    signup,
    verify_email_code,
)


def make_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )


def capture_sender(bucket):
    def _send(email, code):
        bucket.append((email, code))
    return _send


def test_signup_starts_unverified():
    engine = make_engine()
    user = signup("otp@example.com", "very-secure-password", engine=engine)
    assert user["email_verified"] is False


def test_issue_and_verify_code_marks_email_verified():
    engine = make_engine()
    sent = []
    signup("otp@example.com", "very-secure-password", engine=engine)
    result = issue_email_verification("otp@example.com", engine=engine, sender=capture_sender(sent))
    assert result["verification_required"] is True
    assert len(sent) == 1
    assert len(sent[0][1]) == 6
    verified = verify_email_code("otp@example.com", sent[0][1], engine=engine)
    assert verified["email_verified"] is True
    assert verified["approval_status"] == "PENDING_ADMIN"


def test_wrong_code_does_not_verify():
    engine = make_engine()
    sent = []
    signup("otp@example.com", "very-secure-password", engine=engine)
    issue_email_verification("otp@example.com", engine=engine, sender=capture_sender(sent))
    wrong = "000000" if sent[0][1] != "000000" else "000001"
    with pytest.raises(RuntimeError, match="INVALID_VERIFICATION_CODE"):
        verify_email_code("otp@example.com", wrong, engine=engine)


def test_expired_code_is_rejected():
    engine = make_engine()
    sent = []
    user = signup("otp@example.com", "very-secure-password", engine=engine)
    issue_email_verification("otp@example.com", engine=engine, sender=capture_sender(sent))
    with engine.begin() as connection:
        connection.execute(
            update(email_verification_codes)
            .where(email_verification_codes.c.user_id == user["id"])
            .values(expires_at=time.time() - 1)
        )
    with pytest.raises(RuntimeError, match="VERIFICATION_CODE_EXPIRED"):
        verify_email_code("otp@example.com", sent[0][1], engine=engine)


def test_immediate_resend_is_rate_limited():
    engine = make_engine()
    sent = []
    signup("otp@example.com", "very-secure-password", engine=engine)
    issue_email_verification("otp@example.com", engine=engine, sender=capture_sender(sent))
    with pytest.raises(RuntimeError, match="VERIFICATION_CODE_COOLDOWN"):
        issue_email_verification("otp@example.com", engine=engine, sender=capture_sender(sent))


def test_unverified_customer_session_snapshot_is_blocked():
    from services.user_auth_service import create_session

    engine = make_engine()
    user = signup("otp@example.com", "very-secure-password", engine=engine)
    token, _csrf, _expires = create_session(user["id"], engine=engine)
    assert session_snapshot(token, engine=engine) is None


def test_delivery_failure_consumes_generated_code():
    engine = make_engine()
    user = signup("otp@example.com", "very-secure-password", engine=engine)

    def fail_sender(_email, _code):
        raise RuntimeError("EMAIL_DELIVERY_FAILED")

    with pytest.raises(RuntimeError, match="EMAIL_DELIVERY_FAILED"):
        issue_email_verification("otp@example.com", engine=engine, sender=fail_sender)

    with engine.begin() as connection:
        row = connection.execute(
            email_verification_codes.select().where(email_verification_codes.c.user_id == user["id"])
        ).mappings().first()
    assert row is not None
    assert row["consumed_at"] is not None
