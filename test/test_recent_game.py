"""Most recently opened game, recorded when a member loads GET /games/{id}."""

import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player, get_current_player_optional
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.main import games as games_cache
from backend.api.models import Game, Player


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
    user = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash="x",
        is_admin=False,
    )
    other = Player(
        id=str(uuid.uuid4()),
        email="other@example.com",
        username="otheruser",
        password_hash="x",
        is_admin=False,
    )
    db_session.add_all([user, other])
    db_session.commit()
    return user, other


@pytest.fixture()
def client(db_session, players):
    user, _other = players

    def override_get_db():
        yield db_session

    def override_player():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_player
    app.dependency_overrides[get_current_player_optional] = override_player
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        games_cache.clear()


def _add_game(session, *, name, status, players_list, created_by, config=None):
    game = Game(
        id=str(uuid.uuid4()),
        name=name,
        status=status,
        created_by=created_by,
        game_state=json.dumps({}),
        players=json.dumps(players_list),
        config=json.dumps(config) if config is not None else None,
    )
    session.add(game)
    session.commit()
    return game


def test_opening_a_game_sets_recent_and_repeat_loads_do_not_rewrite(client, db_session, players):
    user, _other = players
    game = _add_game(
        db_session,
        name="The Ring Goes South",
        status="active",
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        created_by=user.id,
    )
    r = client.get(f"/games/{game.id}")
    assert r.status_code == 200
    db_session.refresh(user)
    assert user.last_opened_game_id == game.id
    assert user.last_opened_at is not None

    stamped = datetime(2020, 1, 1, 0, 0, 0)
    user.last_opened_at = stamped
    db_session.commit()

    r = client.get(f"/games/{game.id}")
    assert r.status_code == 200
    db_session.refresh(user)
    assert user.last_opened_at == stamped

    recent = client.get("/games/recent")
    assert recent.status_code == 200
    body = recent.json()["game"]
    assert body["id"] == game.id
    assert body["name"] == "The Ring Goes South"


def test_opening_another_game_replaces_recent(client, db_session, players):
    user, _other = players
    first = _add_game(
        db_session,
        name="First",
        status="active",
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        created_by=user.id,
    )
    second = _add_game(
        db_session,
        name="Second",
        status="lobby",
        players_list=[{"player_id": user.id, "faction_id": None}],
        created_by=user.id,
    )
    assert client.get(f"/games/{first.id}").status_code == 200
    assert client.get(f"/games/{second.id}").status_code == 200
    db_session.refresh(user)
    assert user.last_opened_game_id == second.id
    assert client.get("/games/recent").json()["game"]["name"] == "Second"


def test_recent_ignores_games_the_player_cannot_load(client, db_session, players):
    user, other = players
    theirs = _add_game(
        db_session,
        name="Not Mine",
        status="active",
        players_list=[{"player_id": other.id, "faction_id": "mordor"}],
        created_by=other.id,
    )
    finished = _add_game(
        db_session,
        name="Already Over",
        status="finished",
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        created_by=user.id,
    )
    forfeited = _add_game(
        db_session,
        name="Left It",
        status="active",
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        created_by=user.id,
        config={"forfeited_player_ids": [user.id]},
    )
    assert client.get("/games/recent").json()["game"] is None
    for game in (theirs, finished, forfeited):
        assert client.get(f"/games/{game.id}").status_code == 200
    db_session.refresh(user)
    assert user.last_opened_game_id is None

    mine = _add_game(
        db_session,
        name="Still Playing",
        status="active",
        players_list=[{"player_id": user.id, "faction_id": "rohan"}],
        created_by=user.id,
    )
    assert client.get(f"/games/{mine.id}").status_code == 200
    mine.status = "finished"
    db_session.commit()
    assert client.get("/games/recent").json()["game"] is None
