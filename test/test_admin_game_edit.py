"""Admin live-board editor: GET/PUT /admin/games/{id}/editor."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.main import game_defs
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
        games_cache.clear()
        game_defs.clear()


def _unit(instance_id, unit_id, *, remaining=2):
    return {
        "instance_id": instance_id,
        "unit_id": unit_id,
        "remaining_movement": 0,
        "remaining_health": remaining,
        "base_movement": 1,
        "base_health": 2,
    }


def _definitions():
    return {
        "units": {
            "infantry": {
                "id": "infantry",
                "display_name": "Infantry",
                "faction": "gondor",
                "archetype": "infantry",
                "tags": ["land"],
                "attack": 1,
                "defense": 2,
                "movement": 1,
                "health": 2,
                "cost": {"power": 3},
            }
        },
        "territories": {
            "minas_tirith": {
                "id": "minas_tirith",
                "display_name": "Minas Tirith",
                "terrain_type": "land",
                "adjacent": ["pelennor"],
                "produces": {"power": 3},
                "is_stronghold": True,
                "stronghold_base_health": 8,
            },
            "pelennor": {
                "id": "pelennor",
                "display_name": "Pelennor",
                "terrain_type": "land",
                "adjacent": ["minas_tirith", "osgiliath"],
                "produces": {"power": 2},
            },
            "osgiliath": {
                "id": "osgiliath",
                "display_name": "Osgiliath",
                "terrain_type": "land",
                "adjacent": ["pelennor"],
                "produces": {"power": 1},
            },
        },
        "factions": {
            "gondor": {
                "id": "gondor",
                "display_name": "Gondor",
                "alliance": "good",
                "capital": "minas_tirith",
                "color": "#888",
                "subfactions": [],
            }
        },
        "camps": {"osgiliath_camp": {"id": "osgiliath_camp", "territory_id": "osgiliath"}},
        "ports": {},
    }


def _state():
    return {
        "turn_number": 4,
        "phase": "mobilization",
        "current_faction": "gondor",
        "map_asset": "wotr_map",
        "territories": {
            "minas_tirith": {
                "owner": "gondor",
                "units": [_unit("inf_1", "infantry", remaining=1), _unit("inf_2", "infantry", remaining=2)],
                "stronghold_current_health": 5,
            },
            "pelennor": {"owner": None, "units": []},
            "osgiliath": {"owner": "gondor", "units": []},
        },
        "camps_standing": ["osgiliath_camp"],
        "mobilization_camps": ["osgiliath"],
        "pending_moves": [
            {
                "from_territory": "minas_tirith",
                "to_territory": "pelennor",
                "unit_instance_ids": ["inf_2"],
                "phase": "combat_move",
            }
        ],
    }


def _add_game(session):
    game = Game(
        id=str(uuid.uuid4()),
        name="Mid campaign",
        status="active",
        game_state=json.dumps(_state()),
        players=json.dumps([{"player_id": "p1", "faction_id": "gondor"}]),
        config=json.dumps({"definitions": _definitions(), "setup_id": "wotr"}),
    )
    session.add(game)
    session.commit()
    return game


def test_editor_requires_admin(make_client, players, db_session):
    _admin, user = players
    game = _add_game(db_session)
    client = make_client(user)
    assert client.get(f"/admin/games/{game.id}/editor").status_code == 403
    assert client.put(f"/admin/games/{game.id}/editor", json={"confirm": "EDIT GAME"}).status_code == 403


def test_editor_rejects_missing_confirmation(make_client, players, db_session):
    admin, _user = players
    game = _add_game(db_session)
    client = make_client(admin)
    loaded = client.get(f"/admin/games/{game.id}/editor")
    assert loaded.status_code == 200
    body = loaded.json()
    body["confirm"] = "edit game"
    saved = client.put(f"/admin/games/{game.id}/editor", json=body)
    assert saved.status_code == 400
    db_session.refresh(game)
    state = json.loads(game.game_state)
    assert len(state["territories"]["minas_tirith"]["units"]) == 2


def test_editor_writes_board_and_keeps_surviving_units(make_client, players, db_session):
    admin, _user = players
    game = _add_game(db_session)
    client = make_client(admin)
    loaded = client.get(f"/admin/games/{game.id}/editor")
    assert loaded.status_code == 200
    doc = loaded.json()
    assert doc["game"]["name"] == "Mid campaign"
    assert doc["turn_number"] == 4
    assert doc["starting_setup"]["territory_owners"]["minas_tirith"] == "gondor"
    assert doc["starting_setup"]["starting_units"]["minas_tirith"] == [
        {"unit_id": "infantry", "count": 2}
    ]
    assert doc["territories"]["minas_tirith"]["stronghold_current_health"] == 5
    assert "osgiliath_camp" in doc["camps"]
    assert doc["rings"] == []

    doc["confirm"] = "EDIT GAME"
    doc["starting_setup"]["territory_owners"]["pelennor"] = "gondor"
    doc["starting_setup"]["starting_units"] = {
        "minas_tirith": [{"unit_id": "infantry", "count": 1}],
        "osgiliath": [{"unit_id": "infantry", "count": 2}],
    }
    doc["territories"]["minas_tirith"]["stronghold_current_health"] = 3
    doc["territories"]["minas_tirith"]["produces"] = {"power": 9}
    doc["camps"] = {}
    doc["ports"] = {"pelennor_port": {"id": "pelennor_port", "territory_id": "pelennor"}}

    saved = client.put(
        f"/admin/games/{game.id}/editor",
        json={
            "confirm": doc["confirm"],
            "territories": doc["territories"],
            "starting_setup": doc["starting_setup"],
            "camps": doc["camps"],
            "ports": doc["ports"],
        },
    )
    assert saved.status_code == 200, saved.text

    db_session.refresh(game)
    state = json.loads(game.game_state)
    config = json.loads(game.config)
    tirith = state["territories"]["minas_tirith"]
    assert tirith["stronghold_current_health"] == 3
    assert [u["instance_id"] for u in tirith["units"]] == ["inf_1"]
    assert tirith["units"][0]["remaining_health"] == 1
    assert state["territories"]["pelennor"]["owner"] == "gondor"
    osgiliath_units = state["territories"]["osgiliath"]["units"]
    assert len(osgiliath_units) == 2
    assert {u["instance_id"] for u in osgiliath_units}.isdisjoint({"inf_1", "inf_2"})
    assert all(u["remaining_health"] == 2 and u["base_movement"] == 1 for u in osgiliath_units)
    assert state["pending_moves"] == []
    assert state["camps_standing"] == []
    assert "osgiliath" not in state["mobilization_camps"]
    defs = config["definitions"]
    assert defs["camps"] == {}
    assert defs["ports"]["pelennor_port"]["territory_id"] == "pelennor"
    assert defs["territories"]["minas_tirith"]["produces"] == {"power": 9}
    assert defs["territories"]["minas_tirith"]["stronghold_base_health"] == 8
    assert "stronghold_current_health" not in defs["territories"]["minas_tirith"]
    assert config["setup_id"] == "wotr"

    again = client.get(f"/admin/games/{game.id}/editor")
    assert again.status_code == 200
    board = again.json()
    assert board["starting_setup"]["starting_units"]["minas_tirith"] == [{"unit_id": "infantry", "count": 1}]
    assert board["territories"]["minas_tirith"]["stronghold_current_health"] == 3
    assert board["camps"] == {}
    assert "pelennor_port" in board["ports"]


def test_edit_units_requires_confirmation_and_setup_validation(make_client, players, db_session):
    admin, _user = players
    game = _add_game(db_session)
    client = make_client(admin)
    loaded = client.get(f"/admin/games/{game.id}/editor").json()
    units = loaded["units"]

    rejected = client.put(
        f"/admin/games/{game.id}/editor/units",
        json={"confirm": "edit game", "units": units},
    )
    assert rejected.status_code == 400

    broken = json.loads(json.dumps(units))
    del broken["infantry"]["display_name"]
    broken["infantry"]["archetype"] = "not-a-real-archetype"
    invalid = client.put(
        f"/admin/games/{game.id}/editor/units",
        json={"confirm": "EDIT GAME", "units": broken},
    )
    assert invalid.status_code == 400
    errors = invalid.json()["detail"]["validation_errors"]
    assert any("display_name" in message for message in errors)
    assert any("archetype" in message for message in errors)

    removed = {k: v for k, v in units.items() if k != "infantry"}
    still_there = client.put(
        f"/admin/games/{game.id}/editor/units",
        json={"confirm": "EDIT GAME", "units": removed},
    )
    assert still_there.status_code == 400
    assert any("still on the board" in message for message in still_there.json()["detail"]["validation_errors"])

    edited = json.loads(json.dumps(units))
    edited["infantry"]["attack"] = 9
    saved = client.put(
        f"/admin/games/{game.id}/editor/units",
        json={"confirm": "EDIT GAME", "units": edited},
    )
    assert saved.status_code == 200, saved.text
    db_session.refresh(game)
    state = json.loads(game.game_state)
    config = json.loads(game.config)
    assert config["definitions"]["units"]["infantry"]["attack"] == 9
    assert [u["instance_id"] for u in state["territories"]["minas_tirith"]["units"]] == ["inf_1", "inf_2"]
    again = client.get(f"/admin/games/{game.id}/editor")
    assert again.json()["units"]["infantry"]["attack"] == 9
