"""
Land garrison floors for threatened territories: same reach model as the engine.

Used by non_combat_move and combat_move so units are not peeled off a tile that enemies
can strike next combat_move, except when the move is a counterattack into enemy units on
the destination that themselves could reach the origin (threat relief).
"""

from backend.engine.movement import get_reachable_territories_for_unit
from backend.engine.queries import _is_naval_unit, get_unit_faction
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
            if _is_naval_unit(ud.get(u.unit_id)):
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
) -> list[str]:
    """
    Drop land movers from unit_ids so at least required_remain friendly land units stay on
    from_tid. When threat_relief_attack is True, only one land unit must stay (counterattack
    into enemies on the destination that threaten this origin).
    """
    if not is_frontline(from_tid, state, faction_id, fd, td) and threat_from_count <= 0:
        return list(unit_ids)
    terr = state.territories.get(from_tid)
    if not terr:
        return list(unit_ids)
    land_ids = []
    by_iid = {}
    for u in getattr(terr, "units", []) or []:
        if get_unit_faction(u, ud) != faction_id:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
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
            from_tid, state, faction_id, fd, td, ud
        )
        required_remain = min(n_land, max(1, min(threat_from_count, eff)))
    else:
        required_remain = 1
    moving_land = [i for i in unit_ids if i in land_ids]
    staying = n_land - len(moving_land)
    if staying >= required_remain:
        return list(unit_ids)
    shortfall = required_remain - staying
    if shortfall <= 0:
        return list(unit_ids)
    movers_to_cancel = sorted(
        moving_land,
        key=lambda iid: get_unit_power_cost(ud.get(by_iid[iid].unit_id)) or 0,
        reverse=True,
    )[:shortfall]
    cancel_set = set(movers_to_cancel)
    return [i for i in unit_ids if i not in cancel_set]
