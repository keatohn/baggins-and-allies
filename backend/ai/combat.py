"""
Combat-phase policy: initiate a battle when there are contested territories;
continue vs retreat using battle_net_value and combat sim when in active combat.
"""

import random
from collections import Counter

from backend.engine.actions import initiate_combat, continue_combat, retreat
from backend.engine.combat_sim import SimOptions, SimResult, run_simulation
from backend.engine.movement import _is_sea_zone
from backend.engine.queries import get_retreat_options, participates_in_sea_hex_naval_combat

from backend.ai.context import AIContext
from backend.ai.habits import (
    COMBAT_SIM_TRIALS,
    COMBAT_COMPARE_CASUALTY_ORDERS,
    COMBAT_CONTINUE_MODERATE_VARIANCE_PENALTY,
    COMBAT_CONTINUE_UNPREDICTABLE_VARIANCE_PENALTY,
    COMBAT_INITIATE_MODERATE_VARIANCE_PENALTY,
    COMBAT_INITIATE_UNPREDICTABLE_VARIANCE_PENALTY,
    COMBAT_RETREAT_NET_THRESHOLD,
    COMBAT_STRONGHOLD_RISK_MIN_EV,
    RETREAT_SCORE_FRONTLINE_PENALTY,
    RETREAT_SCORE_THREAT_WEIGHT,
)
from backend.ai.formulas import expected_net_gain, battle_gain_if_win, territory_reinforce_base_score
from backend.ai.geography import (
    count_enemies_that_can_reach_territory_combat_move,
    is_frontline,
)
from backend.ai.randomness import pick_from_score_band


def _split_trials_for_casualty_compare() -> tuple[int, int]:
    """Split COMBAT_SIM_TRIALS across two casualty-order sims (sum equals COMBAT_SIM_TRIALS)."""
    n_a = max(1, COMBAT_SIM_TRIALS // 2)
    n_b = max(1, COMBAT_SIM_TRIALS - n_a)
    return n_a, n_b


def _casualty_order_pair_options(is_stronghold: bool) -> tuple[SimOptions, SimOptions]:
    """SimOptions for best_attack vs best_unit (stronghold: must_conquer)."""
    if is_stronghold:
        return (
            SimOptions(must_conquer=True, casualty_order_attacker="best_attack"),
            SimOptions(must_conquer=True, casualty_order_attacker="best_unit"),
        )
    return (
        SimOptions(casualty_order_attacker="best_attack"),
        SimOptions(casualty_order_attacker="best_unit"),
    )


def _initiate_variance_penalty(sim: SimResult) -> float:
    """Risk-averse nudge: slightly deprioritize battles with swingy attacker casualty costs."""
    cat = sim.attacker_casualty_cost_variance_category
    if cat == "Unpredictable":
        return float(COMBAT_INITIATE_UNPREDICTABLE_VARIANCE_PENALTY)
    if cat == "Moderate":
        return float(COMBAT_INITIATE_MODERATE_VARIANCE_PENALTY)
    return 0.0


def _initiate_net_score(
    sim: SimResult,
    territory_id: str,
    territory,
    ctx: AIContext,
) -> float:
    def_cas_mean = sim.defender_casualty_cost_mean
    if territory and getattr(territory, "owner", None) is None:
        def_cas_mean = 0.0
    net = expected_net_gain(
        sim.p_attacker_win,
        territory_id,
        ctx.territory_defs,
        ctx.faction_defs,
        def_cas_mean,
        sim.attacker_casualty_cost_mean,
        ctx.camp_defs,
        ctx.port_defs,
        ctx.unit_defs,
    )
    return net - _initiate_variance_penalty(sim)


def _score_retreat_destination(
    dest_id: str,
    state,
    faction_id: str,
    fd,
    td,
    ud,
) -> float:
    """Higher = safer / more valuable friendly hex to retreat into."""
    base = territory_reinforce_base_score(dest_id, td, fd)
    threat = float(
        count_enemies_that_can_reach_territory_combat_move(
            dest_id, state, faction_id, fd, td, ud
        )
    )
    score = base - threat * RETREAT_SCORE_THREAT_WEIGHT
    if is_frontline(dest_id, state, faction_id, fd, td):
        score -= RETREAT_SCORE_FRONTLINE_PENALTY
    return score


def _continue_net_risk(
    sim: SimResult,
    territory,
    combat_territory_id: str,
    ctx: AIContext,
) -> float:
    def_cas_mean = sim.defender_casualty_cost_mean
    if territory and getattr(territory, "owner", None) is None:
        def_cas_mean = 0.0
    net = expected_net_gain(
        sim.p_attacker_win,
        combat_territory_id,
        ctx.territory_defs,
        ctx.faction_defs,
        def_cas_mean,
        sim.attacker_casualty_cost_mean,
        ctx.camp_defs,
        ctx.port_defs,
        ctx.unit_defs,
    )
    return net - _continue_variance_penalty(sim)


def _continue_variance_penalty(sim: SimResult) -> float:
    """Softer than initiate: penalize net when deciding whether to keep fighting."""
    cat = sim.attacker_casualty_cost_variance_category
    if cat == "Unpredictable":
        return float(COMBAT_CONTINUE_UNPREDICTABLE_VARIANCE_PENALTY)
    if cat == "Moderate":
        return float(COMBAT_CONTINUE_MODERATE_VARIANCE_PENALTY)
    return 0.0


def decide_initiate_combat(ctx: AIContext):
    """
    When phase is combat, no active_combat, and there are contested territories:
    score each by sim expected_net_gain minus variance penalty; pick from top band.
    Falls back to random entry if sims cannot be built.
    """
    territories = ctx.available_actions.get("combat_territories") or []
    if not territories:
        return None
    state = ctx.state
    ud = ctx.unit_defs
    td = ctx.territory_defs
    fd = ctx.faction_defs

    scored: list[tuple[dict, float]] = []
    for entry in territories:
        tid = entry.get("territory_id")
        if not tid:
            continue
        att_ids = entry.get("attacker_unit_ids") or []
        def_ids = entry.get("defender_unit_ids") or []
        att_stacks = _stacks_from_instance_ids(state, tid, att_ids, ud)
        def_stacks = _stacks_from_instance_ids(state, tid, def_ids, ud)
        if not att_stacks or not def_stacks:
            continue
        tdef = td.get(tid)
        is_sh = bool(tdef and getattr(tdef, "is_stronghold", False))
        terr = state.territories.get(tid)
        try:
            if COMBAT_COMPARE_CASUALTY_ORDERS:
                n_a, n_b = _split_trials_for_casualty_compare()
                oa, ob = _casualty_order_pair_options(is_sh)
                sim_a = run_simulation(
                    att_stacks,
                    def_stacks,
                    tid,
                    ud,
                    td,
                    n_trials=n_a,
                    options=oa,
                )
                sim_b = run_simulation(
                    att_stacks,
                    def_stacks,
                    tid,
                    ud,
                    td,
                    n_trials=n_b,
                    options=ob,
                )
                sa = _initiate_net_score(sim_a, tid, terr, ctx)
                sb = _initiate_net_score(sim_b, tid, terr, ctx)
                score = sb if sb > sa + 1e-9 else sa
            else:
                opts = None
                if is_sh:
                    opts = SimOptions(
                        must_conquer=True, casualty_order_attacker="best_attack"
                    )
                sim = run_simulation(
                    att_stacks,
                    def_stacks,
                    tid,
                    ud,
                    td,
                    n_trials=COMBAT_SIM_TRIALS,
                    options=opts,
                )
                score = _initiate_net_score(sim, tid, terr, ctx)
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        scored.append((entry, score))

    if not scored:
        entry = random.choice(territories) if len(territories) > 1 else territories[0]
    else:
        picked = pick_from_score_band(scored)
        entry = picked if picked is not None else scored[0][0]

    territory_id = entry.get("territory_id")
    sea_zone_id = entry.get("sea_zone_id")
    if not territory_id:
        return None
    return initiate_combat(
        ctx.faction_id,
        territory_id,
        {"attacker": [], "defender": []},
        sea_zone_id=sea_zone_id,
    )


def _units_to_stacks(units: list) -> list[dict]:
    """Convert list of Unit-like (with unit_id) to [{"unit_id": str, "count": int}, ...]."""
    counts = Counter(getattr(u, "unit_id", "") for u in units if getattr(u, "unit_id", ""))
    return [{"unit_id": uid, "count": c} for uid, c in counts.items()]


def _stacks_from_instance_ids(state, territory_id: str, instance_ids: list[str], ud) -> list[dict]:
    """Stacks for units in this territory whose instance_id is listed."""
    terr = state.territories.get(territory_id)
    if not terr:
        return []
    want = set(instance_ids)
    units = [
        u
        for u in (getattr(terr, "units", []) or [])
        if getattr(u, "instance_id", "") in want
    ]
    return _units_to_stacks(units)


def decide_combat(ctx: AIContext):
    """
    When there is active_combat and current faction is the attacker:
    run combat sim, compute battle_net_value; if > 0 return continue_combat (dice generated by API),
    else return retreat to first valid destination.
    """
    state = ctx.state
    combat = state.active_combat
    if not combat or state.current_faction != getattr(combat, "attacker_faction", ""):
        return None

    territory = state.territories.get(combat.territory_id)
    if not territory or not hasattr(territory, "units"):
        return None

    attacker_ids = set(combat.attacker_instance_ids)
    sea_zone_id = getattr(combat, "sea_zone_id", None)
    # Match reducer / API _get_active_combat_units: after offload, attackers may be on land only
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        in_sea = [
            u for u in (sea_zone.units if sea_zone else [])
            if getattr(u, "instance_id", "") in attacker_ids
        ]
        attacker_territory = sea_zone if in_sea else territory
    else:
        attacker_territory = territory
    attackers = [u for u in (getattr(attacker_territory, "units", []) or []) if getattr(u, "instance_id", "") in attacker_ids]
    defenders = [u for u in territory.units if getattr(u, "instance_id", "") not in attacker_ids]
    tdef_combat = ctx.territory_defs.get(combat.territory_id)
    if tdef_combat and _is_sea_zone(tdef_combat):
        ud = ctx.unit_defs
        attackers = [u for u in attackers if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))]
        defenders = [u for u in defenders if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))]
    if not attackers or not defenders:
        return None

    att_stacks = _units_to_stacks(attackers)
    def_stacks = _units_to_stacks(defenders)
    if not att_stacks or not def_stacks:
        return None

    # Re-evaluate each round: run sim with current survivors, then decide continue vs retreat
    is_stronghold = bool(
        tdef_combat and getattr(tdef_combat, "is_stronghold", False)
    )
    chosen_order: str | None = None
    if COMBAT_COMPARE_CASUALTY_ORDERS:
        n_attack, n_unit = _split_trials_for_casualty_compare()
        opt_attack, opt_unit = _casualty_order_pair_options(is_stronghold)
        sim_attack = run_simulation(
            att_stacks,
            def_stacks,
            combat.territory_id,
            ctx.unit_defs,
            ctx.territory_defs,
            n_trials=n_attack,
            options=opt_attack,
        )
        sim_unit = run_simulation(
            att_stacks,
            def_stacks,
            combat.territory_id,
            ctx.unit_defs,
            ctx.territory_defs,
            n_trials=n_unit,
            options=opt_unit,
        )
        nr_a = _continue_net_risk(sim_attack, territory, combat.territory_id, ctx)
        nr_u = _continue_net_risk(sim_unit, territory, combat.territory_id, ctx)
        if nr_u > nr_a + 1e-9:
            sim_result = sim_unit
            chosen_order = "best_unit"
        else:
            sim_result = sim_attack
            chosen_order = "best_attack"
    else:
        sim_opts = (
            SimOptions(must_conquer=True, casualty_order_attacker="best_attack")
            if is_stronghold
            else None
        )
        sim_result = run_simulation(
            att_stacks,
            def_stacks,
            combat.territory_id,
            ctx.unit_defs,
            ctx.territory_defs,
            n_trials=COMBAT_SIM_TRIALS,
            options=sim_opts,
        )

    win_rate = sim_result.p_attacker_win
    net_risk = _continue_net_risk(
        sim_result, territory, combat.territory_id, ctx
    )
    gain_if_win = battle_gain_if_win(
        combat.territory_id, ctx.territory_defs, ctx.faction_defs
    )

    # Continue if net is above threshold, or if high-value target (e.g. stronghold) justifies the risk
    continue_anyway = (
        is_stronghold
        and (win_rate * gain_if_win) >= COMBAT_STRONGHOLD_RISK_MIN_EV
    )
    if net_risk > COMBAT_RETREAT_NET_THRESHOLD or continue_anyway:
        casualty_order = None
        must_conquer = None
        if COMBAT_COMPARE_CASUALTY_ORDERS:
            if is_stronghold:
                must_conquer = True
            casualty_order = (
                "best_attack" if chosen_order == "best_attack" else None
            )
        elif is_stronghold:
            casualty_order = "best_attack"
            must_conquer = True
        return continue_combat(
            state.current_faction,
            dice_rolls={"attacker": [], "defender": []},
            casualty_order=casualty_order,
            must_conquer=must_conquer,
        )
    # Retreat: score adjacent friendly hexes (value − threat − frontline exposure)
    retreat_destinations = get_retreat_options(
        state, ctx.territory_defs, ctx.faction_defs, ctx.unit_defs
    )
    if not retreat_destinations:
        return continue_combat(
            state.current_faction,
            dice_rolls={"attacker": [], "defender": []},
        )
    if len(retreat_destinations) == 1:
        return retreat(state.current_faction, retreat_destinations[0])
    scored = [
        (
            d,
            _score_retreat_destination(
                d,
                state,
                state.current_faction,
                ctx.faction_defs,
                ctx.territory_defs,
                ctx.unit_defs,
            ),
        )
        for d in retreat_destinations
    ]
    best = pick_from_score_band(scored)
    dest = best if best is not None else retreat_destinations[0]
    return retreat(state.current_faction, dest)
