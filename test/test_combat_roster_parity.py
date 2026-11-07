"""
Combat roster parity: continue_combat must classify units like initiate_combat.
Previously, any unit not in surviving_attacker_ids was treated as a defender, so allied
units sharing the attacker's alliance (e.g. Harad with Mordor attacking) became
defenders from round 2 onward, corrupting casualties and end-of-battle unit lists.

Roster rule: attacking faction and subfactions attack; enemies defend;
other units in the attacker's alliance are non-participating bystanders.
"""

import pytest

from backend.engine.definitions import load_static_definitions
from backend.engine.reducer import _land_combat_unit_side, _unit_id_from_instance_id_pattern
from backend.engine.state import Unit


def _u(instance_id: str, unit_id: str, ud) -> Unit:
    d = ud[unit_id]
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=d.movement,
        remaining_health=d.health,
        base_movement=d.movement,
        base_health=d.health,
    )


def test_land_combat_side_matches_initiate_rules():
    unit_defs, _, faction_defs, _, _ = load_static_definitions(setup_id="wotr_exp_1.0")
    attacker_faction = "mordor"
    attacker_alliance = faction_defs[attacker_faction].alliance

    mordor_u = _u("mordor_x", "morgul_orc", unit_defs)
    harad_u = _u("harad_x", "haradrim_archer", unit_defs)
    gondor_u = _u("gondor_x", "gondor_archer", unit_defs)

    assert _land_combat_unit_side(mordor_u, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "attacker"
    # Allied units must never become defenders against their own alliance.
    assert _land_combat_unit_side(harad_u, attacker_faction, attacker_alliance, unit_defs, faction_defs) is None
    assert _land_combat_unit_side(gondor_u, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "defender"


def test_instance_id_pattern_round_trip():
    """generate_unit_instance_id uses faction_unitid_NNN; parser must recover unit_id with underscores."""
    _, _, faction_defs, _, _ = load_static_definitions(setup_id="wotr_exp_1.0")
    fids = list(faction_defs.keys())
    assert _unit_id_from_instance_id_pattern("mordor_morgul_orc_001", fids) == "morgul_orc"
    assert _unit_id_from_instance_id_pattern("gondor_gondor_archer_042", fids) == "gondor_archer"


def test_good_allies_both_defend_against_evil():
    """Gondor + Rohan in the same hex vs Mordor: both are defenders (different faction, different alliance from attacker)."""
    unit_defs, _, faction_defs, _, _ = load_static_definitions(setup_id="wotr_exp_1.0")
    attacker_faction = "mordor"
    attacker_alliance = faction_defs[attacker_faction].alliance

    g = _u("g_x", "gondor_archer", unit_defs)
    r = _u("r_x", "rohan_peasant", unit_defs)
    assert _land_combat_unit_side(g, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "defender"
    assert _land_combat_unit_side(r, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "defender"


@pytest.mark.parametrize("attacker,ally,attacking_unit,allied_unit,enemy_unit", [
    ("mordor", "harad", "morgul_orc", "haradrim_archer", "gondor_archer"),
    ("gondor", "rohan", "gondor_archer", "rohan_peasant", "morgul_orc"),
])
def test_allied_cooccupation_is_not_contested_and_cannot_start_battle(
    attacker, ally, attacking_unit, allied_unit, enemy_unit
):
    from backend.engine.definitions import load_starting_setup
    from backend.engine.utils import initialize_game_state
    from backend.engine.queries import get_contested_territories, validate_action
    from backend.engine.actions import initiate_combat, end_phase
    from backend.engine.reducer import apply_action

    ud, td, fd, cd, ports = load_static_definitions(setup_id="wotr_exp_1.0")
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud,
        starting_setup=load_starting_setup(setup_id="wotr_exp_1.0"), camp_defs=cd)
    state.phase = "combat"
    state.current_faction = attacker
    for territory in state.territories.values():
        territory.units = []
    territory = state.territories["minas_tirith"]
    territory.owner = ally
    territory.units = [_u("attacker_x", attacking_unit, ud), _u("ally_x", allied_unit, ud)]
    assert get_contested_territories(state, attacker, fd, ud, td) == []
    assert validate_action(state, end_phase(attacker), ud, td, fd, cd, ports).valid
    action = initiate_combat(attacker, "minas_tirith", {})
    validation = validate_action(state, action, ud, td, fd, cd, ports)
    assert not validation.valid
    assert "No enemy" in validation.error
    with pytest.raises(ValueError):
        apply_action(state, action, ud, td, fd, cd, ports)
    territory.units.append(_u("enemy_x", enemy_unit, ud))
    battles = get_contested_territories(state, attacker, fd, ud, td)
    assert len(battles) == 1
    assert battles[0]["attacker_unit_ids"] == ["attacker_x"]
    assert battles[0]["defender_unit_ids"] == ["enemy_x"]
    assert validate_action(state, action, ud, td, fd, cd, ports).valid
