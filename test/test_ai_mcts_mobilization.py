"""
Tests for the mobilization MCTS Problem and the ai_algorithm routing flag.
Validates that MCTS-returned actions pass engine validation and apply cleanly.
"""

import random

import pytest

from backend.engine.actions import end_phase
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action
from backend.engine.state import UnitStack

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.mobilization import (
    MAX_MOBILIZATION_BRANCHES,
    MobilizationProblem,
    decide_mobilization_mcts,
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


def _mobilization_ctx(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str = "gondor",
    available_actions: dict | None = None,
) -> AIContext:
    state.phase = "mobilization"
    state.current_faction = faction_id
    if available_actions is None:
        available_actions = {"phase": "mobilization", "faction": faction_id, "can_end_phase": True}
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


def _plant_purchased(
    state,
    faction_id: str = "gondor",
    stacks: list[tuple[str, int]] | None = None,
) -> None:
    """Seed owned-at-start (like a real turn start) and the purchased-units pool."""
    if state.faction_territories_at_turn_start.get(faction_id) is None:
        state.faction_territories_at_turn_start[faction_id] = [
            tid for tid, terr in state.territories.items() if terr.owner == faction_id
        ]
    if stacks is None:
        stacks = [("gondor_soldier", 3)]
    state.faction_purchased_units[faction_id] = [
        UnitStack(unit_id=uid, count=count) for uid, count in stacks
    ]


def _plant_pending_camp(state, options: list[str]) -> None:
    """Plant an unplaced pending camp with the given territory options."""
    state.pending_camps = [
        {"territory_options": list(options), "placed_territory_id": None}
    ]


# ---------------------------------------------------------------------------
# MobilizationProblem: enumerator validity
# ---------------------------------------------------------------------------


def test_legal_actions_all_pass_validation():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)

    actions = problem.legal_actions(state)
    assert actions, "expected at least end_phase"
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_capped():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state, stacks=[("gondor_soldier", 4), ("gondor_archer", 2)])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    actions = problem.legal_actions(state)
    assert len(actions) <= MAX_MOBILIZATION_BRANCHES + 1


def test_legal_actions_include_mobilize_or_end_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    actions = problem.legal_actions(state)
    types = {a.type for a in actions}
    assert "mobilize_units" in types or "end_phase" in types


def test_legal_actions_include_queue_camp_when_pending():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    # Camp options must be owned, powered, and without a standing camp.
    _plant_pending_camp(state, ["pelennor", "pelargir"])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    actions = problem.legal_actions(state)
    types = {a.type for a in actions}
    assert "queue_camp_placement" in types


def test_legal_actions_only_end_phase_when_nothing_to_place():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    # No purchased units, no pending camps
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    actions = problem.legal_actions(state)
    assert actions
    assert all(a.type == "end_phase" for a in actions)


# ---------------------------------------------------------------------------
# Apply / terminal / evaluate
# ---------------------------------------------------------------------------


def test_apply_mobilize_units_reduces_purchased_pool():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state, stacks=[("gondor_soldier", 3)])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    mobilize_actions = [a for a in problem.legal_actions(state) if a.type == "mobilize_units"]
    assert mobilize_actions, "expected at least one mobilize candidate"
    haction = mobilize_actions[0]
    new_state = problem.apply(state, haction)
    assert new_state is not state
    # Units moved from purchased pool into pending_mobilizations
    old_pool = [
        (s.unit_id, s.count)
        for s in (state.faction_purchased_units or {}).get("gondor", [])
    ]
    new_pool = [
        (s.unit_id, s.count)
        for s in (new_state.faction_purchased_units or {}).get("gondor", [])
    ]
    assert new_pool != old_pool or new_state.pending_mobilizations
    assert (new_state.phase or "mobilization") == "mobilization"


def test_apply_queue_camp_records_placement():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    # pelennor is owned, powered, and has no standing camp.
    _plant_pending_camp(state, ["pelennor"])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    camp_actions = [a for a in problem.legal_actions(state) if a.type == "queue_camp_placement"]
    assert camp_actions, "expected queue_camp_placement candidate"
    new_state = problem.apply(state, camp_actions[0])
    assert any(getattr(p, "camp_index", None) == 0 for p in (new_state.pending_camp_placements or []))


def test_apply_end_phase_terminal():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    # No purchased units, no camps: end_phase is legal and terminal.
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    assert any(a.type == "end_phase" for a in problem.legal_actions(state))
    end_action = HashableAction(end_phase("gondor"))
    new_state = problem.apply(state, end_action)
    assert problem.is_terminal(new_state)


def test_evaluate_in_unit_range():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    val = problem.evaluate(state)
    assert -1.0 <= val <= 1.0


def test_is_terminal_false_for_live_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    assert not problem.is_terminal(state)


# ---------------------------------------------------------------------------
# Multi-step composition
# ---------------------------------------------------------------------------


def test_multi_step_sequence_then_end_phase():
    """MCTS can sequence a mobilize then end_phase within one search."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state, stacks=[("gondor_soldier", 3)])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    budget = SearchBudget(max_iterations=40, max_time_seconds=2.0)
    chosen = run_mcts(problem, state, budget, rng=random.Random(7))
    assert chosen is not None
    action = _unwrap(chosen)
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    new_state = problem.apply(state, action)
    if action.type == "mobilize_units":
        assert not problem.is_terminal(new_state)
        follow = problem.legal_actions(new_state)
        assert follow, "expected legal actions after a partial mobilization"
        for h in follow:
            raw = _unwrap(h)
            v2 = validate_action(new_state, raw, ud, td, fd, cd, port_d)
            assert v2.valid, f"follow-up illegal: {raw.type} {v2.error}"


def test_multi_step_camp_then_mobilize():
    """MCTS can sequence queue_camp_placement then continue with mobilize/end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state, stacks=[("gondor_soldier", 2)])
    _plant_pending_camp(state, ["pelennor"])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    camp_actions = [a for a in problem.legal_actions(state) if a.type == "queue_camp_placement"]
    assert camp_actions
    new_state = problem.apply(state, camp_actions[0])
    assert not problem.is_terminal(new_state)
    follow = problem.legal_actions(new_state)
    assert follow
    for h in follow:
        raw = _unwrap(h)
        v2 = validate_action(new_state, raw, ud, td, fd, cd, port_d)
        assert v2.valid, f"follow-up after camp illegal: {raw.type} {v2.error}"


# ---------------------------------------------------------------------------
# Seed determinism
# ---------------------------------------------------------------------------


def test_run_mcts_seed_deterministic():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    problem = MobilizationProblem(ctx)
    budget = SearchBudget(max_iterations=25, max_time_seconds=5.0)

    a1 = run_mcts(problem, state, budget, rng=random.Random(42))
    a2 = run_mcts(problem, state, budget, rng=random.Random(42))
    assert a1 is not None and a2 is not None
    assert _unwrap(a1).type == _unwrap(a2).type
    assert _unwrap(a1).payload == _unwrap(a2).payload


def test_decide_mobilization_mcts_returns_valid_action():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)

    action = decide_mobilization_mcts(ctx, rng=random.Random(3))
    assert action is not None
    assert action.type in ("mobilize_units", "queue_camp_placement", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    apply_action(state, action, ud, td, fd, cd, port_d)


def test_decide_mobilization_mcts_queues_camp_when_required():
    """With an unplaced camp blocking end_phase, MCTS must not return end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    _plant_pending_camp(state, ["pelennor"])
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    action = decide_mobilization_mcts(ctx, rng=random.Random(11))
    assert action is not None
    # end_phase is illegal while a camp has valid options; enumerator excludes it.
    assert action.type != "end_phase", "camp placement required before ending mobilization"
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


# ---------------------------------------------------------------------------
# Flag routing
# ---------------------------------------------------------------------------


def test_decide_routes_mobilization_through_mcts_when_enabled(monkeypatch):
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("mobilize_units", "queue_camp_placement", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_keeps_heuristic_when_mcts_disabled(monkeypatch):
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_purchased(state)
    ctx = _mobilization_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("mobilize_units", "queue_camp_placement", "end_phase")
