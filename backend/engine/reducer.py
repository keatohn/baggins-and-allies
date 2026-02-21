"""
Main game reducer.
Applies actions to state, enforcing rules and producing new state.
Returns (new_state, events) where events describe what happened.
"""

from copy import deepcopy
from backend.engine.state import GameState, UnitStack, TerritoryState, Unit, ActiveCombat, CombatRoundResult, PendingMove, PendingMobilization
from backend.engine.actions import Action
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition, CampDefinition
from backend.engine.combat import (
    resolve_combat_round,
    resolve_archer_prefire,
    RoundResult,
    group_dice_by_stat,
    ARCHETYPE_ARCHER,
    calculate_required_dice,
    compute_terrain_stat_modifiers,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    merge_stat_modifiers,
)
from backend.engine.movement import get_reachable_territories_for_unit, calculate_movement_cost
from backend.engine.queries import _territory_is_friendly_for_retreat
from backend.engine.utils import unitstack_to_units
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
    "mobilization": ["mobilize_units", "place_camp", "cancel_mobilization", "end_phase", "end_turn"],
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
            new_state, action, camp_defs or {})
        events.extend(evts)

    elif action.type == "place_camp":
        new_state, evts = _handle_place_camp(new_state, action, camp_defs or {})
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
            new_state, action, unit_defs, territory_defs)
        events.extend(evts)

    elif action.type == "continue_combat":
        new_state, evts = _handle_continue_combat(
            new_state, action, unit_defs, territory_defs)
        events.extend(evts)

    elif action.type == "retreat":
        new_state, evts = _handle_retreat(new_state, action, territory_defs, faction_defs)
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
) -> tuple[GameState, list[GameEvent]]:
    """Purchase a camp. Deduct camp_cost (power); add to pending_camps with territory_options."""
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

    # Territories owned at turn start that don't have a camp (and not already chosen by a pending camp)
    owned_at_start = state.faction_territories_at_turn_start.get(faction_id, [])
    already_placed = [
        p.get("placed_territory_id") for p in state.pending_camps
        if p.get("placed_territory_id")
    ]
    territory_options = [
        tid for tid in owned_at_start
        if not _territory_has_standing_camp(state, tid, camp_defs)
        and tid not in already_placed
    ]

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
        # Validate unit belongs to the faction (instance_id format: faction_unittype_number)
        unit_owner = instance_id.split("_")[0]
        if unit_owner != faction_id:
            raise ValueError(f"Unit {instance_id} does not belong to {faction_id}")
        units_to_move.append(unit)

    charge_through = action.payload.get("charge_through")
    if charge_through is not None and not isinstance(charge_through, list):
        charge_through = []
    charge_through = [str(t) for t in charge_through] if charge_through else []

    first_unit_charge_routes: dict[str, list[list[str]]] = {}
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
        if not first_unit_charge_routes:
            first_unit_charge_routes = charge_routes

        if to_id not in reachable:
            raise ValueError(
                f"Unit {unit.instance_id} cannot reach {to_id} from {from_id} "
                f"(remaining_movement={unit.remaining_movement}, phase={state.phase})"
            )

    if charge_through:
        valid_routes = first_unit_charge_routes.get(to_id, [])
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
    
    for pending_move in moves_to_apply:
        from_id = pending_move.from_territory
        to_id = pending_move.to_territory
        unit_instance_ids = pending_move.unit_instance_ids
        charge_through = getattr(pending_move, "charge_through", None) or []

        from_territory = state.territories.get(from_id)
        to_territory = state.territories.get(to_id)

        if not from_territory or not to_territory:
            continue  # Skip invalid moves

        # Cavalry charging: conquer empty enemy territories we passed through (before moving units)
        if unit_instance_ids:
            faction_id = unit_instance_ids[0].split("_")[0]
            for tid in charge_through:
                t = state.territories.get(tid)
                tdef = territory_defs.get(tid)
                if t and tdef and getattr(tdef, "ownable", True) and t.owner and t.owner != faction_id:
                    state.pending_captures[tid] = faction_id

        # Calculate the movement cost (distance) to destination
        distance = calculate_movement_cost(from_id, to_id, territory_defs)
        if distance is None:
            continue  # Skip if no path
        
        # Build lookup of units in source
        units_by_id = {unit.instance_id: unit for unit in from_territory.units}
        
        # Move each unit
        for instance_id in unit_instance_ids:
            unit = units_by_id.get(instance_id)
            if unit:
                from_territory.units.remove(unit)
                unit.remaining_movement -= distance
                to_territory.units.append(unit)
        
        # Get faction from first unit (all units in a move belong to same faction)
        if unit_instance_ids:
            faction_id = unit_instance_ids[0].split("_")[0]
            
            # Check if this is combat_move into an undefended enemy territory
            if phase == "combat_move":
                to_owner = to_territory.owner
                to_def = territory_defs.get(to_id)
                
                # Check if territory is enemy-owned and ownable
                if to_owner and to_owner != faction_id and to_def and to_def.ownable:
                    # Get the moving faction's alliance
                    moving_faction_def = faction_defs.get(faction_id)
                    moving_alliance = moving_faction_def.alliance if moving_faction_def else ""
                    
                    owner_def = faction_defs.get(to_owner)
                    owner_alliance = owner_def.alliance if owner_def else ""
                    
                    # If enemy territory (different alliance)
                    if moving_alliance != owner_alliance:
                        # Check if there are any enemy units in the territory
                        enemy_units = [
                            u for u in to_territory.units
                            if u.instance_id.split("_")[0] != faction_id
                        ]
                        
                        # If no enemy units, capture the territory (queue for pending)
                        if not enemy_units:
                            state.pending_captures[to_id] = faction_id
    
    return state, events


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
    power_production = dest_def.produces.get("power", 0)
    if total_mobilizing > power_production:
        raise ValueError(
            f"Cannot mobilize {total_mobilizing} units (territory produces only {power_production} power)")

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

    defender_faction = territory.owner

    # Validate territory is not owned by attacker (they're attacking it)
    if territory.owner == attacker_faction:
        raise ValueError(f"Cannot attack own territory {territory_id}")

    # Separate attackers and defenders in the same territory
    # Attackers = units owned by current faction
    # Defenders = units owned by territory owner
    attacker_units = []
    defender_units = []

    for unit in territory.units:
        # Determine unit owner from instance_id (format: faction_unittype_number)
        unit_owner = unit.instance_id.split("_")[0]
        if unit_owner == attacker_faction:
            attacker_units.append(deepcopy(unit))
        elif unit_owner == territory.owner:
            defender_units.append(deepcopy(unit))

    if len(attacker_units) == 0:
        raise ValueError(f"No attacking units in {territory_id}")

    if len(defender_units) == 0:
        raise ValueError(f"No defending units in {territory_id}")

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

    # Check if defender has archers -> run prefire before round 1
    defender_archer_units = [
        u for u in defender_units
        if unit_defs.get(u.unit_id) and getattr(unit_defs[u.unit_id], "archetype", "") == ARCHETYPE_ARCHER
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
        events.append(combat_round_resolved(
            territory_id, 0,
            {}, defender_dice_grouped,
            0, round_result.defender_hits,
            round_result.attacker_casualties, [],
            round_result.attacker_wounded, [],
            len(round_result.surviving_attacker_ids), len(defender_units),
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
                end_round_result, [prefire_log_entry], territory_defs,
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

    round_result = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
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
            round_result, [combat_log_entry], territory_defs,
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
    defender_faction = territory.owner

    # Separate attackers and defenders from the same territory
    attacker_units = []
    defender_units = []
    surviving_attacker_ids = set(combat.attacker_instance_ids)

    for unit in territory.units:
        if unit.instance_id in surviving_attacker_ids:
            attacker_units.append(deepcopy(unit))
        else:
            # Must be a defender (owned by territory owner)
            defender_units.append(deepcopy(unit))

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

    # Fight this round (with terrain modifiers)
    round_result = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
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

    # Emit round resolved event with grouped dice and hits-by-type for UI
    events.append(combat_round_resolved(
        combat.territory_id, new_round_number,
        attacker_dice_grouped, defender_dice_grouped,
        round_result.attacker_hits, round_result.defender_hits,
        round_result.attacker_casualties, round_result.defender_casualties,
        round_result.attacker_wounded, round_result.defender_wounded,
        len(round_result.surviving_attacker_ids), len(round_result.surviving_defender_ids),
        attacker_hits_by_unit_type=attacker_hits_by_type,
        defender_hits_by_unit_type=defender_hits_by_type,
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
        )
        events.extend(end_events)
        state.active_combat = None
        return state, events

    # Combat continues - attacker must decide next action
    return state, events


def _handle_retreat(
    state: GameState,
    action: Action,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Retreat from an active combat.
    Surviving attackers move to an adjacent allied or friendly neutral (no enemy units) territory.
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

    if not _territory_is_friendly_for_retreat(retreat_territory, combat.attacker_faction, faction_defs):
        raise ValueError(
            f"Cannot retreat to {retreat_to} - must be allied or friendly neutral (no enemy units)")

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
) -> tuple[GameState, list[GameEvent]]:
    """
    Resolve the end of combat.
    Both attackers and defenders are in the same contested territory.
    - If defenders eliminated AND at least one attacker survived: territory captured by attacker
    - If attackers eliminated OR both sides eliminated: defender keeps territory (no conquest)
    """
    events: list[GameEvent] = []
    territory = state.territories[territory_id]
    old_owner = territory.owner
    total_rounds = len(combat_log)

    # Attacker only wins if defenders are gone AND at least one attacker survived
    if round_result.defenders_eliminated and not round_result.attackers_eliminated:
        # Attackers win - queue ownership transfer for end of combat phase
        territory_def = territory_defs.get(territory_id)
        if (territory.owner is not None and
            territory_def and
                territory_def.ownable):
            state.pending_captures[territory_id] = attacker_faction

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

    # If ending non_combat_move phase, apply all pending non-combat moves
    if state.phase == "non_combat_move":
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

    # If ending non_combat_move phase, reset movement and health for current faction's units
    if state.phase == "non_combat_move":
        _reset_unit_stats_for_faction(state, state.current_faction)

    phase_order = [
        "purchase",
        "combat_move",
        "combat",
        "non_combat_move",
        "mobilization",
    ]

    current_idx = phase_order.index(
        state.phase) if state.phase in phase_order else 0
    
    # After mobilization, apply pending mobilizations then end the turn
    if state.phase == "mobilization":
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

    # Emit phase changed event
    events.append(phase_changed(old_phase, state.phase, state.current_faction))

    return state, events


def _reset_unit_stats_for_faction(state: GameState, faction_id: str) -> None:
    """
    Reset remaining_movement and remaining_health to base values for all units
    owned by the specified faction.
    """
    for territory in state.territories.values():
        if territory.owner == faction_id:
            for unit in territory.units:
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

    # Determine next faction
    faction_ids = sorted(faction_defs.keys())
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
