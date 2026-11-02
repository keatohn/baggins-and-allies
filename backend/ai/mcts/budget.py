"""
Search budgets for MCTS. Difficulty levels map to iteration + wall-time caps.
Budgets are hard caps: the search stops as soon as either limit is reached.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
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

# How many actions a phase may put in front of the search, per difficulty level.
# Combat is not here: battles are started in order and retreat is chosen after each round.
# (default, admin maximum). The default is what a level uses until it stores its own number.
BRANCH_LIMITS: dict[str, tuple[int, int]] = {
    "purchase": (48, 2000),
    "combat_move": (24, 2000),
    "non_combat_move": (24, 2000),
    "mobilization": (24, 2000),
}
BRANCH_FIELDS: dict[str, str] = {
    "purchase": "purchase_options",
    "combat_move": "combat_move_options",
    "non_combat_move": "non_combat_options",
    "mobilization": "mobilization_options",
}

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


# Set around decide() from the admin level list. Unset in tests, which keep the tables above.
_catalog_ctx: ContextVar[tuple[dict, ...] | None] = ContextVar("mcts_budget_catalog", default=None)


@contextmanager
def budget_catalog_scope(levels: list[dict] | None):
    """Use the admin's step counts and time caps for every phase while this block runs."""
    if not levels:
        yield
        return
    token = _catalog_ctx.set(tuple(levels))
    try:
        yield
    finally:
        _catalog_ctx.reset(token)


def _budget_from_catalog(key: str) -> SearchBudget | None:
    levels = _catalog_ctx.get()
    if not levels:
        return None
    by_id = {str(row.get("id")): row for row in levels if isinstance(row, dict)}
    row = by_id.get(key) or by_id.get(DEFAULT_DIFFICULTY) or levels[0]
    iterations = max(1, int(row["iterations"]))
    seconds = max(0.05, float(row["seconds"]))
    return SearchBudget(max_iterations=iterations, max_time_seconds=seconds)


def get_budget(difficulty: str | None = None) -> SearchBudget:
    """Resolve a budget by difficulty name; falls back to DEFAULT_DIFFICULTY."""
    key = _resolve_difficulty(difficulty)
    custom = _budget_from_catalog(key)
    if custom is not None:
        return custom
    return BUDGETS.get(key, BUDGETS[DEFAULT_DIFFICULTY])


def branch_limit(phase: str, difficulty: str | None = None) -> int:
    """How many options this phase may offer. Reads the active difficulty level when one is set."""
    default, ceiling = BRANCH_LIMITS.get(phase, (12, 64))
    levels = _catalog_ctx.get()
    if not levels:
        return default
    key = _resolve_difficulty(difficulty)
    by_id = {str(row.get("id")): row for row in levels if isinstance(row, dict)}
    row = by_id.get(key) or by_id.get(DEFAULT_DIFFICULTY) or levels[0]
    raw = row.get(BRANCH_FIELDS.get(phase, ""))
    if raw is None:
        return default
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(ceiling, count))


def get_phase_budget(phase: str, difficulty: str | None = None) -> SearchBudget:
    """
    Resolve a budget for one decision phase by difficulty name.

    Falls back to the global BUDGETS table when the phase is unknown, and to
    DEFAULT_DIFFICULTY when the difficulty name is unknown — so env-driven
    difficulty selection behaves exactly like get_budget() for phases without
    a retuned entry.
    """
    key = _resolve_difficulty(difficulty)
    custom = _budget_from_catalog(key)
    if custom is not None:
        return custom
    table = PHASE_BUDGETS.get(phase, BUDGETS)
    return table.get(key, table.get(DEFAULT_DIFFICULTY, BUDGETS[DEFAULT_DIFFICULTY]))


def _resolve_difficulty(difficulty: str | None) -> str:
    return (difficulty or os.environ.get("AI_MCTS_DIFFICULTY") or DEFAULT_DIFFICULTY).strip().lower()


def resolve_game_difficulty(state) -> str:
    """Env override, then the difficulty stored on the game, then the default.

    Self-play sets AI_MCTS_DIFFICULTY so a sweep is not silently rewritten by
    whatever the game row was stamped with.
    """
    env = (os.environ.get("AI_MCTS_DIFFICULTY") or "").strip()
    if env:
        return env
    stored = getattr(state, "ai_difficulty", None) if state is not None else None
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return DEFAULT_DIFFICULTY


def budget_catalog() -> list[dict[str, float | int | str]]:
    """Difficulty knobs the admin page shows. Numbers are the base search caps."""
    return [
        {
            "id": name,
            "iterations": budget.max_iterations,
            "seconds": budget.max_time_seconds,
        }
        for name, budget in BUDGETS.items()
    ]
