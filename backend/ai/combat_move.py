"""
Combat-move phase: balance units across multiple attacks.

Each candidate is garrison-pruned first (see garrison.py): do not strip land from a threatened
origin unless the move is a counterattack into enemy units on the destination that threaten
that origin. Then scoring / sims run on the pruned unit set.

Once an attack reaches CONFIDENT_WIN_RATE (~90%), we can dedicate units to a second attack;
units sent is an input to the sim so we recalc per candidate move.
Cavalry charges conquer intermediate hexes but leave them empty. Empty-target scoring uses
combat_forecast: reducer-applied pending + hypothetical PendingMove vs baseline holes after
pending only (see get_state_after_combat_moves_scenario), so inferred charge paths and capture
order match the real phase end. Open-space execution batches non-cav land in one move and caps
cavalry per destination across the whole combat_move phase (pending + new).
"""

from collections import Counter
from collections.abc import Callable

from backend.engine.actions import Action, move_units, end_phase
from backend.engine.movement import (
    _is_sea_zone,
    get_charge_max_gain_over_moves,
    resolve_territory_key_in_state,
)
from backend.engine.queries import (
    get_movable_units,
    get_unit_move_targets,
    get_unit_faction,
    _is_naval_unit,
    filter_unit_instances_that_can_reach,
    validate_action,
)
from backend.engine.reducer import get_state_after_pending_moves
from backend.engine.combat_sim import run_simulation, SimOptions

from backend.ai.combat_forecast import (
    empty_exposed_holes_map,
    forecast_state_with_extra_combat_moves,
    make_combat_pending_move,
    new_empty_exposed_holes_vs_baseline,
)
from backend.ai.context import AIContext
from backend.ai.habits import (
    FRONTLINE_BONUS,
    COMBAT_SIM_TRIALS,
    COMBAT_MOVE_CONFIDENT_WIN_RATE,
    COMBAT_MOVE_MIN_WIN_RATE,
    COMBAT_MOVE_EMPTY_TERRITORY_BONUS_ADDED,
    COMBAT_MOVE_EMPTY_TERRITORY_BONUS_MULTIPLIER,
    COMBAT_MOVE_FUTURE_CHARGE_WEIGHT,
    COMBAT_MOVE_CHARGE_LOOKAHEAD_TURNS,
    COMBAT_MOVE_MARGINAL_WIN_SATURATION_THRESHOLD,
    COMBAT_MOVE_MARGINAL_NET_SATURATION_THRESHOLD,
    COMBAT_MOVE_SATURATION_SCORE_FACTOR,
    COMBAT_MOVE_EMPTY_WHEN_CONFIDENT_BONUS,
    COMBAT_MOVE_EMPTY_WHEN_HIGH_COMMIT_WIN_BONUS,
    COMBAT_MOVE_HIGH_COMMIT_WIN_RATE_FLOOR,
    COMBAT_MOVE_FRIENDLY_CHARGE_CORRIDOR_BONUS,
    COMBAT_MOVE_OPEN_SPACE_MAX_CHARGE_UNITS,
    COMBAT_MOVE_OPEN_SPACE_SECOND_CHARGE_MIN_FUTURE_GAIN,
    COMBAT_MOVE_OPEN_SPACE_NEW_DIRECTION_BONUS,
    AI_ELITE_UNIT_MIN_POWER_COST,
    COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY,
    COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_PER_STEP,
    COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_EXPONENT,
    COMBAT_MOVE_ABANDON_PRESSURED_FRONTLINE_PENALTY,
    COMBAT_MOVE_FRONTLINE_CRISIS_LONG_MARCH_EXTRA,
    COMBAT_MOVE_DISTANT_STRONGHOLD_FROM_FRONTLINE_PENALTY,
    COMBAT_MOVE_HOLD_PREFERRED_OVER_COUNTERATTACK_PENALTY,
    PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS,
)
from backend.ai.randomness import pick_from_score_band
from backend.ai.formulas import (
    battle_gain_if_win,
    expected_net_gain,
    get_unit_power_cost,
)
from backend.ai.defense_sim import (
    holding_origin_beats_counterattack_into_threat,
    merge_combat_move_attacker_stacks,
)
from backend.ai.garrison import (
    move_attacks_enemy_stack_that_threatens_origin,
    prune_move_unit_ids_for_garrison_floor,
)
from backend.ai.geography import (
    get_faction_territory_blobs,
    territory_to_blob_index,
    blob_nearest_enemy_stronghold,
    count_enemies_that_can_reach_territory_combat_move,
    frontline_defense_outnumbered,
    frontline_hex_next_turn_outnumbered,
    is_frontline,
    is_frontline_threatened_by_enemy_army,
    min_distance_between_territories,
    territory_threatened_by_enemy_combat_move_next_turn,
    exposed_empty_conquest_reinforce_need,
    worth_empty_conquest_combat_move,
)


def _combat_move_range_penalty(
    from_tid: str,
    to_tid: str,
    *,
    state,
    faction_id: str,
    fd,
    td,
    ud,
    land_moving: int,
    outnumbered_front: bool,
) -> float:
    """
    Penalize moving land away toward distant targets (graph steps) so expected gain falls off with
    range. Extra when leaving a frontline hex that is already next-turn outnumbered (local crisis).
    """
    if land_moving <= 0:
        return 0.0
    d_ft = min_distance_between_territories(from_tid, to_tid, td)
    d_step = max(0, d_ft - 1)
    if d_step < 1:
        return 0.0
    pen = COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_PER_STEP * (
        d_step**COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_EXPONENT
    )
    if frontline_hex_next_turn_outnumbered(
        from_tid, state, faction_id, fd, td, ud
    ):
        pen += COMBAT_MOVE_ABANDON_PRESSURED_FRONTLINE_PENALTY * (d_step**2)
    elif outnumbered_front and is_frontline(
        from_tid, state, faction_id, fd, td
    ):
        pen += COMBAT_MOVE_FRONTLINE_CRISIS_LONG_MARCH_EXTRA * d_step
    return pen


def _is_conquest_territory(territory_id: str, state, faction_id: str, fd, td) -> bool:
    """True if we actually gain from conquering this territory (enemy or neutral ownable). False for friendly/allied pass-through—we don't count their power."""
    terr = state.territories.get(territory_id)
    if not terr:
        return False
    tdef = td.get(territory_id)
    owner = getattr(terr, "owner", None)
    if owner == faction_id:
        return False
    if owner is not None:
        our_fd = fd.get(faction_id)
        owner_fd = fd.get(owner)
        if our_fd and owner_fd and getattr(owner_fd, "alliance", "") == getattr(our_fd, "alliance", ""):
            return False
        return True
    return bool(tdef and getattr(tdef, "ownable", True))


def _is_enemy_territory(state, territory_id: str, faction_id: str, fd, ud) -> bool:
    """True if territory is enemy (owned by enemy or has enemy units)."""
    terr = state.territories.get(territory_id)
    if not terr:
        return False
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    owner = getattr(terr, "owner", None)
    if owner and owner != faction_id:
        other_fd = fd.get(owner)
        if other_fd and getattr(other_fd, "alliance", "") != our_alliance:
            return True
    for u in getattr(terr, "units", []) or []:
        uf = get_unit_faction(u, ud)
        if uf == faction_id:
            continue
        if uf and fd.get(uf) and getattr(fd.get(uf), "alliance", "") != our_alliance:
            return True
    return bool(owner and owner != faction_id and fd.get(owner) and getattr(fd.get(owner), "alliance", "") != our_alliance)


def _is_attackable_territory(state, territory_id: str, faction_id: str, fd, ud, td) -> bool:
    """True if we can attack/move into this territory in combat_move: enemy OR empty neutral (free conquest)."""
    if _is_enemy_territory(state, territory_id, faction_id, fd, ud):
        return True
    terr = state.territories.get(territory_id)
    tdef = td.get(territory_id) if td else None
    if not terr or not tdef:
        return False
    if getattr(terr, "owner", None) is not None:
        return False
    if not getattr(tdef, "ownable", True):
        return False
    our_alliance = getattr(fd.get(faction_id), "alliance",
                           "") if fd.get(faction_id) else ""
    for u in getattr(terr, "units", []) or []:
        uf = get_unit_faction(u, ud)
        if uf and uf != faction_id:
            other_fd = fd.get(uf)
            if other_fd and getattr(other_fd, "alliance", "") != our_alliance:
                return False
    return True


def _our_or_allied_territory(state, tid: str, faction_id: str, fd) -> bool:
    """True if we own the territory or it is owned by an ally."""
    terr = state.territories.get(tid)
    if not terr:
        return False
    owner = getattr(terr, "owner", None)
    if owner == faction_id:
        return True
    if owner and fd.get(owner) and fd.get(faction_id):
        return getattr(fd.get(owner), "alliance", "") == getattr(
            fd.get(faction_id), "alliance", ""
        )
    return False


def _defender_stack_total(def_stacks: list) -> int:
    return sum(int(s.get("count", 0) or 0) for s in (def_stacks or []))


def _defender_stack_power_cost_sum(def_stacks: list, ud) -> float:
    """Recruitment power cost of defender stacks (rough value of wiping them)."""
    t = 0.0
    for s in def_stacks or []:
        uid = s.get("unit_id")
        c = int(s.get("count", 0) or 0)
        if not uid or c <= 0:
            continue
        pc = get_unit_power_cost(ud.get(uid)) or 0
        t += float(pc * c)
    return t


def _is_cavalry_combat(ud, unit_id: str) -> bool:
    """True if unit type is cavalry for combat-move heuristics. Always returns bool (never list)."""
    try:
        uid = unit_id if isinstance(unit_id, str) else str(unit_id or "")
        u = ud.get(uid) if ud else None
        if not u:
            return False
        if getattr(u, "archetype", "") == "cavalry":
            return True
        tags = getattr(u, "tags", None)
        if tags is None:
            tags = []
        if not isinstance(tags, list):
            return False
        return "cavalry" in tags
    except (TypeError, AttributeError):
        return False


def _safe_power_cost_for_sort(ud, unit_id: str) -> int:
    """Non-negative int for sort keys; never list/bool mixing."""
    try:
        uid = unit_id if isinstance(unit_id, str) else str(unit_id or "")
        pc = get_unit_power_cost(ud.get(uid) if uid else None)
        n = int(pc)
        return n if n > 0 else 99
    except (TypeError, ValueError):
        return 99


def _pending_cavalry_count_to_territory(
    state,
    to_key: str,
    faction_id: str,
    ud,
    territory_defs,
) -> int:
    """
    Cavalry already queued in combat_move pending into to_key (same phase, same destination).
    Units are still listed on from_territory until phase end.
    """
    n = 0
    for pm in state.pending_moves or []:
        if getattr(pm, "phase", None) != "combat_move":
            continue
        pm_to = resolve_territory_key_in_state(
            state, str(getattr(pm, "to_territory", "") or ""), territory_defs
        )
        if pm_to != to_key:
            continue
        from_key = resolve_territory_key_in_state(
            state, str(getattr(pm, "from_territory", "") or ""), territory_defs
        )
        terr = state.territories.get(from_key)
        if not terr:
            continue
        by_id = {u.instance_id: u for u in getattr(terr, "units", []) or []}
        for iid in getattr(pm, "unit_instance_ids", []) or []:
            u = by_id.get(iid)
            if not u or get_unit_faction(u, ud) != faction_id:
                continue
            if _is_naval_unit(ud.get(u.unit_id)):
                continue
            if _is_cavalry_combat(ud, u.unit_id):
                n += 1
    return n


def _pending_open_space_destinations_from(
    state,
    from_key: str,
    faction_id: str,
    fd,
    ud,
    territory_defs,
) -> set[str]:
    """
    Destination territory keys (resolved) we already have a combat_move pending into from from_key,
    where the destination is still empty of enemy defenders on the live board.
    """
    out: set[str] = set()
    for pm in state.pending_moves or []:
        if getattr(pm, "phase", None) != "combat_move":
            continue
        fk = resolve_territory_key_in_state(
            state, str(getattr(pm, "from_territory", "") or ""), territory_defs
        )
        if fk != from_key:
            continue
        tk = resolve_territory_key_in_state(
            state, str(getattr(pm, "to_territory", "") or ""), territory_defs
        )
        if _defender_stacks(state, tk, faction_id, fd, ud):
            continue
        out.add(tk)
    return out


def _prune_empty_open_space_move(
    unit_ids: list[str],
    from_tid: str,
    state,
    ud,
    future_path_gain: float,
    *,
    would_hold_frontline: bool,
    has_confident_defended_elsewhere: bool,
    pending_cavalry_to_destination: int = 0,
    charge_path_for: Callable[[list[str]], list[str] | None] | None = None,
) -> list[str]:
    """
    Open-space / empty-defender moves:
    - Cavalry: at most COMBAT_MOVE_OPEN_SPACE_MAX_CHARGE_UNITS per destination for the whole
      combat_move phase (pending + this move); 1–2 in a single move when lookahead justifies 2.
    - Non-cavalry land: batch all eligible units in one move (same from→empty to).
    - Cavalry with a non-empty charge path cannot share a move with infantry; foot follow in a
      later decision (also batched).
    - Shallow-hold: still prefer one cheap infantry when another attack is confident and there is
      no deep charge chain.
    """
    terr = state.territories.get(from_tid)
    if not terr:
        return unit_ids[:1]
    ids_set = set(unit_ids)
    units_here = [
        u for u in (getattr(terr, "units", []) or [])
        if getattr(u, "instance_id", "") in ids_set
    ]
    if not units_here:
        return unit_ids[:1]

    def _land_movable(u) -> bool:
        return not _is_naval_unit(ud.get(getattr(u, "unit_id", "")))

    land_units = [u for u in units_here if _land_movable(u)]
    if not land_units:
        return [units_here[0].instance_id]

    shallow_hold = (
        would_hold_frontline
        and has_confident_defended_elsewhere
        and future_path_gain < COMBAT_MOVE_OPEN_SPACE_SECOND_CHARGE_MIN_FUTURE_GAIN
    )
    if shallow_hold:
        infantry = [u for u in land_units if not _is_cavalry_combat(ud, u.unit_id)]
        if infantry:
            infantry.sort(
                key=lambda u: get_unit_power_cost(ud.get(u.unit_id)) or 99,
            )
            return [infantry[0].instance_id]

    room_cav = max(
        0,
        COMBAT_MOVE_OPEN_SPACE_MAX_CHARGE_UNITS - int(pending_cavalry_to_destination),
    )
    max_cav_this_move = 1
    if (
        future_path_gain >= COMBAT_MOVE_OPEN_SPACE_SECOND_CHARGE_MIN_FUTURE_GAIN
        and COMBAT_MOVE_OPEN_SPACE_MAX_CHARGE_UNITS >= 2
    ):
        max_cav_this_move = 2
    max_cav_this_move = min(max_cav_this_move, room_cav)

    cav_units = [u for u in land_units if _is_cavalry_combat(ud, u.unit_id)]
    infantry_units = [u for u in land_units if not _is_cavalry_combat(ud, u.unit_id)]

    cav_units.sort(
        key=lambda u: int(getattr(ud.get(u.unit_id), "movement", 0) or 0),
        reverse=True,
    )

    if max_cav_this_move > 0 and cav_units:
        take_cav = cav_units[:max_cav_this_move]
        cav_ids = [u.instance_id for u in take_cav]
        path: list[str] = []
        if charge_path_for is not None:
            p = charge_path_for(cav_ids)
            path = list(p or [])
        if path:
            return cav_ids
        foot_sorted = sorted(
            infantry_units,
            key=lambda u: get_unit_power_cost(ud.get(u.unit_id)) or 99,
        )
        return cav_ids + [u.instance_id for u in foot_sorted]

    # No cavalry in this move (either none in stack or phase cav cap already hit on this hex).
    if infantry_units:
        infantry_units.sort(
            key=lambda u: get_unit_power_cost(ud.get(u.unit_id)) or 99,
        )
        return [u.instance_id for u in infantry_units]

    # Only cavalry left but phase cap already has max cav queued into this destination.
    return []


def _land_unit_count_moving(
    state, from_tid: str, instance_ids: list[str], ud
) -> int:
    """Land (non-naval) units in this move from from_tid."""
    terr = state.territories.get(from_tid)
    if not terr:
        return 0
    want = set(instance_ids)
    n = 0
    for u in getattr(terr, "units", []) or []:
        if getattr(u, "instance_id", "") not in want:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
            continue
        n += 1
    return n


def _units_to_stacks(units: list, ud) -> list[dict]:
    """Convert list of Unit to [{"unit_id": str, "count": int}, ...]."""
    counts = Counter(getattr(u, "unit_id", "") for u in units if getattr(
        u, "unit_id", "") and ud.get(getattr(u, "unit_id", "")))
    return [{"unit_id": uid, "count": c} for uid, c in counts.items()]


def _committed_attacker_stacks(state, territory_id: str, faction_id: str, ud) -> list[dict]:
    """Our units at this territory (for attack sim)."""
    terr = state.territories.get(territory_id)
    if not terr:
        return []
    our = [u for u in (getattr(terr, "units", []) or [])
           if get_unit_faction(u, ud) == faction_id]
    return _units_to_stacks(our, ud)


def _defender_stacks(state, territory_id: str, faction_id: str, fd, ud) -> list[dict]:
    """Enemy units at this territory."""
    terr = state.territories.get(territory_id)
    if not terr:
        return []
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    enemy = [
        u for u in (getattr(terr, "units", []) or [])
        if get_unit_faction(u, ud) != faction_id
        and fd.get(get_unit_faction(u, ud))
        and getattr(fd.get(get_unit_faction(u, ud)), "alliance", "") != our_alliance
    ]
    return _units_to_stacks(enemy, ud)


def _stacks_for_unit_ids(state, from_territory_id: str, unit_instance_ids: list, ud) -> list[dict]:
    """Convert unit instance ids in from_territory to stacks {unit_id, count}."""
    terr = state.territories.get(from_territory_id)
    if not terr:
        return []
    ids_set = set(unit_instance_ids)
    units = [u for u in (getattr(terr, "units", []) or [])
             if getattr(u, "instance_id", "") in ids_set]
    return _units_to_stacks(units, ud)


def _pick_must_attack_move(
    state,
    faction_id: str,
    fd,
    ud,
    td,
    from_to_units: dict,
    must_attack_boat_ids: set,
    best_charge_path_fn,  # (key, unit_ids) -> list[str] | None
    move_units_fn,
):
    """
    When can_end_phase is False (loaded boats must attack), pick any valid move from a sea zone
    that contains a must-attack boat to land (sea raid) or enemy sea (naval combat).
    """
    if not must_attack_boat_ids:
        return None
    for (from_tid, to_tid), unit_ids in from_to_units.items():
        if not unit_ids:
            continue
        from_def = td.get(from_tid)
        to_def = td.get(to_tid)
        if not from_def or not to_def:
            continue
        if not _is_sea_zone(from_def):
            continue
        to_land = not _is_sea_zone(to_def)
        to_enemy_sea = _is_sea_zone(to_def) and _is_enemy_territory(
            state, to_tid, faction_id, fd, ud
        )
        if not (to_land or to_enemy_sea):
            continue
        from_territory = state.territories.get(from_tid)
        if not from_territory:
            continue
        has_must_attack_boat = any(
            getattr(u, "instance_id", "") in must_attack_boat_ids
            for u in (getattr(from_territory, "units", []) or [])
        )
        if not has_must_attack_boat:
            continue
        ids_to_use = list(unit_ids)
        if to_land:
            ids_set = set(ids_to_use)
            land_ids = [
                u.instance_id
                for u in (getattr(from_territory, "units", []) or [])
                if u.instance_id in ids_set and not _is_naval_unit(ud.get(u.unit_id))
            ]
            if not land_ids:
                continue
            ids_to_use = land_ids
        # Only include unit instances that can reach to_tid (per remaining_movement)
        ids_to_use = filter_unit_instances_that_can_reach(
            state, to_tid, ids_to_use, ud, td, fd
        )
        if not ids_to_use:
            continue
        charge_through = best_charge_path_fn((from_tid, to_tid), ids_to_use)
        return move_units_fn(
            faction_id, from_tid, to_tid, ids_to_use, charge_through=charge_through
        )
    return None


def _last_resort_must_attack_move(
    state,
    faction_id: str,
    fd,
    ud,
    td,
    camp_defs,
    port_defs,
    must_attack_boat_ids: set[str],
    movable: list[dict],
    pending_unit_ids: set[str],
) -> Action | None:
    """
    When combat_move cannot end_phase (loaded boats must attack) but heuristics produced no move,
    brute-force any engine-valid move from a sea zone that holds a must-attack boat.
    Tries single units first, then one batched sea-raid (all reachable land with shared charge path).
    """
    if not must_attack_boat_ids:
        return None
    cd = camp_defs or {}
    pd = port_defs or {}

    sea_from: set[str] = set()
    for tid, terr in (state.territories or {}).items():
        for u in getattr(terr, "units", []) or []:
            if getattr(u, "instance_id", "") in must_attack_boat_ids:
                sea_from.add(tid)
                break

    movable_iids = {
        m.get("instance_id")
        for m in movable
        if m.get("instance_id") and m.get("instance_id") not in pending_unit_ids
    }

    def _intersect_charge_through(
        to_tid: str,
        ids_use: list[str],
        charge_routes_by_iid: dict[str, dict],
    ) -> list[str] | None:
        valid: set[tuple[str, ...]] | None = None
        for uid in ids_use:
            cr = charge_routes_by_iid.get(uid) or {}
            pts = cr.get(to_tid)
            if pts is None:
                return None
            if not pts:
                pt_set = {()}
            else:
                pt_set = {tuple(p) for p in pts}
            valid = pt_set if valid is None else (valid & pt_set)
        if not valid:
            return None
        return list(min(valid, key=lambda t: (len(t), t)))

    # 1) Single-unit attempts (naval -> enemy sea, land -> land sea raid)
    for from_tid in sorted(sea_from):
        from_def = td.get(from_tid)
        if not from_def or not _is_sea_zone(from_def):
            continue
        from_territory = state.territories.get(from_tid)
        if not from_territory:
            continue
        if not any(
            getattr(u, "instance_id", "") in must_attack_boat_ids
            for u in (getattr(from_territory, "units", []) or [])
        ):
            continue
        for u in getattr(from_territory, "units", []) or []:
            iid = getattr(u, "instance_id", "") or ""
            if not iid or iid in pending_unit_ids or iid not in movable_iids:
                continue
            targets, charge_routes = get_unit_move_targets(state, iid, ud, td, fd)
            if not targets:
                continue
            for to_tid in sorted(targets.keys()):
                if not to_tid or to_tid == from_tid:
                    continue
                to_def = td.get(to_tid)
                if not to_def:
                    continue
                to_land = not _is_sea_zone(to_def)
                to_enemy_sea = _is_sea_zone(to_def) and _is_enemy_territory(
                    state, to_tid, faction_id, fd, ud
                )
                if to_land:
                    if _is_naval_unit(ud.get(u.unit_id)):
                        continue
                elif to_enemy_sea:
                    if not _is_naval_unit(ud.get(u.unit_id)):
                        continue
                else:
                    continue
                ids_use = filter_unit_instances_that_can_reach(
                    state, to_tid, [iid], ud, td, fd
                )
                if not ids_use:
                    continue
                ct = _intersect_charge_through(
                    to_tid, ids_use, {iid: charge_routes}
                )
                action = move_units(
                    faction_id,
                    from_tid,
                    to_tid,
                    ids_use,
                    charge_through=ct,
                )
                vr = validate_action(state, action, ud, td, fd, cd, pd)
                if vr.valid:
                    return action
                if ct is not None:
                    action2 = move_units(
                        faction_id,
                        from_tid,
                        to_tid,
                        ids_use,
                        charge_through=None,
                    )
                    if validate_action(state, action2, ud, td, fd, cd, pd).valid:
                        return action2

    # 2) Batch sea raid: all movable land on this sea hex that share a charge path to to_tid
    for from_tid in sorted(sea_from):
        from_def = td.get(from_tid)
        if not from_def or not _is_sea_zone(from_def):
            continue
        from_territory = state.territories.get(from_tid)
        if not from_territory:
            continue
        if not any(
            getattr(u, "instance_id", "") in must_attack_boat_ids
            for u in (getattr(from_territory, "units", []) or [])
        ):
            continue
        land_iids = [
            u.instance_id
            for u in (getattr(from_territory, "units", []) or [])
            if getattr(u, "instance_id", "")
            and u.instance_id not in pending_unit_ids
            and u.instance_id in movable_iids
            and not _is_naval_unit(ud.get(u.unit_id))
        ]
        if len(land_iids) < 2:
            continue
        target_sets: dict[str, set[str]] = {}
        cr_by_iid: dict[str, dict] = {}
        for iid in land_iids:
            targets, cr = get_unit_move_targets(state, iid, ud, td, fd)
            cr_by_iid[iid] = cr or {}
            for t in (targets or {}).keys():
                if not t or t == from_tid:
                    continue
                tdf = td.get(t)
                if not tdf or _is_sea_zone(tdf):
                    continue
                target_sets.setdefault(t, set()).add(iid)
        for to_tid in sorted(target_sets.keys()):
            group = sorted(target_sets[to_tid])
            if len(group) < 2:
                continue
            reachable = filter_unit_instances_that_can_reach(
                state, to_tid, group, ud, td, fd
            )
            if len(reachable) < 2:
                continue
            cr_map = {iid: cr_by_iid.get(iid) or {} for iid in reachable}
            ct = _intersect_charge_through(to_tid, reachable, cr_map)
            action = move_units(
                faction_id, from_tid, to_tid, reachable, charge_through=ct
            )
            vr = validate_action(state, action, ud, td, fd, cd, pd)
            if vr.valid:
                return action
            if ct is not None:
                action2 = move_units(
                    faction_id,
                    from_tid,
                    to_tid,
                    reachable,
                    charge_through=None,
                )
                if validate_action(state, action2, ud, td, fd, cd, pd).valid:
                    return action2

    return None


def decide_combat_move(ctx: AIContext):
    """
    Pick one move_units (into an enemy territory) that balances multiple attacks.

    Before scoring attacks, each candidate is pruned so we do not march land away from a
    territory that enemies can reach next combat_move, matching non_combat garrison rules.
    Exception: attacking into enemy units on the destination that themselves could strike
    the origin (counterattack into the threatening stack) only requires one defender left
    behind so the rest can commit to that battle.
    """
    state = ctx.state
    faction_id = ctx.faction_id
    ud = ctx.unit_defs
    td = ctx.territory_defs
    fd = ctx.faction_defs

    state_after = get_state_after_pending_moves(
        state, "combat_move", ud, td, fd)
    movable = get_movable_units(state, faction_id, ud)
    pending_unit_ids = set()
    for pm in (state.pending_moves or []):
        if getattr(pm, "phase", None) == "combat_move":
            pending_unit_ids.update(getattr(pm, "unit_instance_ids", []) or [])

    # Build candidate moves per unit instance: only (from, to) where this unit can reach to (uses remaining_movement).
    # Store charge routes PER UNIT so we only pass a path valid for ALL units in the move (validator requirement).
    from_to_units: dict[tuple[str, str], list[str]] = {}
    charge_routes_per_unit: dict[tuple[str, str],
                                 dict[str, list[list[str]]]] = {}
    for unit_info in movable:
        iid = unit_info.get("instance_id")
        if not iid or iid in pending_unit_ids:
            continue
        from_tid = unit_info.get("territory_id")
        targets, charge_routes = get_unit_move_targets(
            state, iid, ud, td, fd)  # per-unit reachable set
        for to_tid in (targets or {}).keys():
            if not to_tid or to_tid == from_tid:
                continue
            if not _is_attackable_territory(state, to_tid, faction_id, fd, ud, td):
                continue
            key = (from_tid, to_tid)
            from_to_units.setdefault(key, []).append(iid)
            if charge_routes.get(to_tid):
                charge_routes_per_unit.setdefault(
                    key, {})[iid] = charge_routes[to_tid]

    def _territory_power(tid: str) -> int:
        tdef = td.get(tid)
        if not tdef or not getattr(tdef, "produces", None) or not isinstance(tdef.produces, dict):
            return 0
        return int(tdef.produces.get("power", 0) or 0)

    def _best_charge_path(key: tuple[str, str], unit_ids_for_move: list[str]) -> list[str] | None:
        """Return a charge path valid for ALL units in the move (validator requires this)."""
        per_unit = charge_routes_per_unit.get(key, {})
        if not unit_ids_for_move:
            return None
        valid_paths = None
        for uid in unit_ids_for_move:
            paths = per_unit.get(uid, [])
            path_tuples = {tuple(p) for p in paths}
            if valid_paths is None:
                valid_paths = path_tuples
            else:
                valid_paths &= path_tuples
        if not valid_paths:
            return None
        paths = [list(p) for p in valid_paths]
        _, to_tid = key
        # Rank by conquered value only (exclude friendly pass-through)
        def path_sort_key(path: list[str]) -> tuple[int, int]:
            conquered_power = int(
                sum(
                    _territory_power(t) for t in path
                    if _is_conquest_territory(t, state, faction_id, fd, td)
                )
                + (
                    _territory_power(to_tid)
                    if _is_conquest_territory(to_tid, state, faction_id, fd, td)
                    else 0
                )
            )
            path_len = len(path) + 1
            return (-conquered_power, path_len)

        return min(paths, key=path_sort_key)

    can_end_phase = ctx.available_actions.get("can_end_phase", True)
    must_attack_boat_ids = set(
        ctx.available_actions.get(
            "loaded_naval_must_attack_instance_ids") or []
    )

    def _must_attack_last_resort() -> Action | None:
        if can_end_phase or not must_attack_boat_ids:
            return None
        return _last_resort_must_attack_move(
            state,
            faction_id,
            fd,
            ud,
            td,
            ctx.camp_defs,
            ctx.port_defs,
            must_attack_boat_ids,
            movable,
            pending_unit_ids,
        )

    if not from_to_units:
        if can_end_phase:
            return end_phase(faction_id)
        return _must_attack_last_resort()

    def _defender_cas_mean_for_net(to_tid: str, sim_def_mean: float) -> float:
        """Use 0 for defender casualty cost when territory has neutral units (no owner); no economic gain from killing neutrals."""
        terr = state_after.territories.get(to_tid)
        if terr and getattr(terr, "owner", None) is None:
            return 0.0
        return sim_def_mean

    # Current win rates per attack target (with committed units only)
    attack_targets = set(to_tid for _, to_tid in from_to_units.keys())
    # Marginal "saturation" down-scores only make sense when several defended attacks compete for
    # the same pool of movers — never for a single defended front (would crush the only battle).
    defended_attack_destinations = {
        to_tid
        for (_f, to_tid) in from_to_units.keys()
        if _defender_stacks(state_after, to_tid, faction_id, fd, ud)
    }
    n_defended_destinations = len(defended_attack_destinations)
    committed_win_rate: dict[str, float] = {}
    committed_net: dict[str, float] = {}
    for to_tid in attack_targets:
        att = _committed_attacker_stacks(state_after, to_tid, faction_id, ud)
        def_stacks = _defender_stacks(state_after, to_tid, faction_id, fd, ud)
        if not def_stacks:
            committed_win_rate[to_tid] = 1.0
            committed_net[to_tid] = expected_net_gain(
                1.0, to_tid, td, fd, 0.0, 0.0, ctx.camp_defs, ctx.port_defs, ud)
            continue
        if not att:
            committed_win_rate[to_tid] = 0.0
            committed_net[to_tid] = 0.0
            continue
        opts = None
        if getattr(td.get(to_tid), "is_stronghold", False):
            opts = SimOptions(must_conquer=True,
                              casualty_order_attacker="best_attack")
        sim = run_simulation(att, def_stacks, to_tid, ud,
                             td, n_trials=COMBAT_SIM_TRIALS, options=opts)
        committed_win_rate[to_tid] = sim.p_attacker_win
        committed_net[to_tid] = expected_net_gain(
            sim.p_attacker_win, to_tid, td, fd,
            _defender_cas_mean_for_net(
                to_tid, sim.defender_casualty_cost_mean),
            sim.attacker_casualty_cost_mean,
            ctx.camp_defs,
            ctx.port_defs,
            ud,
        )

    confident_targets = {t for t in attack_targets if committed_win_rate.get(
        t, 0) >= COMBAT_MOVE_CONFIDENT_WIN_RATE}

    has_confident_defended_elsewhere = any(
        committed_win_rate.get(tid, 0) >= COMBAT_MOVE_CONFIDENT_WIN_RATE
        and bool(_defender_stacks(state_after, tid, faction_id, fd, ud))
        for tid in attack_targets
    )

    has_high_commit_elsewhere = any(
        committed_win_rate.get(tid, 0) >= COMBAT_MOVE_HIGH_COMMIT_WIN_RATE_FLOOR
        and bool(_defender_stacks(state_after, tid, faction_id, fd, ud))
        for tid in attack_targets
    )

    blobs = get_faction_territory_blobs(state, faction_id, td)
    tid_to_blob = territory_to_blob_index(state, faction_id, td)
    strategic = ctx.strategic
    base_holes = empty_exposed_holes_map(
        state_after, faction_id, fd, td, ud
    )

    # Next-turn pressure vs local garrison on our front (same reach model as reinforce_value).
    outnumbered_front = frontline_defense_outnumbered(
        state, faction_id, fd, td, ud
    )

    reach_from_cache: dict[str, int] = {}

    # ((from_tid, to_tid, unit_ids), score)
    candidates: list[tuple[tuple, float]] = []

    for (from_tid, to_tid), unit_ids in from_to_units.items():
        if not unit_ids:
            continue
        if from_tid not in reach_from_cache:
            reach_from_cache[from_tid] = count_enemies_that_can_reach_territory_combat_move(
                from_tid, state, faction_id, fd, td, ud
            )
        threat_from = reach_from_cache[from_tid]
        threat_relief = move_attacks_enemy_stack_that_threatens_origin(
            from_tid, to_tid, state, faction_id, fd, td, ud
        )
        if threat_relief:
            move_early = _stacks_for_unit_ids(state, from_tid, unit_ids, ud)
            committed_att_early = _committed_attacker_stacks(
                state_after, to_tid, faction_id, ud
            )
            combined_early = merge_combat_move_attacker_stacks(
                committed_att_early, move_early
            )
            def_target_early = _defender_stacks(
                state_after, to_tid, faction_id, fd, ud
            )
            if (
                combined_early
                and def_target_early
                and holding_origin_beats_counterattack_into_threat(
                    state,
                    from_tid,
                    to_tid,
                    combined_early,
                    def_target_early,
                    faction_id,
                    fd,
                    td,
                    ud,
                )
            ):
                threat_relief = False
        unit_ids = prune_move_unit_ids_for_garrison_floor(
            state,
            from_tid,
            unit_ids,
            faction_id,
            ud,
            td,
            fd,
            threat_from_count=threat_from,
            threat_relief_attack=threat_relief,
        )
        if not unit_ids:
            continue
        move_stacks = _stacks_for_unit_ids(state, from_tid, unit_ids, ud)
        if not move_stacks:
            continue
        def_stacks = _defender_stacks(state_after, to_tid, faction_id, fd, ud)
        if not def_stacks:
            # `worth_empty_conquest_combat_move` throttles "wasting" combat_move on no-value empty
            # hexes (objective/power heuristics). That accidentally dropped obvious sea raids onto empty
            # enemy coast: passengers have no other combat use for that step, but the filter ran
            # before any score comparison. Enemy-owned empty tiles are always worth contesting now
            # (see geography.worth_empty_conquest_combat_move); neutral empty still uses this gate.
            if not worth_empty_conquest_combat_move(
                to_tid,
                state,
                faction_id,
                fd,
                td,
                ud,
                ctx.camp_defs,
                ctx.port_defs,
            ):
                continue
            # Empty defender: 100% win, 0 loss. Only count gain for territories we actually conquer (not friendly/allied pass-through).
            charge_through = _best_charge_path((from_tid, to_tid), unit_ids) or []
            total_gain = 0.0
            if _is_conquest_territory(to_tid, state, faction_id, fd, td):
                total_gain += expected_net_gain(
                    1.0, to_tid, td, fd, 0.0, 0.0, ctx.camp_defs, ctx.port_defs, ud
                )
            for via_tid in charge_through:
                if _is_conquest_territory(via_tid, state, faction_id, fd, td):
                    total_gain += expected_net_gain(
                        1.0, via_tid, td, fd, 0.0, 0.0, ctx.camp_defs, ctx.port_defs, ud
                    )
            score = total_gain * COMBAT_MOVE_EMPTY_TERRITORY_BONUS_MULTIPLIER + COMBAT_MOVE_EMPTY_TERRITORY_BONUS_ADDED
            max_movement = 0
            terr_from = state.territories.get(from_tid)
            ids_set = set(unit_ids)
            for u in (getattr(terr_from, "units", []) or []):
                if getattr(u, "instance_id", "") not in ids_set:
                    continue
                max_movement = max(
                    max_movement,
                    int(getattr(ud.get(u.unit_id), "movement", 0) or 0),
                )
            already_counted = {to_tid} | set(charge_through)

            def _empty_path_gain(tid: str) -> float:
                if _is_conquest_territory(tid, state, faction_id, fd, td):
                    return expected_net_gain(
                        1.0, tid, td, fd, 0.0, 0.0, ctx.camp_defs, ctx.port_defs, ud
                    )
                return 0.0

            future_path_gain = 0.0
            if max_movement > 0:
                future_path_gain = get_charge_max_gain_over_moves(
                    to_tid,
                    state,
                    COMBAT_MOVE_CHARGE_LOOKAHEAD_TURNS * max_movement,
                    faction_id,
                    td,
                    fd,
                    ud,
                    _empty_path_gain,
                    exclude_tids=set(already_counted),
                )
            score += COMBAT_MOVE_FUTURE_CHARGE_WEIGHT * future_path_gain
            if has_confident_defended_elsewhere:
                score += COMBAT_MOVE_EMPTY_WHEN_CONFIDENT_BONUS
            if has_high_commit_elsewhere:
                score += COMBAT_MOVE_EMPTY_WHEN_HIGH_COMMIT_WIN_BONUS
            if charge_through and all(
                _our_or_allied_territory(state, t, faction_id, fd)
                for t in charge_through
            ):
                score += COMBAT_MOVE_FRIENDLY_CHARGE_CORRIDOR_BONUS

            would_hold_frontline = territory_threatened_by_enemy_combat_move_next_turn(
                to_tid, state, faction_id, ud, td, fd
            )
            from_key = resolve_territory_key_in_state(state, from_tid, td)
            pending_open_from_here = _pending_open_space_destinations_from(
                state, from_key, faction_id, fd, ud, td
            )
            to_key = resolve_territory_key_in_state(state, to_tid, td)
            if to_key not in pending_open_from_here:
                score += COMBAT_MOVE_OPEN_SPACE_NEW_DIRECTION_BONUS
            pending_cav_here = _pending_cavalry_count_to_territory(
                state, to_key, faction_id, ud, td
            )
            pruned_ids = _prune_empty_open_space_move(
                unit_ids,
                from_tid,
                state,
                ud,
                future_path_gain,
                would_hold_frontline=would_hold_frontline,
                has_confident_defended_elsewhere=has_confident_defended_elsewhere,
                pending_cavalry_to_destination=pending_cav_here,
                charge_path_for=lambda ids: _best_charge_path((from_tid, to_tid), ids),
            )
            if not pruned_ids:
                continue
            charge_through_exec = _best_charge_path((from_tid, to_tid), pruned_ids) or []
            hole_penalty = 0.0
            try:
                pm = make_combat_pending_move(
                    from_tid,
                    to_tid,
                    pruned_ids,
                    charge_through=charge_through_exec,
                )
                full_f = forecast_state_with_extra_combat_moves(
                    state, ud, td, fd, [pm]
                )
                new_holes = new_empty_exposed_holes_vs_baseline(
                    base_holes, full_f, faction_id, fd, td, ud
                )
                hole_penalty = sum(new_holes.values())
            except (ValueError, TypeError, KeyError, AttributeError):
                for via_tid in charge_through_exec:
                    if _is_conquest_territory(via_tid, state, faction_id, fd, td):
                        hole_penalty += exposed_empty_conquest_reinforce_need(
                            via_tid, state, faction_id, fd, td, ud
                        )
            score -= hole_penalty

            if not charge_through_exec:
                land_n = _land_unit_count_moving(
                    state, from_tid, pruned_ids, ud
                )
                sat = (
                    (1.0 - 1.0 / (1.0 + float(land_n)))
                    if land_n > 0
                    else 0.0
                )
                intr = exposed_empty_conquest_reinforce_need(
                    to_tid, state, faction_id, fd, td, ud
                )
                score += max(intr, base_holes.get(to_tid, 0.0)) * sat
            score -= _combat_move_range_penalty(
                from_tid,
                to_tid,
                state=state,
                faction_id=faction_id,
                fd=fd,
                td=td,
                ud=ud,
                land_moving=_land_unit_count_moving(
                    state, from_tid, pruned_ids, ud
                ),
                outnumbered_front=outnumbered_front,
            )
            if strategic:
                bi_e = tid_to_blob.get(from_tid)
                if bi_e is not None:
                    score *= strategic.combat_attack_mult_by_blob.get(
                        bi_e, 1.0
                    )
                score -= strategic.combat_strip_penalty_from.get(
                    from_tid, 0.0
                )
                score += strategic.combat_move_bonus_to.get(to_tid, 0.0)
            candidates.append(((from_tid, to_tid, pruned_ids), score))
            continue
        committed_att = _committed_attacker_stacks(
            state_after, to_tid, faction_id, ud)
        # Merge committed + this move
        combined: dict[str, int] = {}
        for s in committed_att + move_stacks:
            uid = s.get("unit_id")
            combined[uid] = combined.get(uid, 0) + s.get("count", 0)
        att_stacks = [{"unit_id": uid, "count": c}
                      for uid, c in combined.items()]
        opts = None
        if getattr(td.get(to_tid), "is_stronghold", False):
            opts = SimOptions(must_conquer=True,
                              casualty_order_attacker="best_attack")
        sim = run_simulation(att_stacks, def_stacks, to_tid,
                             ud, td, n_trials=COMBAT_SIM_TRIALS, options=opts)
        net = expected_net_gain(
            sim.p_attacker_win, to_tid, td, fd,
            _defender_cas_mean_for_net(
                to_tid, sim.defender_casualty_cost_mean),
            sim.attacker_casualty_cost_mean,
            ctx.camp_defs,
            ctx.port_defs,
            ud,
        )
        win_rate = sim.p_attacker_win

        def_total = _defender_stack_total(def_stacks)
        d_adj = min_distance_between_territories(from_tid, to_tid, td)
        is_adjacent_strike = d_adj <= 1
        small_local_stack = def_total > 0 and def_total <= 4

        # Skip terrible odds: prefer end_phase over very low win rate
        if win_rate < COMBAT_MOVE_MIN_WIN_RATE:
            continue
        # Skip negative expected value unless confident — except adjacent cleanup of small stacks
        if net < 0 and win_rate < COMBAT_MOVE_CONFIDENT_WIN_RATE:
            if not (
                is_adjacent_strike
                and small_local_stack
                and win_rate >= 0.4
            ):
                continue

        # Marginal gain: win rate and net gain added by *this* move (vs already-committed units to this target)
        marginal_win = win_rate - committed_win_rate.get(to_tid, 0.0)
        marginal_net = net - committed_net.get(to_tid, 0.0)
        # When marginal is small (saturated), down-score so we prefer opening a second attack or
        # empty conquest — not applicable when there is only one defended target (commit freely).
        saturated_win = marginal_win < COMBAT_MOVE_MARGINAL_WIN_SATURATION_THRESHOLD
        saturated_net = marginal_net < COMBAT_MOVE_MARGINAL_NET_SATURATION_THRESHOLD
        # Saturation only when multiple defended targets exist — else never shrink the only attack.
        if n_defended_destinations < 2:
            saturation_factor = 1.0
        elif saturated_win or saturated_net:
            if is_adjacent_strike and def_total <= 4:
                # Adjacent cleanup vs a tiny stack: finishing the fight is not "wasted" overlap.
                saturation_factor = 1.0
            else:
                saturation_factor = COMBAT_MOVE_SATURATION_SCORE_FACTOR
        else:
            saturation_factor = 1.0

        # Score: prefer getting an attack to confident; then prefer balance (second attack) or top gain
        if win_rate >= COMBAT_MOVE_CONFIDENT_WIN_RATE:
            score = net
            if confident_targets and to_tid not in confident_targets:
                score += 5.0  # This move makes this attack confident
        else:
            score = net * (1.0 + (COMBAT_MOVE_CONFIDENT_WIN_RATE - win_rate))
            if confident_targets:
                score += 3.0
        score *= saturation_factor

        # Explicit value for taking an adjacent tile + wiping its garrison (stops turtling vs one scout)
        if is_adjacent_strike and def_total > 0:
            gain_tile = battle_gain_if_win(to_tid, td, fd)
            score += gain_tile * win_rate * 0.35
            score += _defender_stack_power_cost_sum(def_stacks, ud) * win_rate * 0.45
        # Advance toward this blob's nearest enemy stronghold (same line incentive as mobilization)
        bi_local = tid_to_blob.get(from_tid)
        if bi_local is not None and bi_local < len(blobs):
            nearest_sh = blob_nearest_enemy_stronghold(
                blobs[bi_local], state, faction_id, fd, td
            )
            if nearest_sh:
                sh_tid, _ = nearest_sh
                d_from_sh = min_distance_between_territories(from_tid, sh_tid, td)
                d_to_sh = min_distance_between_territories(to_tid, sh_tid, td)
                if d_to_sh < d_from_sh and d_from_sh < 999:
                    score += float(d_from_sh - d_to_sh) * PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS

        if holding_origin_beats_counterattack_into_threat(
            state,
            from_tid,
            to_tid,
            att_stacks,
            def_stacks,
            faction_id,
            fd,
            td,
            ud,
        ):
            score -= COMBAT_MOVE_HOLD_PREFERRED_OVER_COUNTERATTACK_PENALTY

        # Saturated attack: don't strip a threatened origin past what the looming reach count
        # justifies, and don't stack elite pieces (power cost >= AI_ELITE_UNIT_MIN_POWER_COST)
        # when enough non-elite bodies already cover the defender count.
        pressure_from = count_enemies_that_can_reach_territory_combat_move(
            from_tid, state, faction_id, fd, td, ud
        )
        terr_from_ms = state.territories.get(from_tid)
        ids_ms = set(unit_ids)
        cav_moving = 0
        land_moving = 0
        fodder_moving = 0
        elite_moving = 0
        for u in getattr(terr_from_ms, "units", []) or []:
            if getattr(u, "instance_id", "") not in ids_ms:
                continue
            if _is_naval_unit(ud.get(u.unit_id)):
                continue
            land_moving += 1
            pc = get_unit_power_cost(ud.get(u.unit_id)) or 0
            if pc >= AI_ELITE_UNIT_MIN_POWER_COST:
                elite_moving += 1
            else:
                fodder_moving += 1
            if _is_cavalry_combat(ud, u.unit_id):
                cav_moving += 1
        if (saturated_win or saturated_net) and n_defended_destinations >= 2:
            # More attackers than this battle + looming pressure at origin warrants
            reasonable = max(1, def_total) + max(0, pressure_from - 1)
            if land_moving > reasonable:
                score -= COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY * float(
                    land_moving - reasonable
                )
            threatened = is_frontline_threatened_by_enemy_army(
                from_tid, state, faction_id, fd, td, ud
            )
            if threatened and fodder_moving >= max(1, def_total) and elite_moving > 0:
                score -= COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY * float(elite_moving)
            if threatened and cav_moving >= 2 and fodder_moving >= max(1, def_total):
                score -= COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY * float(
                    cav_moving - 1
                )

        if getattr(td.get(to_tid), "is_stronghold", False):
            bi = tid_to_blob.get(from_tid)
            if (
                bi is not None
                and bi < len(blobs)
                and is_frontline(from_tid, state, faction_id, fd, td)
            ):
                nearest = blob_nearest_enemy_stronghold(
                    blobs[bi], state, faction_id, fd, td
                )
                if nearest:
                    sh_tid, _ = nearest
                    d_attack = min_distance_between_territories(
                        from_tid, to_tid, td)
                    d_focal = min_distance_between_territories(
                        from_tid, sh_tid, td)
                    if d_attack > d_focal + 1:
                        gap = d_attack - d_focal - 1
                        score -= COMBAT_MOVE_DISTANT_STRONGHOLD_FROM_FRONTLINE_PENALTY * (
                            gap**1.25
                        )
        score -= _combat_move_range_penalty(
            from_tid,
            to_tid,
            state=state,
            faction_id=faction_id,
            fd=fd,
            td=td,
            ud=ud,
            land_moving=_land_unit_count_moving(state, from_tid, unit_ids, ud),
            outnumbered_front=outnumbered_front,
        )
        if strategic:
            bi_d = tid_to_blob.get(from_tid)
            if bi_d is not None:
                score *= strategic.combat_attack_mult_by_blob.get(bi_d, 1.0)
            score -= strategic.combat_strip_penalty_from.get(from_tid, 0.0)
            score += strategic.combat_move_bonus_to.get(to_tid, 0.0)
        candidates.append(((from_tid, to_tid, unit_ids), score))

    best_move = pick_from_score_band(candidates) if candidates else None

    if not best_move:
        if can_end_phase:
            return end_phase(faction_id)
        # Must attack with loaded boats: try any sea->land or sea->enemy_sea move that uses them
        fallback = _pick_must_attack_move(
            state, faction_id, fd, ud, td, from_to_units, must_attack_boat_ids,
            _best_charge_path, move_units,
        )
        if fallback is not None:
            return fallback
        return _must_attack_last_resort()

    from_tid, to_tid, unit_ids = best_move
    # Only include unit instances that can reach to_tid (per remaining_movement)
    unit_ids = filter_unit_instances_that_can_reach(
        state, to_tid, unit_ids, ud, td, fd
    )
    if not unit_ids:
        if can_end_phase:
            return end_phase(faction_id)
        fallback = _pick_must_attack_move(
            state, faction_id, fd, ud, td, from_to_units, must_attack_boat_ids,
            _best_charge_path, move_units,
        )
        if fallback is not None:
            return fallback
        return _must_attack_last_resort()
    # Sea -> land (offload/sea raid): only land units may move; naval units cannot go on land (backend rule)
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
            if can_end_phase:
                return end_phase(faction_id)
            fallback = _pick_must_attack_move(
                state, faction_id, fd, ud, td, from_to_units, must_attack_boat_ids,
                _best_charge_path, move_units,
            )
            if fallback is not None:
                return fallback
            return _must_attack_last_resort()
    charge_through = _best_charge_path((from_tid, to_tid), unit_ids)
    return move_units(faction_id, from_tid, to_tid, unit_ids, charge_through=charge_through)
