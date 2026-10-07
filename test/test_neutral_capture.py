"""Walking into an empty ownable neutral territory takes it, however the attack is split into moves."""

import pytest

from backend.engine.actions import end_phase, move_units
from backend.engine.definitions import load_setup, load_starting_setup, load_static_definitions
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "woteas_1.0"
FROM = "weather_hills"
TO = "north_eriador"


@pytest.fixture
def defs():
    ud, td, fd, cd, _ports = load_static_definitions(setup_id=SETUP)
    start = load_starting_setup(setup_id=SETUP)
    rules = load_setup(SETUP).get("special_rules")
    return ud, td, fd, cd, start, rules


def _unit(ud, unit_id, n):
    d = ud[unit_id]
    return Unit(
        instance_id=f"sauron_{unit_id}_{n:03d}",
        unit_id=unit_id,
        remaining_movement=d.movement,
        remaining_health=d.health,
        base_movement=d.movement,
        base_health=d.health,
    )


def _state(defs, unit_ids):
    ud, td, fd, cd, start, rules = defs
    state = initialize_game_state(
        fd, td, ud,
        starting_setup=start,
        camp_defs=cd,
        special_rules=rules,
        rings_of_power=True,
        heroes_enabled=True,
    )
    state.current_faction = "sauron"
    state.phase = "combat_move"
    assert state.territories[TO].owner is None
    assert state.territories[TO].units == []
    src = state.territories[FROM]
    src.owner = "sauron"
    src.units = [_unit(ud, uid, i + 1) for i, uid in enumerate(unit_ids)]
    for ring in state.rings:
        if ring.id == "the_one":
            ring.territory_id = FROM
    return state


def _finish_combat(state, defs):
    ud, td, fd, cd, _start, _rules = defs
    state, _ = apply_action(state, end_phase("sauron"), ud, td, fd, cd, None)
    assert state.phase == "combat"
    state, _ = apply_action(state, end_phase("sauron"), ud, td, fd, cd, None)
    return state


def _move(state, defs, ids, ring_id=None):
    ud, td, fd, cd, _start, _rules = defs
    state, _ = apply_action(
        state, move_units("sauron", FROM, TO, ids, ring_id=ring_id), ud, td, fd, cd, None
    )
    return state


def test_one_big_move_with_sauron_and_the_one_takes_the_territory(defs):
    state = _state(defs, ["sauron", "orc_warrior", "orc_warrior", "armored_troll", "battering_ram"])
    ids = [u.instance_id for u in state.territories[FROM].units]
    state = _move(state, defs, ids, ring_id="the_one")
    state = _finish_combat(state, defs)
    assert state.territories[TO].owner == "sauron"


def test_separate_moves_with_siegework_first_still_take_the_territory(defs):
    state = _state(defs, ["battering_ram", "sauron", "orc_warrior", "orc_warrior"])
    units = state.territories[FROM].units
    ram, hero, *orcs = [u.instance_id for u in units]
    state = _move(state, defs, [ram])
    state = _move(state, defs, [hero], ring_id="the_one")
    state = _move(state, defs, orcs)
    state = _finish_combat(state, defs)
    assert state.territories[TO].owner == "sauron"
