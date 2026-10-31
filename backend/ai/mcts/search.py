"""
Domain-agnostic MCTS (PUCT) search.

The search operates behind a `Problem` protocol so any phase can plug in its own
legal-action enumerator, transition function, and leaf evaluator. States must be
treated as immutable by the Problem (the search never mutates them in place).

Building block 3.3 adds an in-run transposition table (TT):
- identical states reached via different action orders merge into one shared Node
  (a DAG) so redundant lines are not re-expanded;
- leaf `evaluate()` results are memoized per run;
- a Problem may override `state_key` for a cheaper structural hash; the default
  handles hashable states directly and falls back to `to_dict()` + sorted JSON
  (measured cheap at ~0.24ms by the 3.1 benchmark) for GameState-like states.
The table is strictly per-run: no cross-run leakage, and `use_tt=False`
reproduces the pre-TT tree behavior exactly.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Hashable, Protocol, TypeVar

from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.node import Node

A = TypeVar("A")  # action type
S = TypeVar("S")  # state type

# PUCT exploration constant. ~1.4 is the AlphaGo Zero default; good general starting point.
DEFAULT_C_PUCT = 1.4

# Floor on each action's prior so no legal action is completely starved of visits.
PRIOR_FLOOR = 1e-3

# Safety cap on select-path length. State merging can in principle create cycles
# (two action orders reaching the same state); this bounds any such pathology.
MAX_SELECT_DEPTH = 128

_MISSING = object()


def _default_state_key(state: Any) -> Hashable:
    """
    Cheap structural key for the transposition table.

    Hashable states (strings, tuples, frozensets, ...) are used directly — exact
    and free. Unhashable states (e.g. GameState, a mutable dataclass) fall back
    to `to_dict()` + sorted compact JSON hashed to an int: the form measured by
    the 3.1 benchmark at ~0.24ms / ~28KB per call, cheap enough per node.
    Collisions at our tree sizes (~10^3 nodes) are negligible, and a wrong merge
    only shares an evaluation — it never produces an illegal action.
    """
    try:
        hash(state)
        return state
    except TypeError:
        pass
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        blob = json.dumps(to_dict(), sort_keys=True, separators=(",", ":"), default=str)
        return hash(blob)
    return repr(state)


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
    # 3.3 transposition-table counters (0 when use_tt=False).
    evaluate_cache_hits: int = 0
    transposition_merges: int = 0


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

    def state_key(self, state: S) -> Hashable:
        """
        Optional (3.3): cheap structural hash of `state` for the in-run
        transposition table. Equal states must produce equal keys; unequal states
        SHOULD produce unequal keys (a collision only shares an evaluation).
        The default implementation is `_default_state_key`; problems whose states
        are already hashable inherit correct behavior for free.
        """
        return _default_state_key(state)


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
    *,
    key_fn: Any = None,
    node_table: dict[Hashable, Node[S, A]] | None = None,
) -> int:
    """
    Create children for all legal actions of node's state. Returns child count.

    With a transposition table (`key_fn` + `node_table`), a child whose state key
    already exists reuses the existing Node (DAG merge) instead of re-expanding a
    duplicate subtree. A child whose key equals its parent's (a null transition)
    is kept as a fresh unshared node to avoid a self-loop.
    """
    actions = problem.legal_actions(node.state)
    if not actions:
        node.mark_expanded()
        return 0
    priors = _normalize_priors(problem.priors(node.state), actions)
    merges = 0
    for action in actions:
        child_state = problem.apply(node.state, action)
        prior = priors.get(action, PRIOR_FLOOR)
        child: Node[S, A]
        if key_fn is not None and node_table is not None:
            child_key = key_fn(child_state)
            existing = node_table.get(child_key)
            if existing is not None and existing is not node:
                # Same state reached via a different action order: share the node.
                child = existing
                merges += 1
            else:
                child = Node(state=child_state, prior=prior, state_key=child_key)
                if existing is None:
                    node_table[child_key] = child
        else:
            child = Node(state=child_state, prior=prior)
        node.add_child(action, child)
    node.untried_actions = []
    node.mark_expanded()
    if stats is not None:
        stats.expansions += 1
        stats.transposition_merges += merges
    return len(actions)


def _count_nodes(root: Node[S, A]) -> int:
    """Unique nodes reachable from root (for SearchStats.tree_nodes; DAG-safe)."""
    count = 0
    seen: set[int] = set()
    stack: list[Node[S, A]] = [root]
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
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
    use_tt: bool = True,
) -> tuple[A | None, dict[A, float], Node[S, A] | None]:
    """
    Unified PUCT loop shared by run_mcts / run_mcts_with_values / run_mcts_search.

    Returns (best_action, root_action_mean_values, root_node). When `stats` is
    given, counters are populated in place; selection/RNG behavior is identical
    whether stats is None or not (counters are integer increments only).

    `use_tt` (3.3, default True) enables the in-run transposition table: node
    merging for identical states + evaluate() memoization. With use_tt=False the
    search behaves exactly as the pre-3.3 tree search (no keys are computed).
    """
    root_actions = problem.legal_actions(root_state)
    if stats is not None:
        stats.root_actions = len(root_actions)
    if not root_actions:
        return None, {}, None

    rng = rng if rng is not None else random.Random()
    # Resolve the state-key function once. Prefer the Problem's own state_key;
    # fall back for problems that don't inherit the Protocol (toy test problems).
    key_fn: Any = None
    node_table: dict[Hashable, Node[S, A]] | None = None
    eval_cache: dict[Hashable, float] | None = None
    if use_tt:
        problem_key = getattr(problem, "state_key", None)
        key_fn = problem_key if callable(problem_key) else _default_state_key
        node_table = {}
        eval_cache = {}

    root: Node[S, A] = Node(state=root_state, state_key=key_fn(root_state) if key_fn else None)
    if node_table is not None and root.state_key is not None:
        node_table[root.state_key] = root
    _expand(problem, root, stats, key_fn=key_fn, node_table=node_table)

    start = time.monotonic()
    for _ in range(budget.max_iterations):
        if time.monotonic() - start >= budget.max_time_seconds:
            break
        if stats is not None:
            stats.iterations_run += 1

        node = root
        # --- Select ---
        # Walk down fully-expanded non-terminal nodes via PUCT, recording the path
        # so backup can update every node on it. Depth-capped as a cycle guard
        # (state merging can theoretically create back-edges).
        path: list[Node[S, A]] = [root]
        while (
            node.is_fully_expanded
            and not problem.is_terminal(node.state)
            and node.children
            and len(path) < MAX_SELECT_DEPTH
        ):
            _action, node = node.select_child(c_puct)
            path.append(node)

        # --- Expand ---
        if not problem.is_terminal(node.state) and not node.is_fully_expanded:
            if node.untried_actions or not node.children:
                _expand(problem, node, stats, key_fn=key_fn, node_table=node_table)
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
        elif eval_cache is not None:
            # Memoize leaf evaluations per run (part of the 3.3 TT).
            node_key = node.state_key if node.state_key is not None else key_fn(node.state)
            cached = eval_cache.get(node_key, _MISSING)
            if cached is not _MISSING:
                value = cached  # type: ignore[assignment]
                if stats is not None:
                    stats.evaluate_cache_hits += 1
            else:
                value = problem.evaluate(node.state)
                eval_cache[node_key] = value
                if stats is not None:
                    stats.leaf_evaluations += 1
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
    use_tt: bool = True,
) -> A | None:
    """
    Run PUCT MCTS from `root_state` and return the best root action (most visited),
    or None if no legal actions exist at the root.

    The search never mutates `root_state`; all transitions go through `problem.apply`.
    """
    action, _values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct, use_tt=use_tt
    )
    return action


def run_mcts_with_values(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
    use_tt: bool = True,
) -> tuple[A | None, dict[A, float]]:
    """
    Like `run_mcts` but also returns each root action's mean value (root perspective).
    Useful for tests and debugging.
    """
    action, values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct, use_tt=use_tt
    )
    return action, values


def run_mcts_search(
    problem: Problem[S, A],
    root_state: S,
    budget: SearchBudget,
    *,
    rng: random.Random | None = None,
    c_puct: float = DEFAULT_C_PUCT,
    use_tt: bool = True,
) -> tuple[A | None, SearchStats]:
    """
    Like `run_mcts` but also returns SearchStats counters for the run.
    Counters are pure bookkeeping — identical seeds produce identical actions
    with or without stats tracking.
    """
    stats = SearchStats()
    action, _values, _root = _search_core(
        problem, root_state, budget, rng=rng, c_puct=c_puct, stats=stats, use_tt=use_tt
    )
    return action, stats
