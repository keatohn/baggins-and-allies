"""
Tests for 3.5a: per-phase MCTS budget resolution.

PHASE_BUDGETS initially mirrors the flat BUDGETS table for every phase (zero
behavior change vs get_budget), so these tests pin the parity guarantee that
3.5b's retuning will deliberately break per phase. Also covers env-difficulty
resolution, fallbacks, and that all five decide_*_mcts entry points resolve
their budget through get_phase_budget with the correct phase key.
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


def test_phase_budgets_match_global_table_for_now():
    """3.5a parity: every phase resolves exactly like get_budget (no retuning yet)."""
    for phase in PHASES:
        for difficulty in BUDGETS:
            assert get_phase_budget(phase, difficulty) == get_budget(difficulty), (phase, difficulty)
        assert get_phase_budget(phase, None) == get_budget(None), phase


def test_unknown_phase_falls_back_to_global_table():
    assert get_phase_budget("bogus", "easy") == BUDGETS["easy"]
    assert get_phase_budget("bogus", "nope") == BUDGETS[DEFAULT_DIFFICULTY]
    assert get_phase_budget("bogus", None) == BUDGETS[DEFAULT_DIFFICULTY]


def test_unknown_difficulty_falls_back_to_default():
    for phase in PHASES:
        assert get_phase_budget(phase, "nope") == BUDGETS[DEFAULT_DIFFICULTY], phase
        assert get_phase_budget(phase, None) == BUDGETS[DEFAULT_DIFFICULTY], phase


def test_env_difficulty_honored(monkeypatch):
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    for phase in PHASES:
        assert get_phase_budget(phase, None) == BUDGETS["easy"], phase
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
    """Phase tables start as independent copies, so 3.5b retunes can't leak."""
    a = PHASE_BUDGETS["purchase"]
    b = PHASE_BUDGETS["non_combat_move"]
    assert a is not b
    assert a["normal"] == b["normal"]  # same values, until 3.5b diverges them
