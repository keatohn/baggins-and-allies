"""Profile avatar + profile-defaults behavior (PATCH /auth/me, GET /profile/avatar-icons, admin defaults)."""

import uuid

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


def test_auth_me_returns_avatar_defaults(make_client, players):
    _admin, user = players
    client = make_client(user)
    r = client.get("/auth/me")
    assert r.status_code == 200
    avatar = r.json()["avatar"]
    assert avatar == {"image": "hobbit.png", "color": "#2d5a27"}


def test_register_includes_avatar_defaults(make_client, players):
    _admin, _user = players
    client = make_client(_user)
    r = client.post(
        "/auth/register",
        json={"email": "new@example.com", "username": "newuser", "password": "pass1234"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["player"]["avatar"] == {"image": "hobbit.png", "color": "#2d5a27"}


def test_patch_avatar_merges_and_returns(make_client, players, db_session):
    _admin, user = players
    client = make_client(user)
    r = client.patch("/auth/me", json={"avatar": {"image": "aragorn.png"}})
    assert r.status_code == 200
    assert r.json()["avatar"]["image"] == "aragorn.png"
    assert r.json()["avatar"]["color"] == "#2d5a27"

    r = client.patch("/auth/me", json={"avatar": {"color": "#1234Ab"}})
    assert r.status_code == 200
    assert r.json()["avatar"] == {"image": "aragorn.png", "color": "#1234ab"}

    db_session.refresh(user)
    stored = user.preferences
    assert "aragorn.png" in stored


def test_patch_avatar_rejects_bad_values(make_client, players):
    _admin, user = players
    client = make_client(user)
    r = client.patch("/auth/me", json={"avatar": {"color": "green"}})
    assert r.status_code == 400
    r = client.patch("/auth/me", json={"avatar": {"color": "#12345"}})
    assert r.status_code == 400
    r = client.patch("/auth/me", json={"avatar": {"image": "evil/../x.png"}})
    assert r.status_code == 400
    r = client.patch("/auth/me", json={"avatar": {"image": "not-a-png.jpg"}})
    assert r.status_code == 400
    r = client.patch("/auth/me", json={"avatar": {}})
    assert r.status_code == 400


def test_admin_profile_defaults_change_only_unpicked_users(make_client, players, db_session):
    admin, user = players
    client = make_client(user)
    # User picks a color before the default changes.
    r = client.patch("/auth/me", json={"avatar": {"color": "#111111"}})
    assert r.status_code == 200

    admin_client = make_client(admin)
    r = admin_client.get("/admin/profile-defaults")
    assert r.status_code == 200
    assert r.json()["default_avatar_color"] == "#2d5a27"

    r = admin_client.put("/admin/profile-defaults", json={"default_avatar_color": "#ABCDEF"})
    assert r.status_code == 200
    assert r.json()["default_avatar_color"] == "#abcdef"

    # User who picked keeps their color (make_client switches the shared current player).
    client = make_client(user)
    r = client.get("/auth/me")
    assert r.json()["avatar"]["color"] == "#111111"

    # Another user who never picked resolves to the new default.
    other = Player(
        id=str(uuid.uuid4()),
        email="other@example.com",
        username="otheruser",
        password_hash=hash_password("other-pass"),
        is_admin=False,
    )
    db_session.add(other)
    db_session.commit()
    other_client = make_client(other)
    r = other_client.get("/auth/me")
    assert r.json()["avatar"]["color"] == "#abcdef"


def test_profile_defaults_validation_and_authz(make_client, players):
    admin, user = players
    client = make_client(user)
    r = client.get("/admin/profile-defaults")
    assert r.status_code == 403
    r = client.put("/admin/profile-defaults", json={"default_avatar_color": "#ABCDEF"})
    assert r.status_code == 403

    admin_client = make_client(admin)
    r = admin_client.put("/admin/profile-defaults", json={"default_avatar_color": "blue"})
    assert r.status_code == 400
    r = admin_client.put("/admin/profile-defaults", json={"default_avatar_color": "#12345"})
    assert r.status_code == 400


def test_avatar_icons_endpoint_lists_hobbit(make_client, players):
    _admin, user = players
    client = make_client(user)
    r = client.get("/profile/avatar-icons")
    assert r.status_code == 200
    icons = r.json()["icons"]
    assert isinstance(icons, list)
    assert "hobbit.png" in icons
    assert icons == sorted(icons)
