"""
Land garrison floors for threatened territories: same reach model as the engine.

Used by non_combat_move and combat_move so units are not peeled off a tile that enemies
can strike next combat_move, except when the move is a counterattack into enemy units on
the destination that themselves could reach the origin (threat relief).
"""

from backend.engine.movement import get_reachable_territories_for_unit
from backend.engine.definitions import faction_acts_as
from backend.engine.queries import _is_water_boat, get_unit_faction
from backend.engine.state import Unit

from backend.ai.formulas import get_unit_power_cost
from backend.ai.geography import (
    count_enemies_that_can_reach_territory_combat_move,
    effective_defensive_reinforce_pressure,
    is_frontline,
)


def move_attacks_enemy_stack_that_threatens_origin(
    from_tid: str,
    to_tid: str,
    state,
    faction_id: str,
    fd,
    td,
    ud,
) -> bool:
    """
    True if the destination has enemy land units that could reach from_tid on a combat_move
    from their current position (i.e. we are attacking part of the force that threatens the
    origin — legitimate to strip defenders for that attack).
    """
    terr_to = state.territories.get(to_tid)
    if not terr_to:
        return False
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    saved = getattr(state, "current_faction", None)
    try:
        for u in getattr(terr_to, "units", []) or []:
            uf = get_unit_faction(u, ud)
            if not uf or uf == faction_id:
                continue
            if fd.get(uf) and getattr(fd.get(uf), "alliance", "") == our_alliance:
                continue
            if _is_water_boat(ud.get(u.unit_id)):
                continue
            base_mov = getattr(u, "base_movement", 0) or 0
            if base_mov <= 0:
                continue
            unit_full = Unit(
                instance_id=u.instance_id,
                unit_id=u.unit_id,
                remaining_movement=base_mov,
                remaining_health=getattr(u, "remaining_health", 1),
                base_movement=base_mov,
                base_health=getattr(u, "base_health", 1),
                loaded_onto=getattr(u, "loaded_onto", None),
            )
            state.current_faction = uf
            reachable, _ = get_reachable_territories_for_unit(
                unit_full,
                to_tid,
                state,
                ud,
                td,
                fd,
                "combat_move",
            )
            if from_tid in (reachable or {}):
                return True
    finally:
        if saved is not None:
            state.current_faction = saved
    return False


def prune_move_unit_ids_for_garrison_floor(
    state,
    from_tid: str,
    unit_ids: list[str],
    faction_id: str,
    ud,
    td,
    fd,
    *,
    threat_from_count: int,
    threat_relief_attack: bool = False,
    effective_reach_count: int | None = None,
    projected_state=None,
) -> list[str]:
    """
    Drop land movers from unit_ids so at least required_remain friendly land units stay on
    from_tid. When threat_relief_attack is True, only one land unit must stay (counterattack
    into enemies on the destination that threaten this origin).

    effective_reach_count: optional precomputed reach count for from_tid (e.g. from a
    batched count_enemies_that_can_reach_territories map) so the full-board reach scan
    is not repeated per candidate; when None, computed as before.
    """
    # Pending declarations leave units physically at the origin until phase end.
    # Count the replayed board so consecutive moves cannot reuse those defenders.
    if projected_state is None and any(getattr(pm, "phase", None) == state.phase for pm in state.pending_moves or []):
        from backend.engine.reducer import get_state_after_pending_moves
        projected_state = get_state_after_pending_moves(state, state.phase, ud, td, fd)
    board = projected_state if projected_state is not None else state
    if not is_frontline(from_tid, board, faction_id, fd, td) and threat_from_count <= 0:
        return _objective_safe_movers(board, from_tid, list(unit_ids), faction_id, ud, td, fd, threat_relief_attack)
    terr = board.territories.get(from_tid)
    if not terr:
        return list(unit_ids)
    land_ids = []
    by_iid = {}
    for u in getattr(terr, "units", []) or []:
        if not faction_acts_as(fd, get_unit_faction(u, ud), faction_id):
            continue
        if _is_water_boat(ud.get(u.unit_id)):
            continue
        land_ids.append(u.instance_id)
        by_iid[u.instance_id] = u
    n_land = len(land_ids)
    if n_land == 0:
        return list(unit_ids)
    if n_land <= 1:
        return []
    if threat_relief_attack and (threat_from_count > 0 or is_frontline(from_tid, state, faction_id, fd, td)):
        required_remain = 1
    elif threat_from_count > 0:
        eff = effective_defensive_reinforce_pressure(
            from_tid, state, faction_id, fd, td, ud,
            precomputed_reach=effective_reach_count,
        )
        required_remain = min(n_land, max(1, min(threat_from_count, eff)))
    else:
        required_remain = 1
    moving_land = [i for i in unit_ids if i in land_ids]
    staying = n_land - len(moving_land)
    if staying >= required_remain:
        return _objective_safe_movers(board, from_tid, list(unit_ids), faction_id, ud, td, fd, threat_relief_attack)
    shortfall = required_remain - staying
    if shortfall <= 0:
        return list(unit_ids)
    movers_to_cancel = sorted(
        moving_land,
        key=lambda iid: get_unit_power_cost(ud.get(by_iid[iid].unit_id)) or 0,
        reverse=True,
    )[:shortfall]
    cancel_set = set(movers_to_cancel)
    return _objective_safe_movers(board, from_tid, [i for i in unit_ids if i not in cancel_set], faction_id, ud, td, fd, threat_relief_attack)


def _objective_safe_movers(board, origin, moving, faction_id, ud, td, fd, threat_relief):
    """Preserve stronghold/capital hold odds unless counterattacking the threat.

    If the territory is already below its defensive target, do not weaken it
    further. Actual post-combat threats are re-evaluated during noncombat moves.
    """
    important = getattr(td.get(origin), "is_stronghold", False) or any(getattr(f, "capital", None) == origin for f in fd.values())
    if not important or threat_relief:
        return moving
    from backend.ai.defense_sim import (
        defender_hold_probability_after_hypothetical_departure,
        defense_hold_saturation_threshold,
    )
    by_faction = {}
    def hold(ids):
        return defender_hold_probability_after_hypothetical_departure(
            board, origin, ids, faction_id, fd, td, ud, by_faction_cache=by_faction, n_trials=64)
    baseline = hold([])
    if baseline is None:
        return []  # Cannot establish that an objective departure is safe.
    target = min(baseline, defense_hold_saturation_threshold(origin, td, fd))
    units = {u.instance_id: u for u in board.territories[origin].units}
    result = list(moving)
    defenders_to_keep = sorted((iid for iid in moving if iid in units and not _is_water_boat(ud.get(units[iid].unit_id))),
                               key=lambda iid: (getattr(ud.get(units[iid].unit_id), "defense", 0),
                                                get_unit_power_cost(ud.get(units[iid].unit_id)) or 0), reverse=True)
    for iid in defenders_to_keep:
        probability = hold(result)
        if probability is not None and probability >= target:
            return result
        result.remove(iid)
    return result
