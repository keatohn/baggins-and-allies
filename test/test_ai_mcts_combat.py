"""
Tests for the combat-phase MCTS Problem and the ai_algorithm routing flag.
Validates that MCTS-returned actions pass engine validation and apply cleanly.
"""

import random

import pytest

from backend.engine.actions import end_phase
from backend.engine.queries import validate_action
from backend.engine.reducer import apply_action
from backend.engine.state import ActiveCombat, Unit

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.budget import SearchBudget
from backend.ai.mcts.combat import (
    MAX_COMBAT_BRANCHES,
    CombatProblem,
    decide_combat_mcts,
)
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


def _combat_ctx(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str = "gondor",
    available_actions: dict | None = None,
) -> AIContext:
    state.phase = "combat"
    state.current_faction = faction_id
    if available_actions is None:
        available_actions = {"phase": "combat", "faction": faction_id}
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )


def _plant_contested_territory(state, ud, td, *, attackers: int = 2, defenders: int = 2, tid: str = "minas_morgul"):
    """Place gondor attackers on a mordor-owned land hex so initiate_combat is legal."""
    if tid not in state.territories:
        pytest.skip(f"{tid} not in setup")
    att_uid = "gondor_soldier" if "gondor_soldier" in ud else next(
        u for u in ud if getattr(ud[u], "faction", "") == "gondor"
    )
    def_uid = "morgul_orc" if "morgul_orc" in ud else next(
        u for u in ud if getattr(ud[u], "faction", "") == "mordor"
    )
    ah = int(getattr(ud[att_uid], "health", 1) or 1)
    dh = int(getattr(ud[def_uid], "health", 1) or 1)
    terr = state.territories[tid]
    units = []
    for i in range(attackers):
        units.append(
            Unit(
                instance_id=f"mcts_att_{i}",
                unit_id=att_uid,
                remaining_movement=0,
                remaining_health=ah,
                base_movement=1,
                base_health=ah,
                loaded_onto=None,
            )
        )
    for i in range(defenders):
        units.append(
            Unit(
                instance_id=f"mcts_def_{i}",
                unit_id=def_uid,
                remaining_movement=0,
                remaining_health=dh,
                base_movement=0,
                base_health=dh,
                loaded_onto=None,
            )
        )
    terr.units = units
    terr.owner = "mordor"
    return tid


def _plant_active_combat(state, ud, td, *, tid: str = "minas_morgul", round_number: int = 1, sea_zone_id=None):
    tid = _plant_contested_territory(state, ud, td, tid=tid)
    att_ids = [u.instance_id for u in state.territories[tid].units if u.instance_id.startswith("mcts_att_")]
    state.active_combat = ActiveCombat(
        attacker_faction="gondor",
        territory_id=tid,
        attacker_instance_ids=att_ids,
        round_number=round_number,
        sea_zone_id=sea_zone_id,
    )
    return tid


# ---------------------------------------------------------------------------
# CombatProblem: enumerator validity
# ---------------------------------------------------------------------------


def test_legal_actions_all_pass_validation_with_active_combat():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))

    actions = problem.legal_actions(state)
    assert actions, "expected continue and/or retreat"
    assert any(a.type == "continue_combat" for a in actions)
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_initiate_plus_end_phase_when_legal():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))

    actions = problem.legal_actions(state)
    types = {a.type for a in actions}
    assert "initiate_combat" in types
    # end_phase is illegal while contested remain, so it must be filtered out.
    assert "end_phase" not in types
    for haction in actions:
        action = _unwrap(haction)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"illegal action {action.type} {action.payload}: {v.error}"


def test_legal_actions_end_phase_when_no_battles():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    actions = problem.legal_actions(state)
    assert all(a.type == "end_phase" for a in actions)
    assert actions
    v = validate_action(state, _unwrap(actions[0]), ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_legal_actions_capped():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    # Add many retreat-ish branches is hard; at least assert the cap holds.
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    actions = problem.legal_actions(state)
    assert len(actions) <= MAX_COMBAT_BRANCHES


def test_legal_actions_empty_when_winner_set():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    state.winner = "good"
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    assert problem.legal_actions(state) == []


# ---------------------------------------------------------------------------
# Apply / terminal / evaluate
# ---------------------------------------------------------------------------


def test_apply_initiate_sets_active_combat_or_resolves():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    tid = _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))

    initiate_actions = [a for a in problem.legal_actions(state) if a.type == "initiate_combat"]
    assert initiate_actions
    new_state = problem.apply(state, initiate_actions[0])

    # Original state must not be mutated.
    assert state.active_combat is None
    assert new_state.phase == "combat"
    # Either combat is still running, or it resolved (attackers wiped / defenders wiped).
    if new_state.active_combat is None:
        # If resolved without active combat, either we took it or attackers died.
        assert new_state.territories[tid].owner in ("gondor", "mordor")
    else:
        assert new_state.active_combat.attacker_faction == "gondor"


def test_apply_retreat_clears_active_combat():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    # Plant active combat next to a friendly hex so retreat is available.
    friendly = None
    enemy_tid = None
    for t, terr_t in state.territories.items():
        tdef = td.get(t)
        if not tdef or getattr(tdef, "terrain_type", "") in ("sea", "river"):
            continue
        if terr_t.owner != "gondor":
            continue
        for adj in getattr(tdef, "adjacent", None) or []:
            at = state.territories.get(adj)
            if (
                at
                and at.owner == "mordor"
                and td.get(adj)
                and getattr(td[adj], "terrain_type", "") not in ("sea", "river")
            ):
                friendly, enemy_tid = t, adj
                break
        if friendly:
            break
    if not friendly:
        pytest.skip("no adjacent gondor/mordor land pair in setup")
    tid = _plant_active_combat(state, ud, td, tid=enemy_tid)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))

    retreat_actions = [a for a in problem.legal_actions(state) if a.type == "retreat"]
    assert retreat_actions, "expected at least one retreat destination"
    new_state = problem.apply(state, retreat_actions[0])
    assert new_state.active_combat is None
    assert new_state.phase == "combat"
    # Survivors moved into the retreat destination.
    dest = _unwrap(retreat_actions[0]).payload["retreat_to"]
    assert any(u.instance_id.startswith("mcts_att_") for u in new_state.territories[dest].units)


def test_apply_continue_progresses_combat():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td, round_number=1)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    cont = next(a for a in problem.legal_actions(state) if a.type == "continue_combat")
    new_state = problem.apply(state, cont)
    assert new_state.phase == "combat"
    # Either still fighting or resolved (active cleared).
    # Casualties must have been possible: original state untouched.
    assert state.active_combat is not None


def test_is_terminal_false_in_combat_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    assert not problem.is_terminal(state)


def test_is_terminal_true_after_end_phase():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    new_state = problem.apply(state, end_phase("gondor"))
    assert new_state.phase == "non_combat_move"
    assert problem.is_terminal(new_state)


def test_evaluate_in_unit_range():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    score = problem.evaluate(state)
    assert -1.0 <= score <= 1.0


def test_terminal_value_winner_alliance():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    state.winner = fd["gondor"].alliance
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    assert problem.terminal_value(state) == 1.0
    state.winner = "evil" if fd["gondor"].alliance != "evil" else "good"
    assert problem.terminal_value(state) == -1.0


# ---------------------------------------------------------------------------
# Multi-step composition
# ---------------------------------------------------------------------------


def test_search_composes_initiate_then_end_phase():
    """MCTS can sequence a battle resolution then end the phase when legal."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td, attackers=3, defenders=1)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    budget = SearchBudget(max_iterations=40, max_time_seconds=3.0)
    action = run_mcts(problem, state, budget)
    assert action is not None
    assert action.type in ("initiate_combat", "continue_combat", "retreat", "end_phase")
    # end_phase is illegal at root with contested remain.
    assert action.type != "end_phase"


def test_multi_step_battle_to_phase_end():
    """Driving legal actions until the phase leaves combat always terminates cleanly."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    tid = _plant_contested_territory(state, ud, td, attackers=2, defenders=2)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    problem = CombatProblem(ctx, rng=random.Random(0))
    s = state
    steps = 0
    while s.phase == "combat" and s.winner is None and steps < 30:
        actions = problem.legal_actions(s)
        assert actions, f"stuck with no legal actions at step {steps}"
        # Prefer fighting; fall back to end_phase / retreat.
        pick = next((a for a in actions if a.type in ("initiate_combat", "continue_combat")), actions[0])
        s = problem.apply(s, pick)
        steps += 1
    assert s.phase != "combat" or s.winner is not None
    assert steps < 30


# ---------------------------------------------------------------------------
# decide_combat_mcts: end-to-end validity
# ---------------------------------------------------------------------------


def test_decide_combat_mcts_returns_valid_action():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide_combat_mcts(ctx, rng=random.Random(0))
    assert action is not None
    assert action.type in ("initiate_combat", "continue_combat", "retreat", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_combat_mcts_leaves_dice_empty():
    """Returned initiate/continue must have empty dice so the API fills real rolls."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide_combat_mcts(ctx, rng=random.Random(0))
    if action.type in ("initiate_combat", "continue_combat"):
        dr = action.payload.get("dice_rolls") or {}
        assert not dr.get("attacker")
        assert not dr.get("defender")


def test_decide_combat_mcts_applies_cleanly():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide_combat_mcts(ctx, rng=random.Random(0))
    new_state, _events = apply_action(state, action, ud, td, fd, cd, port_d)
    assert new_state is not None


def test_decide_combat_mcts_end_phase_when_no_battles():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide_combat_mcts(ctx, rng=random.Random(0))
    assert action is not None
    assert action.type == "end_phase"


def test_decide_combat_mcts_active_combat_returns_continue_or_retreat():
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide_combat_mcts(ctx, rng=random.Random(0))
    assert action.type in ("continue_combat", "retreat")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_combat_mcts_deterministic_with_seed():
    state1, ud1, td1, fd1, cd1, port1 = _load_wotr_setup()
    _plant_contested_territory(state1, ud1, td1)
    ctx1 = _combat_ctx(state1, ud1, td1, fd1, cd1, port1)
    state2, ud2, td2, fd2, cd2, port2 = _load_wotr_setup()
    _plant_contested_territory(state2, ud2, td2)
    ctx2 = _combat_ctx(state2, ud2, td2, fd2, cd2, port2)
    a1 = decide_combat_mcts(ctx1, rng=random.Random(42))
    a2 = decide_combat_mcts(ctx2, rng=random.Random(42))
    assert a1.type == a2.type
    assert a1.payload.get("territory_id") == a2.payload.get("territory_id")


# ---------------------------------------------------------------------------
# ai_algorithm flag routing in decide()
# ---------------------------------------------------------------------------


def test_decide_combat_uses_heuristic_by_default(monkeypatch):
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("continue_combat", "retreat")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_combat_routes_to_mcts_when_flag_set(monkeypatch):
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_contested_territory(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("initiate_combat", "continue_combat", "retreat", "end_phase")
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_decide_combat_mcts_flag_off_keeps_heuristic_path(monkeypatch):
    """AI_ALGORITHM=heuristic (or unset) must not call into the MCTS combat module."""
    monkeypatch.setenv("AI_ALGORITHM", "heuristic")
    called = {"mcts": 0}

    import backend.ai.mcts.combat as mcts_combat

    original = mcts_combat.decide_combat_mcts

    def spy(ctx, rng=None):
        called["mcts"] += 1
        return original(ctx, rng=rng)

    monkeypatch.setattr(mcts_combat, "decide_combat_mcts", spy)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    _plant_active_combat(state, ud, td)
    ctx = _combat_ctx(state, ud, td, fd, cd, port_d)
    decide(ctx)
    assert called["mcts"] == 0
