"""Last human forfeit ends the game: opposing alliance wins instead of AI vs AI."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player
from backend.api.database import Base, get_db
from backend.api.main import games as games_cache
from backend.api.main import app
from backend.api.models import Game, Player
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.state import GameState
from backend.engine.utils import initialize_game_state

SETUP_ID = "wotr_exp_1.0"


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
def make_client(db_session, players):
    user, other = players
    current = {"player": user}

    def override_get_db():
        yield db_session

    def override_current_player():
        return current["player"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player

    def _make(as_player):
        current["player"] = as_player
        return TestClient(app)

    try:
        yield _make
    finally:
        app.dependency_overrides.clear()
        games_cache.clear()


def _fresh_state():
    ud, td, fd, cd, _pd = load_static_definitions(setup_id=SETUP_ID)
    start = load_starting_setup(setup_id=SETUP_ID)
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=start,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 6, "evil": 6}},
    )
    return state


def _add_game(
    session,
    *,
    name="Last forfeit",
    status="active",
    players_list,
    state=None,
    config=None,
    created_by=None,
):
    if state is None:
        state = _fresh_state()
    game = Game(
        id=str(uuid.uuid4()),
        name=name,
        status=status,
        created_by=created_by or players_list[0]["player_id"],
        game_state=json.dumps(state.to_dict()),
        players=json.dumps(players_list),
        config=json.dumps(config or {}),
    )
    session.add(game)
    session.commit()
    return game


def _forfeit(client, game_id, assignments):
    return client.post(
        f"/games/{game_id}/forfeit",
        json={"faction_assignments": assignments},
    )


def test_sole_human_forfeit_ends_game(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        config={"ai_factions": ["mordor", "isengard", "rohan", "elves", "erebor", "rhun", "harad"]},
    )
    r = _forfeit(client, game.id, {"gondor": "computer"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("game_over") is True
    assert body.get("winner") == "evil"

    db_session.refresh(game)
    assert game.status == "finished"
    assert game.completed_at is not None
    assert json.loads(game.players) == []
    state = json.loads(game.game_state)
    assert state["winner"] == "evil"
    config = json.loads(game.config)
    ai = set(config.get("ai_factions") or [])
    assert "gondor" in ai
    assert user.id in (config.get("forfeited_player_ids") or [])
    log = config.get("event_log") or []
    victory_events = [e for e in log if e.get("type") == "victory"]
    assert victory_events, "expected a victory event in the log"
    assert victory_events[-1]["payload"]["winner"] == "evil"


def test_last_evil_human_forfeit_good_wins(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        players_list=[{"player_id": user.id, "faction_id": "mordor"}],
        config={"ai_factions": ["gondor", "rohan", "elves", "erebor", "isengard", "rhun", "harad"]},
    )
    r = _forfeit(client, game.id, {"mordor": "computer"})
    assert r.status_code == 200
    assert r.json().get("winner") == "good"
    db_session.refresh(game)
    assert game.status == "finished"
    state = json.loads(game.game_state)
    assert state["winner"] == "good"


def test_two_humans_one_forfeit_stays_active(make_client, players, db_session):
    user, other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": user.id, "faction_id": "gondor"},
            {"player_id": other.id, "faction_id": "rohan"},
        ],
        config={"ai_factions": ["mordor", "isengard", "elves", "erebor", "rhun", "harad"]},
    )
    r = _forfeit(client, game.id, {"gondor": "computer"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("game_over") is not True
    assert "winner" not in body or body.get("winner") is None

    db_session.refresh(game)
    assert game.status == "active"
    state = json.loads(game.game_state)
    assert state["winner"] is None
    remaining = json.loads(game.players)
    assert any(str(p.get("player_id")) == other.id for p in remaining)
    assert not any(str(p.get("player_id")) == user.id for p in remaining)
    config = json.loads(game.config)
    assert "gondor" in set(config.get("ai_factions") or [])


def test_forfeit_options_ends_game_flag_sole_human(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        config={"ai_factions": ["mordor"]},
    )
    r = client.get(f"/games/{game.id}/forfeit-options")
    assert r.status_code == 200
    body = r.json()
    assert body["forfeit_ends_game"] is True
    assert body["factions"][0]["alliance"] == "good"


def test_forfeit_options_ends_game_flag_false_with_second_human(make_client, players, db_session):
    user, other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": user.id, "faction_id": "gondor"},
            {"player_id": other.id, "faction_id": "rohan"},
        ],
        config={"ai_factions": ["mordor"]},
    )
    r = client.get(f"/games/{game.id}/forfeit-options")
    assert r.status_code == 200
    assert r.json()["forfeit_ends_game"] is False


def test_forfeit_options_ends_game_flag_false_in_lobby(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    game = _add_game(
        db_session,
        status="lobby",
        players_list=[{"player_id": user.id, "faction_id": None}],
        config={"lobby_claims": {"gondor": user.id}},
    )
    r = client.get(f"/games/{game.id}/forfeit-options")
    assert r.status_code == 200
    assert r.json()["forfeit_ends_game"] is False


def test_forfeit_options_ends_game_flag_false_when_finished(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    state = _fresh_state()
    state.winner = "good"
    game = _add_game(
        db_session,
        status="finished",
        state=state,
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        config={"ai_factions": ["mordor"]},
    )
    r = client.get(f"/games/{game.id}/forfeit-options")
    assert r.status_code == 200
    assert r.json()["forfeit_ends_game"] is False


def test_forfeit_on_finished_game_keeps_existing_winner(make_client, players, db_session):
    user, _other = players
    client = make_client(user)
    state = _fresh_state()
    state.winner = "good"
    game = _add_game(
        db_session,
        status="finished",
        state=state,
        players_list=[{"player_id": user.id, "faction_id": "gondor"}],
        config={"ai_factions": ["mordor"]},
    )
    r = _forfeit(client, game.id, {"gondor": "computer"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("game_over") is not True

    db_session.refresh(game)
    assert game.status == "finished"
    state_after = json.loads(game.game_state)
    assert state_after["winner"] == "good"
    config = json.loads(game.config)
    log = config.get("event_log") or []
    assert not [e for e in log if e.get("type") == "victory"]
