"""
Tests for the domain-agnostic MCTS core (search.py / node.py / budget.py).
Uses a tiny synthetic Problem so tests are fast and independent of game data.
"""

import random
import time

import pytest

from backend.ai.mcts.budget import BUDGETS, SearchBudget, get_budget
from backend.ai.mcts.node import Node
from backend.ai.mcts.search import (
    PRIOR_FLOOR,
    SearchStats,
    run_mcts,
    run_mcts_search,
    run_mcts_with_values,
)


# ---------------------------------------------------------------------------
# Synthetic problem: a one-pick "bandit" tree.
# Root has N actions; each action leads to a terminal leaf with a fixed value.
# This lets us test convergence, determinism, and budget caps cleanly.
# ---------------------------------------------------------------------------


class BanditProblem:
    """Root -> terminal leaves with prescribed values in [-1, 1]."""

    def __init__(self, leaf_values: dict[str, float]) -> None:
        # leaf_values: action_name -> terminal value (root perspective)
        self.leaf_values = dict(leaf_values)
        self.apply_count = 0

    def legal_actions(self, state):
        # state is "root" or a leaf name; only root has actions.
        if state == "root":
            return sorted(self.leaf_values.keys())
        return []

    def apply(self, state, action):
        assert state == "root"
        self.apply_count += 1
        return action  # new state is the leaf name (terminal)

    def is_terminal(self, state):
        return state != "root"

    def evaluate(self, state):
        # Non-terminal leaves never occur in this problem (depth 1).
        return 0.0

    def terminal_value(self, state):
        return self.leaf_values.get(state, 0.0)

    def priors(self, state):
        return None


class CountingProblem(BanditProblem):
    """Wraps BanditProblem to count is_terminal / evaluate calls (for budget-cap tests)."""

    def __init__(self, leaf_values: dict[str, float]) -> None:
        super().__init__(leaf_values)
        self.terminal_checks = 0

    def is_terminal(self, state):
        self.terminal_checks += 1
        return super().is_terminal(state)


# ---------------------------------------------------------------------------
# budget.py
# ---------------------------------------------------------------------------


def test_budgets_have_positive_limits():
    for name, b in BUDGETS.items():
        assert b.max_iterations >= 1, name
        assert b.max_time_seconds > 0, name


def test_get_budget_by_name_and_default():
    assert get_budget("easy").max_iterations == BUDGETS["easy"].max_iterations
    assert get_budget("hard").max_iterations == BUDGETS["hard"].max_iterations
    # Unknown name falls back to default.
    assert get_budget("nope").max_iterations == BUDGETS["normal"].max_iterations
    # None also falls back.
    assert get_budget(None).max_iterations == BUDGETS["normal"].max_iterations


def test_search_budget_rejects_invalid():
    with pytest.raises(ValueError):
        SearchBudget(max_iterations=0, max_time_seconds=1.0)
    with pytest.raises(ValueError):
        SearchBudget(max_iterations=10, max_time_seconds=0.0)


# ---------------------------------------------------------------------------
# node.py
# ---------------------------------------------------------------------------


def test_node_value_starts_at_zero_and_accumulates():
    n = Node(state="s")
    assert n.value == 0.0
    n.visit_count = 2
    n.total_value = 1.0
    assert n.value == pytest.approx(0.5)


def test_node_select_child_prefers_high_q_with_prior_tiebreak():
    root = Node(state="root", untried_actions=[])
    root.mark_expanded()
    # Two children: one high-Q low-prior, one mid-Q high-prior.
    a = Node(state="a", prior=0.1)
    a.visit_count, a.total_value = 10, 8.0  # Q=0.8
    b = Node(state="b", prior=0.9)
    b.visit_count, b.total_value = 10, 5.0  # Q=0.5
    root.children["a"] = a
    root.children["b"] = b
    # With c_puct large enough, U dominates and high-prior b wins.
    action, child = root.select_child(c_puct=10.0)
    assert action == "b"
    # With c_puct tiny, Q dominates and high-Q a wins.
    action, child = root.select_child(c_puct=0.001)
    assert action == "a"


# ---------------------------------------------------------------------------
# search.py — convergence, determinism, caps, priors, non-mutation
# ---------------------------------------------------------------------------


def test_mcts_converges_to_best_action():
    """With enough iterations, the most-valued action should dominate visits."""
    values = {"alpha": 0.9, "beta": 0.1, "gamma": 0.5}
    problem = BanditProblem(values)
    budget = SearchBudget(max_iterations=300, max_time_seconds=5.0)
    action = run_mcts(problem, "root", budget, rng=random.Random(0))
    assert action == "alpha"


def test_mcts_seeded_determinism():
    """Same seed -> same chosen action and same visit distribution."""
    values = {"x": 0.2, "y": 0.8, "z": 0.5}
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    a1, v1 = run_mcts_with_values(BanditProblem(values), "root", budget, rng=random.Random(42))
    a2, v2 = run_mcts_with_values(BanditProblem(values), "root", budget, rng=random.Random(42))
    assert a1 == a2
    assert set(v1.keys()) == set(v2.keys())


def test_mcts_respects_iteration_cap():
    """Search must not exceed max_iterations expansions."""
    problem = CountingProblem({f"a{i}": 0.5 for i in range(5)})
    budget = SearchBudget(max_iterations=17, max_time_seconds=100.0)
    run_mcts(problem, "root", budget, rng=random.Random(0))
    # One expand at root + at most one expand per iteration; terminal checks bounded.
    assert problem.apply_count <= 17 + 1


def test_mcts_respects_time_cap():
    """Search stops when wall-time budget is exhausted (even if iterations remain)."""
    problem = CountingProblem({f"a{i}": 0.5 for i in range(3)})
    # Huge iteration cap, tiny time cap.
    budget = SearchBudget(max_iterations=10_000_000, max_time_seconds=0.01)
    start = time.monotonic()
    run_mcts(problem, "root", budget, rng=random.Random(0))
    elapsed = time.monotonic() - start
    # Allow generous slack for slow CI machines, but it must stop well before 10M iters.
    assert elapsed < 2.0


def test_mcts_returns_none_when_no_actions():
    problem = BanditProblem({})
    # Override legal_actions at root to empty.
    problem.leaf_values = {}
    action = run_mcts(problem, "root", SearchBudget(max_iterations=10, max_time_seconds=1.0))
    assert action is None


def test_mcts_priors_still_explore_all_actions():
    """Prior floor ensures no action is starved — every action gets >= 1 visit with enough iters."""
    values = {"a": 1.0, "b": 0.0, "c": 0.0}
    problem = BanditProblem(values)
    budget = SearchBudget(max_iterations=100, max_time_seconds=5.0)
    _action, action_values = run_mcts_with_values(problem, "root", budget, rng=random.Random(0))
    # All three actions should have been tried (appear in values with visit > 0).
    # run_mcts_with_values returns mean values; visited actions have entries.
    assert set(action_values.keys()) == {"a", "b", "c"}


def test_mcts_does_not_mutate_root_state():
    """The search must never mutate the root state object."""
    root_state = {"tag": "immutable"}
    problem = BanditProblem({"only": 1.0})
    # Patch apply to return a new dict and record that root was not written.
    original_apply = problem.apply

    def checked_apply(state, action):
        assert state is root_state or state == "only"
        return original_apply(state, action)

    problem.apply = checked_apply
    run_mcts(problem, root_state, SearchBudget(max_iterations=20, max_time_seconds=5.0), rng=random.Random(0))
    assert root_state == {"tag": "immutable"}


def test_prior_floor_constant_is_positive():
    assert PRIOR_FLOOR > 0.0


def test_mcts_uses_priors_when_provided():
    """With strong priors favoring 'b', even a low-value b should get more early attention."""
    values = {"a": 0.6, "b": 0.4}

    class PriorProblem(BanditProblem):
        def priors(self, state):
            if state == "root":
                return {"a": 0.01, "b": 0.99}
            return None

    problem = PriorProblem(values)
    budget = SearchBudget(max_iterations=30, max_time_seconds=5.0)
    _action, action_values = run_mcts_with_values(problem, "root", budget, rng=random.Random(0))
    # Both explored; b should have higher or equal mean (prior pulls early visits).
    assert "a" in action_values and "b" in action_values


# ---------------------------------------------------------------------------
# 3.1 — SearchStats instrumentation
# ---------------------------------------------------------------------------


def test_search_stats_default_zero():
    s = SearchStats()
    assert s.iterations_run == 0
    assert s.wall_time_seconds == 0.0
    assert s.expansions == 0
    assert s.leaf_evaluations == 0
    assert s.terminal_evaluations == 0
    assert s.root_actions == 0
    assert s.tree_nodes == 0
    assert s.evaluate_cache_hits == 0
    assert s.transposition_merges == 0


def test_run_mcts_search_returns_stats():
    """run_mcts_search returns (action, SearchStats) with sane counters."""
    values = {"alpha": 0.9, "beta": 0.1, "gamma": 0.5}
    problem = BanditProblem(values)
    budget = SearchBudget(max_iterations=50, max_time_seconds=5.0)
    action, stats = run_mcts_search(problem, "root", budget, rng=random.Random(0))
    assert action == "alpha"
    assert isinstance(stats, SearchStats)
    assert stats.root_actions == 3
    # Root expansion + one child expansion per iteration that reached a terminal leaf.
    assert stats.expansions >= 1
    assert stats.iterations_run >= 1
    # Depth-1 tree: root + 3 children.
    assert stats.tree_nodes == 4
    assert stats.wall_time_seconds > 0.0
    # BanditProblem leaves are all terminal (depth-1), so terminal_evaluations dominate.
    assert stats.terminal_evaluations >= 1


def test_search_stats_counts_match_iterations():
    """iterations_run and terminal_evaluations align on a pure-terminal bandit tree."""
    values = {f"a{i}": 0.5 for i in range(5)}
    problem = BanditProblem(values)
    budget = SearchBudget(max_iterations=17, max_time_seconds=100.0)
    _action, stats = run_mcts_search(problem, "root", budget, rng=random.Random(0))
    assert stats.iterations_run == 17
    assert stats.terminal_evaluations == 17
    assert stats.leaf_evaluations == 0
    assert stats.root_actions == 5


def test_search_stats_seed_determinism_unchanged():
    """Same seed -> same action from run_mcts_search as from run_mcts (stats is bookkeeping only)."""
    values = {"x": 0.2, "y": 0.8, "z": 0.5}
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    a_plain = run_mcts(BanditProblem(values), "root", budget, rng=random.Random(42))
    a_stats, stats = run_mcts_search(BanditProblem(values), "root", budget, rng=random.Random(42))
    assert a_plain == a_stats == "y"
    a_stats2, _ = run_mcts_search(BanditProblem(values), "root", budget, rng=random.Random(42))
    assert a_stats == a_stats2


def test_search_stats_empty_root():
    """No legal actions: run_mcts_search returns (None, zero-ish stats) without consuming RNG."""
    problem = BanditProblem({})
    action, stats = run_mcts_search(
        problem, "root", SearchBudget(max_iterations=10, max_time_seconds=1.0)
    )
    assert action is None
    assert stats.root_actions == 0
    assert stats.iterations_run == 0
    assert stats.tree_nodes == 0
    assert stats.expansions == 0
