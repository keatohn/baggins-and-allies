"""
MCTS 3.6a: centralized env-tunable evaluation weights.

- Defaults are bit-identical to the pre-3.6 hardcoded `_score_board` /
  purchase-defense blend (zero behavior change).
- Env knobs (`AI_MCTS_W_*`) override the weights; invalid or out-of-range
  values fall back to defaults.
- Weights are captured once per Problem construction (one search, one set).
- The heuristic AI's constants are not affected by the MCTS knobs.
"""

import random

import pytest

from backend.ai.formulas import (
    attack_value_per_power,
    defense_value_per_power,
    get_unit_power_cost,
)
from backend.ai.context import AIContext
from backend.engine.state import UnitStack
from backend.ai.habits import PURCHASE_DEFENSE_WEIGHT_DEFAULT
from backend.ai.mcts.combat import CombatProblem
from backend.ai.mcts.combat_move import CombatMoveProblem
from backend.ai.mcts.eval_weights import (
    EvalWeights,
    board_score,
    get_eval_weights,
)
from backend.ai.mcts.mobilization import MobilizationProblem
from backend.ai.mcts.non_combat_move import NonCombatMoveProblem
from backend.ai.mcts.purchase import PurchaseProblem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _ctx(
    state, ud, td, fd, cd, port_d, faction_id: str = "gondor", phase: str = "combat"
) -> AIContext:
    state.phase = phase
    state.current_faction = faction_id
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={"phase": phase, "faction": faction_id},
    )


def _legacy_score_board(state, faction_id, td, fd, ud) -> float:
    """Verbatim pre-3.6 private `_score_board` (the four copies were identical)."""
    our_power = 0.0
    enemy_power = 0.0
    our_terr = 0.0
    enemy_terr = 0.0
    our_alliance = ""
    fd_row = fd.get(faction_id)
    if fd_row:
        our_alliance = getattr(fd_row, "alliance", "") or ""

    for tid, terr in (state.territories or {}).items():
        owner = getattr(terr, "owner", None)
        value = 0.0
        tdef = td.get(tid)
        if tdef:
            if getattr(tdef, "is_stronghold", False):
                value += 3.0
            produces = getattr(tdef, "produces", None)
            if isinstance(produces, dict):
                value += float(produces.get("power", 0) or 0) * 0.5
        owner_is_ours = owner == faction_id
        owner_is_enemy = False
        if owner is not None and not owner_is_ours:
            owner_alliance = getattr(fd.get(owner), "alliance", None) if fd.get(owner) else None
            owner_is_enemy = owner_alliance != our_alliance
        if owner_is_ours:
            our_terr += value
        elif owner_is_enemy:
            enemy_terr += value

        for unit in getattr(terr, "units", []) or []:
            udef = ud.get(getattr(unit, "unit_id", ""))
            cost = get_unit_power_cost(udef)
            if cost <= 0:
                continue
            u_faction = getattr(udef, "faction", None)
            u_alliance = (
                getattr(fd.get(u_faction), "alliance", None)
                if u_faction and fd.get(u_faction)
                else None
            )
            if u_faction == faction_id or u_alliance == our_alliance:
                if u_alliance is not None and u_alliance != our_alliance:
                    enemy_power += cost
                else:
                    our_power += cost
            elif u_faction is not None:
                enemy_power += cost

    raw = (our_power + our_terr) - (enemy_power + enemy_terr)
    return raw / (1.0 + abs(raw))


def _clear_weight_env(monkeypatch) -> None:
    for name in (
        "AI_MCTS_W_MATERIAL",
        "AI_MCTS_W_TERRITORY",
        "AI_MCTS_W_STRONGHOLD",
        "AI_MCTS_W_POWER_PROD",
        "AI_MCTS_W_BOARD_SCALE",
        "AI_MCTS_W_PURCHASE_DEFENSE",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# get_eval_weights: defaults, env overrides, invalid fallbacks
# ---------------------------------------------------------------------------


def test_get_eval_weights_defaults_match_legacy_constants(monkeypatch):
    _clear_weight_env(monkeypatch)
    w = get_eval_weights()
    assert w == EvalWeights(
        material_weight=1.0,
        territory_weight=1.0,
        stronghold_value=3.0,
        power_produce_scale=0.5,
        board_scale=1.0,
        purchase_defense_weight=PURCHASE_DEFENSE_WEIGHT_DEFAULT,
    )


def test_env_knobs_override_each_field(monkeypatch):
    _clear_weight_env(monkeypatch)
    monkeypatch.setenv("AI_MCTS_W_MATERIAL", "2.5")
    monkeypatch.setenv("AI_MCTS_W_TERRITORY", "0.25")
    monkeypatch.setenv("AI_MCTS_W_STRONGHOLD", "7")
    monkeypatch.setenv("AI_MCTS_W_POWER_PROD", "1.5")
    monkeypatch.setenv("AI_MCTS_W_BOARD_SCALE", "0.5")
    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "0.9")
    w = get_eval_weights()
    assert w.material_weight == 2.5
    assert w.territory_weight == 0.25
    assert w.stronghold_value == 7.0
    assert w.power_produce_scale == 1.5
    assert w.board_scale == 0.5
    assert w.purchase_defense_weight == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("name", "value", "field", "expected"),
    [
        ("AI_MCTS_W_MATERIAL", "abc", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "   ", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "-1", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "nan", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "inf", "material_weight", 1.0),
        ("AI_MCTS_W_MATERIAL", "1e999", "material_weight", 1.0),
        ("AI_MCTS_W_TERRITORY", "-0.5", "territory_weight", 1.0),
        ("AI_MCTS_W_STRONGHOLD", "not-a-number", "stronghold_value", 3.0),
        ("AI_MCTS_W_POWER_PROD", "nan", "power_produce_scale", 0.5),
        ("AI_MCTS_W_BOARD_SCALE", "-2", "board_scale", 1.0),
        ("AI_MCTS_W_PURCHASE_DEFENSE", "1.5", "purchase_defense_weight", 0.5),
        ("AI_MCTS_W_PURCHASE_DEFENSE", "-0.2", "purchase_defense_weight", 0.5),
        ("AI_MCTS_W_PURCHASE_DEFENSE", "abc", "purchase_defense_weight", 0.5),
    ],
)
def test_invalid_env_values_fall_back_to_default(monkeypatch, name, value, field, expected):
    _clear_weight_env(monkeypatch)
    monkeypatch.setenv(name, value)
    assert getattr(get_eval_weights(), field) == expected


def test_get_eval_weights_reads_env_each_call(monkeypatch):
    """Not cached: a per-turn env swap (self-play matchups) takes effect next call."""
    _clear_weight_env(monkeypatch)
    assert get_eval_weights().board_scale == 1.0
    monkeypatch.setenv("AI_MCTS_W_BOARD_SCALE", "3.0")
    assert get_eval_weights().board_scale == 3.0
    monkeypatch.delenv("AI_MCTS_W_BOARD_SCALE")
    assert get_eval_weights().board_scale == 1.0


# ---------------------------------------------------------------------------
# board_score: default parity + env effect
# ---------------------------------------------------------------------------


def test_board_score_default_bit_identical_to_legacy(monkeypatch):
    """Defaults reproduce the pre-3.6 `_score_board` exactly (float identity)."""
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    for faction_id in ("gondor", "mordor", "elves", "rohan"):
        expected = _legacy_score_board(state, faction_id, td, fd, ud)
        got = board_score(state, faction_id, td, fd, ud, get_eval_weights())
        assert got == expected, f"score mismatch for {faction_id}"


def test_board_score_legacy_parity_on_mutated_state(monkeypatch):
    """Parity holds after flipping owners (exercises enemy-territory branches)."""
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    for tid in ("minas_tirith", "isengard", "dol_amroth"):
        if tid in state.territories:
            state.territories[tid].owner = "mordor" if tid != "isengard" else "gondor"
    for faction_id in ("gondor", "mordor"):
        assert (
            board_score(state, faction_id, td, fd, ud, get_eval_weights())
            == _legacy_score_board(state, faction_id, td, fd, ud)
        )


def test_board_scale_env_doubles_score_exactly(monkeypatch):
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    base = board_score(state, "gondor", td, fd, ud, get_eval_weights())
    monkeypatch.setenv("AI_MCTS_W_BOARD_SCALE", "2.0")
    assert board_score(state, "gondor", td, fd, ud, get_eval_weights()) == 2.0 * base


def test_zero_material_and_territory_weights_give_zero_score(monkeypatch):
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    monkeypatch.setenv("AI_MCTS_W_MATERIAL", "0")
    monkeypatch.setenv("AI_MCTS_W_TERRITORY", "0")
    assert board_score(state, "gondor", td, fd, ud, get_eval_weights()) == 0.0


def test_zero_territory_weight_changes_score(monkeypatch):
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    base = board_score(state, "gondor", td, fd, ud, get_eval_weights())
    monkeypatch.setenv("AI_MCTS_W_TERRITORY", "0")
    changed = board_score(state, "gondor", td, fd, ud, get_eval_weights())
    assert changed != base


# ---------------------------------------------------------------------------
# Problem wiring: delegation + capture-at-construction
# ---------------------------------------------------------------------------


def test_all_four_problems_delegate_to_legacy_parity(monkeypatch):
    """Every Problem's `_score_board` matches the shared scorer AND the legacy copy."""
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    ctx = _ctx(state, ud, td, fd, cd, port_d, "gondor")
    expected = _legacy_score_board(state, "gondor", td, fd, ud)
    for cls in (CombatProblem, CombatMoveProblem, NonCombatMoveProblem, MobilizationProblem):
        problem = cls(ctx, rng=random.Random(0))
        assert problem._score_board(state) == expected, cls.__name__
        assert (
            board_score(state, "gondor", td, fd, ud, problem._eval_weights) == expected
        ), cls.__name__


def test_problems_capture_weights_at_construction(monkeypatch):
    """One search = one weight set: env is read once when the Problem is built."""
    _clear_weight_env(monkeypatch)
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    base = board_score(state, "gondor", td, fd, ud, get_eval_weights())

    monkeypatch.setenv("AI_MCTS_W_BOARD_SCALE", "2.0")
    ctx = _ctx(state, ud, td, fd, cd, port_d, "gondor")
    problem = CombatProblem(ctx, rng=random.Random(0))
    monkeypatch.delenv("AI_MCTS_W_BOARD_SCALE")

    # Problem keeps the construction-time weight set...
    assert problem._eval_weights.board_scale == 2.0
    assert problem._score_board(state) == 2.0 * base
    # ...while a fresh call (new decide) picks up the new env.
    assert get_eval_weights().board_scale == 1.0
    fresh = CombatProblem(ctx, rng=random.Random(0))
    assert fresh._score_board(state) == base


# ---------------------------------------------------------------------------
# Purchase defense/offense blend
# ---------------------------------------------------------------------------


def _purchase_problem(monkeypatch, faction_id="gondor", purchased=None):
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.faction_resources.setdefault(faction_id, {})["power"] = 30
    state.phase = "purchase"
    state.current_faction = faction_id
    if purchased:
        state.faction_purchased_units[faction_id] = [
            UnitStack(unit_id=uid, count=n) for uid, n in purchased
        ]
    ctx = _ctx(state, ud, td, fd, cd, port_d, faction_id, phase="purchase")
    return PurchaseProblem(ctx), state, ud, td, fd, cd, port_d


def test_purchase_default_bundle_parity(monkeypatch):
    """Default blend (0.5/0.5) reproduces the legacy `_bundle_prior_score`."""
    _clear_weight_env(monkeypatch)
    problem, state, ud, td, fd, cd, port_d = _purchase_problem(monkeypatch)
    bundle = {"gondor_archer": 2, "gondor_soldier": 1}
    total = 0.0
    for uid, count in bundle.items():
        udef = ud.get(uid)
        cost = get_unit_power_cost(udef)
        d = defense_value_per_power(udef, cost, 0)
        a = attack_value_per_power(udef, cost, 0)
        total += count * (PURCHASE_DEFENSE_WEIGHT_DEFAULT * d + (1.0 - PURCHASE_DEFENSE_WEIGHT_DEFAULT) * a)
    expected = max(0.05, total)
    assert problem._bundle_prior_score(state, bundle) == expected


def test_purchase_defense_weight_env_pure_defense_and_pure_attack(monkeypatch):
    """AI_MCTS_W_PURCHASE_DEFENSE=1.0 -> all defense; =0.0 -> all attack."""
    _clear_weight_env(monkeypatch)
    bundle = {"gondor_archer": 2}
    udef_archer = None

    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "1.0")
    problem_def, state, ud, td, fd, cd, port_d = _purchase_problem(monkeypatch)
    udef_archer = ud["gondor_archer"]
    cost = get_unit_power_cost(udef_archer)
    expected_def = max(
        0.05, 2 * defense_value_per_power(udef_archer, cost, 0)
    )
    assert problem_def._bundle_prior_score(state, bundle) == expected_def

    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "0.0")
    problem_off, state2, *_ = _purchase_problem(monkeypatch)
    expected_att = max(0.05, 2 * attack_value_per_power(udef_archer, cost, 0))
    assert problem_off._bundle_prior_score(state2, bundle) == expected_att

    # Defense-heavy unit (trebuchet: def 2.4 vs atk 0.4/power) scores higher
    # the more weight defense gets; attack-heavy archer the opposite.
    tb = {"trebuchet": 1}
    assert problem_def._bundle_prior_score(state, tb) > problem_off._bundle_prior_score(state2, tb)
    assert problem_def._bundle_prior_score(state, bundle) < problem_off._bundle_prior_score(state2, bundle)


def test_purchase_score_purchased_uses_env_weight(monkeypatch):
    _clear_weight_env(monkeypatch)
    purchased = [("gondor_archer", 2)]

    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "1.0")
    p_def, state, ud, td, fd, cd, port_d = _purchase_problem(monkeypatch, purchased=purchased)
    cost = get_unit_power_cost(ud["gondor_archer"])
    raw_def = 2 * defense_value_per_power(ud["gondor_archer"], cost, 0)
    assert p_def._score_purchased(state) == raw_def / (1.0 + abs(raw_def))

    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "0.0")
    p_off, state2, ud2, *_rest = _purchase_problem(monkeypatch, purchased=purchased)
    raw_att = 2 * attack_value_per_power(ud2["gondor_archer"], cost, 0)
    assert p_off._score_purchased(state2) == raw_att / (1.0 + abs(raw_att))
    assert p_def._score_purchased(state) < p_off._score_purchased(state2)


def test_purchase_score_purchased_default_parity(monkeypatch):
    _clear_weight_env(monkeypatch)
    purchased = [("gondor_soldier", 3)]
    problem, state, ud, td, fd, cd, port_d = _purchase_problem(monkeypatch, purchased=purchased)
    cost = get_unit_power_cost(ud["gondor_soldier"])
    d = defense_value_per_power(ud["gondor_soldier"], cost, 0)
    a = attack_value_per_power(ud["gondor_soldier"], cost, 0)
    raw = 3 * (0.5 * d + 0.5 * a)
    assert problem._score_purchased(state) == raw / (1.0 + abs(raw))


def test_purchase_captures_weight_at_construction(monkeypatch):
    _clear_weight_env(monkeypatch)
    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "1.0")
    problem, state, ud, td, fd, cd, port_d = _purchase_problem(monkeypatch, purchased=[("gondor_archer", 2)])
    monkeypatch.delenv("AI_MCTS_W_PURCHASE_DEFENSE")
    cost = get_unit_power_cost(ud["gondor_archer"])
    raw_def = 2 * defense_value_per_power(ud["gondor_archer"], cost, 0)
    # Still the construction-time (defense-only) blend despite env cleared.
    assert problem._eval_weights.purchase_defense_weight == 1.0
    assert problem._score_purchased(state) == raw_def / (1.0 + abs(raw_def))


def test_heuristic_default_constant_unaffected_by_mcts_knob(monkeypatch):
    """The MCTS knob is MCTS-only; the heuristic AI's constant stays put."""
    _clear_weight_env(monkeypatch)
    monkeypatch.setenv("AI_MCTS_W_PURCHASE_DEFENSE", "1.0")
    from backend.ai.habits import PURCHASE_DEFENSE_WEIGHT_DEFAULT as heuristic_default

    assert heuristic_default == 0.5
    assert get_eval_weights().purchase_defense_weight == 1.0


# ---------------------------------------------------------------------------
# Determinism under env weights
# ---------------------------------------------------------------------------


def test_decide_combat_mcts_deterministic_under_weight_env(monkeypatch):
    """Same seed + same env weights -> same action (weights don't break determinism)."""
    from backend.engine.queries import validate_action
    from backend.engine.state import Unit

    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    monkeypatch.setenv("AI_MCTS_W_TERRITORY", "0")

    from backend.ai.mcts.combat import decide_combat_mcts

    def _run_once():
        state, ud, td, fd, cd, port_d = _load_wotr_setup()
        state.phase = "combat"
        state.current_faction = "gondor"
        tid = "minas_morgul"
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
        terr.units = [
            Unit(
                instance_id=f"mcts_att_{i}",
                unit_id=att_uid,
                remaining_movement=0,
                remaining_health=ah,
                base_movement=1,
                base_health=ah,
                loaded_onto=None,
            )
            for i in range(2)
        ] + [
            Unit(
                instance_id=f"mcts_def_{i}",
                unit_id=def_uid,
                remaining_movement=0,
                remaining_health=dh,
                base_movement=0,
                base_health=dh,
                loaded_onto=None,
            )
            for i in range(2)
        ]
        terr.owner = "mordor"
        ctx = _ctx(state, ud, td, fd, cd, port_d, "gondor")
        action = decide_combat_mcts(ctx, rng=random.Random(42))
        assert action is not None
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, v.error
        return action.type, dict(action.payload)

    assert _run_once() == _run_once()
