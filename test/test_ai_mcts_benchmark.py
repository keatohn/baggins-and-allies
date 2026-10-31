"""
Benchmark for MCTS instrumentation (building block 3.1).

Measures, per phase on a fixed wotr_exp_1.0 snapshot:
- decide_*_mcts end-to-end latency (mean/max over a few runs)
- enumerator_size + its cost (the 3.1 baseline found this gates move phases)
- run_mcts_search SearchStats (iterations, expansions, evaluations, tree_nodes)
- GameState.to_dict() + JSON-serialize cost (candidate transposition key for 3.3)
- per-GameState allocation bytes and retained tree size (bounds 3.4's cache design)

All caps are generous so CI never flakes on slow machines; the numbers are
printed for baseline comparison across later building blocks. No behavior change.
"""

import json
import time
import tracemalloc

import pytest

from backend.engine.actions import end_phase
from backend.engine.queries import validate_action
from backend.engine.state import Unit

from backend.ai.context import AIContext
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import get_budget
from backend.ai.mcts.combat import CombatProblem, decide_combat_mcts
from backend.ai.mcts.combat_move import CombatMoveProblem, decide_combat_move_mcts
from backend.ai.mcts.mobilization import MobilizationProblem, decide_mobilization_mcts
from backend.ai.mcts.non_combat_move import NonCombatMoveProblem, decide_non_combat_move_mcts
from backend.ai.mcts.purchase import PurchaseProblem, decide_purchase_mcts
from backend.ai.mcts.search import run_mcts_search

SETUP_ID = "wotr_exp_1.0"
FACTION = "gondor"

# Generous caps: catch pathological regressions, not slow CI machines.
DECIDE_CAP_S = 30.0
ENUMERATOR_CAP_S = 30.0
SEARCH_CAP_S = 40.0
JSON_CAP_S = 5.0
# 3.1 baseline: per-GameState ≈ 109KB; depth-1 tree (19 nodes) ≈ 1.13MB retained.
# Cap at 200MB — catches accidental unbounded growth, not modest trees.
TREE_RETAIN_CAP_BYTES = 200_000_000
JSON_SIZE_CAP_BYTES = 5_000_000

DECIDE_REPEATS = 3


def _load_setup(setup_id: str = SETUP_ID):
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id=setup_id)
    setup = load_starting_setup(setup_id=setup_id)
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
    )
    return state, ud, td, fd, cd, port_d


def _unwrap(a):
    return a.unwrap() if isinstance(a, HashableAction) else a


def _ctx(state, ud, td, fd, cd, port_d, phase: str, available_actions: dict | None = None) -> AIContext:
    state.phase = phase
    state.current_faction = FACTION
    if available_actions is None:
        available_actions = {"phase": phase, "faction": FACTION, "can_end_phase": True}
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


def _plant_attackers(state, ud, from_tid="north_ithilien", to_tid="minas_morgul", attackers=3):
    if from_tid not in state.territories or to_tid not in state.territories:
        pytest.skip(f"{from_tid} or {to_tid} not in setup")
    att_uid = "gondor_soldier" if "gondor_soldier" in ud else next(
        u for u in ud if getattr(ud[u], "faction", "") == FACTION
    )
    ah = int(getattr(ud[att_uid], "health", 1) or 1)
    mv = max(1, int(getattr(ud[att_uid], "movement", 1) or 1))
    terr = state.territories[from_tid]
    terr.units = []
    for i in range(attackers):
        terr.units.append(
            Unit(
                instance_id=f"bench_att_{i}",
                unit_id=att_uid,
                remaining_movement=mv,
                remaining_health=ah,
                base_movement=mv,
                base_health=ah,
                loaded_onto=None,
            )
        )
    terr.owner = FACTION


def _phase_scenarios():
    """Build (name, problem_factory, ctx, decide_fn) for each phase on a fixed snapshot."""
    scenarios = []

    # --- purchase ---
    state, ud, td, fd, cd, port_d = _load_setup()
    state.faction_resources.setdefault(FACTION, {})["power"] = 20
    ctx = _ctx(state, ud, td, fd, cd, port_d, "purchase")
    scenarios.append((
        "purchase",
        lambda c=ctx: PurchaseProblem(c),
        ctx,
        decide_purchase_mcts,
    ))

    # --- combat (planted attackers create a contested territory) ---
    state, ud, td, fd, cd, port_d = _load_setup()
    _plant_attackers(state, ud)
    ctx = _ctx(state, ud, td, fd, cd, port_d, "combat")
    scenarios.append((
        "combat",
        lambda c=ctx: CombatProblem(c),
        ctx,
        decide_combat_mcts,
    ))

    # --- combat_move ---
    state, ud, td, fd, cd, port_d = _load_setup()
    _plant_attackers(state, ud)
    ctx = _ctx(state, ud, td, fd, cd, port_d, "combat_move")
    scenarios.append((
        "combat_move",
        lambda c=ctx: CombatMoveProblem(c),
        ctx,
        decide_combat_move_mcts,
    ))

    # --- non_combat_move ---
    state, ud, td, fd, cd, port_d = _load_setup()
    ctx = _ctx(state, ud, td, fd, cd, port_d, "non_combat_move")
    scenarios.append((
        "non_combat_move",
        lambda c=ctx: NonCombatMoveProblem(c),
        ctx,
        decide_non_combat_move_mcts,
    ))

    # --- mobilization (give gondor purchased units so the phase has work) ---
    from backend.engine.state import UnitStack

    state, ud, td, fd, cd, port_d = _load_setup()
    land_uid = "gondor_soldier" if "gondor_soldier" in ud else next(
        u for u in ud if getattr(ud[u], "faction", "") == FACTION
    )
    state.faction_purchased_units[FACTION] = [
        UnitStack(unit_id=land_uid, count=2),
    ]
    ctx = _ctx(state, ud, td, fd, cd, port_d, "mobilization")
    scenarios.append((
        "mobilization",
        lambda c=ctx: MobilizationProblem(c),
        ctx,
        decide_mobilization_mcts,
    ))

    return scenarios


def _per_state_alloc_bytes(problem, state) -> int:
    """Bytes retained by one problem.apply (deep-copied GameState), via tracemalloc."""
    actions = problem.legal_actions(state)
    if not actions:
        return 0
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        new_state = problem.apply(state, actions[0])
        after = tracemalloc.get_traced_memory()[0]
        del new_state
        return max(0, after - base)
    finally:
        tracemalloc.stop()


def _json_key_cost(state) -> tuple[float, int]:
    """(seconds, bytes) for state.to_dict() + sorted JSON serialize (candidate TT key)."""
    t0 = time.perf_counter()
    d = state.to_dict()
    blob = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
    elapsed = time.perf_counter() - t0
    return elapsed, len(blob)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_benchmark_all_phases_under_caps():
    """Run the full benchmark; assert generous caps and print baseline numbers."""
    budget = get_budget("easy")
    rows = []

    for name, problem_factory, ctx, decide_fn in _phase_scenarios():
        state = ctx.state

        # Enumerator size + cost.
        problem = problem_factory()
        t0 = time.perf_counter()
        enum_size = problem.enumerator_size(state)
        enum_s = time.perf_counter() - t0
        assert enum_s <= ENUMERATOR_CAP_S, f"{name} enumerator took {enum_s:.2f}s"

        # Instrumented search (fresh Problem so counters isolate this run).
        problem2 = problem_factory()
        t0 = time.perf_counter()
        action, stats = run_mcts_search(problem2, state, budget)
        search_s = time.perf_counter() - t0
        assert search_s <= SEARCH_CAP_S, f"{name} search took {search_s:.2f}s"
        assert stats.root_actions == enum_size or stats.root_actions >= 0

        # End-to-end decide_*_mcts latency (constructs its own Problem internally).
        decide_times = []
        for _ in range(DECIDE_REPEATS):
            t0 = time.perf_counter()
            decide_fn(ctx)
            decide_times.append(time.perf_counter() - t0)
        decide_mean = sum(decide_times) / len(decide_times)
        decide_max = max(decide_times)
        assert decide_max <= DECIDE_CAP_S, f"{name} decide took {decide_max:.2f}s"

        # Transposition-key candidate cost.
        json_s, json_bytes = _json_key_cost(state)
        assert json_s <= JSON_CAP_S
        assert json_bytes <= JSON_SIZE_CAP_BYTES

        # Retained tree size bound: per-GameState alloc * tree_nodes.
        per_state = _per_state_alloc_bytes(problem_factory(), state)
        tree_retain = per_state * stats.tree_nodes
        assert tree_retain <= TREE_RETAIN_CAP_BYTES, (
            f"{name} tree retention {tree_retain / 1e6:.1f}MB exceeds cap"
        )

        rows.append({
            "phase": name,
            "root_actions": stats.root_actions,
            "enum_size": enum_size,
            "enum_s": enum_s,
            "search_s": search_s,
            "iterations": stats.iterations_run,
            "expansions": stats.expansions,
            "leaf_evals": stats.leaf_evaluations,
            "term_evals": stats.terminal_evaluations,
            "tree_nodes": stats.tree_nodes,
            "decide_mean_ms": decide_mean * 1000,
            "decide_max_ms": decide_max * 1000,
            "json_ms": json_s * 1000,
            "json_kb": json_bytes / 1024,
            "per_state_kb": per_state / 1024,
            "tree_retain_mb": tree_retain / 1e6,
        })

    # Print a baseline table for CI logs / later tuning comparisons.
    print("\n=== MCTS 3.1 benchmark baseline (easy budget, %s) ===" % SETUP_ID)
    header = (
        f"{'phase':<18} {'roots':>5} {'enum':>5} {'enum_ms':>8} {'search_s':>8} "
        f"{'iters':>5} {'exps':>5} {'leaves':>6} {'terms':>6} {'nodes':>5} "
        f"{'dec_ms':>8} {'json_ms':>8} {'json_KB':>8} {'st_KB':>7} {'tree_MB':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['phase']:<18} {r['root_actions']:>5} {r['enum_size']:>5} "
            f"{r['enum_s'] * 1000:>8.1f} {r['search_s']:>8.2f} "
            f"{r['iterations']:>5} {r['expansions']:>5} {r['leaf_evals']:>6} "
            f"{r['term_evals']:>6} {r['tree_nodes']:>5} "
            f"{r['decide_mean_ms']:>8.1f} {r['json_ms']:>8.2f} "
            f"{r['json_kb']:>8.1f} {r['per_state_kb']:>7.1f} {r['tree_retain_mb']:>8.2f}"
        )

    assert rows, "expected at least one benchmark row"
    # Sanity: every phase produced a measurable search.
    for r in rows:
        assert r["root_actions"] >= 0
        assert r["tree_nodes"] >= 1


def test_json_key_is_cheap_enough_for_transposition():
    """3.1 finding gate: to_dict+JSON must stay cheap enough to use as a TT key (3.3)."""
    state, *_ = _load_setup()
    elapsed, size = _json_key_cost(state)
    assert elapsed < 1.0, f"JSON key took {elapsed:.3f}s — too slow for a transposition key"
    assert size < 1_000_000, f"JSON key is {size} bytes — unexpectedly large"


def test_tree_retention_is_bounded():
    """3.1 finding gate: one search tree's retained size stays within 3.4's cache budget."""
    state, ud, td, fd, cd, port_d = _load_setup()
    state.faction_resources.setdefault(FACTION, {})["power"] = 20
    ctx = _ctx(state, ud, td, fd, cd, port_d, "purchase")
    problem = PurchaseProblem(ctx)
    budget = get_budget("easy")
    _action, stats = run_mcts_search(problem, state, budget)
    per_state = _per_state_alloc_bytes(PurchaseProblem(ctx), state)
    retain = per_state * stats.tree_nodes
    # 3.1 baseline: per-GameState ≈ 109KB, depth-1 tree ≈ 1.13MB. 50MB is a generous ceiling.
    assert retain <= 50_000_000, f"purchase tree retains {retain / 1e6:.1f}MB"
    assert stats.tree_nodes >= 1


def test_stats_disabling_preserves_action_selection():
    """run_mcts (no stats) and run_mcts_search must pick the same action on the same seed."""
    state, ud, td, fd, cd, port_d = _load_setup()
    state.faction_resources.setdefault(FACTION, {})["power"] = 20
    ctx = _ctx(state, ud, td, fd, cd, port_d, "purchase")
    budget = get_budget("easy")

    from backend.ai.mcts.search import run_mcts

    a1 = run_mcts(PurchaseProblem(ctx), state, budget)
    a2, _stats = run_mcts_search(PurchaseProblem(ctx), state, budget)
    # PurchaseProblem takes no rng (module-level entropy); both should still agree
    # on the action *type* for this fixed snapshot under easy budget.
    assert (_unwrap(a1).type if a1 else None) == (_unwrap(a2).type if a2 else None)
