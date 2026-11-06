"""
Charge-through prompt flags (issue #113): ask which charge route to take only when
the choice matters — more than one route AND at least one route conquers an
intermediate territory. Mirrors the reducer's charge-through capture conditions.

Run:
  .venv/bin/pytest test/test_charge_route_prompts.py
"""

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.queries import charge_route_prompt_flags
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

DEST = "the_black_gate"


def _make_state():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_1.1"
    )
    starting = load_starting_setup(setup_id="wotr_1.1")
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "gondor"
    state.phase = "combat_move"
    # Deterministic ownership for the predicate (independent of starting setup details).
    state.territories["pelennor"].owner = "gondor"  # own
    state.territories["pelennor"].units = []
    state.territories["edoras"].owner = "rohan"  # allied (good)
    state.territories["edoras"].units = []
    state.territories["minas_morgul"].owner = "mordor"  # enemy (evil)
    state.territories["minas_morgul"].units = []
    state.territories["gorgoroth"].owner = "mordor"  # enemy (evil)
    state.territories["gorgoroth"].units = []
    state.territories["weather_hills"].owner = None  # unowned, ownable
    state.territories["weather_hills"].units = []
    # sea_zone_1 stays unownable (ownable=False)
    return state, unit_defs, territory_defs, faction_defs


def _flags(state, routes, unit_defs, territory_defs, faction_defs, unit_id="gondor_knight"):
    return charge_route_prompt_flags(
        state, routes, "gondor", unit_defs[unit_id], territory_defs, faction_defs
    )


def test_all_own_or_allied_vias_do_not_prompt():
    """Every via is own/allied territory: no reason to ask — auto-pick direct/shortest."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["edoras"], ["pelennor"]]}, ud, td, fd)
    assert flags[DEST] is False


def test_one_enemy_via_prompts():
    """Mixed alliances among routes (allied vs enemy) — the choice matters."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["edoras"], ["minas_morgul"]]}, ud, td, fd)
    assert flags[DEST] is True


def test_all_enemy_vias_prompt():
    """All routes conquer something; the player picks which one to take on the way."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["minas_morgul"], ["gorgoroth"]]}, ud, td, fd)
    assert flags[DEST] is True


def test_direct_plus_allied_via_does_not_prompt():
    """Direct and non-direct are equivalent (nothing conquerable) — pick direct silently."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [[], ["edoras"]]}, ud, td, fd)
    assert flags[DEST] is False


def test_direct_plus_enemy_via_prompts():
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [[], ["minas_morgul"]]}, ud, td, fd)
    assert flags[DEST] is True


def test_unownable_via_does_not_prompt():
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["sea_zone_1"], ["edoras"]]}, ud, td, fd)
    assert flags[DEST] is False


def test_unowned_via_prompts():
    """Unowned ownable vias are conquered by the charge (same as enemy)."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["weather_hills"], ["edoras"]]}, ud, td, fd)
    assert flags[DEST] is True


def test_enemy_via_with_units_does_not_conquer():
    """Reducer only captures enemy vias that are empty (friendly vias may hold units)."""
    state, ud, td, fd = _make_state()
    state.territories["minas_morgul"].units = [
        Unit(
            instance_id="mordor_orc_test",
            unit_id="orc_warrior",
            remaining_movement=1,
            remaining_health=1,
            base_movement=1,
            base_health=1,
        )
    ]
    flags = _flags(state, {DEST: [["minas_morgul"], ["edoras"]]}, ud, td, fd)
    assert flags[DEST] is False


def test_single_route_never_prompts():
    """One option = no button (even when it conquers)."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [["minas_morgul"]]}, ud, td, fd)
    assert flags[DEST] is False


def test_unit_that_cannot_conquer_never_prompts():
    """Aerial-only stacks pass through but never capture (mirrors force_can_conquer)."""
    state, ud, td, fd = _make_state()
    flags = _flags(state, {DEST: [[], ["minas_morgul"]]}, ud, td, fd, unit_id="eagle")
    assert flags[DEST] is False
    flags_siege = _flags(state, {DEST: [[], ["minas_morgul"]]}, ud, td, fd, unit_id="trebuchet")
    assert flags_siege[DEST] is False


def test_empty_charge_routes_yield_no_flags():
    state, ud, td, fd = _make_state()
    assert _flags(state, {}, ud, td, fd) == {}


def main():
    test_all_own_or_allied_vias_do_not_prompt()
    test_one_enemy_via_prompts()
    test_all_enemy_vias_prompt()
    test_direct_plus_allied_via_does_not_prompt()
    test_direct_plus_enemy_via_prompts()
    test_unownable_via_does_not_prompt()
    test_unowned_via_prompts()
    test_enemy_via_with_units_does_not_conquer()
    test_single_route_never_prompts()
    test_unit_that_cannot_conquer_never_prompts()
    test_empty_charge_routes_yield_no_flags()
    print("All charge route prompt tests passed.")


if __name__ == "__main__":
    main()
