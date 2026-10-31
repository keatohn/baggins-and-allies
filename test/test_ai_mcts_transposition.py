"""
Tests for the in-run transposition table (building block 3.3).

Covers: seeded determinism with and without TT, convergence, evaluation
memoization + node merging on an order-independent toy problem, absence of
cross-run leakage, self-loop safety, and the default state_key fallback.
"""

import random
import time

from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.search import (
    _default_state_key,
    run_mcts,
    run_mcts_search,
)


# ---------------------------------------------------------------------------
# Synthetic problems
# ---------------------------------------------------------------------------


class BanditProblem:
    """Root -> terminal leaves with prescribed values (depth 1)."""

    def __init__(self, leaf_values: dict[str, float]) -> None:
        self.leaf_values = dict(leaf_values)

    def legal_actions(self, state):
        if state == "root":
            return sorted(self.leaf_values.keys())
        return []

    def apply(self, state, action):
        assert state == "root"
        return action

    def is_terminal(self, state):
        return state != "root"

    def evaluate(self, state):
        return 0.0

    def terminal_value(self, state):
        return self.leaf_values.get(state, 0.0)

    def priors(self, state):
        return None


class CommutativeProblem:
    """
    Order-independent pick problem: the state is the frozenset of picked items,
    so any two action orders that pick the same items reach the SAME state —
    exactly the redundancy a transposition table should eliminate.

    Terminal after `depth` picks; partial sets are evaluated. Counters track
    apply() calls so tests can show the TT skipping redundant expansions.
    """

    def __init__(self, items: tuple[str, ...], depth: int = 3) -> None:
        self.items = items
        self.depth = depth
        self.evaluate_calls = 0
        self.apply_calls = 0

    def legal_actions(self, state):
        if len(state) >= self.depth:
            return []
        return [f"pick_{it}" for it in self.items if it not in state]

    def apply(self, state, action):
        self.apply_calls += 1
        return state | {action.split("_", 1)[1]}

    def is_terminal(self, state):
        return len(state) >= self.depth

    def evaluate(self, state):
        self.evaluate_calls += 1
        # Deterministic value in [-1, 1] from the item weights.
        raw = sum(ord(c) for it in state for c in it) / 50.0
        return raw / (1.0 + abs(raw))

    def terminal_value(self, state):
        # Cheap and independent of evaluate() so the evaluate_calls counter
        # measures leaf evaluations only.
        return 0.5 if len(state) == self.depth else 0.0

    def priors(self, state):
        return None


class SelfLoopProblem:
    """apply() can return an equal state (a null "noop" transition)."""

    def __init__(self) -> None:
        self.apply_calls = 0

    def legal_actions(self, state):
        return ["noop", "advance"] if state < 2 else []

    def apply(self, state, action):
        self.apply_calls += 1
        return state if action == "noop" else state + 1

    def is_terminal(self, state):
        return state >= 2

    def evaluate(self, state):
        return state / 10.0

    def terminal_value(self, state):
        return 1.0 if state == 2 else 0.0

    def priors(self, state):
        return None


class DiamondProblem:
    """
    root -> L/R -> distinct x/y -> (single action each) -> SHARED m -> terminal t.

    The shared m is reached from both x and y, so with TT the second parent
    min-picks an already-evaluated node: the evaluate() memo must fire.
    Deterministic (no RNG anywhere) so the hit count is exact.
    """

    _TRANSITIONS = {"L": "x", "R": "y", "go": "m", "finish": "t"}

    def __init__(self) -> None:
        self.evaluate_calls = 0

    def legal_actions(self, state):
        return {
            "root": ["L", "R"],
            "x": ["go"],
            "y": ["go"],
            "m": ["finish"],
            "t": [],
        }[state]

    def apply(self, state, action):
        return self._TRANSITIONS[action]

    def is_terminal(self, state):
        return state == "t"

    def evaluate(self, state):
        self.evaluate_calls += 1
        return 0.0  # zero so PUCT's U-term (not Q) drives x-then-y exploration

    def terminal_value(self, state):
        return 1.0

    def priors(self, state):
        return None


# ---------------------------------------------------------------------------
# state_key default fallback
# ---------------------------------------------------------------------------


def test_default_state_key_uses_hashable_state_directly():
    assert _default_state_key("root") == "root"
    assert _default_state_key(("a", 1)) == ("a", 1)


def test_default_state_key_unhashable_uses_to_dict():
    class DictState:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return dict(self._d)

        def __eq__(self, other):
            return isinstance(other, DictState) and self._d == other._d

        # Explicitly unhashable, like GameState (mutable dataclass).
        __hash__ = None

    a = DictState({"x": 1, "y": [1, 2]})
    b = DictState({"x": 1, "y": [1, 2]})
    c = DictState({"x": 2})
    assert _default_state_key(a) == _default_state_key(b)
    assert _default_state_key(a) != _default_state_key(c)


# ---------------------------------------------------------------------------
# Determinism / convergence with and without TT
# ---------------------------------------------------------------------------


def test_seeded_determinism_with_tt():
    values = {"x": 0.2, "y": 0.8, "z": 0.5}
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    a1, s1 = run_mcts_search(BanditProblem(values), "root", budget, rng=random.Random(42))
    a2, s2 = run_mcts_search(BanditProblem(values), "root", budget, rng=random.Random(42))
    assert a1 == a2 == "y"

    def counters(s):
        return (
            s.iterations_run,
            s.expansions,
            s.leaf_evaluations,
            s.terminal_evaluations,
            s.root_actions,
            s.tree_nodes,
            s.evaluate_cache_hits,
            s.transposition_merges,
        )

    assert counters(s1) == counters(s2)


def test_seeded_determinism_without_tt():
    values = {"x": 0.2, "y": 0.8, "z": 0.5}
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    a1, s1 = run_mcts_search(
        BanditProblem(values), "root", budget, rng=random.Random(42), use_tt=False
    )
    a2, s2 = run_mcts_search(
        BanditProblem(values), "root", budget, rng=random.Random(42), use_tt=False
    )
    assert a1 == a2 == "y"
    assert s1.transposition_merges == s2.transposition_merges == 0
    assert s1.evaluate_cache_hits == s2.evaluate_cache_hits == 0


def test_convergence_still_passes_with_tt():
    values = {"alpha": 0.9, "beta": 0.1, "gamma": 0.5}
    budget = SearchBudget(max_iterations=300, max_time_seconds=5.0)
    action = run_mcts(BanditProblem(values), "root", budget, rng=random.Random(0))
    assert action == "alpha"


# ---------------------------------------------------------------------------
# TT effectiveness on an order-independent problem
# ---------------------------------------------------------------------------


_ITEMS = ("a", "b", "c", "d")


def test_tt_reduces_work_on_order_independent_problem():
    """
    Same picks in different orders hit one shared node: the TT skips the
    redundant expansions (fewer apply() calls, smaller tree) instead of
    building duplicate subtrees.

    Note: with this core loop's lazy expansion, each node's evaluate() already
    fires at most once, so the win shows up as saved expansions/applications —
    the evaluate() memo is a safety net for future loop/eager-eval changes.
    """
    budget = SearchBudget(max_iterations=300, max_time_seconds=5.0)

    problem_tt = CommutativeProblem(_ITEMS)
    _a_tt, stats_tt = run_mcts_search(problem_tt, frozenset(), budget, rng=random.Random(1))

    problem_plain = CommutativeProblem(_ITEMS)
    _a_plain, stats_plain = run_mcts_search(
        problem_plain, frozenset(), budget, rng=random.Random(1), use_tt=False
    )

    # Merging happened (duplicate states collapsed into shared nodes).
    assert stats_tt.transposition_merges > 0

    # Redundant transitions skipped: strictly fewer apply() expansions, and the
    # unique DAG (1 + 4 singletons + 6 pairs + 4 triples = 15) is far smaller
    # than the full expansion tree (1 + 4 + 12 + 24 = 41).
    assert problem_tt.apply_calls < problem_plain.apply_calls
    assert stats_tt.tree_nodes < stats_plain.tree_nodes
    assert stats_tt.tree_nodes == 15
    assert stats_plain.tree_nodes == 41

    # Leaf-evaluation work is bounded by unique non-terminal states, never more
    # than the no-TT run.
    assert problem_tt.evaluate_calls <= problem_plain.evaluate_calls

    # Without TT the counters stay at zero.
    assert stats_plain.transposition_merges == 0
    assert stats_plain.evaluate_cache_hits == 0


def test_tt_search_still_returns_a_legal_action():
    budget = SearchBudget(max_iterations=100, max_time_seconds=5.0)
    action = run_mcts(
        CommutativeProblem(_ITEMS), frozenset(), budget, rng=random.Random(3)
    )
    assert action is not None
    assert action.startswith("pick_")


def test_evaluate_memo_fires_on_shared_state():
    """
    Diamond shape: x and y each have a single edge into the same state m, so
    the second parent min-picks an already-evaluated node — the memo must
    serve it without calling evaluate() again (1 call with TT vs 2 without).
    """
    budget = SearchBudget(max_iterations=20, max_time_seconds=5.0)

    problem_tt = DiamondProblem()
    action_tt, stats_tt = run_mcts_search(problem_tt, "root", budget, rng=random.Random(0))
    assert stats_tt.transposition_merges == 1
    assert stats_tt.evaluate_cache_hits == 1
    assert problem_tt.evaluate_calls == 1

    problem_plain = DiamondProblem()
    action_plain, stats_plain = run_mcts_search(
        problem_plain, "root", budget, rng=random.Random(0), use_tt=False
    )
    assert stats_plain.evaluate_cache_hits == 0
    assert problem_plain.evaluate_calls == 2  # duplicate m evaluated once per parent

    # Both modes still find a legal root action leading to the +1 terminal.
    assert action_tt in {"L", "R"}
    assert action_plain in {"L", "R"}


# ---------------------------------------------------------------------------
# No cross-run leakage
# ---------------------------------------------------------------------------


def test_no_cross_run_leakage():
    """A second run on the same problem instance is independent of the first."""
    problem = CommutativeProblem(_ITEMS)
    budget = SearchBudget(max_iterations=150, max_time_seconds=5.0)

    a1, s1 = run_mcts_search(problem, frozenset(), budget, rng=random.Random(7))
    calls_after_first = problem.evaluate_calls

    a2, s2 = run_mcts_search(problem, frozenset(), budget, rng=random.Random(7))

    # Identical results and identical counter profiles — a leaked table would
    # shrink run 2's expansions/merges/evaluations.
    assert a1 == a2
    assert s1.iterations_run == s2.iterations_run
    assert s1.expansions == s2.expansions
    assert s1.transposition_merges == s2.transposition_merges
    assert s1.leaf_evaluations == s2.leaf_evaluations
    assert s1.evaluate_cache_hits == s2.evaluate_cache_hits
    assert s1.tree_nodes == s2.tree_nodes
    # Both runs really evaluated leaves (no reuse of run 1's memo cache).
    assert problem.evaluate_calls == 2 * calls_after_first


# ---------------------------------------------------------------------------
# Self-loop safety (null transitions must not hang the search)
# ---------------------------------------------------------------------------


def test_self_loop_transition_terminates():
    problem = SelfLoopProblem()
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    start = time.monotonic()
    action, stats = run_mcts_search(problem, 0, budget, rng=random.Random(0))
    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    assert action in {"noop", "advance"}
    assert stats.iterations_run >= 1
    # "advance" is the only path to the +1 terminal, so it should win the search.
    assert action == "advance"


def test_self_loop_without_tt_terminates():
    problem = SelfLoopProblem()
    budget = SearchBudget(max_iterations=200, max_time_seconds=5.0)
    action = run_mcts(problem, 0, budget, rng=random.Random(0), use_tt=False)
    assert action == "advance"
