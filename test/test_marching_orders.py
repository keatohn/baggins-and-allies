"""Marching Orders: flag plumbing (serialization, catalog, create-game) and the engine rule."""

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
from backend.engine.actions import cancel_move, end_phase, move_units
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.queries import get_unit_move_targets, validate_action
from backend.engine.reducer import apply_action
from backend.engine.state import GameState
from backend.engine.utils import initialize_game_state


def _empty_state(**overrides) -> GameState:
    return GameState(
        turn_number=1,
        current_faction="gondor",
        phase="purchase",
        territories={},
        faction_resources={},
        **overrides,
    )


def test_off_is_the_default_and_missing_keys_load_clean():
    raw = _empty_state().to_dict()
    assert raw["marching_orders"] is False
    assert raw["combat_move_moved_ids"] == []
    assert raw["marching_orders_bonus_ids"] == []
    assert raw["conquered_this_turn"] == {}
    raw.pop("marching_orders")
    raw.pop("combat_move_moved_ids")
    raw.pop("marching_orders_bonus_ids")
    raw.pop("conquered_this_turn")
    loaded = GameState.from_dict(raw)
    assert loaded.marching_orders is False
    assert loaded.combat_move_moved_ids == []
    assert loaded.marching_orders_bonus_ids == []
    assert loaded.conquered_this_turn == {}


def test_round_trip_preserves_flag_and_snapshot():
    state = _empty_state(
        marching_orders=True,
        combat_move_moved_ids=["gondor_soldier_001", "gondor_archer_002"],
        marching_orders_bonus_ids=["gondor_soldier_003"],
        conquered_this_turn={"anorien": "gondor"},
    )
    loaded = GameState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert loaded.marching_orders is True
    assert loaded.combat_move_moved_ids == ["gondor_soldier_001", "gondor_archer_002"]
    assert loaded.marching_orders_bonus_ids == ["gondor_soldier_003"]
    assert loaded.conquered_this_turn == {"anorien": "gondor"}


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


def test_create_game_persists_marching_orders_flag(client, db_session):
    created = client.post(
        "/games/create",
        json={"name": "Orders", "setup_id": "wotr_exp_1.0", "marching_orders": True},
    )
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["marching_orders"] is True

    row = db_session.query(Game).filter(Game.id == game_id).first()
    stored = json.loads(row.game_state)
    assert stored["marching_orders"] is True
    assert stored["combat_move_moved_ids"] == []


def test_create_game_defaults_marching_orders_off(client, db_session):
    created = client.post("/games/create", json={"name": "No Orders", "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["marching_orders"] is False

    row = db_session.query(Game).filter(Game.id == game_id).first()
    stored = json.loads(row.game_state)
    assert stored["marching_orders"] is False


# --- Engine rule: +1 movement during non-combat move for combat-move non-movers ---


def _game(setup_id: str, marching_orders: bool, faction: str):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=setup_id
    )
    starting = load_starting_setup(setup_id=setup_id)
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
        marching_orders=marching_orders,
    )
    state.current_faction = faction
    state.phase = "combat_move"
    defs = (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    return state, defs


def _act(state, action, defs):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert validation.valid, validation.error
    new_state, _events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    return new_state


def _unit(state, instance_id):
    for territory in state.territories.values():
        for unit in territory.units:
            if unit.instance_id == instance_id:
                return unit
    return None


def _unit_in(state, territory_id: str, unit_id: str, index: int = 0):
    matches = [u for u in state.territories[territory_id].units if u.unit_id == unit_id]
    assert len(matches) > index, f"need {unit_id} #{index} in {territory_id}"
    return matches[index]


def _to_non_combat_move(state, faction: str, defs):
    state = _act(state, end_phase(faction), defs)  # combat_move -> combat
    state = _act(state, end_phase(faction), defs)  # combat -> non_combat_move
    return state


def _gondor_board(marching_orders: bool):
    """Ranger pair in north_ithilien (one moves, one holds) plus distant units.

    Returns instance ids and base movement up front: apply_action deep-copies the
    state, so live unit references must be re-fetched from the current state.
    """
    state, defs = _game("wotr_1.1", marching_orders, "gondor")
    picked = {
        "mover": _unit_in(state, "north_ithilien", "ithilien_ranger", 0),
        "holder": _unit_in(state, "north_ithilien", "ithilien_ranger", 1),
        "knight": _unit_in(state, "minas_tirith", "gondor_knight"),
        "enemy": _unit_in(state, "minas_morgul", "orc_warrior"),
    }
    ids = {name: unit.instance_id for name, unit in picked.items()}
    bases = {name: unit.base_movement for name, unit in picked.items()}
    return state, defs, ids, bases


def test_rule_off_grants_no_extra_movement():
    state, defs, ids, bases = _gondor_board(False)

    state = _act(
        state,
        move_units("gondor", "north_ithilien", "dagorlad", [ids["mover"]]),
        defs,
    )
    state = _act(state, end_phase("gondor"), defs)  # combat_move -> combat
    assert state.combat_move_moved_ids == []
    paid = _unit(state, ids["mover"]).remaining_movement
    assert paid < bases["mover"]

    state = _act(state, end_phase("gondor"), defs)  # combat -> non_combat_move
    assert state.combat_move_moved_ids == []
    assert _unit(state, ids["mover"]).remaining_movement == paid
    for name in ("holder", "knight", "enemy"):
        assert _unit(state, ids[name]).remaining_movement == bases[name], (
            f"{name} must keep base movement when the rule is off"
        )


def test_rule_on_grants_one_extra_movement_to_non_movers():
    state, defs, ids, bases = _gondor_board(True)

    state = _act(
        state,
        move_units("gondor", "north_ithilien", "dagorlad", [ids["mover"]]),
        defs,
    )
    state = _act(state, end_phase("gondor"), defs)  # combat_move -> combat
    assert state.combat_move_moved_ids == [ids["mover"]]
    paid = _unit(state, ids["mover"]).remaining_movement  # movement consumed by the move
    assert paid < bases["mover"]

    state = _act(state, end_phase("gondor"), defs)  # combat -> non_combat_move
    assert state.combat_move_moved_ids == [], "snapshot is consumed at NCM entry"
    assert _unit(state, ids["holder"]).remaining_movement == bases["holder"] + 1
    assert _unit(state, ids["knight"]).remaining_movement == bases["knight"] + 1
    assert _unit(state, ids["mover"]).remaining_movement == paid, "combat-move mover gets no bonus"
    assert _unit(state, ids["enemy"]).remaining_movement == bases["enemy"], "enemies never benefit"


def test_cancelled_combat_move_counts_as_a_non_mover():
    state, defs = _game("wotr_1.1", True, "gondor")
    ranger_id = _unit_in(state, "north_ithilien", "ithilien_ranger", 0).instance_id
    base = _unit(state, ranger_id).base_movement

    state = _act(state, move_units("gondor", "north_ithilien", "dagorlad", [ranger_id]), defs)
    state = _act(state, cancel_move("gondor", 0), defs)
    assert state.pending_moves == []

    state = _to_non_combat_move(state, "gondor", defs)
    assert _unit(state, ranger_id).remaining_movement == base + 1


def test_bonus_is_cleared_when_non_combat_move_ends():
    state, defs, ids, bases = _gondor_board(True)

    state = _to_non_combat_move(state, "gondor", defs)
    assert _unit(state, ids["holder"]).remaining_movement == bases["holder"] + 1

    state = _act(state, end_phase("gondor"), defs)  # non_combat_move -> mobilization
    assert state.phase == "mobilization"
    assert _unit(state, ids["holder"]).remaining_movement == bases["holder"], (
        "end-of-NCM reset wipes the bonus"
    )


def test_units_destroyed_before_the_grant_do_not_break_it():
    state, defs, ids, _bases = _gondor_board(True)
    archer_id = _unit_in(state, "minas_tirith", "gondor_archer").instance_id

    state = _act(
        state,
        move_units("gondor", "north_ithilien", "dagorlad", [ids["mover"]]),
        defs,
    )
    state = _act(state, end_phase("gondor"), defs)  # combat_move -> combat
    # Casualties mid-turn: the snapshot mover and a would-be beneficiary both die.
    for territory_id, dead_id in (("dagorlad", ids["mover"]), ("north_ithilien", ids["holder"])):
        state.territories[territory_id].units = [
            u for u in state.territories[territory_id].units if u.instance_id != dead_id
        ]

    state = _act(state, end_phase("gondor"), defs)  # combat -> non_combat_move
    assert _unit(state, ids["mover"]) is None
    assert _unit(state, ids["holder"]) is None
    archer = _unit(state, archer_id)
    assert archer.remaining_movement == archer.base_movement + 1
    assert state.combat_move_moved_ids == []


def test_zero_cost_load_counts_as_a_move_and_gets_no_bonus():
    state, defs = _game("wotr_exp_1.0", True, "harad")
    loader_id = _unit_in(state, "umbar", "corsair_of_umbar", 0).instance_id
    holder_id = _unit_in(state, "umbar", "corsair_of_umbar", 1).instance_id
    ship_id = _unit_in(state, "sea_zone_11", "black_ship").instance_id
    holder_base = _unit(state, holder_id).base_movement

    # A load costs 0 movement but still counts as moving during combat move.
    state = _act(
        state,
        move_units("harad", "umbar", "sea_zone_11", [loader_id], move_type="load"),
        defs,
    )
    # The sea raid names the boat so the must-attack requirement clears; the engine
    # expands the payload to the pending-loaded passenger, which offloads for 1 movement.
    state = _act(
        state,
        move_units("harad", "sea_zone_11", "harondor", [ship_id], move_type="offload"),
        defs,
    )

    state = _act(state, end_phase("harad"), defs)  # combat_move -> combat
    assert loader_id in state.combat_move_moved_ids
    assert ship_id in state.combat_move_moved_ids
    assert _unit(state, loader_id).remaining_movement == 0, (
        "load was free; only the offload cost 1"
    )

    state = _act(state, end_phase("harad"), defs)  # combat -> non_combat_move
    assert state.combat_move_moved_ids == []
    assert _unit(state, loader_id).remaining_movement == 0, "loader is a mover: no +1 bonus"
    assert _unit(state, holder_id).remaining_movement == holder_base + 1


def test_sea_units_do_not_get_the_bonus():
    state, defs = _game("wotr_exp_1.1", True, "gondor")
    ship_id = _unit_in(state, "sea_zone_12", "gondor_ship").instance_id
    ship_base = _unit(state, ship_id).base_movement
    soldier_id = _unit_in(state, "minas_tirith", "gondor_soldier").instance_id
    soldier_base = _unit(state, soldier_id).base_movement

    state = _to_non_combat_move(state, "gondor", defs)

    assert _unit(state, ship_id).remaining_movement == ship_base, "ships never benefit"
    assert _unit(state, soldier_id).remaining_movement == soldier_base + 1, (
        "land units still get the bonus in the same turn"
    )


def test_aerial_units_do_not_get_the_bonus():
    state, defs = _game("wotr_1.1", True, "mordor")
    nazgul_id = _unit_in(state, "minas_morgul", "nazgul").instance_id
    nazgul_base = _unit(state, nazgul_id).base_movement
    orc_id = _unit_in(state, "minas_morgul", "orc_warrior").instance_id
    orc_base = _unit(state, orc_id).base_movement

    state = _to_non_combat_move(state, "mordor", defs)

    assert _unit(state, nazgul_id).remaining_movement == nazgul_base, (
        "aerial units never benefit"
    )
    assert _unit(state, orc_id).remaining_movement == orc_base + 1, (
        "land units still get the bonus in the same turn"
    )


def _soldier_in_non_combat(marching_orders: bool):
    """Gondor soldier in Minas Tirith, movement 1, so the bonus leaves 2 to spend.

    Land path of length 2 is Minas Tirith → Pelennor → Anorien. Both ends start friendly.
    """
    state, defs = _game("wotr_1.1", marching_orders, "gondor")
    soldier = _unit_in(state, "minas_tirith", "gondor_soldier")
    soldier_id = soldier.instance_id
    state = _to_non_combat_move(state, "gondor", defs)
    unit = _unit(state, soldier_id)
    if marching_orders:
        assert unit.remaining_movement == 2
    return state, defs, soldier_id


def _targets(state, defs, soldier_id):
    unit_defs, territory_defs, faction_defs, _camps, _ports = defs
    targets, _routes = get_unit_move_targets(
        state, soldier_id, unit_defs, territory_defs, faction_defs
    )
    return targets


def test_bonus_unit_cannot_spend_its_last_point_in_a_territory_conquered_this_turn():
    state, defs, soldier_id = _soldier_in_non_combat(True)
    assert soldier_id in state.marching_orders_bonus_ids
    state.conquered_this_turn = {"anorien": "gondor"}

    targets = _targets(state, defs, soldier_id)
    assert "anorien" not in targets
    assert "pelennor" in targets

    action = move_units("gondor", "minas_tirith", "anorien", [soldier_id])
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert not validation.valid
    assert "Marching Orders" in (validation.error or "")
    with pytest.raises(ValueError, match="Marching Orders"):
        apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def test_bonus_unit_may_end_in_a_new_conquest_with_movement_left_and_pass_through_it():
    state, defs, soldier_id = _soldier_in_non_combat(True)
    state.conquered_this_turn = {"pelennor": "gondor"}

    targets = _targets(state, defs, soldier_id)
    assert targets.get("pelennor") == 1
    assert "anorien" in targets

    state = _act(state, move_units("gondor", "minas_tirith", "pelennor", [soldier_id]), defs)
    assert any(m.to_territory == "pelennor" and soldier_id in m.unit_instance_ids for m in state.pending_moves)

    state, defs, soldier_id = _soldier_in_non_combat(True)
    state.conquered_this_turn = {"pelennor": "gondor"}
    state = _act(state, move_units("gondor", "minas_tirith", "anorien", [soldier_id]), defs)
    assert any(m.to_territory == "anorien" and soldier_id in m.unit_instance_ids for m in state.pending_moves)


def test_units_without_the_bonus_and_the_rule_off_may_enter_a_new_conquest():
    state, defs, soldier_id = _soldier_in_non_combat(True)
    state.marching_orders_bonus_ids = [i for i in state.marching_orders_bonus_ids if i != soldier_id]
    state.conquered_this_turn = {"anorien": "gondor"}
    assert "anorien" in _targets(state, defs, soldier_id)
    state = _act(state, move_units("gondor", "minas_tirith", "anorien", [soldier_id]), defs)
    assert any(m.to_territory == "anorien" for m in state.pending_moves)

    state, defs, soldier_id = _soldier_in_non_combat(False)
    _unit(state, soldier_id).remaining_movement = 2
    state.marching_orders_bonus_ids = [soldier_id]
    state.conquered_this_turn = {"anorien": "gondor"}
    assert "anorien" in _targets(state, defs, soldier_id)
    state = _act(state, move_units("gondor", "minas_tirith", "anorien", [soldier_id]), defs)
    assert any(m.to_territory == "anorien" for m in state.pending_moves)


def test_previously_friendly_destination_is_not_restricted():
    state, defs, soldier_id = _soldier_in_non_combat(True)
    state.conquered_this_turn = {}
    assert "anorien" in _targets(state, defs, soldier_id)
    state = _act(state, move_units("gondor", "minas_tirith", "anorien", [soldier_id]), defs)
    assert any(m.to_territory == "anorien" for m in state.pending_moves)


def test_conquest_record_and_bonus_ids_follow_the_turn():
    state, defs, ids, _bases = _gondor_board(True)
    state = _act(
        state,
        move_units("gondor", "north_ithilien", "dagorlad", [ids["mover"]]),
        defs,
    )
    state = _act(state, end_phase("gondor"), defs)  # combat_move -> combat
    state.pending_captures["anorien"] = "gondor"
    state = _act(state, end_phase("gondor"), defs)  # combat -> non_combat_move
    assert state.conquered_this_turn.get("anorien") == "gondor"
    assert ids["holder"] in state.marching_orders_bonus_ids
    assert ids["mover"] not in state.marching_orders_bonus_ids

    state = _act(state, end_phase("gondor"), defs)  # non_combat_move -> mobilization
    assert state.marching_orders_bonus_ids == []
    assert state.conquered_this_turn.get("anorien") == "gondor"

    state = _act(state, end_phase("gondor"), defs)  # mobilization -> next turn
    assert state.conquered_this_turn == {}
    assert state.marching_orders_bonus_ids == []
