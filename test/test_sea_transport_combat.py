"""
Tests for sea movement, transport (load/offload/sail), and sea raid in REAL combat (reducer).
Uses dummy units created from unit_defs so tests never skip based on setup.
Asserts ships stay in sea (e.g. after sea raid conquer, after offload) and land units move correctly.

For behavior that was buggy once, prefer tests that assert BOTH: (1) the old path fails or errors,
and (2) the fix path succeeds. "Always green" on only (2) does not prove the guard exists.
See test_sail_to_offload_land_payload_contract_regression.
"""
import pytest
from copy import deepcopy

from backend.engine.state import Unit, PendingMove, ActiveCombat
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import (
    move_units,
    initiate_combat,
    continue_combat,
    end_phase,
    retreat,
)
import backend.engine.reducer as reducer_mod
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.event_messages import build_message
from backend.engine.utils import (
    initialize_game_state,
    get_unit_faction,
    is_land_unit,
)
from backend.engine.queries import (
    validate_action,
    get_valid_offload_sea_zones,
    get_retreat_options,
    participates_in_sea_hex_naval_combat,
    get_contested_territories,
    get_aerial_units_must_move,
)
from backend.engine.combat import calculate_required_dice
from backend.engine.movement import expand_sea_offload_instance_ids, remaining_sea_load_passenger_slots


def _is_naval_unit(unit_defs, unit_id: str) -> bool:
    ud = unit_defs.get(unit_id)
    if not ud:
        return False
    return getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", [])


def _is_land_passenger(unit_defs, unit_id: str) -> bool:
    """Land unit that can be moved to territory (not naval, not aerial)."""
    return is_land_unit(unit_defs.get(unit_id)) and not _is_naval_unit(unit_defs, unit_id)


def _make_unit(state, faction: str, unit_id: str, unit_defs) -> Unit:
    """Create a unit from unit_defs (dummy for tests; no setup dependency)."""
    ud = unit_defs.get(unit_id)
    if not ud:
        raise ValueError(f"Unknown unit_id: {unit_id}")
    mov = getattr(ud, "movement", 1)
    health = getattr(ud, "health", 1)
    return Unit(
        instance_id=state.generate_unit_instance_id(faction, unit_id),
        unit_id=unit_id,
        remaining_movement=mov,
        remaining_health=health,
        base_movement=mov,
        base_health=health,
    )


@pytest.fixture
def wotr_defs():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    return unit_defs, territory_defs, faction_defs, camp_defs, port_defs


@pytest.fixture
def state_with_sea_units(wotr_defs):
    """State with harad ship + land unit in sea_zone_11 (dummy units from unit_defs). No setup dependency."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_zone = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_zone is not None and harondor is not None
    # Use only our dummy units: clear sea_zone_11 then add one naval, one land
    sea_zone.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    sea_zone.units.append(ship)
    sea_zone.units.append(land)
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat"
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def test_naval_hex_continue_excludes_embarked_passengers_from_defender_dice(wotr_defs):
    """
    Continuing a sea-hex battle must not treat embarked units as defender combatants (regression:
    defender roster was 'all hex units not in attacker_ids', which included passengers).
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea = state.territories["sea_zone_11"]
    sea.units.clear()
    har_ship = _make_unit(state, "harad", "black_ship", unit_defs)
    g_ship = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    soldier = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    soldier.loaded_onto = g_ship.instance_id
    sea.units.extend([har_ship, g_ship, soldier])

    state.current_faction = "harad"
    state.phase = "combat"

    a1 = initiate_combat(
        "harad",
        "sea_zone_11",
        dice_rolls={"attacker": [6], "defender": [6]},
    )
    state, _ = apply_action(
        state, a1, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert state.active_combat is not None

    surviving = set(state.active_combat.attacker_instance_ids)
    defenders_raw = sorted(
        [u for u in sea.units if u.instance_id not in surviving],
        key=lambda u: u.instance_id,
    )
    defenders_filtered = [u for u in defenders_raw if participates_in_sea_hex_naval_combat(u, unit_defs.get(u.unit_id))]
    assert calculate_required_dice(defenders_raw, unit_defs) == 2
    assert calculate_required_dice(defenders_filtered, unit_defs) == 1

    a2 = continue_combat(
        "harad",
        dice_rolls={"attacker": [6], "defender": [6]},
    )
    apply_action(
        state, a2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )


def test_sea_raid_empty_land_conquer(state_with_sea_units):
    """Sea raid on empty land: land units move to territory, ships stay in sea."""
    state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs = state_with_sea_units
    sea_zone = state.territories["sea_zone_11"]
    harondor = state.territories["harondor"]
    ship_ids_before = {u.instance_id for u in sea_zone.units if _is_naval_unit(unit_defs, u.unit_id)}
    land_in_sea_before = [u for u in sea_zone.units if _is_land_passenger(unit_defs, u.unit_id)]
    assert len(land_in_sea_before) >= 1
    assert len(ship_ids_before) >= 1

    action = initiate_combat(
        "harad", "harondor",
        dice_rolls={"attacker": [1], "defender": []},
        sea_zone_id="sea_zone_11",
    )
    state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )

    sea_zone_after = state.territories["sea_zone_11"]
    harondor_after = state.territories["harondor"]
    # Land (passenger) units left sea and are on territory; ships stay in sea
    sea_land_passengers_after = [u for u in sea_zone_after.units if _is_land_passenger(unit_defs, u.unit_id)]
    harad_on_land = [u for u in harondor_after.units if get_unit_faction(u, unit_defs) == "harad"]
    assert len(sea_land_passengers_after) == 0, "land (passenger) units should have left sea zone"
    assert len(harad_on_land) >= 1, "land units should be on harondor"
    # Ships stay in sea
    ships_still_in_sea = [u for u in sea_zone_after.units if _is_naval_unit(unit_defs, u.unit_id)]
    assert len(ships_still_in_sea) == len(ship_ids_before), "ships must stay in sea zone after sea raid conquer"
    assert state.pending_captures.get("harondor") == "harad"
    combat_ended = [e for e in events if e.type == "combat_ended"]
    assert len(combat_ended) == 1 and combat_ended[0].payload.get("outcome") == "conquer"


def test_sea_raid_combat_attacker_win_moves_to_territory(state_with_sea_units):
    """Sea raid with combat: attacker wins -> land survivors move to territory, ships stay in sea."""
    state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs = state_with_sea_units
    sea_zone = state.territories["sea_zone_11"]
    harondor = state.territories["harondor"]
    ship_ids_before = {u.instance_id for u in sea_zone.units if _is_naval_unit(unit_defs, u.unit_id)}
    # Add one defender (dummy from unit_defs)
    defender = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    harondor.units.append(defender)
    harondor.owner = "gondor"

    action = initiate_combat(
        "harad", "harondor",
        dice_rolls={"attacker": [1, 1], "defender": [6, 6]},
        sea_zone_id="sea_zone_11",
    )
    state, _ = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    if state.active_combat:
        state, _ = apply_action(
            state, continue_combat("harad", dice_rolls={"attacker": [1], "defender": [6]}),
            unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
        )

    sea_zone_after = state.territories["sea_zone_11"]
    # Ships must still be in sea
    ships_still_in_sea = [u for u in sea_zone_after.units if _is_naval_unit(unit_defs, u.unit_id)]
    assert len(ships_still_in_sea) == len(ship_ids_before), "ships must stay in sea after sea raid combat"
    assert state.active_combat is None
    # Land (passenger) survivors on territory or dead; no land passengers left in sea; ships stay in sea
    sea_land_passengers_after = [u for u in sea_zone_after.units if _is_land_passenger(unit_defs, u.unit_id)]
    harad_on_land = [u for u in state.territories["harondor"].units if get_unit_faction(u, unit_defs) == "harad"]
    assert len(sea_land_passengers_after) == 0
    assert len(harad_on_land) >= 1 or len(sea_land_passengers_after) == 0


def test_sea_raid_defender_hits_apply_to_land_attackers(state_with_sea_units):
    """Sea raid: defender hits must apply to land attackers (not naval-only hit eligibility)."""
    state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs = state_with_sea_units
    sea_zone = state.territories["sea_zone_11"]
    harondor = state.territories["harondor"]
    sea_zone.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    sea_zone.units.extend([ship, land])
    defender = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    harondor.units.append(defender)
    harondor.owner = "gondor"
    land_id = land.instance_id

    # Corsair attack 1: [6] misses Gondor; Gondor defense 2: [1] hits Corsair
    action = initiate_combat(
        "harad", "harondor",
        dice_rolls={"attacker": [6], "defender": [1]},
        sea_zone_id="sea_zone_11",
    )
    state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    # apply_action deep-copies state; read territories from returned state only
    sea_ids = {u.instance_id for u in state.territories["sea_zone_11"].units}
    assert land_id not in sea_ids, "defender hit should remove land attacker (from sea zone for sea raid)"
    assert state.active_combat is None


def test_load_move_applies_and_sets_loaded_onto(wotr_defs):
    """Load (land -> sea): dummy ship in sea, dummy land on port; after apply, passenger in sea with loaded_onto, ship still in sea."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    # Boat must have transport capacity for load to succeed (data may default to 0)
    ship_def = unit_defs.get("gondor_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    pelargir = state.territories.get("pelargir")
    sea_12 = state.territories.get("sea_zone_12")
    assert pelargir is not None and sea_12 is not None
    # Dummy ship in sea, dummy land on pelargir (no setup dependency)
    ship = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    passenger = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    sea_12.units.append(ship)
    pelargir.units.append(passenger)
    boat_id = ship.instance_id
    land_id = passenger.instance_id
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    action = move_units(
        "gondor", "pelargir", "sea_zone_12", [land_id],
        move_type="load", load_onto_boat_instance_id=boat_id,
    )
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    state, _ = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    sea_after = state.territories["sea_zone_12"]
    in_sea = [u for u in sea_after.units if u.instance_id == land_id]
    assert len(in_sea) == 1, "passenger should be in sea"
    assert getattr(in_sea[0], "loaded_onto", None) == boat_id
    # Ship stayed in sea
    ship_still = [u for u in sea_after.units if u.instance_id == boat_id]
    assert len(ship_still) == 1, "ship must stay in sea zone after load"
    pax = next(u for u in sea_after.units if u.instance_id == land_id)
    assert pax.remaining_movement == pax.base_movement, "load must cost passengers 0 movement"


def test_offload_move_clears_loaded_onto(wotr_defs):
    """Offload (sea -> land): dummy ship + passenger in sea; after apply, passenger on land with loaded_onto cleared, ship stays in sea."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_12 = state.territories.get("sea_zone_12")
    pelargir = state.territories.get("pelargir")
    assert sea_12 is not None and pelargir is not None
    ship = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    passenger = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    passenger.loaded_onto = ship.instance_id
    boat_id = ship.instance_id
    passenger_id = passenger.instance_id
    sea_12.units.append(ship)
    sea_12.units.append(passenger)
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    action = move_units("gondor", "sea_zone_12", "pelargir", [passenger_id], move_type="offload")
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    sim = get_state_after_pending_moves(
        state, "non_combat_move", unit_defs, territory_defs, faction_defs
    )
    on_land_sim = next(
        (x for x in sim.territories["pelargir"].units if x.instance_id == passenger_id), None
    )
    assert on_land_sim is not None
    assert on_land_sim.remaining_movement == max(0, on_land_sim.base_movement - 1), (
        "offload must cost land passenger 1 movement (before phase-end reset)"
    )

    state, _ = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    on_land = next((x for x in state.territories["pelargir"].units if x.instance_id == passenger_id), None)
    assert on_land is not None, "offloaded unit should be on pelargir"
    assert getattr(on_land, "loaded_onto", None) is None
    # Ship stayed in sea
    sea_after = state.territories["sea_zone_12"]
    ship_still = [u for u in sea_after.units if u.instance_id == boat_id]
    assert len(ship_still) == 1, "ship must stay in sea zone after offload"


def test_sail_move_sea_to_sea(wotr_defs):
    """Sail (sea -> sea): naval unit moves to adjacent sea; source sea has one fewer, dest has ship."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    assert sea_11 is not None
    # Ensure we have exactly one ship (dummy if needed)
    existing = [u for u in sea_11.units if _is_naval_unit(unit_defs, u.unit_id)]
    if not existing:
        ship = _make_unit(state, "harad", "black_ship", unit_defs)
        sea_11.units.append(ship)
        ship_instance = ship.instance_id
        faction = "harad"
    else:
        ship_instance = existing[0].instance_id
        faction = get_unit_faction(existing[0], unit_defs)
    adj = getattr(territory_defs.get("sea_zone_11"), "adjacent", []) or []
    sea_adj = [tid for tid in adj if territory_defs.get(tid) and getattr(territory_defs.get(tid), "terrain_type", "").lower() == "sea"]
    assert sea_adj, "sea_zone_11 must have adjacent sea"
    dest = sea_adj[0]
    state.current_faction = faction
    # Sail between empty sea zones is non_combat only; combat_move = hostile sea or sea raid to land.
    state.phase = "non_combat_move"

    action = move_units(faction, "sea_zone_11", dest, [ship_instance], move_type="sail")
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    state, _ = apply_action(state, end_phase(faction), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    assert ship_instance not in [u.instance_id for u in state.territories["sea_zone_11"].units]
    assert ship_instance in [u.instance_id for u in state.territories[dest].units]


def test_offload_combat_move_boat_ids_only_expand_passengers(wotr_defs):
    """Sea raid offload: payload lists only the boat; engine expands to embarked passengers."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    land.loaded_onto = ship.instance_id
    sea_11.units.extend([ship, land])
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat_move"

    action = move_units("harad", "sea_zone_11", "harondor", [ship.instance_id], move_type="offload")
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    state, _ = apply_action(state, end_phase("harad"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    assert land.instance_id in [u.instance_id for u in state.territories["harondor"].units]
    assert ship.instance_id in [u.instance_id for u in state.territories["sea_zone_11"].units]


def test_offload_after_pending_load_same_turn_no_loaded_onto_yet(wotr_defs):
    """Passengers are not on the boat in live state until phase end; pending load + sea raid same combat_move."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    harondor.units.append(land)
    sea_11.units.append(ship)
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat_move"

    load_a = move_units(
        "harad", "harondor", "sea_zone_11", [land.instance_id],
        move_type="load", load_onto_boat_instance_id=ship.instance_id,
    )
    v1 = validate_action(state, load_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v1.valid, v1.error
    state, _ = apply_action(state, load_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert getattr(land, "loaded_onto", None) is None

    off_a = move_units("harad", "sea_zone_11", "harondor", [ship.instance_id], move_type="offload")
    v2 = validate_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v2.valid, v2.error
    state, _ = apply_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 2

    state, _ = apply_action(state, end_phase("harad"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert land.instance_id in [u.instance_id for u in state.territories["harondor"].units]
    assert ship.instance_id in [u.instance_id for u in state.territories["sea_zone_11"].units]


def test_expand_offload_includes_pending_when_request_lists_embarked_passengers(wotr_defs):
    """Regression: do not skip pending-load splice when IDs already include embarked land units in the sea hex."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 3)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    embarked = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    embarked.loaded_onto = ship.instance_id
    pending_only = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    harondor.units.append(pending_only)
    sea_11.units.extend([ship, embarked])
    state.current_faction = "harad"
    state.phase = "combat_move"
    state.pending_moves.append(
        PendingMove(
            from_territory="harondor",
            to_territory="sea_zone_11",
            unit_instance_ids=[pending_only.instance_id],
            phase="combat_move",
            move_type="load",
            load_onto_boat_instance_id=ship.instance_id,
        )
    )
    expanded = expand_sea_offload_instance_ids(
        state,
        "sea_zone_11",
        "harondor",
        [ship.instance_id, embarked.instance_id],
        unit_defs,
        territory_defs,
        "harad",
    )
    assert pending_only.instance_id in expanded, "pending same-phase load must merge even when embarked IDs are listed"
    off_a = move_units("harad", "sea_zone_11", "harondor", expanded, move_type="offload")
    v = validate_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error


def test_offload_after_pending_load_infers_when_move_type_missing(wotr_defs):
    """Pending load with move_type None (legacy/partial JSON): still expands passengers for boat-only offload."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    harondor.units.append(land)
    sea_11.units.append(ship)
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat_move"

    state.pending_moves.append(
        PendingMove(
            from_territory="harondor",
            to_territory="sea_zone_11",
            unit_instance_ids=[land.instance_id],
            phase="combat_move",
            move_type=None,
            load_onto_boat_instance_id=ship.instance_id,
        )
    )

    off_a = move_units("harad", "sea_zone_11", "harondor", [ship.instance_id], move_type="offload")
    v2 = validate_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v2.valid, f"offload must accept pending load when move_type missing: {v2.error}"


def test_offload_after_pending_load_without_specific_boat(wotr_defs):
    """Load with no load_onto_boat_instance_id: pending passengers still expand for boat-only offload same phase."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    harondor.units.append(land)
    sea_11.units.append(ship)
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat_move"

    load_a = move_units(
        "harad", "harondor", "sea_zone_11", [land.instance_id],
        move_type="load",
    )
    v1 = validate_action(state, load_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v1.valid, v1.error
    state, _ = apply_action(state, load_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert getattr(land, "loaded_onto", None) is None

    off_a = move_units("harad", "sea_zone_11", "harondor", [ship.instance_id], move_type="offload")
    v2 = validate_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v2.valid, f"boat-only offload must expand pending load without explicit boat id: {v2.error}"
    state, _ = apply_action(state, off_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 2


def test_sail_to_offload_land_payload_contract_regression(wotr_defs):
    """
    Regression: combat_move naval reachability forbids empty sea as a destination, but
    get_valid_offload_sea_zones allows sailing there for a raid. The payload ties the sail
    to the land drop so we use offload BFS instead.

    * Without* sail_to_offload_land_territory_id: same sail must FAIL (old bug).
    * With* payload: must SUCCEED. Removing the special-case in queries/reducer should turn
      the second assertion red while the first may start passing (wrong).
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_11 = state.territories.get("sea_zone_11")
    harondor = state.territories.get("harondor")
    assert sea_11 is not None and harondor is not None
    sea_11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    land2 = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    harondor.units.extend([land, land2])
    sea_11.units.append(ship)
    if harondor.owner == "harad":
        harondor.owner = "gondor"
    state.current_faction = "harad"
    state.phase = "combat_move"

    load_a = move_units(
        "harad", "harondor", "sea_zone_11", [land.instance_id, land2.instance_id],
        move_type="load", load_onto_boat_instance_id=ship.instance_id,
    )
    state, _ = apply_action(state, load_a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    iids = [ship.instance_id, land.instance_id, land2.instance_id]
    zones = get_valid_offload_sea_zones(
        "sea_zone_11", "harondor", state, iids,
        unit_defs, territory_defs, faction_defs, state.phase,
    )
    assert zones
    target_b = next((z for z in zones if z != "sea_zone_11"), None)
    if target_b is None:
        pytest.skip("map has only one offload sea from sea_zone_11; cannot test multi-hex sail")

    sail_plain = move_units(
        "harad", "sea_zone_11", target_b,
        iids,
        move_type="sail",
    )
    bad = validate_action(
        state, sail_plain, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert not bad.valid, (
        "expected plain combat_move sail to empty sea to fail without offload-land payload; "
        f"got valid=True (error would be: {bad.error})"
    )
    err = (bad.error or "").lower()
    # Without the payload we do not take the offload-BFS path: either naval reachability
    # rejects empty sea ("at least one unit…reach") or driver/passenger split is wrong first
    # ("passengers" / "capacity"). Any of these means "plain sail" is not valid here.
    assert any(
        s in err for s in ("at least one unit", "reach", "passengers", "capacity")
    ), f"unexpected error (if engine order changed, document here): {bad.error!r}"

    sail_flagged = move_units(
        "harad", "sea_zone_11", target_b,
        iids,
        move_type="sail",
        sail_to_offload_land_territory_id="harondor",
    )
    good = validate_action(
        state, sail_flagged, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert good.valid, good.error


def test_valid_offload_sea_zones_includes_adjacent_hostile_sea_only_path_to_land(wotr_defs):
    """
    Umbar's only adjacent sea is sea_zone_11. If that zone is hostile, you must still be able
    to sail into it (naval battle) then sea raid — offload BFS must count the hostile hex as a
    valid destination, not skip it entirely.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("black_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 2)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea10 = state.territories["sea_zone_10"]
    sea11 = state.territories["sea_zone_11"]
    sea10.units.clear()
    sea11.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    enemy_ship = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    sea10.units.extend([ship, land])
    sea11.units.append(enemy_ship)
    state.current_faction = "harad"
    state.phase = "combat_move"
    iids = [ship.instance_id, land.instance_id]
    valid = get_valid_offload_sea_zones(
        "sea_zone_10", "umbar", state, iids,
        unit_defs, territory_defs, faction_defs, state.phase,
    )
    assert "sea_zone_11" in valid, (
        "must allow sail into hostile sea_zone_11 to raid Umbar (its only adjacent sea)"
    )


def test_pending_offload_applies_when_move_type_omitted(wotr_defs):
    """
    Regression: end-of-phase apply must not silently skip sea→land offload when move_type was
    omitted from the pending move (e.g. legacy JSON). Passengers must ashore and sea raid map entry exists.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea = state.territories["sea_zone_11"]
    har = state.territories["harondor"]
    sea.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    sea.units.extend([ship, land])
    if har.owner != "gondor":
        har.owner = "gondor"
    defender = _make_unit(state, "gondor", "gondor_soldier", unit_defs)
    har.units.append(defender)

    state.pending_moves = [
        PendingMove(
            from_territory="sea_zone_11",
            to_territory="harondor",
            unit_instance_ids=[ship.instance_id, land.instance_id],
            phase="combat_move",
            move_type=None,
        )
    ]
    state.current_faction = "harad"
    state.phase = "combat_move"

    after = get_state_after_pending_moves(
        state, "combat_move", unit_defs, territory_defs, faction_defs
    )
    sea_after = after.territories["sea_zone_11"]
    still_aboard = [u for u in sea_after.units if u.instance_id == land.instance_id]
    assert len(still_aboard) == 0, "passenger should leave sea when offload applies"
    on_har = [u for u in after.territories["harondor"].units if u.instance_id == land.instance_id]
    assert len(on_har) == 1, "passenger should be on land after pending apply"
    assert after.territory_sea_raid_from.get("harondor") == "sea_zone_11"


def test_pending_offload_apply_expands_boat_only_instance_ids(wotr_defs):
    """
    Declaration runs expand_sea_offload_instance_ids; apply must do the same. Otherwise a pending
    row that only lists the ship (e.g. drag token, or older stored JSON) consumes the move but
    moves zero land units — passengers stay on the boat and no land battle appears.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea = state.territories["sea_zone_11"]
    har = state.territories["harondor"]
    sea.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    land.loaded_onto = ship.instance_id
    sea.units.extend([ship, land])
    if har.owner != "gondor":
        har.owner = "gondor"
    har.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    state.pending_moves = [
        PendingMove(
            from_territory="sea_zone_11",
            to_territory="harondor",
            unit_instance_ids=[ship.instance_id],
            phase="combat_move",
            move_type="offload",
        )
    ]
    state.current_faction = "harad"
    state.phase = "combat_move"

    after = get_state_after_pending_moves(
        state, "combat_move", unit_defs, territory_defs, faction_defs
    )
    sea_after = after.territories["sea_zone_11"]
    still_aboard = [u for u in sea_after.units if u.instance_id == land.instance_id]
    assert len(still_aboard) == 0, "passenger must offload when apply expands boat-only ids"
    on_har = [u for u in after.territories["harondor"].units if u.instance_id == land.instance_id]
    assert len(on_har) == 1
    assert after.territory_sea_raid_from.get("harondor") == "sea_zone_11"


def test_validate_initiate_sea_raid_when_land_units_only_on_land(wotr_defs):
    """
    After combative offload, attackers stand on the land hex; sea zone may only have the fleet.
    Initiate validation must accept land attackers (same as reducer), not only units in the sea.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea = state.territories["sea_zone_11"]
    har = state.territories["harondor"]
    sea.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    sea.units.append(ship)
    if har.owner != "gondor":
        har.owner = "gondor"
    har.units.append(_make_unit(state, "harad", "corsair_of_umbar", unit_defs))
    har.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))
    state.territory_sea_raid_from["harondor"] = "sea_zone_11"
    state.current_faction = "harad"
    state.phase = "combat"

    action = initiate_combat(
        "harad",
        "harondor",
        dice_rolls={"attacker": [1, 1], "defender": [5, 5]},
        sea_zone_id="sea_zone_11",
    )
    vr = validate_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert vr.valid, vr.error


def test_pending_loads_consume_capacity_no_extra_declaration(wotr_defs):
    """Same-phase pending loads reserve boat slots; cannot declare more loads than remaining space."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("gondor_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 1)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_12 = state.territories.get("sea_zone_12")
    pelargir = state.territories.get("pelargir")
    assert sea_12 is not None and pelargir is not None
    sea_12.units = []
    ship1 = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    ship2 = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    sea_12.units.extend([ship1, ship2])
    s1, s2, s3 = [_make_unit(state, "gondor", "gondor_soldier", unit_defs) for _ in range(3)]
    pelargir.units = [s1, s2, s3]
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    a1 = move_units(
        "gondor", "pelargir", "sea_zone_12", [s1.instance_id],
        move_type="load", load_onto_boat_instance_id=ship1.instance_id,
    )
    assert validate_action(state, a1, unit_defs, territory_defs, faction_defs, camp_defs, port_defs).valid
    state, _ = apply_action(state, a1, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    a2 = move_units(
        "gondor", "pelargir", "sea_zone_12", [s2.instance_id],
        move_type="load", load_onto_boat_instance_id=ship2.instance_id,
    )
    assert validate_action(state, a2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs).valid
    state, _ = apply_action(state, a2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    assert remaining_sea_load_passenger_slots(
        state, "sea_zone_12", "gondor", unit_defs, territory_defs, "non_combat_move",
    ) == 0

    a3_explicit = move_units(
        "gondor", "pelargir", "sea_zone_12", [s3.instance_id],
        move_type="load", load_onto_boat_instance_id=ship1.instance_id,
    )
    v3 = validate_action(state, a3_explicit, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v3.valid, "must reject load onto boat already filled by pending passengers"

    a3_auto = move_units(
        "gondor", "pelargir", "sea_zone_12", [s3.instance_id],
        move_type="load",
    )
    v3a = validate_action(state, a3_auto, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v3a.valid, "must reject auto-assign load when zone has no slots left"


def test_validate_rejects_single_load_when_all_units_reach_but_zone_full(wotr_defs):
    """Regression: all land units 'reach' adjacent sea; validate_action must still enforce capacity."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    ship_def = unit_defs.get("gondor_ship")
    if ship_def is not None:
        setattr(ship_def, "transport_capacity", 1)
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea_12 = state.territories.get("sea_zone_12")
    pelargir = state.territories.get("pelargir")
    assert sea_12 is not None and pelargir is not None
    sea_12.units = []
    ship1 = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    sea_12.units.append(ship1)
    s1, s2 = [_make_unit(state, "gondor", "gondor_soldier", unit_defs) for _ in range(2)]
    pelargir.units = [s1, s2]
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    a = move_units(
        "gondor",
        "pelargir",
        "sea_zone_12",
        [s1.instance_id, s2.instance_id],
        move_type="load",
    )
    v = validate_action(state, a, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v.valid, v.error


def test_naval_loss_after_offload_clears_sea_raid_staging_and_passengers(wotr_defs):
    """
    Mandatory naval combat in the sea zone must not leave a follow-up land sea raid if all attackers
    are eliminated: clear territory_sea_raid_from and remove stranded land attackers on the target hex.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sea = state.territories["sea_zone_11"]
    har = state.territories["harondor"]
    sea.units.clear()
    ship = _make_unit(state, "harad", "black_ship", unit_defs)
    land = _make_unit(state, "harad", "corsair_of_umbar", unit_defs)
    gship = _make_unit(state, "gondor", "gondor_ship", unit_defs)
    sea.units.extend([ship, land, gship])
    if har.owner != "gondor":
        har.owner = "gondor"
    har.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    state.pending_moves = [
        PendingMove(
            from_territory="sea_zone_11",
            to_territory="harondor",
            unit_instance_ids=[ship.instance_id, land.instance_id],
            phase="combat_move",
            move_type="offload",
        )
    ]
    state.current_faction = "harad"
    state.phase = "combat_move"
    state = get_state_after_pending_moves(
        state, "combat_move", unit_defs, territory_defs, faction_defs
    )
    assert state.territory_sea_raid_from.get("harondor") == "sea_zone_11"
    harad_on_land_before = [
        u for u in state.territories["harondor"].units
        if get_unit_faction(u, unit_defs) == "harad"
    ]
    assert len(harad_on_land_before) >= 1

    state.phase = "combat"
    state.current_faction = "harad"
    state, _ = apply_action(
        state,
        initiate_combat(
            "harad",
            "sea_zone_11",
            dice_rolls={"attacker": [10], "defender": [1]},
        ),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert state.active_combat is None
    assert state.territory_sea_raid_from.get("harondor") is None
    harad_on_land_after = [
        u for u in state.territories["harondor"].units
        if get_unit_faction(u, unit_defs) == "harad"
    ]
    assert len(harad_on_land_after) == 0


def test_retreat_and_retreat_options_rejected_during_sea_raid(wotr_defs):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    state.phase = "combat"
    state.current_faction = "harad"
    state.active_combat = ActiveCombat(
        attacker_faction="harad",
        territory_id="harondor",
        attacker_instance_ids=["harad_corsair_test"],
        round_number=1,
        sea_zone_id="sea_zone_11",
        initial_attacker_instance_ids=["harad_corsair_test"],
        initial_defender_instance_ids=["gondor_gondor_soldier_test"],
    )
    vr = validate_action(
        state, retreat("harad", "umbar"),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert not vr.valid, vr.error
    assert "sea raid" in (vr.error or "").lower()

    opts = get_retreat_options(state, territory_defs, faction_defs, unit_defs)
    assert opts == []


def test_contested_sea_zone_counts_aerial_vs_enemy_naval(wotr_defs):
    """Combat phase `combat_territories` must list sea hexes where aerial faces enemy ships (not naval-only filter)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sz = state.territories.get("sea_zone_11")
    assert sz is not None
    sz.units.clear()
    sz.units.append(_make_unit(state, "mordor", "nazgul", unit_defs))
    sz.units.append(_make_unit(state, "gondor", "gondor_ship", unit_defs))
    contested = get_contested_territories(state, "mordor", faction_defs, unit_defs, territory_defs)
    sea_entries = [c for c in contested if c.get("territory_id") == "sea_zone_11"]
    assert len(sea_entries) == 1, f"expected one contested sea entry, got {contested!r}"
    assert sea_entries[0]["attacker_count"] >= 1
    assert sea_entries[0]["defender_count"] >= 1


def test_apply_pending_aerial_land_to_sea_never_logs_as_load(wotr_defs):
    """
    _effective_move_type must not coerce move_type aerial (or infer load for all-aerial) — event log must not say 'Loaded'.
    Regression: aerial was treated as load at apply time even when queued as aerial.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    land = state.territories.get("harondor")
    sz = state.territories.get("sea_zone_11")
    assert land is not None and sz is not None
    sz.units.clear()
    naz = _make_unit(state, "mordor", "nazgul", unit_defs)
    land.units = [u for u in land.units if get_unit_faction(u, unit_defs) != "mordor"]
    land.units.append(naz)
    state.current_faction = "mordor"
    pm = PendingMove(
        from_territory="harondor",
        to_territory="sea_zone_11",
        unit_instance_ids=[naz.instance_id],
        phase="combat_move",
        move_type="aerial",
        primary_unit_id="nazgul",
    )
    state.pending_moves = [pm]
    _st, events = reducer_mod._apply_pending_moves(
        state, "combat_move", unit_defs, territory_defs, faction_defs,
    )
    moved = [e for e in events if getattr(e, "type", None) == "units_moved"]
    assert len(moved) == 1
    line = build_message(moved[0].type, moved[0].payload, unit_defs, territory_defs, faction_defs)
    assert "Loaded" not in line, line
    assert "attack" in line.lower(), line


def test_ncm_apply_pending_aerial_sea_to_land_moves_flyer_ashore(wotr_defs):
    """
    Sea→land apply used the offload passenger filter for any sea→land hex pair, which drops non-land units.
    Aerial flyers never moved, stayed on sea (non-landing-friendly), and can_end_phase stayed false.
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sz = state.territories.get("sea_zone_11")
    har = state.territories.get("harondor")
    assert sz is not None and har is not None
    sz.units.clear()
    har.units = [u for u in har.units if get_unit_faction(u, unit_defs) != "mordor"]
    har.owner = "mordor"
    naz = _make_unit(state, "mordor", "nazgul", unit_defs)
    sz.units.append(naz)
    state.current_faction = "mordor"
    state.phase = "non_combat_move"
    state.pending_moves = [
        PendingMove(
            from_territory="sea_zone_11",
            to_territory="harondor",
            unit_instance_ids=[naz.instance_id],
            phase="non_combat_move",
            move_type="aerial",
            primary_unit_id="nazgul",
        )
    ]
    after = get_state_after_pending_moves(
        state, "non_combat_move", unit_defs, territory_defs, faction_defs,
    )
    assert not any(u.instance_id == naz.instance_id for u in after.territories["sea_zone_11"].units)
    assert any(u.instance_id == naz.instance_id for u in after.territories["harondor"].units)
    stuck = get_aerial_units_must_move(
        after, unit_defs, territory_defs, faction_defs, "mordor",
    )
    assert stuck == []


def test_ncm_apply_pending_aerial_sea_to_land_with_wrong_move_type_land(wotr_defs):
    """Stale client/DB may store move_type land; apply must still move flyers (not use offload passenger filter)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = wotr_defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    sz = state.territories.get("sea_zone_11")
    har = state.territories.get("harondor")
    assert sz is not None and har is not None
    sz.units.clear()
    har.units = [u for u in har.units if get_unit_faction(u, unit_defs) != "mordor"]
    har.owner = "mordor"
    naz = _make_unit(state, "mordor", "nazgul", unit_defs)
    sz.units.append(naz)
    state.current_faction = "mordor"
    state.phase = "non_combat_move"
    state.pending_moves = [
        PendingMove(
            from_territory="sea_zone_11",
            to_territory="harondor",
            unit_instance_ids=[naz.instance_id],
            phase="non_combat_move",
            move_type="land",
            primary_unit_id="nazgul",
        )
    ]
    after = get_state_after_pending_moves(
        state, "non_combat_move", unit_defs, territory_defs, faction_defs,
    )
    assert any(u.instance_id == naz.instance_id for u in after.territories["harondor"].units)
    assert get_aerial_units_must_move(
        after, unit_defs, territory_defs, faction_defs, "mordor",
    ) == []

