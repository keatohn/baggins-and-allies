"""Optional Specials: flag plumbing (serialization, create-game strip) and engine effects when off."""

import json
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Game, Player
from backend.catalog import load_catalog, specials_for_units
from backend.engine.combat_specials import compute_battle_specials_and_modifiers, stacks_to_synthetic_units
from backend.engine.definitions import load_static_definitions
from backend.engine.state import GameState
from backend.engine.utils import has_unit_special, initialize_game_state


def _empty_state(**overrides) -> GameState:
    return GameState(
        turn_number=1,
        current_faction="gondor",
        phase="purchase",
        territories={},
        faction_resources={},
        **overrides,
    )


def test_default_on_and_missing_key_loads_clean():
    assert _empty_state().specials_enabled is True
    raw = _empty_state().to_dict()
    assert raw["specials_enabled"] is True
    raw.pop("specials_enabled")
    loaded = GameState.from_dict(raw)
    assert loaded.specials_enabled is True


def test_round_trip_preserves_flag():
    off = _empty_state(specials_enabled=False)
    assert off.to_dict()["specials_enabled"] is False
    loaded = GameState.from_dict(json.loads(json.dumps(off.to_dict())))
    assert loaded.specials_enabled is False


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


def _strip_unit_defs(ud, catalog):
    special_ids = {e["id"] for e in catalog.get("specials") or [] if isinstance(e, dict) and e.get("id")}
    return {
        uid: replace(
            u,
            specials=[],
            tags=[t for t in (u.tags or []) if t not in special_ids],
        )
        for uid, u in ud.items()
    }


def test_create_game_off_strips_specials(client, db_session):
    created = client.post(
        "/games/create",
        json={"name": "No Specials", "setup_id": "wotr_exp_1.0", "specials_enabled": False},
    )
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["specials_enabled"] is False

    row = db_session.query(Game).filter(Game.id == game_id).first()
    stored = json.loads(row.game_state)
    assert stored["specials_enabled"] is False

    got = client.get(f"/games/{game_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["state"]["specials_enabled"] is False
    assert body["definitions"]["specials"] == {}
    assert body["definitions"]["specials_order"] == []
    for unit in (body["definitions"].get("units") or {}).values():
        assert unit.get("specials") == []


def test_create_game_defaults_specials_on(client, db_session):
    created = client.post("/games/create", json={"name": "With Specials", "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["specials_enabled"] is True

    got = client.get(f"/games/{game_id}")
    body = got.json()
    assert body["state"]["specials_enabled"] is True
    units = body["definitions"]["units"]
    assert "archer" in (units.get("gondor_archer", {}).get("specials") or [])
    assert body["definitions"]["specials"] != {}


def test_initialize_game_state_specials_flag_defaults_on():
    ud, td, fd, cd, pd = load_static_definitions(setup_id="wotr_exp_1.0")
    state = initialize_game_state(fd, td, ud, camp_defs=cd)
    assert state.specials_enabled is True
    off = initialize_game_state(fd, td, ud, camp_defs=cd, specials_enabled=False)
    assert off.specials_enabled is False


def test_stripped_defs_disable_has_unit_special_and_engine_paths():
    ud, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    assert has_unit_special(ud.get("gondor_archer"), "archer") is True

    stripped = _strip_unit_defs(ud, load_catalog(None))
    assert has_unit_special(stripped.get("gondor_archer"), "archer") is False
    assert has_unit_special(stripped.get("ithilien_ranger"), "stealth") is False
    assert has_unit_special(stripped.get("ithilien_ranger"), "home") is False
    # ford_crosser / archer prefire / home deploy all gate on has_unit_special
    ford_ids = [uid for uid, u in stripped.items() if has_unit_special(u, "ford_crosser")]
    assert ford_ids == []
    archer_ids = [uid for uid, u in stripped.items() if has_unit_special(u, "archer")]
    assert archer_ids == []
    # Non-catalog tags (unit-type traits) remain
    assert "climbs_ladder" in (stripped.get("berserker").tags or [])
    assert "bomb" in (stripped.get("bomb").tags or [])


def test_stripped_defs_battle_specials_empty():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    stripped = _strip_unit_defs(ud, load_catalog(None))
    archer_id = next((k for k in stripped if "archer" in k), None)
    soldier_id = next((k for k in stripped if "soldier" in k or "orc" in k), None)
    if not archer_id or not soldier_id:
        pytest.skip("setup lacks archer/soldier")
    att, defs_ = stacks_to_synthetic_units(
        [{"unit_id": archer_id, "count": 1}],
        [{"unit_id": soldier_id, "count": 2}],
        stripped,
    )
    territory_def = next(iter(td.values()))
    result = compute_battle_specials_and_modifiers(
        att, defs_, territory_def, stripped,
        is_sea_raid=False, archer_prefire_applicable=True,
        stealth_prefire_applicable=True, ram_applicable=True,
    )
    for spec in list(result.specials_attacker.values()) + list(result.specials_defender.values()):
        assert not any(bool(v) for v in spec.values()), spec
    assert result.stat_modifiers_attacker == {} or all(m == 0 for m in result.stat_modifiers_attacker.values())
    assert result.stat_modifiers_defender == {} or all(m == 0 for m in result.stat_modifiers_defender.values())


def test_stripped_defs_specials_for_units_empty():
    ud, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    stripped = _strip_unit_defs(ud, load_catalog(None))
    defs, order = specials_for_units(load_catalog(None), stripped.values())
    assert defs == {}
    assert order == []


def test_simulate_combat_off_game_has_empty_battle_context(client):
    created = client.post(
        "/games/create",
        json={"name": "Sim No Specials", "setup_id": "wotr_exp_1.0", "specials_enabled": False},
    )
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]

    sim = client.post(
        "/simulate-combat",
        json={
            "game_id": game_id,
            "attacker_stacks": [{"unit_id": "gondor_archer", "count": 1}],
            "defender_stacks": [{"unit_id": "morannon_orc", "count": 2}],
            "territory_id": next(iter((client.get(f"/games/{game_id}").json()["definitions"]["territories"]).keys())),
            "n_trials": 2,
        },
    )
    assert sim.status_code == 200, sim.text
    bc = sim.json().get("battle_context")
    if bc is None:
        return
    specials_in_battle = bc.get("specials_in_battle") or {}
    assert specials_in_battle == {} or not any(
        any(isinstance(e, dict) and e for e in entries)
        for entries in specials_in_battle.values()
    )
