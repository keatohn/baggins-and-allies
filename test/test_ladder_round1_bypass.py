"""
H2 (issue #51): ladder climbers must bypass stronghold walls in the round 1
resolved during initiate_combat, even when no dedicated siegeworks round runs.

Ladder equipment contributes 0 dice, so ladder+infantry attacks have
siege_needed_at_start=False and open directly with round 1. That round must
still pass the ladder instance ids to resolve_combat_round (climber hits soak
defenders, not the wall) and the ActiveCombat created afterwards must store the
ladder assignment instead of an empty list.

Run: .venv/bin/pytest test/test_ladder_round1_bypass.py
"""

from backend.engine.actions import initiate_combat
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_1.1"
TERRITORY_ID = "rivendell"  # stronghold, base HP 2, forest terrain


def _make_unit(state, faction: str, unit_id: str, unit_defs) -> Unit:
    ud = unit_defs[unit_id]
    return Unit(
        instance_id=state.generate_unit_instance_id(faction, unit_id),
        unit_id=unit_id,
        remaining_movement=ud.movement,
        remaining_health=ud.health,
        base_movement=ud.movement,
        base_health=ud.health,
    )


def _run_initiate(with_ladder: bool, defender_rolls: list[int]):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting, camp_defs=camp_defs,
    )
    t = state.territories[TERRITORY_ID]
    t.units.clear()
    t.owner = "rohan"

    units = []
    ladder = None
    if with_ladder:
        ladder = _make_unit(state, "isengard", "siege_ladder", unit_defs)
        units.append(ladder)
    climber = _make_unit(state, "isengard", "urukhai_warrior", unit_defs)  # climbs_ladder
    footman = _make_unit(state, "isengard", "dunland_wildman", unit_defs)
    units.extend([climber, footman])
    units.extend(_make_unit(state, "gondor", "gondor_soldier", unit_defs) for _ in range(2))
    t.units.extend(units)

    state.current_faction = "isengard"
    state.phase = "combat"
    # Attacker: climber + footman roll (ladder excluded as siegework) -> 2 rolls, both hit.
    # Defender: 2 soldiers -> 2 rolls, both miss.
    action = initiate_combat(
        "isengard", TERRITORY_ID,
        {"attacker": [1, 1], "defender": defender_rolls},
    )
    state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    return state, events, ladder, climber


def _round1_event(events):
    rounds = [e for e in events if e.type == "combat_round_resolved"]
    assert len(rounds) == 1, f"expected one round-1 event, got {len(rounds)}"
    assert rounds[0].payload["round_number"] == 1
    return rounds[0].payload


def test_ladder_climber_bypasses_wall_in_initiate_round1():
    state, events, ladder, climber = _run_initiate(with_ladder=True, defender_rolls=[6, 6])
    ev = _round1_event(events)

    # Climber hit bypassed the wall; footman hit soaked it (2 -> 1).
    assert state.territories[TERRITORY_ID].stronghold_current_health == 1
    assert len(ev["defender_casualties"]) == 1
    assert ev["attacker_casualties"] == []

    # Combat continues and the stored ladder assignment is fresh, not empty.
    assert state.active_combat is not None
    assert state.active_combat.ladder_infantry_instance_ids == [climber.instance_id]
    assert state.active_combat.ladder_equipment_count == 1


def test_without_ladder_hits_soak_the_wall():
    state, events, _, _ = _run_initiate(with_ladder=False, defender_rolls=[6, 6])
    ev = _round1_event(events)

    # No ladder equipment -> no bypass: both hits land on the wall (2 -> 0).
    assert state.territories[TERRITORY_ID].stronghold_current_health == 0
    assert ev["defender_casualties"] == []
    assert state.active_combat is not None
    assert state.active_combat.ladder_infantry_instance_ids == []
    assert state.active_combat.ladder_equipment_count == 0
