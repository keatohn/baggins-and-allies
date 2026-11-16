"""M6 (issue #50): the sail-then-offload chain in POST /move must carry avoid_forced + ring payload.

The chain path (sea origin -> staging sea -> land) builds two pending moves by hand. It used to
drop ``avoid_forced_naval_combat`` and ``ring_id(s)`` on the floor: the sail leg never recorded
the avoid, and rings were never claimed or moved, stranding the ring in the origin sea while the
hero offloaded ashore. This drives the real API end to end:

* declaration: sail pending keeps avoid_forced, ring claim happens, ring_ids land on the offload
  pending (final territory), not the staging-sea sail;
* apply (end combat_move): hero+ring end in the target land with carried_in_by set and the boat
  ends in the staging sea. (The avoided-boat bookkeeping list is intentionally wiped when
  combat_move exits, so the avoid contract is asserted on the declared sail pending.)
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
from backend.api.main import app
from backend.api.models import Game, Player

SETUP_ID = "woteas_1.0"
ORIGIN_SEA = "sea_zone_5"
STAGING_SEA = "sea_zone_3"  # only sea adjacent to harlindon; adjacent to sea_zone_5
TARGET_LAND = "harlindon"
SHIP_ID = "noldor_lindon_ship_t1"
HERO_ID = "noldor_gil_galad_t1"
INTRUDER_ID = "sauron_intruder_t1"
RING_ID = "narya"


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
    db_session.add(admin)
    db_session.commit()
    return admin


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


def _state_row(db_session, game_id):
    return db_session.query(Game).filter(Game.id == game_id).first()


def _state_json(db_session, game_id):
    return json.loads(_state_row(db_session, game_id).game_state)


def _unit(iid, uid, movement=9, loaded_onto=None):
    unit = {
        "instance_id": iid,
        "unit_id": uid,
        "remaining_movement": movement,
        "base_movement": movement,
        "remaining_health": 1,
        "base_health": 1,
    }
    if loaded_onto:
        unit["loaded_onto"] = loaded_onto
    return unit


def _arrange_chain(db_session, game_id, player_id):
    """Combat move as noldor: ship+hero embarked in sea_zone_5, ring there, an enemy
    intruder sharing the zone (so the ship is forced to fight or flee), target
    harlindon reachable only via a sail to sea_zone_3 (chain required)."""
    row = _state_row(db_session, game_id)
    players_list = json.loads(row.players)
    for entry in players_list:
        if str(entry.get("player_id")) == str(player_id):
            entry["faction_id"] = "noldor"
    row.players = json.dumps(players_list)

    state = json.loads(row.game_state)
    state["current_faction"] = "noldor"
    state["phase"] = "combat_move"
    state["heroes_enabled"] = True
    state["rings_of_power"] = True
    state["rings"] = [
        {"id": RING_ID, "name": "Narya", "power": 3, "territory_id": ORIGIN_SEA}
    ]
    state["naval_mobilization_intruder_instance_ids"] = [INTRUDER_ID]
    state["avoided_forced_naval_combat_instance_ids"] = []
    state["loaded_naval_must_attack_instance_ids"] = []
    state["combat_move_naval_idle_sail_instance_ids"] = []
    territories = state["territories"]
    territories[ORIGIN_SEA]["units"] = [
        _unit(SHIP_ID, "lindon_ship"),
        _unit(HERO_ID, "gil_galad", loaded_onto=SHIP_ID),
        # Hostile intruder: only a different-alliance tagged boat creates the
        # standoff (numenor_ship is good like noldor, so it never would).
        _unit(INTRUDER_ID, "orc_warrior"),
    ]
    territories[STAGING_SEA]["units"] = []
    territories[TARGET_LAND]["units"] = []
    territories[TARGET_LAND]["owner"] = "noldor"
    row.game_state = json.dumps(state)
    db_session.commit()


def test_chain_move_carries_ring_and_avoid_forced_to_apply(client, db_session, players):
    created = client.post("/games/create", json={"name": "M6 chain", "setup_id": SETUP_ID})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    _arrange_chain(db_session, game_id, players.id)

    res = client.post(
        f"/games/{game_id}/move",
        json={
            "game_id": game_id,
            "from_territory": ORIGIN_SEA,
            "to_territory": TARGET_LAND,
            "unit_instance_ids": [SHIP_ID, HERO_ID],
            "avoid_forced_naval_combat": True,
            "ring_ids": [RING_ID],
        },
    )
    assert res.status_code == 200, res.text

    pendings = _state_json(db_session, game_id)["pending_moves"]
    sails = [m for m in pendings if m.get("move_type") == "sail"]
    offs = [m for m in pendings if m.get("move_type") == "offload"]
    assert len(sails) == 1, pendings
    assert len(offs) == 1, pendings
    assert sails[0]["from_territory"] == ORIGIN_SEA
    assert sails[0]["to_territory"] == STAGING_SEA
    # avoid_forced must survive onto the sail leg (chain used to drop it).
    assert sails[0].get("avoid_forced_naval_combat") is True, sails[0]
    # Ring claim ran on the sail declaration, then transferred to the offload pending
    # so the ring lands on the final territory (chain used to drop it entirely).
    assert not sails[0].get("ring_ids"), sails[0]
    assert offs[0]["from_territory"] == STAGING_SEA
    assert offs[0]["to_territory"] == TARGET_LAND
    assert offs[0].get("ring_ids") == [RING_ID], offs[0]
    claimed = next(r for r in _state_json(db_session, game_id)["rings"] if r["id"] == RING_ID)
    assert claimed.get("bearer_instance_id") == HERO_ID

    ended = client.post(f"/games/{game_id}/end-phase", json={"game_id": game_id})
    assert ended.status_code == 200, ended.text

    raw = _state_json(db_session, game_id)
    sea_units = {u["instance_id"] for u in raw["territories"][STAGING_SEA]["units"]}
    land_units = {u["instance_id"] for u in raw["territories"][TARGET_LAND]["units"]}
    assert SHIP_ID in sea_units, sea_units
    assert HERO_ID in land_units, land_units
    landed = next(r for r in raw["rings"] if r["id"] == RING_ID)
    assert landed["territory_id"] == TARGET_LAND, landed
    assert landed.get("carried_in_by") == HERO_ID, landed
    assert not landed.get("bearer_instance_id"), landed


def test_chain_move_boat_only_ids_still_carries_ring(client, db_session, players):
    """Boat-only payload (the documented drag-the-ship pattern): the hand-built offload
    pending must store expanded unit ids. apply_carried_ring reads pending_move.unit_instance_ids
    directly — an unexpanded list drops the hero bearer and leaves carried_in_by unset."""
    created = client.post("/games/create", json={"name": "M6 boat-only", "setup_id": SETUP_ID})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    _arrange_chain(db_session, game_id, players.id)

    res = client.post(
        f"/games/{game_id}/move",
        json={
            "game_id": game_id,
            "from_territory": ORIGIN_SEA,
            "to_territory": TARGET_LAND,
            "unit_instance_ids": [SHIP_ID],
            "avoid_forced_naval_combat": True,
            "ring_ids": [RING_ID],
        },
    )
    assert res.status_code == 200, res.text

    pendings = _state_json(db_session, game_id)["pending_moves"]
    offs = [m for m in pendings if m.get("move_type") == "offload"]
    assert len(offs) == 1, pendings
    # Expansion must land on the pending itself, not only inside apply's local list.
    assert set(offs[0]["unit_instance_ids"]) >= {SHIP_ID, HERO_ID}, offs[0]
    assert offs[0].get("ring_ids") == [RING_ID], offs[0]

    ended = client.post(f"/games/{game_id}/end-phase", json={"game_id": game_id})
    assert ended.status_code == 200, ended.text

    raw = _state_json(db_session, game_id)
    land_units = {u["instance_id"] for u in raw["territories"][TARGET_LAND]["units"]}
    assert HERO_ID in land_units, land_units
    landed = next(r for r in raw["rings"] if r["id"] == RING_ID)
    assert landed["territory_id"] == TARGET_LAND, landed
    assert landed.get("carried_in_by") == HERO_ID, landed
    assert not landed.get("bearer_instance_id"), landed
