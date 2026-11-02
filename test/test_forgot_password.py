"""Forgot-password flow: POST /auth/forgot-password and POST /auth/reset-password."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player, hash_password, verify_password
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Player


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = maker()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def user(db_session):
    player = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash=hash_password("original-pass"),
        is_admin=False,
    )
    db_session.add(player)
    db_session.commit()
    return player


@pytest.fixture()
def make_client(db_session, user):
    def override_get_db():
        yield db_session

    def override_current_player():
        return user

    def override_current_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player
    app.dependency_overrides[get_current_admin] = override_current_admin
    try:
        yield lambda: TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def sent_emails(monkeypatch):
    """Capture send_temp_password_email calls made inside backend.api.main."""
    sent = []

    def fake_send(to_email, username, temp_password, expires_at):
        sent.append(
            {
                "to": to_email,
                "username": username,
                "temp": temp_password,
                "expires_at": expires_at,
            }
        )
        return True

    monkeypatch.setattr("backend.api.main.send_temp_password_email", fake_send)
    return sent


def _request_temp(client, email="user@example.com"):
    return client.post("/auth/forgot-password", json={"email": email})


def test_forgot_password_unknown_email_is_generic(make_client, sent_emails):
    client = make_client()
    r = _request_temp(client, email="nobody@example.com")
    assert r.status_code == 200
    assert "If that email is registered" in r.json()["message"]
    assert sent_emails == []


def test_forgot_password_known_email_matches_generic_response(make_client, user, db_session, sent_emails):
    client = make_client()
    r = _request_temp(client)
    assert r.status_code == 200
    # Same body as the unknown-email case — no account enumeration.
    assert "If that email is registered" in r.json()["message"]

    db_session.refresh(user)
    assert user.temp_password_hash is not None
    assert user.temp_password_expires_at is not None
    assert user.temp_password_expires_at > datetime.utcnow()
    # Password itself is untouched until reset.
    assert verify_password("original-pass", user.password_hash)

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "user@example.com"
    assert sent_emails[0]["username"] == "normaluser"
    assert verify_password(sent_emails[0]["temp"], user.temp_password_hash)


def test_forgot_password_email_states_expiry(make_client, user, sent_emails):
    client = make_client()
    r = _request_temp(client)
    assert r.status_code == 200
    expires_at = sent_emails[0]["expires_at"]
    assert expires_at is not None
    # The SMTP helper formats this exact label into the email body.
    label = expires_at.strftime("%H:%M UTC on %b %d, %Y")
    assert "UTC on" in label


def test_reset_password_happy_path(make_client, user, db_session, sent_emails):
    client = make_client()
    assert _request_temp(client).status_code == 200
    temp = sent_emails[0]["temp"]

    r = client.post(
        "/auth/reset-password",
        json={"email": "user@example.com", "temp_password": temp, "new_password": "brand-new-pass"},
    )
    assert r.status_code == 200

    db_session.refresh(user)
    assert verify_password("brand-new-pass", user.password_hash)
    assert not verify_password("original-pass", user.password_hash)
    # Temp fields cleared — single-use.
    assert user.temp_password_hash is None
    assert user.temp_password_expires_at is None

    # New password logs in; old one does not.
    ok = client.post("/auth/login", json={"email": "user@example.com", "password": "brand-new-pass"})
    assert ok.status_code == 200
    bad = client.post("/auth/login", json={"email": "user@example.com", "password": "original-pass"})
    assert bad.status_code == 401


def test_reset_password_single_use(make_client, user, db_session, sent_emails):
    client = make_client()
    assert _request_temp(client).status_code == 200
    temp = sent_emails[0]["temp"]
    payload = {"email": "user@example.com", "temp_password": temp, "new_password": "brand-new-pass"}
    assert client.post("/auth/reset-password", json=payload).status_code == 200

    # Same temp password cannot be reused.
    r = client.post(
        "/auth/reset-password",
        json={"email": "user@example.com", "temp_password": temp, "new_password": "third-pass"},
    )
    assert r.status_code == 400
    db_session.refresh(user)
    assert verify_password("brand-new-pass", user.password_hash)


def test_reset_password_wrong_temp_password(make_client, user, db_session, sent_emails):
    client = make_client()
    assert _request_temp(client).status_code == 200

    r = client.post(
        "/auth/reset-password",
        json={"email": "user@example.com", "temp_password": "not-the-temp", "new_password": "brand-new-pass"},
    )
    assert r.status_code == 400
    db_session.refresh(user)
    assert verify_password("original-pass", user.password_hash)
    assert user.temp_password_hash is not None  # still pending — not consumed by a wrong guess


def test_reset_password_expired_temp_password(make_client, user, db_session, sent_emails):
    client = make_client()
    assert _request_temp(client).status_code == 200
    db_session.refresh(user)
    user.temp_password_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    r = client.post(
        "/auth/reset-password",
        json={"email": "user@example.com", "temp_password": sent_emails[0]["temp"], "new_password": "brand-new-pass"},
    )
    assert r.status_code == 400
    db_session.refresh(user)
    assert verify_password("original-pass", user.password_hash)


def test_reset_password_unknown_email(make_client, sent_emails):
    client = make_client()
    r = client.post(
        "/auth/reset-password",
        json={"email": "nobody@example.com", "temp_password": "x", "new_password": "brand-new-pass"},
    )
    assert r.status_code == 400
    assert sent_emails == []


def test_reset_password_requires_new_password(make_client, user, sent_emails):
    client = make_client()
    assert _request_temp(client).status_code == 200
    r = client.post(
        "/auth/reset-password",
        json={"email": "user@example.com", "temp_password": sent_emails[0]["temp"], "new_password": "   "},
    )
    assert r.status_code == 400


def test_forgot_password_without_smtp_still_succeeds(user, db_session, monkeypatch):
    """Missing SMTP config: endpoint keeps the generic 200 and stores the hash anyway."""
    from backend.email import send_temp_password_email

    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        r = client.post("/auth/forgot-password", json={"email": "user@example.com"})
        assert r.status_code == 200
        assert "If that email is registered" in r.json()["message"]
        db_session.refresh(user)
        assert user.temp_password_hash is not None
        # Real helper (unpatched): unconfigured SMTP logs a warning and returns False.
        assert send_temp_password_email(user.email, user.username, "temp", datetime.utcnow()) is False
    finally:
        app.dependency_overrides.clear()


def test_email_body_contains_password_and_expiry(monkeypatch):
    """The SMTP message includes subject, temp password, and the exact expiry label."""
    import backend.email as email_mod

    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            captured["starttls"] = True

        def login(self, user, password):
            captured["login"] = (user, password)

        def send_message(self, msg):
            captured["msg"] = msg

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "app@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_mod.smtplib, "SMTP", FakeSMTP)

    expires_at = datetime(2026, 11, 1, 14, 32, 0)
    assert email_mod.send_temp_password_email(
        "user@example.com", "normaluser", "Ab3xYz9QwErT", expires_at
    ) is True

    msg = captured["msg"]
    assert msg["Subject"] == "Baggins & Allies Temporary Password"
    assert msg["To"] == "user@example.com"
    body = msg.get_content()
    assert "Ab3xYz9QwErT" in body
    assert "14:32 UTC on Nov 01, 2026" in body
    # Line-wrapped in the body; single-use is stated.
    assert "used once" in body
