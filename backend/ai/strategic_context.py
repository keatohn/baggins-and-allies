"""
Per-turn strategic snapshot for the AI: blob objectives, territory pressure, and cross-phase
weights so purchase / mobilization / non_combat / combat_move share one situational model.

Built fresh from current state each time decide() runs (phases mutate state).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.queries import get_mobilization_capacity, _is_naval_unit
from backend.engine.queries import get_unit_faction
from backend.engine.movement import _is_sea_zone

from backend.ai.context import AIContext
from backend.ai.defense_sim import is_faction_capital_territory
from backend.ai.habits import (
    STRATEGIC_ALLY_COMBAT_MOVE_ADJACENT_SCALE,
    STRATEGIC_ALLY_COMBAT_MOVE_PRESSURE_FLOOR,
    STRATEGIC_ALLY_NON_COMBAT_LAND_MULT,
    STRATEGIC_ALLY_NON_COMBAT_MIN_PRESSURE,
    STRATEGIC_ALLY_NON_COMBAT_SH_MULT,
    STRATEGIC_ALLY_PURCHASE_DEFENSE_PRIORITY_MULT,
)
from backend.ai.geography import (
    adjacent_enemy_land_unit_count,
    blob_nearest_enemy_stronghold,
    count_enemies_that_can_reach_territory_combat_move,
    get_faction_territory_blobs,
    is_frontline,
    min_distance_between_territories,
    territory_to_blob_index,
)


@dataclass
class StrategicTurnContext:
    """Unified situational awareness for one decision tick."""

    faction_id: str
    blobs: list[set[str]]
    territory_blob: dict[str, int]
    blob_mode: dict[int, str]
    territory_pressure: dict[str, float]
    mobilization_land_bonus: dict[str, float]
    combat_attack_mult_by_blob: dict[int, float]
    combat_strip_penalty_from: dict[str, float]
    non_combat_reinforce_bonus_to: dict[str, float]
    non_combat_push_mult_by_blob: dict[int, float]
    combat_move_bonus_to: dict[str, float]
    purchase_defense_priority: float
    naval_sea_zone_bonus: dict[str, float]


def _pressure_for_territory(
    tid: str,
    state,
    faction_id: str,
    fd,
    td,
    ud,
) -> float:
    r = float(
        count_enemies_that_can_reach_territory_combat_move(
            tid, state, faction_id, fd, td, ud
        )
    )
    adj = float(
        adjacent_enemy_land_unit_count(tid, state, faction_id, fd, td, ud)
    )
    return min(1.0, r / 14.0 + adj / 7.0)


def _naval_sea_zone_bonuses(
    state,
    faction_id: str,
    td,
    fd,
    ud,
    port_d,
    capacity_sea_zones: list,
) -> dict[str, float]:
    """Higher score for sea zones adjacent to enemy coast with units (raid / fleet pressure)."""
    out: dict[str, float] = {}
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    for z in capacity_sea_zones or []:
        zid = z.get("sea_zone_id")
        if not zid:
            continue
        zdef = td.get(zid)
        if not zdef:
            continue
        enemy_presence = 0
        for adj_id in getattr(zdef, "adjacent", []) or []:
            if adj_id == zid:
                continue
            adj_def = td.get(adj_id)
            if adj_def and getattr(adj_def, "terrain_type", "").lower() == "sea":
                continue
            terr = state.territories.get(adj_id)
            if not terr:
                continue
            owner = getattr(terr, "owner", None)
            if not owner or owner == faction_id:
                continue
            ofd = fd.get(owner)
            if ofd and getattr(ofd, "alliance", "") == our_alliance:
                continue
            for u in getattr(terr, "units", []) or []:
                uf = get_unit_faction(u, ud)
                if uf and not _is_naval_unit(ud.get(u.unit_id)):
                    enemy_presence += 1
                    break
        if enemy_presence > 0:
            out[zid] = 8.0 + min(22.0, float(enemy_presence) * 5.0)
    return out


def build_strategic_turn_context(ctx: AIContext) -> StrategicTurnContext:
    state = ctx.state
    faction_id = ctx.faction_id
    ud = ctx.unit_defs
    td = ctx.territory_defs
    fd = ctx.faction_defs
    cd = ctx.camp_defs
    port_d = ctx.port_defs

    blobs = get_faction_territory_blobs(state, faction_id, td)
    territory_blob = territory_to_blob_index(state, faction_id, td)

    our_land = {
        tid
        for tid, terr in (state.territories or {}).items()
        if getattr(terr, "owner", None) == faction_id
    }
    territory_pressure: dict[str, float] = {}
    for tid in our_land:
        territory_pressure[tid] = _pressure_for_territory(
            tid, state, faction_id, fd, td, ud
        )

    blob_mode: dict[int, str] = {}
    for bi, blob in enumerate(blobs):
        if not blob:
            blob_mode[bi] = "consolidate"
            continue
        max_p = max((territory_pressure.get(t, 0.0) for t in blob), default=0.0)
        capital_here = any(
            is_faction_capital_territory(t, fd) for t in blob
        )
        front = any(
            is_frontline(t, state, faction_id, fd, td) for t in blob
        )
        nearest = blob_nearest_enemy_stronghold(blob, state, faction_id, fd, td)
        min_d_sh = 999
        if nearest:
            sh_tid, _ = nearest
            min_d_sh = min(
                min_distance_between_territories(t, sh_tid, td) for t in blob
            )
        if (capital_here and max_p > 0.12) or (front and max_p > 0.1):
            blob_mode[bi] = "defend"
        elif nearest is not None and min_d_sh < 999 and min_d_sh <= 6:
            blob_mode[bi] = "push"
        else:
            blob_mode[bi] = "consolidate"

    mobilization_land_bonus: dict[str, float] = {}
    for tid in our_land:
        bi = territory_blob.get(tid, -1)
        p = territory_pressure.get(tid, 0.0)
        bonus = 38.0 * p
        mode = blob_mode.get(bi, "consolidate") if bi >= 0 else "consolidate"
        if mode == "defend" and is_frontline(tid, state, faction_id, fd, td):
            bonus += 22.0
        if mode == "push" and bi >= 0 and bi < len(blobs):
            nearest_sh = blob_nearest_enemy_stronghold(
                blobs[bi], state, faction_id, fd, td
            )
            if nearest_sh:
                sh_tid, _ = nearest_sh
                d = min_distance_between_territories(tid, sh_tid, td)
                if d < 999:
                    bonus += max(0.0, (8.0 - min(d, 8.0)) * 2.5)
        mobilization_land_bonus[tid] = bonus

    combat_attack_mult_by_blob: dict[int, float] = {}
    combat_strip_penalty_from: dict[str, float] = {}
    non_combat_push_mult_by_blob: dict[int, float] = {}
    for bi, mode in blob_mode.items():
        if mode == "defend":
            combat_attack_mult_by_blob[bi] = 0.88
            non_combat_push_mult_by_blob[bi] = 0.55
        elif mode == "push":
            combat_attack_mult_by_blob[bi] = 1.14
            non_combat_push_mult_by_blob[bi] = 1.12
        else:
            combat_attack_mult_by_blob[bi] = 1.0
            non_combat_push_mult_by_blob[bi] = 0.92

    for tid in our_land:
        p = territory_pressure.get(tid, 0.0)
        cap_m = 1.65 if is_faction_capital_territory(tid, fd) else 1.0
        combat_strip_penalty_from[tid] = 10.0 * p * cap_m

    non_combat_reinforce_bonus_to: dict[str, float] = {}
    for tid in our_land:
        tdef = td.get(tid)
        p = territory_pressure.get(tid, 0.0)
        if not tdef:
            continue
        if getattr(tdef, "is_stronghold", False) or is_faction_capital_territory(
            tid, fd
        ):
            non_combat_reinforce_bonus_to[tid] = 18.0 * p
        else:
            non_combat_reinforce_bonus_to[tid] = 7.0 * p

    our_fd = fd.get(faction_id)
    allied_tag = getattr(our_fd, "alliance", "") if our_fd else ""
    ally_pressure_max = 0.0
    combat_move_bonus_to: dict[str, float] = {}
    if allied_tag:
        for tid, terr in (state.territories or {}).items():
            owner = getattr(terr, "owner", None)
            if not owner or owner == faction_id:
                continue
            o_fd = fd.get(owner)
            if not o_fd or getattr(o_fd, "alliance", "") != allied_tag:
                continue
            tdef = td.get(tid)
            if not tdef or _is_sea_zone(tdef):
                continue
            p_ally = _pressure_for_territory(tid, state, faction_id, fd, td, ud)
            adj_local = adjacent_enemy_land_unit_count(
                tid, state, faction_id, fd, td, ud
            )
            if p_ally > ally_pressure_max:
                ally_pressure_max = p_ally
            ally_hot = (
                adj_local > 0 or p_ally >= STRATEGIC_ALLY_NON_COMBAT_MIN_PRESSURE
            )
            if ally_hot and (
                getattr(tdef, "is_stronghold", False)
                or is_faction_capital_territory(tid, fd)
            ):
                non_combat_reinforce_bonus_to[tid] = (
                    STRATEGIC_ALLY_NON_COMBAT_SH_MULT * p_ally
                )
            elif ally_hot and p_ally > 0.06:
                non_combat_reinforce_bonus_to[tid] = (
                    STRATEGIC_ALLY_NON_COMBAT_LAND_MULT * p_ally
                )
            if p_ally >= STRATEGIC_ALLY_COMBAT_MOVE_PRESSURE_FLOOR:
                for adj in getattr(tdef, "adjacent", []) or []:
                    adj_terr = state.territories.get(adj)
                    if not adj_terr:
                        continue
                    ao = getattr(adj_terr, "owner", None)
                    if ao == faction_id:
                        continue
                    if ao:
                        a_fd = fd.get(ao)
                        if a_fd and getattr(a_fd, "alliance", "") == allied_tag:
                            continue
                    b = STRATEGIC_ALLY_COMBAT_MOVE_ADJACENT_SCALE * p_ally
                    if b > combat_move_bonus_to.get(adj, 0.0):
                        combat_move_bonus_to[adj] = b

    max_our_p = max(territory_pressure.values()) if territory_pressure else 0.0
    # Slightly raise purchase defense blend when allies (same alliance) are under map pressure.
    purchase_defense_priority = max(
        max_our_p, ally_pressure_max * STRATEGIC_ALLY_PURCHASE_DEFENSE_PRIORITY_MULT
    )

    capacity = get_mobilization_capacity(state, faction_id, td, cd, port_d, ud)
    naval_sea_zone_bonus = _naval_sea_zone_bonuses(
        state,
        faction_id,
        td,
        fd,
        ud,
        port_d,
        capacity.get("sea_zones", []),
    )

    return StrategicTurnContext(
        faction_id=faction_id,
        blobs=blobs,
        territory_blob=territory_blob,
        blob_mode=blob_mode,
        territory_pressure=territory_pressure,
        mobilization_land_bonus=mobilization_land_bonus,
        combat_attack_mult_by_blob=combat_attack_mult_by_blob,
        combat_strip_penalty_from=combat_strip_penalty_from,
        non_combat_reinforce_bonus_to=non_combat_reinforce_bonus_to,
        non_combat_push_mult_by_blob=non_combat_push_mult_by_blob,
        combat_move_bonus_to=combat_move_bonus_to,
        purchase_defense_priority=purchase_defense_priority,
        naval_sea_zone_bonus=naval_sea_zone_bonus,
    )
