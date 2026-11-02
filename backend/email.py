"""
Outbound email via SMTP (stdlib only — smtplib + email.message).

Configured entirely by env vars so local dev needs no setup:
  SMTP_HOST     e.g. smtp.gmail.com
  SMTP_PORT     e.g. 587 (STARTTLS default)
  SMTP_USER     account login, e.g. the Gmail address
  SMTP_PASSWORD app password (Gmail App Password), never the account password
  SMTP_FROM     optional From header; defaults to SMTP_USER

When SMTP_HOST / SMTP_USER / SMTP_PASSWORD are missing, sends are skipped with a
warning so dev and tests never crash — callers treat a False return as "not sent".
"""

import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

logger = logging.getLogger("backend.email")

TEMP_PASSWORD_EMAIL_SUBJECT = "Baggins & Allies Temporary Password"


def smtp_configured() -> bool:
    return all(
        (os.environ.get(key) or "").strip()
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    )


def send_temp_password_email(
    to_email: str, username: str, temp_password: str, expires_at: datetime
) -> bool:
    """Email the one-time temp password and the exact UTC expiry time.

    Returns True when the message was handed to the SMTP server; False when SMTP
    is not configured or the send failed (logged, never raised).
    """
    if not smtp_configured():
        logger.warning(
            "SMTP not configured (set SMTP_HOST/SMTP_USER/SMTP_PASSWORD); "
            "skipping temporary-password email to %s",
            to_email,
        )
        return False

    host = os.environ["SMTP_HOST"].strip()
    port = int((os.environ.get("SMTP_PORT") or "587").strip() or "587")
    user = os.environ["SMTP_USER"].strip()
    password = os.environ["SMTP_PASSWORD"].strip()
    from_addr = (os.environ.get("SMTP_FROM") or "").strip() or user

    expires_label = expires_at.strftime("%H:%M UTC on %b %d, %Y")
    msg = EmailMessage()
    msg["Subject"] = TEMP_PASSWORD_EMAIL_SUBJECT
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content(
        f"""Hi {username},

Someone requested a temporary password for your Baggins & Allies account.

Your temporary password:
  {temp_password}

On the Log in page, click "Forgot password?" and enter your email, this
temporary password, and a new password. This temporary password can only be
used once.

This temporary password expires at {expires_label}.

If you did not request this, you can ignore this email — your password has
not changed.
"""
    )

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        logger.info("Temporary-password email sent to %s", to_email)
        return True
    except Exception as exc:  # noqa: BLE001 — any SMTP failure must not break the API response
        logger.warning("Failed to send temporary-password email to %s: %s", to_email, exc)
        return False
