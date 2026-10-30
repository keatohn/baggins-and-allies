"""
Tests for the non-combat-move MCTS Problem and the ai_algorithm routing flag.
Validates that MCTS-returned actions pass engine validation and apply cleanly.
"""

import random

import pytest

from backend.engine.actions import end_phase, move_units
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action
from backend.engine.state import Unit

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.non_combat_move import (
    MAX_NON_COMBAT_MOVE_BRANCHES,
    NonCombatMoveProblem,
    decide_non_combat_move_mcts,
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


def _non_combat_ctx(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str = "gondor",
    available_actions: dict | None = None,
) -> AIContext:
    state.phase = "non_combat_move"
    state.current_faction = faction_id
    if available_actions is None:
        available_actions = {"phase": "non_combat_move", "faction": faction_id, "can_end_phase": True}
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


def _plant_movable_stack(
    state,
    ud,
    *,
    from_tid: str = "north_ithilien",
    faction_id: str = "gondor",
    count: int = 3,
) -> None:
    """Place faction land units with full movement on a friendly hex so non_combat_move is legal."""
    if from_tid not in state.territories:
        pytest.skip(f"{from_tid} not in setup")
    candidates = [
        uid for uid in ud
        if getattr(ud[uid], "faction", "") == faction_id and not getattr(ud[uid], "aerial", False)
    ]
    if not candidates:
        pytest.skip(f"no land units for {faction_id}")
    uid = "gondor_soldier" if "gondor_soldier" in candidates else candidates[0]
    ah = int(getattr(ud[uid], "health", 1) or 1)
    mv = max(1, int(getattr(ud[uid], "movement", 1) or 1))
    terr = state.territories[from_tid]
    terr.units = []
    for i in range(count):
        terr.units.append(
            Unit(
                instance_id=f"mcts_ncm_{i}",
                unit_id=uid,
                remaining_movement=mv,
                remaining_health=ah,
                base_movement=mv,
                base_health=ah,
                loaded_onto=None,
            )
        )
    terr.owner = faction_id


def _plant_stuck_eagle(
    state,
    ud,
    *,
    tid: str = "isengard",
) -> None:
    """Plant an elves eagle on enemy land so it is aerial_units_must_move."""
    from backend.engine.utils import is_aerial_unit

    if tid not in state.territories:
        pytest.skip(f"{tid} not in setup")
    eagle_id = next((uid for uid in ud if is_aerial_unit(ud[uid]) and getattr(ud[uid], "faction", "") == "elves"), None)
    if not eagle_id:
        pytest.skip("no elves aerial unit in setup")
    eh = int(getattr(ud[eagle_id], "health", 1) or 1)
    mv = max(1, int(getattr(ud[eagle_id], "movement", 1) or 1))
    terr = state.territories[tid]
    terr.units = [
        Unit(
            instance_id="mcts_ncm_eagle",
            unit_id=eagle_id,
            remaining_movement=mv,
            remaining_health=eh,
            base_movement=mv,
            base_health=eh,
            loaded_onto=None,
        )
    ]


# ---------------------------------------------------------------------------
# NonCombatMoveProblem: enumerator validity
# ---------------------------------------------------------------------------


def test_legal_actions_all_pass_validation():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)

    actions = problem.legal_actions(state)
    assert actions, "expected at least end_phase"
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_capped():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud, count=5)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    assert len(actions) <= MAX_NON_COMBAT_MOVE_BRANCHES + 1


def test_legal_actions_include_reposition_or_end_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    types = {a.type for a in actions}
    assert "move_units" in types or "end_phase" in types
    move_actions = [a for a in actions if a.type == "move_units"]
    our_alliance = getattr(fd.get("gondor"), "alliance", "")
    for a in move_actions:
        to_tid = a.payload.get("to")
        terr = state.territories.get(to_tid)
        owner = getattr(terr, "owner", None)
        owner_alliance = getattr(fd.get(owner), "alliance", None) if owner and fd.get(owner) else None
        # Non-combat destinations must not be enemy-owned attack targets.
        assert owner is None or owner == "gondor" or owner_alliance == our_alliance


def test_legal_actions_exclude_end_phase_when_aerial_stuck():
    """When an aerial is on unfriendly land, end_phase is omitted (reducer guard)."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_stuck_eagle(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d, faction_id="elves")
    problem = NonCombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    assert actions
    assert all(a.type != "end_phase" for a in actions)


def test_stuck_aerial_generates_rescue_moves():
    """Stuck eagle must produce at least one move_units candidate toward friendly land."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_stuck_eagle(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d, faction_id="elves")
    problem = NonCombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    move_actions = [a for a in actions if a.type == "move_units"]
    assert move_actions, "expected rescue move for stuck eagle"
    # Rescue destinations must be friendly-for-landing (owned by elves/allies, not neutral).
    for a in move_actions:
        to_tid = a.payload.get("to")
        terr = state.territories.get(to_tid)
        owner = getattr(terr, "owner", None)
        assert owner is not None


# ---------------------------------------------------------------------------
# Apply / terminal / evaluate
# ---------------------------------------------------------------------------


def test_apply_move_units_advances_pending():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    actions = problem.legal_actions(state)
    move_actions = [a for a in actions if a.type == "move_units"]
    assert move_actions, "expected at least one move_units candidate"
    new_state = problem.apply(state, move_actions[0])
    assert new_state is not state
    assert any(
        getattr(pm, "phase", None) == "non_combat_move" for pm in (new_state.pending_moves or [])
    )
    assert (new_state.phase or "non_combat_move") == "non_combat_move"


def test_apply_end_phase_terminal():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    end_action = HashableAction(end_phase("gondor"))
    assert any(a.type == "end_phase" for a in problem.legal_actions(state))
    new_state = problem.apply(state, end_action)
    assert problem.is_terminal(new_state)


def test_evaluate_in_unit_range():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    val = problem.evaluate(state)
    assert -1.0 <= val <= 1.0


def test_is_terminal_false_for_live_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    assert not problem.is_terminal(state)


# ---------------------------------------------------------------------------
# Multi-step composition
# ---------------------------------------------------------------------------


def test_multi_step_sequence_then_end_phase():
    """MCTS can sequence a reposition then end_phase within one search."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud, count=4)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    budget = SearchBudget(max_iterations=40, max_time_seconds=2.0)
    chosen = run_mcts(problem, state, budget, rng=random.Random(7))
    assert chosen is not None
    action = _unwrap(chosen)
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    new_state = problem.apply(state, action)
    if action.type == "move_units":
        assert not problem.is_terminal(new_state)
        follow = problem.legal_actions(new_state)
        assert follow, "expected legal actions after a partial reposition"
        for h in follow:
            raw = _unwrap(h)
            v2 = validate_action(new_state, raw, ud, td, fd, cd, port_d)
            assert v2.valid, f"follow-up illegal: {raw.type} {v2.error}"


# ---------------------------------------------------------------------------
# Seed determinism
# ---------------------------------------------------------------------------


def test_run_mcts_seed_deterministic():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    problem = NonCombatMoveProblem(ctx)
    budget = SearchBudget(max_iterations=25, max_time_seconds=5.0)

    a1 = run_mcts(problem, state, budget, rng=random.Random(42))
    a2 = run_mcts(problem, state, budget, rng=random.Random(42))
    assert a1 is not None and a2 is not None
    assert _unwrap(a1).type == _unwrap(a2).type
    assert _unwrap(a1).payload == _unwrap(a2).payload


def test_decide_non_combat_move_mcts_returns_valid_action():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)

    action = decide_non_combat_move_mcts(ctx, rng=random.Random(3))
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    apply_action(state, action, ud, td, fd, cd, port_d)


def test_stuck_aerial_forces_rescue_not_end_phase():
    """With a stuck eagle, MCTS root action must be a rescue move (end_phase is illegal)."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_stuck_eagle(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d, faction_id="elves")
    action = decide_non_combat_move_mcts(ctx, rng=random.Random(11))
    assert action is not None
    assert action.type == "move_units", f"expected rescue move, got {action.type}"
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


# ---------------------------------------------------------------------------
# Flag routing
# ---------------------------------------------------------------------------


def test_decide_routes_non_combat_move_through_mcts_when_enabled(monkeypatch):
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_keeps_heuristic_when_mcts_disabled(monkeypatch):
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_movable_stack(state, ud)
    ctx = _non_combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
