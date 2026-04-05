"""
Purchase-phase policy: buy units for mobilization slots.

If a non-allied stack can already reach one of our territories on the next combat_move (same
reachability as the game), other factions may attack before we get to use purchases for offense.
In that case we score purchases on defensive value only and do not run offensive purchase
heuristics (attack targets, siege need). Otherwise we blend defense and attack like before.

Land purchases also get a sim-based term: marginal worst-case P(hold) gain from adding one unit
at threatened territories (weighted by territory_loss_cost), scaled per power. Each land slot
re-scores using phantom defenders stacked on the tile with best marginal weighted delta so
later picks see diminishing returns on the same front.

Units with the home special: while at least one home mobilization slot remains for that unit this
phase (same accounting as mobilization's home-first path), sim and phantom stacking use only owned
home territories — not the global interest list — so purchase value matches expected deployment.
When no home slot remains, they use the same interest-territory sim as other land units.
"""

from backend.engine.actions import purchase_units, end_phase
from backend.engine.queries import (
    get_purchasable_units,
    get_mobilization_capacity,
    get_mobilization_territories,
    count_open_home_mobilization_slots_for_unit,
)
from backend.engine.queries import _is_naval_unit, get_unit_faction
from backend.engine.utils import faction_owns_capital, has_unit_special
from backend.engine.movement import get_reachable_territories_for_unit
from backend.engine.state import Unit

from backend.ai.context import AIContext
from backend.ai.habits import (
    MIN_POWER_RESERVE,
    PURCHASE_DEFENSE_WEIGHT_DEFAULT,
    PURCHASE_DEFENSE_SIM_TRIALS,
    PURCHASE_DIVERSITY_OVER_RATIO,
    PURCHASE_DIVERSITY_OVER_PENALTY,
    PURCHASE_DIVERSITY_UNDER_RATIO,
    PURCHASE_DIVERSITY_UNDER_BONUS,
    PURCHASE_BATCH_DIVERSITY_PENALTY,
    MAX_ACTIVE_SIEGEWORK,
    PURCHASE_CHEAPEST_INFANTRY_BONUS,
    PURCHASE_CHEAP_LINE_COST_BAND_PAD,
    PURCHASE_CHEAP_LINE_DIVERSITY_OVER_MULT,
    PURCHASE_SIM_DEFENSE_VALUE_SCALE,
    PURCHASE_PHANTOM_SPREAD_COEFF,
    NAVAL_PURCHASE_COAST_PRESSURE_MAX,
    NAVAL_PURCHASE_COAST_PRESSURE_SCALE,
)
from backend.ai.formulas import (
    get_unit_power_cost,
    defense_value_per_power,
    attack_value_per_power,
    purchase_cost_bounds,
    cost_range_bonus,
    territory_loss_cost,
)
from backend.ai.defense_sim import (
    marginal_hold_delta_add_land_unit,
    merge_combat_move_attacker_stacks,
    purchase_defense_interest_territories,
)


def _power_cost(unit_cost: dict) -> int:
    """Extract power cost from unit cost dict (e.g. {"power": 5} -> 5)."""
    if isinstance(unit_cost, dict):
        return int(unit_cost.get("power", 0) or 0)
    return 0


def _active_unit_mean_cost(
    state,
    faction_id: str,
    unit_defs: dict,
) -> float:
    """
    Mean power cost of our active units (on map + in purchased pool this turn).
    Used for cost-range incentive: try to keep mean within [lower, upper].
    """
    total_cost = 0
    total_count = 0
    for tid, t in (state.territories or {}).items():
        for u in getattr(t, "units", []) or []:
            ud = unit_defs.get(getattr(u, "unit_id", ""))
            if not ud:
                continue
            # Same faction? Unit has unit_id; faction comes from unit_def
            if getattr(ud, "faction", "") != faction_id:
                continue
            c = get_unit_power_cost(ud)
            total_cost += c
            total_count += 1
    for stack in (state.faction_purchased_units or {}).get(faction_id, []) or []:
        count = getattr(stack, "count", 0) or 0
        ud = unit_defs.get(getattr(stack, "unit_id", ""))
        if ud and count > 0:
            c = get_unit_power_cost(ud)
            total_cost += c * count
            total_count += count
    if total_count <= 0:
        return 0.0
    return total_cost / total_count


def _is_cavalry(ud, unit_id: str) -> bool:
    u = ud.get(unit_id) if ud else None
    if not u:
        return False
    if getattr(u, "archetype", "") == "cavalry":
        return True
    return "cavalry" in getattr(u, "tags", [])


def _is_base_infantry_no_special(ud, unit_id: str) -> bool:
    """True for infantry archetype with no specials (default line infantry candidate)."""
    u = ud.get(unit_id) if ud else None
    if not u:
        return False
    if getattr(u, "archetype", "") != "infantry":
        return False
    specials = getattr(u, "specials", []) or []
    if not isinstance(specials, list):
        return False
    return len(specials) == 0


def _make_dummy_unit_with_full_movement(unit_id: str, ud) -> Unit | None:
    """Unit with remaining_movement = base_movement for reachability check."""
    u_def = ud.get(unit_id) if ud else None
    if not u_def:
        return None
    base_mov = int(getattr(u_def, "movement", 0) or 0)
    base_hp = int(getattr(u_def, "health", 1) or 1)
    return Unit(
        instance_id="_dummy",
        unit_id=unit_id,
        remaining_movement=base_mov,
        remaining_health=base_hp,
        base_movement=base_mov,
        base_health=base_hp,
        loaded_onto=None,
    )


def _threat_has_cavalry(state, faction_id: str, fd, td, ud) -> bool:
    """True if any enemy cavalry can reach any of our territories (using movement engine, not just adjacency)."""
    our_territories = {
        tid for tid, terr in (state.territories or {}).items()
        if getattr(terr, "owner", None) == faction_id
    }
    if not our_territories:
        return False
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    saved_faction = getattr(state, "current_faction", None)
    try:
        for tid, terr in (state.territories or {}).items():
            for u in getattr(terr, "units", []) or []:
                uid = getattr(u, "unit_id", "")
                uf = get_unit_faction(u, ud)
                if uf == faction_id:
                    continue
                if uf and fd.get(uf) and getattr(fd.get(uf), "alliance", "") == our_alliance:
                    continue
                if not _is_cavalry(ud, uid):
                    continue
                dummy = _make_dummy_unit_with_full_movement(uid, ud)
                if not dummy or dummy.remaining_movement <= 0:
                    continue
                state.current_faction = uf
                reachable, _ = get_reachable_territories_for_unit(
                    dummy, tid, state, ud, td, fd, "combat_move"
                )
                if our_territories & set(reachable.keys()):
                    return True
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return False


def _attack_needs_siege(
    state,
    faction_id: str,
    td,
    fd,
    ud,
    cd,
    port_d,
) -> bool:
    """
    True if there is an enemy or neutral stronghold one step from our land mobilization
    territories (so a slow siegework unit can get there the turn after it's placed).
    """
    mob_territories = get_mobilization_territories(state, faction_id, td, cd, port_d, ud)
    if not mob_territories:
        return False
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    # Use movement 1: only value siege when stronghold is adjacent to mobilization (siege units are slow)
    dummy_unit_id = None
    for uid, u_def in (ud or {}).items():
        if _is_naval_unit(u_def):
            continue
        dummy_unit_id = uid
        break
    if not dummy_unit_id:
        return False
    dummy = _make_dummy_unit_with_full_movement(dummy_unit_id, ud)
    if not dummy:
        return False
    dummy = Unit(
        instance_id=dummy.instance_id,
        unit_id=dummy.unit_id,
        remaining_movement=1,
        remaining_health=dummy.remaining_health,
        base_movement=dummy.base_movement,
        base_health=dummy.base_health,
        loaded_onto=None,
    )
    saved_faction = getattr(state, "current_faction", None)
    try:
        state.current_faction = faction_id
        for start_tid in mob_territories:
            reachable, _ = get_reachable_territories_for_unit(
                dummy, start_tid, state, ud, td, fd, "combat_move"
            )
            for tid in reachable.keys():
                if tid == start_tid:
                    continue
                adj = state.territories.get(tid)
                if not adj:
                    continue
                owner = getattr(adj, "owner", None)
                if owner == faction_id:
                    continue
                if owner and fd.get(owner) and getattr(fd.get(owner), "alliance", "") == our_alliance:
                    continue
                tdef = td.get(tid)
                if tdef and getattr(tdef, "is_stronghold", False):
                    return True
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return False


def _attack_targets_have_cavalry(state, faction_id: str, fd, td, ud) -> bool:
    """True if any enemy territory we can reach (by movement) has cavalry."""
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    saved_faction = getattr(state, "current_faction", None)
    try:
        state.current_faction = faction_id
        for tid, terr in (state.territories or {}).items():
            for u in getattr(terr, "units", []) or []:
                if get_unit_faction(u, ud) != faction_id:
                    continue
                uid = getattr(u, "unit_id", "")
                dummy = _make_dummy_unit_with_full_movement(uid, ud)
                if not dummy or dummy.remaining_movement <= 0:
                    continue
                reachable, _ = get_reachable_territories_for_unit(
                    dummy, tid, state, ud, td, fd, "combat_move"
                )
                for adj_tid in reachable.keys():
                    if adj_tid == tid:
                        continue
                    adj = state.territories.get(adj_tid)
                    if not adj:
                        continue
                    owner = getattr(adj, "owner", None)
                    if owner == faction_id:
                        continue
                    if owner and fd.get(owner) and getattr(fd.get(owner), "alliance", "") == our_alliance:
                        continue
                    for v in getattr(adj, "units", []) or []:
                        if _is_cavalry(ud, getattr(v, "unit_id", "")):
                            return True
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return False


def _is_siegework_unit(ud, unit_id: str) -> bool:
    """True if unit type is siegework archetype."""
    u_def = ud.get(unit_id) if ud else None
    return bool(u_def and getattr(u_def, "archetype", "") == "siegework")


def _active_siegework_count(state, faction_id: str, ud) -> int:
    """Count of siegework units on map + in our faction_purchased_units."""
    n = 0
    for t in (state.territories or {}).values():
        for u in getattr(t, "units", []) or []:
            if get_unit_faction(u, ud) != faction_id:
                continue
            if _is_siegework_unit(ud, getattr(u, "unit_id", "")):
                n += 1
    for stack in (state.faction_purchased_units or {}).get(faction_id, []) or []:
        uid = getattr(stack, "unit_id", "")
        if _is_siegework_unit(ud, uid):
            n += getattr(stack, "count", 0) or 0
    return n


def _active_unit_counts(state, faction_id: str, ud) -> tuple[dict[str, int], int]:
    """Return (unit_id -> count on map + purchased, total_count) for our faction."""
    counts: dict[str, int] = {}
    for t in (state.territories or {}).values():
        for u in getattr(t, "units", []) or []:
            uid = getattr(u, "unit_id", "")
            u_def = ud.get(uid)
            if not u_def or getattr(u_def, "faction", "") != faction_id:
                continue
            counts[uid] = counts.get(uid, 0) + 1
    for stack in (state.faction_purchased_units or {}).get(faction_id, []) or []:
        uid = getattr(stack, "unit_id", "")
        c = getattr(stack, "count", 0) or 0
        if uid and c > 0:
            counts[uid] = counts.get(uid, 0) + c
    total = sum(counts.values())
    return counts, total


def _enemy_can_reach_our_territory_this_combat_cycle(
    state,
    faction_id: str,
    faction_defs: dict,
    territory_defs: dict,
    unit_defs: dict,
) -> bool:
    """
    True if some non-allied unit can move into at least one territory we own on combat_move
    from its current position (same rules as the engine). Purchase happens before later phases;
    other players' turns and combat can happen before we use bought units to attack, so this
    gates offensive purchase scoring.
    """
    our_territories = {
        tid
        for tid, terr in (state.territories or {}).items()
        if getattr(terr, "owner", None) == faction_id
    }
    if not our_territories:
        return False
    our_fd = faction_defs.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    saved_faction = getattr(state, "current_faction", None)
    try:
        for start_tid, terr in (state.territories or {}).items():
            for u in getattr(terr, "units", []) or []:
                uid = getattr(u, "unit_id", "")
                uf = get_unit_faction(u, unit_defs)
                if uf == faction_id:
                    continue
                if uf and faction_defs.get(uf) and getattr(
                    faction_defs.get(uf), "alliance", ""
                ) == our_alliance:
                    continue
                if not uf:
                    continue
                dummy = _make_dummy_unit_with_full_movement(uid, unit_defs)
                if not dummy or dummy.remaining_movement <= 0:
                    continue
                state.current_faction = uf
                reachable, _ = get_reachable_territories_for_unit(
                    dummy, start_tid, state, unit_defs, territory_defs, faction_defs, "combat_move"
                )
                if our_territories & reachable.keys():
                    return True
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return False


def decide_purchase(ctx: AIContext):
    """
    Decide one purchase action: fill mobilization slots one unit at a time.

    Defensive need is evaluated first in a strict sense: if enemies can already reach our
    territory on combat_move, we only score defensive unit value (no offensive purchase
    heuristics). Otherwise we blend defense and attack scoring as before.
    """
    faction_id = ctx.faction_id
    state = ctx.state
    ud = ctx.unit_defs
    td = ctx.territory_defs
    cd = ctx.camp_defs
    port_d = ctx.port_defs

    if not faction_owns_capital(state, faction_id, ctx.faction_defs):
        return end_phase(faction_id)

    purchasable = get_purchasable_units(state, faction_id, ud)
    if not purchasable:
        return end_phase(faction_id)

    power = state.faction_resources.get(faction_id, {}).get("power", 0)
    budget = max(0, power - MIN_POWER_RESERVE)
    if budget <= 0:
        return end_phase(faction_id)

    capacity_info = get_mobilization_capacity(
        state, faction_id, td, cd, port_d, ud
    )
    territories_list = capacity_info.get("territories", [])
    land_cap = sum(t.get("power", 0) for t in territories_list) + sum(
        1 for t in territories_list if t.get("home_unit_capacity")
    )
    sea_cap = sum(z.get("power", 0)
                  for z in capacity_info.get("sea_zones", []))
    fpu = getattr(state, "faction_purchased_units", None) or {}
    already_stacks = fpu.get(faction_id, [])
    already_land = sum(
        s.count for s in already_stacks
        if not _is_naval_unit(ud.get(s.unit_id))
    )
    already_naval = sum(
        s.count for s in already_stacks
        if _is_naval_unit(ud.get(s.unit_id))
    )
    land_remaining = max(0, land_cap - already_land)
    sea_remaining = max(0, sea_cap - already_naval)

    enemy_can_hit_us_before_we_attack = _enemy_can_reach_our_territory_this_combat_cycle(
        state, faction_id, ctx.faction_defs, td, ud
    )
    # Cost bounds: purchasable unit costs (this faction only)
    unit_costs = []
    for p in purchasable:
        uid = p.get("unit_id")
        if not uid:
            continue
        u_def = ud.get(uid)
        c = get_unit_power_cost(u_def) or _power_cost(p.get("cost", {}) or {})
        if c > 0:
            unit_costs.append(c)
    lower_bound, upper_bound = purchase_cost_bounds(unit_costs)
    base_infantry_costs: list[int] = []
    for p in purchasable:
        uid = p.get("unit_id")
        if not uid or not _is_base_infantry_no_special(ud, uid):
            continue
        c = get_unit_power_cost(ud.get(uid)) or _power_cost(p.get("cost", {}) or {})
        if c > 0:
            base_infantry_costs.append(c)
    min_base_infantry_cost = min(base_infantry_costs, default=0)
    # Defense context (always): anti-cavalry etc. for defensive value.
    threat_has_cavalry = _threat_has_cavalry(state, faction_id, ctx.faction_defs, td, ud)
    strategic = ctx.strategic
    sdp = (
        strategic.purchase_defense_priority
        if strategic is not None
        else 0.0
    )
    if enemy_can_hit_us_before_we_attack:
        w_def, w_off = 1.0, 0.0
        target_has_cavalry = False
        attack_needs_siege = False
    else:
        w_def = PURCHASE_DEFENSE_WEIGHT_DEFAULT + (
            1.0 - PURCHASE_DEFENSE_WEIGHT_DEFAULT
        ) * (0.65 * sdp)
        w_def = min(0.88, max(0.12, w_def))
        w_off = 1.0 - w_def
        target_has_cavalry = _attack_targets_have_cavalry(
            state, faction_id, ctx.faction_defs, td, ud
        )
        attack_needs_siege = _attack_needs_siege(
            state, faction_id, td, ctx.faction_defs, ud, cd, port_d
        )
    active_counts, active_total = _active_unit_counts(state, faction_id, ud)
    active_siegework = _active_siegework_count(state, faction_id, ud)

    interest_territories = purchase_defense_interest_territories(
        state,
        faction_id,
        ctx.faction_defs,
        td,
        ud,
        n_trials=PURCHASE_DEFENSE_SIM_TRIALS,
    )
    by_f_purchase_cache: dict[str, dict[str, list]] = {}
    hold_prob_cache: dict[tuple, float | None] = {}

    def _owned_home_territory_ids(unit_id: str) -> list[str]:
        """Home hexes for this unit that we currently own (where home mobilization could apply)."""
        u_def = ud.get(unit_id)
        if not u_def:
            return []
        raw = getattr(u_def, "home_territory_ids", None) or []
        out: list[str] = []
        for tid in raw:
            terr = (state.territories or {}).get(tid)
            if terr and getattr(terr, "owner", None) == faction_id:
                out.append(tid)
        return out

    def land_purchase_sim_boost(
        unit_id: str,
        cost: int,
        phantom_by_tid: dict[str, list[dict]] | None,
        phantom_power_by_tid: dict[str, float] | None,
        *,
        home_rem: int | None = None,
    ) -> float:
        if _is_naval_unit(ud.get(unit_id)):
            return 0.0
        u_def = ud.get(unit_id)
        use_home_sim = (
            u_def
            and has_unit_special(u_def, "home")
            and (getattr(u_def, "home_territory_ids", None) or [])
            and home_rem is not None
            and home_rem > 0
        )
        if use_home_sim:
            sim_territories = _owned_home_territory_ids(unit_id)
        else:
            sim_territories = interest_territories
        if not sim_territories:
            return 0.0
        total = 0.0
        pwr = phantom_power_by_tid or {}
        fd = ctx.faction_defs
        for tid in sim_territories:
            ph = None
            if phantom_by_tid and tid in phantom_by_tid:
                ph = phantom_by_tid[tid]
            spread = 1.0 / (
                1.0 + PURCHASE_PHANTOM_SPREAD_COEFF * float(pwr.get(tid, 0.0))
            )
            delta = marginal_hold_delta_add_land_unit(
                tid,
                state,
                faction_id,
                fd,
                td,
                ud,
                unit_id,
                phantom_defender_stacks=ph,
                by_faction_cache=by_f_purchase_cache,
                n_trials=PURCHASE_DEFENSE_SIM_TRIALS,
                hold_prob_cache=hold_prob_cache,
            )
            if delta <= 0:
                continue
            total += (
                delta
                * territory_loss_cost(tid, td, fd)
                * spread
            )
        return (total / max(1, cost)) * PURCHASE_SIM_DEFENSE_VALUE_SCALE

    def compute_purchase_score(
        p: dict,
        phantom_by_tid: dict[str, list[dict]] | None,
        phantom_power_by_tid: dict[str, float] | None,
        *,
        home_rem: int | None = None,
    ) -> float:
        unit_id = p.get("unit_id")
        if not unit_id:
            return 0.0
        u_def = ud.get(unit_id)
        cost = get_unit_power_cost(u_def) or _power_cost(
            p.get("cost", {}) or {})
        if cost <= 0:
            return 0.0
        turns_to_reach = 0
        d_val = defense_value_per_power(
            u_def, cost, turns_to_reach,
            enemy_has_cavalry=threat_has_cavalry,
        )
        a_val = attack_value_per_power(
            u_def, cost, turns_to_reach,
            enemy_has_cavalry=target_has_cavalry,
            attack_needs_siege=attack_needs_siege,
        )
        sim_boost = land_purchase_sim_boost(
            unit_id, cost, phantom_by_tid, phantom_power_by_tid, home_rem=home_rem
        )
        combined = w_def * (d_val + sim_boost) + w_off * a_val
        cheap_line = (
            min_base_infantry_cost > 0
            and cost == min_base_infantry_cost
            and _is_base_infantry_no_special(ud, unit_id)
        )
        bonus = cost_range_bonus(cost, lower_bound, upper_bound)
        if cheap_line:
            bonus += PURCHASE_CHEAPEST_INFANTRY_BONUS
            if float(cost) < float(lower_bound):
                bonus += PURCHASE_CHEAP_LINE_COST_BAND_PAD
        # Diversity: penalize over-represented types, small bonus for under-represented
        if active_total > 0:
            ratio = active_counts.get(unit_id, 0) / active_total
            if ratio >= PURCHASE_DIVERSITY_OVER_RATIO:
                if cheap_line:
                    bonus += PURCHASE_DIVERSITY_OVER_PENALTY * PURCHASE_CHEAP_LINE_DIVERSITY_OVER_MULT
                else:
                    bonus += PURCHASE_DIVERSITY_OVER_PENALTY
            elif ratio <= PURCHASE_DIVERSITY_UNDER_RATIO:
                bonus += PURCHASE_DIVERSITY_UNDER_BONUS
        return combined + bonus

    def best_phantom_destination(
        unit_id: str,
        phantom_by_tid: dict[str, list[dict]],
        phantom_power_by_tid: dict[str, float],
        *,
        home_rem: int | None = None,
    ) -> str | None:
        """Where this purchase slot most reduces weighted expected loss; multi-front spread via phantom power."""
        if _is_naval_unit(ud.get(unit_id)):
            return None
        u_def = ud.get(unit_id)
        use_home_sim = (
            u_def
            and has_unit_special(u_def, "home")
            and (getattr(u_def, "home_territory_ids", None) or [])
            and home_rem is not None
            and home_rem > 0
        )
        if use_home_sim:
            candidate_territories = _owned_home_territory_ids(unit_id)
        else:
            candidate_territories = interest_territories
        if not candidate_territories:
            return None
        best_tid: str | None = None
        best_w = -1.0
        fd = ctx.faction_defs
        pwr = phantom_power_by_tid or {}
        for tid in candidate_territories:
            ph = phantom_by_tid.get(tid) if phantom_by_tid else None
            spread = 1.0 / (
                1.0 + PURCHASE_PHANTOM_SPREAD_COEFF * float(pwr.get(tid, 0.0))
            )
            delta = marginal_hold_delta_add_land_unit(
                tid,
                state,
                faction_id,
                fd,
                td,
                ud,
                unit_id,
                phantom_defender_stacks=ph,
                by_faction_cache=by_f_purchase_cache,
                n_trials=PURCHASE_DEFENSE_SIM_TRIALS,
                hold_prob_cache=hold_prob_cache,
            )
            w = delta * territory_loss_cost(tid, td, fd) * spread
            if w > best_w + 1e-15:
                best_w = w
                best_tid = tid
        if best_tid is None:
            return None
        if best_w <= 1e-15:
            return candidate_territories[0]
        return best_tid

    naval_coast_pressure_extra = 0.0
    if strategic is not None and strategic.naval_sea_zone_bonus:
        naval_coast_pressure_extra = min(
            NAVAL_PURCHASE_COAST_PRESSURE_MAX,
            NAVAL_PURCHASE_COAST_PRESSURE_SCALE
            * max(strategic.naval_sea_zone_bonus.values()),
        )

    # Land: (unit_id, cost, purchasable row) for per-slot rescoring with phantom garrisons.
    land_entries: list[tuple[str, int, dict]] = []
    naval_options: list[tuple[str, int, float]] = []
    for p in purchasable:
        unit_id = p.get("unit_id")
        if not unit_id:
            continue
        cost_per = _power_cost(p.get("cost", {}) or {})
        if cost_per <= 0:
            continue
        if _is_naval_unit(ud.get(unit_id)):
            naval_options.append(
                (
                    unit_id,
                    cost_per,
                    compute_purchase_score(p, None, None)
                    + naval_coast_pressure_extra,
                )
            )
        else:
            land_entries.append((unit_id, cost_per, p))

    # Allocate by need: fill slots one at a time, choosing best unit for each slot
    # (score + balance cheaper/expensive + diversity within batch). Amount = what fits slots and budget.
    purchases: dict[str, int] = {}
    spent = 0
    phantom_by_tid: dict[str, list[dict]] = {}
    phantom_power_by_tid: dict[str, float] = {}

    def choose_next_naval(
        options: list[tuple[str, int, float]],
        budget_left: int,
        batch_counts: dict[str, int],
    ) -> tuple[str | None, int]:
        batch_siegework = sum(
            c for uid, c in batch_counts.items() if _is_siegework_unit(ud, uid)
        )
        at_siegework_cap = (active_siegework + batch_siegework) >= MAX_ACTIVE_SIEGEWORK
        best_unit_id = None
        best_cost = 0
        best_effective = -1e9
        for unit_id, cost_per, score in options:
            if cost_per <= 0 or budget_left < cost_per:
                continue
            if at_siegework_cap and _is_siegework_unit(ud, unit_id):
                continue
            already = batch_counts.get(unit_id, 0)
            effective = score - PURCHASE_BATCH_DIVERSITY_PENALTY * already
            if best_unit_id is None or (effective, -cost_per) > (best_effective, -best_cost):
                best_effective = effective
                best_unit_id = unit_id
                best_cost = cost_per
        return (best_unit_id, best_cost)

    # Fill land slots: rescore each pick using phantom defenders stacked on highest-marginal tiles.
    for _ in range(land_remaining):
        budget_left = budget - spent
        if budget_left <= 0:
            break
        batch_siegework = sum(
            c for uid, c in purchases.items() if _is_siegework_unit(ud, uid)
        )
        at_siegework_cap = (active_siegework + batch_siegework) >= MAX_ACTIVE_SIEGEWORK
        best_unit_id = None
        best_cost = 0
        best_effective = -1e9
        for unit_id, cost_per, p in land_entries:
            if cost_per <= 0 or budget_left < cost_per:
                continue
            if at_siegework_cap and _is_siegework_unit(ud, unit_id):
                continue
            home_rem: int | None = None
            u_e = ud.get(unit_id)
            if u_e and has_unit_special(u_e, "home") and (getattr(u_e, "home_territory_ids", None) or []):
                open_slots = count_open_home_mobilization_slots_for_unit(
                    state, faction_id, unit_id, td, cd, port_d, ud
                )
                home_rem = open_slots - purchases.get(unit_id, 0)
            score = compute_purchase_score(
                p,
                phantom_by_tid or None,
                phantom_power_by_tid,
                home_rem=home_rem,
            )
            already = purchases.get(unit_id, 0)
            effective = score - PURCHASE_BATCH_DIVERSITY_PENALTY * already
            if best_unit_id is None or (effective, -cost_per) > (best_effective, -best_cost):
                best_effective = effective
                best_unit_id = unit_id
                best_cost = cost_per
        if not best_unit_id or best_cost <= 0:
            break
        purchases[best_unit_id] = purchases.get(best_unit_id, 0) + 1
        spent += best_cost
        pick_home_rem: int | None = None
        u_pick = ud.get(best_unit_id)
        if u_pick and has_unit_special(u_pick, "home") and (getattr(u_pick, "home_territory_ids", None) or []):
            open_s = count_open_home_mobilization_slots_for_unit(
                state, faction_id, best_unit_id, td, cd, port_d, ud
            )
            # This pick consumes one slot: remaining-before-pick = open_s - (purchases[uid]-1)
            pick_home_rem = open_s - (purchases.get(best_unit_id, 0) - 1)
        dest = best_phantom_destination(
            best_unit_id,
            phantom_by_tid,
            phantom_power_by_tid,
            home_rem=pick_home_rem,
        )
        if dest:
            prev = phantom_by_tid.get(dest) or []
            phantom_by_tid[dest] = merge_combat_move_attacker_stacks(
                prev, [{"unit_id": best_unit_id, "count": 1}]
            )
            pc = float(get_unit_power_cost(ud.get(best_unit_id)) or 0)
            phantom_power_by_tid[dest] = phantom_power_by_tid.get(dest, 0.0) + pc

    # Fill sea slots
    for _ in range(sea_remaining):
        budget_left = budget - spent
        if budget_left <= 0:
            break
        unit_id, cost_per = choose_next_naval(naval_options, budget_left, purchases)
        if not unit_id or cost_per <= 0:
            break
        purchases[unit_id] = purchases.get(unit_id, 0) + 1
        spent += cost_per

    if not purchases:
        return end_phase(faction_id)

    return purchase_units(faction_id, purchases)
