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
    controlling_faction_id,
    faction_acts_as,
    is_transportable,
)
from backend.engine.utils import effective_territory_owner, faction_owns_capital
from backend.engine.rings import power_for_faction
from backend.engine.special_rules import territory_current_power
from backend.engine.movement import (
    _is_river_unit,
    _is_river_zone,
    _is_sea_zone,
    _is_transport_boat_for_zone,
    _is_water_zone,
    _sea_zone_has_hostile_enemy_boats,
    are_sea_zones_directly_adjacent,
    empty_sea_zone_valid_for_combat_move_sail_then_load_raid,
    get_forced_naval_combat_instance_ids,
    expand_sea_offload_instance_ids,
    get_reachable_territories_for_unit,
    get_sea_zones_reachable_by_sail,
    get_shortest_path,
    is_friendly_territory_for_landing,
    ford_shortcut_requires_escort_lead,
    land_move_ford_escort_cost_for_instances,
    pending_ford_crosser_lead_move_from_origin,
    pending_move_is_same_phase_load_into_sea,
    remaining_ford_escort_slots,
    remaining_load_slots_on_boat,
    remaining_sea_load_passenger_slots,
    resolve_territory_key_in_state,
    resolve_unit_for_move_declaration,
    sea_zone_ids_match,
    sort_sea_zone_ids_numerically,
    water_transport_relation,
)
from backend.engine.subfaction_rules import (
    child_ids,
    parent_may_purchase_unit,
    pool_territory_ids,
    purchase_capacity_error,
    rule_for,
    subfaction_mobilization_error,
    unit_destination_spec,
)
from backend.engine.utils import (
    effective_territory_owner,
    get_unit_faction,
    has_unit_special,
    is_aerial_unit,
    is_land_unit,
    unit_hero_id,
)


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
    """Return list of home territory ids for this unit. Uses home_territory_ids only."""
    ids = getattr(ud, "home_territory_ids", None)
    return list(ids) if ids else []


def subfaction_owns_for(
    owner: str | None,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
) -> bool:
    """True when owner is a subfaction of faction_id. Parent home units may deploy on its land."""
    if not owner or owner == faction_id or not faction_defs:
        return False
    return controlling_faction_id(faction_defs, owner) == faction_id


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
            total += territory_current_power(state, adj_id, adj_def)
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


def _purchase_kind(unit_def: UnitDefinition | None) -> str:
    """'naval', 'river', or 'land'. River craft are not ships and do not spend sea capacity."""
    if _is_naval_unit(unit_def):
        return "naval"
    if _is_river_unit(unit_def):
        return "river"
    return "land"


def _territories_owned_at_turn_start(state: GameState, faction_id: str) -> set[str]:
    raw = getattr(state, "faction_territories_at_turn_start", None) or {}
    return set(raw.get(faction_id) or [])


def river_banks_for_zone(
    state: GameState,
    faction_id: str,
    zone_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """
    Land territories this faction owns that border the river zone and were owned at turn start.
    An allied bank does not count. A bank captured this turn does not count.
    """
    zdef = territory_defs.get(zone_id)
    if not _is_river_zone(zdef):
        return []
    owned_at_start = _territories_owned_at_turn_start(state, faction_id)
    banks: list[str] = []
    for adj_id in getattr(zdef, "adjacent", []) or []:
        if adj_id not in owned_at_start:
            continue
        terr = state.territories.get(adj_id)
        if not terr or terr.owner != faction_id:
            continue
        adj_def = territory_defs.get(adj_id)
        if not adj_def or _is_water_zone(adj_def):
            continue
        banks.append(adj_id)
    return banks


def _pending_mobilization_counts_by_kind(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
) -> tuple[dict[str, int], dict[str, int]]:
    """destination -> count, split into land (not naval, not river) and river craft."""
    land: dict[str, int] = {}
    river: dict[str, int] = {}
    for pm in getattr(state, "pending_mobilizations", []) or []:
        dest = getattr(pm, "destination", None) or ""
        for item in getattr(pm, "units", None) or []:
            n = int(item.get("count", 0) or 0)
            if n <= 0:
                continue
            kind = _purchase_kind(unit_defs.get(item.get("unit_id")))
            if kind == "river":
                river[dest] = river.get(dest, 0) + n
            elif kind == "land":
                land[dest] = land.get(dest, 0) + n
    return land, river


def _boats_fit_on_banks(
    demands: list[tuple[str, int]],
    remaining: dict[str, int],
    zone_banks: dict[str, list[str]],
) -> bool:
    """True if each river zone's boats can be charged to its adjacent banks without exceeding power."""
    rem = dict(remaining)

    def rec(i: int) -> bool:
        if i >= len(demands):
            return True
        zone_id, count = demands[i]
        banks = zone_banks.get(zone_id) or []

        def distribute(bank_index: int, left: int) -> bool:
            if left == 0:
                return rec(i + 1)
            if bank_index >= len(banks):
                return False
            bank_id = banks[bank_index]
            max_take = min(left, rem.get(bank_id, 0))
            for take in range(max_take, -1, -1):
                rem[bank_id] = rem.get(bank_id, 0) - take
                if distribute(bank_index + 1, left - take):
                    return True
                rem[bank_id] = rem.get(bank_id, 0) + take
            return False

        return distribute(0, count)

    return rec(0)


def river_mobilization_fits(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict[str, UnitDefinition],
    extra_land: dict[str, int] | None = None,
    extra_river: dict[str, int] | None = None,
) -> tuple[bool, str]:
    """
    Each turn-start-owned river bank has a pool equal to its power.
    Land mobilized on that bank and rowboats mobilized into adjacent river zones share the pool.
    A boat on a segment bordered by several of your banks can draw from their combined remaining power.
    """
    land, river = _pending_mobilization_counts_by_kind(state, unit_defs)
    for dest, n in (extra_land or {}).items():
        land[dest] = land.get(dest, 0) + int(n)
    for dest, n in (extra_river or {}).items():
        river[dest] = river.get(dest, 0) + int(n)

    banks: dict[str, int] = {}
    zone_banks: dict[str, list[str]] = {}
    for zone_id, zdef in territory_defs.items():
        if not _is_river_zone(zdef):
            continue
        bs = river_banks_for_zone(state, faction_id, zone_id, territory_defs)
        zone_banks[zone_id] = bs
        for bank_id in bs:
            if bank_id in banks:
                continue
            bdef = territory_defs.get(bank_id)
            banks[bank_id] = territory_current_power(state, bank_id, bdef) if bdef else 0

    for bank_id, power in banks.items():
        if land.get(bank_id, 0) > power:
            return False, (
                f"Cannot mobilize that many units at {bank_id}: "
                f"river bank capacity is {power}"
            )

    remaining = {bank_id: power - land.get(bank_id, 0) for bank_id, power in banks.items()}
    demands = [(zone_id, n) for zone_id, n in river.items() if n > 0]
    for zone_id, n in demands:
        if not zone_banks.get(zone_id):
            return False, (
                f"River units can only mobilize to a river zone that borders a territory you owned "
                f"at the start of your turn; {zone_id} is not valid"
            )
        if n > sum(remaining.get(b, 0) for b in zone_banks[zone_id]):
            return False, (
                f"Cannot mobilize {n} river units to {zone_id}: bordering territories do not have "
                f"enough power left"
            )
    if not _boats_fit_on_banks(demands, remaining, zone_banks):
        return False, "Not enough river mobilization capacity on the bordering territories"
    return True, ""


def river_mobilization_capacity_total(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> int:
    """Sum of power of your turn-start river banks, each bank once."""
    seen: set[str] = set()
    total = 0
    for zone_id, zdef in territory_defs.items():
        if not _is_river_zone(zdef):
            continue
        for bank_id in river_banks_for_zone(state, faction_id, zone_id, territory_defs):
            if bank_id in seen:
                continue
            seen.add(bank_id)
            bdef = territory_defs.get(bank_id)
            if bdef:
                total += territory_current_power(state, bank_id, bdef)
    return total


# Action type string for defender casualty order (use constant so we never typo)
SET_TERRITORY_DEFENDER_CASUALTY_ORDER = "set_territory_defender_casualty_order"

# Phase rules (duplicated from reducer to avoid circular imports)
PHASE_ALLOWED_ACTIONS = {
    "purchase": ["purchase_units", "purchase_camp", "repair_stronghold", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
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
            state,
            action,
            unit_defs,
            territory_defs,
            camp_defs,
            port_defs,
            faction_defs,
        )
    elif action_type == "retreat":
        return _validate_retreat(state, action, territory_defs, faction_defs, unit_defs)
    elif action_type == "cancel_move":
        return _validate_cancel_move(state, action)
    elif action_type == "cancel_mobilization":
        return _validate_cancel_mobilization(state, action)
    elif action_type == "purchase_camp":
        return _validate_purchase_camp(state, action, camp_defs, territory_defs)
    elif action_type == "repair_stronghold":
        return _validate_repair_stronghold(state, action, territory_defs)
    elif action_type == "place_camp":
        return _validate_place_camp(state, action, camp_defs, territory_defs)
    elif action_type == "queue_camp_placement":
        return _validate_queue_camp_placement(state, action, camp_defs, territory_defs)
    elif action_type == "cancel_camp_placement":
        return _validate_cancel_camp_placement(state, action)
    elif action_type == "end_phase":
        return _validate_end_phase(
            state,
            faction_defs=faction_defs,
            unit_defs=unit_defs,
            territory_defs=territory_defs,
            camp_defs=camp_defs,
            port_defs=port_defs,
        )
    elif action_type == "end_turn":
        if state.phase == "mobilization":
            return _validate_end_phase(
                state,
                faction_defs=faction_defs,
                unit_defs=unit_defs,
                territory_defs=territory_defs,
                camp_defs=camp_defs,
                port_defs=port_defs,
            )
        return ValidationResult(True)
    elif action_type in ["continue_combat", "skip_turn"]:
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


def participates_in_sea_hex_naval_combat(unit, unit_def: UnitDefinition | None) -> bool:
    """
    Combat in a sea or river territory: hulls (not cargo) and aerial units that are not embarked
    roll and appear as combatants. Matches initiate_combat roster rules.
    """
    if getattr(unit, "loaded_onto", None):
        return False
    if _is_naval_unit(unit_def) or _is_river_unit(unit_def):
        return True
    return is_aerial_unit(unit_def)


def _land_combat_unit_side_for_queries(
    unit: Unit,
    attacker_faction: str,
    attacker_alliance: str | None,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> str | None:
    """Mirror reducer._land_combat_unit_side for initiate_combat validation."""
    uo = get_unit_faction(unit, unit_defs)
    if uo is None:
        return None
    if faction_acts_as(faction_defs, uo, attacker_faction):
        return "attacker"
    ua = getattr(faction_defs.get(uo), "alliance", None)
    if ua != attacker_alliance:
        return "defender"
    return None


def validate_move_as_sea_offload_if_applicable(
    state: GameState,
    origin: str,
    destination: str,
    units_in_stack: list,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    faction_id: str,
    charge_through: Any,
) -> ValidationResult | None:
    """
    Sea zone -> adjacent land: dragging a boat with passengers means land units go ashore (offload / sea raid);
    naval units in the request stay in the sea — they must be included for transport capacity, not treated as moving to land.

    Returns ValidationResult(True/False) when this rule applies (stack includes at least one offloadable land unit),
    or None if some other move validation path should handle the action.
    """
    origin_def = territory_defs.get(origin)
    dest_def = territory_defs.get(destination)
    if water_transport_relation(origin_def, dest_def) != "offload":
        return None

    def _is_zone_craft(ud: UnitDefinition | None) -> bool:
        return _is_transport_boat_for_zone(ud, origin_def)

    land_offload: list = []
    naval_in_move: list = []
    for u in units_in_stack:
        ud = unit_defs.get(u.unit_id)
        if _is_zone_craft(ud):
            naval_in_move.append(u)
        elif is_land_unit(ud) and not _is_zone_craft(ud):
            # Passengers ashore/offload: land, non-naval. Do not require is_transportable here —
            # load validation already enforced that; treating only transportable caused "orphan"
            # units (in stack but neither naval nor land_offload) and the bogus reachability error.
            land_offload.append(u)

    if not land_offload:
        # Boats alone cannot be "moved" onto land — UX often drags the boat token; passengers must be in the move.
        if naval_in_move and len(naval_in_move) == len(units_in_stack):
            return ValidationResult(
                False,
                "Naval units cannot move to land. Only land units (passengers) offload or conduct a sea raid; boats stay in the sea zone.",
            )
        return None

    land_adj = getattr(dest_def, "adjacent", []) or []
    sea_adj = getattr(origin_def, "adjacent", []) or []
    if origin not in land_adj and destination not in sea_adj:
        return ValidationResult(
            False,
            f"Territory {destination} is not adjacent to sea zone {origin} (cannot offload there)",
        )

    if state.phase == "non_combat_move":
        dest_territory = state.territories.get(destination)
        if dest_territory:
            dest_eff = effective_territory_owner(state, destination)
            if dest_eff is not None and dest_eff != faction_id:
                our_fd = faction_defs.get(faction_id)
                owner_fd = faction_defs.get(dest_eff)
                our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
                owner_alliance = getattr(owner_fd, "alliance", "") if owner_fd else ""
                if owner_alliance != our_alliance:
                    return ValidationResult(
                        False,
                        f"Non-combat move cannot target enemy territory {destination} (owner={dest_eff})",
                    )
            elif dest_eff is None:
                dest_def_nc = territory_defs.get(destination)
                if dest_def_nc and getattr(dest_def_nc, "ownable", True):
                    return ValidationResult(
                        False,
                        f"Non-combat move cannot target ownable neutral territory {destination} (conquest is combat move only)",
                    )

    origin_territory = state.territories.get(origin)
    naval_in_territory = [
        u
        for u in (origin_territory.units if origin_territory else [])
        if faction_acts_as(faction_defs, get_unit_faction(u, unit_defs), faction_id)
        and _is_transport_boat_for_zone(unit_defs.get(u.unit_id), origin_def)
    ]
    boat_ids_in_move = {n.instance_id for n in naval_in_move}
    boat_ids_in_sea = {n.instance_id for n in naval_in_territory}

    capacity_boats = naval_in_move if naval_in_move else naval_in_territory
    naval_capacity = sum(
        getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
        for u in capacity_boats
    )
    if len(land_offload) > naval_capacity:
        return ValidationResult(
            False,
            f"Too many passengers ({len(land_offload)}) for transport capacity ({naval_capacity})",
        )

    def _passenger_assigned_to_boat(u, boat_ids: set[str]) -> bool:
        lo = getattr(u, "loaded_onto", None)
        if lo and lo in boat_ids:
            return True
        # Load still pending: no loaded_onto until phase end; match pending load onto this sea zone.
        for pm in getattr(state, "pending_moves", []) or []:
            if not pending_move_is_same_phase_load_into_sea(
                state, pm, origin, territory_defs, state.phase
            ):
                continue
            if u.instance_id not in (getattr(pm, "unit_instance_ids", None) or []):
                continue
            boat = getattr(pm, "load_onto_boat_instance_id", None) or None
            if boat is None:
                return True
            if boat in boat_ids:
                return True
        return False

    # When the client lists boats in the move (e.g. dragged the boat token), every passenger must be on one of those boats.
    if boat_ids_in_move:
        for u in land_offload:
            if not _passenger_assigned_to_boat(u, boat_ids_in_move):
                return ValidationResult(
                    False,
                    f"Offload/sea raid must include each passenger's boat in the move; check unit {u.instance_id} is on a selected boat.",
                )
    else:
        for u in land_offload:
            if not _passenger_assigned_to_boat(u, boat_ids_in_sea):
                return ValidationResult(
                    False,
                    f"Passenger {u.instance_id} must be loaded onto a friendly boat in {origin} to offload.",
                )

    if charge_through is not None and isinstance(charge_through, list) and len(charge_through) > 0:
        return ValidationResult(
            False,
            "charge_through not allowed for sea transport offload/sea raid",
        )

    for u in land_offload:
        if getattr(u, "remaining_movement", 0) < 1:
            return ValidationResult(
                False,
                f"Unit {u.instance_id} needs 1 movement to offload (has {getattr(u, 'remaining_movement', 0)})",
            )

    return ValidationResult(True)


def count_unit_instances(state: GameState, unit_id: str) -> int:
    """
    Count instances of a unit type in the whole game: map (including cargo),
    purchased-but-unplaced pools, and queued pending mobilizations.
    """
    n = 0
    for terr in (state.territories or {}).values():
        for u in getattr(terr, "units", None) or []:
            if getattr(u, "unit_id", None) == unit_id:
                n += 1
    for stacks in (state.faction_purchased_units or {}).values():
        for stack in stacks or []:
            if getattr(stack, "unit_id", None) == unit_id:
                n += int(getattr(stack, "count", 0) or 0)
    for pm in getattr(state, "pending_mobilizations", None) or []:
        for item in getattr(pm, "units", None) or []:
            if isinstance(item, dict):
                uid = item.get("unit_id")
                c = item.get("count", 0)
            else:
                uid = getattr(item, "unit_id", None)
                c = getattr(item, "count", 0)
            if uid == unit_id:
                n += int(c or 0)
    return n


def count_hero_family_instances(
    state: GameState,
    hero_id: str,
    unit_defs: dict[str, UnitDefinition],
) -> int:
    """Count in-play instances of every unit type that shares this hero_id."""
    hid = (hero_id or "").strip()
    if not hid:
        return 0
    n = 0
    for uid, udef in (unit_defs or {}).items():
        if unit_hero_id(udef) == hid:
            n += count_unit_instances(state, uid)
    return n


def unique_units_purchase_error(
    state: GameState,
    purchases: dict[str, int],
    unit_defs: dict[str, UnitDefinition],
) -> str | None:
    """Return an error if this purchase would put more than one of a hero family in play, or heroes are off."""
    heroes_on = bool(getattr(state, "heroes_enabled", True))
    family_purchases: dict[str, int] = {}
    family_names: dict[str, str] = {}
    for unit_id, count in (purchases or {}).items():
        if count <= 0:
            continue
        unit_def = unit_defs.get(unit_id)
        hid = unit_hero_id(unit_def)
        if not hid:
            continue
        name = getattr(unit_def, "display_name", None) or unit_id
        if not heroes_on:
            return f"Cannot purchase {name}: heroes are disabled in this game"
        family_purchases[hid] = family_purchases.get(hid, 0) + count
        family_names[hid] = name
    for hid, count in family_purchases.items():
        existing = count_hero_family_instances(state, hid, unit_defs)
        if existing + count <= 1:
            continue
        name = family_names.get(hid) or hid
        if existing <= 0:
            return (
                f"Cannot purchase more than one {name}: unique units are limited to one in play"
            )
        return f"Cannot purchase {name}: one is already in play"
    return None


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

    if faction_defs and not faction_owns_capital(state, faction_id, faction_defs):
        return ValidationResult(
            False,
            f"Cannot purchase units: {faction_id}'s capital has been captured",
        )

    # Calculate total cost
    total_cost: dict[str, int] = {}
    for unit_id, count in purchases.items():
        unit_def = unit_defs.get(unit_id)
        if not unit_def:
            return ValidationResult(False, f"Unknown unit type: {unit_id}")

        if not unit_def.purchasable:
            return ValidationResult(False, f"Unit {unit_id} is not purchasable")

        if not parent_may_purchase_unit(state, faction_id, unit_def, faction_defs):
            return ValidationResult(False, f"Unit {unit_id} belongs to {unit_def.faction}, not {faction_id}")

        for resource, amount in unit_def.cost.items():
            total_cost[resource] = total_cost.get(resource, 0) + (amount * count)

    unique_err = unique_units_purchase_error(state, purchases, unit_defs)
    if unique_err:
        return ValidationResult(False, unique_err)

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
        state, faction_id, territory_defs, camp_defs or {}, port_defs or {}, unit_defs, faction_defs
    )
    territories_list = capacity_info.get("territories", [])
    land_capacity = sum(t.get("power", 0) for t in territories_list) + sum(
        1 for t in territories_list if t.get("home_unit_capacity")
    )
    sea_capacity = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))

    river_capacity = river_mobilization_capacity_total(state, faction_id, territory_defs)
    cap_err = purchase_capacity_error(
        state,
        faction_id,
        unit_defs,
        faction_defs,
        territory_defs,
        camp_defs,
        port_defs,
        {uid: int(c or 0) for uid, c in purchases.items()},
        land_capacity,
        sea_capacity,
        river_capacity,
    )
    if cap_err:
        return ValidationResult(False, cap_err)

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

    origin = resolve_territory_key_in_state(state, origin, territory_defs)
    destination = resolve_territory_key_in_state(state, destination, territory_defs)

    origin_territory = state.territories.get(origin)
    if not origin_territory:
        return ValidationResult(False, f"Origin territory {origin} does not exist")

    unit_instance_ids = expand_sea_offload_instance_ids(
        state,
        origin,
        destination,
        list(unit_instance_ids),
        unit_defs,
        territory_defs,
        action.faction,
        faction_defs,
    )
    if not unit_instance_ids:
        return ValidationResult(False, "No units specified to move")

    # Build list of units and which can reach destination
    origin_def_chk = territory_defs.get(origin)
    dest_def_chk = territory_defs.get(destination)
    _move_rel = water_transport_relation(origin_def_chk, dest_def_chk)
    if _move_rel == "cross":
        return ValidationResult(False, "Ships cannot enter river zones, and river units cannot enter sea zones")
    sea_to_land_ctx = _move_rel == "offload"
    sea_to_sea_ctx = _move_rel == "sail"
    units_in_stack = []
    for instance_id in unit_instance_ids:
        if sea_to_land_ctx:
            unit = resolve_unit_for_move_declaration(
                state, origin, instance_id, state.phase, territory_defs
            )
        elif sea_to_sea_ctx:
            unit = next((u for u in origin_territory.units if u.instance_id == instance_id), None)
            if not unit:
                unit = resolve_unit_for_move_declaration(
                    state, origin, instance_id, state.phase, territory_defs
                )
        else:
            unit = next((u for u in origin_territory.units if u.instance_id == instance_id), None)
        if not unit:
            return ValidationResult(False, f"Unit {instance_id} not found for move from {origin}")
        units_in_stack.append(unit)

    # Sea→land offload must run *before* reachability. Passengers still on land (pending load) would be
    # pathfound from the sea hex incorrectly → no "drivers" → spurious "At least one unit must reach" errors.
    sea_offload_vr = validate_move_as_sea_offload_if_applicable(
        state,
        origin,
        destination,
        units_in_stack,
        unit_defs,
        territory_defs,
        faction_defs,
        action.faction,
        action.payload.get("charge_through"),
    )
    if sea_offload_vr is not None:
        return sea_offload_vr

    can_reach = {}
    charge_routes_by_unit = {}
    ford_exclude = set(unit_instance_ids)
    same_move_has_ford_crosser = any(
        has_unit_special(unit_defs.get(u.unit_id), "ford_crosser") for u in units_in_stack
    )
    for unit in units_in_stack:
        reachable, charge_routes = get_reachable_territories_for_unit(
            unit,
            origin,
            state,
            unit_defs,
            territory_defs,
            faction_defs,
            state.phase,
            None,
            ford_exclude,
            same_move_has_ford_crosser,
        )
        can_reach[unit.instance_id] = destination in reachable
        charge_routes_by_unit[unit.instance_id] = charge_routes

    if all(can_reach[u.instance_id] for u in units_in_stack):
        # Non-combat move: destination must be friendly, allied, or pass-through neutral only (never enemy, never ownable neutral)
        if state.phase == "non_combat_move":
            dest_territory = state.territories.get(destination)
            faction_id = action.faction
            if dest_territory:
                dest_eff = effective_territory_owner(state, destination)
                if dest_eff is not None and dest_eff != faction_id:
                    our_fd = faction_defs.get(faction_id)
                    owner_fd = faction_defs.get(dest_eff)
                    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
                    owner_alliance = getattr(owner_fd, "alliance", "") if owner_fd else ""
                    if owner_alliance != our_alliance:
                        return ValidationResult(
                            False,
                            f"Non-combat move cannot target enemy territory {destination} (owner={dest_eff})",
                        )
                elif dest_eff is None:
                    dest_def = territory_defs.get(destination)
                    if dest_def and getattr(dest_def, "ownable", True):
                        return ValidationResult(
                            False,
                            f"Non-combat move cannot target ownable neutral territory {destination} (conquest is combat move only)",
                        )
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
        if (
            state.phase == "combat_move"
            and origin_def_chk
            and dest_def_chk
            and water_transport_relation(origin_def_chk, dest_def_chk) == "sail"
            and units_in_stack
            and all(
                _is_transport_boat_for_zone(unit_defs.get(u.unit_id), origin_def_chk)
                for u in units_in_stack
            )
        ):
            avoid_forced = bool(action.payload.get("avoid_forced_naval_combat"))
            forced_ids = set(
                get_forced_naval_combat_instance_ids(
                    state, action.faction, unit_defs, territory_defs, faction_defs
                )
            )
            moving_naval = {
                u.instance_id
                for u in units_in_stack
                if _is_transport_boat_for_zone(unit_defs.get(u.unit_id), origin_def_chk)
            }
            if avoid_forced:
                if origin == destination:
                    return ValidationResult(
                        False,
                        "avoid_forced_naval_combat requires sailing to a different sea zone",
                    )
                if not moving_naval.issubset(forced_ids):
                    return ValidationResult(
                        False,
                        "avoid_forced_naval_combat only applies to boats that must fight or leave the mobilization standoff",
                    )
                if _sea_zone_has_hostile_enemy_boats(
                    state, destination, action.faction, unit_defs, faction_defs, territory_defs
                ):
                    return ValidationResult(
                        False,
                        "Cannot use avoid_forced_naval_combat to sail into a sea zone with hostile enemy boats",
                    )
                if not are_sea_zones_directly_adjacent(territory_defs, origin, destination):
                    return ValidationResult(
                        False,
                        "avoid_forced_naval_combat: you may only sail to an adjacent sea zone (1 hex), regardless of movement allowance",
                    )
            elif not _sea_zone_has_hostile_enemy_boats(
                state, destination, action.faction, unit_defs, faction_defs, territory_defs
            ):
                if not any(
                    empty_sea_zone_valid_for_combat_move_sail_then_load_raid(
                        state,
                        destination,
                        origin,
                        action.faction,
                        u,
                        unit_defs,
                        territory_defs,
                        faction_defs,
                        state.phase,
                    )
                    for u in units_in_stack
                ):
                    return ValidationResult(
                        False,
                        "Combat move cannot sail to this sea zone unless it is hostile, or adjacent to both a friendly land with units to load and a land you can sea raid.",
                    )
        # Land → sea: transportable land = load (capacity). All-aerial = combat into sea, not embark.
        odef = territory_defs.get(origin)
        ddef = territory_defs.get(destination)
        if (
            odef is not None
            and ddef is not None
            and water_transport_relation(odef, ddef) == "load"
            and units_in_stack
        ):
            all_transportable_land = all(
                is_land_unit(unit_defs.get(u.unit_id)) and is_transportable(unit_defs.get(u.unit_id))
                for u in units_in_stack
            )
            all_aerial = all(is_aerial_unit(unit_defs.get(u.unit_id)) for u in units_in_stack)
            if all_transportable_land:
                # Same-phase sail→load: pathfinding uses board after pending moves; capacity must too or the
                # boat is still in its origin sea in `state` and slots at the embark hex read as zero.
                from backend.engine.reducer import get_state_after_pending_moves

                slot_state = get_state_after_pending_moves(
                    state, state.phase, unit_defs, territory_defs, faction_defs
                )
                dest_territory = slot_state.territories.get(destination)
                if not dest_territory:
                    return ValidationResult(False, f"Destination {destination} does not exist")
                faction_id = action.faction
                load_onto_boat_id = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
                if load_onto_boat_id:
                    slots = remaining_load_slots_on_boat(
                        slot_state, destination, load_onto_boat_id, faction_id, unit_defs, territory_defs, state.phase, faction_defs
                    )
                    if len(units_in_stack) > slots:
                        return ValidationResult(
                            False,
                            f"Boat {load_onto_boat_id} has {slots} passenger slot(s) left (capacity minus onboard and pending loads), "
                            f"cannot load {len(units_in_stack)}",
                        )
                else:
                    slots_left = remaining_sea_load_passenger_slots(
                        slot_state, destination, faction_id, unit_defs, territory_defs, state.phase, faction_defs
                    )
                    if len(units_in_stack) > slots_left:
                        return ValidationResult(
                            False,
                            f"Not enough transport capacity in {destination}: {len(units_in_stack)} passengers but only "
                            f"{slots_left} slot(s) left (boats may be full or already reserved by pending loads this phase)",
                        )
            elif not all_aerial:
                return ValidationResult(
                    False,
                    "Only transportable land units can load into a sea zone",
                )
        # Land → land: ford escort (non-ford-crossers using ford-only edges)
        if (
            odef is not None
            and ddef is not None
            and not _is_water_zone(odef)
            and not _is_water_zone(ddef)
            and units_in_stack
        ):
            ford_cost = land_move_ford_escort_cost_for_instances(
                origin, destination, unit_instance_ids, state, unit_defs, territory_defs
            )
            if ford_cost > 0:
                okey = resolve_territory_key_in_state(state, origin, territory_defs)
                dkey = resolve_territory_key_in_state(state, destination, territory_defs)
                needs_lead = ford_shortcut_requires_escort_lead(okey, dkey, territory_defs)
                if needs_lead and not any(
                    has_unit_special(unit_defs.get(u.unit_id), "ford_crosser") for u in units_in_stack
                ):
                    if not pending_ford_crosser_lead_move_from_origin(
                        state, origin, state.phase, unit_defs, territory_defs
                    ):
                        return ValidationResult(
                            False,
                            "Declare a ford crosser's move across this ford before other units may use escort capacity.",
                        )
                ford_rem = remaining_ford_escort_slots(
                    state,
                    origin,
                    action.faction,
                    unit_defs,
                    territory_defs,
                    state.phase,
                    ford_exclude,
                )
                if ford_cost > ford_rem:
                    return ValidationResult(
                        False,
                        f"Not enough ford escort capacity: need {ford_cost} slot(s) but only {ford_rem} remain "
                        f"(ford crossers' transport_capacity in {origin}, minus pending moves)",
                    )
        return ValidationResult(True)

    # Not all units can reach: allow only if valid sea transport (driver + passengers, or load: land-only to sea with boats there)
    path = get_shortest_path(origin, destination, territory_defs)
    path_includes_water = path and any(
        _is_water_zone(territory_defs.get(tid)) for tid in path
    )
    dest_is_water = _is_water_zone(territory_defs.get(destination))
    origin_def = territory_defs.get(origin)
    origin_is_land = origin_def and not _is_water_zone(origin_def)
    if not path_includes_water and not dest_is_water:
        return ValidationResult(
            False,
            f"Unit(s) cannot reach {destination} from {origin} (and this is not a sea transport move)"
        )
    drivers = [u for u in units_in_stack if can_reach[u.instance_id]]
    passengers = [u for u in units_in_stack if not can_reach[u.instance_id]]

    # Sea→sea sail to a sea zone adjacent to the land you dropped on (sea raid / offload chain).
    # Not the same as generic naval combat_move reachability (which forbids empty sea destinations).
    sail_land_raw = (action.payload.get("sail_to_offload_land_territory_id") or "").strip()
    move_type_payload = (action.payload.get("move_type") or "").strip()
    if (
        sail_land_raw
        and move_type_payload == "sail"
        and water_transport_relation(territory_defs.get(origin), territory_defs.get(destination)) == "sail"
    ):
        vr = validate_sail_move_for_offload_sea_raid(
            state,
            origin,
            destination,
            sail_land_raw,
            units_in_stack,
            unit_instance_ids,
            unit_defs,
            territory_defs,
            faction_defs,
            action.faction,
            state.phase,
        )
        return vr

    # Embark: move_type=load, land→sea. Pathfinding may mark some land units as "reaching" the sea hex and
    # others not, splitting drivers/passengers and incorrectly falling through to sea-transport naval_capacity
    # (Too many passengers for transport capacity). Match reducer: treat the whole stack as loading passengers.
    if (
        move_type_payload == "load"
        and origin_is_land
        and dest_is_water
    ):
        for u in units_in_stack:
            ud = unit_defs.get(u.unit_id)
            if not is_land_unit(ud):
                return ValidationResult(
                    False,
                    f"Unit {u.instance_id} cannot be carried (only land units can load into sea)",
                )
            if not is_transportable(ud):
                return ValidationResult(
                    False,
                    f"Unit {u.instance_id} cannot be transported (no transportable tag)",
                )
        from backend.engine.reducer import get_state_after_pending_moves

        slot_state = get_state_after_pending_moves(
            state, state.phase, unit_defs, territory_defs, faction_defs
        )
        dest_territory = slot_state.territories.get(destination)
        if not dest_territory:
            return ValidationResult(False, f"Destination {destination} does not exist")
        faction_id = action.faction
        load_onto_boat_id = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
        if load_onto_boat_id:
            boat_unit = next((u for u in dest_territory.units if u.instance_id == load_onto_boat_id), None)
            if not boat_unit:
                return ValidationResult(False, f"Boat {load_onto_boat_id} not found in {destination}")
            boat_ud = unit_defs.get(boat_unit.unit_id)
            dest_def_boat = territory_defs.get(destination)
            if not _is_transport_boat_for_zone(boat_ud, dest_def_boat):
                return ValidationResult(False, f"Unit {load_onto_boat_id} cannot carry units in {destination}")
            if not faction_acts_as(faction_defs, get_unit_faction(boat_unit, unit_defs), faction_id):
                return ValidationResult(
                    False,
                    f"Boat {load_onto_boat_id} does not belong to faction {faction_id}",
                )
            slots = remaining_load_slots_on_boat(
                slot_state, destination, load_onto_boat_id, faction_id, unit_defs, territory_defs, state.phase, faction_defs
            )
            if len(units_in_stack) > slots:
                return ValidationResult(
                    False,
                    f"Boat {load_onto_boat_id} has {slots} passenger slot(s) left (capacity minus onboard and pending loads), "
                    f"cannot load {len(units_in_stack)}",
                )
        else:
            slots_left = remaining_sea_load_passenger_slots(
                slot_state, destination, faction_id, unit_defs, territory_defs, state.phase, faction_defs
            )
            if len(units_in_stack) > slots_left:
                return ValidationResult(
                    False,
                    f"Not enough transport capacity in {destination}: {len(units_in_stack)} passengers but only "
                    f"{slots_left} slot(s) left (boats may be full or already reserved by pending loads this phase)",
                )
        return ValidationResult(True)

    # Load: land -> adjacent sea; stack can be all land units; boats already in sea zone provide capacity
    if origin_is_land and dest_is_water and not drivers and passengers:
        for u in passengers:
            ud = unit_defs.get(u.unit_id)
            if not is_land_unit(ud):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be carried (only land units can be passengers)")
            if not is_transportable(ud):
                return ValidationResult(False, f"Unit {u.instance_id} cannot be transported (no transportable tag)")
        from backend.engine.reducer import get_state_after_pending_moves

        slot_state = get_state_after_pending_moves(
            state, state.phase, unit_defs, territory_defs, faction_defs
        )
        dest_territory = slot_state.territories.get(destination)
        if not dest_territory:
            return ValidationResult(False, f"Destination {destination} does not exist")
        faction_id = action.faction
        load_onto_boat_id = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
        if load_onto_boat_id:
            boat_unit = next((u for u in dest_territory.units if u.instance_id == load_onto_boat_id), None)
            if not boat_unit:
                return ValidationResult(False, f"Boat {load_onto_boat_id} not found in {destination}")
            boat_ud = unit_defs.get(boat_unit.unit_id)
            if not _is_transport_boat_for_zone(boat_ud, territory_defs.get(destination)):
                return ValidationResult(False, f"Unit {load_onto_boat_id} cannot carry units in {destination}")
            if not faction_acts_as(faction_defs, get_unit_faction(boat_unit, unit_defs), faction_id):
                return ValidationResult(False, f"Boat {load_onto_boat_id} does not belong to faction {faction_id}")
            slots = remaining_load_slots_on_boat(
                slot_state, destination, load_onto_boat_id, faction_id, unit_defs, territory_defs, state.phase, faction_defs
            )
            if len(passengers) > slots:
                return ValidationResult(
                    False,
                    f"Boat {load_onto_boat_id} has {slots} passenger slot(s) left (capacity minus onboard and pending loads), "
                    f"cannot load {len(passengers)}",
                )
        else:
            slots_left = remaining_sea_load_passenger_slots(
                slot_state, destination, faction_id, unit_defs, territory_defs, state.phase, faction_defs
            )
            if len(passengers) > slots_left:
                return ValidationResult(
                    False,
                    f"Not enough transport capacity in {destination}: {len(passengers)} passengers but only "
                    f"{slots_left} slot(s) left (boats may be full or already reserved by pending loads this phase)",
                )
        # Load costs 0 movement for passengers (offload/sea raid pays 1 when going ashore).
        return ValidationResult(True)

    if not drivers:
        return ValidationResult(False, "At least one unit (naval or aerial) must be able to reach the destination")
    for u in passengers:
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud):
            return ValidationResult(False, f"Unit {u.instance_id} cannot be carried (only land units can be passengers)")
        if not is_transportable(ud):
            return ValidationResult(False, f"Unit {u.instance_id} cannot be transported (no transportable tag)")
    dest_zone_def = territory_defs.get(destination)
    naval_capacity = sum(
        getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
        for u in drivers
        if _is_transport_boat_for_zone(unit_defs.get(u.unit_id), dest_zone_def)
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
    combat_territory_is_water = _is_water_zone(territory_def)
    attacker_faction = action.faction
    attacker_alliance = faction_defs.get(attacker_faction, FactionDefinition(
        "", "", "", "", "")).alliance

    if sea_zone_id:
        # Sea raid: attackers in sea zone, target is land territory
        sea_zone = state.territories.get(sea_zone_id)
        sea_def = territory_defs.get(sea_zone_id)
        if not sea_zone or not sea_def or not _is_water_zone(sea_def):
            return ValidationResult(False, f"Sea zone {sea_zone_id} is not a valid sea or river zone")
        if combat_territory_is_water:
            return ValidationResult(False, "Sea raid target must be land territory")
        sea_raid_from = getattr(state, "territory_sea_raid_from", None) or {}
        if sea_raid_from.get(territory_id) != sea_zone_id:
            sea_adj = getattr(sea_def, "adjacent", []) or []
            land_adj = getattr(territory_def, "adjacent", []) or []
            if territory_id not in sea_adj and sea_zone_id not in land_adj:
                return ValidationResult(False, f"Territory {territory_id} is not adjacent to sea zone {sea_zone_id}")
        # Sea raid land battle roster must match _handle_initiate_combat: fleet passengers in sea
        # plus attacker-faction non-naval units on the land hex (e.g. aerial joining the same assault).
        raid_attackers: dict[str, Unit] = {}
        for u in sea_zone.units:
            ud = unit_defs.get(u.unit_id)
            if not faction_acts_as(faction_defs, get_unit_faction(u, unit_defs), attacker_faction):
                continue
            if not is_land_unit(ud) or _is_naval_unit(ud) or _is_river_unit(ud):
                continue
            raid_attackers[u.instance_id] = u
        for u in territory.units:
            if _is_naval_unit(unit_defs.get(u.unit_id)) or _is_river_unit(unit_defs.get(u.unit_id)):
                continue
            if _land_combat_unit_side_for_queries(
                u, attacker_faction, attacker_alliance, unit_defs, faction_defs
            ) != "attacker":
                continue
            raid_attackers[u.instance_id] = u
        attacker_units = list(raid_attackers.values())
        defender_units = [
            u for u in territory.units
            if get_unit_faction(u, unit_defs) is not None
            and faction_defs.get(get_unit_faction(u, unit_defs), FactionDefinition("", "", "", "", "")).alliance != attacker_alliance
        ]
        if not attacker_units:
            return ValidationResult(
                False,
                f"No attacking units for sea raid in sea zone {sea_zone_id} or on territory {territory_id}",
            )
        # Allow empty defenders (conquer without battle)
        return ValidationResult(True)

    if not combat_territory_is_water:
        for unit in territory.units:
            unit_faction = get_unit_faction(unit, unit_defs)
            if not faction_acts_as(faction_defs, unit_faction, action.faction):
                continue
            ud = unit_defs.get(unit.unit_id)
            if (_is_naval_unit(ud) or _is_river_unit(ud)) and not is_aerial_unit(ud):
                return ValidationResult(
                    False,
                    "Naval units cannot attack or fight on land. For a sea raid, initiate combat with sea_zone_id (attackers in that sea zone)."
                )

    # Find attacker and defender units (normal combat: both in same territory)
    attacker_units = []
    defender_units = []
    for unit in territory.units:
        unit_faction = get_unit_faction(unit, unit_defs)
        if faction_acts_as(faction_defs, unit_faction, attacker_faction):
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
    faction_defs: dict[str, FactionDefinition],
) -> ValidationResult:
    """Validate a mobilize_units action. Land units require a camp; naval units require a port-adjacent sea zone."""
    faction_id = action.faction
    if faction_defs and not faction_owns_capital(state, faction_id, faction_defs):
        return ValidationResult(
            False,
            f"Cannot mobilize units: {faction_id}'s capital has been captured",
        )
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

    kinds = {_purchase_kind(unit_defs.get(item.get("unit_id"))) for item in units_to_mobilize}
    if len(kinds) != 1:
        return ValidationResult(False, "Do not mix naval, river, and land units in one mobilization")
    batch_kind = next(iter(kinds))

    sub_mobilization = subfaction_mobilization_error(
        state, faction_id, destination, units_to_mobilize, unit_defs, territory_defs,
        camp_defs, port_defs, faction_defs,
    )
    if sub_mobilization:
        return ValidationResult(False, sub_mobilization)
    if sub_mobilization is None and batch_kind == "naval":
        # Naval: destination must be a sea zone adjacent to an owned port
        if not _sea_zone_adjacent_to_owned_port(state, destination, faction_id, port_defs, territory_defs):
            return ValidationResult(
                False,
                f"Naval units can only mobilize to a sea zone adjacent to a port you own; {destination} is not valid",
            )
        # Shared capacity: each port territory P adjacent to this sea zone has pool P.power; count land to P + naval to P's adjacent sea zones
        power_production = None  # validated per-port below
    elif sub_mobilization is None and batch_kind == "river":
        if not _is_river_zone(dest_def) or not river_banks_for_zone(state, faction_id, destination, territory_defs):
            return ValidationResult(
                False,
                f"River units can only mobilize to a river zone that borders a territory you owned "
                f"at the start of your turn; {destination} is not valid",
            )
        this_count = sum(item.get("count", 0) for item in units_to_mobilize)
        fits, err = river_mobilization_fits(
            state,
            faction_id,
            territory_defs,
            unit_defs,
            extra_river={destination: this_count},
        )
        if not fits:
            return ValidationResult(False, err)
    elif sub_mobilization is None:
        # Land: camp or home territory for that unit (home works at port capitals e.g. Corsair → Umbar). Not generic port land deployment.
        on_subfaction_land = subfaction_owns_for(dest_territory.owner, faction_id, faction_defs)
        has_camp = not on_subfaction_land and _territory_has_standing_camp(state, destination, camp_defs)
        is_home_for = {}  # unit_id -> True if this territory is home for that unit type
        for uid, ud in unit_defs.items():
            if getattr(ud, "faction", None) != faction_id:
                continue
            if has_unit_special(ud, "home") and destination in _home_territory_ids(ud):
                is_home_for[uid] = True
        if not has_camp:
            for item in units_to_mobilize:
                uid = item.get("unit_id")
                if not is_home_for.get(uid):
                    return ValidationResult(
                        False,
                        f"Land units can only mobilize to a standing camp or a home territory for that unit type; "
                        f"{destination} is not valid for {uid}",
                    )
        if dest_territory.owner != faction_id and not on_subfaction_land:
            return ValidationResult(False, f"{destination} is not owned by {faction_id}")
        power_production = territory_current_power(state, destination, dest_def)
        this_count = sum(item.get("count", 0) for item in units_to_mobilize)
        owned_at_turn_start = getattr(state, "faction_territories_at_turn_start", {}).get(faction_id, []) or []
        camp_hex_owned_at_turn_start = destination in owned_at_turn_start
        if has_camp and not camp_hex_owned_at_turn_start:
            for item in units_to_mobilize:
                uid = item.get("unit_id")
                if not is_home_for.get(uid):
                    return ValidationResult(
                        False,
                        f"Cannot mobilize to camp territory {destination}: it was not owned at the start of your turn. "
                        f"You may still mobilize units with the home special to their home here.",
                    )
        if has_camp and camp_hex_owned_at_turn_start:
            already_pending = sum(
                sum(u.get("count", 0) for u in pm.units)
                for pm in state.pending_mobilizations
                if pm.destination == destination
            )
            if already_pending + this_count > power_production:
                return ValidationResult(
                    False,
                    f"Cannot mobilize {this_count} more to {destination}: "
                    f"already {already_pending} pending, capacity is {power_production}",
                )
        elif has_camp and not camp_hex_owned_at_turn_start:
            unit_ids_in_batch = {item.get("unit_id") for item in units_to_mobilize}
            if len(unit_ids_in_batch) != 1:
                return ValidationResult(
                    False,
                    "When mobilizing to a home territory on a camp hex you captured this turn, all units must be the same type",
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
            if already_pending + this_count > 1:
                return ValidationResult(
                    False,
                    f"At most 1 {unit_id} can be mobilized to home territory {destination} per phase (already {already_pending} pending)",
                )
        else:
            unit_ids_in_batch = {item.get("unit_id") for item in units_to_mobilize}
            if len(unit_ids_in_batch) != 1:
                return ValidationResult(
                    False,
                    "When mobilizing to a home territory, all units must be the same type",
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
            if already_pending + this_count > 1:
                return ValidationResult(
                    False,
                    f"At most 1 {unit_id} can be mobilized to home territory {destination} per phase (already {already_pending} pending)",
                )

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
    if sub_mobilization is None and batch_kind == "naval":
        # Naval to sea zone: shared pool with each port adjacent to this sea zone
        sea_def = territory_defs.get(destination)
        if sea_def and _is_sea_zone(sea_def):
            for adj_id in sea_def.adjacent:
                if state.territories.get(adj_id, TerritoryState(None)).owner != faction_id:
                    continue
                if not _territory_has_port(adj_id, port_defs):
                    continue
                port_power = territory_defs.get(adj_id)
                port_power_val = territory_current_power(state, adj_id, port_power) if port_power else 0
                total_for_port = _total_pending_mobilization_to_port(state, adj_id, territory_defs, port_defs)
                if total_for_port + this_action_count > port_power_val:
                    return ValidationResult(
                        False,
                        f"Cannot mobilize {this_action_count} naval to {destination}: "
                        f"port {adj_id} shared pool would exceed capacity ({total_for_port + this_action_count} > {port_power_val})",
                    )
    elif sub_mobilization is None and batch_kind == "land":
        fits, err = river_mobilization_fits(
            state,
            faction_id,
            territory_defs,
            unit_defs,
            extra_land={destination: this_action_count},
        )
        if not fits:
            return ValidationResult(False, err)
    # Land capacity (camp / port / home-only) fully validated in the land branch above.

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

    if getattr(state.active_combat, "sea_zone_id", None):
        return ValidationResult(False, "Retreat is not allowed during a sea raid")

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
        if tdef and territory_current_power(state, tid, tdef) > 0:
            options.append(tid)
    if not options:
        return ValidationResult(False, "No valid territory to place a camp (need owned territory with power production)")
    return ValidationResult(True)


def _validate_repair_stronghold(
    state: GameState,
    action: Action,
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate repair_stronghold: each territory must be owned stronghold with current_hp < base; cost = sum(hp_to_add) * stronghold_repair_cost."""
    faction_id = action.faction
    if state.phase != "purchase" or state.current_faction != faction_id:
        return ValidationResult(False, "Can only repair strongholds during your purchase phase")
    repair_cost_per_hp = getattr(state, "stronghold_repair_cost", 0)
    if repair_cost_per_hp <= 0:
        return ValidationResult(False, "Stronghold repair is not available in this setup")
    repairs = action.payload.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        return ValidationResult(True)  # No repairs is valid (e.g. confirm with 0 repairs)
    power = state.faction_resources.get(faction_id, {}).get("power", 0)
    total_hp = 0
    for r in repairs:
        if not isinstance(r, dict):
            return ValidationResult(False, "Each repair must be {territory_id, hp_to_add}")
        tid = r.get("territory_id")
        hp_to_add = r.get("hp_to_add", 0)
        if not tid or hp_to_add is None:
            return ValidationResult(False, "Each repair must include territory_id and hp_to_add")
        try:
            hp_to_add = int(hp_to_add)
        except (TypeError, ValueError):
            return ValidationResult(False, "hp_to_add must be a number")
        if hp_to_add <= 0:
            continue
        territory = state.territories.get(tid)
        if not territory:
            return ValidationResult(False, f"Unknown territory: {tid}")
        if territory.owner != faction_id:
            return ValidationResult(False, f"You do not own {tid}")
        tdef = territory_defs.get(tid)
        if not tdef or not getattr(tdef, "is_stronghold", False):
            return ValidationResult(False, f"{tid} is not a stronghold")
        base_hp = getattr(tdef, "stronghold_base_health", 0) or 0
        if base_hp <= 0:
            return ValidationResult(False, f"Stronghold {tid} has no base health defined")
        current = getattr(territory, "stronghold_current_health", None)
        current = current if current is not None else base_hp
        if current + hp_to_add > base_hp:
            return ValidationResult(False, f"Cannot repair {tid} above base health ({base_hp})")
        total_hp += hp_to_add
    total_cost = total_hp * repair_cost_per_hp
    if power < total_cost:
        return ValidationResult(False, f"Insufficient power: need {total_cost} for repairs, have {power}")
    return ValidationResult(True)


def valid_camp_placement_territory_ids(
    state: GameState,
    faction_id: str,
    camp_index: int,
    camp_defs: dict[str, CampDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """
    Pending camp territory_options that are still legal: we own the hex, it produces power,
    no standing camp, and no other queued placement reserves this territory.
    """
    pending = getattr(state, "pending_camps", []) or []
    if camp_index < 0 or camp_index >= len(pending):
        return []
    entry = pending[camp_index]
    if entry.get("placed_territory_id"):
        return []
    options = entry.get("territory_options") or []
    queued_by_others = {
        p.territory_id
        for p in getattr(state, "pending_camp_placements", []) or []
        if p.camp_index != camp_index
    }
    out: list[str] = []
    for tid in options:
        if not tid or tid in queued_by_others:
            continue
        terr = state.territories.get(tid)
        if not terr or terr.owner != faction_id:
            continue
        tdef = territory_defs.get(tid)
        if not tdef or territory_current_power(state, tid, tdef) <= 0:
            continue
        if _territory_has_standing_camp(state, tid, camp_defs):
            continue
        out.append(tid)
    return out


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


def mobilization_block_reason(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition] | None,
    territory_defs: dict[str, TerritoryDefinition] | None,
    camp_defs: dict | None,
    port_defs: dict | None,
    faction_defs: dict | None,
) -> str | None:
    """
    Why mobilization cannot end, or None when it can.
    Unplaced units block the phase only while one of them still has a legal destination.
    A lost capital makes placement impossible, so the phase can end and those units are dropped.
    """
    unit_defs = unit_defs or {}
    territory_defs = territory_defs or {}
    stacks = [
        s for s in (state.faction_purchased_units.get(faction_id) or [])
        if int(getattr(s, "count", 0) or 0) > 0
    ]
    if not stacks or not faction_defs or not faction_owns_capital(state, faction_id, faction_defs):
        return None
    pending: dict[str, int] = {}
    pending_unit: dict[tuple[str, str], int] = {}
    for pm in getattr(state, "pending_mobilizations", []) or []:
        dest = getattr(pm, "destination", "") or ""
        for item in getattr(pm, "units", None) or []:
            n = int(item.get("count", 0) or 0)
            uid = item.get("unit_id") or ""
            pending[dest] = pending.get(dest, 0) + n
            pending_unit[(dest, uid)] = pending_unit.get((dest, uid), 0) + n
    if _purchased_units_have_a_destination(
        state, faction_id, stacks, unit_defs, territory_defs, camp_defs, port_defs, faction_defs,
        pending, pending_unit,
    ):
        return "Deploy all purchased and granted units before ending mobilization"
    return None


def _purchased_units_have_a_destination(
    state: GameState,
    faction_id: str,
    stacks: list,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    camp_defs: dict | None,
    port_defs: dict | None,
    faction_defs: dict,
    pending: dict[str, int],
    pending_unit: dict[tuple[str, str], int],
) -> bool:
    capacity = get_mobilization_capacity(
        state, faction_id, territory_defs, camp_defs, port_defs, unit_defs, faction_defs,
    )
    parent_power: dict[str, int] = {}
    parent_home: dict[tuple[str, str], int] = {}
    for bucket in (capacity.get("territories") or [], capacity.get("port_territories") or []):
        for row in bucket:
            tid = row.get("territory_id")
            if not tid:
                continue
            parent_power[tid] = int(row.get("power", 0) or 0)
            for uid, n in (row.get("home_unit_capacity") or {}).items():
                parent_home[(tid, uid)] = int(n or 0)
    for row in capacity.get("sea_zones") or []:
        parent_power[row.get("sea_zone_id")] = int(row.get("power", 0) or 0)
    for row in capacity.get("river_zones") or []:
        parent_power[row.get("river_zone_id")] = int(row.get("power", 0) or 0)
    parent_land = set(get_mobilization_territories(
        state, faction_id, territory_defs, camp_defs, port_defs, unit_defs, faction_defs,
    ))
    parent_sea = set(get_mobilization_sea_zones(state, faction_id, territory_defs, port_defs))
    parent_river = set(get_mobilization_river_zones(state, faction_id, territory_defs))

    for stack in stacks:
        unit_id = stack.unit_id
        unit_def = unit_defs.get(unit_id)
        kind = _purchase_kind(unit_def)
        spec = unit_destination_spec(
            state, faction_id, unit_def, faction_defs, territory_defs, camp_defs, port_defs,
        )
        if spec is not None:
            if kind == "naval":
                ids = spec.get("sea_zones") or []
            elif kind == "river":
                ids = spec.get("river_zones") or []
            else:
                ids = spec.get("territories") or []
            if spec.get("unlimited") and ids:
                return True
            for tid in ids:
                cap = int((spec.get("capacity") or {}).get(tid, 0) or 0)
                if cap - pending.get(tid, 0) > 0:
                    return True
                home = int((spec.get("home") or {}).get(tid, 0) or 0)
                if home and pending_unit.get((tid, unit_id), 0) < home:
                    return True
            continue
        if kind == "naval":
            ids = parent_sea
        elif kind == "river":
            ids = parent_river
        else:
            ids = parent_land
        for tid in ids:
            if parent_power.get(tid, 0) - pending.get(tid, 0) > 0:
                return True
            if parent_home.get((tid, unit_id), 0) > pending_unit.get((tid, unit_id), 0):
                return True
    return False


def _validate_end_phase(
    state: GameState,
    faction_defs: dict | None = None,
    unit_defs: dict | None = None,
    territory_defs: dict | None = None,
    camp_defs: dict | None = None,
    port_defs: dict | None = None,
) -> ValidationResult:
    """Validate end_phase: combat phase cannot end while contested battles remain; mobilization: all camps placed or queued."""
    if state.phase == "combat_move" and unit_defs and territory_defs and faction_defs:
        from backend.engine.reducer import get_state_after_pending_moves

        sim = get_state_after_pending_moves(
            state, "combat_move", unit_defs, territory_defs, faction_defs
        )
        idle = getattr(sim, "combat_move_naval_idle_sail_instance_ids", []) or []
        if idle:
            return ValidationResult(
                False,
                "Combat move cannot end while a naval unit sailed into a sea zone for load/sea raid but did not follow "
                f"through (sea raid, naval battle, or load). Unresolved boats: {idle!s}.",
            )
    if state.phase == "combat":
        if state.active_combat is not None:
            return ValidationResult(
                False,
                "Cannot end combat phase while a battle is in progress. Continue or retreat first.",
            )
        if faction_defs and unit_defs and territory_defs and state.current_faction:
            contested = get_contested_territories(
                state, state.current_faction, faction_defs, unit_defs, territory_defs
            )
            if contested:
                return ValidationResult(
                    False,
                    f"Cannot end combat phase while {len(contested)} unresolved battle(s) remain. Initiate and resolve or retreat from all battles first.",
                )
        return ValidationResult(True)
    if state.phase != "mobilization":
        return ValidationResult(True)
    faction_id = state.current_faction or ""
    blocked = mobilization_block_reason(
        state, faction_id, unit_defs, territory_defs, camp_defs, port_defs, faction_defs,
    )
    if blocked:
        return ValidationResult(False, blocked)
    pending = getattr(state, "pending_camps", [])
    queued_indices = {p.camp_index for p in getattr(state, "pending_camp_placements", [])}
    faction_id = state.current_faction or ""
    cd = camp_defs or {}
    td = territory_defs or {}
    for i, p in enumerate(pending):
        if p.get("placed_territory_id"):
            continue
        if i in queued_indices:
            continue
        valid = valid_camp_placement_territory_ids(state, faction_id, i, cd, td)
        if valid:
            return ValidationResult(
                False,
                "Place or queue all camps before ending mobilization (at least one camp still has a valid placement)",
            )
    return ValidationResult(True)


def _validate_place_camp(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
    territory_defs: dict[str, TerritoryDefinition],
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
    terr = state.territories.get(territory_id)
    if not terr or terr.owner != faction_id:
        return ValidationResult(
            False,
            f"You must own {territory_id} to place a camp there",
        )
    tdef = territory_defs.get(territory_id)
    if not tdef or territory_current_power(state, territory_id, tdef) <= 0:
        return ValidationResult(
            False,
            f"Territory {territory_id} cannot host a mobilization camp (needs power production)",
        )
    if _territory_has_standing_camp(state, territory_id, camp_defs):
        return ValidationResult(False, f"Territory {territory_id} already has a camp")
    return ValidationResult(True)


def _validate_queue_camp_placement(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> ValidationResult:
    """Validate a queue_camp_placement action (same as place_camp; camp must not already be queued; territory must not have another pending placement)."""
    r = _validate_place_camp(state, action, camp_defs, territory_defs)
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
    faction_defs: dict[str, FactionDefinition] | None = None,
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
                belongs = faction_acts_as(faction_defs, unit_faction, faction_id)
            if not belongs:
                continue

            try:
                rm = int(getattr(unit, "remaining_movement", 0) or 0)
            except (TypeError, ValueError):
                rm = 0
            if rm > 0:
                result.append({
                    "instance_id": unit.instance_id,
                    "unit_id": unit.unit_id,
                    "territory_id": territory_id,
                    "remaining_movement": rm,
                })

    return result


def get_unit_move_targets(
    state: GameState,
    unit_instance_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    slot_check_state: GameState | None = None,
) -> tuple[dict[str, int], dict[str, list[list[str]]]]:
    """
    Get all territories a specific unit can move to.
    Returns (targets_dict, charge_routes).
    - targets_dict: territory_id -> movement_cost
    - charge_routes: for cavalry in combat_move, territory_id -> list of charge_through paths (empty enemy IDs)

    slot_check_state: optional snapshot for land→sea embark capacity (same-phase pending applied).
    Pass once per available-actions build to avoid redundant deepcopy per unit.
    """
    for territory_id, territory in state.territories.items():
        for unit in territory.units:
            if unit.instance_id == unit_instance_id:
                return get_reachable_territories_for_unit(
                    unit,
                    territory_id,
                    state,
                    unit_defs,
                    territory_defs,
                    faction_defs,
                    state.phase,
                    None,
                    None,
                    False,
                    slot_check_state,
                )

    return {}, {}  # Unit not found


def filter_unit_instances_that_can_reach(
    state: GameState,
    to_territory_id: str,
    unit_instance_ids: list[str],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> list[str]:
    """
    Return only those unit instance IDs that can reach to_territory_id.
    Uses each unit's remaining_movement and phase; never includes a unit that cannot reach.
    """
    result = []
    for iid in unit_instance_ids:
        targets, _ = get_unit_move_targets(
            state, iid, unit_defs, territory_defs, faction_defs
        )
        if to_territory_id in (targets or {}):
            result.append(iid)
    return result


def get_purchasable_units(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition] | None = None,
) -> list[dict[str, Any]]:
    """
    Get all unit types the faction can purchase with current resources.
    Returns list of {unit_id, display_name, cost, max_affordable, unique, hero_id, ...}.
    Hero units (`hero_id`) cap max_affordable at remaining family slots (0 or 1) based on units
    already on the map, in purchased pools, or queued for mobilization (all versions share the cap).
    """
    faction_resources = state.faction_resources.get(faction_id, {})
    result = []

    for unit_id, unit_def in unit_defs.items():
        if not parent_may_purchase_unit(state, faction_id, unit_def, faction_defs):
            continue
        if not unit_def.purchasable:
            continue
        hid = unit_hero_id(unit_def)
        unique = hid is not None
        if unique and not bool(getattr(state, "heroes_enabled", True)):
            continue

        # Calculate max affordable
        max_affordable = float('inf')
        for resource, cost in unit_def.cost.items():
            available = faction_resources.get(resource, 0)
            if cost > 0:
                max_affordable = min(max_affordable, available // cost)

        if max_affordable == float('inf'):
            max_affordable = 0

        if unique and hid:
            existing = count_hero_family_instances(state, hid, unit_defs)
            max_affordable = min(max_affordable, max(0, 1 - existing))

        result.append({
            "unit_id": unit_id,
            "display_name": unit_def.display_name,
            "cost": unit_def.cost,
            "max_affordable": int(max_affordable),
            "unique": unique,
            "hero_id": hid,
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
    faction_defs: dict[str, FactionDefinition] | None = None,
) -> list[str]:
    """
    Get territory IDs where faction can mobilize land units:
    - Owned territories with a standing camp, or
    - Owned or subfaction territories that are home for at least one unit type (cap 1 per type per phase), including port capitals (e.g. Corsair → Umbar).
    Land does not deploy to ports generically; ships use adjacent sea zones.
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
    # Home territories: owned, no camp yet in list; include even if territory has a port (home special overrides for that unit)
    for territory_id, territory in state.territories.items():
        if territory_id in result:
            continue
        if territory.owner == faction_id:
            if _territory_has_standing_camp(state, territory_id, camp_defs):
                continue
        elif not subfaction_owns_for(territory.owner, faction_id, faction_defs):
            continue
        for ud in unit_defs.values():
            if getattr(ud, "faction", None) != faction_id:
                continue
            if has_unit_special(ud, "home") and territory_id in _home_territory_ids(ud):
                result.append(territory_id)
                break
    return result


def get_mobilization_river_zones(
    state: GameState,
    faction_id: str,
    territory_defs: dict[str, TerritoryDefinition],
) -> list[str]:
    """River zones where this faction can mobilize rowboats (borders a turn-start-owned territory)."""
    result = []
    for tid, tdef in territory_defs.items():
        if not _is_river_zone(tdef):
            continue
        if river_banks_for_zone(state, faction_id, tid, territory_defs):
            result.append(tid)
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
    faction_defs: dict[str, FactionDefinition] | None = None,
) -> dict[str, Any]:
    """
    Get mobilization capacity for a faction.
    Returns dict with:
        - total_capacity: sum of power from camps (land) + port territories (shared land+sea pool)
        - territories: list of {territory_id, power[, home_unit_capacity]} for camp-only and home-only territories
        - port_territories: list of {territory_id, power, sea_zone_ids[, home_unit_capacity]} — naval pool only for sea zones; optional home_unit_capacity for land units whose home is this port (e.g. Corsair at Umbar)
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
        power = territory_current_power(state, territory_id, territory_def)
        if _territory_has_standing_camp(state, territory_id, camp_defs):
            seen.add(territory_id)
            home_at_camp: dict[str, int] = {}
            for uid, uud in unit_defs.items():
                if getattr(uud, "faction", None) != faction_id:
                    continue
                if has_unit_special(uud, "home") and territory_id in _home_territory_ids(uud):
                    home_at_camp[uid] = 1
            owned_at_start = getattr(state, "faction_territories_at_turn_start", {}).get(faction_id, []) or []
            camp_power = power if territory_id in owned_at_start else 0
            camp_row: dict[str, Any] = {"territory_id": territory_id, "power": camp_power}
            if home_at_camp:
                camp_row["home_unit_capacity"] = home_at_camp
            territories.append(camp_row)
            total += camp_power
        elif _territory_has_port(territory_id, port_defs):
            seen.add(territory_id)
            sea_zone_ids = _sea_zones_adjacent_to_port_territory(territory_id, territory_defs)
            home_on_port: dict[str, int] = {}
            for unit_id, ud in unit_defs.items():
                if getattr(ud, "faction", None) != faction_id:
                    continue
                if has_unit_special(ud, "home") and territory_id in _home_territory_ids(ud):
                    home_on_port[unit_id] = 1
            port_territories.append({
                "territory_id": territory_id,
                "power": power,
                "sea_zone_ids": sea_zone_ids,
                **({"home_unit_capacity": home_on_port} if home_on_port else {}),
            })
            # Ports only mobilize naval to sea zones; do not add to total land capacity
    # Home-only territories: owned with no camp/port, or any subfaction land; cap 1 per unit type that has this as home
    for territory_id, territory in state.territories.items():
        if territory_id in seen:
            continue
        if territory.owner == faction_id:
            if _territory_has_standing_camp(state, territory_id, camp_defs) or _territory_has_port(territory_id, port_defs):
                continue
        elif not subfaction_owns_for(territory.owner, faction_id, faction_defs):
            continue
        home_units: dict[str, int] = {}
        for unit_id, ud in unit_defs.items():
            if getattr(ud, "faction", None) != faction_id:
                continue
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

    river_zones = []
    for tid, tdef in territory_defs.items():
        if not _is_river_zone(tdef):
            continue
        banks = river_banks_for_zone(state, faction_id, tid, territory_defs)
        if not banks:
            continue
        power = 0
        for bank_id in banks:
            bdef = territory_defs.get(bank_id)
            if bdef:
                power += territory_current_power(state, bank_id, bdef)
        if power > 0:
            river_zones.append({"river_zone_id": tid, "power": power})

    return {
        "total_capacity": total,
        "territories": territories,
        "port_territories": port_territories,
        "sea_zones": sea_zones,
        "river_zones": river_zones,
    }


def count_open_home_mobilization_slots_for_unit(
    state: GameState,
    faction_id: str,
    unit_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    camp_defs: dict[str, CampDefinition] | None,
    port_defs: dict[str, PortDefinition] | None,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition] | None = None,
) -> int:
    """
    Units with home special only deploy to home territories (and port homes). Count how many
    slots remain for this unit_id this mobilization phase after pending_mobilizations.
    """
    ud = unit_defs.get(unit_id)
    if not ud or not has_unit_special(ud, "home") or not _home_territory_ids(ud):
        return 0
    cap = get_mobilization_capacity(
        state, faction_id, territory_defs, camp_defs, port_defs, unit_defs, faction_defs
    )
    total = 0
    for bucket in (cap.get("territories") or [], cap.get("port_territories") or []):
        for t_info in bucket:
            tid = t_info.get("territory_id")
            if not tid:
                continue
            home_cap = t_info.get("home_unit_capacity") or {}
            if unit_id not in home_cap:
                continue
            max_n = int(home_cap.get(unit_id, 1) or 1)
            already = sum(
                int(u.get("count", 0) or 0)
                for pm in (state.pending_mobilizations or [])
                if getattr(pm, "destination", None) == tid
                for u in getattr(pm, "units", []) or []
                if u.get("unit_id") == unit_id
            )
            total += max(0, max_n - already)
    return total


def _faction_has_sea_raid_passengers_in_sea_zone(
    sea_zone: TerritoryState | None,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition] | None = None,
) -> bool:
    """
    True if this faction has land (non-naval) units in the sea hex — the same units the sea-raid land
    roster pulls from the sea zone (see reducer._sea_raid_attacker_units_from_board). Boats alone do not count.

    Legacy fallback when `territory_sea_raid_faction` has no entry for this land hex (older saves): require
    passengers still aboard so we do not tag sea_zone_id from stale `territory_sea_raid_from` alone.
    """
    if not sea_zone:
        return False
    for u in sea_zone.units:
        if not faction_acts_as(faction_defs, get_unit_faction(u, unit_defs), faction_id):
            continue
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud) or _is_naval_unit(ud):
            continue
        return True
    return False


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
    For sea zones, naval surface units and non-embarked aerial count (same as initiate_combat sea roster);
    land territories use all units.

    Sea raid: after combat_move ends, passengers offload onto the land hex (same as combative offload).
    Attackers are on that land territory; `sea_zone_id` comes from `territory_sea_raid_from` when the staging
    faction matches (see `territory_sea_raid_faction`), so full offload still advertises the sea raid for
    initiate_combat (Sea Raider bonus). Older saves without faction metadata fall back to requiring passengers
    still in the staging sea.
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
            ud = unit_defs.get(unit.unit_id)
            if is_sea and not participates_in_sea_hex_naval_combat(unit, ud):
                continue
            if faction_acts_as(faction_defs, unit_faction, faction_id):
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
            sea_raid_fac = getattr(state, "territory_sea_raid_faction", None) or {}
            staged_sea = sea_raid_from.get(territory_id)
            staged_fac = sea_raid_fac.get(territory_id)
            include_sea_zone = False
            if staged_sea:
                if staged_fac is not None:
                    include_sea_zone = staged_fac == faction_id
                else:
                    include_sea_zone = _faction_has_sea_raid_passengers_in_sea_zone(
                        state.territories.get(staged_sea),
                        faction_id,
                        unit_defs,
                        faction_defs,
                    )
            if include_sea_zone:
                entry["sea_zone_id"] = staged_sea
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
    from_terr = state.territories.get(from_territory)
    if not from_terr:
        return []
    units_in_stack = [u for u in from_terr.units if u.instance_id in unit_instance_ids]
    sea_drivers = [u for u in units_in_stack if _is_naval_unit(unit_defs.get(u.unit_id))]
    river_drivers = [u for u in units_in_stack if _is_river_unit(unit_defs.get(u.unit_id))]
    if sea_drivers and river_drivers:
        return []
    drivers = sea_drivers or river_drivers
    if not drivers:
        return []
    if sea_drivers:
        adjacent_seas = set(get_sea_zones_adjacent_to_land(to_land_territory, territory_defs))
    else:
        land_def = territory_defs.get(to_land_territory)
        adjacent_seas = set()
        if land_def and not _is_water_zone(land_def):
            for tid in getattr(land_def, "adjacent", []) or []:
                if _is_river_zone(territory_defs.get(tid)):
                    adjacent_seas.add(tid)
    if not adjacent_seas:
        return []
    # Sea zones reachable by sail (BFS over sea only; ignores combat_move "must attack" so empty seas count for offload)
    reachable_sea = get_sea_zones_reachable_by_sail(
        from_territory, state, drivers, territory_defs, unit_defs, faction_defs
    )
    if not reachable_sea:
        return []
    return sort_sea_zone_ids_numerically(adjacent_seas & reachable_sea)


def validate_sail_move_for_offload_sea_raid(
    state: GameState,
    origin: str,
    destination: str,
    sail_land_territory_id: str,
    units_in_stack: list,
    unit_instance_ids: list,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    faction_id: str,
    phase: str,
) -> ValidationResult:
    """
    Sea→sea sail that only repositions the fleet for an offload/sea raid onto a specific land hex.
    Uses get_valid_offload_sea_zones (sail BFS + adjacency to land), not get_reachable_territories_for_unit,
    so empty/friendly sea hexes remain valid combat_move destinations for this chain only.
    """
    sail_land = resolve_territory_key_in_state(
        state, str(sail_land_territory_id or "").strip(), territory_defs
    )
    land_def = territory_defs.get(sail_land)
    if not land_def or _is_water_zone(land_def):
        return ValidationResult(False, f"Invalid offload/raid land territory: {sail_land}")
    valid_zones = get_valid_offload_sea_zones(
        origin,
        sail_land,
        state,
        list(unit_instance_ids),
        unit_defs,
        territory_defs,
        faction_defs,
        phase,
    )
    if destination not in valid_zones:
        return ValidationResult(
            False,
            f"Cannot sail to {destination} to raid/offload onto {sail_land}. Valid sea zones: {valid_zones}",
        )
    sea_drivers = [u for u in units_in_stack if _is_naval_unit(unit_defs.get(u.unit_id))]
    river_drivers = [u for u in units_in_stack if _is_river_unit(unit_defs.get(u.unit_id))]
    if sea_drivers and river_drivers:
        return ValidationResult(False, "Ships and river units cannot sail in the same move")
    naval_drivers = sea_drivers or river_drivers
    if not naval_drivers:
        return ValidationResult(
            False,
            "Sail for sea raid/offload requires at least one naval unit in the move",
        )
    driver_ids = {u.instance_id for u in naval_drivers}
    passengers = [u for u in units_in_stack if u.instance_id not in driver_ids]
    for u in passengers:
        ud = unit_defs.get(u.unit_id)
        if not is_land_unit(ud):
            return ValidationResult(
                False,
                f"Unit {u.instance_id} cannot be carried (only land units can be passengers)",
            )
        if not is_transportable(ud):
            return ValidationResult(
                False,
                f"Unit {u.instance_id} cannot be transported (no transportable tag)",
            )
    naval_capacity = sum(
        getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
        for u in naval_drivers
    )
    if len(passengers) > naval_capacity:
        return ValidationResult(
            False,
            f"Too many passengers ({len(passengers)}) for transport capacity ({naval_capacity})",
        )
    return ValidationResult(True)


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
        if not tdef or not _is_water_zone(tdef):
            continue
        my_naval = []
        my_land = []
        enemy_units = False
        for unit in territory.units:
            uf = get_unit_faction(unit, unit_defs)
            if faction_acts_as(faction_defs, uf, faction_id):
                ud = unit_defs.get(unit.unit_id)
                if _is_transport_boat_for_zone(ud, tdef):
                    my_naval.append(unit)
                elif _is_naval_unit(ud) or _is_river_unit(ud):
                    continue
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
            if adj_def and not _is_water_zone(adj_def):
                adj_lands.add(adj_id)
        for tid, land_def in territory_defs.items():
            if _is_water_zone(land_def):
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

    if getattr(state.active_combat, "sea_zone_id", None):
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
            if not faction_acts_as(faction_defs, u_faction, current_faction):
                continue
            unit_def = unit_defs.get(unit.unit_id)
            if not is_aerial_unit(unit_def):
                continue
            if is_friendly_territory_for_landing(
                territory, current_faction, faction_defs, unit_defs,
                state=state, territory_id=territory_id,
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
    power = faction_resources (income is credited when a faction ends their turn); power_per_turn = owned territory production plus ring power credited to this faction.
    unit_power = sum of power cost for all active units for that faction.
    """
    unit_defs = unit_defs or {}
    factions: dict[str, dict[str, int]] = {}
    subfactions: dict[str, dict[str, int]] = {}
    for faction_id in faction_defs:
        territories_count = 0
        strongholds_count = 0
        power_per_turn = 0
        pool_ids = set(pool_territory_ids(state, faction_id, faction_defs))
        for tid, ts in state.territories.items():
            owner = ts.owner
            if controlling_faction_id(faction_defs, owner) != faction_id:
                continue
            territories_count += 1
            tdef = territory_defs.get(tid)
            if tdef and getattr(tdef, "is_stronghold", False):
                strongholds_count += 1
            # Subfaction land is controlled here. It pays only when its economy rule is pool and the capital is held.
            if tdef and (owner == faction_id or tid in pool_ids):
                power_per_turn += territory_current_power(state, tid, tdef)
        power_per_turn += power_for_faction(state, faction_id, unit_defs, faction_defs)
        power = state.faction_resources.get(faction_id, {}).get("power", 0)
        factions[faction_id] = {
            "territories": territories_count,
            "strongholds": strongholds_count,
            "power": power,
            "power_per_turn": power_per_turn,
            "units": 0,
            "unit_power": 0,
        }
        # Each subfaction's share of the parent row; power_per_turn is only what it pays the parent.
        pays = faction_owns_capital(state, faction_id, faction_defs)
        for sub_id in child_ids(faction_defs, faction_id):
            economy = rule_for(state, sub_id)["economy"]
            if economy == "none":
                continue
            sub_pays = pays and economy == "pool"
            sub = {"territories": 0, "strongholds": 0, "power": 0, "power_per_turn": 0, "units": 0, "unit_power": 0}
            for tid, ts in state.territories.items():
                if ts.owner != sub_id:
                    continue
                sub["territories"] += 1
                tdef = territory_defs.get(tid)
                if tdef and getattr(tdef, "is_stronghold", False):
                    sub["strongholds"] += 1
                if tdef and sub_pays:
                    sub["power_per_turn"] += territory_current_power(state, tid, tdef)
            if sub_pays:
                sub["power_per_turn"] += power_for_faction(state, sub_id, unit_defs, faction_defs)
            subfactions[sub_id] = sub

    # Count units and unit_power by unit's faction (so sea units in sea zones are included)
    for tid, ts in state.territories.items():
        for unit in ts.units:
            ud = unit_defs.get(unit.unit_id)
            if not ud:
                continue
            own = getattr(ud, "faction", None)
            fid = controlling_faction_id(faction_defs, own)
            if fid not in factions:
                continue
            cost = ud.cost.get("power", 0) if isinstance(getattr(ud, "cost", None), dict) else 0
            factions[fid]["units"] += 1
            factions[fid]["unit_power"] += cost
            if own in subfactions:
                subfactions[own]["units"] += 1
                subfactions[own]["unit_power"] += cost

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

    out: dict[str, Any] = {
        "factions": factions,
        "alliances": alliances,
        "neutral_strongholds": neutral_strongholds,
    }
    if subfactions:
        out["subfactions"] = subfactions
    # Victory thresholds for UI markers on the good | neutral | evil stronghold bar (setup manifest).
    stronghold_vc: dict[str, int] = {}
    vc = getattr(state, "victory_criteria", None) or {}
    if isinstance(vc, dict):
        sh = vc.get("strongholds")
        if isinstance(sh, dict):
            for key in ("good", "evil"):
                raw = sh.get(key)
                if raw is None:
                    continue
                try:
                    n = int(raw)
                    if n > 0:
                        stronghold_vc[key] = n
                except (TypeError, ValueError):
                    pass
    if stronghold_vc:
        out["stronghold_victory"] = stronghold_vc

    return out


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
            dest_owner = (
                effective_territory_owner(state, dest_id)
                if dest_territory
                else None
            )

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
