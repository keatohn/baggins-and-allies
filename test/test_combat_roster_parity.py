"""
Combat roster parity: continue_combat must classify units like initiate_combat.
Previously, any unit not in surviving_attacker_ids was treated as a defender, so allied
units sharing the attacker's alliance (e.g. Harad with Mordor attacking) became
defenders from round 2 onward, corrupting casualties and end-of-battle unit lists.
"""

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
