"""
Movement calculations and pathfinding.
"""

from collections import deque
from backend.engine.state import GameState, Unit, TerritoryState
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition
from backend.engine.utils import get_unit_faction


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


def _is_friendly_territory_for_landing(
    territory: TerritoryState,
    current_faction: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    """
    True if an aerial unit can land here. Only allied-owned territory counts;
    neutral (unowned) territory is not valid for landing (so aerials must be able to reach allied territory).
    """
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
) -> bool:
    """True if an aerial unit can land/stay here (owned by us or our alliance only; neutral does not count)."""
    return _is_friendly_territory_for_landing(
        territory, current_faction, faction_defs, unit_defs
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
        from_territory, current_faction, faction_defs, unit_defs
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
                adj_territory, current_faction, faction_defs, unit_defs
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
) -> tuple[dict[str, int], dict[str, list[list[str]]]]:
    """
    Calculate all territories reachable by a specific unit instance from a starting territory.
    Returns (reachable_dict, charge_routes).
    - reachable_dict: territory_id -> distance (cost to reach).
    - charge_routes: for cavalry in combat_move, territory_id -> list of charge_through paths
      (each path = list of empty enemy territory IDs passed through). Empty dict for non-cavalry.

    Rules:
    - BFS up to remaining_movement
    - Cavalry (charging): can pass through empty enemy, empty unowned, or empty friendly/allied territory in combat_move; enemy/unowned are conquered, friendly/allied are not.
    - Aerial: can pass through any enemy/neutral in both phases.
    - Empty unownable: can be passed through in combat_move to reach an enemy in 2 moves; not valid as final destination (filtered out).
    - Empty/friendly neutral: enqueued for all units.
    """
    unit_def = unit_defs.get(unit.unit_id)
    if not unit_def:
        return {}, {}

    is_aerial = (
        getattr(unit_def, "archetype", "") == "aerial"
        or "aerial" in getattr(unit_def, "tags", [])
    )
    is_cavalry = (
        getattr(unit_def, "archetype", "") == "cavalry"
        or "cavalry" in getattr(unit_def, "tags", [])
    )
    can_enter_enemy = phase == "combat_move"
    current_faction_def = faction_defs.get(state.current_faction)

    reachable = {}  # territory_id -> distance
    charge_routes: dict[str, list[list[str]]] = {}  # territory_id -> list of charge_through paths
    # For cavalry we track (tid, charge) to allow multiple paths; key = (tid, tuple(charge))
    visited: dict[tuple[str, tuple[str, ...]], int] = {}  # (tid, tuple(charge)) -> best distance
    queue: deque[tuple[str, int, list[str]]] = deque([(start, 0, [])])

    while queue:
        territory_id, distance, charge = queue.popleft()
        charge_key = (territory_id, tuple(charge))

        # Only record as reachable if within movement range (never allow > remaining_movement)
        if distance > 0 and distance <= unit.remaining_movement:
            reachable[territory_id] = min(reachable.get(territory_id, 999), distance)
            if is_cavalry and can_enter_enemy:
                # Via path must never include the destination (no "Via Pelennor" when moving to Pelennor)
                via_path = [t for t in charge if t != territory_id]
                charge_routes.setdefault(territory_id, [])
                if via_path not in charge_routes[territory_id]:
                    charge_routes[territory_id].append(via_path)

        if distance >= unit.remaining_movement:
            continue

        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue

        for adjacent_id in _adjacent_ids(territory_def, is_aerial):
            new_distance = distance + 1
            adj_def = territory_defs.get(adjacent_id)
            if _is_sea_zone(adj_def) and not _can_unit_enter_sea(unit_def):
                # Land unit can load into adjacent sea zone (cost 1); add to reachable but do not expand from sea
                if new_distance <= unit.remaining_movement and adjacent_id not in reachable:
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
                    if uf and uf != state.current_faction and current_faction_def:
                        ufd = faction_defs.get(uf)
                        if ufd and ufd.alliance != current_faction_def.alliance:
                            has_enemy_boats = True
                            break
                    elif not uf:
                        has_enemy_boats = True
                        break
                if has_enemy_boats:
                    if new_distance <= unit.remaining_movement and (
                        adjacent_id not in reachable or new_distance < reachable[adjacent_id]
                    ):
                        reachable[adjacent_id] = new_distance
                    continue

            is_neutral = adjacent_territory.owner is None
            is_enemy_territory = (
                adjacent_territory.owner is not None
                and adjacent_territory.owner != state.current_faction
            )
            is_allied_territory = False
            if is_enemy_territory and current_faction_def:
                owner_faction_def = faction_defs.get(adjacent_territory.owner)
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
            # Cavalry: can pass through EMPTY enemy (conquer), EMPTY unowned ownable, or friendly/allied (with or without units; no conquer).
            adjacent_empty_enemy = (
                is_enemy_territory and not is_allied_territory
                and not adjacent_has_any_units
            )
            adjacent_empty_unowned = (
                is_neutral and not neutral_has_enemies and not adjacent_has_any_units and adjacent_ownable
            )
            adjacent_friendly_or_allied = (
                adjacent_territory.owner == state.current_faction or is_allied_territory
            )
            # Friendly/allied can have units—we're just passing through, not conquering
            can_charge_through = (
                adjacent_empty_enemy or adjacent_empty_unowned or adjacent_friendly_or_allied
            )
            new_charge = charge + [adjacent_id] if (is_cavalry and can_enter_enemy and can_charge_through) else charge
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
                    queue.append((adjacent_id, new_distance, new_charge))
            elif phase == "combat_move" and not is_aerial and new_distance <= unit.remaining_movement:
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

        is_neutral = territory.owner is None
        is_enemy_territory = (
            territory.owner is not None
            and territory.owner != state.current_faction
        )

        # Check if it's allied territory
        is_allied_territory = False
        if is_enemy_territory and current_faction_def:
            owner_faction_def = faction_defs.get(territory.owner)
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
                else:
                    # Empty or friendly sea zone: valid for sail (e.g. sail to zone adjacent to land, then offload/sea raid)
                    filtered_reachable[territory_id] = dist
            elif is_sea and not _can_unit_enter_sea(unit_def) and dist == 1:
                # Land unit loading into adjacent sea zone
                filtered_reachable[territory_id] = dist
            elif is_enemy_territory and not is_allied_territory:
                territory_has_units = len(territory.units) > 0
                if is_aerial:
                    if territory_has_units and _can_reach_friendly_from(
                        territory_id, unit.remaining_movement - dist,
                        state, territory_defs, faction_defs, unit_defs, state.current_faction,
                        for_aerial=True,
                    ):
                        filtered_reachable[territory_id] = dist
                else:
                    filtered_reachable[territory_id] = dist
            elif is_neutral and neutral_has_enemies:
                # Sea zones with enemy units, or hostile neutrals (e.g. goblins). Aerial: allow if can reach friendly (land) after.
                if is_aerial:
                    if _can_reach_friendly_from(
                        territory_id, unit.remaining_movement - dist,
                        state, territory_defs, faction_defs, unit_defs, state.current_faction,
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
                filtered_reachable[territory_id] = dist  # load into sea
            elif is_neutral:
                if not neutral_has_enemies and not is_ownable:
                    filtered_reachable[territory_id] = dist  # empty unownable neutral only (e.g. pass-through)
            elif not is_enemy_territory or is_allied_territory:
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
                is_neutral = adj_territory.owner is None
                is_enemy = (
                    adj_territory.owner is not None
                    and adj_territory.owner != state.current_faction
                )
                is_allied = False
                if is_enemy and current_faction_def:
                    owner_def = faction_defs.get(adj_territory.owner)
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
    Uses max(driver.remaining_movement). Does not expand through enemy-occupied sea zones.
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
            adj_territory = state.territories.get(adj_id)
            if adj_territory and unit_defs and faction_defs and current_faction:
                has_enemy = False
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
                    continue  # cannot sail through enemy sea zone
            new_steps = steps + 1
            if new_steps <= max_steps:
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
        for adjacent_id in _adjacent_ids(territory_def, is_aerial):
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
        if not tdef or b not in _adjacent_ids(tdef, is_aerial):
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
