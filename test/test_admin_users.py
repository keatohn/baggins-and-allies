"""Admin users report (GET /admin/users) and last_login stamping on POST /auth/login."""

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player, hash_password
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
def players(db_session):
    admin = Player(
        id=str(uuid.uuid4()),
        email="admin@example.com",
        username="adminuser",
        password_hash=hash_password("admin-pass"),
        is_admin=True,
    )
    user = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash=hash_password("user-pass"),
        is_admin=False,
    )
    db_session.add_all([admin, user])
    db_session.commit()
    return admin, user


@pytest.fixture()
def make_client(db_session, players):
    admin, user = players
    current = {"player": user}

    def override_get_db():
        yield db_session

    def override_current_player():
        return current["player"]

    def override_current_admin():
        if not getattr(current["player"], "is_admin", False):
            raise HTTPException(status_code=403, detail="Admin access required")
        return current["player"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player
    app.dependency_overrides[get_current_admin] = override_current_admin

    def _make(as_player):
        current["player"] = as_player
        return TestClient(app)

    try:
        yield _make
    finally:
        app.dependency_overrides.clear()
        current["player"] = user


def test_admin_users_requires_admin(make_client, players):
    _admin, user = players
    client = make_client(user)
    r = client.get("/admin/users")
    assert r.status_code == 403


def test_admin_users_shape_and_username_order(make_client, players, db_session):
    admin, user = players
    third = Player(
        id=str(uuid.uuid4()),
        email="aaa@example.com",
        username="aaa_first",
        password_hash="x",
        is_admin=False,
        last_login=datetime(2026, 10, 15, 8, 30, 0),
    )
    db_session.add(third)
    db_session.commit()

    client = make_client(admin)
    r = client.get("/admin/users")
    assert r.status_code == 200
    users = r.json()["users"]
    assert [u["username"] for u in users] == ["aaa_first", "adminuser", "normaluser"]
    by_name = {u["username"]: u for u in users}
    assert by_name["adminuser"]["email"] == "admin@example.com"
    assert by_name["adminuser"]["id"] == admin.id
    # Never logged in yet → null until first login.
    assert by_name["normaluser"]["last_login"] is None
    assert by_name["aaa_first"]["last_login"] is not None


def test_login_sets_last_login(make_client, players, db_session):
    _admin, user = players
    assert user.last_login is None

    client = make_client(user)
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "user-pass"})
    assert r.status_code == 200
    assert r.json()["access_token"]

    db_session.refresh(user)
    assert user.last_login is not None


def test_failed_login_does_not_set_last_login(make_client, players, db_session):
    _admin, user = players
    client = make_client(user)
    r = client.post("/auth/login", json={"email": "user@example.com", "password": "wrong"})
    assert r.status_code == 401

    db_session.refresh(user)
    assert user.last_login is None


def test_login_is_case_insensitive_and_stamps_last_login(make_client, players, db_session):
    _admin, user = players
    client = make_client(user)
    r = client.post("/auth/login", json={"email": "  User@Example.COM ", "password": "user-pass"})
    assert r.status_code == 200

    db_session.refresh(user)
    assert user.last_login is not None
