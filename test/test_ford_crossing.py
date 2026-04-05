"""
Ford crossing: ford_adjacent edges, ford_crosser special, pooled transport_capacity escort.

`min_ford_edges_for_land_move` can be 0 when some adjacent-only detour exists between two territories,
but a direct river-ford link (ford_adjacent only) is still a 1-MP shortcut that spends escort and
requires a crosser lead. Synthetic ford-only pairs (no detour) are used for additional harness tests.
"""

from backend.engine.definitions import TerritoryDefinition, load_static_definitions, load_starting_setup
from backend.engine.state import Unit
from backend.engine.actions import move_units
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state
from backend.engine.queries import validate_action
from backend.engine.movement import (
    _has_adjacent_only_land_path,
    direct_ford_only_land_pair,
    ford_shortcut_requires_escort_lead,
    get_reachable_territories_for_unit,
    land_move_ford_escort_cost_for_instances,
    min_ford_edges_for_land_move,
    pending_ford_crosser_lead_move_from_origin,
    remaining_ford_escort_slots,
)
from backend.engine.state import PendingMove

# Synthetic territories: only connection is ford_adjacent (no adjacent-only path)
FORD_X = "ford_harness_x"
FORD_Y = "ford_harness_y"


def _ford_only_pair_defs() -> dict[str, TerritoryDefinition]:
    return {
        FORD_X: TerritoryDefinition(
            id=FORD_X,
            display_name="Ford X",
            terrain_type="grassland",
            adjacent=[],
            produces={"power": 0},
            aerial_adjacent=[FORD_Y],
            ford_adjacent=[FORD_Y],
        ),
        FORD_Y: TerritoryDefinition(
            id=FORD_Y,
            display_name="Ford Y",
            terrain_type="grassland",
            adjacent=[],
            produces={"power": 0},
            aerial_adjacent=[FORD_X],
            ford_adjacent=[FORD_X],
        ),
    }


def _make_unit(state, faction: str, unit_id: str, unit_defs) -> Unit:
    ud = unit_defs.get(unit_id)
    assert ud
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


def test_min_ford_edges_ford_only_link():
    td = _ford_only_pair_defs()
    assert min_ford_edges_for_land_move(FORD_X, FORD_Y, td) == 1
    assert not _has_adjacent_only_land_path(FORD_X, FORD_Y, td)


def test_min_ford_zero_when_detour_exists():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    # Pelennor ↔ South Ithilien also connected via north_ithilien (global min ford = 0)
    assert min_ford_edges_for_land_move("pelennor", "south_ithilien", territory_defs) == 0
    assert _has_adjacent_only_land_path("pelennor", "south_ithilien", territory_defs)
    assert direct_ford_only_land_pair("pelennor", "south_ithilien", territory_defs)
    assert ford_shortcut_requires_escort_lead("pelennor", "south_ithilien", territory_defs)


def test_pelennor_south_ithilien_shortcut_needs_crosser_lead_then_escort_can_follow():
    """M1 escort cannot use the river ford until a ford crosser has the same-phase pending lead; then can."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"
    fx = state.territories["pelennor"]
    fx.units.clear()
    fx.owner = "harad"
    fy_inst = state.territories["south_ithilien"]
    fy_inst.units.clear()
    fy_inst.owner = "gondor"
    fy_inst.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))
    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1])
    r_w, _ = get_reachable_territories_for_unit(
        w1, "pelennor", state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert "south_ithilien" not in r_w
    state.pending_moves = [
        PendingMove(
            from_territory="pelennor",
            to_territory="south_ithilien",
            unit_instance_ids=[mum.instance_id],
            phase="combat_move",
        )
    ]
    r_w2, _ = get_reachable_territories_for_unit(
        w1, "pelennor", state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert "south_ithilien" in r_w2


def test_ford_escort_stack_validation():
    """Warriors cannot use the ford until a ford crosser has a pending move across it; then up to capacity escorted."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}

    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"

    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    # Good owner so combat_move treats destination as enemy (harad is evil; gondor is good)
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    w2 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    w3 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1, w2, w3])

    ids2 = [w1.instance_id, w2.instance_id]
    action_warriors_first = move_units("harad", FORD_X, FORD_Y, ids2)
    v_blocked = validate_action(
        state, action_warriors_first, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert not v_blocked.valid

    action_mum_lead = move_units("harad", FORD_X, FORD_Y, [mum.instance_id])
    v_mum = validate_action(state, action_mum_lead, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v_mum.valid, v_mum.error
    state, _ = apply_action(state, action_mum_lead, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    action_ok = move_units("harad", FORD_X, FORD_Y, ids2)
    v_ok = validate_action(state, action_ok, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v_ok.valid, v_ok.error

    ids3 = [w1.instance_id, w2.instance_id, w3.instance_id]
    action_bad = move_units("harad", FORD_X, FORD_Y, ids3)
    v_bad = validate_action(state, action_bad, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v_bad.valid
    assert "ford escort" in (v_bad.error or "").lower()


def test_ford_crosser_moves_without_spending_escort():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}

    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"

    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    # Good owner so combat_move treats destination as enemy (harad is evil; gondor is good)
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    fx.units.append(mum)

    action = move_units("harad", FORD_X, FORD_Y, [mum.instance_id])
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert land_move_ford_escort_cost_for_instances(
        FORD_X, FORD_Y, [mum.instance_id], state, unit_defs, territory_defs
    ) == 0


def test_pending_ford_move_reserves_capacity():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}

    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"

    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    # Good owner so combat_move treats destination as enemy (harad is evil; gondor is good)
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    w2 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    w3 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1, w2, w3])

    a_mum = move_units("harad", FORD_X, FORD_Y, [mum.instance_id])
    state, _ = apply_action(state, a_mum, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    a1 = move_units("harad", FORD_X, FORD_Y, [w1.instance_id])
    state, _ = apply_action(state, a1, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    rem = remaining_ford_escort_slots(
        state, FORD_X, "harad", unit_defs, territory_defs, state.phase, None
    )
    assert rem == 1

    a2 = move_units("harad", FORD_X, FORD_Y, [w2.instance_id])
    v2 = validate_action(state, a2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v2.valid, v2.error
    state, _ = apply_action(state, a2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    a3 = move_units("harad", FORD_X, FORD_Y, [w3.instance_id])
    v3 = validate_action(state, a3, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v3.valid


def test_warriors_not_reachable_across_ford_until_mum_pending():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"
    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))
    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1])

    r_w, _ = get_reachable_territories_for_unit(
        w1, FORD_X, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert FORD_Y not in r_w
    r_m, _ = get_reachable_territories_for_unit(
        mum, FORD_X, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert FORD_Y in r_m

    state.pending_moves = [
        PendingMove(
            from_territory=FORD_X,
            to_territory=FORD_Y,
            unit_instance_ids=[mum.instance_id],
            phase="combat_move",
        )
    ]
    assert pending_ford_crosser_lead_move_from_origin(
        state, FORD_X, "combat_move", unit_defs, territory_defs
    )
    r_w2, _ = get_reachable_territories_for_unit(
        w1, FORD_X, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert FORD_Y in r_w2


def test_ford_lead_detected_via_primary_unit_id_solo_move():
    """If instance_ids fail lookup, solo ford_crosser primary_unit_id still establishes the lead."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"
    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))
    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1])

    state.pending_moves = [
        PendingMove(
            from_territory=FORD_X,
            to_territory=FORD_Y,
            unit_instance_ids=["stale_or_client_bug_id"],
            phase="combat_move",
            primary_unit_id="war_mumakil",
        )
    ]
    assert pending_ford_crosser_lead_move_from_origin(
        state, FORD_X, "combat_move", unit_defs, territory_defs
    )


def test_ford_crosser_and_warriors_same_move_no_pending_lead():
    """Ford crosser + escorted transportables in one move: no separate lead declaration."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}

    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"

    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))

    mum = _make_unit(state, "harad", "war_mumakil", unit_defs)
    w1 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    w2 = _make_unit(state, "harad", "haradrim_warrior", unit_defs)
    fx.units.extend([mum, w1, w2])

    action_combined = move_units(
        "harad", FORD_X, FORD_Y, [mum.instance_id, w1.instance_id, w2.instance_id]
    )
    v_ok = validate_action(
        state, action_combined, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert v_ok.valid, v_ok.error

    solo_warrior = move_units("harad", FORD_X, FORD_Y, [w1.instance_id])
    v_solo = validate_action(
        state, solo_warrior, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert not v_solo.valid


def test_non_transportable_cannot_cross_ford_only_link():
    """Cavalry without transportable tag cannot use ford-only edges (no escort pool for them)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "combat_move"
    state.current_faction = "harad"
    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "harad"
    fy.owner = "gondor"
    fy.units.append(_make_unit(state, "gondor", "gondor_soldier", unit_defs))
    rider = _make_unit(state, "harad", "khandish_rider", unit_defs)
    assert "transportable" not in (unit_defs.get("khandish_rider").tags or [])
    fx.units.append(rider)
    reachable, _ = get_reachable_territories_for_unit(
        rider, FORD_X, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert FORD_Y not in reachable


def test_aerial_ignores_ford_budget():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    territory_defs = {**territory_defs, **_ford_only_pair_defs()}

    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup=None, camp_defs=camp_defs
    )
    state.phase = "non_combat_move"
    state.current_faction = "elves"

    fx = state.territories[FORD_X]
    fy = state.territories[FORD_Y]
    fx.units.clear()
    fy.units.clear()
    fx.owner = "elves"
    fy.owner = "gondor"

    eagle = _make_unit(state, "elves", "eagle", unit_defs)
    fx.units.append(eagle)
    reachable, _ = get_reachable_territories_for_unit(
        eagle, FORD_X, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    assert FORD_Y in reachable
    cost = land_move_ford_escort_cost_for_instances(
        FORD_X, FORD_Y, [eagle.instance_id], state, unit_defs, territory_defs
    )
    assert cost == 0
