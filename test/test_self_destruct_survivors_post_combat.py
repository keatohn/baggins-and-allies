"""
Regression: bomb has tag self_destruct but must not be purged at combat end if it survived the round.

Paired bombikazi self-destruct is handled via bombikazi_self_destruct_ids in resolve_combat_round.
End-of-combat must not strip all self_destruct-tagged survivors (breaks fuse_bomb=False stronghold flow).
"""
import pytest

from backend.engine.state import Unit
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import initiate_combat
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state


def _make_unit(state, faction: str, unit_id: str, unit_defs) -> Unit:
    ud = unit_defs.get(unit_id)
    assert ud, f"missing unit {unit_id}"
    return Unit(
        instance_id=state.generate_unit_instance_id(faction, unit_id),
        unit_id=unit_id,
        remaining_movement=ud.movement,
        remaining_health=ud.health,
        base_movement=ud.movement,
        base_health=ud.health,
    )


@pytest.fixture
def defs():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    return unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def test_unpaired_bomb_survives_attacker_win_plain_territory(defs):
    """Unpaired bomb (self_destruct tag, 0 dice) stays on the hex after conquer with another attacker."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    t = state.territories["gap_of_rohan"]
    t.units.clear()
    t.owner = "rohan"
    defender = _make_unit(state, "rohan", "rohan_peasant", unit_defs)
    bomb = _make_unit(state, "isengard", "bomb", unit_defs)
    uruk = _make_unit(state, "isengard", "urukhai_warrior", unit_defs)
    t.units.extend([defender, bomb, uruk])
    state.current_faction = "isengard"
    state.phase = "combat"

    action = initiate_combat(
        "isengard",
        "gap_of_rohan",
        {"attacker": [1], "defender": [6]},
        fuse_bomb=False,
    )
    state, _ = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    assert state.active_combat is None
    # apply_action deep-copies state; use territory from the returned state.
    t_after = state.territories["gap_of_rohan"]
    ids_on_hex = {u.unit_id for u in t_after.units}
    assert "bomb" in ids_on_hex
    assert "urukhai_warrior" in ids_on_hex
    assert "rohan_peasant" not in ids_on_hex
