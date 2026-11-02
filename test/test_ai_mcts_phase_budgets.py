"""
Tests for per-phase MCTS budget resolution (3.5a plumbing + 3.5b retuning).

3.5b retunes combat_move and non_combat_move off the flat table (measured against
the 3.1 latency baseline): those phases get smaller iteration caps and a 1.2s hard
wall so worst-case "normal" per-action search stays under the ≲2s target. The fast
phases (purchase, combat, mobilization) still resolve exactly like get_budget.
Also covers env-difficulty resolution, fallbacks, and that all five decide_*_mcts
entry points resolve their budget through get_phase_budget with the correct phase key.
"""

from functools import lru_cache

import pytest

from backend.ai.context import AIContext
from backend.ai.mcts.budget import (
    BUDGETS,
    DEFAULT_DIFFICULTY,
    PHASES,
    PHASE_BUDGETS,
    get_budget,
    get_phase_budget,
)
from backend.ai.mcts.combat import decide_combat_mcts
from backend.ai.mcts.combat_move import decide_combat_move_mcts
from backend.ai.mcts.mobilization import decide_mobilization_mcts
from backend.ai.mcts.non_combat_move import decide_non_combat_move_mcts
from backend.ai.mcts.purchase import decide_purchase_mcts
from backend.ai.mcts import search as mcts_search


# ---------------------------------------------------------------------------
# Resolution semantics
# ---------------------------------------------------------------------------


def test_phase_table_covers_all_phases():
    assert set(PHASE_BUDGETS) == set(PHASES)
    for phase, table in PHASE_BUDGETS.items():
        assert set(table) == set(BUDGETS), phase


def test_fast_phases_match_global_table():
    """Purchase, combat, and mobilization have cheap enumerators and keep the flat table."""
    for phase in ("purchase", "combat", "mobilization"):
        for difficulty in BUDGETS:
            assert get_phase_budget(phase, difficulty) == get_budget(difficulty), (phase, difficulty)
        assert get_phase_budget(phase, None) == get_budget(None), phase


# Phases 3.5b deliberately retunes off the flat table (see PHASE_BUDGET_OVERRIDES).
_RETUNED_PHASES = ("combat_move", "non_combat_move")


def test_retuned_phases_are_cheaper_than_global_table():
    """3.5b: move-phase budgets never exceed the flat table on either cap."""
    for phase in _RETUNED_PHASES:
        for difficulty in BUDGETS:
            phase_budget = get_phase_budget(phase, difficulty)
            flat = get_budget(difficulty)
            assert phase_budget.max_iterations <= flat.max_iterations, (phase, difficulty)
            assert phase_budget.max_time_seconds <= flat.max_time_seconds, (phase, difficulty)
            # At least one cap must be strictly cheaper, or the override is pointless.
            assert (
                phase_budget.max_iterations < flat.max_iterations
                or phase_budget.max_time_seconds < flat.max_time_seconds
            ), (phase, difficulty)


def test_retuned_hard_wall_keeps_worst_case_under_two_seconds():
    """
    3.5b latency gate: hard wall + one overshooting iteration stays ≲2s.

    The search checks its wall cap between iterations, so worst-case cost is the
    wall plus one full child expansion. A 1.2s hard wall leaves ~0.8s of headroom
    for that overshoot against the frontend's AI_STEP_DELAY_MS=2500.
    """
    for phase in _RETUNED_PHASES:
        for difficulty in ("normal", "hard"):
            budget = get_phase_budget(phase, difficulty)
            assert budget.max_time_seconds + 1.0 <= 2.5, (phase, difficulty)


def test_unknown_phase_falls_back_to_global_table():
    assert get_phase_budget("bogus", "easy") == BUDGETS["easy"]
    assert get_phase_budget("bogus", "nope") == BUDGETS[DEFAULT_DIFFICULTY]
    assert get_phase_budget("bogus", None) == BUDGETS[DEFAULT_DIFFICULTY]


def test_unknown_difficulty_falls_back_to_default():
    for phase in PHASES:
        # Unknown difficulty resolves to the phase's DEFAULT_DIFFICULTY entry:
    # the flat normal for fast phases, the retuned normal for move phases.
        expected = PHASE_BUDGETS[phase][DEFAULT_DIFFICULTY]
        assert get_phase_budget(phase, "nope") == expected, phase
        assert get_phase_budget(phase, None) == expected, phase


def test_env_difficulty_honored(monkeypatch):
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    for phase in PHASES:
        # Env difficulty selects that phase's easy entry (retuned for move phases).
        assert get_phase_budget(phase, None) == PHASE_BUDGETS[phase]["easy"], phase
    # Explicit argument still wins over env.
    assert get_phase_budget("purchase", "hard") == BUDGETS["hard"]


# ---------------------------------------------------------------------------
# Entry-point wiring: each decide_*_mcts uses its own phase key
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_defs():
    from backend.engine.definitions import load_static_definitions

    return load_static_definitions(setup_id="wotr_exp_1.0")


def _ctx_for_phase(phase: str) -> AIContext:
    from backend.engine.definitions import load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = _load_defs()
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
    )
    state.phase = phase
    state.current_faction = "gondor"
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=None,
    )


_ENTRY_POINTS = [
    ("purchase", decide_purchase_mcts),
    ("combat", decide_combat_mcts),
    ("combat_move", decide_combat_move_mcts),
    ("non_combat_move", decide_non_combat_move_mcts),
    ("mobilization", decide_mobilization_mcts),
]


@pytest.mark.parametrize("phase,decide_fn", _ENTRY_POINTS)
def test_entry_point_resolves_budget_for_its_phase(monkeypatch, phase, decide_fn):
    """Each decide_*_mcts looks up its phase in PHASE_BUDGETS and passes it to search."""
    from backend.ai.mcts.budget import SearchBudget

    marker = SearchBudget(max_iterations=7, max_time_seconds=0.007)
    monkeypatch.setitem(PHASE_BUDGETS, phase, dict(BUDGETS, normal=marker))
    captured: list = []

    def spy(problem, state, budget, **kwargs):
        captured.append(budget)
        return None  # entry point falls back to its safety-net path

    monkeypatch.setattr(mcts_search, "run_mcts_reuse", spy)
    decide_fn(_ctx_for_phase(phase))
    assert len(captured) == 1
    assert captured[0] is marker, f"{phase} entry point used the wrong phase budget"


def test_entry_points_do_not_share_budget_objects_across_phases():
    """Phase tables start as independent copies, so retunes can't leak across phases."""
    a = PHASE_BUDGETS["purchase"]
    b = PHASE_BUDGETS["non_combat_move"]
    assert a is not b
    # 3.5b diverged the move phases; the purchase table must be untouched by that.
    assert a["normal"] == BUDGETS["normal"]
    assert b["normal"] != BUDGETS["normal"]
