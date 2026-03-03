"""
Movement calculations and pathfinding.
"""

from collections import deque
from backend.engine.state import GameState, Unit, TerritoryState
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition
from backend.engine.utils import get_unit_faction


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
) -> bool:
    """
    BFS from from_territory_id with moves_left steps. Returns True if any territory
    reachable within that range is friendly for landing (aerial must be able to return).
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
        for adj_id in tdef.adjacent:
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

        for adjacent_id in territory_def.adjacent:
            new_distance = distance + 1
            adjacent_territory = state.territories.get(adjacent_id)
            if not adjacent_territory:
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

            adj_def = territory_defs.get(adjacent_id)
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
        if phase == "combat_move":
            # Combat move: enemy territory; neutral with enemies (attack the units); empty neutral only if ownable (conquer, ground only).
            # Aerial: can only move into territories that have units to attack (enemy or neutral with enemies). No empty destinations.
            if is_enemy_territory and not is_allied_territory:
                territory_has_units = len(territory.units) > 0
                if is_aerial:
                    if territory_has_units and _can_reach_friendly_from(
                        territory_id, unit.remaining_movement - dist,
                        state, territory_defs, faction_defs, unit_defs, state.current_faction,
                    ):
                        filtered_reachable[territory_id] = dist
                else:
                    filtered_reachable[territory_id] = dist
            elif is_neutral and neutral_has_enemies:
                if is_aerial:
                    if _can_reach_friendly_from(
                        territory_id, unit.remaining_movement - dist,
                        state, territory_defs, faction_defs, unit_defs, state.current_faction,
                    ):
                        filtered_reachable[territory_id] = dist
                else:
                    filtered_reachable[territory_id] = dist  # hostile neutral (e.g. goblins, cave trolls) - attack it
            elif is_neutral and not neutral_has_enemies and is_ownable:
                # Empty unowned ownable: ground can move in and capture. Aerial cannot (no combat move into empty).
                if not is_aerial:
                    filtered_reachable[territory_id] = dist
            # Note: friendly/allied territories are NOT valid combat_move destinations
        elif phase == "non_combat_move":
            # Non-combat move: friendly or allied only. Empty unowned ownable = conquer = combat_move only.
            if is_neutral:
                if not neutral_has_enemies and not is_ownable:
                    filtered_reachable[territory_id] = dist  # empty unownable neutral only (e.g. pass-through)
            elif not is_enemy_territory or is_allied_territory:
                filtered_reachable[territory_id] = dist
        else:
            # Other phases: include all reachable
            filtered_reachable[territory_id] = dist

    # Source territory is never a valid move destination (cannot move from X to X)
    filtered_reachable.pop(start, None)

    # Restrict charge_routes to only destinations that are in filtered_reachable
    charge_routes_filtered = {
        tid: paths for tid, paths in charge_routes.items()
        if tid in filtered_reachable
    }
    return filtered_reachable, charge_routes_filtered


def get_shortest_path(
    start: str,
    end: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str] | None:
    """
    BFS to get one shortest path from start to end (inclusive).
    Returns list of territory IDs [start, ..., end] or None if unreachable.
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
        for adjacent_id in territory_def.adjacent:
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
) -> int | None:
    """
    Calculate the minimum movement cost (distance) between two territories using BFS.

    Args:
        start: Starting territory ID
        end: Destination territory ID
        territory_defs: All territory definitions

    Returns:
        Minimum distance, or None if unreachable
    """
    path = get_shortest_path(start, end, territory_defs)
    return len(path) - 1 if path else None


def movement_cost_along_path(
    path: list[str],
    territory_defs: dict[str, TerritoryDefinition],
) -> int | None:
    """
    Movement cost along an explicit path (each step = 1).
    Path must be a sequence of adjacent territories.

    Args:
        path: Ordered list of territory IDs [start, waypoint1, ..., end]
        territory_defs: All territory definitions

    Returns:
        Number of steps (edges), or None if path is invalid (non-adjacent or missing def).
    """
    if not path or len(path) < 2:
        return 0 if len(path) == 1 else None
    cost = 0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        tdef = territory_defs.get(a)
        if not tdef or b not in tdef.adjacent:
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
