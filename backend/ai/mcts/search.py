"""
Domain-agnostic MCTS (PUCT) search.

The search operates behind a `Problem` protocol so any phase can plug in its own
legal-action enumerator, transition function, and leaf evaluator. States must be
treated as immutable by the Problem (the search never mutates them in place).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Protocol, TypeVar

from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.node import Node

A = TypeVar("A")  # action type
S = TypeVar("S")  # state type

# PUCT exploration constant. ~1.4 is the AlphaGo Zero default; good general starting point.
DEFAULT_C_PUCT = 1.4

# Floor on each action's prior so no legal action is completely starved of visits.
PRIOR_FLOOR = 1e-3


@dataclass
class SearchStats:
    """
    Counters from one MCTS run (building block 3.1 instrumentation).

    Pure bookkeeping: integer increments only — no RNG consumption and no effect
    on selection, expansion, or backup. Returned alongside the chosen action via
    `run_mcts_search` so benchmarks and later building blocks (transposition
    table, tree reuse, budget tuning) have a measurement foundation.
    """

    iterations_run: int = 0
    wall_time_seconds: float = 0.0
    expansions: int = 0
    leaf_evaluations: int = 0
    terminal_evaluations: int = 0
    root_actions: int = 0
    tree_nodes: int = 0


class Problem(Protocol[S, A]):
    """
    Contract a domain must implement to run MCTS.

    States must be treated as immutable: `apply` returns a new state and must not
    mutate the input. Actions should be hashable (used as dict keys).
    """

    def legal_actions(self, state: S) -> list[A]:
        """Enumerate legal actions for this state. May be empty for terminal states."""
        ...

    def apply(self, state: S, action: A) -> S:
        """Return the successor state. Must not mutate `state`."""
        ...

    def is_terminal(self, state: S) -> bool:
        """True when no further actions apply / the episode is over."""
        ...

    def evaluate(self, state: S) -> float:
        """
        Leaf evaluation in [-1, 1] from the ROOT faction's perspective.
        Called for non-terminal leaves; ignored for terminal states (backup uses ±1).
        """
        ...

    def terminal_value(self, state: S) -> float:
        """
        Value of a terminal state in [-1, 1] from the ROOT faction's perspective.
        Default implementations may return +1 / -1 / 0 for win / loss / draw.
        """
        ...

    def priors(self, state: S) -> dict[A, float] | None:
        """
        Optional heuristic priors over legal_actions(state).
        Values are relative weights (need not sum to 1); normalized internally.
        Return None (or omit) for uniform priors.
        """
        ...

    def enumerator_size(self, state: S) -> int:
        """
        Optional hook (3.1): branching factor the enumerator yields for `state`.
        Benchmarks use this to measure per-phase action-space size; the search
        itself never calls it. Default: len(legal_actions(state)).
        """
        ...


def _normalize_priors(priors: dict[A, float] | None, actions: list[A]) -> dict[A, float]:
    """Normalize prior weights to a probability distribution with a floor on every action."""
    n = len(actions)
    if n == 0:
        return {}
    if not priors:
        return {a: 1.0 / n for a in actions}
    raw = {a: max(0.0, float(priors.get(a, 0.0))) for a in actions}
    total = sum(raw.values())
    if total <= 0.0:
        return {a: 1.0 / n for a in actions}
    # Apply floor then renormalize so every action gets at least PRIOR_FLOOR mass.
    floored = {a: max(PRIOR_FLOOR, w / total) for a, w in raw.items()}
    z = sum(floored.values())
    return {a: w / z for a, w in floored.items()}


def _expand(
    problem: Problem[S, A],
    node: Node[S, A],
    stats: SearchStats | None = None,
) -> int:
    """Create children for all legal actions of node's state. Returns child count."""
    actions = problem.legal_actions(node.state)
    if not actions:
        node.mark_expanded()
        return 0
    priors = _normalize_priors(problem.priors(node.state), actions)
    for action in actions:
        child_state = problem.apply(node.state, action)
        child = Node(state=child_state, prior=priors.get(action, PRIOR_FLOOR))
        node.add_child(action, child)
    node.untried_actions = []
    node.mark_expanded()
    if stats is not None:
        stats.expansions += 1
    return len(actions)


def _count_nodes(root: Node[S, A]) -> int:
    """Total nodes reachable from root (for SearchStats.tree_nodes)."""
    count = 0
    stack: list[Node[S, A]] = [root]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children.values())
    return count


def _search_core(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
    stats: SearchStats | None = None,
) -> tuple[A | None, dict[A, float], Node[S, A] | None]:
    """
    Unified PUCT loop shared by run_mcts / run_mcts_with_values / run_mcts_search.

    Returns (best_action, root_action_mean_values, root_node). When `stats` is
    given, counters are populated in place; selection/RNG behavior is identical
    whether stats is None or not (counters are integer increments only).
    """
    root_actions = problem.legal_actions(root_state)
    if stats is not None:
        stats.root_actions = len(root_actions)
    if not root_actions:
        return None, {}, None

    rng = rng if rng is not None else random.Random()
    root: Node[S, A] = Node(state=root_state)
    _expand(problem, root, stats)

    start = time.monotonic()
    for _ in range(budget.max_iterations):
        if time.monotonic() - start >= budget.max_time_seconds:
            break
        if stats is not None:
            stats.iterations_run += 1

        node = root
        # --- Select ---
        # Walk down fully-expanded non-terminal nodes via PUCT, recording the path
        # so backup can update every node on it.
        path: list[Node[S, A]] = [root]
        while node.is_fully_expanded and not problem.is_terminal(node.state) and node.children:
            _action, node = node.select_child(c_puct)
            path.append(node)

        # --- Expand ---
        if not problem.is_terminal(node.state) and not node.is_fully_expanded:
            if node.untried_actions or not node.children:
                _expand(problem, node, stats)
            # Pick a child to evaluate: prefer least-visited (balances the tree).
            if node.children:
                _action, child = min(node.children.items(), key=lambda kv: kv[1].visit_count)
                node = child
                path.append(node)

        # --- Evaluate / terminal ---
        if problem.is_terminal(node.state):
            value = problem.terminal_value(node.state)
            if stats is not None:
                stats.terminal_evaluations += 1
        else:
            value = problem.evaluate(node.state)
            if stats is not None:
                stats.leaf_evaluations += 1

        # --- Backup ---
        for ancestor in path:
            ancestor.visit_count += 1
            ancestor.total_value += value

    if stats is not None:
        stats.wall_time_seconds = time.monotonic() - start
        stats.tree_nodes = _count_nodes(root)

    values = {a: child.value for a, child in root.children.items()}
    best_action, _best_child = root.best_child_by_visits()
    return best_action, values, root


def run_mcts(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
) -> A | None:
    """
    Run PUCT MCTS from `root_state` and return the best root action (most visited),
    or None if no legal actions exist at the root.

    The search never mutates `root_state`; all transitions go through `problem.apply`.
    """
    action, _values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct
    )
    return action


def run_mcts_with_values(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
) -> tuple[A | None, dict[A, float]]:
    """
    Like `run_mcts` but also returns each root action's mean value (root perspective).
    Useful for tests and debugging.
    """
    action, values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct
    )
    return action, values


def run_mcts_search(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
) -> tuple[A | None, SearchStats]:
    """
    Like `run_mcts` but also returns SearchStats counters for the run.
    Counters are pure bookkeeping — identical seeds produce identical actions
    with or without stats tracking.
    """
    stats = SearchStats()
    action, _values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct, stats=stats
    )
    return action, stats
