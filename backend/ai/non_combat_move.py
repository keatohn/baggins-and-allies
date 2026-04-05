"""
Non-combat move phase: set up for next turn (attack and defense).
Move units to where they'll be most useful next turn—reinforce threatened territories
or position for attack—or leave them in place. Similar in spirit to purchase/mobilization
but we're moving existing units, not buying/placing.
"""

import math
from collections import defaultdict

from backend.engine.actions import move_units, end_phase
from backend.engine.movement import _is_sea_zone
from backend.engine.queries import (
    get_movable_units,
    get_unit_move_targets,
    get_unit_faction,
    _is_naval_unit,
    filter_unit_instances_that_can_reach,
)

from backend.ai.context import AIContext
from backend.ai.geography import (
    adjacent_enemy_land_unit_count,
    territory_to_blob_index,
    is_frontline,
    min_distance_to_enemy_territory,
    count_enemies_that_can_reach_territory_combat_move,
    effective_defensive_reinforce_pressure,
)
from backend.ai.defense_sim import (
    count_coalition_land_units_on_territory,
    defense_hold_saturation_threshold,
    defender_hold_probability_after_hypothetical_departure,
    defender_hold_probability_sim,
    defender_marginal_hold_metrics,
    is_faction_capital_territory,
    reinforce_value_from_marginal_defense_sim,
)
from backend.ai.garrison import prune_move_unit_ids_for_garrison_floor
from backend.ai.formulas import territory_reinforce_base_score, get_unit_power_cost
from backend.ai.habits import (
    AI_ELITE_UNIT_MIN_POWER_COST,
    DEFEND_VS_ATTACK_WEIGHT,
    FRONTLINE_BONUS,
    PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS,
    NON_COMBAT_UNNECESSARY_RETREAT_PENALTY,
    NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON,
    NON_COMBAT_DEFENSE_MARGINAL_HOLD_SCORE_SCALE,
    NON_COMBAT_STRONGHOLD_OVERSTACK_CUSHION,
    NON_COMBAT_STRONGHOLD_OVERSTACK_MOVE_PENALTY,
    NON_COMBAT_CRISIS_W_DEF,
    NON_COMBAT_STRONGHOLD_CRISIS_REINFORCE_BONUS,
    NON_COMBAT_CRISIS_PUSH_TOWARD_ENEMY_MULT,
    NON_COMBAT_ALLY_REINFORCE_BONUS_PER_THREAT,
    NON_COMBAT_ALLY_REINFORCE_THREAT_CAP,
    COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY,
    NON_COMBAT_ELITE_UNDERMANNED_NEUTRAL_MULT,
    NON_COMBAT_EMPTY_NEUTRAL_STAGING_BONUS,
    NON_COMBAT_SEA_OFFLOAD_ALLIED_BEACH_PENALTY,
)
from backend.ai.randomness import pick_from_score_band


def _owner_is_allied_ally(
    owner_id: str | None, faction_id: str, fd
) -> bool:
    """True if owner is another faction in our alliance (not us)."""
    if not owner_id or owner_id == faction_id:
        return False
    our_fd = fd.get(faction_id)
    other_fd = fd.get(owner_id)
    if not our_fd or not other_fd:
        return False
    return getattr(other_fd, "alliance", "") == getattr(our_fd, "alliance", "")


def _is_friendly_or_allied_territory(state, territory_id: str, faction_id: str, fd) -> bool:
    """True if we can move here in non_combat_move (friendly, allied, or neutral)."""
    terr = state.territories.get(territory_id)
    if not terr:
        return False
    owner = getattr(terr, "owner", None)
    if not owner:
        return True  # Neutral
    if owner == faction_id:
        return True
    other_fd = fd.get(owner)
    our_fd = fd.get(faction_id)
    if not other_fd or not our_fd:
        return False
    return getattr(other_fd, "alliance", "") == getattr(our_fd, "alliance", "")


def _count_adjacent_enemies(territory_id: str, state, faction_id: str, fd, td, ud) -> int:
    """Number of adjacent territories that contain enemy units or are enemy-owned."""
    tdef = td.get(territory_id)
    if not tdef:
        return 0
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    count = 0
    for adj_id in getattr(tdef, "adjacent", []) or []:
        adj = state.territories.get(adj_id)
        if not adj:
            continue
        owner = getattr(adj, "owner", None)
        if owner == faction_id:
            continue
        if owner and fd.get(owner) and getattr(fd.get(owner), "alliance", "") == our_alliance:
            continue
        for u in getattr(adj, "units", []) or []:
            uf = get_unit_faction(u, ud)
            if uf and uf != faction_id and fd.get(uf) and getattr(fd.get(uf), "alliance", "") != our_alliance:
                count += 1
                break
        else:
            if owner and owner != faction_id and fd.get(owner) and getattr(fd.get(owner), "alliance", "") != our_alliance:
                count += 1
    return count


def _count_our_land_units(state, territory_id: str, faction_id: str, ud) -> int:
    terr = state.territories.get(territory_id)
    if not terr:
        return 0
    n = 0
    for u in getattr(terr, "units", []) or []:
        if get_unit_faction(u, ud) != faction_id:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
            continue
        n += 1
    return n


def _unit_is_elite(ud, unit_id: str) -> bool:
    """Elite for AI sacrifice heuristics: recruitment power cost >= AI_ELITE_UNIT_MIN_POWER_COST."""
    pc = get_unit_power_cost(ud.get(unit_id)) or 0
    return pc >= AI_ELITE_UNIT_MIN_POWER_COST


def _elite_into_undermanned_threat_penalty(
    state,
    to_tid: str,
    unit_ids: list[str],
    faction_id: str,
    from_tid: str,
    fd,
    td,
    ud,
) -> float:
    """
    Do not commit expensive units to a hex that would still be badly outnumbered vs every enemy
    that can reach it next turn—unless this move (all land units together) brings the garrison up
    to at least that reach count (parity / good enough odds). Then elites are allowed.
    """
    threat = count_enemies_that_can_reach_territory_combat_move(
        to_tid, state, faction_id, fd, td, ud
    )
    if threat <= 0:
        return 0.0
    garrison_to = count_coalition_land_units_on_territory(
        state, to_tid, faction_id, fd, ud
    )
    moving_land = 0
    moving_elite_pc_sum = 0.0
    terr_f = state.territories.get(from_tid)
    ids_set = set(unit_ids)
    if terr_f:
        for u in getattr(terr_f, "units", []) or []:
            if u.instance_id not in ids_set:
                continue
            if get_unit_faction(u, ud) != faction_id:
                continue
            if _is_naval_unit(ud.get(u.unit_id)):
                continue
            moving_land += 1
            if _unit_is_elite(ud, u.unit_id):
                moving_elite_pc_sum += float(get_unit_power_cost(ud.get(u.unit_id)) or 0)
    after_total = garrison_to + moving_land
    if after_total >= threat:
        return 0.0
    if moving_elite_pc_sum <= 0.0:
        return 0.0
    gap = float(threat - after_total)
    return COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY * gap * (
        moving_elite_pc_sum / max(1.0, float(threat))
    )


def _threatened_high_value_territories(
    state,
    faction_id: str,
    fd,
    td,
    ud,
) -> set[str]:
    """
    Strongholds and capitals we own that enemies can reach next combat_move and that are not
    safely held (sim below saturation threshold or fewer land defenders than effective pressure).
    """
    out: set[str] = set()
    for tid, terr in (state.territories or {}).items():
        if getattr(terr, "owner", None) != faction_id:
            continue
        tdef = td.get(tid)
        if not tdef:
            continue
        if not getattr(tdef, "is_stronghold", False) and not is_faction_capital_territory(
            tid, fd
        ):
            continue
        if count_enemies_that_can_reach_territory_combat_move(
            tid, state, faction_id, fd, td, ud
        ) <= 0:
            continue
        thr = defense_hold_saturation_threshold(tid, td, fd)
        p = defender_hold_probability_sim(tid, state, faction_id, fd, td, ud)
        if p is not None and p < thr:
            out.add(tid)
            continue
        eff = effective_defensive_reinforce_pressure(
            tid, state, faction_id, fd, td, ud
        )
        if _count_our_land_units(state, tid, faction_id, ud) < eff:
            out.add(tid)
    return out


def _empty_ownable_neutral_no_enemy_units(
    state, territory_id: str, faction_id: str, fd, td, ud
) -> bool:
    """Unowned ownable tile with no enemy units present (OK to stage expensive pieces forward)."""
    terr = state.territories.get(territory_id)
    tdef = td.get(territory_id)
    if not terr or not tdef:
        return False
    if getattr(terr, "owner", None) is not None:
        return False
    if not getattr(tdef, "ownable", True):
        return False
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    for u in getattr(terr, "units", []) or []:
        uf = get_unit_faction(u, ud)
        if not uf:
            continue
        o_fd = fd.get(uf)
        if o_fd and getattr(o_fd, "alliance", "") != our_alliance:
            return False
    return True


def _forward_pressure_vs_adjacent_enemies(
    territory_id: str, state, faction_id: str, fd, td, ud
) -> float:
    """Score for holding this hex next turn: adjacent enemy presence + high-value enemy tiles."""
    tdef = td.get(territory_id)
    if not tdef:
        return 0.0
    adj_enemies = _count_adjacent_enemies(territory_id, state, faction_id, fd, td, ud)
    if adj_enemies == 0:
        return 0.0
    base = 1.0 + adj_enemies
    for adj_id in getattr(tdef, "adjacent", []) or []:
        adj_def = td.get(adj_id)
        if not adj_def:
            continue
        if getattr(adj_def, "is_stronghold", False):
            base += 2.0
        for f in (fd or {}).values():
            if getattr(f, "capital", None) == adj_id:
                base += 1.5
                break
    return base


def _attack_setup_value(territory_id: str, state, faction_id: str, fd, td, ud) -> float:
    """
    Higher = better forward position for next turn.
    Counts pressure from *this* hex: our owned tiles, or empty ownable neutrals we can
    step onto in non_combat (otherwise neutrals always scored 0 attack value and only tiny
    defensive sim — armies would not walk into open map toward the enemy).
    Empty neutrals with no adjacent enemy yet still get a positive score from graph distance
    to enemy land so they beat shuffling inland when non_combat pairs are not evaluated.
    """
    tdef = td.get(territory_id)
    if not tdef:
        return 0.0
    terr = state.territories.get(territory_id)
    owner = getattr(terr, "owner", None) if terr else None
    if owner == faction_id:
        return _forward_pressure_vs_adjacent_enemies(
            territory_id, state, faction_id, fd, td, ud
        )
    if owner is None and getattr(tdef, "ownable", True):
        if _empty_ownable_neutral_no_enemy_units(
            state, territory_id, faction_id, fd, td, ud
        ):
            adj_pressure = _forward_pressure_vs_adjacent_enemies(
                territory_id, state, faction_id, fd, td, ud
            )
            if adj_pressure > 0.0:
                return adj_pressure
            d_en = min_distance_to_enemy_territory(
                territory_id, state, faction_id, fd, td
            )
            if d_en < 999:
                return 1.0 / float(max(1, d_en))
    return 0.0


def _enemy_proximity_delta(
    from_tid: str,
    to_tid: str,
    state,
    faction_id: str,
    fd,
    td,
) -> float:
    """
    Steps closer (+) or farther (-) from nearest enemy-owned land when moving from -> to.
    Uses the same BFS as combat pressure: one objective axis per map, not "nearest stronghold"
    (which can point the wrong way on multi-front lines).
    """
    d_from = min_distance_to_enemy_territory(from_tid, state, faction_id, fd, td)
    d_to = min_distance_to_enemy_territory(to_tid, state, faction_id, fd, td)
    if d_from >= 999 or d_to >= 999:
        return 0.0
    return float(d_from - d_to)


def _n_land_movers(
    state,
    from_tid: str,
    unit_ids: list[str],
    faction_id: str,
    ud,
) -> int:
    terr = state.territories.get(from_tid)
    if not terr:
        return 0
    want = set(unit_ids)
    n = 0
    for u in getattr(terr, "units", []) or []:
        if u.instance_id not in want:
            continue
        if get_unit_faction(u, ud) != faction_id:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
            continue
        n += 1
    return n


def decide_non_combat_move(ctx: AIContext):
    """
    One move that best sets up for next turn: reinforce (defense) or forward position (attack).
    Score = DEFEND_VS_ATTACK_WEIGHT * reinforce_value(to) + (1 - DEFEND_VS_ATTACK_WEIGHT) * attack_setup_value(to).
    """
    state = ctx.state
    faction_id = ctx.faction_id
    ud = ctx.unit_defs
    td = ctx.territory_defs
    fd = ctx.faction_defs

    movable = get_movable_units(state, faction_id, ud)
    pending_unit_ids = set()
    for pm in (state.pending_moves or []):
        if getattr(pm, "phase", None) == "non_combat_move":
            pending_unit_ids.update(getattr(pm, "unit_instance_ids", []) or [])

    # Candidates per unit instance: only (from, to) where this unit can reach to (uses remaining_movement).
    from_to_units: dict[tuple[str, str], list[str]] = defaultdict(list)
    for unit_info in movable:
        iid = unit_info.get("instance_id")
        if not iid or iid in pending_unit_ids:
            continue
        from_tid = unit_info.get("territory_id")
        targets, _ = get_unit_move_targets(state, iid, ud, td, fd)  # per-unit reachable set
        for to_tid in (targets or {}).keys():
            if not to_tid or to_tid == from_tid:
                continue
            if not _is_friendly_or_allied_territory(state, to_tid, faction_id, fd):
                continue
            from_to_units[(from_tid, to_tid)].append(iid)

    crisis_reinforce = _threatened_high_value_territories(
        state, faction_id, fd, td, ud
    )
    strategic = ctx.strategic
    w_def = max(DEFEND_VS_ATTACK_WEIGHT, NON_COMBAT_CRISIS_W_DEF) if crisis_reinforce else DEFEND_VS_ATTACK_WEIGHT
    if strategic is not None:
        sdp = strategic.purchase_defense_priority
        w_def = w_def * (1.0 - 0.35 * sdp) + 0.55 * sdp
        w_def = min(0.92, max(0.18, w_def))
    w_att = 1.0 - w_def
    tid_to_blob = territory_to_blob_index(state, faction_id, td)
    candidates: list[tuple[tuple, float]] = []  # ((from_tid, to_tid, unit_ids), score)
    reach_by_to_cache: dict[str, dict[str, list]] = {}
    p_before_cache: dict[str, float] = {}
    reach_from_cache: dict[str, int] = {}

    for (from_tid, to_tid), unit_ids in from_to_units.items():
        if not unit_ids:
            continue
        if from_tid not in reach_from_cache:
            reach_from_cache[from_tid] = count_enemies_that_can_reach_territory_combat_move(
                from_tid, state, faction_id, fd, td, ud
            )
        threat_from = reach_from_cache[from_tid]
        unit_ids = prune_move_unit_ids_for_garrison_floor(
            state,
            from_tid,
            unit_ids,
            faction_id,
            ud,
            td,
            fd,
            threat_from_count=threat_from,
            threat_relief_attack=False,
        )
        if not unit_ids:
            continue
        terr_owner = state.territories.get(to_tid)
        owner_to = getattr(terr_owner, "owner", None) if terr_owner else None
        threat_to_dest = count_enemies_that_can_reach_territory_combat_move(
            to_tid, state, faction_id, fd, td, ud
        )
        # Do not burn non_combat shuffling units onto allied backfield: no local heat and no
        # adjacent enemy land — the reach-count model can still show "pressure" far from reality.
        if _owner_is_allied_ally(owner_to, faction_id, fd):
            if threat_to_dest == 0 and adjacent_enemy_land_unit_count(
                to_tid, state, faction_id, fd, td, ud
            ) == 0:
                continue
        pb, _pa, mg = defender_marginal_hold_metrics(
            state,
            from_tid,
            to_tid,
            list(unit_ids),
            faction_id,
            fd,
            td,
            ud,
            by_faction_cache=reach_by_to_cache,
            p_before_cache=p_before_cache,
        )
        base = territory_reinforce_base_score(to_tid, td, fd)
        rev = reinforce_value_from_marginal_defense_sim(
            base, pb, mg, to_tid, td, fd
        )
        asv = _attack_setup_value(to_tid, state, faction_id, fd, td, ud)
        score = w_def * rev + w_att * asv
        if strategic is not None:
            score += strategic.non_combat_reinforce_bonus_to.get(to_tid, 0.0)
        if crisis_reinforce and to_tid in crisis_reinforce:
            score += NON_COMBAT_STRONGHOLD_CRISIS_REINFORCE_BONUS
        if _owner_is_allied_ally(owner_to, faction_id, fd):
            if threat_to_dest > 0:
                score += NON_COMBAT_ALLY_REINFORCE_BONUS_PER_THREAT * min(
                    float(threat_to_dest),
                    float(NON_COMBAT_ALLY_REINFORCE_THREAT_CAP),
                )
        elite_pen = _elite_into_undermanned_threat_penalty(
            state, to_tid, unit_ids, faction_id, from_tid, fd, td, ud
        )
        if _empty_ownable_neutral_no_enemy_units(state, to_tid, faction_id, fd, td, ud):
            elite_pen *= NON_COMBAT_ELITE_UNDERMANNED_NEUTRAL_MULT
            score += NON_COMBAT_EMPTY_NEUTRAL_STAGING_BONUS
        score -= elite_pen
        if threat_from > 0:
            p_origin_after = defender_hold_probability_after_hypothetical_departure(
                state,
                from_tid,
                list(unit_ids),
                faction_id,
                fd,
                td,
                ud,
                by_faction_cache=reach_by_to_cache,
            )
            thr_origin = defense_hold_saturation_threshold(from_tid, td, fd)
            if p_origin_after is not None and p_origin_after < thr_origin:
                score -= (
                    (thr_origin - p_origin_after)
                    * float(NON_COMBAT_DEFENSE_MARGINAL_HOLD_SCORE_SCALE)
                )
        eff_t = effective_defensive_reinforce_pressure(
            to_tid, state, faction_id, fd, td, ud
        )
        defenders_to = count_coalition_land_units_on_territory(
            state, to_tid, faction_id, fd, ud
        )
        tdef_to = td.get(to_tid)
        thr = defense_hold_saturation_threshold(to_tid, td, fd)
        defense_saturated = pb is not None and (
            pb >= thr
            or (mg is not None and mg <= NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON)
        )
        high_value_tile = bool(
            tdef_to
            and (
                getattr(tdef_to, "is_stronghold", False)
                or is_faction_capital_territory(to_tid, fd)
            )
        )
        stronghold_overstack = bool(
            high_value_tile
            and (
                defense_saturated
                or defenders_to >= eff_t + NON_COMBAT_STRONGHOLD_OVERSTACK_CUSHION
            )
        )
        # Frontline: prefer threatened borders — but not piling onto an already-safe stronghold
        if stronghold_overstack:
            score -= NON_COMBAT_STRONGHOLD_OVERSTACK_MOVE_PENALTY
        elif is_frontline(to_tid, state, faction_id, fd, td):
            score += FRONTLINE_BONUS
        # Advance toward enemy land (graph distance), not toward a single arbitrary stronghold.
        blob_ix = tid_to_blob.get(from_tid)
        if blob_ix is None:
            blob_ix = tid_to_blob.get(to_tid)
        prox_delta = _enemy_proximity_delta(
            from_tid, to_tid, state, faction_id, fd, td
        )
        if prox_delta != 0.0 and not stronghold_overstack:
            step = prox_delta * PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS
            if prox_delta > 0:
                if crisis_reinforce and to_tid not in crisis_reinforce:
                    step *= NON_COMBAT_CRISIS_PUSH_TOWARD_ENEMY_MULT
                if strategic is not None and blob_ix is not None:
                    step *= strategic.non_combat_push_mult_by_blob.get(blob_ix, 1.0)
            elif prox_delta < 0:
                # Rearward move: hurt more when marching the whole stack away from contact.
                legitimate_rear = (
                    to_tid in crisis_reinforce
                    or threat_to_dest > 0
                    or (
                        owner_to == faction_id
                        and pb is not None
                        and mg is not None
                        and pb < thr
                        and mg > NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON
                    )
                )
                if not legitimate_rear:
                    step *= math.sqrt(float(max(1, _n_land_movers(
                        state, from_tid, unit_ids, faction_id, ud
                    ))))
            score += step
        # Do not shuffle mobile units to the rear when the origin is not threatened (wastes next-turn attack reach).
        asv_from = _attack_setup_value(from_tid, state, faction_id, fd, td, ud)
        asv_to = _attack_setup_value(to_tid, state, faction_id, fd, td, ud)
        if (
            threat_from == 0
            and asv_from > asv_to + 0.25
        ):
            score -= NON_COMBAT_UNNECESSARY_RETREAT_PENALTY
        from_def = td.get(from_tid)
        to_def = td.get(to_tid)
        if (
            from_def
            and to_def
            and _is_sea_zone(from_def)
            and not _is_sea_zone(to_def)
            and owner_to
            and owner_to != faction_id
            and _owner_is_allied_ally(owner_to, faction_id, fd)
            and not (crisis_reinforce and to_tid in crisis_reinforce)
        ):
            score -= NON_COMBAT_SEA_OFFLOAD_ALLIED_BEACH_PENALTY
        if score > 0:
            candidates.append(((from_tid, to_tid, list(unit_ids)), score))

    best_move = pick_from_score_band(candidates) if candidates else None

    if not best_move:
        return end_phase(faction_id)
    from_tid, to_tid, unit_ids = best_move
    # Only include unit instances that can reach to_tid (per remaining_movement)
    unit_ids = filter_unit_instances_that_can_reach(
        state, to_tid, unit_ids, ud, td, fd
    )
    if not unit_ids:
        return end_phase(faction_id)
    # Sea -> land (offload): only land units may move; naval units cannot go on land (backend rule)
    from_def = td.get(from_tid)
    to_def = td.get(to_tid)
    if from_def and to_def and _is_sea_zone(from_def) and not _is_sea_zone(to_def):
        terr = state.territories.get(from_tid)
        ids_set = set(unit_ids)
        unit_ids = [
            u.instance_id for u in (getattr(terr, "units", []) or [])
            if u.instance_id in ids_set and not _is_naval_unit(ud.get(u.unit_id))
        ]
        if not unit_ids:
            return end_phase(faction_id)
    return move_units(faction_id, from_tid, to_tid, unit_ids)
