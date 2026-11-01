"""Friends lifecycle: search, request, accept/decline, list, pending count."""

import json
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
from backend.api.models import Game, GameResult, Player


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
    buddy = Player(
        id=str(uuid.uuid4()),
        email="buddy@example.com",
        username="buddyone",
        password_hash=hash_password("buddy-pass"),
        is_admin=False,
    )
    db_session.add_all([admin, user, buddy])
    db_session.commit()
    return admin, user, buddy


@pytest.fixture()
def make_client(db_session, players):
    _admin, user, _buddy = players
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


def _send(client, username):
    return client.post("/friends/requests", json={"username": username})


def test_search_prefix_and_self_excluded(make_client, players):
    _admin, user, buddy = players
    client = make_client(user)
    r = client.get("/friends/search", params={"username": "buddy"})
    assert r.status_code == 200
    users = r.json()["users"]
    assert [u["username"] for u in users] == ["buddyone"]
    assert users[0]["id"] == buddy.id
    assert users[0]["relation"] == "none"

    r = client.get("/friends/search", params={"username": "norm"})
    assert [u["username"] for u in r.json()["users"]] == []  # self excluded

    r = client.get("/friends/search", params={"username": ""})
    assert r.json()["users"] == []


def test_played_with_active_and_finished_not_lobby(make_client, players, db_session):
    admin, user, buddy = players
    active = Game(
        id=str(uuid.uuid4()),
        name="Active",
        status="active",
        game_state="{}",
        players=json.dumps(
            [
                {"player_id": user.id, "faction_id": "gondor"},
                {"player_id": buddy.id, "faction_id": "rohan"},
                {"player_id": "__computer__", "faction_id": "mordor"},
            ]
        ),
    )
    lobby = Game(
        id=str(uuid.uuid4()),
        name="Lobby",
        status="lobby",
        game_state="{}",
        players=json.dumps(
            [
                {"player_id": user.id, "faction_id": "gondor"},
                {"player_id": admin.id, "faction_id": "isengard"},
            ]
        ),
    )
    db_session.add_all([active, lobby])
    db_session.commit()

    client = make_client(user)
    r = client.get("/friends/played-with")
    assert r.status_code == 200
    assert [u["username"] for u in r.json()["users"]] == ["buddyone"]
    assert r.json()["users"][0]["relation"] == "none"

    finished = Game(
        id=str(uuid.uuid4()),
        name="Finished",
        status="finished",
        game_state="{}",
        players="[]",
    )
    db_session.add(finished)
    db_session.flush()
    db_session.add(
        GameResult(
            id=str(uuid.uuid4()),
            game_id=finished.id,
            player_id=user.id,
            faction_id="gondor",
            alliance="good",
            won=True,
            participant_ids=json.dumps([user.id, admin.id, "__computer__"]),
        )
    )
    db_session.commit()
    r = client.get("/friends/played-with")
    assert [u["username"] for u in r.json()["users"]] == ["adminuser", "buddyone"]


def test_request_lifecycle_accept(make_client, players):
    _admin, user, buddy = players
    client = make_client(user)

    r = _send(client, "buddyone")
    assert r.status_code == 200
    req_id = r.json()["request"]["id"]

    # Duplicate while pending → 409
    r = _send(client, "buddyone")
    assert r.status_code == 409

    # Badge for the addressee
    buddy_client = make_client(buddy)
    r = buddy_client.get("/friends/requests/pending-count")
    assert r.json() == {"count": 1}

    r = buddy_client.get("/friends/requests")
    assert [x["username"] for x in r.json()["incoming"]] == ["normaluser"]
    assert r.json()["outgoing"] == []

    # Search shows relation for both sides (make_client switches the shared current player)
    r = buddy_client.get("/friends/search", params={"username": "normal"})
    assert r.json()["users"][0]["relation"] == "pending_in"
    client = make_client(user)
    r = client.get("/friends/search", params={"username": "buddy"})
    assert r.json()["users"][0]["relation"] == "pending_out"

    # Requester cannot accept
    r = client.post(f"/friends/requests/{req_id}/accept")
    assert r.status_code == 404

    buddy_client = make_client(buddy)
    r = buddy_client.post(f"/friends/requests/{req_id}/accept")
    assert r.status_code == 200

    r = buddy_client.get("/friends/requests/pending-count")
    assert r.json() == {"count": 0}

    for as_player in (user, buddy):
        c = make_client(as_player)
        r = c.get("/friends")
        expected = ["buddyone"] if as_player is user else ["normaluser"]
        assert [f["username"] for f in r.json()["friends"]] == expected

    # Now friends: search relation + re-request 409
    client = make_client(user)
    r = client.get("/friends/search", params={"username": "buddy"})
    assert r.json()["users"][0]["relation"] == "friend"
    r = _send(client, "buddyone")
    assert r.status_code == 409


def test_decline_removes_request(make_client, players):
    _admin, user, buddy = players
    client = make_client(user)
    r = _send(client, "buddyone")
    req_id = r.json()["request"]["id"]

    buddy_client = make_client(buddy)
    r = buddy_client.post(f"/friends/requests/{req_id}/decline")
    assert r.status_code == 200

    r = buddy_client.get("/friends/requests")
    assert r.json() == {"incoming": [], "outgoing": []}
    r = buddy_client.get("/friends")
    assert r.json() == {"friends": []}

    # Can re-request after decline
    client = make_client(user)
    r = _send(client, "buddyone")
    assert r.status_code == 200


def test_request_edge_cases(make_client, players):
    _admin, user, buddy = players
    client = make_client(user)

    r = _send(client, "normaluser")
    assert r.status_code == 400  # self

    r = _send(client, "ghost")
    assert r.status_code == 404

    # Accept by a third party → 404
    r = _send(client, "buddyone")
    req_id = r.json()["request"]["id"]
    admin_client = make_client(_admin)
    r = admin_client.post(f"/friends/requests/{req_id}/accept")
    assert r.status_code == 404
    r = admin_client.post(f"/friends/requests/{req_id}/decline")
    assert r.status_code == 404

    # Missing request id → 404
    buddy_client = make_client(buddy)
    r = buddy_client.post("/friends/requests/does-not-exist/accept")
    assert r.status_code == 404
