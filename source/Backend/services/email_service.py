from __future__ import annotations

import json
import os
import smtplib
from html import escape
import urllib.error
import urllib.request
from email.mime.text import MIMEText

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_GMAIL_FROM = "flowsignal.contact@gmail.com"


def _code_html(code: str, *, title: str, instruction: str) -> str:
    return (
        "<div style='font-family:Arial,sans-serif;background:#07111e;color:#eaf3ff;"
        "padding:28px;border-radius:16px'>"
        f"<h2 style='margin:0 0 12px'>{title}</h2>"
        f"<p style='color:#a9bbce'>{instruction}</p>"
        f"<div style='font-size:34px;font-weight:800;letter-spacing:8px;margin:24px 0'>{code}</div>"
        "<p style='color:#a9bbce'>This code expires in 10 minutes. If you did not request it, you can ignore this email.</p>"
        "</div>"
    )


def _send_with_resend(email: str, code: str, api_key: str, *, subject: str, html: str) -> None:
    sender = str(
        os.getenv("FLOWSIGNAL_EMAIL_FROM", "NathauxFX <onboarding@resend.dev>") or ""
    ).strip()
    if not sender:
        raise RuntimeError("EMAIL_FROM_NOT_CONFIGURED")

    payload = {
        "from": sender,
        "to": [str(email).strip()],
        "subject": subject,
        "html": html,
    }
    request = urllib.request.Request(
        RESEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "NathauxFX/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if int(getattr(response, "status", 200)) >= 300:
                raise RuntimeError("EMAIL_DELIVERY_FAILED")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("EMAIL_DELIVERY_FAILED") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("EMAIL_DELIVERY_FAILED") from exc


def _send_with_gmail(email: str, app_password: str, *, subject: str, html: str) -> None:
    sender = str(os.getenv("FEEDBACK_EMAIL", DEFAULT_GMAIL_FROM) or DEFAULT_GMAIL_FROM).strip()
    if not sender:
        raise RuntimeError("EMAIL_FROM_NOT_CONFIGURED")

    message = MIMEText(html, "html")
    message["Subject"] = subject
    message["From"] = f"NathauxFX <{sender}>"
    message["To"] = str(email).strip()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=12) as server:
            server.starttls()
            server.login(sender, app_password)
            server.send_message(message)
    except Exception as exc:
        raise RuntimeError("EMAIL_DELIVERY_FAILED") from exc


def _send_code_email(email: str, code: str, *, subject: str, title: str, instruction: str) -> None:
    html = _code_html(code, title=title, instruction=instruction)
    resend_key = str(os.getenv("RESEND_API_KEY", "") or "").strip()
    if resend_key:
        _send_with_resend(email, code, resend_key, subject=subject, html=html)
        return

    gmail_password = str(os.getenv("FEEDBACK_APP_PASSWORD", "") or "").strip()
    if gmail_password:
        _send_with_gmail(email, gmail_password, subject=subject, html=html)
        return

    raise RuntimeError("EMAIL_PROVIDER_NOT_CONFIGURED")


def send_verification_email(email: str, code: str) -> None:
    _send_code_email(
        email,
        code,
        subject="Your NathauxFX verification code",
        title="Verify your NathauxFX account",
        instruction="Enter this 6-digit code to finish creating your account.",
    )


def send_password_reset_email(email: str, code: str) -> None:
    _send_code_email(
        email,
        code,
        subject="Reset your NathauxFX password",
        title="Reset your NathauxFX password",
        instruction="Enter this 6-digit code to choose a new password.",
    )


def send_account_approved_email(email: str, full_name: str) -> None:
    name = escape(str(full_name or "there").strip())
    html = (
        "<div style='font-family:Arial,sans-serif;background:#07111e;color:#eaf3ff;"
        "padding:28px;border-radius:16px'>"
        "<h2 style='margin:0 0 12px'>Your NathauxFX account is approved</h2>"
        f"<p style='color:#a9bbce'>Hello {name}, your administrator has approved your access.</p>"
        "<p style='color:#a9bbce'>You can now sign in with the email address and password you registered.</p>"
        "</div>"
    )
    resend_key = str(os.getenv("RESEND_API_KEY", "") or "").strip()
    if resend_key:
        _send_with_resend(email, "", resend_key, subject="Your NathauxFX access is approved", html=html)
        return
    gmail_password = str(os.getenv("FEEDBACK_APP_PASSWORD", "") or "").strip()
    if gmail_password:
        _send_with_gmail(email, gmail_password, subject="Your NathauxFX access is approved", html=html)
        return
    raise RuntimeError("EMAIL_PROVIDER_NOT_CONFIGURED")
