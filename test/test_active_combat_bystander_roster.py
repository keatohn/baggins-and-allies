"""
Regression: _get_active_combat_units must exclude same-alliance bystanders.

The reducer classifies rosters with _land_combat_unit_side (None = bystander:
same-alliance non-attacker or no-faction). The API previously defined defenders
as "not in attacker_instance_ids", so an allied faction stacked in the battle hex
was rolled as a defender — inflating dice generation and round-1 terror hit totals
that are then passed to the reducer as defender_hits_override.
"""

from backend.api.main import _get_active_combat_units
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import _land_combat_unit_side
from backend.engine.state import ActiveCombat, TerritoryState, Unit
from backend.engine.utils import initialize_game_state


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


def _unit(instance_id: str, unit_id: str, ud) -> Unit:
    d = ud[unit_id]
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=0,
        remaining_health=d.health,
        base_movement=0,
        base_health=d.health,
        loaded_onto=None,
    )


def test_bystander_excluded_from_api_defender_roster():
    """Harad (evil) bystander on a Mordor-attack hex is not a defender for dice/terror."""
    state, ud, td, fd = _load()
    tid = "pelennor"
    if tid not in state.territories:
        tid = next(t for t in state.territories if t in td)

    attacker = _unit("mordor_orc_001", "morgul_orc", ud)
    defender = _unit("gondor_archer_001", "gondor_archer", ud)
    bystander = _unit("harad_archer_001", "haradrim_archer", ud)
    terr = TerritoryState(owner="gondor", original_owner="gondor", units=[attacker, defender, bystander])
    state.territories[tid] = terr
    state.active_combat = ActiveCombat(
        attacker_faction="mordor",
        territory_id=tid,
        attacker_instance_ids=[attacker.instance_id],
        round_number=1,
    )

    attackers, defenders = _get_active_combat_units(state, td, ud, fd)

    assert [u.instance_id for u in attackers] == [attacker.instance_id]
    assert [u.instance_id for u in defenders] == [defender.instance_id], (
        "same-alliance bystander must not appear as a defender"
    )
    # Engine side agrees
    aa = fd["mordor"].alliance
    assert _land_combat_unit_side(bystander, "mordor", aa, ud, fd) is None
    assert _land_combat_unit_side(defender, "mordor", aa, ud, fd) == "defender"


def test_enemy_allies_both_still_defend():
    """Gondor + Rohan (both good) vs Mordor: both remain defenders."""
    state, ud, td, fd = _load()
    tid = "pelennor"
    if tid not in state.territories:
        tid = next(t for t in state.territories if t in td)

    attacker = _unit("mordor_orc_001", "morgul_orc", ud)
    gondor_u = _unit("gondor_archer_001", "gondor_archer", ud)
    rohan_u = _unit("rohan_peasant_001", "rohan_peasant", ud)
    terr = TerritoryState(owner="gondor", original_owner="gondor", units=[attacker, gondor_u, rohan_u])
    state.territories[tid] = terr
    state.active_combat = ActiveCombat(
        attacker_faction="mordor",
        territory_id=tid,
        attacker_instance_ids=[attacker.instance_id],
        round_number=1,
    )

    attackers, defenders = _get_active_combat_units(state, td, ud, fd)
    def_ids = {u.instance_id for u in defenders}
    assert def_ids == {gondor_u.instance_id, rohan_u.instance_id}


def test_attacker_roster_excludes_bystander_in_attacker_ids_edge():
    """A bystander id mistakenly in attacker_instance_ids is not rolled as attacker."""
    state, ud, td, fd = _load()
    tid = "pelennor"
    if tid not in state.territories:
        tid = next(t for t in state.territories if t in td)

    attacker = _unit("mordor_orc_001", "morgul_orc", ud)
    bystander = _unit("harad_archer_001", "haradrim_archer", ud)
    defender = _unit("gondor_archer_001", "gondor_archer", ud)
    terr = TerritoryState(owner="gondor", original_owner="gondor", units=[attacker, bystander, defender])
    state.territories[tid] = terr
    # Corrupt roster: bystander listed as attacker (should not happen; defense-in-depth)
    state.active_combat = ActiveCombat(
        attacker_faction="mordor",
        territory_id=tid,
        attacker_instance_ids=[attacker.instance_id, bystander.instance_id],
        round_number=1,
    )

    attackers, defenders = _get_active_combat_units(state, td, ud, fd)
    assert [u.instance_id for u in attackers] == [attacker.instance_id]
    assert bystander.instance_id not in {u.instance_id for u in attackers}
    assert bystander.instance_id not in {u.instance_id for u in defenders}
