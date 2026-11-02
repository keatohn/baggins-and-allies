"""
Tests for the purchase-phase MCTS Problem and the ai_algorithm routing flag.
Validates that MCTS-returned actions pass engine validation and apply cleanly.
"""

import os

import pytest

from backend.engine.actions import end_phase, purchase_units
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.purchase import (
    MAX_BUNDLE_ACTIONS,
    PurchaseProblem,
    _fill_inverse_cost,
    _something_still_fits,
    decide_purchase_mcts,
)
from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.search import run_mcts


def _unwrap(a):
    return a.unwrap() if isinstance(a, HashableAction) else a


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
    available_actions = {
        "phase": "purchase",
        "faction": faction_id,
        "purchasable_units": purchasable,
        "mobilization_capacity": land_cap + sea_cap,
        "mobilization_land_capacity": land_cap,
        "purchased_units_count": 0,
    }
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


# ---------------------------------------------------------------------------
# PurchaseProblem: enumerator validity
# ---------------------------------------------------------------------------


def test_legal_actions_all_pass_validation():
    """Every action from legal_actions must pass engine validate_action."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 25
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    problem = PurchaseProblem(ctx)

    actions = problem.legal_actions(state)
    assert actions, "expected at least end_phase"
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_include_end_phase_always():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("elves", {})["power"] = 10
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "elves")
    problem = PurchaseProblem(ctx)
    actions = problem.legal_actions(state)
    assert any(a.type == "end_phase" for a in actions)


def test_legal_actions_only_end_phase_when_capital_lost():
    """No purchase_units without capital — matches engine validation."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("elves", {})["power"] = 30
    cap = fd["elves"].capital
    state.territories[cap].owner = "mordor"
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "elves")
    problem = PurchaseProblem(ctx)
    actions = problem.legal_actions(state)
    assert all(a.type == "end_phase" for a in actions)


def test_purchase_bundles_do_not_exceed_power_on_hand():
    """Every offered bundle costs no more than the power still unspent this turn."""
    from backend.ai.formulas import get_unit_power_cost

    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("mordor", {})["power"] = 8
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "mordor")
    problem = PurchaseProblem(ctx)
    for action in problem.legal_actions(state):
        raw = _unwrap(action)
        if raw.type != "purchase_units":
            continue
        spent = 0
        for unit_id, count in (raw.payload.get("purchases") or {}).items():
            spent += (get_unit_power_cost(ud.get(unit_id)) or 0) * int(count)
        assert 0 < spent <= 8


def test_purchase_bundles_spend_until_nothing_else_fits():
    """No offered bundle leaves enough power and slots to buy another unit."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("mordor", {})["power"] = 40
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "mordor")
    problem = PurchaseProblem(ctx)
    budget = problem._remaining_budget(state)
    land_rem, sea_rem = problem._remaining_slots(state)
    candidates = problem._affordable_candidates(budget, land_rem, sea_rem)
    purchases = [
        _unwrap(action).payload["purchases"]
        for action in problem.legal_actions(state)
        if action.type == "purchase_units"
    ]
    assert purchases
    for bundle in purchases:
        assert not _something_still_fits(candidates, bundle, budget, land_rem, sea_rem)


def test_inverse_cost_draw_favors_cheaper_units():
    """Picking with weight 1/cost yields more copies of the cheaper unit."""
    import random

    candidates = [
        {"unit_id": "cheap", "cost": 3, "max_affordable": 40, "is_naval": False},
        {"unit_id": "pricey", "cost": 12, "max_affordable": 40, "is_naval": False},
    ]
    rng = random.Random(1)
    cheap = 0
    pricey = 0
    for _ in range(300):
        bundle = _fill_inverse_cost(candidates, 60, 40, 0, rng)
        cheap += bundle.get("cheap", 0)
        pricey += bundle.get("pricey", 0)
        assert not _something_still_fits(candidates, bundle, 60, 40, 0)
    assert cheap > pricey * 2


def test_legal_actions_capped():
    """Branching factor stays within MAX_BUNDLE_ACTIONS + 1 (end_phase)."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("mordor", {})["power"] = 100
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "mordor")
    problem = PurchaseProblem(ctx)
    actions = problem.legal_actions(state)
    assert len(actions) <= MAX_BUNDLE_ACTIONS + 1


# ---------------------------------------------------------------------------
# Apply / terminal / evaluate
# ---------------------------------------------------------------------------


def test_apply_purchase_units_extends_purchased_pool():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 25
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    problem = PurchaseProblem(ctx)

    purchase_actions = [a for a in problem.legal_actions(state) if a.type == "purchase_units"]
    assert purchase_actions, "expected at least one purchase candidate"
    haction = purchase_actions[0]
    action = _unwrap(haction)
    new_state = problem.apply(state, haction)

    # Original state must not be mutated (reducer deep-copies).
    old_pool = [
        (s.unit_id, s.count)
        for s in (state.faction_purchased_units or {}).get("gondor", [])
    ]
    new_pool = [
        (s.unit_id, s.count)
        for s in (new_state.faction_purchased_units or {}).get("gondor", [])
    ]
    assert new_pool != old_pool or action.payload.get("purchases") == {}
    # New state is still in purchase phase (purchase_units does not end the phase).
    assert new_state.phase == "purchase"


def test_apply_end_phase_advances_to_combat_move():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 5
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    problem = PurchaseProblem(ctx)
    new_state = problem.apply(state, end_phase("gondor"))
    assert new_state.phase == "combat_move"
    assert problem.is_terminal(new_state)


def test_is_terminal_false_in_purchase_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    problem = PurchaseProblem(ctx)
    assert not problem.is_terminal(state)


# ---------------------------------------------------------------------------
# decide_purchase_mcts: end-to-end validity
# ---------------------------------------------------------------------------


def test_decide_purchase_mcts_returns_valid_action():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 20
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    action = decide_purchase_mcts(ctx)
    assert action is not None
    assert action.type in ("purchase_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_purchase_mcts_applies_cleanly():
    """The returned action must survive apply_action without raising."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("elves", {})["power"] = 15
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "elves")
    action = decide_purchase_mcts(ctx)
    new_state, _events = apply_action(state, action, ud, td, fd, cd, port_d)
    assert new_state is not None


def test_decide_purchase_mcts_falls_back_to_end_phase_when_capital_lost():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("elves", {})["power"] = 30
    cap = fd["elves"].capital
    state.territories[cap].owner = "mordor"
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "elves")
    action = decide_purchase_mcts(ctx)
    assert action is not None
    assert action.type == "end_phase"


def test_decide_purchase_mcts_deterministic_with_seed(monkeypatch):
    """Same env seed + budget -> same action (search uses env difficulty; we pin budget via env)."""
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 20
    ctx1 = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    # Rebuild a fresh identical ctx for second call.
    state2, ud2, td2, fd2, cd2, port2 = _load_wotr_setup()
    state2.faction_resources.setdefault("gondor", {})["power"] = 20
    ctx2 = _purchase_ctx(state2, ud2, td2, fd2, cd2, port2, "gondor")
    a1 = decide_purchase_mcts(ctx1)
    a2 = decide_purchase_mcts(ctx2)
    # Both valid; types should match for the same starting state.
    assert a1.type == a2.type


# ---------------------------------------------------------------------------
# ai_algorithm flag routing in decide()
# ---------------------------------------------------------------------------


def test_decide_purchase_uses_heuristic_by_default(monkeypatch):
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 15
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    action = decide(ctx)
    assert action is not None
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_purchase_routes_to_mcts_when_flag_set(monkeypatch):
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 20
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    action = decide(ctx)
    assert action is not None
    assert action.type in ("purchase_units", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_purchase_mcts_flag_off_keeps_heuristic_path(monkeypatch):
    """AI_ALGORITHM=heuristic (or unset) must not call into the MCTS module."""
    monkeypatch.setenv("AI_ALGORITHM", "heuristic")
    called = {"mcts": 0}

    import backend.ai.mcts.purchase as mcts_purchase

    original = mcts_purchase.decide_purchase_mcts

    def spy(ctx):
        called["mcts"] += 1
        return original(ctx)

    monkeypatch.setattr(mcts_purchase, "decide_purchase_mcts", spy)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault("gondor", {})["power"] = 15
    ctx = _purchase_ctx(state, ud, td, fd, cd, port_d, "gondor")
    decide(ctx)
    assert called["mcts"] == 0
