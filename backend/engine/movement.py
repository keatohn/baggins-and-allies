"""
Movement calculations and pathfinding.
"""

from collections import deque
from backend.engine.state import GameState, Unit
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition


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
    - Cavalry (charging): can pass through empty enemy territory in combat_move; those are conquered.
    - Aerial: can pass through any enemy/neutral in both phases.
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

        if distance > 0:
            reachable[territory_id] = min(reachable.get(territory_id, 999), distance)
            if is_cavalry and can_enter_enemy:
                charge_routes.setdefault(territory_id, [])
                if charge not in charge_routes[territory_id]:
                    charge_routes[territory_id].append(charge)

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
                    unit_faction = u.instance_id.split("_")[0]
                    unit_faction_def = faction_defs.get(unit_faction)
                    if unit_faction_def and unit_faction_def.alliance != current_faction_def.alliance:
                        neutral_has_enemies = True
                        break
                    elif not unit_faction_def:
                        neutral_has_enemies = True
                        break

            # Cavalry in combat_move: can pass through empty enemy (charging)
            adjacent_empty_enemy = (
                is_enemy_territory and not is_allied_territory
                and len(adjacent_territory.units) == 0
            )
            new_charge = charge + [adjacent_id] if (is_cavalry and can_enter_enemy and adjacent_empty_enemy) else charge
            adj_key = (adjacent_id, tuple(new_charge))

            can_pass = True
            if is_enemy_territory and not is_allied_territory and not can_enter_enemy and not is_aerial:
                can_pass = False
            if is_enemy_territory and not is_allied_territory and can_enter_enemy and not is_aerial and not (is_cavalry and adjacent_empty_enemy):
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
                        charge_routes.setdefault(adjacent_id, [])
                        if charge not in charge_routes[adjacent_id]:
                            charge_routes[adjacent_id].append(charge)

    # Filter destinations based on phase and unit type
    filtered_reachable = {}
    for territory_id, dist in reachable.items():
        territory = state.territories.get(territory_id)
        if not territory:
            continue
            
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
                unit_faction = u.instance_id.split("_")[0]
                unit_faction_def = faction_defs.get(unit_faction)
                if unit_faction_def and unit_faction_def.alliance != current_faction_def.alliance:
                    neutral_has_enemies = True
                    break
                elif not unit_faction_def:
                    neutral_has_enemies = True
                    break

        # Apply phase-specific filters
        if phase == "combat_move":
            # Combat move: can end in enemy territory OR neutral with enemies
            if is_enemy_territory and not is_allied_territory:
                filtered_reachable[territory_id] = dist
            elif is_neutral and neutral_has_enemies:
                filtered_reachable[territory_id] = dist
            # Note: friendly/allied territories are NOT valid combat_move destinations
        elif phase == "non_combat_move":
            # Non-combat move: can end in friendly, allied, or empty neutral territory
            # Cannot enter neutral territory with enemy units (that would be combat)
            if is_neutral:
                if not neutral_has_enemies:
                    filtered_reachable[territory_id] = dist
            elif not is_enemy_territory or is_allied_territory:
                filtered_reachable[territory_id] = dist
        else:
            # Other phases: include all reachable
            filtered_reachable[territory_id] = dist

    # Restrict charge_routes to only destinations that are in filtered_reachable
    charge_routes_filtered = {
        tid: paths for tid, paths in charge_routes.items()
        if tid in filtered_reachable
    }
    return filtered_reachable, charge_routes_filtered


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
    if start == end:
        return 0

    queue = deque([(start, 0)])
    visited = {start}

    while queue:
        territory_id, distance = queue.popleft()

        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue

        for adjacent_id in territory_def.adjacent:
            if adjacent_id == end:
                return distance + 1

            if adjacent_id not in visited:
                visited.add(adjacent_id)
                queue.append((adjacent_id, distance + 1))

    return None  # Unreachable


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
