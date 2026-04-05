"""
Mobilization-phase policy: place purchased units into territories (camps, home, ports).
Prefer strongholds and high-power territories; prefer closer to enemy (fewer territories away);
spread units across zones instead of dumping all into one.

Adds sim-based proximity to territories with high expected defensive loss (see
defense_expected_loss_by_territory) on top of distance-to-enemy, stronghold scoring, and spread.
"""

from collections import deque

from backend.engine.actions import mobilize_units, end_phase, place_camp
from backend.engine.queries import (
    get_purchased_units,
    get_mobilization_capacity,
    valid_camp_placement_territory_ids,
)
from backend.engine.utils import faction_owns_capital
from backend.engine.queries import _is_naval_unit

from backend.ai.context import AIContext
from backend.ai.defense_sim import (
    defense_expected_loss_by_territory,
    defense_hold_saturation_threshold,
    defender_hold_probability_sim,
    is_faction_capital_territory,
    marginal_hold_delta_add_land_unit,
)
from backend.ai.geography import (
    get_faction_territory_blobs,
    blob_nearest_enemy_stronghold,
    count_enemies_that_can_reach_territory_combat_move,
    count_our_land_units_on_territory,
    effective_defensive_reinforce_pressure,
    is_frontline,
    min_distance_between_territories,
)
from backend.ai.formulas import territory_loss_cost
from backend.ai.habits import (
    FRONTLINE_BONUS,
    MOBILIZATION_DEFENSE_SIM_TRIALS,
    MOBILIZATION_FORWARD_SPLIT_MAX,
    MOBILIZATION_MARGINAL_HOLD_SCALE,
    MOBILIZATION_NEED_PROXIMITY_SCALE,
    MOBILIZATION_SPLIT_MIN_TOTAL_LAND,
    NON_COMBAT_STRONGHOLD_OVERSTACK_CUSHION,
    PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS,
)
from backend.ai.randomness import pick_from_score_band


def _is_sea_zone(territory_def) -> bool:
    return getattr(territory_def, "terrain_type", "").lower() == "sea"


def _remaining_land_capacity(state, territory_id: str, territory_info: dict, ud) -> int:
    """How many more land units can be mobilized to this territory this phase (camp/port only). Home use _can_mobilize_home_unit."""
    power = territory_info.get("power", 0)
    if territory_info.get("home_unit_capacity"):
        return 0  # Home handled separately
    already = sum(
        sum(u.get("count", 0) for u in pm.units if not _is_naval_unit(ud.get(u.get("unit_id"))))
        for pm in (state.pending_mobilizations or [])
        if pm.destination == territory_id
    )
    return max(0, power - already)


def _can_mobilize_home_unit(state, territory_id: str, territory_info: dict, unit_id: str) -> bool:
    """True if we can mobilize 1 of unit_id to this home territory (not yet pending)."""
    home_cap = territory_info.get("home_unit_capacity") or {}
    if unit_id not in home_cap:
        return False
    already = sum(
        u.get("count", 0)
        for pm in (state.pending_mobilizations or [])
        if pm.destination == territory_id
        for u in pm.units
        if u.get("unit_id") == unit_id
    )
    return already < 1


def _remaining_sea_capacity(state, faction_id: str, sea_zone_id: str, zone_info: dict, td, port_d, ud) -> int:
    """Approximate: remaining naval capacity for this sea zone (shared port pool)."""
    power = zone_info.get("power", 0)
    if power <= 0:
        return 0
    already = sum(
        sum(u.get("count", 0) for u in pm.units if _is_naval_unit(ud.get(u.get("unit_id"))))
        for pm in (state.pending_mobilizations or [])
        if pm.destination == sea_zone_id
    )
    return max(0, power - already)


def _enemy_territories_with_units(state, faction_id: str, fd, ud) -> set:
    """Territory IDs that are enemy alliance and have at least one unit (neutrals excluded)."""
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    out = set()
    for tid, terr in (state.territories or {}).items():
        units = getattr(terr, "units", []) or []
        if not units:
            continue
        owner = getattr(terr, "owner", None)
        if owner is None:
            continue  # neutral excluded
        owner_fd = fd.get(owner)
        if not owner_fd or getattr(owner_fd, "alliance", "") == our_alliance:
            continue
        out.add(tid)
    return out


def _distance_to_nearest_enemy_territory_with_units(
    start_territory_id: str, state, faction_id: str, fd, td, ud
) -> int:
    """
    Minimum number of territory steps (ground adjacency) from start to any enemy alliance
    territory that has units. Neutrals excluded. Returns 999 if none reachable.
    """
    targets = _enemy_territories_with_units(state, faction_id, fd, ud)
    if not targets:
        return 999
    if start_territory_id in targets:
        return 0
    visited = {start_territory_id}
    queue = deque([(start_territory_id, 0)])
    while queue:
        tid, dist = queue.popleft()
        tdef = td.get(tid)
        if not tdef:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            if adj_id in visited:
                continue
            visited.add(adj_id)
            if adj_id in targets:
                return dist + 1
            queue.append((adj_id, dist + 1))
    return 999


def _score_land_destination(
    territory_id: str,
    territory_info: dict,
    state,
    faction_id: str,
    fd,
    td,
    ud,
    pending_mobilizations: list,
    territory_to_stronghold_dist: dict | None = None,
    defense_hold_cache: dict[str, float | None] | None = None,
    strategic_land_bonus: float = 0.0,
    need_by_territory: dict[str, float] | None = None,
    marginal_land_unit_id: str | None = None,
    marginal_by_f_cache: dict[str, dict[str, list]] | None = None,
    hold_prob_cache: dict[tuple, float | None] | None = None,
) -> float:
    """
    Higher = prefer this. Stronghold + power, plus bonus for being closer to enemy
    (number of territories away from nearest enemy alliance territory with units; neutrals excluded),
    frontline and push-toward-stronghold (lines), minus spread penalty (already mobilizing here this phase).
    """
    tdef = td.get(territory_id)
    if not tdef:
        return 0.0
    power = territory_info.get("power", 0)
    is_stronghold = getattr(tdef, "is_stronghold", False)
    is_capital = is_faction_capital_territory(territory_id, fd)
    # Capitals are strongholds in our setups and top defensive priority — same mobilization sim path
    if is_stronghold or is_capital:
        eff = effective_defensive_reinforce_pressure(
            territory_id, state, faction_id, fd, td, ud
        )
        our_here = count_our_land_units_on_territory(
            state, territory_id, faction_id, ud
        )
        hp: float | None = None
        cache = defense_hold_cache if defense_hold_cache is not None else None
        if cache is not None:
            if territory_id not in cache:
                cache[territory_id] = defender_hold_probability_sim(
                    territory_id, state, faction_id, fd, td, ud
                )
            hp = cache.get(territory_id)
        thr = defense_hold_saturation_threshold(territory_id, td, fd)
        reach_next_combat = count_enemies_that_can_reach_territory_combat_move(
            territory_id, state, faction_id, fd, td, ud
        )
        # While any enemy can strike this tile next combat_move, keep treating it as a live
        # defensive sink — do not demote score for "saturation" (that steers buys toward the front).
        if reach_next_combat > 0:
            score = 100.0 + float(power)
        else:
            saturated_sim = hp is not None and hp >= thr
            saturated_pressure = (
                our_here >= eff + NON_COMBAT_STRONGHOLD_OVERSTACK_CUSHION
            )
            if saturated_sim or saturated_pressure:
                score = 18.0 + float(power)
            else:
                score = 100.0 + float(power)
    else:
        score = float(power)

    # Closer to enemy = higher score (use distance in territory steps)
    dist = _distance_to_nearest_enemy_territory_with_units(
        territory_id, state, faction_id, fd, td, ud
    )
    if dist < 999:
        score += (6 - min(dist, 6)) * 5.0  # 0 away -> +30, 1 -> +25, ..., 5 -> +5, 6+ -> 0

    # Lines: frontline bonus (our territory adjacent to enemy)
    if is_frontline(territory_id, state, faction_id, fd, td):
        score += FRONTLINE_BONUS
    # Push toward blob's nearest enemy stronghold: prefer mobilizing closer to that stronghold
    if territory_to_stronghold_dist:
        d = territory_to_stronghold_dist.get(territory_id)
        if d is not None and d < 999:
            score += (10 - min(d, 10)) * PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS

    # Spread penalty: prefer not to pile everything into one zone
    already = sum(
        sum(u.get("count", 0) for u in pm.units)
        for pm in (pending_mobilizations or [])
        if getattr(pm, "destination", None) == territory_id
    )
    score -= already * 8.0
    score += strategic_land_bonus

    if need_by_territory:
        prox = 0.0
        for tid_need, w in need_by_territory.items():
            if w <= 0:
                continue
            d = min_distance_between_territories(territory_id, tid_need, td)
            if d < 999:
                prox += float(w) / (1.0 + float(d))
        score += prox * MOBILIZATION_NEED_PROXIMITY_SCALE

    if marginal_land_unit_id and not _is_naval_unit(ud.get(marginal_land_unit_id)):
        tdef_m = td.get(territory_id)
        is_sh = bool(tdef_m and getattr(tdef_m, "is_stronghold", False))
        is_cap = is_faction_capital_territory(territory_id, fd)
        in_need = bool(need_by_territory and territory_id in need_by_territory)
        if (
            (is_sh or is_cap or in_need)
            and count_enemies_that_can_reach_territory_combat_move(
                territory_id, state, faction_id, fd, td, ud
            )
            > 0
        ):
            delta = marginal_hold_delta_add_land_unit(
                territory_id,
                state,
                faction_id,
                fd,
                td,
                ud,
                marginal_land_unit_id,
                by_faction_cache=marginal_by_f_cache,
                n_trials=MOBILIZATION_DEFENSE_SIM_TRIALS,
                hold_prob_cache=hold_prob_cache,
            )
            if delta > 0:
                score += (
                    delta
                    * territory_loss_cost(territory_id, td, fd)
                    * MOBILIZATION_MARGINAL_HOLD_SCALE
                )

    return score


def _queue_next_pending_camp_if_needed(
    state,
    faction_id: str,
    fd,
    td,
    ud,
    camp_defs,
    pending_mobilizations: list,
    territory_to_stronghold_dist: dict[str, int],
    land_bonus_map: dict[str, float],
    defense_hold_cache: dict[str, float | None] | None = None,
    need_by_territory: dict[str, float] | None = None,
    marginal_land_unit_id: str | None = None,
    marginal_by_f_cache: dict[str, dict[str, list]] | None = None,
    hold_prob_cache: dict[tuple, float | None] | None = None,
):
    """
    Purchased camps must be placed or queued before mobilization can end (engine rule).
    We use immediate place_camp (not queue_camp_placement) so two camps in one turn never
    deadlock on the same territory option (queue reserves a hex without a standing camp).
    """
    pending_camps = getattr(state, "pending_camps", []) or []
    pending_placements = getattr(state, "pending_camp_placements", []) or []
    queued_indices = {p.camp_index for p in pending_placements}
    for camp_index, entry in enumerate(pending_camps):
        if entry.get("placed_territory_id"):
            continue
        if camp_index in queued_indices:
            continue
        options = valid_camp_placement_territory_ids(
            state, faction_id, camp_index, camp_defs, td
        )
        if not options:
            continue
        candidates: list[tuple[tuple[int, str], float]] = []
        for tid in options:
            tdef = td.get(tid)
            power = 0
            if tdef and getattr(tdef, "produces", None) and isinstance(
                tdef.produces, dict
            ):
                power = int(tdef.produces.get("power", 0) or 0)
            t_info = {"territory_id": tid, "power": power}
            sc = _score_land_destination(
                tid,
                t_info,
                state,
                faction_id,
                fd,
                td,
                ud,
                pending_mobilizations,
                territory_to_stronghold_dist=territory_to_stronghold_dist,
                defense_hold_cache=defense_hold_cache,
                strategic_land_bonus=land_bonus_map.get(tid, 0.0),
                need_by_territory=need_by_territory,
                marginal_land_unit_id=marginal_land_unit_id,
                marginal_by_f_cache=marginal_by_f_cache,
                hold_prob_cache=hold_prob_cache,
            )
            candidates.append(((camp_index, tid), sc))
        best = pick_from_score_band(candidates) if candidates else None
        if best:
            ci, tid = best[0]
            # Immediate placement (not queue): queue_camp_placement reserves a territory
            # without a standing camp, so a second purchased camp with overlapping options
            # can never be queued—end_phase then fails validation. place_camp applies now
            # so later camps see standing camps and disjoint territory options correctly.
            return place_camp(faction_id, ci, tid)
    return None


def decide_mobilization(ctx: AIContext):
    """
    Return one mobilize_units action (one destination, one batch of units) or end_phase/end_turn.
    Prefer strongholds, then high-power territories; land and naval handled separately.
    """
    state = ctx.state
    faction_id = ctx.faction_id
    ud = ctx.unit_defs
    td = ctx.territory_defs
    cd = ctx.camp_defs
    port_d = ctx.port_defs

    pending_mob = state.pending_mobilizations or []
    defense_hold_cache: dict[str, float | None] = {}
    strategic = ctx.strategic
    land_bonus = (
        strategic.mobilization_land_bonus if strategic is not None else {}
    )
    naval_bonus = strategic.naval_sea_zone_bonus if strategic is not None else {}
    need_by_territory = defense_expected_loss_by_territory(
        state, faction_id, ctx.faction_defs, td, ud
    )
    purchased = get_purchased_units(state, faction_id)
    preview_land_uid: str | None = None
    for p in purchased or []:
        uid = p.get("unit_id")
        c = int(p.get("count", 0) or 0)
        if c > 0 and uid and not _is_naval_unit(ud.get(uid)):
            preview_land_uid = uid
            break
    mobilization_marginal_by_f_cache: dict[str, dict[str, list]] = {}
    mobilization_hold_prob_cache: dict[tuple, float | None] = {}

    territory_to_stronghold_dist: dict[str, int] = {}
    blobs = get_faction_territory_blobs(state, faction_id, td)
    for blob in blobs:
        nearest = blob_nearest_enemy_stronghold(
            blob, state, faction_id, ctx.faction_defs, td
        )
        if not nearest:
            continue
        sh_tid, _ = nearest
        for tid in blob:
            d = min_distance_between_territories(tid, sh_tid, td)
            territory_to_stronghold_dist[tid] = d

    camp_action = _queue_next_pending_camp_if_needed(
        state,
        faction_id,
        ctx.faction_defs,
        td,
        ud,
        cd,
        pending_mob,
        territory_to_stronghold_dist,
        land_bonus,
        defense_hold_cache=defense_hold_cache,
        need_by_territory=need_by_territory,
        marginal_land_unit_id=preview_land_uid,
        marginal_by_f_cache=mobilization_marginal_by_f_cache,
        hold_prob_cache=mobilization_hold_prob_cache,
    )
    if camp_action:
        return camp_action

    # Engine forbids mobilize_units without your capital; purchased pool may still have units
    # from purchase phase before the capital fell — do not emit invalid mobilize_units.
    if not faction_owns_capital(state, faction_id, ctx.faction_defs):
        return end_phase(faction_id)

    if not purchased:
        return end_phase(faction_id)

    capacity = get_mobilization_capacity(state, faction_id, td, cd, port_d, ud)
    territories = list(capacity.get("territories", []))
    port_territories = capacity.get("port_territories", [])
    for pt in port_territories:
        tid = pt.get("territory_id")
        if not tid:
            continue
        p_pow = int(pt.get("power", 0) or 0)
        home_cap = pt.get("home_unit_capacity") or {}
        # Must mirror purchase / count_open_home_mobilization_slots: port homes (e.g. corsair at
        # Umbar) often have land power 0 — still need home_unit_capacity on the working list.
        if p_pow <= 0 and not home_cap:
            continue
        row: dict = {"territory_id": tid, "power": p_pow}
        if home_cap:
            row["home_unit_capacity"] = dict(home_cap)
        territories.append(row)
    sea_zones = capacity.get("sea_zones", [])

    # Split purchased into land and naval
    land_stacks = [(p["unit_id"], p["count"]) for p in purchased if not _is_naval_unit(ud.get(p["unit_id"]))]
    naval_stacks = [(p["unit_id"], p["count"]) for p in purchased if _is_naval_unit(ud.get(p["unit_id"]))]

    # Try land: (1) camp/port destinations with power; (2) home destinations (1 unit type, count 1)
    if land_stacks:
        # 1) Home first: any purchased land unit that matches a home territory slot goes there
        #    before camp/port (so e.g. Wildmen use Dunland home instead of everything piling on capital).
        home_candidates: list[tuple[tuple[str, str], float]] = []  # ((territory_id, unit_id), score)
        for t_info in territories:
            tid = t_info.get("territory_id")
            if not tid:
                continue
            home_cap = t_info.get("home_unit_capacity") or {}
            if not home_cap:
                continue
            for unit_id, count in land_stacks:
                if count <= 0 or unit_id not in home_cap:
                    continue
                if not _can_mobilize_home_unit(state, tid, t_info, unit_id):
                    continue
                sc = _score_land_destination(
                    tid, t_info, state, faction_id, ctx.faction_defs, td, ud, pending_mob,
                    territory_to_stronghold_dist=territory_to_stronghold_dist,
                    defense_hold_cache=defense_hold_cache,
                    strategic_land_bonus=land_bonus.get(tid, 0.0),
                    need_by_territory=need_by_territory,
                    marginal_land_unit_id=unit_id,
                    marginal_by_f_cache=mobilization_marginal_by_f_cache,
                    hold_prob_cache=mobilization_hold_prob_cache,
                )
                home_candidates.append(((tid, unit_id), sc))
        if home_candidates:
            best_home = pick_from_score_band(home_candidates)
            if best_home:
                tid, unit_id = best_home
                return mobilize_units(
                    faction_id, tid, [{"unit_id": unit_id, "count": 1}]
                )

        # 2) Camp/port with power: score all valid destinations, then pick with band (vary and prefer frontline)
        candidates_land: list[tuple[tuple, float]] = []  # ((tid, rem, t_info), score)
        for t_info in territories:
            tid = t_info.get("territory_id")
            if not tid:
                continue
            if t_info.get("home_unit_capacity") and t_info.get("power", 0) == 0:
                continue  # Pure home: handle below
            rem = _remaining_land_capacity(state, tid, t_info, ud)
            if rem <= 0:
                continue
            sc = _score_land_destination(
                tid, t_info, state, faction_id, ctx.faction_defs, td, ud, pending_mob,
                territory_to_stronghold_dist=territory_to_stronghold_dist,
                defense_hold_cache=defense_hold_cache,
                strategic_land_bonus=land_bonus.get(tid, 0.0),
                need_by_territory=need_by_territory,
                marginal_land_unit_id=preview_land_uid,
                marginal_by_f_cache=mobilization_marginal_by_f_cache,
                hold_prob_cache=mobilization_hold_prob_cache,
            )
            candidates_land.append(((tid, rem, t_info), sc))

        fd_self = ctx.faction_defs.get(faction_id)
        capital_tid: str | None = getattr(fd_self, "capital", None) if fd_self else None

        total_land = sum(int(c or 0) for _, c in land_stacks)
        forward_entries = [
            c for c in candidates_land
            if capital_tid and c[0][0] != capital_tid
        ]
        capital_entries = [
            c for c in candidates_land
            if capital_tid and c[0][0] == capital_tid
        ]

        best_land = None
        rem_cap_limit: int | None = None
        # Never force all remaining land onto the capital just because we already mobilized forward
        # once — scored destinations (threat, strongholds, need map, spread penalty) must keep deciding.
        if (
            capital_tid
            and forward_entries
            and capital_entries
            and total_land >= MOBILIZATION_SPLIT_MIN_TOTAL_LAND
        ):
            bf = pick_from_score_band(forward_entries)
            if bf:
                best_land = bf
                _, rem_cap_f, _ = bf
                rem_cap_limit = min(
                    rem_cap_f,
                    max(
                        1,
                        min(
                            MOBILIZATION_FORWARD_SPLIT_MAX,
                            total_land - 1,
                        ),
                    ),
                )
        if best_land is None:
            best_land = pick_from_score_band(candidates_land) if candidates_land else None
            rem_cap_limit = None

        if best_land:
            dest_id, rem_cap, t_info = best_land
            if rem_cap_limit is not None:
                rem_cap = rem_cap_limit
            unit_stacks = []
            for unit_id, count in land_stacks:
                if rem_cap <= 0:
                    break
                take = min(count, rem_cap)
                if take <= 0:
                    continue
                unit_stacks.append({"unit_id": unit_id, "count": take})
                rem_cap -= take
            if unit_stacks:
                return mobilize_units(faction_id, dest_id, unit_stacks)

    # Try naval: pick sea zone by strategic pressure (enemy coast) then capacity
    if naval_stacks:
        naval_cands: list[tuple[tuple[str, dict, int], float]] = []
        for z_info in sea_zones:
            zid = z_info.get("sea_zone_id")
            if not zid:
                continue
            rem = _remaining_sea_capacity(state, faction_id, zid, z_info, td, port_d, ud)
            if rem <= 0:
                continue
            bon = naval_bonus.get(zid, 0.0) + 0.15 * float(rem)
            naval_cands.append(((zid, z_info, rem), bon))
        best_naval = pick_from_score_band(naval_cands) if naval_cands else None
        if best_naval:
            zid, z_info, rem = best_naval[0]
            unit_stacks = []
            for unit_id, count in naval_stacks:
                if rem <= 0:
                    break
                take = min(count, rem)
                if take <= 0:
                    continue
                unit_stacks.append({"unit_id": unit_id, "count": take})
                rem -= take
            if unit_stacks:
                return mobilize_units(faction_id, zid, unit_stacks)

    return end_phase(faction_id)
