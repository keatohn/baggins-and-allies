"""
Movement calculations and pathfinding.
"""

import heapq
import re
from collections import deque
from collections.abc import Iterable
from typing import Any
from backend.engine.state import GameState, TerritoryState, Unit
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition, is_transportable
from backend.engine.utils import (
    effective_territory_owner,
    get_unit_faction,
    has_unit_special,
    is_aerial_unit,
    is_land_unit,
)


def _is_sea_zone(territory_def: TerritoryDefinition | None) -> bool:
    """True if this territory is a sea zone (land units cannot enter)."""
    if not territory_def:
        return False
    return getattr(territory_def, "terrain_type", "").lower() == "sea"


def _can_unit_enter_sea(unit_def: UnitDefinition | None) -> bool:
    """True if this unit can enter sea zones (naval or aerial). Aerial can fly over sea; naval can sail."""
    if not unit_def:
        return False
    arch = getattr(unit_def, "archetype", "") or ""
    tags = getattr(unit_def, "tags", []) or []
    return arch == "naval" or "naval" in tags or arch == "aerial" or "aerial" in tags


def _is_naval_only(unit_def: UnitDefinition | None) -> bool:
    """True if this unit is naval and cannot enter land (ships). Aerial can enter both land and sea."""
    if not unit_def:
        return False
    arch = getattr(unit_def, "archetype", "") or ""
    tags = getattr(unit_def, "tags", []) or []
    is_naval = arch == "naval" or "naval" in tags
    is_aerial = arch == "aerial" or "aerial" in tags
    return is_naval and not is_aerial


def canonical_sea_zone_id(tid: str) -> str:
    """Match frontend: sea_zone_n is canonical (sea_zone9 and sea_zone_9 equivalent)."""
    t = (tid or "").strip()
    m = re.match(r"^sea_zone_*(\d+)$", t, re.I)
    return f"sea_zone_{m.group(1)}" if m else t


def resolve_territory_key_in_state(
    state: GameState,
    territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> str:
    """Return a key that exists in state.territories when ids differ only by sea_zone_9 vs sea_zone9."""
    tid = (territory_id or "").strip()
    if not tid:
        return tid
    if tid in state.territories:
        return tid
    can = canonical_sea_zone_id(tid)
    if can in state.territories:
        return can
    for k in state.territories:
        if canonical_sea_zone_id(k) == can:
            return k
            
    try:
        tdef = territory_defs.get(tid) or territory_defs.get(can)
        if tdef is not None:
            for k, v in territory_defs.items():
                if v is tdef and k in state.territories:
                    return k
    except TypeError:
        pass
    return tid


def sea_zone_ids_match(a: str | None, b: str | None) -> bool:
    """True if two ids are the same sea zone (handles sea_zone_9 vs sea_zone9)."""
    if not a or not b:
        return False
    return canonical_sea_zone_id(str(a)) == canonical_sea_zone_id(str(b))


def pending_move_is_same_phase_load_into_sea(
    state: GameState,
    pm: Any,
    target_sea_zone_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    phase: str,
) -> bool:
    """
    True if pm is a pending load into target_sea_zone_id for the current phase.

    move_type is normally "load"; if missing/None (legacy or partial JSON), infer land→sea from
    from_territory/to_territory so same-phase offload/sea raid still expands passengers.
    """
    if getattr(pm, "phase", None) != phase:
        return False
    if not sea_zone_ids_match(getattr(pm, "to_territory", None), target_sea_zone_id):
        return False
    mt = getattr(pm, "move_type", None)
    if mt == "load":
        return True
    if mt is not None:
        return False
    from_raw = str(getattr(pm, "from_territory", "") or "").strip()
    to_raw = str(getattr(pm, "to_territory", "") or "").strip()
    from_key = resolve_territory_key_in_state(state, from_raw, territory_defs)
    to_key = resolve_territory_key_in_state(state, to_raw, territory_defs)
    from_def = territory_defs.get(from_key) or territory_defs.get(from_raw)
    to_def = territory_defs.get(to_key) or territory_defs.get(to_raw)
    return bool(
        from_def
        and to_def
        and not _is_sea_zone(from_def)
        and _is_sea_zone(to_def)
    )


def _is_naval_transport_boat(ud: UnitDefinition | None) -> bool:
    if not ud:
        return False
    return getattr(ud, "archetype", "") == "naval" or "naval" in (getattr(ud, "tags", []) or [])


def remaining_load_slots_on_boat(
    state: GameState,
    sea_zone_id: str,
    boat_instance_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    phase: str,
) -> int:
    """
    Empty passenger slots on a specific boat for new load declarations this phase.
    Subtracts onboard passengers and same-phase pending loads targeting this boat.
    """
    to_key = resolve_territory_key_in_state(state, sea_zone_id, territory_defs)
    to_t = state.territories.get(to_key)
    if not to_t:
        return 0
    boat_unit = next((u for u in to_t.units if u.instance_id == boat_instance_id), None)
    if not boat_unit:
        return 0
    bud = unit_defs.get(boat_unit.unit_id)
    if not _is_naval_transport_boat(bud) or get_unit_faction(boat_unit, unit_defs) != faction_id:
        return 0
    cap = getattr(bud, "transport_capacity", 0) or 0
    onboard = sum(1 for u in to_t.units if getattr(u, "loaded_onto", None) == boat_instance_id)
    pending_onto = 0
    for pm in state.pending_moves or []:
        if getattr(pm, "phase", None) != phase:
            continue
        if not pending_move_is_same_phase_load_into_sea(state, pm, sea_zone_id, territory_defs, phase):
            continue
        bid = getattr(pm, "load_onto_boat_instance_id", None) or None
        if bid != boat_instance_id:
            continue
        pending_onto += len(getattr(pm, "unit_instance_ids", None) or [])
    return max(0, cap - onboard - pending_onto)


def remaining_sea_load_passenger_slots(
    state: GameState,
    sea_zone_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    phase: str,
) -> int:
    """
    How many more land units can be declared loading into this sea zone this phase.
    Per-boat: capacity minus onboard minus explicit pending onto that boat.
    Then subtract pending loads with no boat (auto-assign pool), matching apply order packing.
    """
    to_key = resolve_territory_key_in_state(state, sea_zone_id, territory_defs)
    to_t = state.territories.get(to_key)
    if not to_t:
        return 0
    total = 0
    for boat in to_t.units:
        if getattr(boat, "loaded_onto", None):
            continue
        bud = unit_defs.get(boat.unit_id)
        if get_unit_faction(boat, unit_defs) != faction_id or not _is_naval_transport_boat(bud):
            continue
        cap = getattr(bud, "transport_capacity", 0) or 0
        onboard = sum(1 for u in to_t.units if getattr(u, "loaded_onto", None) == boat.instance_id)
        pending_onto = 0
        for pm in state.pending_moves or []:
            if getattr(pm, "phase", None) != phase:
                continue
            if not pending_move_is_same_phase_load_into_sea(state, pm, sea_zone_id, territory_defs, phase):
                continue
            bid = getattr(pm, "load_onto_boat_instance_id", None) or None
            if bid != boat.instance_id:
                continue
            pending_onto += len(getattr(pm, "unit_instance_ids", None) or [])
        total += max(0, cap - onboard - pending_onto)
    unassigned = 0
    for pm in state.pending_moves or []:
        if getattr(pm, "phase", None) != phase:
            continue
        if not pending_move_is_same_phase_load_into_sea(state, pm, sea_zone_id, territory_defs, phase):
            continue
        if getattr(pm, "load_onto_boat_instance_id", None):
            continue
        unassigned += len(getattr(pm, "unit_instance_ids", None) or [])
    return max(0, total - unassigned)


def _sea_zone_has_hostile_enemy_boats(
    state: GameState,
    sea_zone_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    to_key = resolve_territory_key_in_state(state, sea_zone_id, territory_defs)
    to_t = state.territories.get(to_key)
    if not to_t:
        return False
    current_faction_def = faction_defs.get(faction_id)
    for u in to_t.units:
        uf = get_unit_faction(u, unit_defs)
        if uf and uf != faction_id:
            ufd = faction_defs.get(uf)
            if ufd and current_faction_def and ufd.alliance != current_faction_def.alliance:
                return True
        elif not uf:
            return True
    return False


def get_forced_naval_combat_instance_ids(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> list[str]:
    """
    Defender boats in a sea zone that contains a hostile naval unit tagged as a mobilization intruder.
    That faction must fight (combat phase) or sail away this combat_move (avoid_forced_naval_combat).
    """
    raw = getattr(state, "naval_mobilization_intruder_instance_ids", None) or []
    if not raw:
        return []
    live = {u.instance_id for t in state.territories.values() for u in t.units}
    intruders = [i for i in raw if i in live]
    if not intruders:
        return []
    intruder_set = set(intruders)
    avoided = set(getattr(state, "avoided_forced_naval_combat_instance_ids", None) or [])

    seas_with_intruder: set[str] = set()
    for tid, terr in state.territories.items():
        tkey = resolve_territory_key_in_state(state, tid, territory_defs)
        tdef = territory_defs.get(tkey) or territory_defs.get(tid)
        if not tdef or not _is_sea_zone(tdef):
            continue
        if any(u.instance_id in intruder_set for u in terr.units):
            seas_with_intruder.add(tkey)

    out: list[str] = []
    for sea_id in seas_with_intruder:
        terr = state.territories.get(sea_id)
        if not terr:
            continue
        for u in terr.units:
            if u.instance_id in intruder_set:
                continue
            if u.instance_id in avoided:
                continue
            ud = unit_defs.get(u.unit_id)
            if not _is_naval_only(ud):
                continue
            if get_unit_faction(u, unit_defs) != faction_id:
                continue
            out.append(u.instance_id)
    return sorted(set(out))


def _land_adjacent_has_friendly_loadable_passengers(
    state: GameState,
    land_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> bool:
    terr = state.territories.get(land_id)
    if not terr:
        return False
    cf = faction_defs.get(faction_id)
    if terr.owner == faction_id:
        pass
    elif terr.owner:
        od = faction_defs.get(terr.owner)
        if not cf or not od or od.alliance != cf.alliance:
            return False
    else:
        return False
    for u in terr.units:
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud) or not is_transportable(ud):
            continue
        if get_unit_faction(u, unit_defs) != faction_id:
            continue
        if getattr(u, "loaded_onto", None):
            continue
        return True
    return False


def _land_adjacent_is_sea_raid_target(
    terr: TerritoryState,
    adj_def: TerritoryDefinition | None,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    if not adj_def:
        return False
    current_faction_def = faction_defs.get(faction_id)
    is_ownable = getattr(adj_def, "ownable", True)
    is_neutral = terr.owner is None
    is_enemy = terr.owner is not None and terr.owner != faction_id
    is_allied = False
    if is_enemy and current_faction_def:
        owner_def = faction_defs.get(terr.owner)
        if owner_def and owner_def.alliance == current_faction_def.alliance:
            is_allied = True
    neutral_has_enemies = False
    if is_neutral and current_faction_def:
        for u in terr.units:
            ud = unit_defs.get(u.unit_id)
            uf = ud.faction if ud else None
            ufd = faction_defs.get(uf) if uf else None
            if ufd and ufd.alliance != current_faction_def.alliance:
                neutral_has_enemies = True
                break
            if not ufd:
                neutral_has_enemies = True
                break
    if is_enemy and not is_allied:
        return True
    if is_neutral and neutral_has_enemies:
        return True
    if is_neutral and not neutral_has_enemies and is_ownable:
        return True
    return False


def empty_sea_zone_valid_for_combat_move_sail_then_load_raid(
    state: GameState,
    destination_sea_zone_id: str,
    boat_current_sea_zone_id: str,
    faction_id: str,
    boat_unit: Unit,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    phase: str,
) -> bool:
    if phase != "combat_move":
        return False
    bud = unit_defs.get(boat_unit.unit_id)
    if not _is_naval_only(bud):
        return False
    if remaining_load_slots_on_boat(
        state,
        boat_current_sea_zone_id,
        boat_unit.instance_id,
        faction_id,
        unit_defs,
        territory_defs,
        phase,
    ) <= 0:
        return False
    sea_def = territory_defs.get(
        resolve_territory_key_in_state(state, destination_sea_zone_id, territory_defs)
    )
    if not sea_def or not _is_sea_zone(sea_def):
        return False
    if _sea_zone_has_hostile_enemy_boats(
        state, destination_sea_zone_id, faction_id, unit_defs, faction_defs, territory_defs
    ):
        return False
    sea_t = state.territories.get(resolve_territory_key_in_state(state, destination_sea_zone_id, territory_defs))
    if not sea_t:
        return False
    cur_fd = faction_defs.get(faction_id)
    for u in sea_t.units:
        uf = get_unit_faction(u, unit_defs)
        if uf and uf != faction_id:
            ufd = faction_defs.get(uf)
            if ufd and cur_fd and ufd.alliance != cur_fd.alliance:
                return False
        elif not uf:
            return False
    has_passenger = False
    has_raid = False
    for adj_land in getattr(sea_def, "adjacent", []) or []:
        ld = territory_defs.get(adj_land)
        if not ld or _is_sea_zone(ld):
            continue
        adj_terr = state.territories.get(adj_land)
        if not adj_terr:
            continue
        if _land_adjacent_has_friendly_loadable_passengers(
            state, adj_land, faction_id, unit_defs, faction_defs
        ):
            has_passenger = True
        if _land_adjacent_is_sea_raid_target(
            adj_terr, ld, faction_id, faction_defs, unit_defs
        ):
            has_raid = True
    return has_passenger and has_raid


def sort_sea_zone_ids_numerically(sea_ids: Iterable[str]) -> list[str]:
    """Order sea_zone_N by N ascending (sea_zone_2 before sea_zone_10, unlike plain string sort)."""

    def sort_key(sid: str) -> tuple[int, str]:
        t = (sid or "").strip()
        m = re.match(r"^sea_zone_*(\d+)$", t, re.I)
        if m:
            return (int(m.group(1)), t.lower())
        return (10**9, t.lower())

    return sorted(sea_ids, key=sort_key)


def expand_sea_offload_instance_ids(
    state: GameState,
    from_id: str,
    to_id: str,
    unit_instance_ids: list,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_id: str,
) -> list[str]:
    """
    Sea -> land: the client often sends only boat IDs (dragging the ship token). Append:
    - embarked passengers (loaded_onto selected boats), and
    - same-phase pending loads into this sea (passengers still on land),

    even when the request already lists embarked passengers (do not return early — pending loads
    must still be merged or sea-offload validation fails).
    """
    ids = [str(x).strip() for x in unit_instance_ids if x and str(x).strip()]
    if not ids:
        return ids

    resolved_from = resolve_territory_key_in_state(state, str(from_id or "").strip(), territory_defs)
    resolved_to = resolve_territory_key_in_state(state, str(to_id or "").strip(), territory_defs)
    from_def = territory_defs.get(resolved_from) or territory_defs.get(str(from_id or "").strip())
    to_def = territory_defs.get(resolved_to) or territory_defs.get(str(to_id or "").strip())
    # From must be sea. Allow both sea→land (offload) and sea→sea (sail) so pending loads merge
    # into the same stack (passengers still on land until pending applies).
    if not from_def or not to_def or not _is_sea_zone(from_def):
        return ids

    terr = state.territories.get(resolved_from)
    if not terr:
        return ids
    by_id = {u.instance_id: u for u in terr.units}
    boat_ids_in_request: list[str] = []
    for iid in ids:
        u = by_id.get(iid)
        if not u:
            continue
        ud = unit_defs.get(u.unit_id)
        if _is_naval_only(ud) and get_unit_faction(u, unit_defs) == faction_id:
            boat_ids_in_request.append(iid)

    # Need at least one friendly naval driver in the request to know which boat(s) we're offloading from.
    if not boat_ids_in_request:
        return ids

    boat_set = set(boat_ids_in_request)
    added: list[str] = []
    for u in terr.units:
        if get_unit_faction(u, unit_defs) != faction_id:
            continue
        lo = getattr(u, "loaded_onto", None)
        if lo and lo in boat_set and u.instance_id not in ids:
            added.append(u.instance_id)

    # Load declared this phase but not applied yet: passengers are still on land with no loaded_onto.
    phase = getattr(state, "phase", "") or ""
    for pm in getattr(state, "pending_moves", []) or []:
        if not pending_move_is_same_phase_load_into_sea(
            state, pm, resolved_from, territory_defs, phase
        ):
            continue
        boat = getattr(pm, "load_onto_boat_instance_id", None) or None
        # Load without a specific boat uses zone capacity; still counts for same-phase offload expansion.
        if boat is not None and boat not in boat_set:
            continue
        for pid in getattr(pm, "unit_instance_ids", []) or []:
            ps = str(pid).strip()
            if ps and ps not in ids and ps not in added:
                added.append(ps)

    if not added:
        return ids
    combined = ids + added
    return list(dict.fromkeys(combined))


def resolve_unit_for_move_declaration(
    state: GameState,
    from_id: str,
    instance_id: str,
    phase: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> Unit | None:
    """
    Units moving *from* a sea zone are usually listed in that territory.
    If a load into that sea is still pending, passengers still sit on land — resolve from the load's from_territory.
    """
    tid = str(instance_id).strip()
    if not tid:
        return None
    terr = state.territories.get(from_id)
    if terr:
        u = next((x for x in terr.units if x.instance_id == tid), None)
        if u:
            return u
    from_def = territory_defs.get(from_id)
    if not from_def or not _is_sea_zone(from_def):
        return None
    for pm in getattr(state, "pending_moves", []) or []:
        if not pending_move_is_same_phase_load_into_sea(
            state, pm, from_id, territory_defs, phase
        ):
            continue
        if tid not in (getattr(pm, "unit_instance_ids", None) or []):
            continue
        land_key = resolve_territory_key_in_state(
            state, str(getattr(pm, "from_territory", "") or "").strip(), territory_defs
        )
        land = state.territories.get(land_key) or state.territories.get(
            str(getattr(pm, "from_territory", "") or "").strip()
        )
        if land:
            u = next((x for x in land.units if x.instance_id == tid), None)
            if u:
                return u
    return None


def instance_allowed_in_new_move_from_territory(
    state: GameState,
    instance_id: str,
    new_from_territory: str,
    phase: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """
    False if this instance is already committed to another pending move this phase that is not
    a load *into* new_from_territory (chained load then offload/sail from same sea in one phase).
    """
    iid = str(instance_id).strip()
    for pm in getattr(state, "pending_moves", []) or []:
        if getattr(pm, "phase", None) != phase:
            continue
        if iid not in (getattr(pm, "unit_instance_ids", None) or []):
            continue
        if pending_move_is_same_phase_load_into_sea(
            state, pm, new_from_territory, territory_defs, phase
        ):
            continue
        return False
    return True


def _adjacent_ids(
    territory_def: TerritoryDefinition | None,
    is_aerial: bool = False,
) -> list[str]:
    """Neighbors for movement: adjacent + aerial_adjacent when is_aerial (deduped, order preserved)."""
    if not territory_def:
        return []
    adj = list(territory_def.adjacent)
    if is_aerial:
        extra = getattr(territory_def, "aerial_adjacent", None) or []
        seen = set(adj)
        for tid in extra:
            if tid not in seen:
                seen.add(tid)
                adj.append(tid)
    return adj


def _has_adjacent_only_land_path(
    origin: str,
    dest: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """True if dest is reachable from origin using only regular ground adjacency (no ford-only edges)."""
    if origin == dest:
        return True
    queue: deque[str] = deque([origin])
    visited = {origin}
    while queue:
        tid = queue.popleft()
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        for nxt in getattr(tdef, "adjacent", []) or []:
            if nxt == dest:
                return True
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return False


def min_ford_edges_for_land_move(
    origin: str,
    dest: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> int | None:
    """
    Minimum number of ford-only edges (ford_adjacent but not in adjacent) on any land path.
    Returns 0 if an adjacent-only path exists; None if unreachable.
    """
    if origin == dest:
        return 0
    if _has_adjacent_only_land_path(origin, dest, territory_defs):
        return 0
    INF = 10**9
    pq: list[tuple[int, str]] = [(0, origin)]
    best: dict[str, int] = {origin: 0}
    while pq:
        fc, tid = heapq.heappop(pq)
        if fc != best.get(tid, INF):
            continue
        if tid == dest:
            return fc
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        adj = set(getattr(tdef, "adjacent", []) or [])
        for nxt in adj:
            nfc = fc
            if nfc < best.get(nxt, INF):
                best[nxt] = nfc
                heapq.heappush(pq, (nfc, nxt))
        for nxt in getattr(tdef, "ford_adjacent", []) or []:
            if nxt in adj:
                continue
            nfc = fc + 1
            if nfc < best.get(nxt, INF):
                best[nxt] = nfc
                heapq.heappush(pq, (nfc, nxt))
    return None if dest not in best else best[dest]


def direct_ford_only_land_pair(
    a: str,
    b: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """
    True if a one-step crossing between these territories uses a ford-only link (ford_adjacent on one
    side but not also listed in adjacent). That river ford is a movement shortcut (often 1 MP) even
    when a longer adjacent-only detour exists between the same two ids; those moves consume escort
    and need a crosser lead like any ford-only edge.
    """
    ta = territory_defs.get(a)
    tb = territory_defs.get(b)
    if not ta or not tb:
        return False
    if _is_sea_zone(ta) or _is_sea_zone(tb):
        return False
    adj_a = set(getattr(ta, "adjacent", []) or [])
    adj_b = set(getattr(tb, "adjacent", []) or [])
    fa = getattr(ta, "ford_adjacent", []) or []
    fb = getattr(tb, "ford_adjacent", []) or []
    if b in fa and b not in adj_a:
        return True
    if a in fb and a not in adj_b:
        return True
    return False


def ford_shortcut_requires_escort_lead(
    okey: str,
    dkey: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """True if declaring O→D is a ford escort move: global min ford ≥1, or direct river-ford pair."""
    mf = min_ford_edges_for_land_move(okey, dkey, territory_defs)
    if mf is not None and mf >= 1:
        return True
    return direct_ford_only_land_pair(okey, dkey, territory_defs)


def total_ford_escort_capacity(
    state: GameState,
    territory_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> int:
    """Sum of transport_capacity on ford_crosser units in territory owned by faction."""
    tkey = resolve_territory_key_in_state(state, territory_id, territory_defs)
    t = state.territories.get(tkey)
    if not t:
        return 0
    total = 0
    for u in t.units:
        if get_unit_faction(u, unit_defs) != faction_id:
            continue
        ud = unit_defs.get(u.unit_id)
        if not has_unit_special(ud, "ford_crosser"):
            continue
        total += int(getattr(ud, "transport_capacity", 0) or 0)
    return total


def pending_ford_escort_usage_from_origin(
    state: GameState,
    origin: str,
    phase: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    exclude_instance_ids: set[str] | None = None,
) -> int:
    """Ford escort slots consumed by pending land moves from origin this phase."""
    exclude = exclude_instance_ids or set()
    okey = resolve_territory_key_in_state(state, origin, territory_defs)
    total = 0
    for pm in state.pending_moves or []:
        if getattr(pm, "phase", None) != phase:
            continue
        fr = resolve_territory_key_in_state(
            state, getattr(pm, "from_territory", "") or "", territory_defs
        )
        if fr != okey:
            continue
        mt = getattr(pm, "move_type", None)
        if mt in ("load", "offload", "sail"):
            continue
        instance_ids = [
            i for i in (getattr(pm, "unit_instance_ids", None) or []) if i not in exclude
        ]
        if not instance_ids:
            continue
        to = resolve_territory_key_in_state(
            state, getattr(pm, "to_territory", "") or "", territory_defs
        )
        total += land_move_ford_escort_cost_for_instances(
            okey, to, instance_ids, state, unit_defs, territory_defs
        )
    return total


def pending_ford_crosser_lead_move_from_origin(
    state: GameState,
    origin: str,
    phase: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """
    True if some pending land move from origin this phase includes at least one ford_crosser
    and declares a ford escort destination (min_ford ≥ 1 on some path, or a direct river-ford pair).

    Non-ford-crosser land units may only spend pooled ford escort capacity after such a lead exists,
    so a ford crosser declares across the ford first (then others may use transport_capacity).
    """
    okey = resolve_territory_key_in_state(state, origin, territory_defs)
    for pm in getattr(state, "pending_moves", None) or []:
        if getattr(pm, "phase", None) != phase:
            continue
        fr = resolve_territory_key_in_state(
            state, str(getattr(pm, "from_territory", "") or "").strip(), territory_defs
        )
        if fr != okey:
            continue
        mt = getattr(pm, "move_type", None)
        if mt in ("load", "offload", "sail"):
            continue
        instance_ids = getattr(pm, "unit_instance_ids", None) or []
        if not instance_ids:
            continue
        to = resolve_territory_key_in_state(
            state, str(getattr(pm, "to_territory", "") or "").strip(), territory_defs
        )
        if not ford_shortcut_requires_escort_lead(fr, to, territory_defs):
            continue
        terr = state.territories.get(fr)
        if not terr:
            continue
        by_iid = {u.instance_id: u for u in terr.units}
        for iid in instance_ids:
            u = by_iid.get(iid)
            if not u:
                continue
            ud = unit_defs.get(u.unit_id)
            if has_unit_special(ud, "ford_crosser"):
                return True
        # Solo ford-crosser declaration: primary_unit_id is authoritative if stack lookup ever misses (ordering/UI edge cases).
        pu = (getattr(pm, "primary_unit_id", None) or "").strip()
        if (
            pu
            and len(instance_ids) == 1
            and has_unit_special(unit_defs.get(pu), "ford_crosser")
        ):
            return True
    return False


def land_move_ford_escort_cost_for_instances(
    origin: str,
    dest: str,
    instance_ids: list[str],
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> int:
    """
    Escort slots needed for this move: each non-ford-crosser land non-aerial transportable unit pays
    min_ford_edges over paths. A direct river-ford shortcut (ford_adjacent only) costs 1 per escort
    even when a longer adjacent-only detour exists between the same territories.
    """
    okey = resolve_territory_key_in_state(state, origin, territory_defs)
    t = state.territories.get(okey)
    if not t:
        return 0
    dkey = resolve_territory_key_in_state(state, dest, territory_defs)
    min_ford = min_ford_edges_for_land_move(okey, dkey, territory_defs)
    if min_ford is None:
        return 0
    if min_ford == 0 and direct_ford_only_land_pair(okey, dkey, territory_defs):
        min_ford = 1
    if min_ford == 0:
        return 0
    n = 0
    for iid in instance_ids:
        u = next((x for x in t.units if x.instance_id == iid), None)
        if not u:
            continue
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud):
            continue
        if is_aerial_unit(ud):
            continue
        if has_unit_special(ud, "ford_crosser"):
            continue
        if not is_transportable(ud):
            continue
        n += 1
    return n * min_ford


def remaining_ford_escort_slots(
    state: GameState,
    origin: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    phase: str,
    exclude_instance_ids: set[str] | None = None,
) -> int:
    cap = total_ford_escort_capacity(state, origin, faction_id, unit_defs, territory_defs)
    used = pending_ford_escort_usage_from_origin(
        state, origin, phase, faction_id, unit_defs, territory_defs, exclude_instance_ids
    )
    return max(0, cap - used)


def _land_adjacent_and_ford_edges(territory_def: TerritoryDefinition) -> list[str]:
    """Ground connectivity (adjacent + ford_adjacent, deduped). For shortest-path checks."""
    adj = list(territory_def.adjacent)
    seen = set(adj)
    for fid in getattr(territory_def, "ford_adjacent", []) or []:
        if fid not in seen:
            seen.add(fid)
            adj.append(fid)
    return adj


def _land_move_neighbors_with_ford(
    territory_def: TerritoryDefinition,
    is_aerial: bool,
    is_ford_crosser: bool,
) -> list[tuple[str, bool]]:
    """
    Neighbors for one BFS step. (neighbor_id, is_ford_only_for_escort_budget).
    Aerial uses aerial_adjacent only (ford is duplicated there in data). Ford crossers treat ford edges as free.
    Ford-only edges (ford_adjacent but not adjacent) always spend pooled escort for non–ford-crossers —
    river fords are movement shortcuts even when a longer land detour exists overall.
    """
    if is_aerial:
        return [(aid, False) for aid in _adjacent_ids(territory_def, True)]
    adj = list(territory_def.adjacent)
    adj_set = set(adj)
    out: list[tuple[str, bool]] = [(a, False) for a in adj]
    for fid in getattr(territory_def, "ford_adjacent", []) or []:
        if fid in adj_set:
            continue
        if is_ford_crosser:
            out.append((fid, False))
        else:
            out.append((fid, True))
    return out


def _is_friendly_territory_for_landing(
    territory: TerritoryState,
    current_faction: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    state: GameState | None = None,
    territory_id: str | None = None,
) -> bool:
    """
    True if an aerial unit can land here. Only allied-owned territory counts;
    neutral (unowned) territory is not valid for landing (so aerials must be able to reach allied territory).
    """
    if state is not None and territory_id:
        owner = effective_territory_owner(state, territory_id)
    else:
        owner = territory.owner
    if owner is None:
        return False

    current_alliance = faction_defs.get(current_faction)
    current_alliance = current_alliance.alliance if current_alliance else ""
    owner_alliance = faction_defs.get(owner)
    owner_alliance = owner_alliance.alliance if owner_alliance else ""
    return owner_alliance == current_alliance


def is_friendly_territory_for_landing(
    territory: TerritoryState,
    current_faction: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    state: GameState | None = None,
    territory_id: str | None = None,
) -> bool:
    """True if an aerial unit can land/stay here (owned by us or our alliance only; neutral does not count)."""
    return _is_friendly_territory_for_landing(
        territory, current_faction, faction_defs, unit_defs,
        state=state, territory_id=territory_id,
    )


def _can_reach_friendly_from(
    from_territory_id: str,
    moves_left: int,
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    current_faction: str,
    for_aerial: bool = False,
) -> bool:
    """
    BFS from from_territory_id with moves_left steps. Returns True if any territory
    reachable within that range is friendly for landing (aerial must be able to return).
    When for_aerial, use aerial_adjacent so flying over mountains/rivers is allowed.
    """
    if moves_left < 0:
        return False
    from_territory = state.territories.get(from_territory_id)
    if from_territory and _is_friendly_territory_for_landing(
        from_territory, current_faction, faction_defs, unit_defs,
        state=state, territory_id=from_territory_id,
    ):
        return True
    if moves_left == 0:
        return False
    visited = {from_territory_id}
    queue: deque[tuple[str, int]] = deque([(from_territory_id, 0)])
    while queue:
        tid, steps = queue.popleft()
        if steps >= moves_left:
            continue
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        for adj_id in _adjacent_ids(tdef, for_aerial):
            if adj_id in visited:
                continue
            visited.add(adj_id)
            adj_territory = state.territories.get(adj_id)
            if adj_territory and _is_friendly_territory_for_landing(
                adj_territory, current_faction, faction_defs, unit_defs,
                state=state, territory_id=adj_id,
            ):
                return True
            queue.append((adj_id, steps + 1))
    return False


def get_reachable_territories_for_unit(
    unit: Unit,
    start: str,
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    phase: str,
    acting_faction_id: str | None = None,
    exclude_instance_ids_from_ford_pending: set[str] | None = None,
    same_move_includes_ford_crosser: bool = False,
) -> tuple[dict[str, int], dict[str, list[list[str]]]]:
    """
    Calculate all territories reachable by a specific unit instance from a starting territory.
    Returns (reachable_dict, charge_routes).
    - reachable_dict: territory_id -> distance (cost to reach).
    - charge_routes: for cavalry in combat_move, territory_id -> list of charge_through paths
      (each path = list of empty enemy territory IDs passed through). Empty dict for non-cavalry.

    acting_faction_id: if set, treat this faction as the moving player (friend/enemy/neutral
    edges). Defaults to state.current_faction. Used for AI threat checks without mutating state.

    exclude_instance_ids_from_ford_pending: pending moves containing these instance IDs do not
    count toward ford escort usage (so the current move can replace a pending declaration).

    same_move_includes_ford_crosser: True when this pathfinding is for a land stack that also
    includes at least one ford_crosser in the same move_units action. Escort units then treat the
    pooled ford capacity as available without a prior pending crosser-only declaration.

    Rules:
    - BFS up to remaining_movement
    - Cavalry (charging): can pass through empty enemy, empty unowned, or empty friendly/allied territory in combat_move; enemy/unowned are conquered, friendly/allied are not.
    - Aerial: can pass through any enemy/neutral in both phases.
    - Empty unownable: can be passed through in combat_move to reach an enemy in 2 moves; not valid as final destination (filtered out).
    - Empty/friendly neutral: enqueued for all units.
    - Land ford: ford_adjacent edges require ford_crosser or remaining escort capacity (transport_capacity pooled).
    """
    unit_def = unit_defs.get(unit.unit_id)
    if not unit_def:
        return {}, {}

    # Coerce so BFS never compares int distances to str/None (e.g. deepcopy/hypo state edge cases).
    try:
        max_move = int(getattr(unit, "remaining_movement", 0) or 0)
    except (TypeError, ValueError):
        max_move = 0
    if max_move <= 0:
        return {}, {}

    cf = (
        acting_faction_id
        if acting_faction_id is not None
        else getattr(state, "current_faction", None)
    ) or ""

    is_aerial = (
        getattr(unit_def, "archetype", "") == "aerial"
        or "aerial" in getattr(unit_def, "tags", [])
    )
    is_cavalry = (
        getattr(unit_def, "archetype", "") == "cavalry"
        or "cavalry" in getattr(unit_def, "tags", [])
    )
    can_enter_enemy = phase == "combat_move"
    current_faction_def = faction_defs.get(cf)

    forced_naval_ids: set[str] = set()
    if phase == "combat_move" and _is_naval_only(unit_def):
        forced_naval_ids = set(
            get_forced_naval_combat_instance_ids(
                state, cf, unit_defs, territory_defs, faction_defs
            )
        )

    is_ford_crosser = has_unit_special(unit_def, "ford_crosser")
    uses_ford_budget = (
        is_land_unit(unit_def)
        and not is_aerial
        and not is_ford_crosser
        and is_transportable(unit_def)
    )
    ford_exclude = exclude_instance_ids_from_ford_pending or set()
    remaining_ford = remaining_ford_escort_slots(
        state, start, cf, unit_defs, territory_defs, phase, ford_exclude
    )
    if (
        uses_ford_budget
        and not pending_ford_crosser_lead_move_from_origin(
            state, start, phase, unit_defs, territory_defs
        )
        and not same_move_includes_ford_crosser
    ):
        remaining_ford = 0

    reachable = {}  # territory_id -> distance
    charge_routes: dict[str, list[list[str]]] = {}  # territory_id -> list of charge_through paths
    # For cavalry we track (tid, charge) to allow multiple paths; key = (tid, tuple(charge))
    # When uses_ford_budget, key includes ford_used along the path (ford-only edges).
    visited: dict[tuple, int] = {}
    queue: deque[tuple[str, int, list[str], int]] = deque([(start, 0, [], 0)])

    while queue:
        territory_id, distance, charge, ford_used = queue.popleft()
        charge_key = (territory_id, tuple(charge))

        # Only record as reachable if within movement range (never allow > remaining_movement)
        if distance > 0 and distance <= max_move:
            reachable[territory_id] = min(reachable.get(territory_id, 999), distance)
            if is_cavalry and can_enter_enemy:
                # Via path must never include the destination (no "Via Pelennor" when moving to Pelennor)
                via_path = [t for t in charge if t != territory_id]
                charge_routes.setdefault(territory_id, [])
                if via_path not in charge_routes[territory_id]:
                    charge_routes[territory_id].append(via_path)

        if distance >= max_move:
            continue

        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue

        neighbors = _land_move_neighbors_with_ford(
            territory_def, is_aerial, is_ford_crosser
        )
        for adjacent_id, is_ford_budget_step in neighbors:
            # Only transportable land (plus crossers/aerials via neighbor rules) may use ford-only edges / escort pool.
            if (
                is_ford_budget_step
                and not is_ford_crosser
                and not is_aerial
                and not is_transportable(unit_def)
            ):
                continue
            if is_ford_budget_step and ford_used + 1 > remaining_ford:
                continue
            new_fu = ford_used + (1 if is_ford_budget_step else 0)
            new_distance = distance + 1
            adj_def = territory_defs.get(adjacent_id)
            if _is_sea_zone(adj_def) and not _can_unit_enter_sea(unit_def):
                # Land unit can load into adjacent sea zone (cost 1); add to reachable but do not expand from sea
                if new_distance <= max_move and adjacent_id not in reachable:
                    reachable[adjacent_id] = new_distance
                continue
            if not _is_sea_zone(adj_def) and _is_naval_only(unit_def):
                continue
            adjacent_territory = state.territories.get(adjacent_id)
            if not adjacent_territory:
                continue

            # Naval movement: sea zones with enemy boats are hostile — valid destination (attack) but do not sail through
            if _is_naval_only(unit_def) and _is_sea_zone(adj_def):
                has_enemy_boats = False
                for u in adjacent_territory.units:
                    uf = get_unit_faction(u, unit_defs)
                    if uf and uf != cf and current_faction_def:
                        ufd = faction_defs.get(uf)
                        if ufd and ufd.alliance != current_faction_def.alliance:
                            has_enemy_boats = True
                            break
                    elif not uf:
                        has_enemy_boats = True
                        break
                if has_enemy_boats:
                    if new_distance <= max_move and (
                        adjacent_id not in reachable or new_distance < reachable[adjacent_id]
                    ):
                        reachable[adjacent_id] = new_distance
                    continue

            eo = effective_territory_owner(state, adjacent_id)
            is_neutral = eo is None
            is_enemy_territory = eo is not None and eo != cf
            is_allied_territory = False
            if is_enemy_territory and current_faction_def:
                owner_faction_def = faction_defs.get(eo)
                if owner_faction_def and owner_faction_def.alliance == current_faction_def.alliance:
                    is_allied_territory = True

            neutral_has_enemies = False
            if is_neutral and current_faction_def:
                for u in adjacent_territory.units:
                    ud = unit_defs.get(u.unit_id)
                    unit_faction = ud.faction if ud else None
                    unit_faction_def = faction_defs.get(unit_faction) if unit_faction else None
                    if unit_faction_def and unit_faction_def.alliance != current_faction_def.alliance:
                        neutral_has_enemies = True
                        break
                    elif not unit_faction_def:
                        neutral_has_enemies = True
                        break

            adjacent_ownable = getattr(adj_def, "ownable", True)
            adjacent_has_any_units = len(adjacent_territory.units) > 0
            # Cavalry "open space" for charge: EMPTY (enemy or unowned ownable) OR friendly/allied. Can charge through any of these.
            adjacent_empty_enemy = (
                is_enemy_territory and not is_allied_territory
                and not adjacent_has_any_units
            )
            adjacent_empty_unowned = (
                is_neutral and not neutral_has_enemies and not adjacent_has_any_units and adjacent_ownable
            )
            adjacent_friendly_or_allied = eo == cf or is_allied_territory
            can_charge_through = (
                adjacent_empty_enemy or adjacent_empty_unowned or adjacent_friendly_or_allied
            )
            new_charge = charge + [adjacent_id] if (is_cavalry and can_enter_enemy and can_charge_through) else charge
            if uses_ford_budget:
                adj_key = (adjacent_id, tuple(new_charge), new_fu)
            else:
                adj_key = (adjacent_id, tuple(new_charge))

            can_pass = True
            if is_enemy_territory and not is_allied_territory and not can_enter_enemy and not is_aerial:
                can_pass = False
            if is_enemy_territory and not is_allied_territory and can_enter_enemy and not is_aerial and not (is_cavalry and can_charge_through):
                can_pass = False
            if is_neutral and phase == "combat_move" and neutral_has_enemies and not is_aerial:
                can_pass = False
            if can_pass:
                if adj_key not in visited or new_distance < visited[adj_key]:
                    visited[adj_key] = new_distance
                    queue.append((adjacent_id, new_distance, new_charge, new_fu))
            elif phase == "combat_move" and not is_aerial and new_distance <= max_move:
                if (is_enemy_territory and not is_allied_territory) or (is_neutral and neutral_has_enemies):
                    if adjacent_id not in reachable or new_distance < reachable[adjacent_id]:
                        reachable[adjacent_id] = new_distance
                    if is_cavalry:
                        via_path = [t for t in charge if t != adjacent_id]
                        charge_routes.setdefault(adjacent_id, [])
                        if via_path not in charge_routes[adjacent_id]:
                            charge_routes[adjacent_id].append(via_path)

    # Filter destinations based on phase and unit type
    filtered_reachable = {}
    for territory_id, dist in reachable.items():
        territory = state.territories.get(territory_id)
        if not territory:
            continue
        territory_def = territory_defs.get(territory_id)
        is_ownable = getattr(territory_def, "ownable", True)

        eff_o = effective_territory_owner(state, territory_id)
        is_neutral = eff_o is None
        is_enemy_territory = eff_o is not None and eff_o != cf

        # Check if it's allied territory
        is_allied_territory = False
        if is_enemy_territory and current_faction_def:
            owner_faction_def = faction_defs.get(eff_o)
            if owner_faction_def and owner_faction_def.alliance == current_faction_def.alliance:
                is_allied_territory = True

        # Check if neutral territory has enemy units
        neutral_has_enemies = False
        if is_neutral and current_faction_def:
            for u in territory.units:
                ud = unit_defs.get(u.unit_id)
                unit_faction = ud.faction if ud else None
                unit_faction_def = faction_defs.get(unit_faction) if unit_faction else None
                if unit_faction_def and unit_faction_def.alliance != current_faction_def.alliance:
                    neutral_has_enemies = True
                    break
                elif not unit_faction_def:
                    neutral_has_enemies = True
                    break

        # Apply phase-specific filters
        territory_def_for_filter = territory_defs.get(territory_id)
        is_sea = territory_def_for_filter and _is_sea_zone(territory_def_for_filter)
        if phase == "combat_move":
            # Combat move: enemy territory; neutral with enemies (attack); empty neutral ownable (conquer); adjacent sea zone (load).
            # Aerial: can only move into territories that have units to attack. No empty destinations.
            # Naval: sea zones with enemy units (naval combat); also allow empty reachable sea zones so sail+offload/sea raid works.
            if is_sea and _is_naval_only(unit_def):
                if is_enemy_territory and not is_allied_territory and len(territory.units) > 0:
                    filtered_reachable[territory_id] = dist
                elif is_neutral and neutral_has_enemies:
                    filtered_reachable[territory_id] = dist
                elif empty_sea_zone_valid_for_combat_move_sail_then_load_raid(
                    state,
                    territory_id,
                    start,
                    cf,
                    unit,
                    unit_defs,
                    territory_defs,
                    faction_defs,
                    phase,
                ):
                    filtered_reachable[territory_id] = dist
                elif (
                    unit.instance_id in forced_naval_ids
                    and dist == 1
                    and not _sea_zone_has_hostile_enemy_boats(
                        state, territory_id, cf, unit_defs, faction_defs, territory_defs
                    )
                ):
                    filtered_reachable[territory_id] = dist
            elif is_sea and not _can_unit_enter_sea(unit_def) and dist == 1:
                # Land unit loading into adjacent sea zone (transportable only; hide when no slots left)
                if (
                    is_land_unit(unit_def)
                    and is_transportable(unit_def)
                    and remaining_sea_load_passenger_slots(
                        state, territory_id, cf, unit_defs, territory_defs, phase
                    )
                    > 0
                ):
                    filtered_reachable[territory_id] = dist
            elif is_sea and is_aerial:
                # Aerial vs ships: must keep enough movement to reach friendly land after (same check as land attacks).
                if len(territory.units) == 0:
                    pass
                elif not _can_reach_friendly_from(
                    territory_id,
                    max_move - dist,
                    state,
                    territory_defs,
                    faction_defs,
                    unit_defs,
                    cf,
                    for_aerial=True,
                ):
                    pass
                elif (is_enemy_territory and not is_allied_territory) or (
                    is_neutral and neutral_has_enemies
                ):
                    filtered_reachable[territory_id] = dist
            elif is_enemy_territory and not is_allied_territory:
                territory_has_units = len(territory.units) > 0
                if is_aerial:
                    if territory_has_units and _can_reach_friendly_from(
                        territory_id, max_move - dist,
                        state, territory_defs, faction_defs, unit_defs, cf,
                        for_aerial=True,
                    ):
                        filtered_reachable[territory_id] = dist
                else:
                    filtered_reachable[territory_id] = dist
            elif is_neutral and neutral_has_enemies:
                # Sea zones with enemy units, or hostile neutrals (e.g. goblins). Aerial: allow if can reach friendly (land) after.
                if is_aerial:
                    if _can_reach_friendly_from(
                        territory_id, max_move - dist,
                        state, territory_defs, faction_defs, unit_defs, cf,
                        for_aerial=True,
                    ):
                        filtered_reachable[territory_id] = dist
                else:
                    filtered_reachable[territory_id] = dist  # hostile neutral (e.g. goblins, cave trolls) - attack it
            elif is_neutral and not neutral_has_enemies and is_ownable:
                # Empty unowned ownable: ground can move in and capture. Aerial cannot (no combat move into empty).
                if not is_aerial:
                    filtered_reachable[territory_id] = dist
            # Note: friendly/allied territories are NOT valid combat_move destinations (except load into sea)
        elif phase == "non_combat_move":
            # Non-combat move: friendly or allied; empty unownable (pass-through); adjacent sea zone (load).
            territory_def_ncm = territory_defs.get(territory_id)
            is_sea_ncm = territory_def_ncm and _is_sea_zone(territory_def_ncm)
            if is_sea_ncm and _is_naval_only(unit_def):
                # Naval: sail to any reachable sea zone
                filtered_reachable[territory_id] = dist
            elif is_sea_ncm and not _can_unit_enter_sea(unit_def) and dist == 1:
                if (
                    is_land_unit(unit_def)
                    and is_transportable(unit_def)
                    and remaining_sea_load_passenger_slots(
                        state, territory_id, cf, unit_defs, territory_defs, phase
                    )
                    > 0
                ):
                    filtered_reachable[territory_id] = dist
            elif is_neutral:
                if not neutral_has_enemies and not is_ownable:
                    filtered_reachable[territory_id] = dist  # empty unownable neutral only (e.g. pass-through)
            elif not is_enemy_territory or is_allied_territory:
                # Friendly or allied only; exclude ownable neutral (conquest is combat_move only)
                if is_neutral and is_ownable:
                    pass
                else:
                    filtered_reachable[territory_id] = dist
        else:
            # Other phases: include all reachable
            filtered_reachable[territory_id] = dist

    # Combat move: for naval-only units, add land territories adjacent to *any* reachable sea zone as sea-raid targets
    # (use reachable, not filtered_reachable: sea zones may not be in filtered_reachable for naval, but they are in reachable)
    if phase == "combat_move" and _is_naval_only(unit_def):
        sea_raid_land: dict[str, int] = {}
        for sea_id, dist in list(reachable.items()):
            sea_def = territory_defs.get(sea_id)
            if not sea_def or not _is_sea_zone(sea_def):
                continue
            for adj_id in sea_def.adjacent:
                adj_def = territory_defs.get(adj_id)
                if not adj_def or _is_sea_zone(adj_def):
                    continue
                adj_territory = state.territories.get(adj_id)
                if not adj_territory:
                    continue
                is_ownable = getattr(adj_def, "ownable", True)
                adj_eo = effective_territory_owner(state, adj_id)
                is_neutral = adj_eo is None
                is_enemy = adj_eo is not None and adj_eo != cf
                is_allied = False
                if is_enemy and current_faction_def:
                    owner_def = faction_defs.get(adj_eo)
                    if owner_def and owner_def.alliance == current_faction_def.alliance:
                        is_allied = True
                neutral_has_enemies = False
                if is_neutral and current_faction_def:
                    for u in adj_territory.units:
                        ud = unit_defs.get(u.unit_id)
                        uf = ud.faction if ud else None
                        ufd = faction_defs.get(uf) if uf else None
                        if ufd and ufd.alliance != current_faction_def.alliance:
                            neutral_has_enemies = True
                            break
                        if not ufd:
                            neutral_has_enemies = True
                            break
                if is_enemy and not is_allied:
                    sea_raid_land[adj_id] = min(sea_raid_land.get(adj_id, 999), dist)
                elif is_neutral and neutral_has_enemies:
                    sea_raid_land[adj_id] = min(sea_raid_land.get(adj_id, 999), dist)
                elif is_neutral and not neutral_has_enemies and is_ownable:
                    sea_raid_land[adj_id] = min(sea_raid_land.get(adj_id, 999), dist)
        for tid, d in sea_raid_land.items():
            if tid not in filtered_reachable or d < filtered_reachable[tid]:
                filtered_reachable[tid] = d

    # Source territory is never a valid move destination (cannot move from X to X)
    filtered_reachable.pop(start, None)

    # Restrict charge_routes to only destinations that are in filtered_reachable
    charge_routes_filtered = {
        tid: paths for tid, paths in charge_routes.items()
        if tid in filtered_reachable
    }
    return filtered_reachable, charge_routes_filtered


def get_charge_reachable_over_moves(
    start_territory_id: str,
    state: GameState,
    movement_range: int,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> set[str]:
    """
    BFS from start_territory_id with movement_range steps through cavalry "open space"
    (empty enemy, empty neutral ownable, friendly/allied). Returns the set of territory IDs
    that are *conquerable* (empty enemy or empty neutral ownable) within that range.
    Used for multi-turn charge lookahead: from where we end this turn, what can we charge to in the next 1–2 turns?
    Ground adjacency only (no sea, no aerial).
    """
    if movement_range <= 0:
        return set()
    current_faction_def = faction_defs.get(faction_id)
    conquerable: set[str] = set()
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_territory_id, 0)])
    while queue:
        tid, steps = queue.popleft()
        if tid in visited:
            continue
        visited.add(tid)
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        terr = state.territories.get(tid)
        if not terr:
            continue
        is_sea = _is_sea_zone(tdef)
        if is_sea:
            continue
        is_neutral = terr.owner is None
        is_enemy = terr.owner is not None and terr.owner != faction_id
        is_allied = False
        if is_enemy and current_faction_def:
            od = faction_defs.get(terr.owner)
            if od and getattr(od, "alliance", "") == getattr(current_faction_def, "alliance", ""):
                is_allied = True
        has_units = len(getattr(terr, "units", []) or []) > 0
        neutral_has_enemies = False
        if is_neutral and current_faction_def:
            for u in getattr(terr, "units", []) or []:
                uf = get_unit_faction(u, unit_defs)
                ufd = faction_defs.get(uf) if uf else None
                if ufd and getattr(ufd, "alliance", "") != getattr(current_faction_def, "alliance", ""):
                    neutral_has_enemies = True
                    break
                if not ufd:
                    neutral_has_enemies = True
                    break
        ownable = getattr(tdef, "ownable", True)
        empty_enemy = is_enemy and not is_allied and not has_units
        empty_neutral_ownable = is_neutral and not neutral_has_enemies and not has_units and ownable
        friendly_or_allied = terr.owner == faction_id or is_allied
        if empty_enemy or empty_neutral_ownable:
            conquerable.add(tid)
        can_pass = empty_enemy or empty_neutral_ownable or friendly_or_allied
        if not can_pass or steps >= movement_range:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            adj_def = territory_defs.get(adj_id)
            if not adj_def or _is_sea_zone(adj_def):
                continue
            if adj_id in visited:
                continue
            queue.append((adj_id, steps + 1))
    return conquerable


def get_charge_max_gain_over_moves(
    start_territory_id: str,
    state: GameState,
    movement_range: int,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    gain_fn: callable,
    exclude_tids: set[str] | None = None,
) -> float:
    """
    Over movement_range steps through cavalry open space, find the single path that
    maximizes total gain (sum of gain_fn(t) for conquerable t on that path). Returns
    that max total. So we value "best possible charge path over the next 1–2 turns",
    not the sum of all reachable territories. exclude_tids: don't count gain for these (e.g. already conquered this turn).
    """
    if movement_range <= 0:
        return 0.0
    exclude = exclude_tids or set()
    current_faction_def = faction_defs.get(faction_id)

    # (tid, steps_used) -> best total gain achievable when at tid after steps_used
    best: dict[tuple[str, int], float] = {}
    queue: deque[tuple[str, int, float]] = deque([(start_territory_id, 0, 0.0)])
    best[(start_territory_id, 0)] = 0.0

    while queue:
        tid, steps, total_gain = queue.popleft()
        if total_gain < best.get((tid, steps), -1.0):
            continue
        if steps >= movement_range:
            continue
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        terr = state.territories.get(tid)
        if not terr:
            continue
        if _is_sea_zone(tdef):
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            adj_def = territory_defs.get(adj_id)
            if not adj_def or _is_sea_zone(adj_def):
                continue
            adj_terr = state.territories.get(adj_id)
            if not adj_terr:
                continue
            is_neutral = adj_terr.owner is None
            is_enemy = adj_terr.owner is not None and adj_terr.owner != faction_id
            is_allied = False
            if is_enemy and current_faction_def:
                od = faction_defs.get(adj_terr.owner)
                if od and getattr(od, "alliance", "") == getattr(current_faction_def, "alliance", ""):
                    is_allied = True
            has_units = len(getattr(adj_terr, "units", []) or []) > 0
            neutral_has_enemies = False
            if is_neutral and current_faction_def:
                for u in getattr(adj_terr, "units", []) or []:
                    uf = get_unit_faction(u, unit_defs)
                    ufd = faction_defs.get(uf) if uf else None
                    if ufd and getattr(ufd, "alliance", "") != getattr(current_faction_def, "alliance", ""):
                        neutral_has_enemies = True
                        break
                    if not ufd:
                        neutral_has_enemies = True
                        break
            ownable = getattr(adj_def, "ownable", True)
            empty_enemy = is_enemy and not is_allied and not has_units
            empty_neutral_ownable = is_neutral and not neutral_has_enemies and not has_units and ownable
            friendly_or_allied = adj_terr.owner == faction_id or is_allied
            can_pass = empty_enemy or empty_neutral_ownable or friendly_or_allied
            if not can_pass:
                continue
            new_steps = steps + 1
            add_gain = 0.0
            if (empty_enemy or empty_neutral_ownable) and adj_id not in exclude:
                add_gain = float(gain_fn(adj_id)) if callable(gain_fn) else 0.0
            new_gain = total_gain + add_gain
            key = (adj_id, new_steps)
            if new_gain > best.get(key, -1.0):
                best[key] = new_gain
                queue.append((adj_id, new_steps, new_gain))

    return max(best.values(), default=0.0)


def get_sea_zones_reachable_by_sail(
    from_territory: str,
    state: GameState,
    drivers: list,
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict | None = None,
    faction_defs: dict | None = None,
) -> set[str]:
    """
    Sea zone IDs reachable by sailing from from_territory (BFS over sea only).
    Uses max(driver.remaining_movement). Does not expand through enemy-occupied sea zones,
    but an adjacent (or in-range) hostile sea zone is still a valid destination: you may sail
    in to fight a naval battle, then sea raid from there — same as combat_move reachability.
    Used for offload pathfinding so we don't rely on combat_move "must attack" filter.
    """
    if not drivers:
        return set()
    max_steps = max(getattr(u, "remaining_movement", 0) for u in drivers)
    current_faction = getattr(state, "current_faction", None)
    result: set[str] = set()
    if _is_sea_zone(territory_defs.get(from_territory)):
        result.add(from_territory)
    queue: deque[tuple[str, int]] = deque([(from_territory, 0)])
    visited = {from_territory}
    while queue:
        tid, steps = queue.popleft()
        tdef = territory_defs.get(tid)
        if not tdef:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            adj_def = territory_defs.get(adj_id)
            if not adj_def or not _is_sea_zone(adj_def):
                continue
            new_steps = steps + 1
            if new_steps > max_steps:
                continue

            adj_territory = state.territories.get(adj_id)
            has_enemy = False
            if adj_territory and unit_defs and faction_defs and current_faction:
                for u in adj_territory.units:
                    uf = get_unit_faction(u, unit_defs)
                    if uf and uf != current_faction:
                        fdef = faction_defs.get(uf)
                        cur_fdef = faction_defs.get(current_faction)
                        if fdef and cur_fdef and fdef.alliance != cur_fdef.alliance:
                            has_enemy = True
                            break
                    elif not uf:
                        has_enemy = True
                        break
            if has_enemy:
                # Valid terminal destination (naval combat), but never a corridor — do not enqueue.
                result.add(adj_id)
                continue

            result.add(adj_id)
            if adj_id not in visited:
                visited.add(adj_id)
                queue.append((adj_id, new_steps))
    return result


def get_shortest_path(
    start: str,
    end: str,
    territory_defs: dict[str, TerritoryDefinition],
    is_aerial: bool = False,
) -> list[str] | None:
    """
    BFS to get one shortest path from start to end (inclusive).
    Returns list of territory IDs [start, ..., end] or None if unreachable.
    When is_aerial, may use aerial_adjacent edges (fly over mountains/rivers).
    Land paths include ford_adjacent edges (same graph as ford escort pathfinding).
    """
    if start == end:
        return [start]
    parent: dict[str, str] = {}
    queue = deque([start])
    visited = {start}
    while queue:
        territory_id = queue.popleft()
        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue
        neighbor_ids = (
            _adjacent_ids(territory_def, is_aerial)
            if is_aerial
            else _land_adjacent_and_ford_edges(territory_def)
        )
        for adjacent_id in neighbor_ids:
            if adjacent_id == end:
                path = [end]
                cur = territory_id
                while cur:
                    path.append(cur)
                    cur = parent.get(cur)
                path.reverse()
                return path
            if adjacent_id not in visited:
                visited.add(adjacent_id)
                parent[adjacent_id] = territory_id
                queue.append(adjacent_id)
    return None


def are_sea_zones_directly_adjacent(
    territory_defs: dict[str, TerritoryDefinition],
    sea_a: str,
    sea_b: str,
) -> bool:
    """True if both IDs are sea zones and share a map edge (1-hex sail), not merely connected via land."""
    if not sea_a or not sea_b or sea_a == sea_b:
        return False
    da = territory_defs.get(sea_a)
    db = territory_defs.get(sea_b)
    if not da or not db or not _is_sea_zone(da) or not _is_sea_zone(db):
        return False
    return sea_b in _adjacent_ids(da, False) or sea_a in _adjacent_ids(db, False)


def sea_land_adjacent_for_offload(
    sea_id: str,
    land_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """
    True if this sea hex can offload/sea raid onto this land (either direction in adjacency lists).
    Generic shortest-path may return None when only one side lists the other.
    """
    sea_def = territory_defs.get(sea_id)
    land_def = territory_defs.get(land_id)
    if not sea_def or not land_def or not _is_sea_zone(sea_def) or _is_sea_zone(land_def):
        return False
    return land_id in _adjacent_ids(sea_def, False) or sea_id in _adjacent_ids(land_def, False)


def calculate_movement_cost(
    start: str,
    end: str,
    territory_defs: dict[str, TerritoryDefinition],
    is_aerial: bool = False,
) -> int | None:
    """
    Calculate the minimum movement cost (distance) between two territories using BFS.
    When is_aerial, may use aerial_adjacent edges.
    """
    path = get_shortest_path(start, end, territory_defs, is_aerial=is_aerial)
    return len(path) - 1 if path else None


def movement_cost_along_path(
    path: list[str],
    territory_defs: dict[str, TerritoryDefinition],
    is_aerial: bool = False,
) -> int | None:
    """
    Movement cost along an explicit path (each step = 1).
    Path must be a sequence of adjacent territories (or adjacent + aerial_adjacent when is_aerial).
    """
    if not path or len(path) < 2:
        return 0 if len(path) == 1 else None
    cost = 0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        tdef = territory_defs.get(a)
        if not tdef:
            return None
        if is_aerial:
            ok = b in _adjacent_ids(tdef, True)
        else:
            ok = b in _land_adjacent_and_ford_edges(tdef)
        if not ok:
            return None
        cost += 1
    return cost


# Legacy function for backwards compatibility during transition
def get_reachable_territories(
    unit_def: UnitDefinition,
    start: str,
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    phase: str,
) -> set[str]:
    """
    DEPRECATED: Use get_reachable_territories_for_unit instead.
    Calculate all territories reachable by a unit type from a starting territory.
    """
    # Create a temporary Unit with full movement for legacy compatibility
    temp_unit = Unit(
        instance_id="temp",
        unit_id=unit_def.id,
        remaining_movement=unit_def.movement,
        remaining_health=unit_def.health,
        base_movement=unit_def.movement,
        base_health=unit_def.health,
    )
    reachable_dict, _ = get_reachable_territories_for_unit(
        temp_unit, start, state, {unit_def.id: unit_def}, territory_defs, faction_defs, phase
    )
    return set(reachable_dict.keys())
