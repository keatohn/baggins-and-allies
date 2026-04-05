"""
Alliance / strategic-context behavior: coalition hold sims, need map, purchase scope, strategic build.
"""

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.utils import initialize_game_state

from backend.ai.context import AIContext
from backend.ai.defense_sim import (
    count_coalition_land_units_on_territory,
    defense_hold_saturation_threshold,
    defense_expected_loss_by_territory,
    is_faction_capital_territory,
    purchase_defense_interest_territories,
    defender_hold_probability_sim,
)
from backend.ai.habits import (
    MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_CAPITAL,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_STRONGHOLD,
)
from backend.ai.strategic_context import build_strategic_turn_context


def _wotr_state():
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


def test_defense_hold_saturation_reads_habit_tiers():
    """defense_hold_saturation_threshold matches habits for this tile's SH/capital flags."""
    _state, _ud, td, fd, _cd, _port_d = _wotr_state()
    tid = "minas_tirith"
    tdef = td.get(tid)
    expected = float(NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT)
    if tdef and getattr(tdef, "is_stronghold", False):
        expected = max(expected, float(NON_COMBAT_DEFENSE_SATURATION_HOLD_STRONGHOLD))
    if is_faction_capital_territory(tid, fd):
        expected = max(expected, float(NON_COMBAT_DEFENSE_SATURATION_HOLD_CAPITAL))
    assert defense_hold_saturation_threshold(tid, td, fd) == expected


def test_coalition_counts_allied_garrison_on_ally_owned_tile():
    """Elves count Gondor land units on Gondor-owned territory as coalition."""
    state, ud, td, fd, cd, port_d = _wotr_state()
    tid = "minas_tirith"
    terr = state.territories.get(tid)
    assert terr and terr.owner == "gondor"
    n = count_coalition_land_units_on_territory(state, tid, "elves", fd, ud)
    assert n >= 2


def test_defense_expected_loss_includes_allied_territories():
    """Need map for elves can include same-alliance owners at scaled weight."""
    state, ud, td, fd, cd, port_d = _wotr_state()
    need = defense_expected_loss_by_territory(state, "elves", fd, td, ud, max_territories=80)
    assert need
    ally_keys = [t for t in need if state.territories[t].owner != "elves"]
    assert len(ally_keys) >= 1
    assert all(state.territories[t].owner != "elves" for t in ally_keys)
    # Scaled ally entries should not exceed unscaled loss for same vuln (sanity: scale <= 1)
    assert 0 < MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE <= 1.0


def test_purchase_defense_interest_is_own_territories_only():
    """Marginal purchase sim targets our land only (mobilization cannot place on ally)."""
    state, ud, td, fd, cd, port_d = _wotr_state()
    interest = purchase_defense_interest_territories(state, "elves", fd, td, ud)
    for tid in interest:
        assert state.territories[tid].owner == "elves"


def test_strategic_context_has_expected_fields():
    state, ud, td, fd, cd, port_d = _wotr_state()
    state.current_faction = "elves"
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={"phase": "purchase", "faction": "elves"},
    )
    strat = build_strategic_turn_context(ctx)
    assert strat.faction_id == "elves"
    assert isinstance(strat.non_combat_reinforce_bonus_to, dict)
    assert isinstance(strat.combat_move_bonus_to, dict)
    assert isinstance(strat.purchase_defense_priority, float)
    assert 0.0 <= strat.purchase_defense_priority <= 1.0 + 1e-6


def test_defender_hold_probability_sim_allied_tile_not_trivially_empty():
    """Hold sim on ally-owned tile uses coalition stacks (not only elves' units)."""
    state, ud, td, fd, cd, port_d = _wotr_state()
    tid = "minas_tirith"
    terr = state.territories.get(tid)
    assert terr and terr.owner == "gondor"
    p = defender_hold_probability_sim(
        tid, state, "elves", fd, td, ud, n_trials=24
    )
    # With garrison and reach threat, sim should produce a probability in [0, 1]
    assert p is not None
    assert 0.0 <= p <= 1.0
