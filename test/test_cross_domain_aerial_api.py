"""L8 (issue #50): POST /games/{id}/move must let aerial units cross river↔sea.

The cross-domain guard fired unconditionally at the API, validation, and reducer
layers, rejecting every river↔sea move with "Ships cannot enter river zones, and
river units cannot enter sea zones". Aerial units may fly between water domains
(pathfinding already allowed it), so an all-aerial cross move must validate and
stage an ``aerial`` pending move; non-aerial stacks keep the original error.

Relocation itself is asserted at engine level (test_river_transport applies the
pending move). End of non_combat_move additionally requires aerials to rest in
friendly-owned territory (get_aerial_units_must_move — a pre-existing rule that
treats unownable water like neutral land), so these tests stop at staging.

woteas_1.0 ships the only shipped cross pair (gwathlo_south river ↔ sea_zone_9)
but no aerial units, so an aerial unit definition is injected into the game's
definition snapshot (same shape ``definitions_from_snapshot`` reads).
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app, game_defs
from backend.api.models import Game, Player

SETUP_ID = "woteas_1.0"
RIVER = "gwathlo_south"
SEA = "sea_zone_9"
EAGLE_IID = "eagle_api_1"
EAGLE_UID = "test_eagle"
LAND_IID = "warrior_api_1"
LAND_UID = "khazad_warrior"


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
    player = Player(
        id=str(uuid.uuid4()),
        email="l8@example.com",
        username="l8user",
        password_hash="x",
        is_admin=True,
    )
    db_session.add(player)
    db_session.commit()
    return player


@pytest.fixture()
def client(db_session, players):
    def override_get_db():
        yield db_session

    def override_current_player():
        return players

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _state_json(db_session, game_id):
    row = db_session.query(Game).filter(Game.id == game_id).first()
    return json.loads(row.game_state)


def _arrange(db_session, game_id, player_id, unit_iid, unit_uid, origin, destination):
    """Assign the player to noldor, inject the aerial unit def, and park a single
    mover of unit_uid in origin with a clear non_combat_move turn."""
    row = db_session.query(Game).filter(Game.id == game_id).first()
    players_list = json.loads(row.players)
    for entry in players_list:
        if str(entry.get("player_id")) == str(player_id):
            entry["faction_id"] = "noldor"
    row.players = json.dumps(players_list)

    config = json.loads(row.config)
    units_snap = config.setdefault("definitions", {}).setdefault("units", {})
    units_snap[EAGLE_UID] = {
        "id": EAGLE_UID,
        "display_name": "Test Eagle",
        "faction": "noldor",
        "archetype": "aerial",
        "tags": [],
        "attack": 1,
        "defense": 1,
        "movement": 2,
        "health": 1,
        "cost": {"power": 1},
    }
    row.config = json.dumps(config)
    game_defs.pop(game_id, None)

    state = json.loads(row.game_state)
    state["current_faction"] = "noldor"
    state["phase"] = "non_combat_move"
    territories = state["territories"]
    territories[origin]["units"] = [
        {
            "instance_id": unit_iid,
            "unit_id": unit_uid,
            "remaining_movement": 3,
            "base_movement": 3,
            "remaining_health": 1,
            "base_health": 1,
        }
    ]
    territories[destination]["units"] = []
    row.game_state = json.dumps(state)
    db_session.commit()


def _create(client, db_session, players, unit_iid, unit_uid, origin, destination):
    created = client.post("/games/create", json={"name": "L8 aerial cross", "setup_id": SETUP_ID})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    _arrange(db_session, game_id, players.id, unit_iid, unit_uid, origin, destination)
    return game_id


def _move(client, game_id, unit_iid, origin, destination):
    return client.post(
        f"/games/{game_id}/move",
        json={
            "game_id": game_id,
            "from_territory": origin,
            "to_territory": destination,
            "unit_instance_ids": [unit_iid],
        },
    )


def test_aerial_cross_river_to_sea_stages_aerial_pending(client, db_session, players):
    game_id = _create(client, db_session, players, EAGLE_IID, EAGLE_UID, RIVER, SEA)

    res = _move(client, game_id, EAGLE_IID, RIVER, SEA)
    assert res.status_code == 200, res.text

    pendings = _state_json(db_session, game_id)["pending_moves"]
    assert len(pendings) == 1, pendings
    assert pendings[0]["move_type"] == "aerial", pendings[0]
    assert pendings[0]["from_territory"] == RIVER
    assert pendings[0]["to_territory"] == SEA
    assert pendings[0]["unit_instance_ids"] == [EAGLE_IID]


def test_aerial_cross_sea_to_river_stages_aerial_pending(client, db_session, players):
    game_id = _create(client, db_session, players, EAGLE_IID, EAGLE_UID, SEA, RIVER)

    res = _move(client, game_id, EAGLE_IID, SEA, RIVER)
    assert res.status_code == 200, res.text

    pendings = _state_json(db_session, game_id)["pending_moves"]
    assert len(pendings) == 1, pendings
    assert pendings[0]["move_type"] == "aerial", pendings[0]
    assert pendings[0]["from_territory"] == SEA
    assert pendings[0]["to_territory"] == RIVER
    assert pendings[0]["unit_instance_ids"] == [EAGLE_IID]


def test_non_aerial_cross_is_rejected_with_original_error(client, db_session, players):
    game_id = _create(client, db_session, players, LAND_IID, LAND_UID, RIVER, SEA)

    res = _move(client, game_id, LAND_IID, RIVER, SEA)
    assert res.status_code == 400, res.text
    assert "river" in res.json().get("detail", "")

    raw = _state_json(db_session, game_id)
    assert not raw["pending_moves"], raw["pending_moves"]
