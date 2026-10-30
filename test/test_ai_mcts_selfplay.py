"""
Self-play smoke harness: drive decide() through validate_action + apply_action
in-process (no API/DB). Building block 3.2b: full-turn driver across all five
phases via the real decide() dispatcher with AI_ALGORITHM=mcts, plus the lazy
ctx.strategic build check. (3.2c will add two-faction matchups and win rates.)
"""

import json
import pathlib
import random
import time

import pytest

from backend.engine.actions import end_phase
from backend.engine.queries import (
    get_contested_territories,
    get_mobilization_capacity,
    get_mobilization_river_zones,
    get_mobilization_sea_zones,
    get_mobilization_territories,
    get_purchasable_units,
    get_purchased_units,
    validate_action,
)
from backend.engine.reducer import apply_action
from backend.engine.subfaction_rules import unit_destination_spec

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.dice import sample_dice_for_continue, sample_dice_for_initiate

SETUP_ID = "motw_1.0"

# Generous per-action wall cap (seconds) — catches pathological blowups, not slow CI.
PER_ACTION_CAP_S = 25.0
# Hard step cap per phase (anti-stuck safety net for the test itself).
MAX_STEPS_PER_PHASE = 30


def _load_setup(setup_id: str = SETUP_ID):
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id=setup_id)
    setup = load_starting_setup(setup_id=setup_id)
    manifest = json.loads(
        (pathlib.Path("backend/data/setups") / setup_id / "manifest.json").read_text()
    )
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria=manifest.get("victory_criteria"),
        camp_cost=manifest.get("camp_cost"),
        stronghold_repair_cost=manifest.get("stronghold_repair_cost"),
    )
    return state, ud, td, fd, cd, port_d


def _available_actions(state, faction_id, phase, ud, td, fd, cd, port_d) -> dict:
    """Compact available-actions dict for the in-process driver.

    The MCTS Problems compute their own legality from state; the keys here cover
    what decide() itself reads (combat_territories, can_end_phase) plus enough
    for heuristic fallbacks to not crash if MCTS returns nothing. can_end_phase
    uses the reducer's own end-phase guard as ground truth.
    """
    actions: dict = {
        "faction": faction_id,
        "phase": phase,
        "can_end_phase": validate_action(
            state, end_phase(faction_id), ud, td, fd, cd, port_d
        ).valid,
    }
    if phase == "purchase":
        purchasable = get_purchasable_units(state, faction_id, ud, fd)
        capacity_info = get_mobilization_capacity(state, faction_id, td, cd, port_d, ud, fd)
        territories_list = capacity_info.get("territories", [])
        land_cap = sum(t.get("power", 0) for t in territories_list) + sum(
            1 for t in territories_list if t.get("home_unit_capacity")
        )
        sea_cap = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))
        actions["purchasable_units"] = purchasable
        actions["mobilization_capacity"] = land_cap + sea_cap
        actions["mobilization_land_capacity"] = land_cap
        actions["purchased_units_count"] = sum(
            s.count for s in (state.faction_purchased_units or {}).get(faction_id, [])
        )
    elif phase == "combat":
        actions["combat_territories"] = get_contested_territories(
            state, faction_id, fd, ud, td
        )
    elif phase == "mobilization":
        purchased = get_purchased_units(state, faction_id)
        unit_destinations = {}
        for stack in purchased:
            spec = unit_destination_spec(
                state, faction_id, ud.get(stack.get("unit_id")), fd, td, cd, port_d
            )
            if spec:
                unit_destinations[stack["unit_id"]] = spec
        actions["mobilize_options"] = {
            "territories": get_mobilization_territories(state, faction_id, td, cd, port_d, ud, fd),
            "sea_zones": get_mobilization_sea_zones(state, faction_id, td, port_d),
            "river_zones": get_mobilization_river_zones(state, faction_id, td),
            "capacity": get_mobilization_capacity(state, faction_id, td, cd, port_d, ud, fd),
            "pending_units": purchased,
        }
        if unit_destinations:
            actions["mobilize_options"]["unit_destinations"] = unit_destinations
    return actions


def _ctx_for(state, ud, td, fd, cd, port_d) -> AIContext:
    faction_id = state.current_faction or ""
    phase = state.phase or "purchase"
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=_available_actions(state, faction_id, phase, ud, td, fd, cd, port_d),
    )


def _fill_combat_dice(state, action, ud, td, fd, rng) -> None:
    """Fill empty dice on combat actions, mirroring the API's server-side generation."""
    if action.type == "initiate_combat":
        pl = action.payload
        dr = pl.get("dice_rolls") or {}
        if not dr.get("attacker") and not dr.get("defender"):
            pl["dice_rolls"] = sample_dice_for_initiate(
                state,
                pl.get("territory_id"),
                pl.get("sea_zone_id"),
                action.faction,
                ud,
                td,
                fd,
                rng=rng,
            )
    elif action.type == "continue_combat" and state.active_combat:
        dr = action.payload.get("dice_rolls") or {}
        if not dr.get("attacker") and not dr.get("defender"):
            action.payload["dice_rolls"] = sample_dice_for_continue(
                state, ud, td, fd, rng=rng
            )


def run_phase_actions(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str,
    *,
    max_steps: int = MAX_STEPS_PER_PHASE,
    per_action_cap_s: float = PER_ACTION_CAP_S,
    rng: random.Random | None = None,
) -> tuple[list[str], object]:
    """Drive decide -> validate -> apply until the phase advances or the turn ends.

    Mirrors the /ai-step API loop in-process: fresh AIContext per action, dice
    filled for combat actions. Returns (applied action types, latest state).
    """
    rng = rng if rng is not None else random.Random(1234)
    start_phase = state.phase or "purchase"
    action_types: list[str] = []
    for _step in range(max_steps):
        if state.winner or state.current_faction != faction_id:
            break
        if (state.phase or "purchase") != start_phase:
            break
        ctx = _ctx_for(state, ud, td, fd, cd, port_d)
        t0 = time.perf_counter()
        action = decide(ctx)
        elapsed = time.perf_counter() - t0
        assert elapsed <= per_action_cap_s, (
            f"decide() in {start_phase} took {elapsed:.2f}s (cap {per_action_cap_s}s)"
        )
        if action is None:
            # Anti-stuck: same fallback shape as the API loop.
            action = end_phase(faction_id)
        _fill_combat_dice(state, action, ud, td, fd, rng)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"invalid action {action.type} {action.payload}: {v.error}"
        state, _events = apply_action(state, action, ud, td, fd, cd, port_d)
        action_types.append(action.type)
    else:
        raise AssertionError(
            f"phase {start_phase} hit step cap {max_steps} without advancing (anti-stuck broken?)"
        )
    return action_types, state


def run_faction_turn(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str,
    *,
    per_action_cap_s: float = PER_ACTION_CAP_S,
    rng: random.Random | None = None,
) -> tuple[list[str], object]:
    """Drive all five phases of one faction's turn. Returns (action types, latest state)."""
    rng = rng if rng is not None else random.Random(1234)
    all_types: list[str] = []
    for expected_phase in ("purchase", "combat_move", "combat", "non_combat_move", "mobilization"):
        if state.winner or state.current_faction != faction_id:
            break
        assert (state.phase or "purchase") == expected_phase, (
            f"expected phase {expected_phase}, got {state.phase}"
        )
        types, state = run_phase_actions(
            state, ud, td, fd, cd, port_d, faction_id,
            per_action_cap_s=per_action_cap_s, rng=rng,
        )
        all_types.extend(types)
    return all_types, state


# ---------------------------------------------------------------------------
# 3.2b: full-turn smoke via the real decide() dispatcher
# ---------------------------------------------------------------------------


def test_full_turn_smoke_mcts(monkeypatch):
    """One complete faction turn (all five phases) with AI_ALGORITHM=mcts.

    Asserts the structural smoke contract: every action passes validate_action,
    no per-action wall-time blowup, the loop terminates, and the turn hands off
    to the next faction (or the game ends).
    """
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    faction_id = state.current_faction or ""
    assert state.phase == "purchase"

    action_types, final_state = run_faction_turn(
        state, ud, td, fd, cd, port_d, faction_id
    )

    assert action_types, "expected at least one applied action"
    assert "end_phase" in action_types, "turn must advance via end_phase"
    assert final_state.current_faction != faction_id or final_state.winner is not None, (
        "faction turn must hand off (or game must end)"
    )


def test_decide_skips_strategic_build_when_mcts_succeeds(monkeypatch):
    """MCTS paths must not pay the ctx.strategic build; heuristic paths still do."""
    import importlib

    decide_mod = importlib.import_module("backend.ai.decide")

    calls = {"n": 0}
    original = decide_mod.build_strategic_turn_context

    def spy(ctx):
        calls["n"] += 1
        return original(ctx)

    # decide() imports the symbol by name, so patch the name it bound.
    monkeypatch.setattr(decide_mod, "build_strategic_turn_context", spy)

    # MCTS path: purchase decides via MCTS -> strategic never built.
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources.setdefault(fid, {})["power"] = 20
    ctx = _ctx_for(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert calls["n"] == 0, "MCTS path built ctx.strategic unnecessarily"
    assert ctx.strategic is None

    # Heuristic path: strategic must be built before the purchase policy runs.
    monkeypatch.setenv("AI_ALGORITHM", "heuristic")
    state2, ud2, td2, fd2, cd2, port2 = _load_setup()
    fid2 = state2.current_faction or ""
    state2.faction_resources.setdefault(fid2, {})["power"] = 15
    ctx2 = _ctx_for(state2, ud2, td2, fd2, cd2, port2)
    action2 = decide(ctx2)
    assert action2 is not None
    assert calls["n"] == 1
    assert ctx2.strategic is not None


# ---------------------------------------------------------------------------
# Recovered 3.2a coverage: purchase-phase smoke + determinism + anti-stuck
# ---------------------------------------------------------------------------


def test_purchase_phase_smoke_buys_then_ends(monkeypatch):
    """MCTS purchase phase: buys (power permitting) then ends the phase cleanly."""
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources.setdefault(fid, {})["power"] = 25
    action_types, final_state = run_phase_actions(
        state, ud, td, fd, cd, port_d, fid
    )
    assert action_types[-1] == "end_phase"
    assert "purchase_units" in action_types, "expected MCTS to spend available power"
    assert (final_state.phase or "") != "purchase" or final_state.current_faction != fid


def test_purchase_phase_zero_power_anti_stuck(monkeypatch):
    """Zero power: purchase must terminate with a single end_phase (no soft-lock)."""
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources[fid] = {"power": 0}
    action_types, final_state = run_phase_actions(
        state, ud, td, fd, cd, port_d, fid
    )
    assert action_types == ["end_phase"]
    assert (final_state.phase or "") != "purchase" or final_state.current_faction != fid


def test_purchase_mcts_seed_determinism(monkeypatch):
    """Same seeded PurchaseProblem search -> same action-type sequence."""
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.purchase import PurchaseProblem
    from backend.ai.mcts.search import run_mcts

    def purchase_seq(seed: int) -> list[str]:
        random.seed(seed)  # PurchaseProblem takes no rng; seed module-level entropy.
        state, ud, td, fd, cd, port_d = _load_setup()
        fid = state.current_faction or ""
        state.faction_resources.setdefault(fid, {})["power"] = 25
        ctx = _ctx_for(state, ud, td, fd, cd, port_d)
        problem = PurchaseProblem(ctx)
        budget = get_budget("easy")
        seq: list[str] = []
        s = ctx.state
        for _ in range(8):
            a = run_mcts(problem, s, budget)
            if a is None:
                break
            raw = a.unwrap() if isinstance(a, HashableAction) else a
            seq.append(raw.type)
            if raw.type == "end_phase":
                break
            s = problem.apply(s, a)
        return seq

    seq1 = purchase_seq(7)
    seq2 = purchase_seq(7)
    assert seq1, "expected a non-empty purchase sequence"
    assert seq1 == seq2
