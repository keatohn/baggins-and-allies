"""
Movement tests: land combat move, non-combat move, and validation.
Run without creating games in the UI.

  PYTHONPATH=. .venv/bin/python test/test_movement.py

Covers:
- Land combat move (friendly -> enemy territory)
- Non-combat move (friendly -> friendly)
- Validation that destination is required (engine accepts "to" or "to_territory")
"""

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import move_units, end_phase
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state, print_game_state
from backend.engine.queries import validate_action


def test_land_combat_move():
    """Land combat move: north_ithilien (gondor) -> minas_morgul (mordor, enemy)."""
    print("=" * 60)
    print("TEST: Land combat move (north_ithilien -> minas_morgul)")
    print("=" * 60)

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="1.0")
    starting = load_starting_setup(setup_id="1.0")
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
    print(f"  OK: Pending move {pm.from_territory} -> {pm.to_territory}, {len(pm.unit_instance_ids)} units")


def test_non_combat_move():
    """Non-combat move: same from/to, different phase."""
    print("=" * 60)
    print("TEST: Non-combat move (minas_tirith -> pelennor)")
    print("=" * 60)

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="1.0")
    starting = load_starting_setup(setup_id="1.0")
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

    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id="1.0")
    starting = load_starting_setup(setup_id="1.0")
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


def main():
    test_land_combat_move()
    test_non_combat_move()
    test_move_requires_destination()
    print("\n" + "=" * 60)
    print("All movement tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
