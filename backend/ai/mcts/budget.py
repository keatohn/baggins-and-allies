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

# Per-phase retuning (3.5b), calibrated against the 3.1 latency baseline after the
# move-phase enumerator optimizations that landed with it (batched threat map, one
# shared post-pending snapshot, per-state enumeration memo). Each child expansion
# in a move phase still pays a full top-K enumeration + validates (~50ms measured
# per expansion on the benchmark snapshot, more on bloated mid-game boards), so the
# two move phases get roughly a third of the flat iteration caps and a 1.2s hard
# wall (vs 2.0s flat): worst-case search cost is wall time plus one overshooting
# iteration, keeping a "normal" per-action search under the ≲2s target (the
# frontend waits AI_STEP_DELAY_MS=2500 between /ai-step calls anyway). The fast
# phases (purchase, combat, mobilization) exhaust or bottom out long before the
# flat caps bind, so they keep the flat table.
PHASE_BUDGET_OVERRIDES: dict[str, dict[str, SearchBudget]] = {
    "combat_move": {
        "easy": SearchBudget(max_iterations=50, max_time_seconds=0.1),
        "normal": SearchBudget(max_iterations=200, max_time_seconds=0.4),
        "hard": SearchBudget(max_iterations=500, max_time_seconds=1.2),
    },
    "non_combat_move": {
        "easy": SearchBudget(max_iterations=50, max_time_seconds=0.1),
        "normal": SearchBudget(max_iterations=150, max_time_seconds=0.45),
        "hard": SearchBudget(max_iterations=400, max_time_seconds=1.2),
    },
}

# Per-phase difficulty -> budget table. Each phase starts from the flat BUDGETS
# table with that phase's overrides applied on top; unknown phases resolve to
# the flat table via get_phase_budget.
PHASE_BUDGETS: dict[str, dict[str, SearchBudget]] = {
    phase: dict(BUDGETS, **PHASE_BUDGET_OVERRIDES.get(phase, {}))
    for phase in PHASES
}


def get_budget(difficulty: str | None = None) -> SearchBudget:
    """Resolve a budget by difficulty name; falls back to DEFAULT_DIFFICULTY."""
    key = _resolve_difficulty(difficulty)
    return BUDGETS.get(key, BUDGETS[DEFAULT_DIFFICULTY])


def get_phase_budget(phase: str, difficulty: str | None = None) -> SearchBudget:
    """
    Resolve a budget for one decision phase by difficulty name.

    Falls back to the global BUDGETS table when the phase is unknown, and to
    DEFAULT_DIFFICULTY when the difficulty name is unknown — so env-driven
    difficulty selection behaves exactly like get_budget() for phases without
    a retuned entry.
    """
    key = _resolve_difficulty(difficulty)
    table = PHASE_BUDGETS.get(phase, BUDGETS)
    return table.get(key, table.get(DEFAULT_DIFFICULTY, BUDGETS[DEFAULT_DIFFICULTY]))


def _resolve_difficulty(difficulty: str | None) -> str:
    return (difficulty or os.environ.get("AI_MCTS_DIFFICULTY") or DEFAULT_DIFFICULTY).strip().lower()
