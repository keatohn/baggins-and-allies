"""Admin games report (GET /admin/games) and player names on GET /games."""

import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player
from backend.api.database import Base, get_db
from backend.api.main import games as games_cache
from backend.api.main import app, save_game
from backend.api.models import Game, Player
from backend.engine.state import GameState


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
        password_hash="x",
        is_admin=True,
    )
    user = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash="x",
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
            from fastapi import HTTPException

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


def _add_game(
    session,
    *,
    name,
    status,
    players_list,
    state=None,
    completed_at=None,
    created_by=None,
):
    game = Game(
        id=str(uuid.uuid4()),
        name=name,
        status=status,
        completed_at=completed_at,
        created_by=created_by,
        game_state=json.dumps(state or {}),
        players=json.dumps(players_list),
        config=None,
    )
    session.add(game)
    session.commit()
    return game


def test_admin_games_requires_admin(make_client, players):
    admin, user = players
    client = make_client(user)
    r = client.get("/admin/games")
    assert r.status_code == 403


def test_admin_games_lists_all_statuses_with_details(make_client, players, db_session):
    admin, user = players
    done_at = datetime(2026, 10, 1, 12, 0, 0)
    lobby = _add_game(
        db_session,
        name="Lobby game",
        status="lobby",
        players_list=[
            {"player_id": user.id, "faction_id": None},
            {"player_id": admin.id, "faction_id": None},
        ],
        state={"turn_order": ["good", "evil"], "turn_number": 1, "phase": "purchase"},
        created_by=user.id,
    )
    _add_game(
        db_session,
        name="Running game",
        status="active",
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        state={"turn_number": 3, "phase": "non_combat_move", "current_faction": "gondor"},
    )
    finished = _add_game(
        db_session,
        name="Done game",
        status="finished",
        players_list=[{"player_id": user.id, "faction_id": "mordor"}],
        state={"winner": "good", "turn_number": 9, "phase": "purchase"},
        completed_at=done_at,
    )

    client = make_client(admin)
    r = client.get("/admin/games")
    assert r.status_code == 200
    games = r.json()["games"]
    assert {g["status"] for g in games} == {"lobby", "active", "finished"}
    by_id = {g["id"]: g for g in games}

    assert by_id[lobby.id]["players"][0]["username"] == "normaluser"
    assert [p["username"] for p in by_id[lobby.id]["players"]] == ["normaluser", "adminuser"]
    assert by_id[lobby.id]["lobby_players"] == 2

    done = by_id[finished.id]
    assert done["winner"] == "good"
    assert done["completed_at"] is not None
    assert done["players"][0]["username"] == "normaluser"
    assert done["turn_number"] == 9


def test_list_games_excludes_finished_and_includes_player_names(make_client, players, db_session):
    admin, user = players
    _add_game(
        db_session,
        name="Lobby game",
        status="lobby",
        players_list=[{"player_id": user.id, "faction_id": None}],
        state={"turn_order": ["good", "evil"]},
    )
    _add_game(
        db_session,
        name="Running game",
        status="active",
        players_list=[
            {"player_id": user.id, "faction_id": "gondor"},
            {"player_id": admin.id, "faction_id": "mordor"},
        ],
        state={"turn_number": 2, "phase": "purchase", "current_faction": "gondor"},
    )
    _add_game(
        db_session,
        name="Done game",
        status="finished",
        players_list=[{"player_id": user.id, "faction_id": "mordor"}],
        state={"winner": "evil"},
        completed_at=datetime(2026, 9, 1),
    )

    client = make_client(user)
    r = client.get("/games")
    assert r.status_code == 200
    games = r.json()["games"]
    names = {g["name"] for g in games}
    assert names == {"Lobby game", "Running game"}
    for g in games:
        assert g["status"] in ("lobby", "active")
        usernames = [p["username"] for p in g["players"]]
        assert usernames
        assert all(u for u in usernames)

    running = next(g for g in games if g["name"] == "Running game")
    assert [p["username"] for p in running["players"]] == ["normaluser", "adminuser"]
    assert running["winner"] is None


def test_games_list_unique_players_ordered_by_turn_order(make_client, players, db_session):
    """A user controlling multiple factions appears once, ordered by turn order (not players-list order)."""
    admin, user = players
    game = _add_game(
        db_session,
        name="Multi-faction game",
        status="active",
        players_list=[
            {"player_id": user.id, "faction_id": "mordor"},
            {"player_id": admin.id, "faction_id": "gondor"},
            {"player_id": user.id, "faction_id": "isengard"},
        ],
        state={
            "turn_number": 2,
            "phase": "purchase",
            "current_faction": "gondor",
            "turn_order": ["gondor", "mordor", "isengard"],
        },
    )

    admin_client = make_client(admin)
    r = admin_client.get("/admin/games")
    assert r.status_code == 200
    admin_game = next(g for g in r.json()["games"] if g["id"] == game.id)
    admin_names = [p["username"] for p in admin_game["players"]]
    assert admin_names == ["adminuser", "normaluser"]  # gondor rank 0; user's earliest is mordor (rank 1)
    assert [p["player_id"] for p in admin_game["players"]] == [admin.id, user.id]

    user_client = make_client(user)
    r = user_client.get("/games")
    assert r.status_code == 200
    mine = next(g for g in r.json()["games"] if g["id"] == game.id)
    assert [p["username"] for p in mine["players"]] == ["adminuser", "normaluser"]


def test_save_game_stamps_completed_at(db_session):
    game = _add_game(
        db_session,
        name="Finishing game",
        status="active",
        players_list=[{"player_id": "p1", "faction_id": None}],
        state={},
    )
    try:
        state = GameState.from_dict({})
        state.winner = "good"
        save_game(game.id, state, db_session)
        db_session.refresh(game)
        assert game.status == "finished"
        assert game.completed_at is not None
    finally:
        games_cache.pop(game.id, None)
