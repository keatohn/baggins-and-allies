"""
Search budgets for MCTS. Difficulty levels map to iteration + wall-time caps.
Budgets are hard caps: the search stops as soon as either limit is reached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchBudget:
    """Hard caps for one MCTS run."""

    max_iterations: int
    max_time_seconds: float

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.max_time_seconds <= 0:
            raise ValueError("max_time_seconds must be > 0")


# Difficulty -> budget. easy is snappy; hard trades latency for depth.
BUDGETS: dict[str, SearchBudget] = {
    "easy": SearchBudget(max_iterations=100, max_time_seconds=0.1),
    "normal": SearchBudget(max_iterations=500, max_time_seconds=0.5),
    "hard": SearchBudget(max_iterations=2000, max_time_seconds=2.0),
}

DEFAULT_DIFFICULTY = "normal"


def get_budget(difficulty: str | None = None) -> SearchBudget:
    """Resolve a budget by difficulty name; falls back to DEFAULT_DIFFICULTY."""
    key = (difficulty or os.environ.get("AI_MCTS_DIFFICULTY") or DEFAULT_DIFFICULTY).strip().lower()
    return BUDGETS.get(key, BUDGETS[DEFAULT_DIFFICULTY])
