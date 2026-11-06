"""
Movement tests: land combat move, non-combat move, and validation.
Run without creating games in the UI.

  PYTHONPATH=. .venv/bin/python test/test_movement.py

Covers:
- Land combat move (friendly -> enemy territory)
- Non-combat move (friendly -> friendly)
- Validation that destination is required (engine accepts "to" or "to_territory")
"""

from dataclasses import replace

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import move_units, end_phase
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state, print_game_state
from backend.engine.queries import validate_action
from backend.engine.movement import get_reachable_territories_for_unit, path_traversable_for_charging
from backend.engine.state import Unit


def test_non_combat_reaches_territory_with_pending_capture_owner():
    """
    After combat/conquest, territory.owner may still be None while pending_captures holds the capturer
    until combat phase fully applies. Non-combat reachability must treat that hex as friendly so
    reinforcements can be plotted (same as validation / apply).
    """
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    dest = "minas_morgul"
    origin = "north_ithilien"
    # Empty conquered hex: not yet flushed to territory.owner
    state.territories[dest].units = []
    state.territories[dest].owner = None
    state.pending_captures[dest] = "gondor"

    units = [u for u in state.territories[origin].units if getattr(u, "remaining_movement", 0) >= 1]
    assert units, "need a gondor unit in north_ithilien"
    u = units[0]
    u.remaining_movement = 2

    reachable, _ = get_reachable_territories_for_unit(
        u, origin, state, unit_defs, territory_defs, faction_defs, "non_combat_move",
    )
    assert dest in reachable, "pending capture should count as friendly for non-combat destinations"


def test_non_combat_apply_accepts_pending_capture_owner():
    """Reducer apply must use effective_territory_owner for non-combat (same as validate_action / BFS)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    dest = "minas_morgul"
    origin = "north_ithilien"
    state.territories[dest].units = []
    state.territories[dest].owner = None
    state.pending_captures[dest] = "gondor"

    units = [u for u in state.territories[origin].units if getattr(u, "remaining_movement", 0) >= 1]
    assert units, "need a gondor unit in north_ithilien"
    u = units[0]
    u.remaining_movement = 2
    action = move_units("gondor", origin, dest, [u.instance_id])
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state2, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state2.pending_moves) == 1


def test_land_combat_move():
    """Land combat move: north_ithilien (gondor) -> minas_morgul (mordor, enemy)."""
    print("=" * 60)
    print("TEST: Land combat move (north_ithilien -> minas_morgul)")
    print("=" * 60)

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "combat_move"

    from_territory = "north_ithilien"
    to_territory = "minas_morgul"
    units = [u for u in state.territories[from_territory].units if getattr(u, "remaining_movement", 1) >= 1]
    assert units, f"{from_territory} should have starting units with movement"
    instance_ids = [u.instance_id for u in units[:2]]

    action = move_units("gondor", from_territory, to_territory, instance_ids)
    validation = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert validation.valid, f"Move should be valid: {validation.error}"

    state, events = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 1, "Should have one pending move"
    pm = state.pending_moves[0]
    assert pm.from_territory == from_territory and pm.to_territory == to_territory
    assert pm.primary_unit_id == units[0].unit_id, "API/UI must get canonical unit_id without parsing instance ids"
    print(f"  OK: Pending move {pm.from_territory} -> {pm.to_territory}, {len(pm.unit_instance_ids)} units")


def test_non_combat_move():
    """Non-combat move: same from/to, different phase."""
    print("=" * 60)
    print("TEST: Non-combat move (minas_tirith -> pelennor)")
    print("=" * 60)

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    from_territory = "minas_tirith"
    to_territory = "pelennor"
    units = [u for u in state.territories[from_territory].units if getattr(u, "remaining_movement", 1) >= 1]
    assert units, "minas_tirith should have units with movement"
    instance_ids = [u.instance_id for u in units[:1]]

    action = move_units("gondor", from_territory, to_territory, instance_ids)
    validation = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert validation.valid, f"Move should be valid: {validation.error}"

    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 1
    assert state.pending_moves[0].to_territory == to_territory
    print(f"  OK: Non-combat pending move to {to_territory}")


def test_move_requires_destination():
    """Validation rejects move when destination is missing (payload keys: to / to_territory)."""
    print("=" * 60)
    print("TEST: Move requires destination (validation)")
    print("=" * 60)

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "combat_move"

    from backend.engine.actions import Action

    # Action with "to" (engine style) – should be valid when present (combat: need enemy dest)
    movable = [u for u in state.territories["north_ithilien"].units if getattr(u, "remaining_movement", 1) >= 1]
    instance_ids = [u.instance_id for u in movable[:1]]
    action_ok = Action(
        type="move_units",
        faction="gondor",
        payload={
            "from": "north_ithilien",
            "to": "minas_morgul",
            "unit_instance_ids": instance_ids,
        },
    )
    v_ok = validate_action(state, action_ok, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v_ok.valid, f"Expected valid when to is set: {v_ok.error}"

    # Action with "to_territory" (API style) – should also be valid
    action_api = Action(
        type="move_units",
        faction="gondor",
        payload={
            "from_territory": "north_ithilien",
            "to_territory": "minas_morgul",
            "unit_instance_ids": instance_ids,
        },
    )
    v_api = validate_action(state, action_api, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v_api.valid, f"Expected valid when to_territory is set: {v_api.error}"

    # Missing destination – should be invalid
    action_no_dest = Action(
        type="move_units",
        faction="gondor",
        payload={
            "from": "north_ithilien",
            "unit_instance_ids": instance_ids,
        },
    )
    v_bad = validate_action(state, action_no_dest, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert not v_bad.valid and "destination" in v_bad.error.lower(), f"Expected invalid when destination missing: {v_bad}"
    print(f"  OK: Validation rejects missing destination: {v_bad.error}")


def test_move_charges_hostility_aware_distance_enemy_land_shortcut():
    """Issue #50 M5: geometric shortest path cuts through enemy land; charge the
    hostility-aware reachability distance (4), not the pure-graph distance (3)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "combat_move"

    origin, dest = "north_ithilien", "west_rhun"
    u = next(x for x in state.territories[origin].units if x.unit_id == "ithilien_ranger")
    u.remaining_movement = 4

    # Geometric path north_ithilien -> dagorlad (neutral) -> rhun_hills (enemy) -> west_rhun
    # costs 3; pathfinding must detour via friendly land for a total of 4.
    reachable, _ = get_reachable_territories_for_unit(
        u, origin, state, unit_defs, territory_defs, faction_defs, "combat_move",
    )
    assert reachable.get(dest) == 4, f"expected hostility-aware distance 4, got {reachable.get(dest)}"

    action = move_units("gondor", origin, dest, [u.instance_id], move_type="land")
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 1
    state, _ = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    moved = next(x for x in state.territories[dest].units if x.instance_id == u.instance_id)
    # Charged 4 of 4: geometric charging would leave 1 movement remaining.
    assert moved.remaining_movement == 0, (
        f"expected charge of 4 (remaining 0), got remaining={moved.remaining_movement}"
    )


def test_move_charges_hostility_aware_distance_water_shortcut():
    """Issue #50 M5: land unit's geometric path crosses a sea zone it cannot enter;
    charge the reachability detour (4), not the geometric distance (2)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "combat_move"

    origin, dest = "west_osgiliath", "harondor"
    u = next(x for x in state.territories[origin].units if x.unit_id == "gondor_soldier")
    u.remaining_movement = 4

    # Geometric path west_osgiliath -> sea_zone_12 -> harondor costs 2 but a land unit
    # cannot enter the water hex; the land detour costs 4.
    reachable, _ = get_reachable_territories_for_unit(
        u, origin, state, unit_defs, territory_defs, faction_defs, "combat_move",
    )
    assert reachable.get(dest) == 4, f"expected hostility-aware distance 4, got {reachable.get(dest)}"

    action = move_units("gondor", origin, dest, [u.instance_id], move_type="land")
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 1
    state, _ = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs)

    moved = next(x for x in state.territories[dest].units if x.instance_id == u.instance_id)
    # Charged 4 of 4: geometric charging would leave 2 movement remaining.
    assert moved.remaining_movement == 0, (
        f"expected charge of 4 (remaining 0), got remaining={moved.remaining_movement}"
    )


def test_sail_charges_hostility_aware_distance_hostile_sea():
    """Issue #50 M5: NCM sail whose geometric path crosses a hostile sea zone;
    charge the reachability detour (3), not the geometric distance (2).
    Checked on the simulated post-pending state — leaving NCM resets movement."""
    from backend.engine.reducer import get_state_after_pending_moves
    from backend.engine.state import Unit

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "non_combat_move"

    origin, dest = "sea_zone_2", "sea_zone_5"
    ship_def = unit_defs["gondor_ship"]
    ship = Unit("gondor_ship_t2", "gondor_ship", ship_def.movement, ship_def.health, ship_def.movement, ship_def.health, None)
    ship.remaining_movement = 3
    state.territories[origin].units.append(ship)
    enemy = Unit("mordor_black_ship_t2", "black_ship", 3, 3, 3, 3, None)
    state.territories["sea_zone_3"].units.append(enemy)

    # Geometric path sea_zone_2 -> sea_zone_3 (hostile ship) -> sea_zone_5 costs 2;
    # the ship must detour around sea_zone_3 for a total of 3.
    reachable, _ = get_reachable_territories_for_unit(
        ship, origin, state, unit_defs, territory_defs, faction_defs, "non_combat_move",
    )
    assert reachable.get(dest) == 3, f"expected hostility-aware distance 3, got {reachable.get(dest)}"

    action = move_units("gondor", origin, dest, [ship.instance_id], move_type="sail")
    v = validate_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert v.valid, v.error
    state, _ = apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    assert len(state.pending_moves) == 1

    sim = get_state_after_pending_moves(state, "non_combat_move", unit_defs, territory_defs, faction_defs)
    moved = next(x for x in sim.territories[dest].units if x.instance_id == ship.instance_id)
    # Charged 3 of 3: geometric charging would leave 1 movement remaining.
    assert moved.remaining_movement == 0, (
        f"expected charge of 3 (remaining 0), got remaining={moved.remaining_movement}"
    )


def _board(faction: str, phase: str):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="wotr_1.1")
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = faction
    state.phase = phase
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def test_non_combat_cannot_cross_neutral_land():
    """Neutral land is not a non-combat corridor: empty or enemy-held.

    Grey Havens -> Tower Hills (empty ownable) -> The Shire must not be offered
    when a 1-move unit has the Marching Orders second point. An adjacent empty
    unownable is still a place to stop, and movement does not continue past it.
    """
    state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs = _board(
        "freepeoples", "non_combat_move"
    )
    origin = "grey_havens"
    warrior = next(u for u in state.territories[origin].units if u.unit_id == "rivendell_warrior")
    warrior.remaining_movement = 2

    reachable, _ = get_reachable_territories_for_unit(
        warrior, origin, state, unit_defs, territory_defs, faction_defs, "non_combat_move",
    )
    assert "tower_hills" not in reachable
    assert "the_shire" not in reachable
    assert "forlindon" in reachable  # friendly, still in range

    action = move_units("freepeoples", origin, "the_shire", [warrior.instance_id])
    rejected = validate_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert not rejected.valid

    # Hostile neutral (goblins in Carrock) is the same wall, not only empty neutrals.
    state.current_faction = "freepeoples"
    elves = [u for u in state.territories["rivendell"].units if u.unit_id == "rivendell_warrior"]
    assert elves
    elf = elves[0]
    elf.remaining_movement = 2
    state.territories["gundabad"].owner = "freepeoples"
    state.territories["gundabad"].units = []
    reachable, _ = get_reachable_territories_for_unit(
        elf, "rivendell", state, unit_defs, territory_defs, faction_defs, "non_combat_move",
    )
    assert "carrock" not in reachable
    assert "gundabad" not in reachable

    # Adjacent empty unownable land: stop there, do not step through to the far side.
    gate = territory_defs["minas_morgul"]
    previous_ownable = gate.ownable
    try:
        gate.ownable = False
        state.current_faction = "gondor"
        state.territories["minas_morgul"].owner = None
        state.territories["minas_morgul"].units = []
        state.territories["cirith_ungol"].owner = "gondor"
        state.territories["cirith_ungol"].units = []
        ranger = next(u for u in state.territories["north_ithilien"].units if u.remaining_movement >= 1)
        ranger.remaining_movement = 2
        reachable, _ = get_reachable_territories_for_unit(
            ranger, "north_ithilien", state, unit_defs, territory_defs, faction_defs, "non_combat_move",
        )
        assert reachable.get("minas_morgul") == 1
        assert "cirith_ungol" not in reachable
    finally:
        gate.ownable = previous_ownable


def test_non_combat_charge_does_not_treat_neutral_land_as_a_shortcut():
    state, unit_defs, territory_defs, faction_defs, _, _ = _board("freepeoples", "non_combat_move")
    warrior = next(u for u in state.territories["grey_havens"].units if u.unit_id == "rivendell_warrior")
    path = ["grey_havens", "tower_hills", "the_shire"]
    assert path_traversable_for_charging(
        path, state, [warrior], unit_defs, territory_defs, faction_defs, "freepeoples", "non_combat_move",
    ) is False
    assert path_traversable_for_charging(
        path, state, [warrior], unit_defs, territory_defs, faction_defs, "freepeoples", "combat_move",
    ) is True


def test_aerial_non_combat_still_crosses_neutral_land():
    """Flyers keep their corridor over neutral land. Ground units in the same spot do not."""
    state, unit_defs, territory_defs, faction_defs, _, _ = _board("mordor", "non_combat_move")
    state.territories["north_ithilien"].owner = "mordor"
    state.territories["minas_morgul"].owner = None
    state.territories["minas_morgul"].units = [
        Unit("mordor_orc_wall", "orc_warrior", 1, 1, 1, 1, None)
    ]
    state.territories["cirith_ungol"].owner = "mordor"
    state.territories["cirith_ungol"].units = []
    nazgul_def = unit_defs["nazgul"]
    nazgul = Unit(
        "mordor_nazgul_t", "nazgul",
        2, nazgul_def.health, 2, nazgul_def.health, None,
    )
    state.territories["north_ithilien"].units.append(nazgul)
    origin_def = territory_defs["north_ithilien"]
    saved_aerial = list(getattr(origin_def, "aerial_adjacent", []) or [])
    try:
        # Drop the direct fly-over so Cirith Ungol is two steps, across Minas Morgul.
        origin_def.aerial_adjacent = []
        reachable, _ = get_reachable_territories_for_unit(
            nazgul, "north_ithilien", state, unit_defs, territory_defs, faction_defs, "non_combat_move",
        )
    finally:
        origin_def.aerial_adjacent = saved_aerial
    assert reachable.get("cirith_ungol") == 2
    assert "minas_morgul" not in reachable


def test_charging_special_conquers_empty_enemy_land_on_the_way():
    """Infantry with the charging special charge like cavalry. Infantry without it stop."""
    state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs = _board(
        "gondor", "combat_move"
    )
    unit_defs = dict(unit_defs)
    soldier = unit_defs["gondor_soldier"]
    unit_defs["plain"] = replace(soldier, id="plain", movement=2, specials=[])
    unit_defs["charger"] = replace(soldier, id="charger", movement=2, specials=["charging"])
    # Cavalry with the special removed still charges by archetype.
    unit_defs["gondor_knight"] = replace(unit_defs["gondor_knight"], specials=[])

    state.territories["pelennor"].owner = "mordor"
    state.territories["pelennor"].units = []
    state.territories["west_osgiliath"].owner = "mordor"
    if not state.territories["west_osgiliath"].units:
        state.territories["west_osgiliath"].units = [
            Unit("mordor_hold", "orc_warrior", 0, 1, 0, 1, None)
        ]
    plain = Unit("plain_1", "plain", 2, 1, 2, 1, None)
    charger = Unit("charger_1", "charger", 2, 1, 2, 1, None)
    knight = Unit("knight_1", "gondor_knight", 2, 1, 2, 1, None)
    state.territories["minas_tirith"].units.extend([plain, charger, knight])

    def routes_for(unit):
        _reachable, routes = get_reachable_territories_for_unit(
            unit, "minas_tirith", state, unit_defs, territory_defs, faction_defs, "combat_move",
        )
        return _reachable, routes

    plain_reach, plain_routes = routes_for(plain)
    assert "west_osgiliath" not in plain_reach
    assert plain_routes.get("west_osgiliath", []) == []

    charger_reach, charger_routes = routes_for(charger)
    assert charger_reach.get("west_osgiliath") == 2
    assert ["pelennor"] in charger_routes["west_osgiliath"]

    _knight_reach, knight_routes = routes_for(knight)
    assert ["pelennor"] in knight_routes["west_osgiliath"]

    rejected = validate_action(
        state,
        move_units("gondor", "minas_tirith", "west_osgiliath", ["plain_1"], charge_through=["pelennor"]),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert not rejected.valid

    accepted = validate_action(
        state,
        move_units("gondor", "minas_tirith", "west_osgiliath", ["charger_1"], charge_through=["pelennor"]),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert accepted.valid, accepted.error
    state, _ = apply_action(
        state,
        move_units("gondor", "minas_tirith", "west_osgiliath", ["charger_1"], charge_through=["pelennor"]),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    state, _ = apply_action(
        state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    captured = state.territories["pelennor"].owner == "gondor" or state.pending_captures.get("pelennor") == "gondor"
    assert captured, "charging through empty Pelennor should conquer it"


def main():
    test_land_combat_move()
    test_non_combat_move()
    test_move_requires_destination()
    test_move_charges_hostility_aware_distance_enemy_land_shortcut()
    test_move_charges_hostility_aware_distance_water_shortcut()
    test_sail_charges_hostility_aware_distance_hostile_sea()
    test_non_combat_cannot_cross_neutral_land()
    test_non_combat_charge_does_not_treat_neutral_land_as_a_shortcut()
    test_aerial_non_combat_still_crosses_neutral_land()
    test_charging_special_conquers_empty_enemy_land_on_the_way()
    print("\n" + "=" * 60)
    print("All movement tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
