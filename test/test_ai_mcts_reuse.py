"""
Tests for cross-call tree reuse (building block 3.4).

Covers: cache hits on successor states, work actually skipped on a warm start,
warm/cold agreement, no reuse for a repeated identical root state, invalidation
on state change, seeded determinism with a cleared cache, clone isolation, LRU
bounding, the env kill switch, the reuse=False baseline, the wiring of all five
decide_*_mcts entry points, and an end-to-end purchase flow against the real
reducer.
"""

import importlib
import random

import pytest

from backend.engine.actions import Action
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action

from backend.ai.context import AIContext
from backend.ai.mcts import tree_cache
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import SearchBudget, get_budget
from backend.ai.mcts.node import Node
from backend.ai.mcts.search import SearchStats, run_mcts, run_mcts_reuse


# ---------------------------------------------------------------------------
# Synthetic problems
# ---------------------------------------------------------------------------


class PickProblem:
    """
    Order-independent pick problem with hashable states (frozensets), so keys are
    exact and free. Counters let tests show a warm start skipping work.
    """

    def __init__(self, items: tuple[str, ...] = ("a", "b", "c", "d"), depth: int = 3):
        self.items = items
        self.depth = depth
        self.legal_calls = 0
        self.apply_calls = 0
        self.evaluate_calls = 0

    def legal_actions(self, state):
        self.legal_calls += 1
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
        raw = sum(ord(c) for it in state for c in it) / 50.0
        return raw / (1.0 + abs(raw))

    def terminal_value(self, state):
        return 1.0 if len(state) == self.depth else 0.0

    def priors(self, state):
        return None


class LinearProblem:
    """Exactly one legal action per state: warm and cold must always agree."""

    def __init__(self) -> None:
        self.legal_calls = 0

    def legal_actions(self, state):
        self.legal_calls += 1
        return ["advance"] if state < 3 else []

    def apply(self, state, action):
        return state + 1

    def is_terminal(self, state):
        return state >= 3

    def evaluate(self, state):
        return state / 10.0

    def terminal_value(self, state):
        return 1.0 if state == 3 else 0.0

    def priors(self, state):
        return None


BUDGET = SearchBudget(max_iterations=100, max_time_seconds=5.0)


# ---------------------------------------------------------------------------
# Cache behavior through run_mcts_reuse
# ---------------------------------------------------------------------------


def test_reuse_hit_on_successor_state():
    """Search at S, apply the choice, search S' -> cache hit + resumed root."""
    problem = PickProblem()
    stats1 = SearchStats()
    a1 = run_mcts_reuse(problem, frozenset(), BUDGET, rng=random.Random(1), stats=stats1)
    assert a1 is not None
    assert stats1.reused_root is False
    info = tree_cache.cache_info()
    assert info["misses"] == 1 and info["hits"] == 0

    successor = problem.apply(frozenset(), a1)
    stats2 = SearchStats()
    a2 = run_mcts_reuse(
        PickProblem(), successor, BUDGET, rng=random.Random(1), stats=stats2
    )
    assert a2 is not None
    info = tree_cache.cache_info()
    assert info["hits"] >= 1, "successor state must hit the cache"
    assert stats2.reused_root is True
    assert stats2.reused_nodes >= 1


def test_reuse_skips_root_re_expansion():
    """
    A warm start must not pay for the root expansion again: with one iteration,
    the cold run enumerates the root twice (root_actions + expand) while the
    warm run only enumerates it once (root_actions).
    """
    tiny = SearchBudget(max_iterations=1, max_time_seconds=5.0)

    first = PickProblem()
    a1 = run_mcts_reuse(first, frozenset(), tiny, rng=random.Random(2))
    successor = first.apply(frozenset(), a1)

    warm = PickProblem()
    warm_stats = SearchStats()
    run_mcts_reuse(warm, successor, tiny, rng=random.Random(2), stats=warm_stats)
    assert warm_stats.reused_root is True

    tree_cache.clear()
    cold = PickProblem()
    cold_stats = SearchStats()
    run_mcts_reuse(cold, successor, tiny, rng=random.Random(2), stats=cold_stats)
    assert cold_stats.reused_root is False

    assert warm.legal_calls < cold.legal_calls, (
        f"warm start should skip the root expansion ({warm.legal_calls} vs {cold.legal_calls})"
    )
    assert warm.apply_calls < cold.apply_calls


def test_warm_and_cold_agree_when_action_forced():
    """With one legal action, a resumed search returns exactly what a cold one does."""
    first = LinearProblem()
    a1 = run_mcts_reuse(first, 0, BUDGET, rng=random.Random(3))
    assert a1 == "advance"

    warm = LinearProblem()
    a_warm = run_mcts_reuse(warm, 1, BUDGET, rng=random.Random(3))
    assert a_warm == "advance"
    assert tree_cache.cache_info()["hits"] == 1

    tree_cache.clear()
    cold = LinearProblem()
    a_cold = run_mcts_reuse(cold, 1, BUDGET, rng=random.Random(3))
    assert a_cold == a_warm


def test_repeated_identical_state_unchanged():
    """
    Only the chosen child is stashed, so re-searching the *same* root state is
    still a cold miss and must return exactly what the first cold call returned.
    """
    p1 = PickProblem()
    a1 = run_mcts_reuse(p1, frozenset(), BUDGET, rng=random.Random(5))
    hits_after_first = tree_cache.cache_info()["hits"]

    p2 = PickProblem()
    a2 = run_mcts_reuse(p2, frozenset(), BUDGET, rng=random.Random(5))

    assert a2 == a1
    info = tree_cache.cache_info()
    assert info["hits"] == hits_after_first, "the root state itself is never stashed"
    assert info["misses"] == 2


def test_invalidation_on_state_change():
    """A cached subtree is only ever served for its own key."""
    problem = PickProblem()
    a1 = run_mcts_reuse(problem, frozenset(), BUDGET, rng=random.Random(9))
    successor = problem.apply(frozenset(), a1)
    assert tree_cache.cache_info()["entries"] >= 1

    other = frozenset({"zzz"})
    stats = SearchStats()
    run_mcts_reuse(PickProblem(), other, BUDGET, rng=random.Random(9), stats=stats)
    assert stats.reused_root is False
    assert tree_cache.cache_info()["hits"] == 0


def test_seeded_determinism_with_cleared_cache():
    """Clearing between runs restores exact cold-start determinism."""

    def run_once() -> tuple:
        tree_cache.clear()
        stats = SearchStats()
        action = run_mcts_reuse(
            PickProblem(), frozenset(), BUDGET, rng=random.Random(11), stats=stats
        )
        return (
            action,
            stats.iterations_run,
            stats.expansions,
            stats.leaf_evaluations,
            stats.tree_nodes,
            stats.reused_root,
            stats.reused_nodes,
        )

    r1 = run_once()
    r2 = run_once()
    assert r1 == r2
    assert r1[0] is not None
    assert r1[5] is False


def test_lookup_returns_isolated_clone():
    """The cached entry is never mutated by searches that consume it."""
    problem = PickProblem()
    a1 = run_mcts_reuse(problem, frozenset(), BUDGET, rng=random.Random(2))
    successor = problem.apply(frozenset(), a1)

    stats1 = SearchStats()
    run_mcts_reuse(
        PickProblem(), successor, BUDGET, rng=random.Random(2), stats=stats1
    )
    assert stats1.reused_root is True

    first = tree_cache.lookup(successor)
    assert first is not None
    before = (first.visit_count, first.total_value, len(first.children))

    stats2 = SearchStats()
    run_mcts_reuse(
        PickProblem(), successor, BUDGET, rng=random.Random(9), stats=stats2
    )
    assert stats2.reused_root is True

    second = tree_cache.lookup(successor)
    assert second is not None
    assert second is not first, "each lookup must hand out a private clone"
    assert (second.visit_count, second.total_value, len(second.children)) == before


def test_plain_run_mcts_never_touches_cache():
    """run_mcts (the pre-3.4 entry) must leave the cache completely alone."""
    problem = PickProblem()
    a1 = run_mcts(problem, frozenset(), BUDGET, rng=random.Random(4))
    assert a1 is not None
    run_mcts(problem, problem.apply(frozenset(), a1), BUDGET, rng=random.Random(4))
    info = tree_cache.cache_info()
    assert info["entries"] == 0 and info["hits"] == 0 and info["misses"] == 0


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def test_lru_eviction_bounded():
    tree_cache.clear()
    cap = tree_cache.cache_info()["max_entries"]
    assert cap >= 1

    for i in range(cap + 3):
        tree_cache.stash(f"k{i}", Node(state=f"s{i}"))

    info = tree_cache.cache_info()
    assert info["entries"] == cap, "LRU must hold at most max_entries subtrees"
    assert tree_cache.lookup("k0") is None, "oldest entries are evicted"
    assert tree_cache.lookup(f"k{cap + 2}") is not None, "newest entry is retained"


def test_cache_disabled_via_env(monkeypatch):
    monkeypatch.setenv(tree_cache.CACHE_ENTRIES_ENV, "0")
    tree_cache.clear()

    problem = PickProblem()
    a1 = run_mcts_reuse(problem, frozenset(), BUDGET, rng=random.Random(4))
    successor = problem.apply(frozenset(), a1)

    stats = SearchStats()
    run_mcts_reuse(
        PickProblem(), successor, BUDGET, rng=random.Random(4), stats=stats
    )
    info = tree_cache.cache_info()
    assert info["max_entries"] == 0
    assert info["entries"] == 0
    assert stats.reused_root is False


def test_clear_resets_counters():
    problem = PickProblem()
    run_mcts_reuse(problem, frozenset(), BUDGET, rng=random.Random(6))
    info = tree_cache.cache_info()
    assert info["misses"] == 1
    assert info["entries"] == 1

    tree_cache.clear()
    info = tree_cache.cache_info()
    assert info == {
        "entries": 0,
        "hits": 0,
        "misses": 0,
        "max_entries": info["max_entries"],
    }


def test_reuse_flag_off_is_fully_cold():
    """reuse=False must neither read nor write the cache."""
    a_warm = run_mcts_reuse(PickProblem(), frozenset(), BUDGET, rng=random.Random(7))
    warm_info = tree_cache.cache_info()

    a_cold = run_mcts_reuse(
        PickProblem(), frozenset(), BUDGET, rng=random.Random(7), reuse=False
    )
    cold_info = tree_cache.cache_info()

    assert a_cold == a_warm, "a cold run must match what reuse returned cold"
    assert cold_info == warm_info, "reuse=False must not add entries or counters"


# ---------------------------------------------------------------------------
# Entry-point wiring
# ---------------------------------------------------------------------------


_PHASE_CASES = [
    pytest.param("purchase", "backend.ai.mcts.purchase", "decide_purchase_mcts", False, id="purchase"),
    pytest.param("combat", "backend.ai.mcts.combat", "decide_combat_mcts", True, id="combat"),
    pytest.param("combat_move", "backend.ai.mcts.combat_move", "decide_combat_move_mcts", True, id="combat_move"),
    pytest.param("non_combat_move", "backend.ai.mcts.non_combat_move", "decide_non_combat_move_mcts", True, id="non_combat_move"),
    pytest.param("mobilization", "backend.ai.mcts.mobilization", "decide_mobilization_mcts", True, id="mobilization"),
]


def _load_wotr_setup():
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
    )
    return state, ud, td, fd, cd, port_d


@pytest.mark.parametrize("phase,module_name,entry_name,accepts_rng", _PHASE_CASES)
def test_entry_point_runs_through_tree_reuse(
    monkeypatch, phase, module_name, entry_name, accepts_rng
):
    """Every decide_*_mcts entry point must search through run_mcts_reuse."""
    import backend.ai.mcts.search as search_mod

    sentinel = Action(type="end_phase", faction="gondor", payload={})
    calls = {"n": 0}

    def fake_reuse(problem, state, budget, **kwargs):
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(search_mod, "run_mcts_reuse", fake_reuse)

    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.current_faction = "gondor"
    state.phase = phase
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={},
    )
    entry = getattr(importlib.import_module(module_name), entry_name)
    result = entry(ctx, rng=random.Random(0)) if accepts_rng else entry(ctx)

    assert calls["n"] == 1, f"{entry_name} must search via run_mcts_reuse"
    assert result is sentinel


# ---------------------------------------------------------------------------
# End-to-end purchase flow against the real reducer
# ---------------------------------------------------------------------------


def _purchase_ctx(state, ud, td, fd, cd, port_d, faction_id: str) -> AIContext:
    from backend.engine.queries import get_mobilization_capacity, get_purchasable_units

    state.phase = "purchase"
    state.current_faction = faction_id
    purchasable = get_purchasable_units(state, faction_id, ud, fd)
    capacity = get_mobilization_capacity(state, faction_id, td, cd, port_d, ud, fd)
    territories = capacity.get("territories", [])
    land_cap = sum(t.get("power", 0) for t in territories) + sum(
        1 for t in territories if t.get("home_unit_capacity")
    )
    sea_cap = sum(z.get("power", 0) for z in capacity.get("sea_zones", []))
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={
            "phase": "purchase",
            "faction": faction_id,
            "purchasable_units": purchasable,
            "mobilization_capacity": land_cap + sea_cap,
            "mobilization_land_capacity": land_cap,
            "purchased_units_count": 0,
        },
    )


def test_purchase_reuse_on_applied_successor_state():
    """
    Real-phase flow: search at S, apply the chosen purchase exactly like the API
    does, then search S' — that call must resume from the stashed subtree and
    still emit a validated action.
    """
    from backend.ai.mcts.purchase import PurchaseProblem

    budget = get_budget("easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 20
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")

    def unwrap(a):
        return a.unwrap() if isinstance(a, HashableAction) else a

    stats1 = SearchStats()
    a1 = run_mcts_reuse(PurchaseProblem(ctx), state, budget, stats=stats1)
    assert a1 is not None
    assert stats1.reused_root is False
    raw1 = unwrap(a1)
    v1 = validate_action(state, raw1, ud, td, fd, cd, port_d)
    assert v1.valid, v1.error
    assert raw1.type == "purchase_units", "power>0 must lead to a spend, not end_phase"

    new_state, _events = apply_action(state, raw1, ud, td, fd, cd, port_d)
    ctx2 = _purchase_ctx(new_state, ud, td, fd, cd, port_d, "gondor")
    stats2 = SearchStats()
    a2 = run_mcts_reuse(
        PurchaseProblem(ctx2), new_state, budget, stats=stats2
    )

    assert tree_cache.cache_info()["hits"] >= 1
    assert stats2.reused_root is True, "the successor state must resume from cache"
    assert stats2.reused_nodes >= 1
    assert a2 is not None
    v2 = validate_action(new_state, unwrap(a2), ud, td, fd, cd, port_d)
    assert v2.valid, v2.error
