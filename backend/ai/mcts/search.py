"""
Domain-agnostic MCTS (PUCT) search.

The search operates behind a `Problem` protocol so any phase can plug in its own
legal-action enumerator, transition function, and leaf evaluator. States must be
treated as immutable by the Problem (the search never mutates them in place).
"""

from __future__ import annotations

import random
import time
from typing import Protocol, TypeVar

from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.node import Node

A = TypeVar("A")  # action type
S = TypeVar("S")  # state type

# PUCT exploration constant. ~1.4 is the AlphaGo Zero default; good general starting point.
DEFAULT_C_PUCT = 1.4

# Floor on each action's prior so no legal action is completely starved of visits.
PRIOR_FLOOR = 1e-3


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


def _expand(problem: Problem[S, A], node: Node[S, A]) -> None:
    """Create children for all legal actions of node's state."""
    actions = problem.legal_actions(node.state)
    if not actions:
        node.mark_expanded()
        return
    priors = _normalize_priors(problem.priors(node.state), actions)
    for action in actions:
        child_state = problem.apply(node.state, action)
        child = Node(state=child_state, prior=priors.get(action, PRIOR_FLOOR))
        node.add_child(action, child)
    node.untried_actions = []
    node.mark_expanded()


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
    root_actions = problem.legal_actions(root_state)
    if not root_actions:
        return None

    rng = rng if rng is not None else random.Random()
    root: Node[S, A] = Node(state=root_state)
    _expand(problem, root)

    start = time.monotonic()
    for _ in range(budget.max_iterations):
        if time.monotonic() - start >= budget.max_time_seconds:
            break

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
                _expand(problem, node)
            # Pick a child to evaluate: prefer least-visited (balances the tree).
            if node.children:
                _action, child = min(node.children.items(), key=lambda kv: kv[1].visit_count)
                node = child
                path.append(node)

        # --- Evaluate / terminal ---
        if problem.is_terminal(node.state):
            value = problem.terminal_value(node.state)
        else:
            value = problem.evaluate(node.state)

        # --- Backup ---
        for ancestor in path:
            ancestor.visit_count += 1
            ancestor.total_value += value

    best_action, _best_child = root.best_child_by_visits()
    return best_action


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
    root_actions = problem.legal_actions(root_state)
    if not root_actions:
        return None, {}

    rng = rng if rng is not None else random.Random()
    root: Node[S, A] = Node(state=root_state)
    _expand(problem, root)

    start = time.monotonic()
    for _ in range(budget.max_iterations):
        if time.monotonic() - start >= budget.max_time_seconds:
            break

        node = root
        path: list[Node[S, A]] = [root]
        while node.is_fully_expanded and not problem.is_terminal(node.state) and node.children:
            _action, node = node.select_child(c_puct)
            path.append(node)

        if not problem.is_terminal(node.state) and not node.is_fully_expanded:
            if node.untried_actions or not node.children:
                _expand(problem, node)
            if node.children:
                _action, child = min(node.children.items(), key=lambda kv: kv[1].visit_count)
                node = child
                path.append(node)

        if problem.is_terminal(node.state):
            value = problem.terminal_value(node.state)
        else:
            value = problem.evaluate(node.state)

        for ancestor in path:
            ancestor.visit_count += 1
            ancestor.total_value += value

    values = {a: child.value for a, child in root.children.items()}
    best_action, _ = root.best_child_by_visits()
    return best_action, values
