"""
Query functions for UI integration.
These functions help the UI understand what actions are available
without mutating game state.
"""

from dataclasses import dataclass
from typing import Any
from backend.engine.state import GameState, Unit
from backend.engine.actions import Action
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition
from backend.engine.movement import get_reachable_territories_for_unit


# Phase rules (duplicated from reducer to avoid circular imports)
PHASE_ALLOWED_ACTIONS = {
    "purchase": ["purchase_units", "end_phase"],
    "combat_move": ["move_units", "cancel_move", "end_phase"],
    "combat": ["initiate_combat", "continue_combat", "retreat", "end_phase"],
    "non_combat_move": ["move_units", "cancel_move", "end_phase"],
    "mobilization": ["mobilize_units", "cancel_mobilization", "end_phase", "end_turn"],
}


@dataclass
class ValidationResult:
    """Result of action validation."""
    valid: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "error": self.error}


# ===== Action Validation =====

def validate_action(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> ValidationResult:
    """
    Validate an action without applying it.
    Returns ValidationResult with valid=True or valid=False with error message.
    """
    # Check if game is over
    if state.winner is not None:
        return ValidationResult(False, f"Game is over. {state.winner} alliance has won.")

    # Check faction
    if action.faction != state.current_faction:
        return ValidationResult(
            False,
            f"Not {action.faction}'s turn. Current faction: {state.current_faction}"
        )

    # Check phase allows this action type
    allowed = PHASE_ALLOWED_ACTIONS.get(state.phase, [])
    if action.type not in allowed:
        return ValidationResult(
            False,
            f"Cannot {action.type} during {state.phase} phase. Allowed: {allowed}"
        )

    # Combat phase special rules
    if state.phase == "combat":
        if state.active_combat is not None:
            if action.type not in ["continue_combat", "retreat"]:
                return ValidationResult(
                    False,
                    "Active combat in progress. Must continue_combat or retreat."
                )
        else:
            if action.type in ["continue_combat", "retreat"]:
                return ValidationResult(
                    False,
                    f"No active combat to {action.type}."
                )

    # Action-specific validation
    if action.type == "purchase_units":
        return _validate_purchase(state, action, unit_defs, faction_defs, territory_defs)
    elif action.type == "move_units":
        return _validate_move(state, action, unit_defs, territory_defs, faction_defs)
    elif action.type == "initiate_combat":
        return _validate_initiate_combat(state, action, faction_defs)
    elif action.type == "mobilize_units":
        return _validate_mobilize(state, action, unit_defs, territory_defs)
    elif action.type == "retreat":
        return _validate_retreat(state, action, territory_defs, faction_defs)
    elif action.type == "cancel_move":
        return _validate_cancel_move(state, action)
    elif action.type == "cancel_mobilization":
        return _validate_cancel_mobilization(state, action)
    elif action.type in ["end_phase", "end_turn", "continue_combat"]:
        return ValidationResult(True)

    return ValidationResult(False, f"Unknown action type: {action.type}")


def _validate_purchase(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate a purchase_units action. Total units purchased cannot exceed mobilization capacity."""
    faction_id = action.faction
    # Format: {"purchases": {unit_id: count}}
    purchases = action.payload.get("purchases", {})

    if not purchases:
        return ValidationResult(False, "No units specified to purchase")

    faction_def = faction_defs.get(faction_id)
    if not faction_def:
        return ValidationResult(False, f"Unknown faction: {faction_id}")

    # Calculate total cost
    total_cost: dict[str, int] = {}
    for unit_id, count in purchases.items():
        unit_def = unit_defs.get(unit_id)
        if not unit_def:
            return ValidationResult(False, f"Unknown unit type: {unit_id}")

        if not unit_def.purchasable:
            return ValidationResult(False, f"Unit {unit_id} is not purchasable")

        if unit_def.faction != faction_id:
            return ValidationResult(False, f"Unit {unit_id} belongs to {unit_def.faction}, not {faction_id}")

        for resource, amount in unit_def.cost.items():
            total_cost[resource] = total_cost.get(resource, 0) + (amount * count)

    # Check resources
    faction_resources = state.faction_resources.get(faction_id, {})
    for resource, needed in total_cost.items():
        available = faction_resources.get(resource, 0)
        if available < needed:
            return ValidationResult(
                False,
                f"Insufficient {resource}: need {needed}, have {available}"
            )

    # Check mobilization capacity: total units (already purchased + this purchase) cannot exceed capacity
    capacity_info = get_mobilization_capacity(state, faction_id, territory_defs)
    capacity = capacity_info["total_capacity"]
    already_purchased = sum(
        stack.count for stack in state.faction_purchased_units.get(faction_id, [])
    )
    this_purchase_total = sum(purchases.values())
    if already_purchased + this_purchase_total > capacity:
        return ValidationResult(
            False,
            f"Cannot purchase {this_purchase_total} more units: you can only mobilize {capacity} units this turn "
            f"(already purchased: {already_purchased})"
        )

    return ValidationResult(True)


def _validate_move(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> ValidationResult:
    """Validate a move_units action."""
    unit_instance_ids = action.payload.get("unit_instance_ids", [])
    destination = action.payload.get("to")  # action uses "to" not "destination"
    origin = action.payload.get("from")  # action uses "from" not "origin"

    if not unit_instance_ids:
        return ValidationResult(False, "No units specified to move")

    if not destination:
        return ValidationResult(False, "No destination specified")

    if not origin:
        return ValidationResult(False, "No origin specified")

    origin_territory = state.territories.get(origin)
    if not origin_territory:
        return ValidationResult(False, f"Origin territory {origin} does not exist")

    # Find and validate each unit
    for instance_id in unit_instance_ids:
        unit = next((u for u in origin_territory.units if u.instance_id == instance_id), None)
        if not unit:
            return ValidationResult(False, f"Unit {instance_id} not found in {origin}")

        # Check reachability
        reachable = get_reachable_territories_for_unit(
            unit, origin, state, unit_defs, territory_defs, faction_defs, state.phase
        )
        if destination not in reachable:
            return ValidationResult(
                False,
                f"Unit {instance_id} cannot reach {destination} from {origin}"
            )

    return ValidationResult(True)


def _validate_initiate_combat(
    state: GameState,
    action: Action,
    faction_defs: dict[str, FactionDefinition],
) -> ValidationResult:
    """Validate an initiate_combat action."""
    territory_id = action.payload.get("territory_id")
    if not territory_id:
        return ValidationResult(False, "No territory specified for combat")

    territory = state.territories.get(territory_id)
    if not territory:
        return ValidationResult(False, f"Territory {territory_id} does not exist")

    attacker_faction = action.faction
    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance

    # Find attacker and defender units
    attacker_units = []
    defender_units = []

    for unit in territory.units:
        unit_faction = unit.instance_id.split("_")[0]
        if unit_faction == attacker_faction:
            attacker_units.append(unit)
        else:
            unit_alliance = faction_defs.get(unit_faction, FactionDefinition(
                "", "", "", "", "")).alliance
            if unit_alliance != attacker_alliance:
                defender_units.append(unit)

    if not attacker_units:
        return ValidationResult(False, f"No attacking units in {territory_id}")

    if not defender_units:
        return ValidationResult(False, f"No enemy units to fight in {territory_id}")

    return ValidationResult(True)


def _validate_mobilize(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate a mobilize_units action."""
    faction_id = action.faction
    destination = action.payload.get("destination")
    units_to_mobilize = action.payload.get("units", [])

    if not destination:
        return ValidationResult(False, "No destination specified")

    if not units_to_mobilize:
        return ValidationResult(False, "No units specified to mobilize")

    dest_territory = state.territories.get(destination)
    dest_def = territory_defs.get(destination)

    if not dest_territory or not dest_def:
        return ValidationResult(False, f"Territory {destination} does not exist")

    if not dest_def.is_stronghold:
        return ValidationResult(False, f"{destination} is not a stronghold")

    if dest_territory.owner != faction_id:
        return ValidationResult(False, f"{destination} is not owned by {faction_id}")

    # Check purchased units are available
    purchased = state.faction_purchased_units.get(faction_id, [])
    purchased_counts = {stack.unit_id: stack.count for stack in purchased}

    for item in units_to_mobilize:
        unit_id = item.get("unit_id")
        count = item.get("count", 1)

        available = purchased_counts.get(unit_id, 0)
        if available < count:
            return ValidationResult(
                False,
                f"Not enough {unit_id} purchased: need {count}, have {available}"
            )

    return ValidationResult(True)


def _validate_retreat(
    state: GameState,
    action: Action,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> ValidationResult:
    """Validate a retreat action."""
    if not state.active_combat:
        return ValidationResult(False, "No active combat to retreat from")

    destination = action.payload.get("retreat_to")
    if not destination:
        return ValidationResult(False, "No retreat destination specified")

    combat_territory = state.active_combat.territory_id
    combat_def = territory_defs.get(combat_territory)

    if not combat_def:
        return ValidationResult(False, f"Combat territory {combat_territory} not found")

    # Check destination is adjacent
    if destination not in combat_def.adjacent:
        return ValidationResult(
            False,
            f"{destination} is not adjacent to {combat_territory}"
        )

    # Check destination is friendly
    dest_territory = state.territories.get(destination)
    if not dest_territory:
        return ValidationResult(False, f"Territory {destination} does not exist")

    attacker_faction = action.faction
    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance

    dest_owner = dest_territory.owner
    if dest_owner:
        dest_alliance = faction_defs.get(dest_owner, FactionDefinition(
            "", "", "", "", "")).alliance
        if dest_alliance != attacker_alliance:
            return ValidationResult(
                False,
                f"Cannot retreat to {destination}: owned by enemy"
            )

    return ValidationResult(True)


def _validate_cancel_move(state: GameState, action: Action) -> ValidationResult:
    """Validate a cancel_move action."""
    move_index = action.payload.get("move_index", -1)
    if move_index < 0 or move_index >= len(state.pending_moves):
        return ValidationResult(
            False,
            f"Invalid move index: {move_index}. Pending moves: {len(state.pending_moves)}"
        )
    return ValidationResult(True)


def _validate_cancel_mobilization(state: GameState, action: Action) -> ValidationResult:
    """Validate a cancel_mobilization action."""
    idx = action.payload.get("mobilization_index", -1)
    if idx < 0 or idx >= len(state.pending_mobilizations):
        return ValidationResult(
            False,
            f"Invalid mobilization index: {idx}. Pending: {len(state.pending_mobilizations)}"
        )
    return ValidationResult(True)


# ===== Query Functions =====

def get_available_action_types(state: GameState) -> list[str]:
    """Get action types available in the current phase and combat state."""
    if state.winner is not None:
        return []

    allowed = list(PHASE_ALLOWED_ACTIONS.get(state.phase, []))

    # Filter based on combat state
    if state.phase == "combat":
        if state.active_combat is not None:
            allowed = [a for a in allowed if a in ["continue_combat", "retreat"]]
        else:
            allowed = [a for a in allowed if a not in ["continue_combat", "retreat"]]

    return allowed


def get_movable_units(
    state: GameState,
    faction_id: str,
) -> list[dict[str, Any]]:
    """
    Get all units for a faction that can still move (remaining_movement > 0).
    Returns list of {instance_id, unit_id, territory_id, remaining_movement}.
    """
    result = []

    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            # Check if unit belongs to faction
            if not unit.instance_id.startswith(faction_id + "_"):
                continue

            if unit.remaining_movement > 0:
                result.append({
                    "instance_id": unit.instance_id,
                    "unit_id": unit.unit_id,
                    "territory_id": territory_id,
                    "remaining_movement": unit.remaining_movement,
                })

    return result


def get_unit_move_targets(
    state: GameState,
    unit_instance_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> dict[str, int]:
    """
    Get all territories a specific unit can move to.
    Returns dict of {territory_id: movement_cost}.
    """
    # Find the unit and its location
    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            if unit.instance_id == unit_instance_id:
                return get_reachable_territories_for_unit(
                    unit, territory_id, state, unit_defs,
                    territory_defs, faction_defs, state.phase
                )

    return {}  # Unit not found


def get_purchasable_units(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
) -> list[dict[str, Any]]:
    """
    Get all unit types the faction can purchase with current resources.
    Returns list of {unit_id, display_name, cost, max_affordable}.
    """
    faction_resources = state.faction_resources.get(faction_id, {})
    result = []

    for unit_id, unit_def in unit_defs.items():
        if unit_def.faction != faction_id:
            continue
        if not unit_def.purchasable:
            continue

        # Calculate max affordable
        max_affordable = float('inf')
        for resource, cost in unit_def.cost.items():
            available = faction_resources.get(resource, 0)
            if cost > 0:
                max_affordable = min(max_affordable, available // cost)

        if max_affordable == float('inf'):
            max_affordable = 0

        result.append({
            "unit_id": unit_id,
            "display_name": unit_def.display_name,
            "cost": unit_def.cost,
            "max_affordable": int(max_affordable),
            "attack": unit_def.attack,
            "defense": unit_def.defense,
            "movement": unit_def.movement,
            "health": unit_def.health,
            "dice": getattr(unit_def, "dice", 1),
        })

    return result


def get_mobilization_territories(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """
    Get territories where faction can mobilize purchased units.
    Returns list of stronghold territory IDs owned by faction.
    """
    result = []

    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id:
            continue

        territory_def = territory_defs.get(territory_id)
        if territory_def and territory_def.is_stronghold:
            result.append(territory_id)

    return result


def get_mobilization_capacity(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> dict[str, Any]:
    """
    Get mobilization capacity for a faction.
    Returns dict with:
        - total_capacity: sum of power production from all owned strongholds
        - territories: list of {territory_id, power} for each stronghold
    """
    territories = []
    total = 0

    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id:
            continue

        territory_def = territory_defs.get(territory_id)
        if territory_def and territory_def.is_stronghold:
            power = territory_def.produces.get("power", 0)
            territories.append({
                "territory_id": territory_id,
                "power": power,
            })
            total += power

    return {
        "total_capacity": total,
        "territories": territories,
    }


def get_contested_territories(
    state: GameState,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition],
) -> list[dict[str, Any]]:
    """
    Get territories where faction has units alongside enemy units.
    These are territories where combat can be initiated.
    Returns list of {territory_id, attacker_count, defender_count}.
    """
    attacker_alliance = faction_defs.get(faction_id, FactionDefinition(
        "", "", "", "", "")).alliance

    result = []

    for territory_id, territory in state.territories.items():
        attacker_units = []
        defender_units = []

        for unit in territory.units:
            unit_faction = unit.instance_id.split("_")[0]
            if unit_faction == faction_id:
                attacker_units.append(unit)
            else:
                unit_alliance = faction_defs.get(unit_faction, FactionDefinition(
                    "", "", "", "", "")).alliance
                if unit_alliance != attacker_alliance:
                    defender_units.append(unit)

        if attacker_units and defender_units:
            result.append({
                "territory_id": territory_id,
                "attacker_count": len(attacker_units),
                "defender_count": len(defender_units),
                "attacker_unit_ids": [u.instance_id for u in attacker_units],
                "defender_unit_ids": [u.instance_id for u in defender_units],
            })

    return result


def get_retreat_options(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> list[str]:
    """
    Get valid retreat destinations for the current active combat.
    Returns list of adjacent friendly territory IDs.
    """
    if not state.active_combat:
        return []

    combat_territory = state.active_combat.territory_id
    combat_def = territory_defs.get(combat_territory)
    if not combat_def:
        return []

    attacker_faction = state.active_combat.attacker_faction
    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance

    result = []
    for adj_id in combat_def.adjacent:
        adj_territory = state.territories.get(adj_id)
        if not adj_territory:
            continue

        # Check if friendly (owned by same alliance or unowned)
        owner = adj_territory.owner
        if owner is None:
            result.append(adj_id)
        else:
            owner_alliance = faction_defs.get(owner, FactionDefinition(
                "", "", "", "", "")).alliance
            if owner_alliance == attacker_alliance:
                result.append(adj_id)

    return result


def get_faction_stats(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> dict[str, Any]:
    """
    Get per-faction and per-alliance stats for the UI (territories, strongholds, power, power_per_turn).
    power = current resource from faction_resources; power_per_turn = sum of produces across owned territories.
    """
    factions: dict[str, dict[str, int]] = {}
    for faction_id in faction_defs:
        territories_count = 0
        strongholds_count = 0
        power_per_turn = 0
        units_count = 0
        for tid, ts in state.territories.items():
            if ts.owner != faction_id:
                continue
            territories_count += 1
            units_count += len(ts.units)
            tdef = territory_defs.get(tid)
            if tdef and getattr(tdef, "is_stronghold", False):
                strongholds_count += 1
            if tdef and hasattr(tdef, "produces") and isinstance(tdef.produces, dict):
                power_per_turn += tdef.produces.get("power", 0)
        power = state.faction_resources.get(faction_id, {}).get("power", 0)
        factions[faction_id] = {
            "territories": territories_count,
            "strongholds": strongholds_count,
            "power": power,
            "power_per_turn": power_per_turn,
            "units": units_count,
        }

    alliances: dict[str, dict[str, int]] = {}
    for faction_id, fd in faction_defs.items():
        alliance = getattr(fd, "alliance", "") or ""
        if alliance not in alliances:
            alliances[alliance] = {"territories": 0, "strongholds": 0, "power": 0, "power_per_turn": 0, "units": 0}
        st = factions.get(faction_id, {})
        alliances[alliance]["territories"] += st.get("territories", 0)
        alliances[alliance]["strongholds"] += st.get("strongholds", 0)
        alliances[alliance]["power"] += st.get("power", 0)
        alliances[alliance]["power_per_turn"] += st.get("power_per_turn", 0)
        alliances[alliance]["units"] += st.get("units", 0)

    return {"factions": factions, "alliances": alliances}


def get_purchased_units(
    state: GameState,
    faction_id: str,
) -> list[dict[str, Any]]:
    """
    Get units purchased this turn but not yet mobilized.
    Returns list of {unit_id, count}.
    """
    purchased = state.faction_purchased_units.get(faction_id, [])
    return [{"unit_id": stack.unit_id, "count": stack.count} for stack in purchased]


def get_faction_resources(state: GameState, faction_id: str) -> dict[str, int]:
    """Get current resources for a faction."""
    return state.faction_resources.get(faction_id, {}).copy()


def get_territory_units(
    state: GameState,
    territory_id: str,
) -> list[dict[str, Any]]:
    """
    Get all units in a territory.
    Returns list of unit details.
    """
    territory = state.territories.get(territory_id)
    if not territory:
        return []

    return [
        {
            "instance_id": u.instance_id,
            "unit_id": u.unit_id,
            "remaining_movement": u.remaining_movement,
            "remaining_health": u.remaining_health,
            "base_movement": u.base_movement,
            "base_health": u.base_health,
        }
        for u in territory.units
    ]


def get_game_summary(
    state: GameState,
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> dict[str, Any]:
    """
    Get a summary of the current game state for UI display.
    """
    # Count strongholds per alliance
    stronghold_counts: dict[str, int] = {}
    for tid, ts in state.territories.items():
        td = territory_defs.get(tid)
        if td and td.is_stronghold and ts.owner:
            fd = faction_defs.get(ts.owner)
            if fd:
                alliance = fd.alliance
                stronghold_counts[alliance] = stronghold_counts.get(alliance, 0) + 1

    # Count territories per faction
    territory_counts: dict[str, int] = {}
    for ts in state.territories.values():
        if ts.owner:
            territory_counts[ts.owner] = territory_counts.get(ts.owner, 0) + 1

    # Count units per faction
    unit_counts: dict[str, int] = {}
    for ts in state.territories.values():
        for unit in ts.units:
            faction = unit.instance_id.split("_")[0]
            unit_counts[faction] = unit_counts.get(faction, 0) + 1

    return {
        "turn_number": state.turn_number,
        "current_faction": state.current_faction,
        "phase": state.phase,
        "winner": state.winner,
        "active_combat": state.active_combat.territory_id if state.active_combat else None,
        "stronghold_counts": stronghold_counts,
        "territory_counts": territory_counts,
        "unit_counts": unit_counts,
        "available_actions": get_available_action_types(state),
    }


# ===== UI-Friendly Stack-Based Queries =====

def get_territory_unit_stacks(
    state: GameState,
    territory_id: str,
    faction_id: str | None = None,
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> list[dict[str, Any]]:
    """
    Get units in a territory grouped by type (for drag-and-drop UI).

    Returns list of stacks:
    {
        "unit_id": str,
        "display_name": str,
        "count": int,
        "can_move_count": int,  # units with remaining_movement > 0
        "instance_ids": [str],  # all instance IDs in this stack
        "movable_instance_ids": [str],  # instance IDs that can move
    }
    """
    territory = state.territories.get(territory_id)
    if not territory:
        return []

    # Group units by type
    stacks: dict[str, dict] = {}

    for unit in territory.units:
        # Filter by faction if specified
        if faction_id:
            unit_faction = unit.instance_id.split("_")[0]
            if unit_faction != faction_id:
                continue

        unit_type = unit.unit_id
        if unit_type not in stacks:
            display_name = unit_type
            if unit_defs and unit_type in unit_defs:
                display_name = unit_defs[unit_type].display_name

            stacks[unit_type] = {
                "unit_id": unit_type,
                "display_name": display_name,
                "count": 0,
                "can_move_count": 0,
                "instance_ids": [],
                "movable_instance_ids": [],
            }

        stacks[unit_type]["count"] += 1
        stacks[unit_type]["instance_ids"].append(unit.instance_id)

        if unit.remaining_movement > 0:
            stacks[unit_type]["can_move_count"] += 1
            stacks[unit_type]["movable_instance_ids"].append(unit.instance_id)

    return list(stacks.values())


def get_stack_move_targets(
    state: GameState,
    territory_id: str,
    unit_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    max_units: int | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Get move targets for a stack of units of the same type.

    Shows ALL destinations reachable by ANY unit in the stack (union).
    For each destination, returns which units can reach it, sorted by
    remaining_movement descending (most mobile first).

    UI flow:
    - User drags stack -> highlight all destinations (from most mobile units)
    - User drops on destination -> show max_units as default, with +/- to adjust
    - When moving N units, take first N from instance_ids (most mobile)

    Args:
        state: Game state
        territory_id: Origin territory
        unit_id: Unit type (e.g., "gondor_infantry")
        unit_defs: Unit definitions
        territory_defs: Territory definitions
        faction_defs: Faction definitions
        max_units: Optional limit on units to consider

    Returns:
        Dict of {destination_id: {
            "cost": int,  # movement cost to reach
            "max_units": int,  # how many of this type can reach it
            "instance_ids": [str],  # units that can reach, sorted by mobility (most first)
        }}
    """
    territory = state.territories.get(territory_id)
    if not territory:
        return {}

    # Find all units of this type that can move, sorted by remaining_movement desc
    movable_units = [
        u for u in territory.units
        if u.unit_id == unit_id and u.remaining_movement > 0
    ]
    movable_units.sort(key=lambda u: u.remaining_movement, reverse=True)

    if max_units:
        movable_units = movable_units[:max_units]

    if not movable_units:
        return {}

    # Build map of unit -> remaining_movement for sorting
    unit_mobility = {u.instance_id: u.remaining_movement for u in movable_units}

    # Get destinations for each unit (union of all reachable)
    destinations: dict[str, dict] = {}

    for unit in movable_units:
        targets = get_reachable_territories_for_unit(
            unit, territory_id, state, unit_defs,
            territory_defs, faction_defs, state.phase
        )

        for dest_id, cost in targets.items():
            if dest_id not in destinations:
                destinations[dest_id] = {
                    "cost": cost,
                    "max_units": 0,
                    "instance_ids": [],
                }
            destinations[dest_id]["max_units"] += 1
            destinations[dest_id]["instance_ids"].append(unit.instance_id)

    # Sort instance_ids in each destination by mobility (most mobile first)
    for dest_info in destinations.values():
        dest_info["instance_ids"].sort(
            key=lambda iid: unit_mobility.get(iid, 0),
            reverse=True
        )

    return destinations


def get_move_preview(
    state: GameState,
    territory_id: str,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> dict[str, Any]:
    """
    Get a complete movement preview for a territory (for UI hover/selection).

    Returns all unit stacks and their possible destinations, filtered by phase.

    Returns:
    {
        "territory_id": str,
        "owner": str,
        "stacks": [
            {
                "unit_id": str,
                "display_name": str,
                "count": int,
                "can_move_count": int,
                "destinations": {
                    destination_id: {
                        "cost": int,
                        "max_units": int,
                        "is_enemy": bool,
                    }
                }
            }
        ]
    }
    """
    territory = state.territories.get(territory_id)
    if not territory:
        return {"territory_id": territory_id, "owner": None, "stacks": []}

    current_alliance = None
    current_faction_def = faction_defs.get(faction_id)
    if current_faction_def:
        current_alliance = current_faction_def.alliance

    stacks = get_territory_unit_stacks(state, territory_id, faction_id, unit_defs)

    for stack in stacks:
        # Get destinations for this stack
        raw_destinations = get_stack_move_targets(
            state, territory_id, stack["unit_id"],
            unit_defs, territory_defs, faction_defs
        )

        # Add is_enemy flag and filter for combat_move phase
        destinations = {}
        for dest_id, dest_info in raw_destinations.items():
            dest_territory = state.territories.get(dest_id)
            dest_owner = dest_territory.owner if dest_territory else None

            is_enemy = False
            if dest_owner and dest_owner != faction_id:
                dest_faction_def = faction_defs.get(dest_owner)
                if dest_faction_def and dest_faction_def.alliance != current_alliance:
                    is_enemy = True

            # Phase-based filtering
            if state.phase == "combat_move":
                # Only show enemy/neutral territories
                if dest_owner and dest_owner == faction_id:
                    continue  # Skip friendly territories
                if dest_owner and not is_enemy:
                    continue  # Skip allied territories
            elif state.phase == "non_combat_move":
                # Only show friendly/allied/neutral territories
                if is_enemy:
                    continue  # Skip enemy territories

            destinations[dest_id] = {
                "cost": dest_info["cost"],
                "max_units": dest_info["max_units"],
                "is_enemy": is_enemy,
                "instance_ids": dest_info["instance_ids"],  # Include for UI to use
            }

        stack["destinations"] = destinations

    return {
        "territory_id": territory_id,
        "owner": territory.owner,
        "stacks": stacks,
    }
