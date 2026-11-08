"""
Modal H2 (issue #51): pending-round flags must ride on state.active_combat.

App.tsx reads combat_siegeworks_pending / combat_archer_prefire_pending (and the
siegeworks dice) off state.active_combat, but the backend only ever wrote them
onto the available-actions copy of the dict — so the combat modal's ready-state
shelf filtering, "Siegeworks"/"Archers" pending chips, and ram R-badge were
gated on flags that were always false.

Fix: state_for_response and the available-actions payload now share
_enrich_active_combat_pending, which stamps the flags (single source:
pending_combat_round) onto whichever active_combat dict is being serialized.

Run: .venv/bin/pytest test/test_active_combat_pending_flags.py
"""

from backend.api import main as api_main
from backend.api.main import _build_available_actions, state_for_response
from backend.engine.actions import initiate_combat
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
STRONGHOLD = "edoras"  # base HP 1: the attacker's ram rolls in the siegeworks round
OPEN_TERRITORY = "gap_of_rohan"  # non-stronghold: opens directly with round 1


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


def _fresh_battle(territory_id: str, attackers: list[str], defenders: list[str]):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    t = state.territories[territory_id]
    t.units.clear()
    t.owner = "rohan"
    t.units.extend(_make_unit(state, "isengard", uid, unit_defs) for uid in attackers)
    t.units.extend(_make_unit(state, "rohan", uid, unit_defs) for uid in defenders)
    state.current_faction = "isengard"
    state.phase = "combat"
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _initiate(state, ud, td, fd, cd, pd, territory_id: str, rolls):
    action = initiate_combat("isengard", territory_id, rolls)
    return apply_action(state, action, ud, td, fd, cd, pd)


def _actions_for(state, ud, td, fd, cd, pd):
    """Build the available-actions payload without touching the DB.

    get_game_definitions short-circuits on the game_defs cache, so registering the
    setup's defs under a throwaway game id keeps this a pure in-process call.
    """
    game_id = "test-pending-flags"
    api_main.game_defs[game_id] = (ud, td, fd, cd, pd)
    try:
        return _build_available_actions(state, game_id, None)
    finally:
        api_main.game_defs.pop(game_id, None)


def test_pending_flags_present_after_siegeworks_open():
    """Round-0 state after the siegeworks round: archer prefire is pending next.

    Both payloads must carry the flags with identical values — the modal reads
    them off state.active_combat, other consumers read them off available-actions.
    """
    territory_id = STRONGHOLD
    state, ud, td, fd, cd, pd = _fresh_battle(
        territory_id,
        attackers=["battering_ram", "urukhai_warrior"],  # ram: 3 siege dice vs walls
        defenders=["rohirrim_archer"],
    )
    # 3 ram dice (all miss: 10 > attack 5), no defender siege dice (archer is not siegework)
    state, events = _initiate(
        state, ud, td, fd, cd, pd, territory_id,
        {"attacker": [10, 10, 10], "defender": []},
    )
    rounds = [e for e in events if e.type == "combat_round_resolved"]
    assert len(rounds) == 1 and rounds[0].payload.get("is_siegeworks_round")
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0

    out = state_for_response(state)
    ac_state = out["active_combat"]
    assert ac_state["combat_archer_prefire_pending"] is True
    assert ac_state["combat_siegeworks_pending"] is False  # already logged this battle
    assert ac_state["combat_siegeworks_dice"] == {"attacker": 3, "defender": 0}

    actions = _actions_for(state, ud, td, fd, cd, pd)
    assert "active_combat" in actions, actions
    ac_actions = actions["active_combat"]
    for key in (
        "combat_siegeworks_pending",
        "combat_archer_prefire_pending",
        "combat_siegeworks_dice",
    ):
        assert ac_actions[key] == ac_state[key], key


def test_pending_flags_false_after_round1():
    """A battle past round 0 has nothing pending: both flags must be false."""
    territory_id = OPEN_TERRITORY
    state, ud, td, fd, cd, pd = _fresh_battle(
        territory_id,
        attackers=["urukhai_warrior", "dunland_wildman"],
        defenders=["gondor_soldier"],
    )
    # All-miss rolls on both sides: round 1 runs, both sides survive, battle continues.
    state, events = _initiate(
        state, ud, td, fd, cd, pd, territory_id,
        {"attacker": [10, 10], "defender": [10]},
    )
    rounds = [e for e in events if e.type == "combat_round_resolved"]
    assert len(rounds) == 1 and rounds[0].payload["round_number"] == 1
    assert state.active_combat is not None
    assert state.active_combat.round_number == 1

    out = state_for_response(state)
    ac_state = out["active_combat"]
    assert ac_state["combat_siegeworks_pending"] is False
    assert ac_state["combat_archer_prefire_pending"] is False

    actions = _actions_for(state, ud, td, fd, cd, pd)
    assert "active_combat" in actions, actions
    assert actions["active_combat"]["combat_siegeworks_pending"] is False
    assert actions["active_combat"]["combat_archer_prefire_pending"] is False
