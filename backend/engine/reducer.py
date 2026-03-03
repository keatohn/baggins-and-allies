"""
Main game reducer.
Applies actions to state, enforcing rules and producing new state.
Returns (new_state, events) where events describe what happened.
"""

from copy import deepcopy
from backend.engine.state import GameState, UnitStack, TerritoryState, Unit, ActiveCombat, CombatRoundResult, PendingMove, PendingMobilization, PendingCampPlacement
from backend.engine.actions import Action
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition, CampDefinition
from backend.engine.combat import (
    resolve_combat_round,
    resolve_archer_prefire,
    RoundResult,
    group_dice_by_stat,
    ARCHETYPE_ARCHER,
    ARCHETYPE_CAVALRY,
    calculate_required_dice,
    compute_terrain_stat_modifiers,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    merge_stat_modifiers,
)
from backend.engine.movement import get_reachable_territories_for_unit, calculate_movement_cost, movement_cost_along_path, get_shortest_path
from backend.engine.queries import _territory_is_friendly_for_retreat, get_aerial_units_must_move
from backend.engine.utils import unitstack_to_units, get_unit_faction, is_ground_unit
from backend.engine.events import (
    GameEvent,
    phase_changed,
    turn_started,
    turn_ended,
    resources_changed,
    units_purchased,
    income_calculated,
    income_collected,
    units_moved,
    combat_started,
    combat_round_resolved,
    combat_ended,
    units_retreated,
    territory_captured,
    unit_destroyed,
    units_mobilized,
    victory,
)


def _build_round_unit_display(
    unit: Unit,
    unit_def: UnitDefinition | None,
    stat_mod: int,
    is_attacker: bool,
    faction: str,
    territory_def: TerritoryDefinition | None,
    terrain_mods: dict[str, int],
    captain_mods: dict[str, int],
    anticav_mods: dict[str, int],
) -> dict:
    """Build one unit dict for combat_round_resolved payload. Shape must match events.py docstring (UI contract)."""
    if not unit_def:
        return {
            "instance_id": unit.instance_id,
            "unit_id": unit.unit_id,
            "display_name": unit.unit_id,
            "attack": 0,
            "defense": 0,
            "effective_attack": 0 if is_attacker else None,
            "effective_defense": 0 if not is_attacker else None,
            "health": getattr(unit, "base_health", 1),
            "remaining_health": unit.remaining_health,
            "remaining_movement": getattr(unit, "remaining_movement", 0),
            "is_archer": False,
            "faction": faction,
            "terror": False,
            "terrain_mountain": False,
            "terrain_forest": False,
            "captain_bonus": False,
            "anti_cavalry": False,
        }
    base_attack = getattr(unit_def, "attack", 0)
    base_defense = getattr(unit_def, "defense", 0)
    tags = getattr(unit_def, "tags", []) or []
    archetype = getattr(unit_def, "archetype", "")
    is_archer = archetype == ARCHETYPE_ARCHER or "archer" in tags
    terrain_type = (getattr(territory_def, "terrain_type", None) or "").lower() if territory_def else ""
    has_terrain = unit.instance_id in terrain_mods and terrain_mods[unit.instance_id]
    return {
        "instance_id": unit.instance_id,
        "unit_id": unit.unit_id,
        "display_name": getattr(unit_def, "display_name", unit.unit_id),
        "attack": base_attack,
        "defense": base_defense,
        "effective_attack": base_attack + stat_mod if is_attacker else None,
        "effective_defense": base_defense + stat_mod if not is_attacker else None,
        "health": getattr(unit_def, "health", 1),
        "remaining_health": unit.remaining_health,
        "remaining_movement": getattr(unit, "remaining_movement", 0),
        "is_archer": is_archer,
        "faction": faction,
        "terror": is_attacker and "terror" in tags,
        "terrain_mountain": has_terrain and terrain_type in ("mountain", "mountains"),
        "terrain_forest": has_terrain and terrain_type == "forest",
        "captain_bonus": unit.instance_id in captain_mods and captain_mods[unit.instance_id] > 0,
        "anti_cavalry": unit.instance_id in anticav_mods and anticav_mods[unit.instance_id] > 0,
    }


def _normalize_unit_health_for_combat(
    units: list,
    unit_defs: dict[str, UnitDefinition],
) -> None:
    """Ensure multi-HP units have correct base_health/remaining_health from unit_def (fixes legacy/corrupt state)."""
    for unit in units:
        ud = unit_defs.get(unit.unit_id)
        if not ud or getattr(ud, "health", 1) <= 1:
            continue
        def_health = getattr(ud, "health", 1)
        if unit.base_health != def_health:
            unit.base_health = def_health
            unit.remaining_health = min(max(1, unit.remaining_health), def_health)


def _territory_has_standing_camp(
    state: GameState,
    territory_id: str,
    camp_defs: dict[str, CampDefinition],
) -> bool:
    """True if the territory has a camp that is still standing (not destroyed by capture)."""
    for camp_id in state.camps_standing:
        if state.dynamic_camps.get(camp_id) == territory_id:
            return True
        camp = camp_defs.get(camp_id)
        if camp and camp.territory_id == territory_id:
            return True
    return False


def _faction_owns_capital(
    state: GameState,
    faction_id: str,
    faction_defs: dict[str, FactionDefinition],
) -> bool:
    """Check if a faction owns their capital territory."""
    faction_def = faction_defs.get(faction_id)
    if not faction_def:
        return False
    capital = faction_def.capital
    capital_state = state.territories.get(capital)
    if not capital_state:
        return False
    return capital_state.owner == faction_id


# Phase rules: which action types are allowed in which phases
# Note: During active combat, only continue_combat and retreat are allowed
PHASE_ALLOWED_ACTIONS = {
    "purchase": ["purchase_units", "purchase_camp", "end_phase"],
    "combat_move": ["move_units", "cancel_move", "end_phase"],
    "combat": ["initiate_combat", "continue_combat", "retreat", "end_phase"],
    "non_combat_move": ["move_units", "cancel_move", "end_phase"],
    "mobilization": ["mobilize_units", "queue_camp_placement", "cancel_camp_placement", "cancel_mobilization", "end_phase", "end_turn"],
}


def _validate_action_for_phase(action: Action, state: GameState) -> None:
    """
    Validate that an action is allowed in the current phase and combat state.

    Special rules for combat phase:
    - If active_combat exists: only continue_combat and retreat allowed
    - If no active_combat: only initiate_combat and end_phase allowed
    """
    phase = state.phase
    allowed_actions = PHASE_ALLOWED_ACTIONS.get(phase, [])

    if action.type not in allowed_actions:
        raise ValueError(
            f"Action '{action.type}' is not allowed in phase '{phase}'. "
            f"Allowed actions: {', '.join(allowed_actions)}"
        )

    # Special combat phase restrictions
    if phase == "combat":
        if state.active_combat is not None:
            # During active combat, only continue_combat and retreat allowed
            if action.type not in ["continue_combat", "retreat"]:
                raise ValueError(
                    f"Active combat in progress. Must use 'continue_combat' or 'retreat', "
                    f"not '{action.type}'"
                )
        else:
            # No active combat, can't continue or retreat
            if action.type in ["continue_combat", "retreat"]:
                raise ValueError(
                    f"No active combat to {action.type}. Use 'initiate_combat' first."
                )


def apply_action(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    Apply a single action to the current state, returning new state and events.

    Validates:
    - Action faction matches current_faction
    - Action is valid for current phase and combat state

    Args:
        state: Current game state
        action: Action to apply
        unit_defs: Unit definitions
        territory_defs: Territory definitions
        faction_defs: Faction definitions
        camp_defs: Camp definitions (mobilization points); used for standing camps and mobilization

    Returns:
        Tuple of (new_state, events) where events describe what happened
    """
    if camp_defs is None:
        camp_defs = {}
    # Check if game is already won
    if state.winner is not None:
        raise ValueError(f"Game is over. {state.winner} alliance has won.")

    # Validate faction
    if action.faction != state.current_faction:
        raise ValueError(
            f"Action faction {action.faction} does not match current faction {state.current_faction}")

    # Validate action is allowed in current phase and combat state
    _validate_action_for_phase(action, state)

    new_state = state.copy()
    events: list[GameEvent] = []

    if action.type == "purchase_camp":
        new_state, evts = _handle_purchase_camp(
            new_state, action, camp_defs or {}, territory_defs)
        events.extend(evts)

    elif action.type == "place_camp":
        new_state, evts = _handle_place_camp(new_state, action, camp_defs or {})
        events.extend(evts)

    elif action.type == "queue_camp_placement":
        new_state, evts = _handle_queue_camp_placement(new_state, action, camp_defs or {})
        events.extend(evts)

    elif action.type == "cancel_camp_placement":
        new_state, evts = _handle_cancel_camp_placement(new_state, action)
        events.extend(evts)

    elif action.type == "purchase_units":
        new_state, evts = _handle_purchase_units(
            new_state, action, unit_defs, faction_defs)
        events.extend(evts)

    elif action.type == "move_units":
        new_state, evts = _handle_move_units(
            new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action.type == "initiate_combat":
        new_state, evts = _handle_initiate_combat(
            new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action.type == "continue_combat":
        new_state, evts = _handle_continue_combat(
            new_state, action, unit_defs, territory_defs)
        events.extend(evts)

    elif action.type == "retreat":
        new_state, evts = _handle_retreat(new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action.type == "mobilize_units":
        new_state, evts = _handle_mobilize_units(
            new_state, action, unit_defs, territory_defs, faction_defs, camp_defs)
        events.extend(evts)

    elif action.type == "cancel_move":
        new_state, evts = _handle_cancel_move(new_state, action)
        events.extend(evts)

    elif action.type == "cancel_mobilization":
        new_state, evts = _handle_cancel_mobilization(new_state, action)
        events.extend(evts)

    elif action.type == "end_phase":
        new_state, evts = _handle_end_phase(
            new_state, unit_defs, territory_defs, faction_defs, camp_defs)
        events.extend(evts)

    elif action.type == "end_turn":
        new_state, evts = _handle_end_turn(
            new_state, territory_defs, faction_defs, camp_defs)
        events.extend(evts)

    else:
        raise ValueError(f"Unknown action type: {action.type}")

    return new_state, events


def _handle_purchase_units(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Purchase units for a faction.
    Validates:
    - Faction owns their capital (required to purchase)
    - Only purchasable units can be bought
    - Faction has sufficient resources
    - Units are added to capital territory
    """
    events: list[GameEvent] = []
    faction_id = action.faction
    purchases = action.payload.get("purchases", {})  # {unit_id: count}

    faction_def = faction_defs.get(faction_id)
    if not faction_def:
        raise ValueError(f"Unknown faction: {faction_id}")

    # Check capital ownership - cannot purchase if capital is captured
    if not _faction_owns_capital(state, faction_id, faction_defs):
        raise ValueError(f"Cannot purchase units: {faction_id}'s capital has been captured")

    # Validate all purchases and calculate total cost
    total_cost = {}
    for unit_id, count in purchases.items():
        if count <= 0:
            continue

        unit_def = unit_defs.get(unit_id)
        if not unit_def:
            raise ValueError(f"Unknown unit: {unit_id}")

        if not unit_def.purchasable:
            raise ValueError(f"Unit {unit_id} is not purchasable")

        if unit_def.faction != faction_id:
            raise ValueError(f"Faction {faction_id} cannot purchase {unit_id}")

        # Accumulate cost
        for resource_id, cost_amount in unit_def.cost.items():
            if resource_id not in total_cost:
                total_cost[resource_id] = 0
            total_cost[resource_id] += cost_amount * count

    # Validate faction has resources
    faction_resources = state.faction_resources.get(faction_id, {})
    for resource_id, required_amount in total_cost.items():
        available = faction_resources.get(resource_id, 0)
        if available < required_amount:
            raise ValueError(
                f"Insufficient {resource_id}: have {available}, need {required_amount}")

    # Deduct resources and emit events
    for resource_id, amount in total_cost.items():
        old_value = state.faction_resources[faction_id][resource_id]
        state.faction_resources[faction_id][resource_id] -= amount
        new_value = state.faction_resources[faction_id][resource_id]
        events.append(resources_changed(faction_id, resource_id, old_value, new_value, "purchase"))

    # Add units to faction's purchased pool (for mobilization in phase 5)
    if faction_id not in state.faction_purchased_units:
        state.faction_purchased_units[faction_id] = []

    for unit_id, count in purchases.items():
        if count <= 0:
            continue

        # Find existing stack or create new one
        found = False
        for stack in state.faction_purchased_units[faction_id]:
            if stack.unit_id == unit_id:
                stack.count += count
                found = True
                break

        if not found:
            state.faction_purchased_units[faction_id].append(
                UnitStack(unit_id=unit_id, count=count))

    # Emit purchase event
    events.append(units_purchased(faction_id, purchases, total_cost))

    return state, events


def _handle_purchase_camp(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Purchase a camp. Deduct camp_cost (power); add to pending_camps with territory_options (only territories that produce power)."""
    events: list[GameEvent] = []
    faction_id = action.faction

    if state.phase != "purchase":
        raise ValueError("Can only purchase a camp during purchase phase")
    if state.current_faction != faction_id:
        raise ValueError("Not this faction's turn")

    cost = getattr(state, "camp_cost", 10)
    resources = state.faction_resources.get(faction_id, {})
    power = resources.get("power", 0)
    if power < cost:
        raise ValueError(f"Insufficient power for camp: have {power}, need {cost}")

    # Territories owned at turn start that don't have a camp, not already chosen, and produce power (so units can mobilize there)
    owned_at_start = state.faction_territories_at_turn_start.get(faction_id, [])
    already_placed = [
        p.get("placed_territory_id") for p in state.pending_camps
        if p.get("placed_territory_id")
    ]
    territory_options = []
    for tid in owned_at_start:
        if tid in already_placed:
            continue
        if _territory_has_standing_camp(state, tid, camp_defs):
            continue
        tdef = territory_defs.get(tid)
        if tdef and (tdef.produces.get("power", 0) or 0) > 0:
            territory_options.append(tid)

    if not territory_options:
        raise ValueError("No valid territory to place a camp (all owned territories already have a camp or were used)")

    # Deduct cost
    state.faction_resources[faction_id]["power"] = power - cost
    events.append(resources_changed(faction_id, "power", power, power - cost, "purchase_camp"))

    state.pending_camps.append({
        "territory_options": territory_options,
        "placed_territory_id": None,
    })

    return state, events


def _handle_place_camp(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Place a purchased camp on a territory. Valid in mobilization phase."""
    events: list[GameEvent] = []
    faction_id = action.faction
    camp_index = action.payload.get("camp_index", -1)
    territory_id = action.payload.get("territory_id", "")

    if state.phase != "mobilization":
        raise ValueError("Can only place a camp during mobilization phase")
    if state.current_faction != faction_id:
        raise ValueError("Not this faction's turn")
    if camp_index < 0 or camp_index >= len(state.pending_camps):
        raise ValueError(f"Invalid camp_index {camp_index}; have {len(state.pending_camps)} pending camps")

    pending = state.pending_camps[camp_index]
    if pending.get("placed_territory_id"):
        raise ValueError("This camp has already been placed")

    options = pending.get("territory_options") or []
    if territory_id not in options:
        raise ValueError(
            f"Territory {territory_id} is not a valid placement (options: {options})"
        )
    if _territory_has_standing_camp(state, territory_id, camp_defs):
        raise ValueError(f"Territory {territory_id} already has a camp")

    # Place: create dynamic camp and add to camps_standing
    camp_id = f"purchased_camp_{territory_id}"
    state.dynamic_camps[camp_id] = territory_id
    state.camps_standing.append(camp_id)
    state.pending_camps[camp_index]["placed_territory_id"] = territory_id

    # mobilization_camps is fixed at turn start; newly placed camp only counts next turn
    return state, events


def _handle_queue_camp_placement(
    state: GameState,
    action: Action,
    camp_defs: dict[str, CampDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Queue a camp placement (applied at end of mobilization phase, like mobilize_units)."""
    events: list[GameEvent] = []
    faction_id = action.faction
    camp_index = action.payload.get("camp_index", -1)
    territory_id = action.payload.get("territory_id", "")

    if state.phase != "mobilization":
        raise ValueError("Can only queue camp placement during mobilization phase")
    if state.current_faction != faction_id:
        raise ValueError("Not this faction's turn")
    if camp_index < 0 or camp_index >= len(state.pending_camps):
        raise ValueError(f"Invalid camp_index {camp_index}; have {len(state.pending_camps)} pending camps")

    pending = state.pending_camps[camp_index]
    if pending.get("placed_territory_id"):
        raise ValueError("This camp has already been placed")

    options = pending.get("territory_options") or []
    if territory_id not in options:
        raise ValueError(
            f"Territory {territory_id} is not a valid placement (options: {options})"
        )
    if _territory_has_standing_camp(state, territory_id, camp_defs):
        raise ValueError(f"Territory {territory_id} already has a camp")

    # Already queued for this camp_index?
    for p in state.pending_camp_placements:
        if p.camp_index == camp_index:
            raise ValueError(f"Camp {camp_index} is already queued for placement")

    state.pending_camp_placements.append(PendingCampPlacement(camp_index=camp_index, territory_id=territory_id))
    return state, events


def _handle_cancel_camp_placement(
    state: GameState,
    action: Action,
) -> tuple[GameState, list[GameEvent]]:
    """Remove a queued camp placement."""
    placement_index = action.payload.get("placement_index", -1)
    if placement_index < 0 or placement_index >= len(state.pending_camp_placements):
        raise ValueError(
            f"Invalid placement_index {placement_index}. Pending: {len(state.pending_camp_placements)}"
        )
    state.pending_camp_placements.pop(placement_index)
    return state, []


def _apply_pending_camp_placements(
    state: GameState,
    camp_defs: dict[str, CampDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Apply all queued camp placements (at end of mobilization phase)."""
    events: list[GameEvent] = []
    for p in state.pending_camp_placements:
        camp_index = p.camp_index
        territory_id = p.territory_id
        if camp_index < 0 or camp_index >= len(state.pending_camps):
            continue
        pending = state.pending_camps[camp_index]
        if pending.get("placed_territory_id"):
            continue
        options = pending.get("territory_options") or []
        if territory_id not in options:
            continue
        if _territory_has_standing_camp(state, territory_id, camp_defs):
            continue
        camp_id = f"purchased_camp_{territory_id}"
        state.dynamic_camps[camp_id] = territory_id
        state.camps_standing.append(camp_id)
        state.pending_camps[camp_index]["placed_territory_id"] = territory_id
    state.pending_camp_placements = []
    return state, events


def _handle_move_units(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Declare a pending move. Units are not actually moved until phase ends.
    Validates:
    - Destination is reachable based on unit's remaining_movement and current phase
    - Units exist in source territory and belong to the faction
    - Each unit has sufficient remaining_movement
    - Unit is not already in a pending move
    """
    events: list[GameEvent] = []
    from_id = action.payload.get("from")
    to_id = action.payload.get("to")
    unit_instance_ids = action.payload.get("unit_instance_ids", [])
    faction_id = action.faction

    if from_id not in state.territories or to_id not in state.territories:
        raise ValueError(f"Invalid territory: {from_id} or {to_id}")

    from_territory = state.territories[from_id]

    if len(unit_instance_ids) == 0:
        raise ValueError("No units specified to move")

    # Build a lookup of units in source territory by instance_id
    units_by_id = {unit.instance_id: unit for unit in from_territory.units}

    # Check which units are already in pending moves
    already_pending = set()
    for pm in state.pending_moves:
        already_pending.update(pm.unit_instance_ids)

    # Validate all units exist in source territory and belong to the faction
    units_to_move = []
    for instance_id in unit_instance_ids:
        if instance_id in already_pending:
            raise ValueError(f"Unit {instance_id} already has a pending move")
        unit = units_by_id.get(instance_id)
        if not unit:
            raise ValueError(f"Unit {instance_id} not found in {from_id}")
        # Validate unit belongs to the faction (use unit def: faction from unit def; instance_id can contain underscores)
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def or unit_def.faction != faction_id:
            raise ValueError(f"Unit {instance_id} does not belong to {faction_id}")
        units_to_move.append(unit)

    charge_through = action.payload.get("charge_through")
    if charge_through is not None and not isinstance(charge_through, list):
        charge_through = []
    charge_through = [str(t) for t in charge_through if str(t) != to_id] if charge_through else []
    # Destination must never be in charge_through (we only pass through; destination can have units)

    all_charge_routes: list[dict[str, list[list[str]]]] = []
    for unit in units_to_move:
        reachable, charge_routes = get_reachable_territories_for_unit(
            unit,
            from_id,
            state,
            unit_defs,
            territory_defs,
            faction_defs,
            state.phase,
        )
        all_charge_routes.append(charge_routes)

        if to_id not in reachable:
            raise ValueError(
                f"Unit {unit.instance_id} cannot reach {to_id} from {from_id} "
                f"(remaining_movement={unit.remaining_movement}, phase={state.phase})"
            )

    if charge_through:
        # Path must be valid for every unit we're moving (intersection of all units' routes)
        valid_routes = None
        for cr in all_charge_routes:
            routes_for_dest = cr.get(to_id, [])
            if valid_routes is None:
                valid_routes = list(routes_for_dest)
            else:
                valid_routes = [r for r in valid_routes if r in routes_for_dest]
        if valid_routes is None:
            valid_routes = []
        if charge_through not in valid_routes:
            raise ValueError(
                f"Invalid charge_through for {to_id}: must be one of the valid charging routes"
            )

    pending_move = PendingMove(
        from_territory=from_id,
        to_territory=to_id,
        unit_instance_ids=unit_instance_ids,
        phase=state.phase,
        charge_through=charge_through,
    )
    state.pending_moves.append(pending_move)

    # Emit movement event (declared, not yet executed)
    events.append(units_moved(faction_id, from_id, to_id, unit_instance_ids, state.phase))

    return state, events


def _apply_pending_moves(
    state: GameState,
    phase: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Apply all pending moves for the given phase.
    Called at phase end to actually move the units.
    """
    events: list[GameEvent] = []
    
    # Get moves for this phase
    moves_to_apply = [pm for pm in state.pending_moves if pm.phase == phase]
    remaining_moves = [pm for pm in state.pending_moves if pm.phase != phase]
    state.pending_moves = remaining_moves

    # Apply charge-through moves before moves whose destination is a via-territory.
    # If A charges through T and B has destination T, A must be applied first so T is still empty for A.
    # Use a deterministic key (not id(m)) so deepcopy(state) produces the same apply order as real state.
    def _move_key(m) -> tuple:
        return (m.from_territory, m.to_territory, tuple(m.unit_instance_ids))

    def _charge_through_order(moves: list) -> list:
        keys = [_move_key(m) for m in moves]
        moves_by_key = dict(zip(keys, moves))
        ct = {k: getattr(m, "charge_through", None) or [] for k, m in zip(keys, moves)}
        to_id = {k: m.to_territory for k, m in zip(keys, moves)}
        succ = {k: [] for k in keys}
        in_degree = {k: 0 for k in keys}
        for i, a in enumerate(moves):
            ak = keys[i]
            for tid in ct.get(ak, []):
                for j, b in enumerate(moves):
                    if i == j:
                        continue
                    if to_id.get(keys[j]) == tid:
                        succ[ak].append(keys[j])
                        in_degree[keys[j]] += 1
        order = [moves_by_key[k] for k in keys if in_degree[k] == 0]
        order.sort(key=_move_key)
        q = list(order)
        while q:
            a = q.pop(0)
            ak = _move_key(a)
            for bid in sorted(succ[ak]):
                in_degree[bid] -= 1
                if in_degree[bid] == 0:
                    order.append(moves_by_key[bid])
                    q.append(moves_by_key[bid])
        if len(order) < len(moves):
            order = sorted(moves, key=_move_key)
        return order

    moves_to_apply = _charge_through_order(moves_to_apply)

    for pending_move in moves_to_apply:
        from_id = pending_move.from_territory
        to_id = pending_move.to_territory
        unit_instance_ids = pending_move.unit_instance_ids
        charge_through = getattr(pending_move, "charge_through", None) or []
        charge_through = [t for t in charge_through if t != to_id]
        # Destination must never be in charge_through (only via-territories are checked for empty)

        from_territory = state.territories.get(from_id)
        to_territory = state.territories.get(to_id)

        if not from_territory or not to_territory:
            continue  # Skip invalid moves

        # Build lookup of units in source (need for faction_id and moves)
        units_by_id = {u.instance_id: u for u in from_territory.units}
        moving_units = [units_by_id[i] for i in unit_instance_ids if i in units_by_id]
        force_has_ground = any(is_ground_unit(unit_defs.get(u.unit_id)) for u in moving_units)

        # Cavalry charging: conquer only empty enemy/unowned via-territories (never friendly/allied)
        faction_id = None
        if unit_instance_ids:
            first_unit = units_by_id.get(unit_instance_ids[0])
            faction_id = get_unit_faction(first_unit, unit_defs) if first_unit else None
            if faction_id:
                moving_faction_def = faction_defs.get(faction_id)
                moving_alliance = moving_faction_def.alliance if moving_faction_def else ""
                # Use charge_through from payload, or infer from shortest path only for cavalry charges
                territories_to_capture = list(charge_through)
                if not territories_to_capture and phase == "combat_move" and from_id != to_id:
                    first_unit = units_by_id.get(unit_instance_ids[0])
                    ud = unit_defs.get(first_unit.unit_id) if first_unit else None
                    is_cavalry = ud and getattr(ud, "archetype", "") == ARCHETYPE_CAVALRY
                    if is_cavalry:
                        path = get_shortest_path(from_id, to_id, territory_defs)
                        if path and len(path) > 2:
                            # Middle territories (exclude from_id and to_id) — cavalry charge conquers path
                            for tid in path[1:-1]:
                                t = state.territories.get(tid)
                                tdef = territory_defs.get(tid)
                                if not t or not tdef or not getattr(tdef, "ownable", True):
                                    continue
                                if len(t.units) > 0:
                                    continue
                                if t.owner is None or t.owner != faction_id:
                                    territories_to_capture.append(tid)
                # Only conquer via-territories that are unowned or enemy (never friendly/allied)
                for tid in territories_to_capture:
                    if not force_has_ground:
                        continue  # Aerial-only forces cannot conquer
                    t = state.territories.get(tid)
                    tdef = territory_defs.get(tid)
                    if not t or not tdef or not getattr(tdef, "ownable", True):
                        continue
                    owner = t.owner
                    if owner == faction_id:
                        continue  # friendly: never conquer
                    if owner and moving_faction_def:
                        owner_def = faction_defs.get(owner)
                        if owner_def and owner_def.alliance == moving_alliance:
                            continue  # allied: never conquer
                    # unowned or enemy (empty already validated earlier for charge path)
                    if owner is None:
                        state.pending_captures[tid] = faction_id
                    elif len(t.units) == 0:
                        state.pending_captures[tid] = faction_id

        # Calculate the movement cost (distance) to destination.
        # When charging through territories, use the actual path: from_id -> charge_through[0] -> ... -> to_id.
        if charge_through:
            path = [from_id] + list(charge_through) + [to_id]
            distance = movement_cost_along_path(path, territory_defs)
            if distance is None:
                raise ValueError(
                    f"Charge path from {from_id} through {charge_through} to {to_id} is invalid (non-adjacent steps)"
                )
        else:
            distance = calculate_movement_cost(from_id, to_id, territory_defs)
        if distance is None:
            continue  # Skip if no path

        # Enforce movement range: no unit can move farther than its remaining_movement
        for instance_id in unit_instance_ids:
            unit = units_by_id.get(instance_id)
            if unit and distance > unit.remaining_movement:
                raise ValueError(
                    f"Move from {from_id} to {to_id} has distance {distance} but unit {instance_id} "
                    f"has remaining_movement={unit.remaining_movement}"
                )

        # Cavalry charge_through: enemy/unowned via-territories must be empty; friendly/allied may have units
        if charge_through and faction_id:
            moving_faction_def = faction_defs.get(faction_id)
            moving_alliance = moving_faction_def.alliance if moving_faction_def else ""
            for tid in charge_through:
                t = state.territories.get(tid)
                if not t or len(t.units) == 0:
                    continue
                owner = t.owner
                if owner == faction_id:
                    continue  # friendly: allow units
                if owner and moving_faction_def:
                    owner_def = faction_defs.get(owner)
                    if owner_def and owner_def.alliance == moving_alliance:
                        continue  # allied: allow units
                raise ValueError(
                    f"Charge path cannot pass through {tid}: territory has units (charging only through empty enemy/unowned or through friendly/allied)"
                )

        # Move each unit
        for instance_id in unit_instance_ids:
            unit = units_by_id.get(instance_id)
            if unit:
                from_territory.units.remove(unit)
                unit.remaining_movement -= distance
                to_territory.units.append(unit)

        # Check if this is combat_move into territory we capture (undefended enemy or empty unowned)
        # Aerial-only forces cannot conquer: require at least one ground unit
        if unit_instance_ids and faction_id and phase == "combat_move" and force_has_ground:
            to_owner = to_territory.owner
            to_def = territory_defs.get(to_id)
            if not to_def or not getattr(to_def, "ownable", True):
                pass
            elif to_owner is None:
                # Empty unowned (neutral): moving in captures it. If neutral had defenders, combat will decide.
                other_units = [u for u in to_territory.units if u.instance_id not in unit_instance_ids]
                if not other_units:
                    state.pending_captures[to_id] = faction_id
            elif to_owner != faction_id:
                # Enemy-owned: capture only if no enemy units left after our units moved in
                moving_faction_def = faction_defs.get(faction_id)
                moving_alliance = moving_faction_def.alliance if moving_faction_def else ""
                owner_def = faction_defs.get(to_owner)
                owner_alliance = owner_def.alliance if owner_def else ""
                if moving_alliance != owner_alliance:
                    enemy_units = [
                        u for u in to_territory.units
                        if get_unit_faction(u, unit_defs) != faction_id
                    ]
                    if not enemy_units:
                        state.pending_captures[to_id] = faction_id
    
    return state, events


def get_state_after_pending_moves(
    state: GameState,
    phase: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> GameState:
    """
    Return a copy of state with all pending moves for the given phase applied.
    Used to check conditions (e.g. aerial units still in enemy territory) after pending moves.
    """
    state_copy = deepcopy(state)
    _apply_pending_moves(state_copy, phase, unit_defs, territory_defs, faction_defs)
    return state_copy


def _handle_cancel_move(
    state: GameState,
    action: Action,
) -> tuple[GameState, list[GameEvent]]:
    """
    Cancel a pending move by index.
    Removes the move from pending_moves list.
    """
    events: list[GameEvent] = []
    move_index = action.payload.get("move_index", -1)
    
    if move_index < 0 or move_index >= len(state.pending_moves):
        raise ValueError(f"Invalid move index: {move_index}")
    
    # Remove the move at the specified index
    cancelled_move = state.pending_moves.pop(move_index)
    
    # Emit event
    events.append(GameEvent(
        type="move_cancelled",
        payload={
            "from_territory": cancelled_move.from_territory,
            "to_territory": cancelled_move.to_territory,
            "unit_instance_ids": cancelled_move.unit_instance_ids,
        }
    ))
    
    return state, events


def _handle_mobilize_units(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Queue a mobilization: add to pending_mobilizations and deduct from faction_purchased_units.
    Actual deployment to territory happens at end of mobilization phase.
    Destination must be an owned territory with a standing camp (in mobilization_camps).
    """
    events: list[GameEvent] = []
    faction_id = action.faction
    destination_id = action.payload.get("destination")
    units_to_mobilize = list(action.payload.get("units", []))

    if not units_to_mobilize:
        raise ValueError("No units specified to mobilize")

    # Check capital ownership
    if not _faction_owns_capital(state, faction_id, faction_defs):
        raise ValueError(f"Cannot mobilize units: {faction_id}'s capital has been captured")

    if destination_id not in state.mobilization_camps:
        raise ValueError(
            f"Cannot mobilize to {destination_id}: must be an owned camp at start of turn"
        )

    dest_territory = state.territories.get(destination_id)
    dest_def = territory_defs.get(destination_id)
    if not dest_territory or not dest_def:
        raise ValueError(f"Territory {destination_id} does not exist")
    if not _territory_has_standing_camp(state, destination_id, camp_defs):
        raise ValueError(f"Territory {destination_id} has no standing camp (camp was destroyed)")

    purchased_units = state.faction_purchased_units.get(faction_id, [])

    for unit_request in units_to_mobilize:
        unit_id = unit_request.get("unit_id")
        count = unit_request.get("count", 0)
        found_count = 0
        for stack in purchased_units:
            if stack.unit_id == unit_id:
                found_count = stack.count
                break
        if found_count < count:
            raise ValueError(
                f"Not enough purchased {unit_id}: have {found_count}, need {count}")

    total_mobilizing = sum(u.get("count", 0) for u in units_to_mobilize)
    # Count units already pending for this destination this phase
    already_pending = sum(
        sum(item.get("count", 0) for item in pm.units)
        for pm in state.pending_mobilizations
        if pm.destination == destination_id
    )
    power_production = dest_def.produces.get("power", 0)
    if already_pending + total_mobilizing > power_production:
        raise ValueError(
            f"Cannot mobilize {total_mobilizing} more units to {destination_id}: "
            f"already {already_pending} pending, territory produces only {power_production} power")

    # Deduct from purchased pool and append to pending_mobilizations
    for unit_request in units_to_mobilize:
        unit_id = unit_request.get("unit_id")
        count = unit_request.get("count", 0)
        for stack in purchased_units:
            if stack.unit_id == unit_id:
                stack.count -= count
                break

    state.faction_purchased_units[faction_id] = [
        s for s in purchased_units if s.count > 0
    ]

    state.pending_mobilizations.append(
        PendingMobilization(destination=destination_id, units=units_to_mobilize)
    )

    return state, events


def _apply_pending_mobilizations(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Apply all pending mobilizations: add units to territories. Called at end of mobilization phase."""
    events: list[GameEvent] = []
    faction_id = state.current_faction

    for pending in state.pending_mobilizations:
        dest_territory = state.territories.get(pending.destination)
        if not dest_territory:
            continue
        mobilized_info = []
        for unit_request in pending.units:
            unit_id = unit_request.get("unit_id")
            count = unit_request.get("count", 0)
            units_to_add = unitstack_to_units(
                UnitStack(unit_id=unit_id, count=count),
                faction_id,
                state,
                unit_defs,
            )
            dest_territory.units.extend(units_to_add)
            for u in units_to_add:
                mobilized_info.append({"unit_id": u.unit_id, "instance_id": u.instance_id})
        if mobilized_info:
            events.append(units_mobilized(faction_id, pending.destination, mobilized_info))

    state.pending_mobilizations = []
    return state, events


def _handle_cancel_mobilization(
    state: GameState,
    action: Action,
) -> tuple[GameState, list[GameEvent]]:
    """Cancel a pending mobilization by index; return units to faction_purchased_units."""
    events: list[GameEvent] = []
    idx = action.payload.get("mobilization_index", -1)
    if idx < 0 or idx >= len(state.pending_mobilizations):
        raise ValueError(f"Invalid mobilization index: {idx}")

    cancelled = state.pending_mobilizations.pop(idx)
    faction_id = state.current_faction
    purchased = state.faction_purchased_units.setdefault(faction_id, [])

    for unit_request in cancelled.units:
        unit_id = unit_request.get("unit_id")
        count = unit_request.get("count", 0)
        found = False
        for stack in purchased:
            if stack.unit_id == unit_id:
                stack.count += count
                found = True
                break
        if not found:
            purchased.append(UnitStack(unit_id=unit_id, count=count))

    return state, events


def _handle_initiate_combat(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Initiate multi-round combat in a contested territory.

    During combat_move, attackers move INTO enemy territory.
    Both attackers and defenders are now in the same territory.

    Validates:
    - Territory exists and is contested (has both attacker and defender units)
    - No active combat already in progress

    Fights round 1 and creates ActiveCombat if both sides have survivors.
    """
    events: list[GameEvent] = []
    attacker_faction = action.payload.get("attacker")
    territory_id = action.payload.get("territory_id")
    dice_rolls = action.payload.get("dice_rolls", {})

    # Validate no active combat
    if state.active_combat is not None:
        raise ValueError("Cannot initiate combat while another combat is active")

    # Get territory
    territory = state.territories.get(territory_id)
    if not territory:
        raise ValueError(f"Invalid territory: {territory_id}")

    # Validate territory is not owned by attacker (they're attacking it)
    if territory.owner == attacker_faction:
        raise ValueError(f"Cannot attack own territory {territory_id}")

    # Normalize multi-HP unit health from unit_defs (fixes legacy/corrupt state where units had 1 HP)
    _normalize_unit_health_for_combat(territory.units, unit_defs)

    # Separate attackers and defenders; sort by instance_id so roll assignment matches API
    attacker_alliance = getattr(faction_defs.get(attacker_faction), "alliance", None)
    attacker_units = []
    defender_units = []
    for unit in territory.units:
        unit_owner = get_unit_faction(unit, unit_defs)
        if unit_owner == attacker_faction:
            attacker_units.append(deepcopy(unit))
        elif unit_owner is not None:
            unit_alliance = getattr(faction_defs.get(unit_owner), "alliance", None)
            if unit_alliance != attacker_alliance:
                defender_units.append(deepcopy(unit))
    attacker_units.sort(key=lambda u: u.instance_id)
    defender_units.sort(key=lambda u: u.instance_id)

    if len(attacker_units) == 0:
        raise ValueError(f"No attacking units in {territory_id}")

    if len(defender_units) == 0:
        raise ValueError(f"No defending units in {territory_id}")

    defender_faction = territory.owner or get_unit_faction(defender_units[0], unit_defs) or "neutral"

    # Get attacker/defender instance IDs for tracking
    attacker_instance_ids = [u.instance_id for u in attacker_units]
    defender_instance_ids = [u.instance_id for u in defender_units]

    # Emit combat started event
    events.append(combat_started(
        territory_id, attacker_faction, attacker_instance_ids,
        defender_faction, defender_instance_ids
    ))

    # Terrain + anti-cavalry + captain bonuses (merged; recomputed every round)
    territory_def = territory_defs.get(territory_id)
    terrain_att, terrain_def = compute_terrain_stat_modifiers(
        territory_def, attacker_units, defender_units, unit_defs
    )
    anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    captain_att, captain_def = compute_captain_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    # Check if defender has archers -> run prefire before round 1 (archetype "archer" or "archer" in tags)
    def _is_archer(unit_def) -> bool:
        if not unit_def:
            return False
        if getattr(unit_def, "archetype", "") == ARCHETYPE_ARCHER:
            return True
        return "archer" in getattr(unit_def, "tags", []) or []
    defender_archer_units = [
        u for u in defender_units
        if _is_archer(unit_defs.get(u.unit_id))
    ]
    if defender_archer_units:
        # Prefire: only defender archers roll (at defense-1); hits applied to attackers only
        prefire_defender_rolls = dice_rolls.get("defender", [])
        round_result = resolve_archer_prefire(
            attacker_units, defender_archer_units, unit_defs, prefire_defender_rolls,
            stat_modifiers_defender_extra=defender_mods,
        )
        # Group defender dice for UI (archers at defense-1, merged with terrain)
        archer_stat_modifiers = {
            u.instance_id: -1 + defender_mods.get(u.instance_id, 0)
            for u in defender_archer_units
        }
        defender_dice_grouped = group_dice_by_stat(
            defender_archer_units, prefire_defender_rolls, unit_defs, is_attacker=False,
            stat_modifiers=archer_stat_modifiers,
        )
        prefire_log_entry = CombatRoundResult(
            round_number=0,
            attacker_rolls=[],
            defender_rolls=prefire_defender_rolls,
            attacker_hits=0,
            defender_hits=round_result.defender_hits,
            attacker_casualties=round_result.attacker_casualties,
            defender_casualties=[],
            attackers_remaining=len(round_result.surviving_attacker_ids),
            defenders_remaining=len(defender_units),  # no defender casualties in prefire
            is_archer_prefire=True,
        )
        # Units at start of round for frontend (prefire: no attackers rolling; defenders = archers only)
        attacker_units_at_start_prefire: list[dict] = []
        defender_units_at_start_prefire = [
            _build_round_unit_display(
                u,
                unit_defs.get(u.unit_id),
                -1 + defender_mods.get(u.instance_id, 0),
                False,
                defender_faction,
                territory_def,
                terrain_def,
                captain_def,
                anticav_def,
            )
            for u in defender_archer_units
        ]
        events.append(combat_round_resolved(
            territory_id, 0,
            {}, defender_dice_grouped,
            0, round_result.defender_hits,
            round_result.attacker_casualties, [],
            round_result.attacker_wounded, [],
            len(round_result.surviving_attacker_ids), len(defender_units),
            attacker_units_at_start_prefire,
            defender_units_at_start_prefire,
            is_archer_prefire=True,
        ))
        for casualty_id in round_result.attacker_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, attacker_faction, territory_id, "combat"))
        _remove_casualties(territory, round_result.attacker_casualties)
        _sync_survivor_health(territory, attacker_units, defender_units)

        if round_result.attackers_eliminated:
            # All attackers dead from prefire; defender wins, no round 1
            end_round_result = RoundResult(
                attacker_hits=0,
                defender_hits=round_result.defender_hits,
                attacker_casualties=round_result.attacker_casualties,
                defender_casualties=[],
                attacker_wounded=[],
                defender_wounded=[],
                surviving_attacker_ids=[],
                surviving_defender_ids=defender_instance_ids,
                attackers_eliminated=True,
                defenders_eliminated=False,
            )
            state, end_events = _resolve_combat_end(
                state, attacker_faction, territory_id,
                end_round_result, [prefire_log_entry], territory_defs, unit_defs,
            )
            events.extend(end_events)
            return state, events

        # Combat continues; create active combat with round 0 (round 1 not yet run)
        state.active_combat = ActiveCombat(
            attacker_faction=attacker_faction,
            territory_id=territory_id,
            attacker_instance_ids=round_result.surviving_attacker_ids,
            round_number=0,
            combat_log=[prefire_log_entry],
            attackers_have_rolled=False,
        )
        return state, events

    # No archers: run round 1 as usual (with terrain modifiers)
    attacker_dice_grouped = group_dice_by_stat(
        attacker_units, dice_rolls.get("attacker", []), unit_defs, is_attacker=True,
        stat_modifiers=attacker_mods or None,
    )
    defender_dice_grouped = group_dice_by_stat(
        defender_units, dice_rolls.get("defender", []), unit_defs, is_attacker=False,
        stat_modifiers=defender_mods or None,
    )

    # Build instance_id -> (unit_id, base_health) before combat modifies units (for hit badges)
    attacker_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in attacker_units}
    defender_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in defender_units}

    # Units at start of round for frontend (before combat modifies anything)
    attacker_units_at_start_init = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            attacker_mods.get(u.instance_id, 0), True, attacker_faction,
            territory_def, terrain_att, captain_att, anticav_att,
        )
        for u in attacker_units
    ]
    defender_units_at_start_init = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            defender_mods.get(u.instance_id, 0), False, defender_faction,
            territory_def, terrain_def, captain_def, anticav_def,
        )
        for u in defender_units
    ]

    round_result = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
        defender_hits_override=action.payload.get("terror_final_defender_hits"),
    )

    # Hits per unit type this round (for UI hit badges): casualties add base_health each, wounded add 1
    def _hits_by_unit_type(casualties: list[str], wounded: list[str], id_map: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for iid in casualties:
            tup = id_map.get(iid)
            if tup:
                uid, health = tup
                out[uid] = out.get(uid, 0) + health
        for iid in wounded:
            tup = id_map.get(iid)
            if tup:
                uid = tup[0]
                out[uid] = out.get(uid, 0) + 1
        return out

    attacker_hits_by_type = _hits_by_unit_type(
        round_result.attacker_casualties, round_result.attacker_wounded, attacker_id_to_type_health
    )
    defender_hits_by_type = _hits_by_unit_type(
        round_result.defender_casualties, round_result.defender_wounded, defender_id_to_type_health
    )

    combat_log_entry = CombatRoundResult(
        round_number=1,
        attacker_rolls=dice_rolls.get("attacker", []),
        defender_rolls=dice_rolls.get("defender", []),
        attacker_hits=round_result.attacker_hits,
        defender_hits=round_result.defender_hits,
        attacker_casualties=round_result.attacker_casualties,
        defender_casualties=round_result.defender_casualties,
        attackers_remaining=len(round_result.surviving_attacker_ids),
        defenders_remaining=len(round_result.surviving_defender_ids),
    )

    events.append(combat_round_resolved(
        territory_id, 1,
        attacker_dice_grouped, defender_dice_grouped,
        round_result.attacker_hits, round_result.defender_hits,
        round_result.attacker_casualties, round_result.defender_casualties,
        round_result.attacker_wounded, round_result.defender_wounded,
        len(round_result.surviving_attacker_ids), len(round_result.surviving_defender_ids),
        attacker_units_at_start_init,
        defender_units_at_start_init,
        attacker_hits_by_unit_type=attacker_hits_by_type,
        defender_hits_by_unit_type=defender_hits_by_type,
        terror_applied=action.payload.get("terror_applied", False),
    ))

    for casualty_id in round_result.attacker_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, attacker_faction, territory_id, "combat"))
    for casualty_id in round_result.defender_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, defender_faction, territory_id, "combat"))

    _remove_casualties(territory, round_result.attacker_casualties)
    _remove_casualties(territory, round_result.defender_casualties)
    _sync_survivor_health(territory, attacker_units, defender_units)

    if round_result.attackers_eliminated or round_result.defenders_eliminated:
        state, end_events = _resolve_combat_end(
            state, attacker_faction, territory_id,
            round_result, [combat_log_entry], territory_defs, unit_defs,
        )
        events.extend(end_events)
        return state, events

    state.active_combat = ActiveCombat(
        attacker_faction=attacker_faction,
        territory_id=territory_id,
        attacker_instance_ids=round_result.surviving_attacker_ids,
        round_number=1,
        combat_log=[combat_log_entry],
    )
    return state, events


def _handle_continue_combat(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Continue an active combat with another round.
    Both attackers and defenders are in the same contested territory.
    """
    events: list[GameEvent] = []

    if state.active_combat is None:
        raise ValueError("No active combat to continue")

    dice_rolls = action.payload.get("dice_rolls", {})
    combat = state.active_combat

    # Get the contested territory
    territory = state.territories[combat.territory_id]

    # Normalize multi-HP unit health from unit_defs (fixes legacy/corrupt state)
    _normalize_unit_health_for_combat(territory.units, unit_defs)

    # Separate attackers and defenders; sort by instance_id so roll assignment matches API
    surviving_attacker_ids = set(combat.attacker_instance_ids)
    attacker_units = sorted(
        [deepcopy(u) for u in territory.units if u.instance_id in surviving_attacker_ids],
        key=lambda u: u.instance_id,
    )
    defender_units = sorted(
        [deepcopy(u) for u in territory.units if u.instance_id not in surviving_attacker_ids],
        key=lambda u: u.instance_id,
    )

    defender_faction = territory.owner or (get_unit_faction(defender_units[0], unit_defs) if defender_units else "neutral")

    # Terrain + anti-cavalry + captain bonuses (merged; recomputed every round)
    territory_def = territory_defs.get(combat.territory_id)
    terrain_att, terrain_def = compute_terrain_stat_modifiers(
        territory_def, attacker_units, defender_units, unit_defs
    )
    anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    captain_att, captain_def = compute_captain_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    # Compute grouped dice BEFORE combat (units get modified during resolution)
    attacker_dice_grouped = group_dice_by_stat(
        attacker_units, dice_rolls.get("attacker", []), unit_defs, is_attacker=True,
        stat_modifiers=attacker_mods or None,
    )
    defender_dice_grouped = group_dice_by_stat(
        defender_units, dice_rolls.get("defender", []), unit_defs, is_attacker=False,
        stat_modifiers=defender_mods or None,
    )

    # Build instance_id -> (unit_id, base_health) before combat modifies units
    attacker_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in attacker_units}
    defender_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in defender_units}

    # Units at start of round for frontend (before combat modifies anything)
    attacker_units_at_start = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            attacker_mods.get(u.instance_id, 0), True, combat.attacker_faction,
            territory_def, terrain_att, captain_att, anticav_att,
        )
        for u in attacker_units
    ]
    defender_units_at_start = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            defender_mods.get(u.instance_id, 0), False, defender_faction,
            territory_def, terrain_def, captain_def, anticav_def,
        )
        for u in defender_units
    ]

    # Fight this round (with terrain modifiers)
    round_result = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
        defender_hits_override=action.payload.get("terror_final_defender_hits"),
    )

    # Hits per unit type this round (for UI hit badges): casualties add base_health each, wounded add 1
    def hits_by_unit_type(casualties: list[str], wounded: list[str], id_map: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for iid in casualties:
            tup = id_map.get(iid)
            if tup:
                uid, health = tup
                out[uid] = out.get(uid, 0) + health
        for iid in wounded:
            tup = id_map.get(iid)
            if tup:
                uid = tup[0]
                out[uid] = out.get(uid, 0) + 1
        return out

    attacker_hits_by_type = hits_by_unit_type(
        round_result.attacker_casualties, round_result.attacker_wounded, attacker_id_to_type_health
    )
    defender_hits_by_type = hits_by_unit_type(
        round_result.defender_casualties, round_result.defender_wounded, defender_id_to_type_health
    )

    # Create combat log entry
    new_round_number = combat.round_number + 1
    combat_log_entry = CombatRoundResult(
        round_number=new_round_number,
        attacker_rolls=dice_rolls.get("attacker", []),
        defender_rolls=dice_rolls.get("defender", []),
        attacker_hits=round_result.attacker_hits,
        defender_hits=round_result.defender_hits,
        attacker_casualties=round_result.attacker_casualties,
        defender_casualties=round_result.defender_casualties,
        attackers_remaining=len(round_result.surviving_attacker_ids),
        defenders_remaining=len(round_result.surviving_defender_ids),
    )

    # Emit round resolved event with full payload for UI (dice, hits, casualties, units at start)
    events.append(combat_round_resolved(
        combat.territory_id, new_round_number,
        attacker_dice_grouped, defender_dice_grouped,
        round_result.attacker_hits, round_result.defender_hits,
        round_result.attacker_casualties, round_result.defender_casualties,
        round_result.attacker_wounded, round_result.defender_wounded,
        len(round_result.surviving_attacker_ids), len(round_result.surviving_defender_ids),
        attacker_units_at_start,
        defender_units_at_start,
        attacker_hits_by_unit_type=attacker_hits_by_type,
        defender_hits_by_unit_type=defender_hits_by_type,
        terror_applied=action.payload.get("terror_applied", False) if new_round_number == 1 else False,
    ))

    # Emit unit destroyed events for casualties
    for casualty_id in round_result.attacker_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
    for casualty_id in round_result.defender_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, defender_faction, combat.territory_id, "combat"))

    # Remove casualties from the territory (both attackers and defenders are here)
    _remove_casualties(territory, round_result.attacker_casualties)
    _remove_casualties(territory, round_result.defender_casualties)
    # Sync surviving units' remaining_health so multi-HP units carry damage across rounds
    _sync_survivor_health(territory, attacker_units, defender_units)

    # Update combat log
    combat.combat_log.append(combat_log_entry)
    combat.round_number = new_round_number
    combat.attacker_instance_ids = round_result.surviving_attacker_ids
    combat.attackers_have_rolled = True  # round 1 (or later) has been run

    # Check for combat end conditions
    if round_result.attackers_eliminated or round_result.defenders_eliminated:
        # Combat ended
        state, end_events = _resolve_combat_end(
            state,
            combat.attacker_faction,
            combat.territory_id,
            round_result,
            combat.combat_log,
            territory_defs,
            unit_defs,
        )
        events.extend(end_events)
        state.active_combat = None
        return state, events

    # Combat continues - attacker must decide next action
    return state, events


def _handle_retreat(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Retreat from an active combat.
    Surviving attackers move to an adjacent allied territory.
    """
    events: list[GameEvent] = []

    if state.active_combat is None:
        raise ValueError("No active combat to retreat from")

    combat = state.active_combat
    if not combat.attackers_have_rolled:
        raise ValueError("Cannot retreat until attackers have rolled (after archer prefire, click Continue first)")

    retreat_to = action.payload.get("retreat_to")
    if not retreat_to:
        raise ValueError("Must specify retreat_to territory")

    retreat_territory = state.territories.get(retreat_to)
    if not retreat_territory:
        raise ValueError(f"Invalid retreat territory: {retreat_to}")

    if not _territory_is_friendly_for_retreat(retreat_territory, combat.attacker_faction, faction_defs, unit_defs):
        raise ValueError(
            f"Cannot retreat to {retreat_to} - must be allied territory")

    # Must be adjacent to the contested territory
    combat_territory_def = territory_defs.get(combat.territory_id)
    if combat_territory_def and retreat_to not in combat_territory_def.adjacent:
        raise ValueError(
            f"Cannot retreat to {retreat_to} - not adjacent to {combat.territory_id}")

    # Move surviving attackers from contested territory to retreat territory
    combat_territory = state.territories[combat.territory_id]
    surviving_ids = set(combat.attacker_instance_ids)

    units_to_move = [
        u for u in combat_territory.units
        if u.instance_id in surviving_ids
    ]

    # Remove attackers from contested territory
    combat_territory.units = [
        u for u in combat_territory.units
        if u.instance_id not in surviving_ids
    ]

    # Add to retreat territory
    retreat_territory.units.extend(units_to_move)

    # Emit retreat event
    events.append(units_retreated(
        combat.attacker_faction,
        combat.territory_id,
        retreat_to,
        list(surviving_ids),
    ))

    # Emit combat ended event (defender wins by default on retreat)
    territory = state.territories[combat.territory_id]
    defender_ids = [u.instance_id for u in territory.units]
    events.append(combat_ended(
        combat.territory_id,
        "defender",
        combat.attacker_faction,
        territory.owner,
        [],  # No surviving attackers in territory
        defender_ids,
        combat.round_number,
    ))

    # Clear active combat
    state.active_combat = None

    return state, events


def _remove_casualties(territory: TerritoryState, casualty_ids: list[str]) -> None:
    """Remove units with the given instance_ids from a territory."""
    casualty_set = set(casualty_ids)
    territory.units = [u for u in territory.units if u.instance_id not in casualty_set]


def _sync_survivor_health(
    territory: TerritoryState,
    attacker_units: list[Unit],
    defender_units: list[Unit],
) -> None:
    """
    Sync remaining_health from combat-round copies back to territory.units.
    Combat modifies deepcopies; survivors' remaining_health must be written back
    so multi-HP units (e.g. trolls) carry damage across rounds.
    """
    survivor_health = {u.instance_id: u.remaining_health for u in attacker_units + defender_units}
    for unit in territory.units:
        if unit.instance_id in survivor_health:
            unit.remaining_health = survivor_health[unit.instance_id]


def _resolve_combat_end(
    state: GameState,
    attacker_faction: str,
    territory_id: str,
    round_result: RoundResult,
    combat_log: list[CombatRoundResult],
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict[str, UnitDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Resolve the end of combat.
    Both attackers and defenders are in the same contested territory.
    - If defenders eliminated AND at least one attacker survived: territory captured by attacker
      (only if surviving attackers include at least one ground unit; aerial-only cannot conquer)
    - If attackers eliminated OR both sides eliminated: defender keeps territory (no conquest)
    """
    events: list[GameEvent] = []
    territory = state.territories[territory_id]
    old_owner = territory.owner
    total_rounds = len(combat_log)

    # Attacker only wins if defenders are gone AND at least one attacker survived
    if round_result.defenders_eliminated and not round_result.attackers_eliminated:
        # Conquest requires a living ground unit by conclusion of battle; aerial-only cannot conquer
        surviving_attacker_ids_set = set(round_result.surviving_attacker_ids)
        surviving_attacker_units = [
            u for u in territory.units
            if u.instance_id in surviving_attacker_ids_set
        ]
        # Only consider units actually present in territory (after casualties removed)
        has_living_ground_attacker = any(
            is_ground_unit(unit_defs.get(u.unit_id))
            for u in surviving_attacker_units
        )
        territory_def = territory_defs.get(territory_id)
        if (
            has_living_ground_attacker
            and territory_def
            and territory_def.ownable
        ):
            # Conquer ownable territory (enemy-owned or neutral) when attacker wins
            state.pending_captures[territory_id] = attacker_faction
        else:
            # Attackers won but only aerial survived (or other reason not to conquer): ensure we don't capture
            state.pending_captures.pop(territory_id, None)

        events.append(combat_ended(
            territory_id,
            "attacker",
            attacker_faction,
            old_owner,
            round_result.surviving_attacker_ids,
            [],
            total_rounds,
        ))
    else:
        # Defenders win: attacker eliminated, or mutual annihilation (no conquest)
        events.append(combat_ended(
            territory_id,
            "defender",
            attacker_faction,
            old_owner,
            [],
            round_result.surviving_defender_ids,
            total_rounds,
        ))

    # Clear active combat
    state.active_combat = None

    return state, events


def _handle_end_phase(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    End the current phase and advance to the next.
    Phase order: purchase -> combat_move -> combat -> non_combat_move -> mobilization

    At end of combat_move phase, apply all pending combat moves.
    At end of non_combat_move phase, apply all pending non-combat moves and
    reset all units' remaining_movement and remaining_health to their base values.

    After mobilization phase, automatically ends the turn (switches to next faction).

    Cannot end combat phase while there is an active combat.
    """
    events: list[GameEvent] = []

    # Cannot end combat phase while combat is active
    if state.phase == "combat" and state.active_combat is not None:
        raise ValueError(
            "Cannot end combat phase while combat is active. "
            "Must continue_combat or retreat first."
        )

    old_phase = state.phase

    # If ending combat_move phase, apply all pending combat moves
    if state.phase == "combat_move":
        state, move_events = _apply_pending_moves(
            state, "combat_move", unit_defs, territory_defs, faction_defs
        )
        events.extend(move_events)

    # If ending non_combat_move phase: validate using state AFTER pending moves (same as API can_end_phase)
    if state.phase == "non_combat_move":
        state_after_moves = get_state_after_pending_moves(
            state, "non_combat_move", unit_defs, territory_defs, faction_defs
        )
        aerial_must_move = get_aerial_units_must_move(
            state_after_moves, unit_defs, territory_defs, faction_defs, state.current_faction
        )
        if aerial_must_move:
            instance_ids = [u["instance_id"] for u in aerial_must_move]
            raise ValueError(
                "Aerial units must move to friendly territory before ending phase: "
                f"{instance_ids!s}. Move all aerial units out of enemy territory first."
            )
        state, move_events = _apply_pending_moves(
            state, "non_combat_move", unit_defs, territory_defs, faction_defs
        )
        events.extend(move_events)

    # If ending combat phase, apply all pending territory captures
    if state.phase == "combat":
        for territory_id, capturer in state.pending_captures.items():
            territory = state.territories[territory_id]
            old_owner = territory.owner
            
            # Liberation check: if original_owner exists and is allied with capturer,
            # restore to original owner instead of capturer
            new_owner = capturer
            original_owner = territory.original_owner
            
            if original_owner and original_owner != capturer:
                capturer_def = faction_defs.get(capturer)
                original_def = faction_defs.get(original_owner)
                
                if capturer_def and original_def:
                    if capturer_def.alliance == original_def.alliance:
                        # Liberation! Restore to original owner
                        new_owner = original_owner
            
            territory.owner = new_owner

            # Destroy any camp in this territory (camps are destroyed when territory is captured/liberated)
            def _camp_in_territory(cid: str) -> bool:
                if state.dynamic_camps.get(cid) == territory_id:
                    return True
                camp = camp_defs.get(cid) if camp_defs else None
                return camp is not None and camp.territory_id == territory_id

            state.camps_standing = [cid for cid in state.camps_standing if not _camp_in_territory(cid)]
            state.dynamic_camps = {cid: tid for cid, tid in state.dynamic_camps.items() if tid != territory_id}
            
            # Get surviving attacker unit IDs in this territory
            surviving_attacker_ids = [
                u.instance_id for u in territory.units
            ]
            
            events.append(territory_captured(
                territory_id,
                old_owner,
                new_owner,
                surviving_attacker_ids,
            ))
        
        # Clear pending captures
        state.pending_captures = {}

    # Only reset remaining_health (and movement) when leaving non_combat_move — never between combat rounds.
    # Combat damage must persist across rounds until the combat phase is over.
    if state.phase == "non_combat_move":
        _reset_unit_stats_for_faction(state, state.current_faction, unit_defs)

    phase_order = [
        "purchase",
        "combat_move",
        "combat",
        "non_combat_move",
        "mobilization",
    ]

    current_idx = phase_order.index(
        state.phase) if state.phase in phase_order else 0
    
    # After mobilization, apply pending camp placements then pending mobilizations then end the turn
    if state.phase == "mobilization":
        state, camp_events = _apply_pending_camp_placements(state, camp_defs or {})
        events.extend(camp_events)
        state, mobilize_events = _apply_pending_mobilizations(
            state, unit_defs, territory_defs, faction_defs
        )
        events.extend(mobilize_events)
        events.append(phase_changed(old_phase, "turn_end", state.current_faction))
        state, turn_events = _handle_end_turn(
            state, territory_defs, faction_defs, camp_defs or {}
        )
        events.extend(turn_events)
        return state, events
    
    next_idx = current_idx + 1
    state.phase = phase_order[next_idx]

    # When entering combat_move, ensure all current-faction units have full movement
    # (including units in neutral/unownable territories like Dagorlad that may have
    # been missed by the end-of-non_combat_move reset in edge cases or loaded state).
    if state.phase == "combat_move":
        _reset_unit_stats_for_faction(state, state.current_faction, unit_defs)

    # Emit phase changed event
    events.append(phase_changed(old_phase, state.phase, state.current_faction))

    return state, events


def _reset_unit_stats_for_faction(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
) -> None:
    """
    Reset remaining_movement and remaining_health to base values for all units
    that belong to the specified faction, regardless of territory. Ensures
    units in neutral/unownable territories (e.g. Dagorlad) also get movement
    back. Only called when ending non_combat_move phase — never during or
    between combat rounds.
    """
    for territory in state.territories.values():
        for unit in territory.units:
            unit_faction = get_unit_faction(unit, unit_defs)
            if unit_faction is None and unit.instance_id.startswith(faction_id + "_"):
                unit_faction = faction_id
            if unit_faction == faction_id:
                unit.remaining_movement = unit.base_movement
                unit.remaining_health = unit.base_health


def _check_victory(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[str, dict[str, int], list[str]] | None:
    """
    Check if any alliance has achieved victory by controlling enough strongholds.

    Returns:
        None if no victory, or tuple of:
        (winner_alliance, {alliance: stronghold_count}, [controlled_stronghold_ids])
    """
    # Count strongholds controlled by each alliance
    stronghold_counts: dict[str, int] = {}
    controlled_by_alliance: dict[str, list[str]] = {}

    for territory_id, territory_state in state.territories.items():
        territory_def = territory_defs.get(territory_id)
        if not territory_def or not territory_def.is_stronghold:
            continue

        owner = territory_state.owner
        if not owner:
            continue  # Unowned strongholds don't count

        faction_def = faction_defs.get(owner)
        if not faction_def:
            continue

        alliance = faction_def.alliance
        stronghold_counts[alliance] = stronghold_counts.get(alliance, 0) + 1

        if alliance not in controlled_by_alliance:
            controlled_by_alliance[alliance] = []
        controlled_by_alliance[alliance].append(territory_id)

    # Check if any alliance meets victory threshold (from victory_criteria.strongholds)
    strongholds_criteria = state.victory_criteria.get("strongholds") or {}
    for alliance, count in stronghold_counts.items():
        required = int(strongholds_criteria.get(alliance, 0)) if isinstance(
            strongholds_criteria, dict
        ) else 0
        if required > 0 and count >= required:
            return (alliance, stronghold_counts, controlled_by_alliance[alliance])

    return None


def _handle_end_turn(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    End the current turn and advance to the next faction.

    At end of turn:
    - Clears purchased units pool (unspent purchases are lost)
    - Calculates and stores pending income based on currently owned territories

    At start of next faction's turn:
    - Applies any pending income they have stored from their previous turn
    """
    events: list[GameEvent] = []
    old_faction = state.current_faction

    # Clear purchased units for this faction (they must be mobilized before end of turn)
    state.faction_purchased_units[state.current_faction] = []

    # Calculate and store pending income for the ending faction
    # Only if they still own their capital - if capital captured, no income
    if _faction_owns_capital(state, old_faction, faction_defs):
        pending_income: dict[str, int] = {}
        contributing_territories: list[str] = []

        for territory_id, territory_state in state.territories.items():
            if territory_state.owner != old_faction:
                continue

            territory_def = territory_defs.get(territory_id)
            if not territory_def:
                continue

            # Add production from this territory
            for resource_id, amount in territory_def.produces.items():
                if resource_id not in pending_income:
                    pending_income[resource_id] = 0
                pending_income[resource_id] += amount

            if territory_def.produces:
                contributing_territories.append(territory_id)

        # Store the pending income for collection at their next turn start
        state.faction_pending_income[old_faction] = pending_income

        # Emit income calculated event
        if pending_income:
            events.append(income_calculated(old_faction, pending_income, contributing_territories))
    else:
        # Capital captured - no income
        state.faction_pending_income[old_faction] = {}

    # Emit turn ended event
    events.append(turn_ended(state.turn_number, old_faction))

    # Determine next faction (use state.turn_order from setup if set, else alphabetical)
    faction_ids = state.turn_order if state.turn_order else sorted(faction_defs.keys())
    current_idx = faction_ids.index(
        state.current_faction) if state.current_faction in faction_ids else 0
    next_idx = (current_idx + 1) % len(faction_ids)

    state.current_faction = faction_ids[next_idx]
    state.phase = "purchase"  # Reset to purchase phase for new faction

    # If we've cycled back to the first faction, check victory and increment turn
    if next_idx == 0:
        # Check for victory before incrementing turn
        victory_result = _check_victory(state, territory_defs, faction_defs)
        if victory_result:
            winner_alliance, stronghold_counts, controlled = victory_result
            state.winner = winner_alliance
            strongholds_criteria = state.victory_criteria.get("strongholds") or {}
            strongholds_required = int(strongholds_criteria.get(winner_alliance, 0)) if isinstance(
                strongholds_criteria, dict
            ) else 0
            events.append(victory(
                winner_alliance,
                stronghold_counts,
                strongholds_required,
                controlled,
            ))

        state.turn_number += 1

    # Apply pending income for the new current faction (from their previous turn end)
    new_faction = state.current_faction
    if new_faction in state.faction_pending_income:
        faction_income = state.faction_pending_income[new_faction]
        if faction_income:
            # Ensure faction has a resources dict
            if new_faction not in state.faction_resources:
                state.faction_resources[new_faction] = {}

            new_totals = {}
            for resource_id, amount in faction_income.items():
                if resource_id not in state.faction_resources[new_faction]:
                    state.faction_resources[new_faction][resource_id] = 0
                state.faction_resources[new_faction][resource_id] += amount
                new_totals[resource_id] = state.faction_resources[new_faction][resource_id]

            # Emit income collected event
            events.append(income_collected(new_faction, faction_income, new_totals))

        # Clear the pending income (it's been collected)
        state.faction_pending_income[new_faction] = {}

    # Snapshot territories owned by new faction at turn start (for camp placement options this turn)
    state.faction_territories_at_turn_start[new_faction] = [
        tid for tid, ts in state.territories.items() if ts.owner == new_faction
    ]
    state.pending_camps = []

    # Calculate mobilization territories for the new faction (owned territories with a standing camp at turn start)
    camp_defs = camp_defs or {}
    state.mobilization_camps = [
        tid for tid, ts in state.territories.items()
        if ts.owner == new_faction and _territory_has_standing_camp(state, tid, camp_defs)
    ]

    # Emit turn started event
    events.append(turn_started(state.turn_number, state.current_faction))

    return state, events


def replay_from_actions(
    initial_state: GameState,
    actions: list[Action],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    Replay a series of actions from an initial state.
    Event sourcing: state is derived from action log.

    Args:
        initial_state: Starting game state
        actions: List of actions to apply in sequence
        unit_defs: Unit definitions
        territory_defs: Territory definitions
        faction_defs: Faction definitions
        camp_defs: Camp definitions (optional)

    Returns:
        Tuple of (final_state, all_events) after all actions applied
    """
    camp_defs = camp_defs or {}
    current_state = initial_state.copy()
    all_events: list[GameEvent] = []

    for action in actions:
        current_state, events = apply_action(
            current_state,
            action,
            unit_defs,
            territory_defs,
            faction_defs,
            camp_defs,
        )
        all_events.extend(events)

    return current_state, all_events
