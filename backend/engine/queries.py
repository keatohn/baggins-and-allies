"""
Query functions for UI integration.
These functions help the UI understand what actions are available
without mutating game state.
"""

from dataclasses import dataclass
from typing import Any
from backend.engine.state import GameState, Unit, TerritoryState
from backend.engine.actions import Action
from backend.engine.definitions import (
    CampDefinition,
    PortDefinition,
    UnitDefinition,
    TerritoryDefinition,
    FactionDefinition,
)
from backend.engine.movement import (
    get_reachable_territories_for_unit,
    get_sea_zones_reachable_by_sail,
    get_shortest_path,
    is_friendly_territory_for_landing,
    _is_sea_zone,
)
from backend.engine.utils import get_unit_faction, has_unit_special, is_aerial_unit, is_land_unit


def _territory_has_standing_camp(
    state: GameState,
    territory_id: str,
    camp_defs: dict[str, CampDefinition],
) -> bool:
    """True if the territory has a camp that is still standing."""
    for camp_id in state.camps_standing:
        if getattr(state, "dynamic_camps", {}).get(camp_id) == territory_id:
            return True
        camp = camp_defs.get(camp_id)
        if camp and camp.territory_id == territory_id:
            return True
    return False


def _territory_has_port(
    territory_id: str,
    port_defs: dict[str, PortDefinition],
) -> bool:
    """True if the territory has a port (immutable, not destroyed on conquest)."""
    for port in (port_defs or {}).values():
        if port.territory_id == territory_id:
            return True
    return False


def _home_territory_ids(ud: UnitDefinition) -> list[str]:
    """Return list of home territory ids for this unit (supports single or multiple)."""
    ids = getattr(ud, "home_territory_ids", None)
    if ids:
        return list(ids)
    single = getattr(ud, "home_territory_id", None)
    return [single] if single else []


def _sea_zone_adjacent_to_owned_port(
    state: GameState,
    sea_zone_id: str,
    faction_id: str,
    port_defs: dict[str, PortDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> bool:
    """True if sea_zone_id is a sea zone and is adjacent to a territory that faction owns and that has a port."""
    port_defs = port_defs or {}
    sea_def = territory_defs.get(sea_zone_id)
    if not sea_def or not _is_sea_zone(sea_def):
        return False
    for adj_id in sea_def.adjacent:
        if state.territories.get(adj_id, TerritoryState(None)).owner != faction_id:
            continue
        if _territory_has_port(adj_id, port_defs):
            return True
    return False


def _port_power_for_sea_zone(
    state: GameState,
    faction_id: str,
    sea_zone_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    port_defs: dict[str, PortDefinition],
) -> int:
    """Sum of power of all owned port territories adjacent to this sea zone. 0 if not a valid naval destination."""
    port_defs = port_defs or {}
    sea_def = territory_defs.get(sea_zone_id)
    if not sea_def or not _is_sea_zone(sea_def):
        return 0
    total = 0
    for adj_id in sea_def.adjacent:
        if state.territories.get(adj_id, TerritoryState(None)).owner != faction_id:
            continue
        if not _territory_has_port(adj_id, port_defs):
            continue
        adj_def = territory_defs.get(adj_id)
        if adj_def:
            total += adj_def.produces.get("power", 0)
    return total


def _sea_zones_adjacent_to_port_territory(
    port_territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """Sea zone IDs that are adjacent to this (port) territory."""
    tdef = territory_defs.get(port_territory_id)
    if not tdef:
        return []
    return [
        adj_id for adj_id in tdef.adjacent
        if _is_sea_zone(territory_defs.get(adj_id))
    ]


def _total_pending_mobilization_to_port(
    state: GameState,
    port_territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    port_defs: dict[str, PortDefinition],
) -> int:
    """Total unit count pending mobilization to this port's pool: land to port territory + naval to any adjacent sea zone."""
    sea_zones = _sea_zones_adjacent_to_port_territory(port_territory_id, territory_defs)
    dests = [port_territory_id] + sea_zones
    total = 0
    for pm in getattr(state, "pending_mobilizations", []):
        if pm.destination in dests:
            total += sum(u.get("count", 0) for u in pm.units)
    return total


# Action type string for defender casualty order (use constant so we never typo)
SET_TERRITORY_DEFENDER_CASUALTY_ORDER = "set_territory_defender_casualty_order"

# Phase rules (duplicated from reducer to avoid circular imports)
PHASE_ALLOWED_ACTIONS = {
    "purchase": ["purchase_units", "purchase_camp", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "combat_move": ["move_units", "cancel_move", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "combat": ["initiate_combat", "continue_combat", "retreat", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "non_combat_move": ["move_units", "cancel_move", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "mobilization": ["mobilize_units", "place_camp", "queue_camp_placement", "cancel_camp_placement", "cancel_mobilization", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "end_turn", "skip_turn"],
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
    camp_defs: dict[str, CampDefinition] | None = None,
    port_defs: dict[str, PortDefinition] | None = None,
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

    # Normalize action type once (handles Action dataclass or dict-like)
    _raw = getattr(action, "type", None) or getattr(action, "action_type", None)
    if _raw is None and isinstance(action, dict):
        _raw = action.get("type") or action.get("action_type")
    action_type = (str(_raw) if _raw is not None else "").strip()

    # Check phase allows this action type
    allowed = PHASE_ALLOWED_ACTIONS.get(state.phase, [])
    if action_type and action_type not in allowed:
        return ValidationResult(
            False,
            f"Cannot {action_type} during {state.phase} phase. Allowed: {allowed}"
        )

    # Combat phase special rules
    if state.phase == "combat":
        if state.active_combat is not None:
            if action_type not in ["continue_combat", "retreat", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "skip_turn"]:
                return ValidationResult(
                    False,
                    "Active combat in progress. Must continue_combat or retreat."
                )
        else:
            if action_type in ["continue_combat", "retreat"]:
                return ValidationResult(
                    False,
                    f"No active combat to {action_type}."
                )

    # Action-specific validation (set_territory_defender_casualty_order allowed every phase on owner's turn)
    camp_defs = camp_defs or {}
    port_defs = port_defs or {}
    # Handle defender casualty order first so it can never fall through to "Unknown action type"
    if (action_type == SET_TERRITORY_DEFENDER_CASUALTY_ORDER or
            (action_type and "set_territory_defender_casualty_order" in action_type) or
            (action_type and "defender_casualty" in action_type)):
        return _validate_set_territory_defender_casualty_order(state, action)
    if action_type == "purchase_units":
        return _validate_purchase(
            state, action, unit_defs, faction_defs, territory_defs, camp_defs, port_defs
        )
    elif action_type == "move_units":
        return _validate_move(state, action, unit_defs, territory_defs, faction_defs)
    elif action_type == "initiate_combat":
        return _validate_initiate_combat(state, action, faction_defs, unit_defs, territory_defs)
    elif action_type == "mobilize_units":
        return _validate_mobilize(
            state, action, unit_defs, territory_defs, camp_defs, port_defs
        )
    elif action_type == "retreat":
        return _validate_retreat(state, action, territory_defs, faction_defs, unit_defs)
    elif action_type == "cancel_move":
        return _validate_cancel_move(state, action)
    elif action_type == "cancel_mobilization":
        return _validate_cancel_mobilization(state, action)
    elif action_type == "purchase_camp":
        return _validate_purchase_camp(state, action, camp_defs, territory_defs)
    elif action_type == "place_camp":
        return _validate_place_camp(state, action, camp_defs)
    elif action_type == "queue_camp_placement":
        return _validate_queue_camp_placement(state, action, camp_defs)
    elif action_type == "cancel_camp_placement":
        return _validate_cancel_camp_placement(state, action)
    elif action_type == "end_phase":
        return _validate_end_phase(state)
    elif action_type in ["end_turn", "continue_combat", "skip_turn"]:
        return ValidationResult(True)

    if (action_type == SET_TERRITORY_DEFENDER_CASUALTY_ORDER or
            (action_type and "defender_casualty" in action_type)):
        return _validate_set_territory_defender_casualty_order(state, action)
    return ValidationResult(False, f"Unknown action type: {action_type or getattr(action, 'type', '?')}")


def _is_naval_unit(unit_def: UnitDefinition | None) -> bool:
    if not unit_def:
        return False
    return (
        getattr(unit_def, "archetype", "") == "naval"
        or "naval" in getattr(unit_def, "tags", [])
    )


def _validate_purchase(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
    port_defs: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate a purchase_units action. Land and naval units are capped by land and sea mobilization capacity separately."""
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

    # Check mobilization capacity: land and sea are capped separately (camps vs port-adjacent sea zones)
    capacity_info = get_mobilization_capacity(
        state, faction_id, territory_defs, camp_defs or {}, port_defs or {}, unit_defs
    )
    territories_list = capacity_info.get("territories", [])
    land_capacity = sum(t.get("power", 0) for t in territories_list) + sum(
        1 for t in territories_list if t.get("home_unit_capacity")
    )
    sea_capacity = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))

    def _land_naval_counts(unit_stacks: list) -> tuple[int, int]:
        land, naval = 0, 0
        for stack in unit_stacks:
            ud = unit_defs.get(stack.unit_id)
            n = getattr(stack, "count", 0)
            if _is_naval_unit(ud):
                naval += n
            else:
                land += n
        return land, naval

    already_stacks = state.faction_purchased_units.get(faction_id, [])
    already_land, already_naval = _land_naval_counts(already_stacks)

    this_land, this_naval = 0, 0
    for unit_id, count in purchases.items():
        if _is_naval_unit(unit_defs.get(unit_id)):
            this_naval += count
        else:
            this_land += count

    if already_land + this_land > land_capacity:
        return ValidationResult(
            False,
            f"Cannot purchase that many land units: land mobilization capacity is {land_capacity} "
            f"(already purchased: {already_land} land, this purchase: {this_land} land)"
        )
    if already_naval + this_naval > sea_capacity:
        return ValidationResult(
            False,
            f"Cannot purchase that many naval units: sea mobilization capacity is {sea_capacity} "
            f"(already purchased: {already_naval} naval, this purchase: {this_naval} naval)"
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
    # API sends "to_territory"; engine action builder uses "to". Accept both.
    destination = (action.payload.get("to") or action.payload.get("to_territory")) or ""
    destination = destination.strip() if isinstance(destination, str) else ""
    origin = action.payload.get("from") or action.payload.get("from_territory") or ""
    origin = origin.strip() if isinstance(origin, str) else ""

    if not unit_instance_ids:
        return ValidationResult(False, "No units specified to move")

    if not destination:
        return ValidationResult(False, "No destination specified")

    if not origin:
        return ValidationResult(False, "No origin specified")

    origin_territory = state.territories.get(origin)
    if not origin_territory:
        return ValidationResult(False, f"Origin territory {origin} does not exist")

    # Build list of units and which can reach destination
    units_in_stack = []
    can_reach = {}
    charge_routes_by_unit = {}
    for instance_id in unit_instance_ids:
        unit = next((u for u in origin_territory.units if u.instance_id == instance_id), None)
        if not unit:
            return ValidationResult(False, f"Unit {instance_id} not found in {origin}")
        units_in_stack.append(unit)
        reachable, charge_routes = get_reachable_territories_for_unit(
            unit, origin, state, unit_defs, territory_defs, faction_defs, state.phase
        )
        can_reach[unit.instance_id] = destination in reachable
        charge_routes_by_unit[unit.instance_id] = charge_routes

    if all(can_reach[u.instance_id] for u in units_in_stack):
        charge_through = action.payload.get("charge_through")
        if charge_through is not None and isinstance(charge_through, list):
            charge_through = [str(t) for t in charge_through]
            for unit in units_in_stack:
                cr = charge_routes_by_unit.get(unit.instance_id, {})
                if charge_through and charge_through not in cr.get(destination, []):
                    return ValidationResult(
                        False,
                        f"Invalid charge_through for {destination}: not a valid charging route"
                    )
        return ValidationResult(True)

    # Not all units can reach: allow only if valid sea transport (driver + passengers, or load: land-only to sea with boats there)
    path = get_shortest_path(origin, destination, territory_defs)
    path_includes_sea = path and any(
        _is_sea_zone(territory_defs.get(tid)) for tid in path
    )
    dest_is_sea = _is_sea_zone(territory_defs.get(destination))
    origin_def = territory_defs.get(origin)
    origin_is_land = origin_def and not _is_sea_zone(origin_def)
    if not path_includes_sea and not dest_is_sea:
        return ValidationResult(
            False,
            f"Unit(s) cannot reach {destination} from {origin} (and this is not a sea transport move)"
        )
    drivers = [u for u in units_in_stack if can_reach[u.instance_id]]
    passengers = [u for u in units_in_stack if not can_reach[u.instance_id]]

    # Offload: sea -> adjacent land; no driver needs to "reach" land (boats stay in sea)
    origin_is_sea = origin_def and _is_sea_zone(origin_def)
    if origin_is_sea and not dest_is_sea and not drivers and passengers:
        land_def = territory_defs.get(destination)
        land_adj = getattr(land_def, "adjacent", []) or [] if land_def else []
        sea_adj = getattr(origin_def, "adjacent", []) or []
        if origin not in land_adj and destination not in sea_adj:
            return ValidationResult(
                False,
                f"Territory {destination} is not adjacent to sea zone {origin} (cannot offload there)",
            )
        for u in passengers:
            ud = unit_defs.get(u.unit_id)
            if not is_land_unit(ud):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be carried (only land units offload)")
            if not getattr(ud, "transportable", True):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be transported")
        naval_capacity = sum(
            getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
            for u in units_in_stack
            if _is_naval_unit(unit_defs.get(u.unit_id))
        )
        if len(passengers) > naval_capacity:
            return ValidationResult(
                False,
                f"Too many passengers ({len(passengers)}) for transport capacity ({naval_capacity})",
            )
        return ValidationResult(True)

    # Load: land -> adjacent sea; stack can be all land units; boats already in sea zone provide capacity
    if origin_is_land and dest_is_sea and not drivers and passengers:
        for u in passengers:
            ud = unit_defs.get(u.unit_id)
            if not is_land_unit(ud):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be carried (only land units can be passengers)")
            if not getattr(ud, "transportable", True):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be transported (transportable=false)")
        dest_territory = state.territories.get(destination)
        if not dest_territory:
            return ValidationResult(False, f"Destination {destination} does not exist")
        faction_id = action.faction
        load_onto_boat_id = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
        if load_onto_boat_id:
            boat_unit = next((u for u in dest_territory.units if u.instance_id == load_onto_boat_id), None)
            if not boat_unit:
                return ValidationResult(False, f"Boat {load_onto_boat_id} not found in {destination}")
            boat_ud = unit_defs.get(boat_unit.unit_id)
            if not boat_ud or (getattr(boat_ud, "archetype", "") != "naval" and "naval" not in getattr(boat_ud, "tags", [])):
                return ValidationResult(False, f"Unit {load_onto_boat_id} is not a naval unit")
            if get_unit_faction(boat_unit, unit_defs) != faction_id:
                return ValidationResult(False, f"Boat {load_onto_boat_id} does not belong to faction {faction_id}")
            cap = getattr(boat_ud, "transport_capacity", 0) or 0
            if len(passengers) > cap:
                return ValidationResult(
                    False,
                    f"Boat {load_onto_boat_id} has capacity {cap}, cannot load {len(passengers)} passengers"
                )
        else:
            naval_capacity = sum(
                getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
                for u in dest_territory.units
                if get_unit_faction(u, unit_defs) == faction_id
                and (getattr(unit_defs.get(u.unit_id), "archetype", "") == "naval" or "naval" in getattr(unit_defs.get(u.unit_id), "tags", []))
            )
            if len(passengers) > naval_capacity:
                return ValidationResult(
                    False,
                    f"Too many passengers ({len(passengers)}) for transport capacity in {destination} ({naval_capacity})"
                )
        # Load move: each passenger needs at least 1 movement
        for u in passengers:
            if getattr(u, "remaining_movement", 0) < 1:
                return ValidationResult(False, f"Unit {u.instance_id} needs 1 movement to load (has {getattr(u, 'remaining_movement', 0)})")
        return ValidationResult(True)

    if not drivers:
        return ValidationResult(False, "At least one unit (naval or aerial) must be able to reach the destination")
    for u in passengers:
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud):
            return ValidationResult(False, f"Unit {u.instance_id} cannot be carried (only land units can be passengers)")
        if not getattr(ud, "transportable", True):
            return ValidationResult(False, f"Unit {u.instance_id} cannot be transported (transportable=false)")
    naval_capacity = sum(
        getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
        for u in drivers
        if (getattr(unit_defs.get(u.unit_id), "archetype", "") == "naval" or "naval" in getattr(unit_defs.get(u.unit_id), "tags", []))
    )
    if len(passengers) > naval_capacity:
        return ValidationResult(
            False,
            f"Too many passengers ({len(passengers)}) for transport capacity ({naval_capacity})"
        )
    charge_through = action.payload.get("charge_through")
    if charge_through:
        return ValidationResult(False, "charge_through not allowed for sea transport moves")
    return ValidationResult(True)


def _validate_initiate_combat(
    state: GameState,
    action: Action,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate an initiate_combat action. Supports sea raid (attackers in adjacent sea zone, target land)."""
    territory_id = action.payload.get("territory_id")
    sea_zone_id = action.payload.get("sea_zone_id")
    if not territory_id:
        return ValidationResult(False, "No territory specified for combat")

    territory = state.territories.get(territory_id)
    if not territory:
        return ValidationResult(False, f"Territory {territory_id} does not exist")

    territory_def = territory_defs.get(territory_id)
    combat_territory_is_sea = _is_sea_zone(territory_def)
    attacker_faction = action.faction
    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance

    if sea_zone_id:
        # Sea raid: attackers in sea zone, target is land territory
        sea_zone = state.territories.get(sea_zone_id)
        sea_def = territory_defs.get(sea_zone_id)
        if not sea_zone or not sea_def or not _is_sea_zone(sea_def):
            return ValidationResult(False, f"Sea zone {sea_zone_id} is not a valid sea zone")
        if combat_territory_is_sea:
            return ValidationResult(False, "Sea raid target must be land territory")
        sea_raid_from = getattr(state, "territory_sea_raid_from", None) or {}
        if sea_raid_from.get(territory_id) != sea_zone_id:
            sea_adj = getattr(sea_def, "adjacent", []) or []
            land_adj = getattr(territory_def, "adjacent", []) or []
            if territory_id not in sea_adj and sea_zone_id not in land_adj:
                return ValidationResult(False, f"Territory {territory_id} is not adjacent to sea zone {sea_zone_id}")
        attacker_units = [u for u in sea_zone.units if get_unit_faction(u, unit_defs) == attacker_faction]
        defender_units = [
            u for u in territory.units
            if get_unit_faction(u, unit_defs) is not None
            and faction_defs.get(get_unit_faction(u, unit_defs), FactionDefinition("", "", "", "", "")).alliance != attacker_alliance
        ]
        if not attacker_units:
            return ValidationResult(False, f"No attacking units in sea zone {sea_zone_id}")
        # Allow empty defenders (conquer without battle)
        return ValidationResult(True)

    if not combat_territory_is_sea:
        for unit in territory.units:
            unit_faction = get_unit_faction(unit, unit_defs)
            if unit_faction != action.faction:
                continue
            ud = unit_defs.get(unit.unit_id)
            if _is_naval_unit(ud) and not is_aerial_unit(ud):
                return ValidationResult(
                    False,
                    "Naval units cannot attack or fight on land. For a sea raid, initiate combat with sea_zone_id (attackers in that sea zone)."
                )

    # Find attacker and defender units (normal combat: both in same territory)
    attacker_units = []
    defender_units = []
    for unit in territory.units:
        unit_faction = get_unit_faction(unit, unit_defs)
        if unit_faction == attacker_faction:
            attacker_units.append(unit)
        elif unit_faction is not None:
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
    camp_defs: dict[str, CampDefinition],
    port_defs: dict[str, PortDefinition],
) -> ValidationResult:
    """Validate a mobilize_units action. Land units require a camp; naval units require a port-adjacent sea zone."""
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

    # Determine if this batch is naval (all naval) or land (all land); mixed batches validated per unit in reducer
    all_naval = True
    all_land = True
    for item in units_to_mobilize:
        ud = unit_defs.get(item.get("unit_id"))
        is_naval = ud and (getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", []))
        if is_naval:
            all_land = False
        else:
            all_naval = False

    if all_naval:
        # Naval: destination must be a sea zone adjacent to an owned port
        if not _sea_zone_adjacent_to_owned_port(state, destination, faction_id, port_defs, territory_defs):
            return ValidationResult(
                False,
                f"Naval units can only mobilize to a sea zone adjacent to a port you own; {destination} is not valid",
            )
        # Shared capacity: each port territory P adjacent to this sea zone has pool P.power; count land to P + naval to P's adjacent sea zones
        power_production = None  # validated per-port below
    else:
        # Land: destination must be owned territory with a standing camp or a home territory for the unit type (cap 1). Ports do NOT accept land.
        has_camp = _territory_has_standing_camp(state, destination, camp_defs)
        is_home_for = {}  # unit_id -> True if this territory is home for that unit type
        for uid, ud in unit_defs.items():
            if has_unit_special(ud, "home") and destination in _home_territory_ids(ud):
                is_home_for[uid] = True
        if not has_camp and not is_home_for:
            return ValidationResult(
                False,
                f"Land units can only mobilize to a territory with a standing camp or a home territory; {destination} has neither",
            )
        if dest_territory.owner != faction_id:
            return ValidationResult(False, f"{destination} is not owned by {faction_id}")
        if not has_camp:
            # Home-only: single unit type, cap 1 total (pending + this) for that unit type to this destination
            unit_ids_in_batch = {item.get("unit_id") for item in units_to_mobilize}
            if len(unit_ids_in_batch) != 1:
                return ValidationResult(
                    False,
                    "When mobilizing to a home territory (no camp/port), all units must be the same type",
                )
            unit_id = next(iter(unit_ids_in_batch))
            if not is_home_for.get(unit_id):
                return ValidationResult(
                    False,
                    f"{destination} is not a home territory for {unit_id}",
                )
            already_pending = sum(
                u.get("count", 0)
                for pm in state.pending_mobilizations
                if pm.destination == destination
                for u in pm.units
                if u.get("unit_id") == unit_id
            )
            this_count = sum(item.get("count", 0) for item in units_to_mobilize)
            if already_pending + this_count > 1:
                return ValidationResult(
                    False,
                    f"At most 1 {unit_id} can be mobilized to home territory {destination} per phase (already {already_pending} pending)",
                )
            # Skip normal capacity check below for land; we've validated home cap
            power_production = 0
        else:
            power_production = dest_def.produces.get("power", 0)

    # If mixed batch, we still validate capacity here; per-unit naval/land rules enforced in reducer
    for item in units_to_mobilize:
        unit_id = item.get("unit_id")
        ud = unit_defs.get(unit_id)
        is_naval = ud and (getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", []))
        if is_naval and not all_naval:
            return ValidationResult(False, "Do not mix naval and land units in one mobilization")
        if not is_naval and not all_land:
            return ValidationResult(False, "Do not mix naval and land units in one mobilization")

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

    # Total mobilized to this destination (pending + this action) cannot exceed capacity
    this_action_count = sum(item.get("count", 0) for item in units_to_mobilize)
    if all_naval:
        # Naval to sea zone: shared pool with each port adjacent to this sea zone
        sea_def = territory_defs.get(destination)
        if sea_def and _is_sea_zone(sea_def):
            for adj_id in sea_def.adjacent:
                if state.territories.get(adj_id, TerritoryState(None)).owner != faction_id:
                    continue
                if not _territory_has_port(adj_id, port_defs):
                    continue
                port_power = territory_defs.get(adj_id)
                port_power_val = port_power.produces.get("power", 0) if port_power else 0
                total_for_port = _total_pending_mobilization_to_port(state, adj_id, territory_defs, port_defs)
                if total_for_port + this_action_count > port_power_val:
                    return ValidationResult(
                        False,
                        f"Cannot mobilize {this_action_count} naval to {destination}: "
                        f"port {adj_id} shared pool would exceed capacity ({total_for_port + this_action_count} > {port_power_val})",
                    )
    else:
        # Land: camp territory uses simple count; home-only already validated above (ports do not accept land)
        if not has_camp:
            pass  # Home-only: cap already enforced above
        else:
            already_pending = sum(
                sum(u.get("count", 0) for u in pm.units)
                for pm in state.pending_mobilizations
                if pm.destination == destination
            )
            if already_pending + this_action_count > power_production:
                return ValidationResult(
                    False,
                    f"Cannot mobilize {this_action_count} more to {destination}: "
                    f"already {already_pending} pending, capacity is {power_production}",
                )

    return ValidationResult(True)


def _validate_retreat(
    state: GameState,
    action: Action,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
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

    # Check destination is adjacent: ground-only if any retreating unit is land, else allow aerial_adjacent
    retreat_adjacent = _get_retreat_adjacent_ids(state, territory_defs, unit_defs)
    if destination not in retreat_adjacent:
        return ValidationResult(
            False,
            f"{destination} is not adjacent to {combat_territory}"
        )

    dest_territory = state.territories.get(destination)
    if not dest_territory:
        return ValidationResult(False, f"Territory {destination} does not exist")

    attacker_faction = action.faction
    if not _territory_is_friendly_for_retreat(dest_territory, attacker_faction, faction_defs, unit_defs):
        return ValidationResult(
            False,
            f"Cannot retreat to {destination}: must be allied territory"
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


def _validate_purchase_camp(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate a purchase_camp action. Only territories that produce power are valid (so units can mobilize there)."""
    faction_id = action.faction
    if state.phase != "purchase" or state.current_faction != faction_id:
        return ValidationResult(False, "Can only purchase a camp during your purchase phase")
    cost = getattr(state, "camp_cost", 0)
    power = state.faction_resources.get(faction_id, {}).get("power", 0)
    if power < cost:
        return ValidationResult(False, f"Insufficient power: need {cost}, have {power}")
    owned_at_start = getattr(state, "faction_territories_at_turn_start", {}).get(faction_id, [])
    already_placed = [
        p.get("placed_territory_id") for p in getattr(state, "pending_camps", [])
        if p.get("placed_territory_id")
    ]
    options = []
    for tid in owned_at_start:
        if tid in already_placed:
            continue
        if _territory_has_standing_camp(state, tid, camp_defs):
            continue
        tdef = territory_defs.get(tid)
        if tdef and (tdef.produces.get("power", 0) or 0) > 0:
            options.append(tid)
    if not options:
        return ValidationResult(False, "No valid territory to place a camp (need owned territory with power production)")
    return ValidationResult(True)


def _validate_set_territory_defender_casualty_order(
    state: GameState,
    action: Action,
) -> ValidationResult:
    """Validate set_territory_defender_casualty_order: territory must exist and be owned by the acting faction."""
    territory_id = action.payload.get("territory_id")
    casualty_order = action.payload.get("casualty_order")
    if not territory_id:
        return ValidationResult(False, "payload must include territory_id")
    if not casualty_order or casualty_order not in ("best_unit", "best_defense"):
        return ValidationResult(False, "casualty_order must be 'best_unit' or 'best_defense'")
    territory = state.territories.get(territory_id)
    if not territory:
        return ValidationResult(False, f"Unknown territory: {territory_id}")
    if territory.owner != action.faction:
        return ValidationResult(False, f"Only the owner of {territory_id} can set defensive casualty priority")
    return ValidationResult(True)


def _validate_end_phase(state: GameState) -> ValidationResult:
    """Validate end_phase: in mobilization, all purchased camps must be placed or queued."""
    if state.phase != "mobilization":
        return ValidationResult(True)
    pending = getattr(state, "pending_camps", [])
    queued_indices = {p.camp_index for p in getattr(state, "pending_camp_placements", [])}
    unplaced = [
        p for i, p in enumerate(pending)
        if not p.get("placed_territory_id") and i not in queued_indices
    ]
    if unplaced:
        return ValidationResult(
            False,
            f"Place or queue all camps before ending mobilization ({len(unplaced)} camp(s) remaining)",
        )
    return ValidationResult(True)


def _validate_place_camp(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
) -> ValidationResult:
    """Validate a place_camp action."""
    faction_id = action.faction
    if state.phase != "mobilization" or state.current_faction != faction_id:
        return ValidationResult(False, "Can only place a camp during your mobilization phase")
    camp_index = action.payload.get("camp_index", -1)
    territory_id = action.payload.get("territory_id", "")
    pending = getattr(state, "pending_camps", [])
    if camp_index < 0 or camp_index >= len(pending):
        return ValidationResult(False, f"Invalid camp_index: {camp_index}")
    if pending[camp_index].get("placed_territory_id"):
        return ValidationResult(False, "Camp already placed")
    options = pending[camp_index].get("territory_options") or []
    if territory_id not in options:
        return ValidationResult(False, f"Territory {territory_id} not in placement options")
    if _territory_has_standing_camp(state, territory_id, camp_defs):
        return ValidationResult(False, f"Territory {territory_id} already has a camp")
    return ValidationResult(True)


def _validate_queue_camp_placement(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
) -> ValidationResult:
    """Validate a queue_camp_placement action (same as place_camp; camp must not already be queued; territory must not have another pending placement)."""
    r = _validate_place_camp(state, action, camp_defs)
    if not r.valid:
        return r
    camp_index = action.payload.get("camp_index", -1)
    territory_id = action.payload.get("territory_id", "")
    pending_placements = getattr(state, "pending_camp_placements", [])
    for p in pending_placements:
        if p.camp_index == camp_index:
            return ValidationResult(False, "Camp already queued for placement")
        if p.territory_id == territory_id:
            return ValidationResult(False, "Territory already has a pending camp placement")
    return ValidationResult(True)


def _validate_cancel_camp_placement(state: GameState, action: Action) -> ValidationResult:
    """Validate a cancel_camp_placement action."""
    idx = action.payload.get("placement_index", -1)
    pending = getattr(state, "pending_camp_placements", [])
    if idx < 0 or idx >= len(pending):
        return ValidationResult(
            False,
            f"Invalid placement_index: {idx}. Pending: {len(pending)}"
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
            allowed = [a for a in allowed if a in ["continue_combat", "retreat", "set_territory_defender_casualty_order"]]
        else:
            allowed = [a for a in allowed if a not in ["continue_combat", "retreat"]]

    return allowed


def get_movable_units(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> list[dict[str, Any]]:
    """
    Get all units for a faction that can still move (remaining_movement > 0).
    Includes units in any territory (owned, allied, or neutral) that belong to the current faction.
    Returns list of {instance_id, unit_id, territory_id, remaining_movement}.
    """
    result = []

    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            # Unit belongs to faction if instance_id prefix matches or unit_def.faction matches (e.g. units in neutral with def faction)
            belongs = unit.instance_id.startswith(faction_id + "_")
            if not belongs and unit_defs:
                unit_faction = get_unit_faction(unit, unit_defs)
                belongs = unit_faction == faction_id
            if not belongs:
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
) -> tuple[dict[str, int], dict[str, list[list[str]]]]:
    """
    Get all territories a specific unit can move to.
    Returns (targets_dict, charge_routes).
    - targets_dict: territory_id -> movement_cost
    - charge_routes: for cavalry in combat_move, territory_id -> list of charge_through paths (empty enemy IDs)
    """
    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            if unit.instance_id == unit_instance_id:
                return get_reachable_territories_for_unit(
                    unit, territory_id, state, unit_defs,
                    territory_defs, faction_defs, state.phase
                )

    return {}, {}  # Unit not found


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
    camp_defs: dict[str, CampDefinition] | None = None,
    port_defs: dict[str, PortDefinition] | None = None,
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> list[str]:
    """
    Get territory IDs where faction can mobilize land units:
    - Owned territories with a standing camp, or
    - Owned territories that are home_territory_id for some unit type (cap 1 per unit type; no camp required).
    Ports do NOT allow land deployment; they only mobilize naval units to adjacent sea zones.
    """
    camp_defs = camp_defs or {}
    port_defs = port_defs or {}
    unit_defs = unit_defs or {}
    result = []
    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id:
            continue
        if _territory_has_standing_camp(state, territory_id, camp_defs):
            result.append(territory_id)
    # Home territories: owned, no camp, but at least one unit type has this as home
    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id or territory_id in result:
            continue
        if _territory_has_standing_camp(state, territory_id, camp_defs) or _territory_has_port(territory_id, port_defs):
            continue
        for ud in unit_defs.values():
            if has_unit_special(ud, "home") and territory_id in _home_territory_ids(ud):
                result.append(territory_id)
                break
    return result


def get_mobilization_sea_zones(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    port_defs: dict[str, PortDefinition] | None = None,
) -> list[str]:
    """
    Get sea zone IDs where faction can mobilize naval units (sea zones adjacent to an owned port).
    """
    port_defs = port_defs or {}
    result = []
    seen = set()
    for tid, tdef in territory_defs.items():
        if not _is_sea_zone(tdef) or tid in seen:
            continue
        if _sea_zone_adjacent_to_owned_port(state, tid, faction_id, port_defs, territory_defs):
            result.append(tid)
            seen.add(tid)
    return result


def get_mobilization_capacity(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
    port_defs: dict[str, PortDefinition] | None = None,
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> dict[str, Any]:
    """
    Get mobilization capacity for a faction.
    Returns dict with:
        - total_capacity: sum of power from camps (land) + port territories (shared land+sea pool)
        - territories: list of {territory_id, power[, home_unit_capacity]} for camp-only and home-only territories
        - port_territories: list of {territory_id, power, sea_zone_ids} for port territories (land to port shares pool with naval to adjacent sea zones)
        - sea_zones: list of {sea_zone_id, power} for port-adjacent sea zones (naval mobilization)
    Home-only territories have power 0 and home_unit_capacity: { unit_id: 1 } (max 1 unit of that type per phase).
    """
    camp_defs = camp_defs or {}
    port_defs = port_defs or {}
    unit_defs = unit_defs or {}
    territories = []
    port_territories = []
    total = 0
    seen = set()
    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id or territory_id in seen:
            continue
        territory_def = territory_defs.get(territory_id)
        if not territory_def:
            continue
        power = territory_def.produces.get("power", 0)
        if _territory_has_standing_camp(state, territory_id, camp_defs):
            seen.add(territory_id)
            territories.append({"territory_id": territory_id, "power": power})
            total += power
        elif _territory_has_port(territory_id, port_defs):
            seen.add(territory_id)
            sea_zone_ids = _sea_zones_adjacent_to_port_territory(territory_id, territory_defs)
            port_territories.append({
                "territory_id": territory_id,
                "power": power,
                "sea_zone_ids": sea_zone_ids,
            })
            # Ports only mobilize naval to sea zones; do not add to total land capacity
    # Home-only territories: owned, no camp/port; cap 1 per unit type that has this as home
    for territory_id, territory in state.territories.items():
        if territory.owner != faction_id or territory_id in seen:
            continue
        if _territory_has_standing_camp(state, territory_id, camp_defs) or _territory_has_port(territory_id, port_defs):
            continue
        home_units: dict[str, int] = {}
        for unit_id, ud in unit_defs.items():
            if has_unit_special(ud, "home") and territory_id in _home_territory_ids(ud):
                home_units[unit_id] = 1
        if home_units:
            seen.add(territory_id)
            territories.append({
                "territory_id": territory_id,
                "power": 0,
                "home_unit_capacity": home_units,
            })
            total += 1  # 1 land slot per home territory (cap 1 per unit type, for purchase total we count 1)

    sea_zones = []
    for tid, tdef in territory_defs.items():
        if not _is_sea_zone(tdef):
            continue
        power = _port_power_for_sea_zone(state, faction_id, tid, territory_defs, port_defs)
        if power > 0:
            sea_zones.append({"sea_zone_id": tid, "power": power})

    return {
        "total_capacity": total,
        "territories": territories,
        "port_territories": port_territories,
        "sea_zones": sea_zones,
    }


def get_contested_territories(
    state: GameState,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Get territories where faction has units alongside enemy units.
    These are territories where combat can be initiated.
    For sea zones, only naval units count (naval combat); land territories use all units.
    Returns list of {territory_id, attacker_count, defender_count}.
    """
    attacker_alliance = faction_defs.get(faction_id, FactionDefinition(
        "", "", "", "", "")).alliance

    result = []

    for territory_id, territory in state.territories.items():
        attacker_units = []
        defender_units = []

        is_sea = False
        if territory_defs:
            tdef = territory_defs.get(territory_id)
            is_sea = tdef and getattr(tdef, "terrain_type", "").lower() == "sea"

        for unit in territory.units:
            unit_faction = get_unit_faction(unit, unit_defs)
            if is_sea and not _is_naval_unit(unit_defs.get(unit.unit_id)):
                continue  # In sea zones only naval units are combatants
            if unit_faction == faction_id:
                attacker_units.append(unit)
            elif unit_faction is not None:
                unit_alliance = faction_defs.get(unit_faction, FactionDefinition(
                    "", "", "", "", "")).alliance
                if unit_alliance != attacker_alliance:
                    defender_units.append(unit)

        if attacker_units and defender_units:
            entry = {
                "territory_id": territory_id,
                "attacker_count": len(attacker_units),
                "defender_count": len(defender_units),
                "attacker_unit_ids": [u.instance_id for u in attacker_units],
                "defender_unit_ids": [u.instance_id for u in defender_units],
            }
            sea_raid_from = getattr(state, "territory_sea_raid_from", None) or {}
            if territory_id in sea_raid_from:
                entry["sea_zone_id"] = sea_raid_from[territory_id]
            result.append(entry)

    return result


def get_sea_zones_adjacent_to_land(
    land_territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """Sea zone IDs that are adjacent to the given land territory (for offload targets)."""
    land_def = territory_defs.get(land_territory_id)
    if not land_def or _is_sea_zone(land_def):
        return []
    adj = getattr(land_def, "adjacent", []) or []
    return [
        tid for tid in adj
        if territory_defs.get(tid) and _is_sea_zone(territory_defs.get(tid))
    ]


def get_valid_offload_sea_zones(
    from_territory: str,
    to_land_territory: str,
    state: GameState,
    unit_instance_ids: list[str],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    phase: str,
) -> list[str]:
    """
    Sea zones from which the stack can offload to to_land_territory: must be both
    (1) adjacent to to_land_territory and (2) reachable by sail from from_territory
    (using the moving stack's naval units). Used when user drags sea -> land; boat
    sails to one of these zones, then passengers offload to land.
    """
    adjacent_seas = set(get_sea_zones_adjacent_to_land(to_land_territory, territory_defs))
    if not adjacent_seas:
        return []
    from_terr = state.territories.get(from_territory)
    if not from_terr:
        return []
    units_in_stack = [u for u in from_terr.units if u.instance_id in unit_instance_ids]
    drivers = [
        u for u in units_in_stack
        if _is_naval_unit(unit_defs.get(u.unit_id))
    ]
    if not drivers:
        return []
    # Sea zones reachable by sail (BFS over sea only; ignores combat_move "must attack" so empty seas count for offload)
    reachable_sea = get_sea_zones_reachable_by_sail(
        from_territory, state, drivers, territory_defs, unit_defs, faction_defs
    )
    if not reachable_sea:
        return []
    valid = sorted(adjacent_seas & reachable_sea)
    return valid


def get_sea_raid_targets(
    state: GameState,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Land territories that can be sea-raided: adjacent to a sea zone where the faction
    has at least one naval unit, at least one passenger (land unit), and no enemy units.
    Returns list of { territory_id, sea_zone_id } for the frontend to show as sea raid options.
    """
    if not territory_defs:
        return []
    attacker_alliance = faction_defs.get(faction_id, FactionDefinition("", "", "", "", "")).alliance
    result = []
    for sea_zone_id, territory in state.territories.items():
        tdef = territory_defs.get(sea_zone_id)
        if not tdef or getattr(tdef, "terrain_type", "").lower() != "sea":
            continue
        my_naval = []
        my_land = []
        enemy_units = False
        for unit in territory.units:
            uf = get_unit_faction(unit, unit_defs)
            if uf == faction_id:
                if _is_naval_unit(unit_defs.get(unit.unit_id)):
                    my_naval.append(unit)
                else:
                    my_land.append(unit)
            elif uf is not None:
                other_alliance = faction_defs.get(uf, FactionDefinition("", "", "", "", "")).alliance
                if other_alliance != attacker_alliance:
                    enemy_units = True
                    break
        if enemy_units or not my_naval or not my_land:
            continue
        # Adjacent land: from sea's adjacent list, or any land that lists this sea zone (symmetric)
        adj_lands = set()
        for adj_id in getattr(tdef, "adjacent", []) or []:
            adj_def = territory_defs.get(adj_id)
            if adj_def and getattr(adj_def, "terrain_type", "").lower() != "sea":
                adj_lands.add(adj_id)
        for tid, land_def in territory_defs.items():
            if getattr(land_def, "terrain_type", "").lower() == "sea":
                continue
            if sea_zone_id in (getattr(land_def, "adjacent", []) or []):
                adj_lands.add(tid)
        for adj_id in adj_lands:
            result.append({"territory_id": adj_id, "sea_zone_id": sea_zone_id})
    return result


def _territory_is_friendly_for_retreat(
    territory: TerritoryState,
    attacker_faction: str,
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    """
    True if a territory is valid for retreat: allied-owned only (same alliance).
    Neutral (unowned) territory is not valid for retreat.
    """
    owner = territory.owner
    if owner is None:
        return False

    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance
    owner_alliance = faction_defs.get(owner, FactionDefinition("", "", "", "", "")).alliance
    return owner_alliance == attacker_alliance


def _get_retreat_adjacent_ids(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> list[str]:
    """
    Territory IDs that count as adjacent for this combat's retreat.
    If any retreating unit is land, only ground-adjacent; if all are aerial, allow aerial_adjacent too.
    (All attackers must stay together, so land units restrict the group to ground-adjacent only.)
    """
    if not state.active_combat:
        return []
    combat = state.active_combat
    combat_territory_id = combat.territory_id
    combat_def = territory_defs.get(combat_territory_id)
    if not combat_def:
        return []
    # Retreating units: for sea raid they are in the sea zone; otherwise in the combat territory
    sea_zone_id = getattr(combat, "sea_zone_id", None)
    source_id = sea_zone_id if sea_zone_id else combat_territory_id
    source = state.territories.get(source_id)
    if not source:
        return []
    surviving_ids = set(combat.attacker_instance_ids)
    retreating_units = [u for u in source.units if u.instance_id in surviving_ids]
    any_land = any(is_land_unit(unit_defs.get(u.unit_id)) for u in retreating_units)
    if any_land:
        return list(combat_def.adjacent)
    return list(dict.fromkeys(
        list(combat_def.adjacent) + getattr(combat_def, "aerial_adjacent", [])
    ))


def get_retreat_options(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> list[str]:
    """
    Get valid retreat destinations for the current active combat.
    Returns list of adjacent territory IDs that are allied (same alliance).
    Only ground-adjacent if any retreating unit is land; aerial_adjacent allowed only if all are aerial.
    """
    if not state.active_combat:
        return []

    combat_territory = state.active_combat.territory_id
    combat_def = territory_defs.get(combat_territory)
    if not combat_def:
        return []

    attacker_faction = state.active_combat.attacker_faction
    result = []
    retreat_adjacent = _get_retreat_adjacent_ids(state, territory_defs, unit_defs)
    for adj_id in retreat_adjacent:
        adj_territory = state.territories.get(adj_id)
        if not adj_territory:
            continue
        if _territory_is_friendly_for_retreat(adj_territory, attacker_faction, faction_defs, unit_defs):
            result.append(adj_id)

    return result


def get_aerial_units_must_move(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    current_faction: str,
) -> list[dict[str, str]]:
    """
    Aerial units that attacked but did not conquer are in enemy/non-friendly territory.
    They must move to friendly territory before non-combat move phase can end.
    Returns list of {"territory_id", "unit_id", "instance_id"} for current_faction's aerial units
    that are in a territory that is not friendly for landing.
    """
    result: list[dict[str, str]] = []
    for territory_id, territory in state.territories.items():
        unit_faction = None
        for unit in territory.units:
            u_faction = get_unit_faction(unit, unit_defs)
            if u_faction != current_faction:
                continue
            unit_def = unit_defs.get(unit.unit_id)
            if not is_aerial_unit(unit_def):
                continue
            if is_friendly_territory_for_landing(
                territory, current_faction, faction_defs, unit_defs
            ):
                continue
            result.append({
                "territory_id": territory_id,
                "unit_id": unit.unit_id,
                "instance_id": unit.instance_id,
            })
    return result


def get_faction_stats(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> dict[str, Any]:
    """
    Get per-faction and per-alliance stats for the UI (territories, strongholds, power, power_per_turn, units, unit_power).
    power = current resource from faction_resources; power_per_turn = sum of produces across owned territories.
    unit_power = sum of power cost for all active units for that faction.
    """
    unit_defs = unit_defs or {}
    factions: dict[str, dict[str, int]] = {}
    for faction_id in faction_defs:
        territories_count = 0
        strongholds_count = 0
        power_per_turn = 0
        for tid, ts in state.territories.items():
            if ts.owner != faction_id:
                continue
            territories_count += 1
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
            "units": 0,
            "unit_power": 0,
        }

    # Count units and unit_power by unit's faction (so sea units in sea zones are included)
    for tid, ts in state.territories.items():
        for unit in ts.units:
            ud = unit_defs.get(unit.unit_id)
            if not ud:
                continue
            fid = getattr(ud, "faction", None)
            if fid not in factions:
                continue
            factions[fid]["units"] += 1
            if isinstance(getattr(ud, "cost", None), dict):
                factions[fid]["unit_power"] += ud.cost.get("power", 0)

    alliances: dict[str, dict[str, int]] = {}
    for faction_id, fd in faction_defs.items():
        alliance = getattr(fd, "alliance", "") or ""
        if alliance not in alliances:
            alliances[alliance] = {"territories": 0, "strongholds": 0, "power": 0, "power_per_turn": 0, "units": 0, "unit_power": 0}
        st = factions.get(faction_id, {})
        alliances[alliance]["territories"] += st.get("territories", 0)
        alliances[alliance]["strongholds"] += st.get("strongholds", 0)
        alliances[alliance]["power"] += st.get("power", 0)
        alliances[alliance]["power_per_turn"] += st.get("power_per_turn", 0)
        alliances[alliance]["units"] += st.get("units", 0)
        alliances[alliance]["unit_power"] += st.get("unit_power", 0)

    # Strongholds with no owner (e.g. Moria at start) for UI bar: good | neutral | evil
    neutral_strongholds = 0
    for tid, ts in state.territories.items():
        if ts.owner is not None:
            continue
        tdef = territory_defs.get(tid)
        if tdef and getattr(tdef, "is_stronghold", False):
            neutral_strongholds += 1

    return {"factions": factions, "alliances": alliances, "neutral_strongholds": neutral_strongholds}


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
    unit_defs: dict[str, UnitDefinition],
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
            faction = get_unit_faction(unit, unit_defs)
            if faction:
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
            unit_faction = get_unit_faction(unit, unit_defs) if unit_defs else (unit.instance_id.split("_")[0] if unit.instance_id else None)
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
        targets, _ = get_reachable_territories_for_unit(
            unit, territory_id, state, unit_defs,
            territory_defs, faction_defs, state.phase
        )

        for dest_id, cost in targets.items():
            if dest_id == territory_id:
                continue  # Never allow move from X to X
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
            if dest_id == territory_id:
                continue  # Never allow move from X to X
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
