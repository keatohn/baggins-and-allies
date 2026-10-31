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

# The five decision phases that route through MCTS (see backend/ai/decide.py).
PHASES: tuple[str, ...] = (
    "purchase",
    "combat",
    "combat_move",
    "non_combat_move",
    "mobilization",
)

# Per-phase difficulty -> budget table. 3.5a initializes every phase from the
# flat BUDGETS table (zero behavior change vs get_budget); per-phase retuning
# (3.5b) edits the entries here, e.g. cheaper budgets for the slow move-phase
# enumerators measured in the 3.1 benchmark.
PHASE_BUDGETS: dict[str, dict[str, SearchBudget]] = {phase: dict(BUDGETS) for phase in PHASES}


def get_budget(difficulty: str | None = None) -> SearchBudget:
    """Resolve a budget by difficulty name; falls back to DEFAULT_DIFFICULTY."""
    key = _resolve_difficulty(difficulty)
    return BUDGETS.get(key, BUDGETS[DEFAULT_DIFFICULTY])


def get_phase_budget(phase: str, difficulty: str | None = None) -> SearchBudget:
    """
    Resolve a budget for one decision phase by difficulty name.

    Falls back to the global BUDGETS table when the phase is unknown, and to
    DEFAULT_DIFFICULTY when the difficulty name is unknown — so env-driven
    difficulty selection behaves exactly like get_budget() until per-phase
    tables diverge (3.5b).
    """
    key = _resolve_difficulty(difficulty)
    table = PHASE_BUDGETS.get(phase, BUDGETS)
    return table.get(key, BUDGETS[DEFAULT_DIFFICULTY])


def _resolve_difficulty(difficulty: str | None) -> str:
    return (difficulty or os.environ.get("AI_MCTS_DIFFICULTY") or DEFAULT_DIFFICULTY).strip().lower()
