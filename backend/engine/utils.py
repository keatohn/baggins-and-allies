"""
Utility functions for the game engine.
"""

import random
from collections import Counter
from backend.engine.state import GameState, TerritoryState, UnitStack, Unit
from backend.engine.definitions import FactionDefinition, TerritoryDefinition, UnitDefinition, load_starting_setup
from backend.engine import DICE_SIDES


def unitstack_to_units(
    stack: UnitStack,
    faction_id: str,
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
) -> list[Unit]:
    """
    Convert a UnitStack to individual Unit instances.

    Args:
        stack: UnitStack to convert
        faction_id: Faction owning these units
        state: Current game state (for ID generation and base stats)
        unit_defs: Unit definitions for base movement/health

    Returns:
        List of Unit instances
    """
    unit_def = unit_defs.get(stack.unit_id)
    if not unit_def:
        return []

    units = []
    for _ in range(stack.count):
        instance_id = state.generate_unit_instance_id(
            faction_id, stack.unit_id)
        units.append(Unit(
            instance_id=instance_id,
            unit_id=stack.unit_id,
            remaining_movement=unit_def.movement,
            remaining_health=unit_def.health,
            base_movement=unit_def.movement,
            base_health=unit_def.health,
        ))
    return units


def initialize_game_state(
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict[str, UnitDefinition] | None = None,
    starting_setup: dict[str, dict[str, list[dict]]] | None = None,
) -> GameState:
    """
    Create an initial game state with all factions and territories set up.

    Args:
        faction_defs: Faction definitions
        territory_defs: Territory definitions
        unit_defs: Unit definitions (required if starting_setup provided)
        starting_setup: Optional starting configuration:
            {
                "territory_owners": {"territory_id": "faction_id", ...},
                "starting_units": {
                    "territory_id": [{"unit_id": str, "count": int}, ...],
                    ...
                }
            }
            If not provided, uses default setup based on faction capitals.
    """
    # Initialize territories
    territories = {}
    for territory_id, territory_def in territory_defs.items():
        territories[territory_id] = TerritoryState(owner=None, units=[])

    faction_ids = sorted(faction_defs.keys())

    # Set territory ownership and original_owner
    if starting_setup and "territory_owners" in starting_setup:
        for territory_id, owner in starting_setup["territory_owners"].items():
            if territory_id in territories:
                territories[territory_id].owner = owner
                territories[territory_id].original_owner = owner  # Set original owner
    else:
        # Default: assign capitals to factions
        for faction_id, faction_def in faction_defs.items():
            capital = faction_def.capital
            if capital in territories:
                territories[capital].owner = faction_id
                territories[capital].original_owner = faction_id  # Set original owner

    # Calculate starting resources from owned territories
    faction_resources: dict[str, dict[str, int]] = {
        faction_id: {} for faction_id in faction_defs.keys()
    }
    
    for territory_id, territory_state in territories.items():
        owner = territory_state.owner
        if not owner or owner not in faction_resources:
            continue
        
        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue
        
        # Add this territory's production to the owner's starting resources
        for resource_id, amount in territory_def.produces.items():
            if resource_id not in faction_resources[owner]:
                faction_resources[owner][resource_id] = 0
            faction_resources[owner][resource_id] += amount

    # Calculate initial mobilization strongholds for first faction
    first_faction = faction_ids[0]
    initial_mob_strongholds = [
        tid for tid, ts in territories.items()
        if ts.owner == first_faction and territory_defs.get(tid) and territory_defs[tid].is_stronghold
    ]

    # Create game state (need it for unit ID generation)
    state = GameState(
        turn_number=1,
        current_faction=first_faction,
        phase="purchase",
        territories=territories,
        faction_resources=faction_resources,
        faction_purchased_units={faction_id: []
                                 for faction_id in faction_defs.keys()},
        unit_id_counters={},
        mobilization_strongholds=initial_mob_strongholds,
    )

    # Add starting units if provided
    if starting_setup and "starting_units" in starting_setup and unit_defs:
        for territory_id, unit_list in starting_setup["starting_units"].items():
            if territory_id not in state.territories:
                continue

            territory = state.territories[territory_id]
            # Determine faction from territory owner
            faction_id = territory.owner
            if not faction_id:
                continue

            for unit_entry in unit_list:
                unit_id = unit_entry.get("unit_id")
                count = unit_entry.get("count", 1)

                unit_def = unit_defs.get(unit_id)
                if not unit_def:
                    continue

                # Create individual unit instances
                for _ in range(count):
                    instance_id = state.generate_unit_instance_id(faction_id, unit_id)
                    unit = Unit(
                        instance_id=instance_id,
                        unit_id=unit_id,
                        remaining_movement=unit_def.movement,
                        remaining_health=unit_def.health,
                        base_movement=unit_def.movement,
                        base_health=unit_def.health,
                    )
                    territory.units.append(unit)

    return state


def get_default_starting_setup() -> dict:
    """
    Get the default starting setup from JSON file.
    Wrapper around load_starting_setup for backward compatibility.
    """
    return load_starting_setup()


def print_game_state(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    verbose: bool = False,
):
    """
    Pretty-print the current game state.

    Args:
        state: Current game state
        territory_defs: Territory definitions
        verbose: If True, show individual unit details (instance_id, movement, health)
    """
    print(f"\n{'='*60}")
    print(
        f"Turn {state.turn_number} | Faction: {state.current_faction} | Phase: {state.phase}")
    print(f"{'='*60}")

    # Print territories
    for territory_id in sorted(state.territories.keys()):
        territory_state = state.territories[territory_id]
        territory_def = territory_defs.get(territory_id)

        owner_str = territory_state.owner or "neutral"
        print(f"\n{territory_def.display_name} (Owner: {owner_str})")

        if territory_state.units:
            if verbose:
                # Show individual unit details
                for unit in territory_state.units:
                    print(f"  - {unit.instance_id}: mv={unit.remaining_movement}/{unit.base_movement}, "
                          f"hp={unit.remaining_health}/{unit.base_health}")
            else:
                # Aggregate by unit_id
                unit_counts = Counter(unit.unit_id for unit in territory_state.units)
                for unit_id, count in sorted(unit_counts.items()):
                    print(f"  - {unit_id}: {count}")
        else:
            print("  - No units")

    # Print resources
    print(f"\n{'Resources':.<40}")
    for faction_id, resources in state.faction_resources.items():
        resource_str = ", ".join(f"{k}: {v}" for k, v in resources.items())
        print(f"  {faction_id}: {resource_str}")
    print()


def get_unit_count_in_territory(state: GameState, territory_id: str, unit_id: str) -> int:
    """Get the count of a specific unit type in a territory."""
    territory = state.territories.get(territory_id)
    if not territory:
        return 0

    return sum(1 for unit in territory.units if unit.unit_id == unit_id)


def get_units_in_territory(state: GameState, territory_id: str) -> list[Unit]:
    """Get all units in a territory."""
    territory = state.territories.get(territory_id)
    if not territory:
        return []
    return list(territory.units)


def get_unit_by_instance_id(state: GameState, instance_id: str) -> tuple[Unit | None, str | None]:
    """
    Find a unit by its instance_id across all territories.

    Returns:
        Tuple of (Unit, territory_id) or (None, None) if not found
    """
    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            if unit.instance_id == instance_id:
                return unit, territory_id
    return None, None


def apply_resource_production(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
) -> GameState:
    """
    Apply resource production from all territories.
    Each faction gains resources from territories they own.
    """
    new_state = state.copy()

    for territory_id, territory_state in new_state.territories.items():
        if territory_state.owner is None:
            continue  # Neutral territories don't produce

        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue

        faction_id = territory_state.owner

        # Add produced resources to faction
        for resource_id, amount in territory_def.produces.items():
            if faction_id not in new_state.faction_resources:
                new_state.faction_resources[faction_id] = {}

            if resource_id not in new_state.faction_resources[faction_id]:
                new_state.faction_resources[faction_id][resource_id] = 0

            new_state.faction_resources[faction_id][resource_id] += amount

    return new_state


def generate_dice_rolls_for_units(
    units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    seed: int | None = None,
) -> list[int]:
    """
    Generate dice rolls for a list of Unit instances.
    Each unit rolls based on its unit definition's 'dice' attribute.

    Args:
        units: List of Unit instances
        unit_defs: Unit definitions
        seed: Optional random seed for reproducibility

    Returns:
        List of dice rolls (1 to DICE_SIDES per roll)
    """
    if seed is not None:
        random.seed(seed)

    rolls = []

    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        dice_count = getattr(unit_def, 'dice', 1) if unit_def else 1
        # Roll dice_count times for this unit
        for _ in range(dice_count):
            roll = random.randint(1, DICE_SIDES)
            rolls.append(roll)

    return rolls


def generate_combat_rolls_for_units(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    seed: int | None = None,
) -> dict[str, list[int]]:
    """
    Generate dice rolls for both sides of a combat using Unit instances.

    Args:
        attacker_units: Attacking Unit instances
        defender_units: Defending Unit instances
        unit_defs: Unit definitions
        seed: Optional random seed (applies to both sides)

    Returns:
        Dict with "attacker" and "defender" roll lists
    """
    attacker_rolls = generate_dice_rolls_for_units(attacker_units, unit_defs, seed)

    # Use a different seed for defender if seed was provided
    defender_seed = seed + 1 if seed is not None else None
    defender_rolls = generate_dice_rolls_for_units(defender_units, unit_defs, defender_seed)

    return {
        "attacker": attacker_rolls,
        "defender": defender_rolls,
    }


# Legacy functions for backwards compatibility during transition
def generate_dice_rolls(
    unit_stacks: list[UnitStack],
    unit_defs: dict[str, UnitDefinition],
    seed: int | None = None,
) -> list[int]:
    """
    DEPRECATED: Use generate_dice_rolls_for_units instead.
    Generate dice rolls for a list of unit stacks.
    """
    if seed is not None:
        random.seed(seed)

    rolls = []

    for stack in unit_stacks:
        unit_def = unit_defs.get(stack.unit_id)
        if not unit_def:
            continue

        # Each unit rolls once per health
        for _ in range(stack.count):
            for _ in range(unit_def.health):
                roll = random.randint(1, DICE_SIDES)
                rolls.append(roll)

    return rolls


def generate_combat_rolls(
    attacker_stacks: list[UnitStack],
    defender_stacks: list[UnitStack],
    unit_defs: dict[str, UnitDefinition],
    seed: int | None = None,
) -> dict[str, list[int]]:
    """
    DEPRECATED: Use generate_combat_rolls_for_units instead.
    Generate dice rolls for both sides of a combat using UnitStacks.
    """
    attacker_rolls = generate_dice_rolls(attacker_stacks, unit_defs, seed)

    # Use a different seed for defender if seed was provided
    defender_seed = seed + 1 if seed is not None else None
    defender_rolls = generate_dice_rolls(
        defender_stacks, unit_defs, defender_seed)

    return {
        "attacker": attacker_rolls,
        "defender": defender_rolls,
    }


def print_combat_log(
    battle_result,
    attacker_faction: str,
    defender_faction: str,
    territory_id: str,
):
    """
    Pretty-print a combat log from BattleResult.

    Args:
        battle_result: BattleResult object with combat_log
        attacker_faction: Name of attacking faction
        defender_faction: Name of defending faction
        territory_id: Territory where combat occurred
    """
    print(f"\n{'='*70}")
    print(
        f"COMBAT LOG: {attacker_faction} attacks {defender_faction} in {territory_id}")
    print(f"{'='*70}")

    if not battle_result.combat_log:
        print("No combat log available")
        return

    for round_num, round_log in enumerate(battle_result.combat_log, 1):
        print(f"\n--- ROUND {round_num} ---")

        print(
            f"\nAttacker Rolls ({len(round_log.attacker_rolls)} dice): {round_log.attacker_rolls}")
        print(f"Attacker Hits: {round_log.attacker_hits}")

        # Format defender casualties (list of instance_ids)
        defender_cas_str = ", ".join(round_log.defender_casualties) if round_log.defender_casualties else "None"
        print(f"Defender Casualties: {defender_cas_str}")

        print(
            f"\nDefender Rolls ({len(round_log.defender_rolls)} dice): {round_log.defender_rolls}")
        print(f"Defender Hits: {round_log.defender_hits}")

        # Format attacker casualties (list of instance_ids)
        attacker_cas_str = ", ".join(round_log.attacker_casualties) if round_log.attacker_casualties else "None"
        print(f"Attacker Casualties: {attacker_cas_str}")

    print(f"\n{'='*70}")
    if battle_result.territory_captured:
        print(f"✓ Territory CAPTURED by {attacker_faction}")
    else:
        print(f"✗ Territory HELD by {defender_faction}")
    print(f"{'='*70}\n")
