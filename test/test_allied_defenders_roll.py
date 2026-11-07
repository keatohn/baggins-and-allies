"""Allied garrisons are bystanders: they neither force battles nor roll/take enemy hits."""

import pytest

from backend.api.main import _get_active_combat_units
from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.queries import get_contested_territories
from backend.engine.reducer import _land_combat_unit_side, apply_action
from backend.engine.state import ActiveCombat, TerritoryState, Unit
from backend.engine.utils import initialize_game_state

TID = "pelennor"  # non-stronghold land hex (no wall soak, no terrain mods)


def _load():
    ud, td, fd, cd, port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
    )
    return state, ud, td, fd


def _unit(faction: str, unit_id: str, ud) -> Unit:
    d = ud[unit_id]
    return Unit(
        instance_id=f"{faction}_{unit_id}_900",
        unit_id=unit_id,
        remaining_movement=0,
        remaining_health=d.health,
        base_movement=0,
        base_health=d.health,
        loaded_onto=None,
    )


def _hex(units, owner="gondor") -> TerritoryState:
    return TerritoryState(owner=owner, original_owner=owner, units=list(units))


def test_attacker_allied_garrison_is_a_bystander():
    """Harad must not fight Mordor; Gondor and Rohan still defend against Mordor."""
    _, ud, _, fd = _load()
    attacker_alliance = fd["mordor"].alliance

    harad = _unit("harad", "haradrim_warrior", ud)
    rohan = _unit("rohan", "rohan_peasant", ud)
    gondor = _unit("gondor", "gondor_soldier", ud)
    mordor = _unit("mordor", "morgul_orc", ud)

    assert _land_combat_unit_side(harad, "mordor", attacker_alliance, ud, fd) is None
    # Defender-side allies keep defending too (Gondor + Rohan both vs Mordor).
    assert _land_combat_unit_side(rohan, "mordor", attacker_alliance, ud, fd) == "defender"
    assert _land_combat_unit_side(gondor, "mordor", attacker_alliance, ud, fd) == "defender"
    assert _land_combat_unit_side(mordor, "mordor", attacker_alliance, ud, fd) == "attacker"


def test_initiate_combat_rejects_only_allied_garrison():
    import pytest
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    garrison = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex([attacker, garrison], owner="harad")
    state.phase = "combat"
    state.current_faction = "mordor"
    assert get_contested_territories(state, "mordor", fd, ud, td) == []
    with pytest.raises(ValueError):
        apply_action(state, initiate_combat("mordor", TID, {"attacker": [1], "defender": [1]}), ud, td, fd)
    assert state.active_combat is None
    assert state.territories[TID].units == [attacker, garrison]


def test_allied_garrison_survives_enemy_casualties_across_rounds():
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    ally = _unit("harad", "haradrim_warrior", ud)
    enemy = _unit("gondor", "gondor_soldier", ud)
    state.territories[TID] = _hex([attacker, ally, enemy])
    state.phase = "combat"
    state.current_faction = "mordor"
    state, events = apply_action(state, initiate_combat("mordor", TID,
        {"attacker": [10], "defender": [10]}), ud, td, fd)
    assert state.active_combat is not None
    assert state.active_combat.initial_defender_instance_ids == [enemy.instance_id]
    attackers, defenders = _get_active_combat_units(state, td, ud, fd)
    assert [u.instance_id for u in defenders] == [enemy.instance_id]
    state, events = apply_action(state, continue_combat("mordor",
        {"attacker": [1], "defender": [10]}), ud, td, fd)
    survivors = {u.instance_id for u in state.territories[TID].units}
    assert ally.instance_id in survivors
    assert enemy.instance_id not in survivors
    assert ally.remaining_health == ud[ally.unit_id].health


def test_api_active_combat_roster_excludes_attacker_allies():
    """API dice/terror rosters exclude attacker allies, matching the engine."""
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    owner_def = _unit("gondor", "gondor_soldier", ud)
    defender_ally = _unit("rohan", "rohan_peasant", ud)
    attacker_ally = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex(
        [attacker, owner_def, defender_ally, attacker_ally], owner="gondor"
    )
    state.active_combat = ActiveCombat(
        attacker_faction="mordor",
        territory_id=TID,
        attacker_instance_ids=[attacker.instance_id],
        round_number=1,
    )

    attackers, defenders = _get_active_combat_units(state, td, ud, fd)

    assert [u.instance_id for u in attackers] == [attacker.instance_id]
    assert [u.instance_id for u in defenders] == sorted(
        [owner_def.instance_id, defender_ally.instance_id]
    ), "only the enemy alliance defends"


def test_contested_territories_excludes_attacker_allied_garrison():
    """The combat preview must exclude attacker allies from the defender side."""
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    gondor_u = _unit("gondor", "gondor_soldier", ud)
    harad_u = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex([attacker, gondor_u, harad_u], owner="gondor")

    entries = get_contested_territories(state, "mordor", fd, ud, td)
    entry = next((e for e in entries if e["territory_id"] == TID), None)
    assert entry is not None, "hex with attackers + defenders must be listed"
    assert sorted(entry["defender_unit_ids"]) == sorted(
        [gondor_u.instance_id]
    )
    assert entry["attacker_unit_ids"] == [attacker.instance_id]


@pytest.mark.parametrize("owner", [None, "gondor"])
def test_allied_bystander_does_not_block_undefended_capture(owner):
    from backend.engine.actions import move_units

    state, ud, td, fd = _load()
    origin = next(tid for tid in td[TID].adjacent if td[tid].terrain_type not in ("sea", "river"))
    attacker = _unit("mordor", "morgul_orc", ud)
    attacker.remaining_movement = attacker.base_movement = 2
    ally = _unit("harad", "haradrim_warrior", ud)
    state.territories[origin] = _hex([attacker], owner="mordor")
    state.territories[TID] = _hex([ally], owner=owner)
    state.current_faction = "mordor"
    state.phase = "combat_move"
    state, _ = apply_action(state, move_units("mordor", origin, TID, [attacker.instance_id]), ud, td, fd)
    assert state.pending_captures[TID] == "mordor"
    assert ally.instance_id in [u.instance_id for u in state.territories[TID].units]
    assert get_contested_territories(state, "mordor", fd, ud, td) == []
