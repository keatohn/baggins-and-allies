"""Admin Computer Only games: every faction is the computer, with its own knobs."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player
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


def test_non_admin_cannot_create_computer_only(make_client, players):
    _admin, user = players
    client = make_client(user)
    response = client.post("/games/create", json={"name": "Bots", "computer_only": True, "setup_id": "wotr_exp_1.0"})
    assert response.status_code == 403


def test_admin_computer_only_sets_weights_and_starts_with_no_human(make_client, players):
    admin, _user = players
    client = make_client(admin)
    created = client.post("/games/create", json={"name": "Bots", "computer_only": True, "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]

    meta = client.get(f"/games/{game_id}/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["computer_only"] is True
    assert body["game_code"] is None
    settings = body["ai_faction_settings"]
    assert settings
    faction_id = next(iter(settings))
    assert settings[faction_id]["difficulty"] == "normal"
    assert settings[faction_id]["algorithm"] == "heuristic"
    assert [level["name"] for level in body["ai_levels"]] == ["Easy", "Normal", "Hard"]
    assert settings[faction_id]["weights"]["material"] == 1.0

    settings[faction_id]["difficulty"] = "hard"
    settings[faction_id]["algorithm"] = "mcts"
    settings[faction_id]["weights"]["material"] = 2
    settings[faction_id]["weights"]["purchase_defense"] = 5
    saved = client.put(f"/games/{game_id}/ai-faction-settings", json={"factions": settings})
    assert saved.status_code == 200, saved.text
    saved_row = saved.json()["ai_faction_settings"][faction_id]
    assert saved_row["difficulty"] == "hard"
    assert saved_row["algorithm"] == "mcts"
    assert saved_row["weights"]["material"] == 2
    assert saved_row["weights"]["purchase_defense"] == 1.0

    started = client.post(f"/games/{game_id}/start")
    assert started.status_code == 200, started.text
    after = client.get(f"/games/{game_id}/meta").json()
    assert faction_id in after["ai_factions"]
    assert after["players"][0]["player_id"] == admin.id
    assert after["players"][0]["faction_id"] is None
    assert after["ai_faction_settings"][faction_id]["weights"]["material"] == 2


def test_admin_levels_drive_new_games_and_the_lobby_choice(make_client, players):
    admin, _user = players
    client = make_client(admin)
    saved = client.put("/admin/ai", json={
        "difficulty": "nightmare",
        "algorithm": "mcts",
        "levels": [
            {"id": "easy", "name": "Casual", "iterations": 40, "seconds": 0.1},
            {"id": "normal", "name": "Normal", "iterations": 500, "seconds": 0.5},
            {"id": "nightmare", "name": "Nightmare", "iterations": 3000, "seconds": 3},
        ],
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["algorithm"] == "mcts"
    assert body["difficulty"] == "nightmare"
    assert [level["name"] for level in body["levels"]] == ["Casual", "Normal", "Nightmare"]
    assert body["levels"][0]["id"] == "easy"
    assert body["levels"][2]["iterations"] == 3000

    created = client.post("/games/create", json={"name": "Solo", "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    meta = client.get(f"/games/{game_id}/meta").json()
    assert meta["ai_algorithm"] == "mcts"
    assert meta["ai_difficulty"] == "nightmare"
    assert [level["name"] for level in meta["ai_levels"]] == ["Casual", "Normal", "Nightmare"]

    chosen = client.put(f"/games/{game_id}/ai-difficulty", json={"difficulty": "easy"})
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["ai_difficulty"] == "easy"
    again = client.get(f"/games/{game_id}/meta").json()
    assert again["ai_difficulty"] == "easy"

    unknown = client.put(f"/games/{game_id}/ai-difficulty", json={"difficulty": "gone"})
    assert unknown.status_code == 200
    assert unknown.json()["ai_difficulty"] == "normal"

    empty = client.put("/admin/ai", json={"difficulty": "normal", "algorithm": "heuristic", "levels": []})
    assert empty.status_code == 400


def test_single_player_still_requires_a_human(make_client, players):
    admin, _user = players
    client = make_client(admin)
    created = client.post("/games/create", json={"name": "Solo", "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    started = client.post(f"/games/{created.json()['game_id']}/start")
    assert started.status_code == 400
