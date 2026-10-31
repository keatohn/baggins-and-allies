"""
MCTS (Monte Carlo Tree Search) package.

Building block 1 (this package's core) is domain-agnostic: `search.py` runs PUCT
behind a `Problem` protocol so any game phase can plug in its own enumerator and
evaluator. Phase-specific Problems live in sibling modules (e.g. `purchase.py`).
"""

from backend.ai.mcts.budget import (
    BUDGETS,
    DEFAULT_DIFFICULTY,
    PHASES,
    PHASE_BUDGETS,
    SearchBudget,
    get_budget,
    get_phase_budget,
)
from backend.ai.mcts.node import Node
from backend.ai.mcts.search import (
    DEFAULT_C_PUCT,
    PRIOR_FLOOR,
    Problem,
    SearchStats,
    run_mcts,
    run_mcts_reuse,
    run_mcts_search,
    run_mcts_with_values,
)

__all__ = [
    "BUDGETS",
    "DEFAULT_C_PUCT",
    "DEFAULT_DIFFICULTY",
    "PHASES",
    "PHASE_BUDGETS",
    "PRIOR_FLOOR",
    "Node",
    "Problem",
    "SearchBudget",
    "SearchStats",
    "get_budget",
    "get_phase_budget",
    "run_mcts",
    "run_mcts_reuse",
    "run_mcts_search",
    "run_mcts_with_values",
]
