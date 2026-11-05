"""
Allied garrisons defend the territory (game rule).

Every factioned unit in the contested hex that is not part of the attacking force
defends it — including garrisons from a faction allied with the attacker. They roll
and take hits; they are not bystanders.

Previously the engine classified same-alliance units as bystanders (excluded from
defender_units) while the API rolled them as defenders, so allied garrisons sat out
the battle and API/engine disagreed on dice and terror hit totals.
"""

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


def test_attacker_allied_garrison_is_a_defender():
    """Harad (evil, attacker's ally) on the defending hex defends — not a bystander."""
    _, ud, _, fd = _load()
    attacker_alliance = fd["mordor"].alliance

    harad = _unit("harad", "haradrim_warrior", ud)
    rohan = _unit("rohan", "rohan_peasant", ud)
    gondor = _unit("gondor", "gondor_soldier", ud)
    mordor = _unit("mordor", "morgul_orc", ud)

    assert _land_combat_unit_side(harad, "mordor", attacker_alliance, ud, fd) == "defender"
    # Defender-side allies keep defending too (Gondor + Rohan both vs Mordor).
    assert _land_combat_unit_side(rohan, "mordor", attacker_alliance, ud, fd) == "defender"
    assert _land_combat_unit_side(gondor, "mordor", attacker_alliance, ud, fd) == "defender"
    assert _land_combat_unit_side(mordor, "mordor", attacker_alliance, ud, fd) == "attacker"


def test_initiate_combat_fights_an_attacker_allied_garrison():
    """A hex whose only defender is the attacker's ally must start a battle with it defending.

    Previously this raised "No defending units" (same-alliance units were bystanders),
    so an allied garrison could neither roll nor take hits.
    """
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    garrison = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex([attacker, garrison], owner="gondor")
    state.phase = "combat"
    state.current_faction = "mordor"

    # All-miss dice keep both sides alive after the initiate round.
    dice = {"attacker": [10], "defender": [10]}

    state, events = apply_action(
        state, initiate_combat("mordor", TID, dice), ud, td, fd
    )

    started = [e for e in events if e.type == "combat_started"]
    assert started, "combat must start against an allied garrison"
    assert started[0].payload["defender_units"] == [garrison.instance_id]

    assert state.active_combat is not None
    assert state.active_combat.initial_defender_instance_ids == [garrison.instance_id]


def test_allied_garrison_takes_hits_as_a_defender():
    """The allied garrison is on the defender shelf: attacker hits land on it."""
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    garrison = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex([attacker, garrison], owner="gondor")
    state.phase = "combat"
    state.current_faction = "mordor"

    dice = {"attacker": [10], "defender": [10]}
    state, events = apply_action(
        state, initiate_combat("mordor", TID, dice), ud, td, fd
    )
    assert state.active_combat is not None

    # Attacker roll 1 <= attack 2 -> 1 hit; haradrim_warrior has 1 HP -> destroyed.
    state, events = apply_action(
        state,
        continue_combat("mordor", {"attacker": [1], "defender": [10]}),
        ud, td, fd,
    )

    round_events = [e for e in events if e.type == "combat_round_resolved"]
    assert round_events, "a round must resolve"
    casualties = round_events[-1].payload.get("defender_casualties") or []
    assert garrison.instance_id in casualties, (
        "allied garrison must take hits as a defender"
    )
    # Attacker survives: the garrison fought back and lost, it did not sit out.
    assert attacker.instance_id in [
        u.instance_id for u in state.territories[TID].units
    ] or any(
        attacker.instance_id in (e.payload.get("attacker_units") or [])
        for e in events
        if e.type == "combat_ended"
    )


def test_api_active_combat_roster_includes_allied_defenders():
    """API dice/terror rosters must include allied garrisons (same as the engine)."""
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
        [owner_def.instance_id, defender_ally.instance_id, attacker_ally.instance_id]
    ), "owner, defender-ally, and attacker-ally garrisons all defend"


def test_contested_territories_lists_allied_garrison_as_defender():
    """The combat preview must show allied garrisons on the defender side."""
    state, ud, td, fd = _load()
    attacker = _unit("mordor", "morgul_orc", ud)
    gondor_u = _unit("gondor", "gondor_soldier", ud)
    harad_u = _unit("harad", "haradrim_warrior", ud)
    state.territories[TID] = _hex([attacker, gondor_u, harad_u], owner="gondor")

    entries = get_contested_territories(state, "mordor", fd, ud, td)
    entry = next((e for e in entries if e["territory_id"] == TID), None)
    assert entry is not None, "hex with attackers + defenders must be listed"
    assert sorted(entry["defender_unit_ids"]) == sorted(
        [gondor_u.instance_id, harad_u.instance_id]
    )
    assert entry["attacker_unit_ids"] == [attacker.instance_id]
