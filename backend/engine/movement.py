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
) -> dict[str, int]:
    """
    Calculate all territories reachable by a specific unit instance from a starting territory.
    Returns a dict mapping territory_id -> distance (cost to reach).

    Rules:
    - BFS up to remaining_movement
    - Non-combat: cannot pass through or end in enemy territory (allied/friendly/empty neutral only).
    - Combat: can end in enemy (or neutral-with-enemies) to attack, but cannot pass through
      enemy territory; each step into enemy/contested is attack-only (no expansion from there).
    - Aerial units:
      - Can pass through enemy territory in any phase
      - Cannot end in enemy territory during non_combat_move
    - In non_combat_move: can move freely into allied territory (same alliance, different faction)

    Args:
        unit: The unit instance (uses remaining_movement)
        start: Starting territory ID
        state: Current game state
        unit_defs: Unit definitions (for tags like "aerial")
        territory_defs: All territory definitions
        faction_defs: All faction definitions
        phase: Current game phase ("combat_move", "non_combat_move", etc.)

    Returns:
        Dict mapping reachable territory_id -> distance to reach it
    """
    unit_def = unit_defs.get(unit.unit_id)
    if not unit_def:
        return {}

    is_aerial = "aerial" in unit_def.tags
    can_enter_enemy = phase == "combat_move"
    current_faction_def = faction_defs.get(state.current_faction)

    reachable = {}  # territory_id -> distance
    queue = deque([(start, 0)])  # (territory_id, distance)
    visited = {start: 0}  # territory_id -> best distance

    while queue:
        territory_id, distance = queue.popleft()

        if distance > 0:  # Don't include start in reachable
            reachable[territory_id] = distance

        if distance >= unit.remaining_movement:
            continue

        # Get adjacent territories
        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue

        for adjacent_id in territory_def.adjacent:
            new_distance = distance + 1

            # Skip if we've found a better path already
            if adjacent_id in visited and visited[adjacent_id] <= new_distance:
                continue

            adjacent_territory = state.territories.get(adjacent_id)
            if not adjacent_territory:
                continue

            # Check if we can move through this territory
            is_neutral = adjacent_territory.owner is None
            is_enemy_territory = (
                adjacent_territory.owner is not None
                and adjacent_territory.owner != state.current_faction
            )

            # Check if it's allied territory (same alliance, different faction)
            is_allied_territory = False
            if is_enemy_territory and current_faction_def:
                owner_faction_def = faction_defs.get(adjacent_territory.owner)
                if owner_faction_def and owner_faction_def.alliance == current_faction_def.alliance:
                    is_allied_territory = True

            # Check if neutral territory has enemy units (for combat_move restriction)
            neutral_has_enemies = False
            if is_neutral and current_faction_def:
                for u in adjacent_territory.units:
                    unit_faction = u.instance_id.split("_")[0]
                    unit_faction_def = faction_defs.get(unit_faction)
                    if unit_faction_def and unit_faction_def.alliance != current_faction_def.alliance:
                        neutral_has_enemies = True
                        break
                    elif not unit_faction_def:
                        # Units without a faction (goblins, neutral monsters) are enemies to all
                        neutral_has_enemies = True
                        break

            # Determine if we can pass through (use as stepping stone in BFS)
            # Non-combat: cannot pass through enemy territory (allied/friendly/empty neutral only)
            # Combat: can pass through friendly/allied/empty neutral; enemy (or neutral with enemies)
            #         is valid as a destination only (attack) — do not expand from it
            can_pass = True
            if is_enemy_territory and not is_allied_territory and not can_enter_enemy and not is_aerial:
                can_pass = False
            if is_enemy_territory and not is_allied_territory and can_enter_enemy and not is_aerial:
                # Combat move: enemy territory is destination-only; do not pass through
                can_pass = False
            if is_neutral and phase == "combat_move" and neutral_has_enemies and not is_aerial:
                # Combat move: neutral with enemies is attack-only; do not pass through
                can_pass = False
            # Combat_move: cannot pass through empty neutral (must be attacking)
            if is_neutral and phase == "combat_move" and not neutral_has_enemies:
                can_pass = False

            if can_pass:
                visited[adjacent_id] = new_distance
                queue.append((adjacent_id, new_distance))
            elif phase == "combat_move" and not is_aerial and new_distance <= unit.remaining_movement:
                # Combat: adjacent is enemy or neutral-with-enemies — valid destination only
                if (is_enemy_territory and not is_allied_territory) or (is_neutral and neutral_has_enemies):
                    if adjacent_id not in visited or new_distance < visited[adjacent_id]:
                        visited[adjacent_id] = new_distance
                        reachable[adjacent_id] = new_distance

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

    return filtered_reachable


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
    reachable_dict = get_reachable_territories_for_unit(
        temp_unit, start, state, {unit_def.id: unit_def}, territory_defs, faction_defs, phase
    )
    return set(reachable_dict.keys())
