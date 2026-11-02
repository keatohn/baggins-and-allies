"""
Tests for the combat-move MCTS Problem and the ai_algorithm routing flag.
Validates that MCTS-returned actions pass engine validation and apply cleanly.
"""

import os

import pytest

from backend.engine.actions import end_phase, move_units
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.state import PendingMove, Unit

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.combat_move import (
    MAX_COMBAT_MOVE_BRANCHES,
    CombatMoveProblem,
    decide_combat_move_mcts,
)
from backend.ai.mcts.search import run_mcts


def _unwrap(a):
    return a.unwrap() if isinstance(a, HashableAction) else a


def _load_wotr_setup():
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

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
    return state, ud, td, fd, cd, port_d


def _combat_move_ctx(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str = "gondor",
    available_actions: dict | None = None,
) -> AIContext:
    state.phase = "combat_move"
    state.current_faction = faction_id
    if available_actions is None:
        available_actions = {"phase": "combat_move", "faction": faction_id, "can_end_phase": True}
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


def _plant_attackers_near_enemy(
    state,
    ud,
    td,
    *,
    from_tid: str = "north_ithilien",
    to_tid: str = "minas_morgul",
    attackers: int = 3,
) -> None:
    """Place gondor land units on a friendly hex adjacent to an enemy hex so combat_move is legal."""
    if from_tid not in state.territories or to_tid not in state.territories:
        pytest.skip(f"{from_tid} or {to_tid} not in setup")
    att_uid = "gondor_soldier" if "gondor_soldier" in ud else next(
        u for u in ud if getattr(ud[u], "faction", "") == "gondor"
    )
    ah = int(getattr(ud[att_uid], "health", 1) or 1)
    mv = max(1, int(getattr(ud[att_uid], "movement", 1) or 1))
    terr = state.territories[from_tid]
    # Clear existing units so we control the scenario.
    terr.units = []
    for i in range(attackers):
        terr.units.append(
            Unit(
                instance_id=f"mcts_cm_att_{i}",
                unit_id=att_uid,
                remaining_movement=mv,
                remaining_health=ah,
                base_movement=mv,
                base_health=ah,
                loaded_onto=None,
            )
        )
    terr.owner = "gondor"


# ---------------------------------------------------------------------------
# CombatMoveProblem: enumerator validity
# ---------------------------------------------------------------------------


def test_legal_actions_all_pass_validation():
    """Every action from legal_actions must pass engine validate_action."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)

    actions = problem.legal_actions(state)
    assert actions, "expected at least end_phase"
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_capped():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td, attackers=5)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    assert len(actions) <= MAX_COMBAT_MOVE_BRANCHES + 1


def test_legal_actions_include_attack_or_end_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    types = {a.type for a in actions}
    assert "move_units" in types or "end_phase" in types
    move_targets = {
        a.payload.get("to") for a in actions if a.type == "move_units"
    }
    assert "minas_morgul" in move_targets


def test_legal_actions_excludes_end_phase_when_forbidden():
    """When can_end_phase constraints (idle sails) block ending, end_phase is omitted."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    # Simulate an unresolved idle sail boat blocking phase end.
    state.combat_move_naval_idle_sail_instance_ids = ["some_boat"]
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    assert all(a.type != "end_phase" for a in actions)


# ---------------------------------------------------------------------------
# Apply / terminal / evaluate
# ---------------------------------------------------------------------------


def test_apply_move_units_advances_pending():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    move_actions = [a for a in actions if a.type == "move_units"]
    assert move_actions, "expected at least one move_units candidate"
    new_state = problem.apply(state, move_actions[0])
    assert new_state is not state
    assert any(getattr(pm, "phase", None) == "combat_move" for pm in (new_state.pending_moves or []))
    assert (new_state.phase or "combat_move") == "combat_move"


def test_apply_end_phase_terminal():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    # No must-attack boats; end_phase should be legal and terminal.
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    end_action = HashableAction(end_phase("gondor"))
    # Ensure end_phase is legal here
    assert any(a.type == "end_phase" for a in problem.legal_actions(state))
    new_state = problem.apply(state, end_action)
    assert problem.is_terminal(new_state)


def test_evaluate_in_unit_range():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    val = problem.evaluate(state)
    assert -1.0 <= val <= 1.0


def test_is_terminal_false_for_live_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    assert not problem.is_terminal(state)


# ---------------------------------------------------------------------------
# Multi-step composition
# ---------------------------------------------------------------------------


def test_multi_step_sequence_then_end_phase():
    """MCTS can sequence a move then end_phase within one search."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td, attackers=4)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    budget = SearchBudget(max_iterations=40, max_time_seconds=2.0)
    chosen = run_mcts(problem, state, budget, rng=__import__("random").Random(7))
    assert chosen is not None
    action = _unwrap(chosen)
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    # Apply the chosen root action; if it is a move, the phase must stay combat_move.
    new_state = problem.apply(state, action)
    if action.type == "move_units":
        assert not problem.is_terminal(new_state)
        # Further actions must still be valid on the successor.
        follow = problem.legal_actions(new_state)
        assert follow, "expected legal actions after a partial move"
        for h in follow:
            raw = _unwrap(h)
            v2 = validate_action(new_state, raw, ud, td, fd, cd, port_d)
            assert v2.valid, f"follow-up illegal: {raw.type} {v2.error}"


# ---------------------------------------------------------------------------
# Seed determinism
# ---------------------------------------------------------------------------


def test_run_mcts_seed_deterministic():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatMoveProblem(ctx)
    budget = SearchBudget(max_iterations=25, max_time_seconds=5.0)
    import random

    a1 = run_mcts(problem, state, budget, rng=random.Random(42))
    a2 = run_mcts(problem, state, budget, rng=random.Random(42))
    assert a1 is not None and a2 is not None
    assert _unwrap(a1).type == _unwrap(a2).type
    assert _unwrap(a1).payload == _unwrap(a2).payload


def test_decide_combat_move_mcts_returns_valid_action():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    import random

    action = decide_combat_move_mcts(ctx, rng=random.Random(3))
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    # Must apply cleanly (end_phase may raise if boats must attack — none here).
    apply_action(state, action, ud, td, fd, cd, port_d)


# ---------------------------------------------------------------------------
# Flag routing
# ---------------------------------------------------------------------------


def test_decide_routes_combat_move_through_mcts_when_enabled(monkeypatch):
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def _load_motw_setup():
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id="motw_1.0")
    setup = load_starting_setup(setup_id="motw_1.0")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
    )
    return state, ud, td, fd, cd, port_d


def _replay_combat_queue(state, ud, td, fd):
    """Apply the combat_move queue the way phase-end does. Raises ValueError on a bad queue."""
    get_state_after_pending_moves(state, "combat_move", ud, td, fd)


def test_charge_sequences_survive_pending_replay():
    """
    Two charges that pass through each other's destinations are each legal on the
    pre-pending board and then raise when the queue is replayed:

        Charge path cannot pass through fords_of_isen: territory has units

    legal_actions must not offer the second move.
    """
    state, ud, td, fd, cd, port_d = _load_motw_setup()
    state.phase = "combat_move"
    state.current_faction = "isengard"

    def unit(iid, uid):
        defn = ud[uid]
        return Unit(iid, uid, defn.movement, defn.health, defn.movement, defn.health, None)

    for terr in state.territories.values():
        terr.units = [u for u in terr.units if ud[u.unit_id].faction != "isengard"]

    def plant(tid, units):
        terr = state.territories[tid]
        terr.owner = "isengard"
        terr.units = [u for u in terr.units if ud[u.unit_id].faction != "isengard"] + units

    plant(
        "south_dunland",
        [unit("isengard_warg_a1", "isengard_warg_rider"), unit("isengard_warg_a2", "isengard_warg_rider")],
    )
    plant(
        "west_march",
        [unit("isengard_warg_b1", "isengard_warg_rider"), unit("isengard_warg_b2", "isengard_warg_rider")],
    )
    state.territories["fords_of_isen"].units = []
    state.territories["fords_of_isen"].owner = "rohan"
    state.territories["gap_of_rohan"].units = []
    state.territories["gap_of_rohan"].owner = "rohan"

    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d, faction_id="isengard")
    problem = CombatMoveProblem(ctx)
    first_moves = [a.unwrap() for a in problem.legal_actions(state) if a.unwrap().type != "end_phase"]
    assert first_moves, "expected charge candidates from the planted wargs"

    checked = 0
    for first in first_moves:
        after_first, _ = apply_action(state, first, ud, td, fd, cd, port_d)
        _replay_combat_queue(after_first, ud, td, fd)
        follow = CombatMoveProblem(
            _combat_move_ctx(after_first, ud, td, fd, cd, port_d, faction_id="isengard")
        )
        for second in follow.legal_actions(after_first):
            raw = second.unwrap()
            if raw.type == "end_phase":
                continue
            after_second, _ = apply_action(after_first, raw, ud, td, fd, cd, port_d)
            _replay_combat_queue(after_second, ud, td, fd)
            checked += 1
    assert checked > 0


def test_ship_three_hex_sail_is_not_queued():
    """
    A gondor ship with movement 2 cannot sail three sea zones. Replaying that
    pending move is the sweep failure:

        unit gondor_gondor_ship_034 has remaining_movement=2, need 3

    The enumerator must not offer that sail, and any sail it does offer has to
    replay together with moves already queued.
    """
    state, ud, td, fd, cd, port_d = _load_motw_setup()
    state.phase = "combat_move"
    state.current_faction = "gondor"
    ship_id = None
    origin = None
    for tid, terr in state.territories.items():
        for unit in terr.units:
            if unit.unit_id == "gondor_ship":
                ship_id = unit.instance_id
                origin = tid
                break
        if ship_id:
            break
    assert ship_id == "gondor_gondor_ship_034"
    assert origin == "sea_zone_12"

    doomed = state.copy()
    doomed.pending_moves.append(
        PendingMove(origin, "sea_zone_5", [ship_id], "combat_move", [], "sail")
    )
    with pytest.raises(ValueError, match=r"gondor_gondor_ship_034 has remaining_movement=2, need 3"):
        _replay_combat_queue(doomed, ud, td, fd)

    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d, faction_id="gondor")
    problem = CombatMoveProblem(ctx)
    assert problem._pending_sequence_replays(state, origin, "sea_zone_5", [ship_id], []) is False

    for action in problem.legal_actions(state):
        raw = action.unwrap()
        if raw.type == "end_phase":
            continue
        ids = raw.payload.get("unit_instance_ids") or []
        assert not (raw.payload.get("to") == "sea_zone_5" and ship_id in ids)
        after, _ = apply_action(state, raw, ud, td, fd, cd, port_d)
        _replay_combat_queue(after, ud, td, fd)


def test_decide_keeps_heuristic_when_mcts_disabled(monkeypatch):
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_attackers_near_enemy(state, ud, td)
    ctx = _combat_move_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
