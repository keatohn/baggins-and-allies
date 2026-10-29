"""
MCTS (Monte Carlo Tree Search) package.

Building block 1 (this package's core) is domain-agnostic: `search.py` runs PUCT
behind a `Problem` protocol so any game phase can plug in its own enumerator and
evaluator. Phase-specific Problems live in sibling modules (e.g. `purchase.py`).
"""

from backend.ai.mcts.budget import BUDGETS, DEFAULT_DIFFICULTY, SearchBudget, get_budget
from backend.ai.mcts.node import Node
from backend.ai.mcts.search import (
    DEFAULT_C_PUCT,
    PRIOR_FLOOR,
    Problem,
    run_mcts,
    run_mcts_with_values,
)

__all__ = [
    "BUDGETS",
    "DEFAULT_C_PUCT",
    "DEFAULT_DIFFICULTY",
    "PRIOR_FLOOR",
    "Node",
    "Problem",
    "SearchBudget",
    "get_budget",
    "run_mcts",
    "run_mcts_with_values",
]
