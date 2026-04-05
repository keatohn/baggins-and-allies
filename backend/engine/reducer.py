"""
Main game reducer.
Applies actions to state, enforcing rules and producing new state.
Returns (new_state, events) where events describe what happened.
"""

from copy import deepcopy
from backend.engine.state import GameState, UnitStack, TerritoryState, Unit, ActiveCombat, CombatRoundResult, PendingMove, PendingMobilization, PendingCampPlacement
from backend.engine.actions import Action
from backend.engine.definitions import UnitDefinition, TerritoryDefinition, FactionDefinition, CampDefinition, PortDefinition, is_transportable
from backend.engine.combat import (
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_bombikazi_pairing,
    get_ladder_infantry_instance_ids,
    get_siegework_dice_counts,
    get_siegework_attacker_rolling_units,
    get_siegework_round_attacker_display_units,
    get_siegework_round_defender_display_units,
    SIEGEWORK_SPECIAL_LADDER,
    resolve_combat_round,
    resolve_archer_prefire,
    resolve_stealth_prefire,
    resolve_siegeworks_round,
    _is_siegework_unit,
    RoundResult,
    group_dice_by_stat,
    group_siegework_attacker_dice_ram_and_flex,
    group_attacker_dice_with_ladder_segments,
    sort_attackers_for_ladder_dice_order,
    ARCHETYPE_CAVALRY,
    calculate_required_dice,
    compute_terrain_stat_modifiers,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    compute_sea_raider_stat_modifiers,
    merge_stat_modifiers,
)
from backend.engine.movement import (
    get_reachable_territories_for_unit,
    calculate_movement_cost,
    movement_cost_along_path,
    get_shortest_path,
    ford_shortcut_requires_escort_lead,
    land_move_ford_escort_cost_for_instances,
    pending_ford_crosser_lead_move_from_origin,
    remaining_ford_escort_slots,
    _is_sea_zone,
    _sea_zone_has_hostile_enemy_boats,
    are_sea_zones_directly_adjacent,
    expand_sea_offload_instance_ids,
    remaining_load_slots_on_boat,
    remaining_sea_load_passenger_slots,
    resolve_territory_key_in_state,
    resolve_unit_for_move_declaration,
    instance_allowed_in_new_move_from_territory,
    sea_land_adjacent_for_offload,
    get_forced_naval_combat_instance_ids,
)
from backend.engine.queries import (
    _get_retreat_adjacent_ids,
    _territory_is_friendly_for_retreat,
    get_aerial_units_must_move,
    get_contested_territories,
    _sea_zone_adjacent_to_owned_port,
    _port_power_for_sea_zone,
    get_mobilization_capacity,
    _home_territory_ids,
    _is_naval_unit,
    participates_in_sea_hex_naval_combat,
    _territory_has_port,
    _total_pending_mobilization_to_port,
    validate_move_as_sea_offload_if_applicable,
    validate_sail_move_for_offload_sea_raid,
    valid_camp_placement_territory_ids,
)
from backend.engine.utils import (
    unitstack_to_units,
    get_unit_faction,
    is_land_unit,
    is_aerial_unit,
    has_unit_special,
    archer_prefire_eligible,
    can_conquer_territory_as_attacker,
    faction_owns_capital,
    effective_territory_owner,
    effective_original_owner,
)


def _prefire_stat_delta(state: GameState) -> int:
    """Manifest prefire_penalty True (default): -1 on stealth/archer prefire; False: 0."""
    return -1 if getattr(state, "prefire_penalty", True) else 0
from backend.engine.combat_specials import (
    BattleSpecialsResult,
    compute_battle_specials_and_modifiers,
    empty_round_special_payload,
    specials_flags_for_round_payload,
)
from backend.engine.events import (
    GameEvent,
    phase_changed,
    turn_started,
    turn_ended,
    turn_skipped,
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
    enrich_event,
    camp_placed,
)


def _passengers_aboard_on_boat(
    boat: Unit,
    container_units: list,
    unit_defs: dict[str, UnitDefinition],
) -> int:
    if not boat.instance_id or not _is_naval_unit(unit_defs.get(boat.unit_id)):
        return 0
    return sum(
        1 for u in container_units
        if getattr(u, "loaded_onto", None) == boat.instance_id
    )


def _unit_id_from_instance_id_pattern(instance_id: str, faction_ids: list[str]) -> str | None:
    """
    Reverse GameState.generate_unit_instance_id: "{faction}_{unit_id}_{counter}".
    unit_id may contain underscores (e.g. morgul_orc). Counter is numeric suffix after last underscore.
    """
    for fid in sorted(faction_ids, key=len, reverse=True):
        prefix = fid + "_"
        if not instance_id.startswith(prefix):
            continue
        rest = instance_id[len(prefix) :]
        parts = rest.rsplit("_", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        return parts[0]
    return None


def _land_combat_unit_side(
    unit: Unit,
    attacker_faction: str,
    attacker_alliance: str | None,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> str | None:
    """
    Land / sea-raid-land combat roster: must stay identical between initiate_combat and continue_combat.
    attacker = attacking faction's units; defender = other faction with a different alliance;
    None = bystander (no faction on unit def, or allied non-attacker sharing attacker's alliance).
    """
    uo = get_unit_faction(unit, unit_defs)
    if uo is None:
        return None
    if uo == attacker_faction:
        return "attacker"
    ua = getattr(faction_defs.get(uo), "alliance", None)
    if ua != attacker_alliance:
        return "defender"
    return None


def _build_round_unit_display(
    unit: Unit,
    unit_def: UnitDefinition | None,
    stat_mod: int,
    is_attacker: bool,
    faction: str,
    territory_def: TerritoryDefinition | None,
    spec_result: BattleSpecialsResult,
    attacker_effective_attack_override: dict[str, int] | None = None,
    passenger_aboard: int = 0,
) -> dict:
    """Build one unit dict for combat_round_resolved payload. Special booleans from combat_specials engine (events.py UI contract)."""
    sp = empty_round_special_payload() if not unit_def else specials_flags_for_round_payload(
        unit.instance_id, is_attacker, spec_result
    )
    if not unit_def:
        out_missing = {
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
            **sp,
            "siegework_archetype": False,
        }
        if passenger_aboard > 0:
            out_missing["passenger_count"] = passenger_aboard
        return out_missing
    base_attack = getattr(unit_def, "attack", 0)
    base_defense = getattr(unit_def, "defense", 0)
    archetype = getattr(unit_def, "archetype", "")
    is_archer = archer_prefire_eligible(unit_def)
    ov = attacker_effective_attack_override or {}
    eff_att = (
        ov[unit.instance_id]
        if is_attacker and unit.instance_id in ov
        else base_attack + stat_mod
    )
    out = {
        "instance_id": unit.instance_id,
        "unit_id": unit.unit_id,
        "display_name": getattr(unit_def, "display_name", unit.unit_id),
        "attack": base_attack,
        "defense": base_defense,
        "effective_attack": eff_att if is_attacker else None,
        "effective_defense": base_defense + stat_mod if not is_attacker else None,
        "health": getattr(unit_def, "health", 1),
        "remaining_health": unit.remaining_health,
        "remaining_movement": getattr(unit, "remaining_movement", 0),
        "is_archer": is_archer,
        "faction": faction,
        **sp,
        "siegework_archetype": archetype == "siegework",
    }
    if passenger_aboard > 0:
        out["passenger_count"] = passenger_aboard
    return out


def _is_naval_combat_attacker_hit_rules(
    attacker_units: list[Unit],
    sea_zone_id: str | None,
    territory_is_sea: bool,
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    """
    When True, defender hits may only target naval/aerial attackers (passengers on transports are protected).
    Sea raid land attackers fight on land — use normal land rules so infantry/cavalry take hits.
    Pure sea-hex combat still uses naval rules.
    """
    if territory_is_sea:
        return True
    if not sea_zone_id or not attacker_units:
        return False
    return all(
        _is_naval_unit(unit_defs.get(u.unit_id)) or is_aerial_unit(unit_defs.get(u.unit_id))
        for u in attacker_units
    )


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


def _territory_has_port(
    territory_id: str,
    port_defs: dict[str, PortDefinition],
) -> bool:
    """True if the territory has a port (immutable, not destroyed on conquest)."""
    for port in (port_defs or {}).values():
        if port.territory_id == territory_id:
            return True
    return False


def _faction_unit_count(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
) -> int:
    """Count active units belonging to this faction anywhere on the map (by unit def faction)."""
    n = 0
    for _tid, ts in state.territories.items():
        for unit in ts.units:
            ud = unit_defs.get(unit.unit_id)
            if ud and getattr(ud, "faction", None) == faction_id:
                n += 1
    return n


# Action type for defender casualty order (single source of truth so we never typo)
SET_TERRITORY_DEFENDER_CASUALTY_ORDER = "set_territory_defender_casualty_order"

# Phase rules: which action types are allowed in which phases
# Note: During active combat, only continue_combat and retreat are allowed
PHASE_ALLOWED_ACTIONS = {
    "purchase": ["purchase_units", "purchase_camp", "repair_stronghold", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "combat_move": ["move_units", "cancel_move", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "combat": ["initiate_combat", "continue_combat", "retreat", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "non_combat_move": ["move_units", "cancel_move", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "skip_turn"],
    "mobilization": ["mobilize_units", "queue_camp_placement", "cancel_camp_placement", "cancel_mobilization", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "end_phase", "end_turn", "skip_turn"],
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

    action_type_attr = getattr(action, "type", None) or getattr(action, "action_type", None)
    if action_type_attr is None and isinstance(action, dict):
        action_type_attr = action.get("type") or action.get("action_type")
    action_type_attr = str(action_type_attr or "").strip()
    if action_type_attr not in allowed_actions:
        raise ValueError(
            f"Action '{action_type_attr}' is not allowed in phase '{phase}'. "
            f"Allowed actions: {', '.join(allowed_actions)}"
        )

    # Special combat phase restrictions
    if phase == "combat":
        if state.active_combat is not None:
            # During active combat, continue_combat, retreat, and set_territory_defender_casualty_order allowed
            if action_type_attr not in ["continue_combat", "retreat", SET_TERRITORY_DEFENDER_CASUALTY_ORDER, "skip_turn"]:
                raise ValueError(
                    f"Active combat in progress. Must use 'continue_combat' or 'retreat', "
                    f"not '{action_type_attr}'"
                )
        else:
            # No active combat, can't continue or retreat
            if action_type_attr in ["continue_combat", "retreat"]:
                raise ValueError(
                    f"No active combat to {action_type_attr}. Use 'initiate_combat' first."
                )


def apply_action(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
    port_defs: dict[str, PortDefinition] | None = None,
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
    if port_defs is None:
        port_defs = {}
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

    _raw = getattr(action, "type", None) or getattr(action, "action_type", None)
    if _raw is None and isinstance(action, dict):
        _raw = action.get("type") or action.get("action_type")
    action_type = (str(_raw) if _raw is not None else "").strip()

    # Handle defender casualty order first so it can never raise "Unknown action type"
    if (action_type == SET_TERRITORY_DEFENDER_CASUALTY_ORDER or
            (action_type and "set_territory_defender_casualty_order" in action_type) or
            (action_type and "defender_casualty" in action_type)):
        new_state, evts = _handle_set_territory_defender_casualty_order(new_state, action)
        events.extend(evts)

    elif action_type == "purchase_camp":
        new_state, evts = _handle_purchase_camp(
            new_state, action, camp_defs or {}, territory_defs)
        events.extend(evts)

    elif action_type == "repair_stronghold":
        new_state, evts = _handle_repair_stronghold(new_state, action, territory_defs)
        events.extend(evts)

    elif action_type == "place_camp":
        new_state, evts = _handle_place_camp(new_state, action, camp_defs or {})
        events.extend(evts)

    elif action_type == "queue_camp_placement":
        new_state, evts = _handle_queue_camp_placement(new_state, action, camp_defs or {})
        events.extend(evts)

    elif action_type == "cancel_camp_placement":
        new_state, evts = _handle_cancel_camp_placement(new_state, action)
        events.extend(evts)

    elif action_type == "purchase_units":
        new_state, evts = _handle_purchase_units(
            new_state, action, unit_defs, faction_defs,
            territory_defs, camp_defs or {}, port_defs or {})
        events.extend(evts)

    elif action_type == "move_units":
        new_state, evts = _handle_move_units(
            new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action_type == "initiate_combat":
        new_state, evts = _handle_initiate_combat(
            new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action_type == "continue_combat":
        new_state, evts = _handle_continue_combat(
            new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action_type == "retreat":
        new_state, evts = _handle_retreat(new_state, action, unit_defs, territory_defs, faction_defs)
        events.extend(evts)

    elif action_type == "mobilize_units":
        new_state, evts = _handle_mobilize_units(
            new_state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
        events.extend(evts)

    elif action_type == "cancel_move":
        new_state, evts = _handle_cancel_move(new_state, action)
        events.extend(evts)

    elif action_type == "cancel_mobilization":
        new_state, evts = _handle_cancel_mobilization(new_state, action)
        events.extend(evts)

    elif action_type == "end_phase":
        new_state, evts = _handle_end_phase(
            new_state, unit_defs, territory_defs, faction_defs, camp_defs)
        events.extend(evts)

    elif action_type == "end_turn":
        new_state, evts = _handle_end_turn(
            new_state, territory_defs, faction_defs, camp_defs, unit_defs)
        events.extend(evts)

    elif action_type == "skip_turn":
        new_state, evts = _handle_skip_turn(
            new_state, unit_defs, territory_defs, faction_defs, camp_defs)
        events.extend(evts)

    else:
        if SET_TERRITORY_DEFENDER_CASUALTY_ORDER in (action_type or "") or "defender_casualty" in (action_type or ""):
            new_state, evts = _handle_set_territory_defender_casualty_order(new_state, action)
            events.extend(evts)
        else:
            raise ValueError(f"Unknown action type: {action_type or getattr(action, 'type', '?')}")

    # Enrich every event with turn_number, phase, faction, and human-readable message
    for e in events:
        enrich_event(e, state, unit_defs, territory_defs, faction_defs)

    return new_state, events


def _handle_purchase_units(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    camp_defs: dict[str, CampDefinition],
    port_defs: dict[str, PortDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Purchase units for a faction.
    Validates:
    - Faction owns their capital (required to purchase)
    - Only purchasable units can be bought
    - Faction has sufficient resources
    - Land and naval units cannot exceed land and sea mobilization capacity respectively
    """
    events: list[GameEvent] = []
    faction_id = action.faction
    purchases = action.payload.get("purchases", {})  # {unit_id: count}

    faction_def = faction_defs.get(faction_id)
    if not faction_def:
        raise ValueError(f"Unknown faction: {faction_id}")

    # Check capital ownership - cannot purchase if capital is captured
    if not faction_owns_capital(state, faction_id, faction_defs):
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

    # Validate land vs sea mobilization capacity (camps vs port-adjacent sea zones)
    capacity_info = get_mobilization_capacity(
        state, faction_id, territory_defs, camp_defs, port_defs, unit_defs
    )
    territories_list = capacity_info.get("territories", [])
    land_cap = sum(t.get("power", 0) for t in territories_list) + sum(
        1 for t in territories_list if t.get("home_unit_capacity")
    )
    sea_cap = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))
    already_stacks = state.faction_purchased_units.get(faction_id, [])
    already_land = sum(s.count for s in already_stacks if not _is_naval_unit(unit_defs.get(s.unit_id)))
    already_naval = sum(s.count for s in already_stacks if _is_naval_unit(unit_defs.get(s.unit_id)))
    this_land = sum(c for uid, c in purchases.items() if not _is_naval_unit(unit_defs.get(uid)))
    this_naval = sum(c for uid, c in purchases.items() if _is_naval_unit(unit_defs.get(uid)))
    if already_land + this_land > land_cap:
        raise ValueError(
            f"Cannot purchase that many land units: land mobilization capacity is {land_cap} "
            f"(already purchased: {already_land} land, this purchase: {this_land} land)"
        )
    if already_naval + this_naval > sea_cap:
        raise ValueError(
            f"Cannot purchase that many naval units: sea mobilization capacity is {sea_cap} "
            f"(already purchased: {already_naval} naval, this purchase: {this_naval} naval)"
        )

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


def _handle_repair_stronghold(
    state: GameState,
    action: Action,
    territory_defs: dict[str, TerritoryDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """Apply stronghold repairs: deduct power, increase territory.stronghold_current_health (capped at base)."""
    events: list[GameEvent] = []
    faction_id = action.faction
    repairs = action.payload.get("repairs") or []
    if not repairs:
        return state, events

    repair_cost_per_hp = getattr(state, "stronghold_repair_cost", 0)
    if repair_cost_per_hp <= 0:
        raise ValueError("Stronghold repair is not available in this setup")
    power = state.faction_resources.get(faction_id, {}).get("power", 0)
    total_hp = 0
    for r in repairs:
        if not isinstance(r, dict):
            continue
        hp_to_add = r.get("hp_to_add", 0)
        try:
            hp_to_add = int(hp_to_add)
        except (TypeError, ValueError):
            continue
        if hp_to_add <= 0:
            continue
        tid = r.get("territory_id")
        if not tid or tid not in state.territories:
            continue
        territory = state.territories[tid]
        if territory.owner != faction_id:
            continue
        tdef = territory_defs.get(tid)
        if not tdef or not getattr(tdef, "is_stronghold", False):
            continue
        base_hp = getattr(tdef, "stronghold_base_health", 0) or 0
        if base_hp <= 0:
            continue
        current = getattr(territory, "stronghold_current_health", None)
        current = current if current is not None else base_hp
        actual_add = min(hp_to_add, base_hp - current)
        if actual_add <= 0:
            continue
        territory.stronghold_current_health = current + actual_add
        total_hp += actual_add
    total_cost = total_hp * repair_cost_per_hp
    if total_cost > 0:
        if power < total_cost:
            raise ValueError(f"Insufficient power for repairs: need {total_cost}, have {power}")
        state.faction_resources[faction_id]["power"] = power - total_cost
        events.append(resources_changed(faction_id, "power", power, power - total_cost, "stronghold_repair"))
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
    events.append(camp_placed(faction_id, territory_id))
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
        fac = state.current_faction or ""
        if fac:
            events.append(camp_placed(fac, territory_id))
    state.pending_camp_placements = []
    return state, events


def _load_boat_count_for_message(
    from_id: str,
    to_id: str,
    unit_instance_ids: list[str],
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    faction_id: str,
    load_onto_boat_instance_id: str | None,
) -> int:
    """How many distinct transport boats receive passengers from this load declaration (for event log wording)."""
    if load_onto_boat_instance_id:
        return 1
    from_t = state.territories.get(from_id)
    to_t = state.territories.get(to_id)
    if not from_t or not to_t:
        return 1

    def _is_naval(ud: UnitDefinition | None) -> bool:
        if not ud:
            return False
        return getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", [])

    units_by_id = {u.instance_id: u for u in from_t.units}
    passengers_n = 0
    for iid in unit_instance_ids:
        u = units_by_id.get(iid)
        if u and is_land_unit(unit_defs.get(u.unit_id)):
            passengers_n += 1
    if passengers_n == 0:
        return 1

    naval_units = sorted(
        [u for u in to_t.units if get_unit_faction(u, unit_defs) == faction_id and _is_naval(unit_defs.get(u.unit_id))],
        key=lambda u: u.instance_id,
    )
    boats_used: set[str] = set()
    idx = 0
    for boat in naval_units:
        cap = getattr(unit_defs.get(boat.unit_id), "transport_capacity", 0) or 0
        existing_on_boat = sum(1 for u in to_t.units if getattr(u, "loaded_onto", None) == boat.instance_id)
        slots = max(0, cap - existing_on_boat)
        for _ in range(slots):
            if idx >= passengers_n:
                break
            boats_used.add(boat.instance_id)
            idx += 1
    return max(1, len(boats_used))


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
    from_id = action.payload.get("from") or action.payload.get("from_territory") or ""
    to_id = action.payload.get("to") or action.payload.get("to_territory") or ""
    from_id = resolve_territory_key_in_state(state, str(from_id).strip(), territory_defs)
    to_id = resolve_territory_key_in_state(state, str(to_id).strip(), territory_defs)
    unit_instance_ids = action.payload.get("unit_instance_ids", [])
    faction_id = action.faction

    if not from_id or not to_id:
        raise ValueError("No origin or destination specified")
    if from_id not in state.territories or to_id not in state.territories:
        raise ValueError(f"Invalid territory: {from_id} or {to_id}")

    from_territory = state.territories[from_id]

    if len(unit_instance_ids) == 0:
        raise ValueError("No units specified to move")

    unit_instance_ids = expand_sea_offload_instance_ids(
        state,
        from_id,
        to_id,
        list(unit_instance_ids),
        unit_defs,
        territory_defs,
        faction_id,
    )
    if len(unit_instance_ids) == 0:
        raise ValueError("No units specified to move")

    # Build a lookup of units in source territory by instance_id
    units_by_id = {unit.instance_id: unit for unit in from_territory.units}
    from_def_m = territory_defs.get(from_id)
    to_def_m = territory_defs.get(to_id)
    sea_to_land_move = (
        from_def_m
        and to_def_m
        and _is_sea_zone(from_def_m)
        and not _is_sea_zone(to_def_m)
    )
    sea_to_sea_move = (
        from_def_m
        and to_def_m
        and _is_sea_zone(from_def_m)
        and _is_sea_zone(to_def_m)
    )

    # Validate all units exist (on origin sea, or still on land if load into this sea is pending) and belong to the faction
    units_to_move = []
    for instance_id in unit_instance_ids:
        if not instance_allowed_in_new_move_from_territory(
            state, instance_id, from_id, state.phase, territory_defs
        ):
            raise ValueError(f"Unit {instance_id} already has a pending move")
        if sea_to_land_move:
            unit = resolve_unit_for_move_declaration(
                state, from_id, instance_id, state.phase, territory_defs
            )
        elif sea_to_sea_move:
            unit = units_by_id.get(instance_id)
            if not unit:
                unit = resolve_unit_for_move_declaration(
                    state, from_id, instance_id, state.phase, territory_defs
                )
        else:
            unit = units_by_id.get(instance_id)
        if not unit:
            raise ValueError(f"Unit {instance_id} not found in {from_id}")
        # Validate unit belongs to the faction (use unit def: faction from unit def; instance_id can contain underscores)
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def or unit_def.faction != faction_id:
            raise ValueError(f"Unit {instance_id} does not belong to {faction_id}")
        units_to_move.append(unit)
        units_by_id[unit.instance_id] = unit

    charge_through = action.payload.get("charge_through")
    if charge_through is not None and not isinstance(charge_through, list):
        charge_through = []
    charge_through = [str(t) for t in charge_through if str(t) != to_id] if charge_through else []
    # Destination must never be in charge_through (we only pass through; destination can have units)

    sea_offload_vr = validate_move_as_sea_offload_if_applicable(
        state,
        from_id,
        to_id,
        units_to_move,
        unit_defs,
        territory_defs,
        faction_defs,
        faction_id,
        charge_through,
    )
    if sea_offload_vr is not None and not sea_offload_vr.valid:
        raise ValueError(sea_offload_vr.error or "Invalid sea offload / sea raid move")
    sea_offload_ok = sea_offload_vr is not None and sea_offload_vr.valid

    all_charge_routes: list[dict[str, list[list[str]]]] = []
    can_reach_list: list[bool] = []
    ford_pending_exclude = set(unit_instance_ids)
    same_move_has_ford_crosser = any(
        has_unit_special(unit_defs.get(u.unit_id), "ford_crosser") for u in units_to_move
    )
    if not sea_offload_ok:
        for unit in units_to_move:
            reachable, charge_routes = get_reachable_territories_for_unit(
                unit,
                from_id,
                state,
                unit_defs,
                territory_defs,
                faction_defs,
                state.phase,
                None,
                ford_pending_exclude,
                same_move_has_ford_crosser,
            )
            all_charge_routes.append(charge_routes)
            can_reach_list.append(to_id in reachable)

    # Land → sea: transportable land = embark (enforce boat/zone capacity). All-aerial = combat entry into sea
    # (naval battle), not load — same adjacent-sea reachability makes all(can_reach_list) True for both.
    if not sea_offload_ok and all(can_reach_list):
        from_def_chk = territory_defs.get(from_id)
        to_def_chk = territory_defs.get(to_id)
        if (
            from_def_chk
            and to_def_chk
            and not _is_sea_zone(from_def_chk)
            and _is_sea_zone(to_def_chk)
            and units_to_move
        ):
            all_transportable_land = all(
                is_land_unit(unit_defs.get(u.unit_id)) and is_transportable(unit_defs.get(u.unit_id))
                for u in units_to_move
            )
            all_aerial = all(is_aerial_unit(unit_defs.get(u.unit_id)) for u in units_to_move)
            if all_transportable_land:
                to_territory = state.territories.get(to_id)
                if to_territory:
                    load_onto_decl = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
                    if load_onto_decl:
                        boat_slots = remaining_load_slots_on_boat(
                            state, to_id, load_onto_decl, faction_id, unit_defs, territory_defs, state.phase
                        )
                        if len(units_to_move) > boat_slots:
                            raise ValueError(
                                f"Boat {load_onto_decl} has only {boat_slots} passenger slot(s) left in {to_id} "
                                f"(onboard + pending loads), cannot load {len(units_to_move)}"
                            )
                    else:
                        zone_slots = remaining_sea_load_passenger_slots(
                            state, to_id, faction_id, unit_defs, territory_defs, state.phase
                        )
                        if len(units_to_move) > zone_slots:
                            raise ValueError(
                                f"Not enough transport capacity in {to_id}: {len(units_to_move)} passengers but only "
                                f"{zone_slots} slot(s) left (including pending loads this phase)"
                            )
            elif all_aerial:
                pass
            else:
                raise ValueError("Only transportable land units can load into a sea zone")

        if (
            from_def_chk
            and to_def_chk
            and not _is_sea_zone(from_def_chk)
            and not _is_sea_zone(to_def_chk)
            and units_to_move
        ):
            ford_cost = land_move_ford_escort_cost_for_instances(
                from_id, to_id, unit_instance_ids, state, unit_defs, territory_defs
            )
            if ford_cost > 0:
                okey = resolve_territory_key_in_state(state, from_id, territory_defs)
                dkey = resolve_territory_key_in_state(state, to_id, territory_defs)
                needs_lead = ford_shortcut_requires_escort_lead(okey, dkey, territory_defs)
                if needs_lead and not any(
                    has_unit_special(unit_defs.get(u.unit_id), "ford_crosser") for u in units_to_move
                ):
                    if not pending_ford_crosser_lead_move_from_origin(
                        state, from_id, state.phase, unit_defs, territory_defs
                    ):
                        raise ValueError(
                            "Declare a ford crosser's move across this ford before other units may use escort capacity."
                        )
                ford_rem = remaining_ford_escort_slots(
                    state,
                    from_id,
                    faction_id,
                    unit_defs,
                    territory_defs,
                    state.phase,
                    ford_pending_exclude,
                )
                if ford_cost > ford_rem:
                    raise ValueError(
                        f"Not enough ford escort capacity: need {ford_cost} slot(s) but only {ford_rem} remain "
                        f"(ford crossers' transport_capacity in {from_id}, minus pending moves)"
                    )

    if not all(can_reach_list) and not sea_offload_ok:
        path = get_shortest_path(from_id, to_id, territory_defs)
        path_includes_sea = path and any(
            _is_sea_zone(territory_defs.get(tid)) for tid in path
        )
        dest_is_sea = _is_sea_zone(territory_defs.get(to_id))
        from_sea = _is_sea_zone(territory_defs.get(from_id))
        drivers = [u for u, cr in zip(units_to_move, can_reach_list) if cr]
        passengers = [u for u, cr in zip(units_to_move, can_reach_list) if not cr]
        sail_land_raw = (action.payload.get("sail_to_offload_land_territory_id") or "").strip()
        move_type_payload = (action.payload.get("move_type") or "").strip()
        if (
            sail_land_raw
            and move_type_payload == "sail"
            and from_sea
            and dest_is_sea
        ):
            vr = validate_sail_move_for_offload_sea_raid(
                state,
                from_id,
                to_id,
                sail_land_raw,
                units_to_move,
                unit_instance_ids,
                unit_defs,
                territory_defs,
                faction_defs,
                faction_id,
                state.phase,
            )
            if not vr.valid:
                raise ValueError(vr.error or "Invalid sail for sea raid/offload")
        # Load: land -> sea, stack is all land; boats already in destination sea zone provide capacity
        elif not from_sea and dest_is_sea and not drivers and passengers:
            for u in passengers:
                ud = unit_defs.get(u.unit_id)
                if not is_land_unit(ud):
                    raise ValueError(f"Unit {u.instance_id} cannot be carried (only land units can be passengers)")
                if not is_transportable(ud):
                    raise ValueError(f"Unit {u.instance_id} cannot be transported (no transportable tag)")
            to_territory = state.territories.get(to_id)
            if to_territory:
                load_onto_decl = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
                if load_onto_decl:
                    boat_slots = remaining_load_slots_on_boat(
                        state, to_id, load_onto_decl, faction_id, unit_defs, territory_defs, state.phase
                    )
                    if len(passengers) > boat_slots:
                        raise ValueError(
                            f"Boat {load_onto_decl} has only {boat_slots} passenger slot(s) left in {to_id} "
                            f"(onboard + pending loads), cannot load {len(passengers)}"
                        )
                else:
                    zone_slots = remaining_sea_load_passenger_slots(
                        state, to_id, faction_id, unit_defs, territory_defs, state.phase
                    )
                    if len(passengers) > zone_slots:
                        raise ValueError(
                            f"Not enough transport capacity in {to_id}: {len(passengers)} passengers but only "
                            f"{zone_slots} slot(s) left (including pending loads this phase)"
                        )
        elif path_includes_sea or dest_is_sea:
            if not drivers:
                raise ValueError(
                    "At least one unit (naval or aerial) must be able to reach the destination"
                )
            for u in passengers:
                ud = unit_defs.get(u.unit_id)
                if not is_land_unit(ud):
                    raise ValueError(
                        f"Unit {u.instance_id} cannot be carried (only land units can be passengers)"
                    )
                if not is_transportable(ud):
                    raise ValueError(
                        f"Unit {u.instance_id} cannot be transported (no transportable tag)"
                    )
            naval_capacity = sum(
                getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
                for u in drivers
                if (
                    getattr(unit_defs.get(u.unit_id), "archetype", "") == "naval"
                    or "naval" in getattr(unit_defs.get(u.unit_id), "tags", [])
                )
            )
            if len(passengers) > naval_capacity:
                raise ValueError(
                    f"Too many passengers ({len(passengers)}) for transport capacity ({naval_capacity})"
                )
            if charge_through:
                raise ValueError("charge_through not allowed for sea transport moves")
        else:
            bad = next(u for u, cr in zip(units_to_move, can_reach_list) if not cr)
            raise ValueError(
                f"Unit {bad.instance_id} cannot reach {to_id} from {from_id} "
                f"(remaining_movement={bad.remaining_movement}, phase={state.phase})"
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

    # Non-combat move: destination must be friendly, allied, or pass-through neutral only (never enemy, never ownable neutral).
    # Use effective_territory_owner (same as pathfinding / validate_action) so pending_captures matches conquer-before-flush.
    if state.phase == "non_combat_move":
        to_territory = state.territories.get(to_id)
        if to_territory:
            eff_o = effective_territory_owner(state, to_id)
            if eff_o is not None and eff_o != faction_id:
                our_fd = faction_defs.get(faction_id)
                owner_fd = faction_defs.get(eff_o)
                our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
                owner_alliance = getattr(owner_fd, "alliance", "") if owner_fd else ""
                if owner_alliance != our_alliance:
                    raise ValueError(
                        f"Non-combat move cannot target enemy territory {to_id} (owner={eff_o})"
                    )
            elif eff_o is None:
                to_def = territory_defs.get(to_id)
                if to_def and getattr(to_def, "ownable", True):
                    raise ValueError(
                        f"Non-combat move cannot target ownable neutral territory {to_id} (conquest is combat move only)"
                    )

    # Sea transport move_type: infer from from/to. Land→sea with only aerial = combat entry (move_type aerial),
    # even if the client sent move_type load (embark is transportable land only).
    move_type = action.payload.get("move_type")
    from_def_mt = territory_defs.get(from_id)
    to_def_mt = territory_defs.get(to_id)
    from_sea_mt = _is_sea_zone(from_def_mt) or (
        isinstance(from_id, str) and from_id and "sea_zone" in from_id.lower()
    )
    to_sea_mt = _is_sea_zone(to_def_mt) or (
        isinstance(to_id, str) and to_id and "sea_zone" in to_id.lower()
    )
    all_aerial_movers = bool(units_to_move) and all(
        is_aerial_unit(unit_defs.get(u.unit_id)) for u in units_to_move
    )
    if not from_sea_mt and to_sea_mt and all_aerial_movers:
        move_type = "aerial"
    elif from_sea_mt and not to_sea_mt and all_aerial_movers:
        # Sea→land with only flyers: not naval offload (pending move must not show as offload).
        move_type = "aerial"
    elif move_type not in ("load", "offload", "sail", "aerial", "land"):
        if not from_sea_mt and to_sea_mt:
            move_type = "load"
        elif from_sea_mt and not to_sea_mt:
            move_type = "offload"
        elif from_sea_mt and to_sea_mt:
            move_type = "sail"
        else:
            path = get_shortest_path(from_id, to_id, territory_defs)
            if path and any(_is_sea_zone(territory_defs.get(t)) for t in path):
                move_type = "sail"
            else:
                any_aerial = any(
                    u and (getattr(unit_defs.get(u.unit_id), "archetype", "") == "aerial" or "aerial" in getattr(unit_defs.get(u.unit_id), "tags", []))
                    for iid in unit_instance_ids
                    for u in [units_by_id.get(iid)]
                )
                move_type = "aerial" if any_aerial else "land"

    load_onto_boat_instance_id = (action.payload.get("load_onto_boat_instance_id") or "").strip() or None
    primary_unit_id = str(units_to_move[0].unit_id) if units_to_move else ""
    avoid_forced_naval = bool(action.payload.get("avoid_forced_naval_combat"))
    if avoid_forced_naval:
        if state.phase != "combat_move":
            raise ValueError("avoid_forced_naval_combat is only valid during combat_move")
        if not sea_to_sea_move:
            raise ValueError("avoid_forced_naval_combat requires a sea-to-sea sail")
        if from_id == to_id:
            raise ValueError("avoid_forced_naval_combat requires a different destination sea zone")
        forced = set(
            get_forced_naval_combat_instance_ids(
                state, faction_id, unit_defs, territory_defs, faction_defs
            )
        )
        naval_moving = {
            u.instance_id for u in units_to_move if _is_naval_unit(unit_defs.get(u.unit_id))
        }
        if not naval_moving.issubset(forced):
            raise ValueError(
                "avoid_forced_naval_combat only applies to boats that must fight or leave the mobilization standoff"
            )
        if _sea_zone_has_hostile_enemy_boats(
            state, to_id, faction_id, unit_defs, faction_defs, territory_defs
        ):
            raise ValueError(
                "Cannot use avoid_forced_naval_combat to sail into a sea zone with hostile enemy boats"
            )
        if not are_sea_zones_directly_adjacent(territory_defs, from_id, to_id):
            raise ValueError(
                "avoid_forced_naval_combat: you may only sail to an adjacent sea zone (1 hex), regardless of movement allowance"
            )
    pending_move = PendingMove(
        from_territory=from_id,
        to_territory=to_id,
        unit_instance_ids=unit_instance_ids,
        phase=state.phase,
        charge_through=charge_through,
        move_type=move_type,
        load_onto_boat_instance_id=load_onto_boat_instance_id,
        primary_unit_id=primary_unit_id,
        avoid_forced_naval_combat=avoid_forced_naval,
    )
    state.pending_moves.append(pending_move)

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

    # Sea transport apply order: load before sail before offload (so load→sail→offload in one turn works).
    # Charge-through edges below are for cavalry only; these sea edges are separate.
    def _effective_move_type(m: PendingMove) -> str | None:
        mt = getattr(m, "move_type", None)
        # Preserve land/aerial (combat moves); do not treat aerial as load when inferring from hex shape.
        if mt in ("load", "offload", "sail", "land", "aerial"):
            return mt
        raw_from = str(getattr(m, "from_territory", "") or "").strip()
        raw_to = str(getattr(m, "to_territory", "") or "").strip()
        fk = resolve_territory_key_in_state(state, raw_from, territory_defs)
        tk = resolve_territory_key_in_state(state, raw_to, territory_defs)
        from_def = territory_defs.get(fk)
        to_def = territory_defs.get(tk)
        from_sea = _is_sea_zone(from_def)
        to_sea = _is_sea_zone(to_def)
        if not from_sea and to_sea:
            from_terr = state.territories.get(fk)
            ids = list(getattr(m, "unit_instance_ids", None) or [])
            if from_terr and ids:
                by_id = {u.instance_id: u for u in from_terr.units}
                movers = [by_id[i] for i in ids if i in by_id]
                if movers and all(is_aerial_unit(unit_defs.get(u.unit_id)) for u in movers):
                    return "aerial"
            return "load"
        if from_sea and not to_sea:
            from_terr = state.territories.get(fk)
            ids = list(getattr(m, "unit_instance_ids", None) or [])
            if from_terr and ids:
                by_id = {u.instance_id: u for u in from_terr.units}
                movers = [by_id[i] for i in ids if i in by_id]
                if movers and all(is_aerial_unit(unit_defs.get(u.unit_id)) for u in movers):
                    return "aerial"
            return "offload"
        if from_sea and to_sea:
            return "sail"
        return None

    def _sea_priority(m: PendingMove) -> int:
        mt = _effective_move_type(m)
        if mt == "load":
            return 0
        if mt == "sail":
            return 1
        if mt == "offload":
            return 2
        return 3

    # Use a deterministic key (not id(m)) so deepcopy(state) produces the same apply order as real state.
    # Normalize to str/tuple[str] so sorted(succ[...]) never compares bool vs list across moves.
    def _move_key(m) -> tuple[str, str, tuple[str, ...]]:
        raw_ids = getattr(m, "unit_instance_ids", None) or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        safe_ids = tuple(str(x) for x in raw_ids)
        return (
            str(getattr(m, "from_territory", "") or ""),
            str(getattr(m, "to_territory", "") or ""),
            safe_ids,
        )

    def _charge_through_order(moves: list) -> list:
        keys = [_move_key(m) for m in moves]
        moves_by_key = dict(zip(keys, moves))
        ct = {k: getattr(m, "charge_through", None) or [] for k, m in zip(keys, moves)}
        to_id = {k: m.to_territory for k, m in zip(keys, moves)}
        from_id = {k: m.from_territory for k, m in zip(keys, moves)}
        succ = {k: [] for k in keys}
        in_degree = {k: 0 for k in keys}
        # Cavalry charge-through: move that charges through T must run before move whose destination is T
        for i, a in enumerate(moves):
            ak = keys[i]
            for tid in ct.get(ak, []):
                for j, b in enumerate(moves):
                    if i == j:
                        continue
                    if to_id.get(keys[j]) == tid:
                        succ[ak].append(keys[j])
                        in_degree[keys[j]] += 1
            # Sea transport: load (to A) before sail (from A); sail (to B) before offload (from B)
            mt_a = _effective_move_type(a)
            for j, b in enumerate(moves):
                if i == j:
                    continue
                bk = keys[j]
                mt_b = _effective_move_type(b)
                if mt_a == "load" and mt_b == "sail" and to_id.get(ak) == from_id.get(bk):
                    succ[ak].append(bk)
                    in_degree[bk] += 1
                elif mt_a == "sail" and mt_b == "offload" and to_id.get(ak) == from_id.get(bk):
                    succ[ak].append(bk)
                    in_degree[bk] += 1
        order = [moves_by_key[k] for k in keys if in_degree[k] == 0]
        order.sort(key=lambda m: (_sea_priority(m), _move_key(m)))
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
            order = sorted(moves, key=lambda m: (_sea_priority(m), _move_key(m)))
        return order

    # Apply in order: load before sail before offload (so load→sail→offload in one turn works).
    moves_to_apply = sorted(moves_to_apply, key=lambda m: (_sea_priority(m), _move_key(m)))
    moves_to_apply = _charge_through_order(moves_to_apply)
    boat_instance_ids_that_attacked: set[str] = set()

    for pending_move in moves_to_apply:
        from_id = resolve_territory_key_in_state(
            state, str(pending_move.from_territory or "").strip(), territory_defs
        )
        to_id = resolve_territory_key_in_state(
            state, str(pending_move.to_territory or "").strip(), territory_defs
        )
        unit_instance_ids = list(pending_move.unit_instance_ids)
        charge_through = getattr(pending_move, "charge_through", None) or []
        charge_through = [t for t in charge_through if t != to_id]
        # Destination must never be in charge_through (only via-territories are checked for empty)

        from_territory = state.territories.get(from_id)
        to_territory = state.territories.get(to_id)

        if not from_territory or not to_territory:
            if phase == "combat_move":
                raise ValueError(
                    f"Cannot apply pending combat move: missing territory "
                    f"(from={pending_move.from_territory!r} -> {from_id!r}, "
                    f"to={pending_move.to_territory!r} -> {to_id!r})"
                )
            continue  # Skip invalid moves (non-combat: defensive)

        # Same expansion as move declaration: client/DB may store only boat IDs; without this,
        # ids_to_move filters to land units → empty → pending move is consumed and passengers never offload.
        from_def_exp = territory_defs.get(from_id)
        to_def_exp = territory_defs.get(to_id)
        if (
            from_def_exp
            and to_def_exp
            and _is_sea_zone(from_def_exp)
            and not _is_sea_zone(to_def_exp)
            and getattr(state, "current_faction", None)
        ):
            unit_instance_ids = expand_sea_offload_instance_ids(
                state,
                from_id,
                to_id,
                unit_instance_ids,
                unit_defs,
                territory_defs,
                state.current_faction,
            )

        # Non-combat: never apply a move into enemy territory or ownable neutral (defensive guard)
        if phase == "non_combat_move":
            faction_id_check = None
            if unit_instance_ids:
                first_unit = next((u for u in from_territory.units if u.instance_id == unit_instance_ids[0]), None)
                faction_id_check = get_unit_faction(first_unit, unit_defs) if first_unit else None
            to_eff = effective_territory_owner(state, to_id)
            if to_eff is not None and faction_id_check and to_eff != faction_id_check:
                our_fd = faction_defs.get(faction_id_check)
                owner_fd = faction_defs.get(to_eff)
                if not our_fd or not owner_fd or getattr(owner_fd, "alliance", "") != getattr(our_fd, "alliance", ""):
                    continue  # Skip invalid move: would place units in enemy territory
            if to_eff is None:
                to_def = territory_defs.get(to_id)
                if to_def and getattr(to_def, "ownable", True):
                    continue  # Skip: cannot move into ownable neutral in non-combat

        # Build lookup of units in source (need for faction_id and moves)
        units_by_id = {u.instance_id: u for u in from_territory.units}
        moving_units = [units_by_id[i] for i in unit_instance_ids if i in units_by_id]
        all_movers_aerial = bool(moving_units) and all(
            is_aerial_unit(unit_defs.get(u.unit_id)) for u in moving_units
        )
        # Conquest (charge-through hexes, combat_move capture): need a unit that can hold territory,
        # not aerial-only or siegework-only stacks (ladders/rams alone cannot conquer).
        force_can_conquer_territory = any(
            can_conquer_territory_as_attacker(unit_defs.get(u.unit_id)) for u in moving_units
        )

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
                        path = get_shortest_path(from_id, to_id, territory_defs, is_aerial=False)
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
                    if not force_can_conquer_territory:
                        continue  # Aerial-only / siegework-only cannot conquer
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
        # Sea transport movement: load = 0 (passengers and boat); sail = distance for naval drivers only, 0 for passengers;
        # offload = 1 for land passengers going ashore, 0 for boats (boats stay in sea).
        move_type = getattr(pending_move, "move_type", None)
        if move_type is None:
            move_type = _effective_move_type(pending_move)
        # Sea→land with only flyers must apply as aerial (not offload/land); stale JSON may use wrong move_type.
        from_def_norm = territory_defs.get(from_id)
        to_def_norm = territory_defs.get(to_id)
        if (
            from_def_norm
            and to_def_norm
            and _is_sea_zone(from_def_norm)
            and not _is_sea_zone(to_def_norm)
            and all_movers_aerial
        ):
            move_type = "aerial"
        any_aerial = False
        for iid in unit_instance_ids:
            u = units_by_id.get(iid)
            if not u:
                continue
            ud = unit_defs.get(u.unit_id)
            if ud and (getattr(ud, "archetype", "") == "aerial" or "aerial" in getattr(ud, "tags", [])):
                any_aerial = True
                break
        if charge_through:
            path = [from_id] + list(charge_through) + [to_id]
            distance = movement_cost_along_path(path, territory_defs, is_aerial=any_aerial)
            if distance is None:
                raise ValueError(
                    f"Charge path from {from_id} through {charge_through} to {to_id} is invalid (non-adjacent steps)"
                )
        else:
            distance = calculate_movement_cost(from_id, to_id, territory_defs, is_aerial=any_aerial)
        # Offload / aerial sea→land: single step; BFS can return None if only one side lists the edge.
        if (
            distance is None
            and move_type in ("offload", "aerial")
            and from_def_norm
            and to_def_norm
            and _is_sea_zone(from_def_norm)
            and not _is_sea_zone(to_def_norm)
            and sea_land_adjacent_for_offload(from_id, to_id, territory_defs)
        ):
            distance = 1
        if distance is None:
            if move_type == "offload":
                raise ValueError(
                    f"Cannot apply pending sea offload: no valid path from {from_id} to {to_id}"
                )
            if phase == "combat_move":
                raise ValueError(
                    f"Cannot apply pending combat move: no valid path from {from_id} to {to_id} "
                    f"(move_type={move_type!r})"
                )
            continue  # Skip if no path

        if (
            move_type == "sail"
            and phase == "combat_move"
            and getattr(pending_move, "avoid_forced_naval_combat", False)
        ):
            if not are_sea_zones_directly_adjacent(territory_defs, from_id, to_id):
                raise ValueError(
                    "avoid_forced_naval_combat requires adjacent sea zones (1 hex); pending move is invalid"
                )
            distance = 1

        # Per-unit movement cost for sea transport
        if move_type == "load":
            cost_per_unit = 0
        elif move_type == "offload":
            cost_per_unit = None  # per-unit: land passengers 1, naval (boats in list) 0
        elif move_type == "sail":
            # Drivers pay distance, passengers pay 0
            def _is_driver(ud):
                if not ud:
                    return False
                return (getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", [])
                    or getattr(ud, "archetype", "") == "aerial" or "aerial" in getattr(ud, "tags", []))
            cost_per_unit = None  # per-unit below
        else:
            cost_per_unit = distance

        # Enforce movement range: no unit can move farther than its remaining_movement
        for instance_id in unit_instance_ids:
            unit = units_by_id.get(instance_id)
            if not unit:
                continue
            if move_type == "sail":
                ud = unit_defs.get(unit.unit_id)
                unit_cost = distance if _is_driver(ud) else 0
            elif move_type == "offload":
                ud = unit_defs.get(unit.unit_id)
                unit_cost = (
                    1
                    if ud and is_land_unit(ud) and not _is_naval_unit(ud)
                    else 0
                )
            else:
                unit_cost = cost_per_unit
            if unit_cost > unit.remaining_movement:
                raise ValueError(
                    f"Move from {from_id} to {to_id}: unit {instance_id} has remaining_movement={unit.remaining_movement}, need {unit_cost}"
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

        # Offload (sea -> land): ONLY land units (passengers) move to land; boats stay in sea. Naval units cannot go on land.
        # Aerial sea→land (move_type aerial): movers are flyers, not embarked land — do not use the passenger filter.
        # move_type is always set at creation (API + reducer). Defensive: if from is sea and to is land, only move land units.
        from_sea = _is_sea_zone(territory_defs.get(from_id))
        to_land = territory_defs.get(to_id) and not _is_sea_zone(territory_defs.get(to_id))
        ids_to_move = list(unit_instance_ids)
        # Naval offload: only land passengers leave the boat. All-aerial stacks are never filtered here.
        if (move_type == "offload" or (from_sea and to_land)) and not all_movers_aerial:
            ids_to_move = [
                iid for iid in unit_instance_ids
                if units_by_id.get(iid)
                and is_land_unit(unit_defs.get(units_by_id[iid].unit_id))
                and not _is_naval_unit(unit_defs.get(units_by_id[iid].unit_id))
            ]
            if ids_to_move and phase == "combat_move":
                if not hasattr(state, "territory_sea_raid_from") or state.territory_sea_raid_from is None:
                    state.territory_sea_raid_from = {}
                state.territory_sea_raid_from[to_id] = from_id
            elif phase == "combat_move" and not ids_to_move:
                boat_ids_in_move = {
                    iid for iid in unit_instance_ids
                    if (uu := units_by_id.get(iid)) and _is_naval_unit(unit_defs.get(uu.unit_id))
                }
                stranded = [
                    u for u in from_territory.units
                    if is_land_unit(unit_defs.get(u.unit_id))
                    and not _is_naval_unit(unit_defs.get(u.unit_id))
                    and getattr(u, "loaded_onto", None)
                    and getattr(u, "loaded_onto") in boat_ids_in_move
                ]
                if stranded:
                    raise ValueError(
                        "Pending sea offload/raid would move no land units ashore, but passengers remain "
                        f"on boats in this move (from={from_id}, to={to_id}). "
                        f"stored_instance_ids={list(pending_move.unit_instance_ids)!r} "
                        f"after_expand={unit_instance_ids!r}."
                    )
        load_boat_count_for_event: int | None = None
        if move_type == "load":
            load_boat_count_for_event = _load_boat_count_for_message(
                from_id,
                to_id,
                list(unit_instance_ids),
                state,
                unit_defs,
                state.current_faction or "",
                getattr(pending_move, "load_onto_boat_instance_id", None) or None,
            )
        # Move each unit and deduct movement cost
        for instance_id in ids_to_move:
            unit = units_by_id.get(instance_id)
            if unit:
                from_territory.units.remove(unit)
                if move_type == "sail":
                    ud = unit_defs.get(unit.unit_id)
                    unit_cost = distance if _is_driver(ud) else 0
                elif move_type == "offload":
                    unit_cost = 1  # ids_to_move are land passengers only
                else:
                    unit_cost = cost_per_unit
                unit.remaining_movement -= unit_cost
                to_territory.units.append(unit)

        if (
            phase == "combat_move"
            and move_type == "sail"
            and getattr(pending_move, "avoid_forced_naval_combat", False)
        ):
            avoided = list(getattr(state, "avoided_forced_naval_combat_instance_ids", None) or [])
            for iid in ids_to_move:
                u = units_by_id.get(iid)
                if u and _is_naval_unit(unit_defs.get(u.unit_id)) and iid not in avoided:
                    avoided.append(iid)
            state.avoided_forced_naval_combat_instance_ids = avoided

        # Sea transport: assign loaded_onto on load, clear on offload
        if move_type == "load":
            # Passengers (land units we just moved) get assigned to faction's naval units in to_territory
            def _is_naval(ud):
                if not ud:
                    return False
                return getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", [])
            moved_set = set(unit_instance_ids)
            passengers = sorted(
                [u for u in to_territory.units if u.instance_id in moved_set and is_land_unit(unit_defs.get(u.unit_id))],
                key=lambda u: u.instance_id,
            )
            load_onto_boat_id = getattr(pending_move, "load_onto_boat_instance_id", None) or None
            if load_onto_boat_id:
                # Assign only to the specified boat (must exist and have capacity)
                boat_unit = next((u for u in to_territory.units if u.instance_id == load_onto_boat_id), None)
                if not boat_unit or not _is_naval(unit_defs.get(boat_unit.unit_id)):
                    raise ValueError(f"Boat {load_onto_boat_id} not found or not naval in {to_id}")
                cap = getattr(unit_defs.get(boat_unit.unit_id), "transport_capacity", 0) or 0
                existing_on_boat = sum(1 for u in to_territory.units if getattr(u, "loaded_onto", None) == load_onto_boat_id)
                slots = max(0, cap - existing_on_boat)
                if len(passengers) > slots:
                    raise ValueError(
                        f"Boat {load_onto_boat_id} has capacity for {slots} more passengers, but {len(passengers)} requested"
                    )
                for p in passengers:
                    p.loaded_onto = load_onto_boat_id
            else:
                naval_units = sorted(
                    [u for u in to_territory.units if get_unit_faction(u, unit_defs) == faction_id and _is_naval(unit_defs.get(u.unit_id))],
                    key=lambda u: u.instance_id,
                )
                idx = 0
                for boat in naval_units:
                    cap = getattr(unit_defs.get(boat.unit_id), "transport_capacity", 0) or 0
                    existing_on_boat = sum(1 for u in to_territory.units if getattr(u, "loaded_onto", None) == boat.instance_id)
                    slots = max(0, cap - existing_on_boat)
                    for _ in range(slots):
                        if idx >= len(passengers):
                            break
                        passengers[idx].loaded_onto = boat.instance_id
                        idx += 1
                if idx < len(passengers):
                    raise ValueError(
                        f"Not enough transport capacity in {to_id} for {len(passengers)} passengers "
                        f"({len(passengers) - idx} unassigned after filling boats)"
                    )
        elif move_type == "offload":
            # Clear loaded_onto for units we moved to land (same object refs in to_territory)
            for u in to_territory.units:
                if u.instance_id in ids_to_move:
                    u.loaded_onto = None

        # If load in combat_move phase, every boat that received passengers must attack before phase end
        if move_type == "load" and phase == "combat_move" and to_id:
            if not hasattr(state, "loaded_naval_must_attack_instance_ids"):
                state.loaded_naval_must_attack_instance_ids = []
            # Boats in to_territory that now have at least one passenger (loaded_onto == boat.instance_id)
            for u in to_territory.units:
                if not _is_naval_unit(unit_defs.get(u.unit_id)):
                    continue
                boat_id = u.instance_id or ""
                if any(getattr(p, "loaded_onto", None) == boat_id for p in to_territory.units):
                    if boat_id not in state.loaded_naval_must_attack_instance_ids:
                        state.loaded_naval_must_attack_instance_ids.append(boat_id)

        # If combat_move from sea to land (sea raid/offload) or sea to enemy sea (naval combat), boats in move have "attacked"
        if phase == "combat_move" and from_id and getattr(state, "loaded_naval_must_attack_instance_ids", []):
            from_def = territory_defs.get(from_id)
            to_def = territory_defs.get(to_id)
            if from_def and _is_sea_zone(from_def) and to_def:
                to_land = not _is_sea_zone(to_def)
                moving_faction_def = faction_defs.get(faction_id)
                to_enemy_sea = (
                    _is_sea_zone(to_def)
                    and any(
                        get_unit_faction(p, unit_defs) != faction_id
                        and (
                            not moving_faction_def
                            or not faction_defs.get(get_unit_faction(p, unit_defs))
                            or faction_defs.get(get_unit_faction(p, unit_defs)).alliance != moving_faction_def.alliance
                        )
                        for p in to_territory.units
                    )
                )
                if to_land or to_enemy_sea:
                    for iid in unit_instance_ids:
                        u = units_by_id.get(iid)
                        if u and _is_naval_unit(unit_defs.get(u.unit_id)):
                            boat_instance_ids_that_attacked.add(iid)

        # Check if this is combat_move into territory we capture (undefended enemy or empty unowned)
        # Aerial-only / siegework-only cannot conquer: require at least one conquering-capable unit
        if unit_instance_ids and faction_id and phase == "combat_move" and force_can_conquer_territory:
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

        event_faction = state.current_faction or faction_id or ""
        ids_for_log = ids_to_move if ids_to_move else list(unit_instance_ids)
        events.append(
            units_moved(
                event_faction,
                from_id,
                to_id,
                ids_for_log,
                phase,
                move_type=move_type,
                load_boat_count=load_boat_count_for_event if move_type == "load" else None,
            )
        )

    # Post-pass: clear loaded_naval_must_attack_instance_ids for any boat that attacked (sea raid or naval combat)
    if phase == "combat_move" and boat_instance_ids_that_attacked and getattr(state, "loaded_naval_must_attack_instance_ids", []):
        state.loaded_naval_must_attack_instance_ids = [
            bid for bid in state.loaded_naval_must_attack_instance_ids if bid not in boat_instance_ids_that_attacked
        ]

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


def get_state_after_combat_moves_scenario(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    additional_pending_moves: list[PendingMove] | None = None,
) -> GameState:
    """
    Deepcopy state, optionally append extra combat_move pending entries, then apply every
    combat_move pending move (same ordering, charge-through inference, and captures as phase end).
    Use for AI/simulation: forecast outcome of queued combat moves plus hypothetical candidates.
    """
    state_copy = deepcopy(state)
    if additional_pending_moves:
        state_copy.pending_moves = list(state_copy.pending_moves or []) + list(
            additional_pending_moves
        )
    _apply_pending_moves(
        state_copy, "combat_move", unit_defs, territory_defs, faction_defs
    )
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
    
    state.pending_moves.pop(move_index)
    return state, events


def _handle_mobilize_units(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition],
    port_defs: dict[str, PortDefinition],
) -> tuple[GameState, list[GameEvent]]:
    """
    Queue a mobilization: add to pending_mobilizations and deduct from faction_purchased_units.
    Actual deployment happens at end of mobilization phase.
    Land: destination must be an owned territory with a standing camp, a port, or a home territory for the unit type (cap 1 per unit type per phase).
    Naval: destination must be a sea zone adjacent to an owned port (boats are produced in the adjacent sea).
    """
    events: list[GameEvent] = []
    faction_id = action.faction
    destination_id = action.payload.get("destination")
    units_to_mobilize = list(action.payload.get("units", []))

    if not units_to_mobilize:
        raise ValueError("No units specified to mobilize")

    if not faction_owns_capital(state, faction_id, faction_defs):
        raise ValueError(f"Cannot mobilize units: {faction_id}'s capital has been captured")

    dest_territory = state.territories.get(destination_id)
    dest_def = territory_defs.get(destination_id)
    if not dest_territory or not dest_def:
        raise ValueError(f"Territory {destination_id} does not exist")

    all_naval = True
    all_land = True
    for unit_request in units_to_mobilize:
        ud = unit_defs.get(unit_request.get("unit_id"))
        is_naval = ud and (getattr(ud, "archetype", "") == "naval" or "naval" in getattr(ud, "tags", []))
        if is_naval:
            all_land = False
        else:
            all_naval = False
    if not all_naval and not all_land:
        raise ValueError("Do not mix naval and land units in one mobilization")

    if all_naval:
        if not _sea_zone_adjacent_to_owned_port(state, destination_id, faction_id, port_defs, territory_defs):
            raise ValueError(
                f"Naval units can only mobilize to a sea zone adjacent to a port you own; {destination_id} is not valid"
            )
        # Shared pool: each port adjacent to this sea zone must have room
        total_mobilizing = sum(u.get("count", 0) for u in units_to_mobilize)
        sea_def = territory_defs.get(destination_id)
        if sea_def and getattr(sea_def, "terrain_type", "").lower() == "sea":
            for adj_id in sea_def.adjacent:
                if state.territories.get(adj_id, TerritoryState(None)).owner != faction_id:
                    continue
                if not _territory_has_port(adj_id, port_defs):
                    continue
                port_power_val = territory_defs.get(adj_id).produces.get("power", 0) if territory_defs.get(adj_id) else 0
                total_for_port = _total_pending_mobilization_to_port(state, adj_id, territory_defs, port_defs)
                if total_for_port + total_mobilizing > port_power_val:
                    raise ValueError(
                        f"Cannot mobilize {total_mobilizing} naval to {destination_id}: "
                        f"port {adj_id} shared pool would exceed capacity ({total_for_port + total_mobilizing} > {port_power_val})"
                    )
    else:
        if dest_territory.owner != faction_id:
            raise ValueError(f"Cannot mobilize to {destination_id}: not owned by {faction_id}")
        has_camp = _territory_has_standing_camp(state, destination_id, camp_defs)
        is_home_for = {}
        for uid, ud in unit_defs.items():
            if getattr(ud, "faction", None) != faction_id:
                continue
            if has_unit_special(ud, "home") and destination_id in _home_territory_ids(ud):
                is_home_for[uid] = True
        # Land never deploys to a port *as* a port (naval pool). Camps OK; home units OK (e.g. Corsair → Umbar even though Umbar has a port).
        if not has_camp:
            for ureq in units_to_mobilize:
                uid = ureq.get("unit_id")
                if not is_home_for.get(uid):
                    raise ValueError(
                        f"Land units can only mobilize to a standing camp or a home territory for that unit type; "
                        f"{destination_id} is not valid for {uid}"
                    )
        power_production = dest_def.produces.get("power", 0)
        total_mobilizing = sum(u.get("count", 0) for u in units_to_mobilize)
        if has_camp:
            already_pending = sum(
                sum(item.get("count", 0) for item in pm.units)
                for pm in state.pending_mobilizations
                if pm.destination == destination_id
            )
            if already_pending + total_mobilizing > power_production:
                raise ValueError(
                    f"Cannot mobilize {total_mobilizing} more units to {destination_id}: "
                    f"already {already_pending} pending, capacity is {power_production}"
                )
        else:
            # Home deployment (including port + home, e.g. Umbar for Corsair): single unit type, cap 1 per type per phase
            unit_ids_in_batch = {u.get("unit_id") for u in units_to_mobilize}
            if len(unit_ids_in_batch) != 1:
                raise ValueError(
                    "When mobilizing to a home territory, all units must be the same type"
                )
            unit_id = next(iter(unit_ids_in_batch))
            if not is_home_for.get(unit_id):
                raise ValueError(
                    f"{destination_id} is not a home territory for {unit_id}"
                )
            already_pending = sum(
                u.get("count", 0)
                for pm in state.pending_mobilizations
                if pm.destination == destination_id
                for u in pm.units
                if u.get("unit_id") == unit_id
            )
            if already_pending + total_mobilizing > 1:
                raise ValueError(
                    f"At most 1 {unit_id} can be mobilized to home territory {destination_id} per phase (already {already_pending} pending)"
                )

    purchased_units = state.faction_purchased_units.get(faction_id, [])

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
        dest_key = resolve_territory_key_in_state(
            state, str(pending.destination or "").strip(), territory_defs
        )
        dest_territory = state.territories.get(dest_key)
        if not dest_territory:
            continue
        dest_def = territory_defs.get(dest_key)
        had_hostile_naval_before = False
        if dest_def and _is_sea_zone(dest_def):
            had_hostile_naval_before = _sea_zone_has_hostile_enemy_boats(
                state, dest_key, faction_id, unit_defs, faction_defs, territory_defs
            )
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
                if had_hostile_naval_before and _is_naval_unit(unit_defs.get(u.unit_id)):
                    nm = list(getattr(state, "naval_mobilization_intruder_instance_ids", None) or [])
                    if u.instance_id not in nm:
                        nm.append(u.instance_id)
                    state.naval_mobilization_intruder_instance_ids = nm
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
    sea_zone_id = action.payload.get("sea_zone_id")
    dice_rolls = action.payload.get("dice_rolls", {})
    fuse_bomb = action.payload.get("fuse_bomb", True)
    if not isinstance(fuse_bomb, bool):
        fuse_bomb = True

    # Validate no active combat
    if state.active_combat is not None:
        raise ValueError("Cannot initiate combat while another combat is active")

    # Get territory (combat target = land)
    territory = state.territories.get(territory_id)
    if not territory:
        raise ValueError(f"Invalid territory: {territory_id}")

    # Validate territory is not owned by attacker (they're attacking it)
    if territory.owner == attacker_faction:
        raise ValueError(f"Cannot attack own territory {territory_id}")

    attacker_alliance = getattr(faction_defs.get(attacker_faction), "alliance", None)
    attacker_territory = territory  # Where attacker units live; for sea raid = sea zone
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        sea_def = territory_defs.get(sea_zone_id)
        if not sea_zone or not sea_def or getattr(sea_def, "terrain_type", "").lower() != "sea":
            raise ValueError(f"Invalid sea zone: {sea_zone_id}")
        # Skip adjacency when this territory was recorded as sea-raided (offload already applied)
        sea_raid_from = getattr(state, "territory_sea_raid_from", None) or {}
        if sea_raid_from.get(territory_id) != sea_zone_id:
            sea_adj = getattr(sea_def, "adjacent", []) or []
            land_adj = getattr(territory_defs.get(territory_id), "adjacent", []) or []
            if territory_id not in sea_adj and sea_zone_id not in land_adj:
                raise ValueError(f"Territory {territory_id} is not adjacent to sea zone {sea_zone_id}")
        attacker_territory = sea_zone
        _normalize_unit_health_for_combat(sea_zone.units, unit_defs)
        _normalize_unit_health_for_combat(territory.units, unit_defs)
        # Sea raid: only land units (passengers) fight; boats stay in sea zone. Naval units cannot attack land.
        # After phase end, land units may already be on territory (offloaded); use them then.
        attacker_units = [
            deepcopy(u) for u in sea_zone.units
            if get_unit_faction(u, unit_defs) == attacker_faction
            and is_land_unit(unit_defs.get(u.unit_id))
            and not _is_naval_unit(unit_defs.get(u.unit_id))
        ]
        if not attacker_units:
            attacker_units = [
                deepcopy(u) for u in territory.units
                if get_unit_faction(u, unit_defs) == attacker_faction
                and is_land_unit(unit_defs.get(u.unit_id))
                and not _is_naval_unit(unit_defs.get(u.unit_id))
            ]
            if attacker_units:
                attacker_territory = territory  # Attackers already offloaded to land
        defender_units = [
            deepcopy(u) for u in territory.units
            if _land_combat_unit_side(u, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "defender"
        ]
        attacker_units.sort(key=lambda u: u.instance_id)
        defender_units.sort(key=lambda u: u.instance_id)
        if not attacker_units:
            raise ValueError(f"No attacking units in sea zone {sea_zone_id} or on territory {territory_id}")
        if not defender_units:
            # Sea raid conquer: empty land; move only land units (passengers) to territory, boats stay in sea zone
            for u in sea_zone.units[:]:
                ud = unit_defs.get(u.unit_id)
                if (get_unit_faction(u, unit_defs) == attacker_faction
                        and is_land_unit(ud)
                        and not _is_naval_unit(ud)):
                    sea_zone.units.remove(u)
                    setattr(u, "loaded_onto", None)
                    territory.units.append(u)
            state.pending_captures[territory_id] = attacker_faction
            landed_ids = [u.instance_id for u in territory.units if get_unit_faction(u, unit_defs) == attacker_faction]
            events.append(combat_started(territory_id, attacker_faction, landed_ids, territory.owner or "neutral", []))
            events.append(combat_ended(
                territory_id, "attacker", attacker_faction, territory.owner,
                landed_ids, [], 0,
                outcome="conquer",
                liberated_for=_liberation_beneficiary_if_allied_original(
                    territory_id, territory, attacker_faction, faction_defs, state,
                ),
            ))
            return state, events
    else:
        _normalize_unit_health_for_combat(territory.units, unit_defs)
        attacker_units = []
        defender_units = []
        territory_def = territory_defs.get(territory_id)
        is_naval_combat = territory_def and getattr(territory_def, "terrain_type", "").lower() == "sea"
        for unit in territory.units:
            if is_naval_combat and not participates_in_sea_hex_naval_combat(
                unit, unit_defs.get(unit.unit_id)
            ):
                continue  # Sea combat: ships only; passengers (loaded_onto) do not fight
            side = _land_combat_unit_side(
                unit, attacker_faction, attacker_alliance, unit_defs, faction_defs,
            )
            if side == "attacker":
                attacker_units.append(deepcopy(unit))
            elif side == "defender":
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

    # Clear loaded_naval_must_attack_instance_ids for attacker boats (they are attacking)
    if getattr(state, "loaded_naval_must_attack_instance_ids", []):
        attacker_boat_ids = {
            u.instance_id for u in attacker_units
            if u.instance_id and _is_naval_unit(unit_defs.get(u.unit_id))
        }
        state.loaded_naval_must_attack_instance_ids = [
            bid for bid in state.loaded_naval_must_attack_instance_ids if bid not in attacker_boat_ids
        ]

    if not sea_zone_id:
        tdef_nav = territory_defs.get(territory_id)
        if tdef_nav and getattr(tdef_nav, "terrain_type", "").lower() == "sea":
            in_sea = {u.instance_id for u in territory.units}
            nm = list(getattr(state, "naval_mobilization_intruder_instance_ids", None) or [])
            state.naval_mobilization_intruder_instance_ids = [i for i in nm if i not in in_sea]

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
    sea_raider_att, _ = compute_sea_raider_stat_modifiers(
        attacker_units, unit_defs, is_sea_raid=bool(sea_zone_id)
    )
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att, sea_raider_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    # Stealth: if EVERY attacker has stealth, they prefire at attack-1 (hits to defenders) and cancel defender archer prefire
    all_attackers_have_stealth = (
        len(attacker_units) > 0
        and all(has_unit_special(unit_defs.get(u.unit_id), "stealth") for u in attacker_units)
    )
    if all_attackers_have_stealth:
        pd_prefire = _prefire_stat_delta(state)
        spec_stealth = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=bool(sea_zone_id),
            archer_prefire_applicable=False,
            stealth_prefire_applicable=True,
        )
        attacker_units_at_start_stealth = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                pd_prefire + attacker_mods.get(u.instance_id, 0), True, attacker_faction,
                territory_def, spec_stealth,
                passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
            )
            for u in attacker_units
        ]
        defender_units_at_start_stealth = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                defender_mods.get(u.instance_id, 0), False, defender_faction,
                territory_def, spec_stealth,
                passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
            )
            for u in defender_units
        ]
        prefire_attacker_rolls = dice_rolls.get("attacker", [])
        round_result = resolve_stealth_prefire(
            attacker_units, defender_units, unit_defs, prefire_attacker_rolls,
            stat_modifiers_attacker_extra=attacker_mods,
            prefire_penalty_delta=pd_prefire,
        )
        stealth_stat_modifiers = {
            u.instance_id: pd_prefire + attacker_mods.get(u.instance_id, 0) for u in attacker_units
        }
        attacker_dice_grouped_stealth = group_dice_by_stat(
            attacker_units, prefire_attacker_rolls, unit_defs, is_attacker=True,
            stat_modifiers=stealth_stat_modifiers,
        )
        prefire_log_entry = CombatRoundResult(
            round_number=0,
            attacker_rolls=prefire_attacker_rolls,
            defender_rolls=[],
            attacker_hits=round_result.attacker_hits,
            defender_hits=0,
            attacker_casualties=[],
            defender_casualties=round_result.defender_casualties,
            attackers_remaining=len(attacker_units),
            defenders_remaining=len(defender_units),
            is_stealth_prefire=True,
        )
        events.append(combat_round_resolved(
            territory_id, 0,
            attacker_dice_grouped_stealth, {},
            round_result.attacker_hits, 0,
            [], round_result.defender_casualties,
            [], round_result.defender_wounded,
            len(attacker_units), len(defender_units),
            attacker_units_at_start_stealth,
            defender_units_at_start_stealth,
            is_stealth_prefire=True,
        ))
        for casualty_id in round_result.defender_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, defender_faction, territory_id, "combat"))
        passenger_att = _remove_casualties(attacker_territory, round_result.attacker_casualties, unit_defs)
        passenger_def = _remove_casualties(territory, round_result.defender_casualties, unit_defs)
        for pid in passenger_att:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, attacker_faction, territory_id, "combat"))
        for pid in passenger_def:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, defender_faction, territory_id, "combat"))
        _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)

        if round_result.defenders_eliminated:
            end_round_result = RoundResult(
                attacker_hits=round_result.attacker_hits,
                defender_hits=0,
                attacker_casualties=[],
                defender_casualties=round_result.defender_casualties,
                attacker_wounded=[],
                defender_wounded=round_result.defender_wounded,
                surviving_attacker_ids=attacker_instance_ids,
                surviving_defender_ids=[u.instance_id for u in defender_units],
                attackers_eliminated=False,
                defenders_eliminated=True,
            )
            state, end_events = _resolve_combat_end(
                state, attacker_faction, territory_id,
                end_round_result, [prefire_log_entry], territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=attacker_instance_ids,
                initial_defender_instance_ids=defender_instance_ids,
            )
            events.extend(end_events)
            return state, events

        state.active_combat = ActiveCombat(
            attacker_faction=attacker_faction,
            territory_id=territory_id,
            attacker_instance_ids=attacker_instance_ids,
            round_number=0,
            combat_log=[prefire_log_entry],
            attackers_have_rolled=False,
            sea_zone_id=sea_zone_id,
            casualty_order_attacker="best_unit",
            must_conquer=False,
            initial_attacker_instance_ids=attacker_instance_ids,
            initial_defender_instance_ids=defender_instance_ids,
            cumulative_hits_received_by_attacker=0,
            cumulative_hits_received_by_defender=round_result.attacker_hits,
            fuse_bomb=fuse_bomb,
        )
        return state, events

    combat_log_prefix: list[CombatRoundResult] = []

    # Check if defender has archer special -> prefire before round 1
    defender_archer_units = [
        u for u in defender_units
        if archer_prefire_eligible(unit_defs.get(u.unit_id))
    ]
    defender_casualty_order = getattr(state, "territory_defender_casualty_order", {}).get(territory_id, "best_unit")

    # Dedicated siegeworks round first when applicable (independent of archer prefire; precedes it).
    defender_territory_is_stronghold_early = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    territory_is_sea_for_sh = _is_sea_zone(territory_defs.get(territory_id))
    defender_stronghold_hp_cur_sw: int | None = None
    if not territory_is_sea_for_sh and territory_def:
        base_hp_sw = getattr(territory_def, "stronghold_base_health", 0) or 0
        if getattr(territory_def, "is_stronghold", False) and base_hp_sw > 0:
            cur_sw = getattr(territory, "stronghold_current_health", None)
            defender_stronghold_hp_cur_sw = cur_sw if cur_sw is not None else base_hp_sw
    siegework_att_dice_init, siegework_def_dice_init = get_siegework_dice_counts(
        attacker_units, defender_units, unit_defs, defender_territory_is_stronghold_early,
        defender_stronghold_hp=defender_stronghold_hp_cur_sw,
        fuse_bomb=fuse_bomb,
    )
    siege_needed_at_start = (
        siegework_att_dice_init > 0
        or siegework_def_dice_init > 0
    )
    _initiate_ladder_infantry_ids: list[str] = []
    _initiate_ladder_equipment_count = 0

    if siege_needed_at_start:
        _, _, att_attack_ov_siege = get_attacker_effective_dice_and_bombikazi_self_destruct(
            attacker_units, unit_defs,
            use_paired_fused_siegework_rules=fuse_bomb,
        )
        attacker_id_to_type_health_sw = {u.instance_id: (u.unit_id, u.base_health) for u in attacker_units}
        defender_id_to_type_health_sw = {u.instance_id: (u.unit_id, u.base_health) for u in defender_units}

        def hits_by_unit_type_sw(casualties: list[str], wounded: list[str], id_map: dict) -> dict[str, int]:
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

        spec_sw = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=bool(sea_zone_id),
            archer_prefire_applicable=False,
            ram_applicable=True,
        )
        disp_att_sw = get_siegework_round_attacker_display_units(
            attacker_units, unit_defs, defender_territory_is_stronghold_early,
            defender_stronghold_hp=defender_stronghold_hp_cur_sw,
            fuse_bomb=fuse_bomb,
        )
        disp_def_sw = get_siegework_round_defender_display_units(defender_units, unit_defs)
        # Siegeworks UI: only units that belong on shelves this round (rolling + ladder gear + ram when
        # applicable, etc.) — not the full battle roster.
        attacker_units_at_start_sw = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                attacker_mods.get(u.instance_id, 0), True, attacker_faction,
                territory_def, spec_sw,
                attacker_effective_attack_override=att_attack_ov_siege,
                passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
            )
            for u in disp_att_sw
        ]
        defender_units_at_start_sw = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                defender_mods.get(u.instance_id, 0), False, defender_faction,
                territory_def, spec_sw,
                passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
            )
            for u in disp_def_sw
        ]
        att_rolling_sw = get_siegework_attacker_rolling_units(
            attacker_units, unit_defs, defender_territory_is_stronghold_early,
            defender_stronghold_hp=defender_stronghold_hp_cur_sw,
            fuse_bomb=fuse_bomb,
        )
        def_sw_units = [u for u in defender_units if _is_siegework_unit(unit_defs.get(u.unit_id))]
        round_result_sw, defender_stronghold_hp_after_sw, ladder_count_sw = resolve_siegeworks_round(
            attacker_units, defender_units, unit_defs, dice_rolls,
            stat_modifiers_attacker=attacker_mods or None,
            stat_modifiers_defender=defender_mods or None,
            casualty_order_attacker="best_unit",
            casualty_order_defender=defender_casualty_order,
            defender_stronghold_hp=defender_stronghold_hp_cur_sw,
            defender_territory_is_stronghold=defender_territory_is_stronghold_early,
            fuse_bomb=fuse_bomb,
        )
        ladder_ids_sw = get_ladder_infantry_instance_ids(attacker_units, unit_defs)
        _initiate_ladder_infantry_ids = ladder_ids_sw
        _initiate_ladder_equipment_count = ladder_count_sw
        if defender_stronghold_hp_after_sw is not None:
            territory.stronghold_current_health = defender_stronghold_hp_after_sw
        siege_att_rolls = dice_rolls.get("attacker", [])
        siege_def_rolls = dice_rolls.get("defender", [])
        siege_att_dice_grouped = group_dice_by_stat(
            att_rolling_sw, siege_att_rolls, unit_defs, is_attacker=True,
            stat_modifiers=attacker_mods or None,
        ) if att_rolling_sw else {}
        siege_att_dice_split_sw = (
            group_siegework_attacker_dice_ram_and_flex(
                att_rolling_sw, siege_att_rolls, unit_defs,
                stat_modifiers=attacker_mods or None,
            )
            if att_rolling_sw else None
        )
        siege_def_dice_grouped = group_dice_by_stat(
            def_sw_units, siege_def_rolls, unit_defs, is_attacker=False,
            stat_modifiers=defender_mods or None,
        ) if def_sw_units else {}
        attacker_hits_by_type_sw = hits_by_unit_type_sw(
            round_result_sw.attacker_casualties, round_result_sw.attacker_wounded, attacker_id_to_type_health_sw
        )
        defender_hits_by_type_sw = hits_by_unit_type_sw(
            round_result_sw.defender_casualties, round_result_sw.defender_wounded, defender_id_to_type_health_sw
        )
        events.append(combat_round_resolved(
            territory_id, 0,
            siege_att_dice_grouped, siege_def_dice_grouped,
            round_result_sw.attacker_hits, round_result_sw.defender_hits,
            round_result_sw.attacker_casualties, round_result_sw.defender_casualties,
            round_result_sw.attacker_wounded, round_result_sw.defender_wounded,
            len(round_result_sw.surviving_attacker_ids), len(round_result_sw.surviving_defender_ids),
            attacker_units_at_start_sw,
            defender_units_at_start_sw,
            attacker_hits_by_unit_type=attacker_hits_by_type_sw,
            defender_hits_by_unit_type=defender_hits_by_type_sw,
            is_siegeworks_round=True,
            attacker_dice_siegework_split=siege_att_dice_split_sw,
        ))
        for casualty_id in round_result_sw.attacker_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, attacker_faction, territory_id, "combat"))
        for casualty_id in round_result_sw.defender_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, defender_faction, territory_id, "combat"))
        passenger_att_sw = _remove_casualties(attacker_territory, round_result_sw.attacker_casualties, unit_defs)
        passenger_def_sw = _remove_casualties(territory, round_result_sw.defender_casualties, unit_defs)
        for pid in passenger_att_sw:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, attacker_faction, territory_id, "combat"))
        for pid in passenger_def_sw:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, defender_faction, territory_id, "combat"))
        _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)
        attacker_units[:] = [u for u in attacker_units if u.instance_id in round_result_sw.surviving_attacker_ids]
        bomb_pair_casualties_sw: list[str] = []
        if fuse_bomb:
            paired_bombikazi_sw, paired_bombs_sw = get_bombikazi_pairing(attacker_units, unit_defs)
            bomb_pair_casualties_sw = list(paired_bombikazi_sw | paired_bombs_sw)
        if bomb_pair_casualties_sw:
            attacker_units[:] = [u for u in attacker_units if u.instance_id not in bomb_pair_casualties_sw]
            passenger_att_bomb_sw = _remove_casualties(attacker_territory, bomb_pair_casualties_sw, unit_defs)
            for iid in bomb_pair_casualties_sw:
                unit_type = iid.split("_")[1] if "_" in iid else "unknown"
                events.append(unit_destroyed(iid, unit_type, attacker_faction, territory_id, "combat"))
            for pid in passenger_att_bomb_sw:
                unit_type = pid.split("_")[1] if "_" in pid else "unknown"
                events.append(unit_destroyed(pid, unit_type, attacker_faction, territory_id, "combat"))
        siege_log_entry = CombatRoundResult(
            round_number=0,
            attacker_rolls=dice_rolls.get("attacker", []),
            defender_rolls=dice_rolls.get("defender", []),
            attacker_hits=round_result_sw.attacker_hits,
            defender_hits=round_result_sw.defender_hits,
            attacker_casualties=round_result_sw.attacker_casualties + bomb_pair_casualties_sw,
            defender_casualties=round_result_sw.defender_casualties,
            attackers_remaining=len(attacker_units),
            defenders_remaining=len(round_result_sw.surviving_defender_ids),
            is_siegeworks_round=True,
        )
        combat_log_prefix = [siege_log_entry]

        if round_result_sw.attackers_eliminated or round_result_sw.defenders_eliminated:
            state, end_events = _resolve_combat_end(
                state, attacker_faction, territory_id,
                round_result_sw, combat_log_prefix, territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=attacker_instance_ids,
                initial_defender_instance_ids=defender_instance_ids,
            )
            events.extend(end_events)
            return state, events
        if len(attacker_units) == 0:
            end_round_result_sw = RoundResult(
                attacker_hits=round_result_sw.attacker_hits,
                defender_hits=round_result_sw.defender_hits,
                attacker_casualties=round_result_sw.attacker_casualties,
                defender_casualties=round_result_sw.defender_casualties,
                attacker_wounded=round_result_sw.attacker_wounded,
                defender_wounded=round_result_sw.defender_wounded,
                surviving_attacker_ids=[],
                surviving_defender_ids=round_result_sw.surviving_defender_ids,
                attackers_eliminated=True,
                defenders_eliminated=round_result_sw.defenders_eliminated,
            )
            state, end_events = _resolve_combat_end(
                state, attacker_faction, territory_id,
                end_round_result_sw, combat_log_prefix, territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=attacker_instance_ids,
                initial_defender_instance_ids=defender_instance_ids,
            )
            events.extend(end_events)
            return state, events

        # Siegework round is finished for this action. Further rounds always use continue_combat
        # (archer prefire if any, then standard rounds) — never resolve round 1 in the same initiate,
        # or siege-only dice_rolls would be reused and defenders could get no rolls incorrectly.
        archer_prefire_follows = bool(defender_archer_units)
        state.active_combat = ActiveCombat(
            attacker_faction=attacker_faction,
            territory_id=territory_id,
            attacker_instance_ids=[u.instance_id for u in attacker_units],
            round_number=0,
            combat_log=combat_log_prefix,
            attackers_have_rolled=False if archer_prefire_follows else True,
            sea_zone_id=sea_zone_id,
            casualty_order_attacker="best_unit",
            must_conquer=False,
            initial_attacker_instance_ids=attacker_instance_ids,
            initial_defender_instance_ids=defender_instance_ids,
            cumulative_hits_received_by_attacker=round_result_sw.defender_hits,
            cumulative_hits_received_by_defender=round_result_sw.attacker_hits,
            ladder_infantry_instance_ids=ladder_ids_sw,
            ladder_equipment_count=ladder_count_sw,
            fuse_bomb=fuse_bomb,
        )
        return state, events

    # Defender archer prefire only when battle did not open with a siegeworks round (no combat_log_prefix).
    if defender_archer_units and not combat_log_prefix:
        # Prefire: only defender archers roll at defense-1 (or defense+0 if manifest disables penalty); hits to attackers only
        archer_prefire_penalty = _prefire_stat_delta(state)
        prefire_defender_rolls = dice_rolls.get("defender", [])
        round_result = resolve_archer_prefire(
            attacker_units, defender_archer_units, unit_defs, prefire_defender_rolls,
            stat_modifiers_defender_extra=defender_mods,
            territory_def=territory_def,
            prefire_penalty_delta=archer_prefire_penalty,
        )
        # Group defender dice for UI (archers at defense-1 or defense+0, merged with terrain)
        archer_stat_modifiers = {
            u.instance_id: archer_prefire_penalty + defender_mods.get(u.instance_id, 0)
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
        spec_archer_prefire = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=bool(sea_zone_id),
            archer_prefire_applicable=True,
        )
        _, _, att_attack_ov_prefire = get_attacker_effective_dice_and_bombikazi_self_destruct(
            attacker_units, unit_defs
        )
        # Full rosters at round start (only archers roll; attackers may take hits with no dice).
        attacker_units_at_start_prefire = [
            _build_round_unit_display(
                u,
                unit_defs.get(u.unit_id),
                attacker_mods.get(u.instance_id, 0),
                True,
                attacker_faction,
                territory_def,
                spec_archer_prefire,
                attacker_effective_attack_override=att_attack_ov_prefire,
                passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
            )
            for u in attacker_units
        ]
        defender_units_at_start_prefire = [
            _build_round_unit_display(
                u,
                unit_defs.get(u.unit_id),
                archer_prefire_penalty + defender_mods.get(u.instance_id, 0),
                False,
                defender_faction,
                territory_def,
                spec_archer_prefire,
                passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
            )
            for u in defender_units
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
        passenger_att = _remove_casualties(attacker_territory, round_result.attacker_casualties, unit_defs)
        passenger_def = _remove_casualties(territory, round_result.defender_casualties, unit_defs)
        for pid in passenger_att:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, attacker_faction, territory_id, "combat"))
        for pid in passenger_def:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, defender_faction, territory_id, "combat"))
        _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)

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
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=[u.instance_id for u in attacker_units],
                initial_defender_instance_ids=defender_instance_ids,
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
            sea_zone_id=sea_zone_id,
            casualty_order_attacker="best_unit",
            must_conquer=False,
            initial_attacker_instance_ids=[u.instance_id for u in attacker_units],
            initial_defender_instance_ids=defender_instance_ids,
            cumulative_hits_received_by_attacker=round_result.defender_hits,
            cumulative_hits_received_by_defender=0,
            fuse_bomb=fuse_bomb,
        )
        return state, events

    # No archers: run round 1 as usual (with terrain modifiers)
    att_effective_dice, att_self_destruct, att_attack_override = get_attacker_effective_dice_and_bombikazi_self_destruct(
        attacker_units, unit_defs,
        use_paired_fused_siegework_rules=True,
    )
    attacker_dice_grouped = group_dice_by_stat(
        attacker_units, dice_rolls.get("attacker", []), unit_defs, is_attacker=True,
        stat_modifiers=attacker_mods or None,
        effective_dice_override=att_effective_dice,
        effective_stat_override=att_attack_override or None,
        exclude_archetypes_from_rolling={"siegework"},
    )
    defender_dice_grouped = group_dice_by_stat(
        defender_units, dice_rolls.get("defender", []), unit_defs, is_attacker=False,
        stat_modifiers=defender_mods or None,
        exclude_archetypes_from_rolling={"siegework"},
    )

    # Build instance_id -> (unit_id, base_health) before combat modifies units (for hit badges)
    attacker_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in attacker_units}
    defender_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in defender_units}

    spec_round = compute_battle_specials_and_modifiers(
        attacker_units,
        defender_units,
        territory_def,
        unit_defs,
        is_sea_raid=bool(sea_zone_id),
        archer_prefire_applicable=False,
    )
    # Units at start of round for frontend (before combat modifies anything)
    attacker_units_at_start_init = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            attacker_mods.get(u.instance_id, 0), True, attacker_faction,
            territory_def, spec_round,
            attacker_effective_attack_override=att_attack_override,
            passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
        )
        for u in attacker_units
    ]
    defender_units_at_start_init = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            defender_mods.get(u.instance_id, 0), False, defender_faction,
            territory_def, spec_round,
            passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
        )
        for u in defender_units
    ]

    territory_is_sea = _is_sea_zone(territory_defs.get(territory_id))
    is_naval_combat_attacker = _is_naval_combat_attacker_hit_rules(
        attacker_units, sea_zone_id, territory_is_sea, unit_defs,
    )
    is_naval_combat_defender = territory_is_sea
    # Stronghold soaks attacker hits first (land strongholds only)
    defender_stronghold_hp: int | None = None
    if not territory_is_sea and territory_def:
        base_hp = getattr(territory_def, "stronghold_base_health", 0) or 0
        if getattr(territory_def, "is_stronghold", False) and base_hp > 0:
            ts = territory
            current = getattr(ts, "stronghold_current_health", None)
            defender_stronghold_hp = current if current is not None else base_hp
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    round_result, defender_stronghold_hp_after = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
        defender_hits_override=action.payload.get("terror_final_defender_hits"),
        attacker_effective_dice_override=att_effective_dice,
        attacker_effective_attack_override=att_attack_override or None,
        bombikazi_self_destruct_ids=att_self_destruct,
        casualty_order_attacker="best_unit",
        casualty_order_defender=defender_casualty_order,
        must_conquer=False,
        is_naval_combat_attacker=is_naval_combat_attacker,
        is_naval_combat_defender=is_naval_combat_defender,
        defender_stronghold_hp=defender_stronghold_hp,
        defender_territory_is_stronghold=defender_territory_is_stronghold,
        exclude_archetypes_from_rolling=["siegework"],
        attacker_ladder_instance_ids=set(_initiate_ladder_infantry_ids),
    )
    if defender_stronghold_hp_after is not None:
        territory.stronghold_current_health = defender_stronghold_hp_after

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
        terror_reroll_count=action.payload.get("terror_reroll_count"),
    ))

    for casualty_id in round_result.attacker_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, attacker_faction, territory_id, "combat"))
    for casualty_id in round_result.defender_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, defender_faction, territory_id, "combat"))

    passenger_att = _remove_casualties(attacker_territory, round_result.attacker_casualties, unit_defs)
    passenger_def = _remove_casualties(territory, round_result.defender_casualties, unit_defs)
    for pid in passenger_att:
        unit_type = pid.split("_")[1] if "_" in pid else "unknown"
        events.append(unit_destroyed(pid, unit_type, attacker_faction, territory_id, "combat"))
    for pid in passenger_def:
        unit_type = pid.split("_")[1] if "_" in pid else "unknown"
        events.append(unit_destroyed(pid, unit_type, defender_faction, territory_id, "combat"))
    _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)

    full_combat_log_init = combat_log_prefix + [combat_log_entry]
    prefix_cum_att = sum(r.defender_hits for r in combat_log_prefix)
    prefix_cum_def = sum(r.attacker_hits for r in combat_log_prefix)

    if round_result.attackers_eliminated or round_result.defenders_eliminated:
        state, end_events = _resolve_combat_end(
            state, attacker_faction, territory_id,
            round_result, full_combat_log_init, territory_defs, unit_defs,
            faction_defs,
            sea_zone_id=sea_zone_id,
            initial_attacker_instance_ids=attacker_instance_ids,
            initial_defender_instance_ids=defender_instance_ids,
        )
        events.extend(end_events)
        return state, events

    state.active_combat = ActiveCombat(
        attacker_faction=attacker_faction,
        territory_id=territory_id,
        attacker_instance_ids=round_result.surviving_attacker_ids,
        round_number=1,
        combat_log=full_combat_log_init,
        sea_zone_id=sea_zone_id,
        casualty_order_attacker="best_unit",
        must_conquer=False,
        initial_attacker_instance_ids=attacker_instance_ids,
        initial_defender_instance_ids=defender_instance_ids,
        cumulative_hits_received_by_attacker=prefix_cum_att + round_result.defender_hits,
        cumulative_hits_received_by_defender=prefix_cum_def + round_result.attacker_hits,
        ladder_infantry_instance_ids=_initiate_ladder_infantry_ids,
        ladder_equipment_count=_initiate_ladder_equipment_count,
        fuse_bomb=fuse_bomb,
    )
    return state, events


def _handle_continue_combat(
    state: GameState,
    action: Action,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
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
    sea_zone_id = getattr(combat, "sea_zone_id", None)
    fuse_bomb = getattr(combat, "fuse_bomb", True)
    if not isinstance(fuse_bomb, bool):
        fuse_bomb = True
    had_siegeworks_in_battle = any(
        getattr(r, "is_siegeworks_round", False) for r in (combat.combat_log or [])
    )
    use_paired_fused_siegework_rules = (not had_siegeworks_in_battle) or fuse_bomb

    # Get the contested territory (land) and where attackers live (same or sea zone for sea raid; after offload, they're on territory)
    territory = state.territories[combat.territory_id]
    surviving_attacker_ids = set(combat.attacker_instance_ids)
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        land_attackers_on_land = [
            u.instance_id for u in territory.units
            if u.instance_id in surviving_attacker_ids
            and is_land_unit(unit_defs.get(u.unit_id))
            and not _is_naval_unit(unit_defs.get(u.unit_id))
        ]
        if land_attackers_on_land:
            attacker_territory = territory
        else:
            in_sea = [
                u for u in (sea_zone.units if sea_zone else [])
                if u.instance_id in surviving_attacker_ids
            ]
            attacker_territory = sea_zone if in_sea else territory
    else:
        attacker_territory = territory

    # Normalize multi-HP unit health from unit_defs (fixes legacy/corrupt state)
    _normalize_unit_health_for_combat(attacker_territory.units, unit_defs)
    _normalize_unit_health_for_combat(territory.units, unit_defs)

    # Separate attackers and defenders — same rules as initiate_combat (not merely "not in attacker ids")
    attacker_faction = combat.attacker_faction
    attacker_alliance = getattr(faction_defs.get(attacker_faction), "alliance", None)
    attacker_units = sorted(
        [
            deepcopy(u) for u in attacker_territory.units
            if u.instance_id in surviving_attacker_ids
            and _land_combat_unit_side(u, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "attacker"
        ],
        key=lambda u: u.instance_id,
    )
    defender_units = sorted(
        [
            deepcopy(u) for u in territory.units
            if _land_combat_unit_side(u, attacker_faction, attacker_alliance, unit_defs, faction_defs) == "defender"
        ],
        key=lambda u: u.instance_id,
    )

    territory_def = territory_defs.get(combat.territory_id)
    if territory_def and _is_sea_zone(territory_def):
        attacker_units = [
            u for u in attacker_units
            if participates_in_sea_hex_naval_combat(u, unit_defs.get(u.unit_id))
        ]
        defender_units = [
            u for u in defender_units
            if participates_in_sea_hex_naval_combat(u, unit_defs.get(u.unit_id))
        ]

    defender_faction = territory.owner or (get_unit_faction(defender_units[0], unit_defs) if defender_units else "neutral")

    # Terrain + anti-cavalry + captain bonuses (merged; recomputed every round)
    terrain_att, terrain_def = compute_terrain_stat_modifiers(
        territory_def, attacker_units, defender_units, unit_defs
    )
    anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    captain_att, captain_def = compute_captain_stat_modifiers(
        attacker_units, defender_units, unit_defs
    )
    sea_raider_att, _ = compute_sea_raider_stat_modifiers(
        attacker_units, unit_defs, is_sea_raid=bool(sea_zone_id)
    )
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att, sea_raider_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    att_effective_dice, att_self_destruct, att_attack_override = get_attacker_effective_dice_and_bombikazi_self_destruct(
        attacker_units, unit_defs,
        use_paired_fused_siegework_rules=use_paired_fused_siegework_rules,
    )
    # Re-evaluate which infantry are currently "on ladders" for this round.
    # Ladder equipment (siegeworks) can be destroyed between rounds, so the
    # available capacity (and thus which climbers are laddered) can change.
    ladder_ids_list_combat = get_ladder_infantry_instance_ids(attacker_units, unit_defs)
    ladder_ids_combat = set(ladder_ids_list_combat)
    combat.ladder_infantry_instance_ids = ladder_ids_list_combat
    combat.ladder_equipment_count = len([
        u for u in attacker_units
        if _is_siegework_unit(unit_defs.get(u.unit_id))
        and has_unit_special(unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_LADDER)
    ])

    if ladder_ids_combat:
        sort_attackers_for_ladder_dice_order(
            attacker_units, unit_defs, ladder_ids_combat,
            attacker_mods, att_attack_override or None,
        )
        attacker_dice_grouped = group_attacker_dice_with_ladder_segments(
            attacker_units, dice_rolls.get("attacker", []), unit_defs, ladder_ids_combat,
            stat_modifiers=attacker_mods or None,
            effective_dice_override=att_effective_dice,
            effective_stat_override=att_attack_override or None,
            exclude_archetypes_from_rolling={"siegework"},
        )
    else:
        attacker_dice_grouped = group_dice_by_stat(
            attacker_units, dice_rolls.get("attacker", []), unit_defs, is_attacker=True,
            stat_modifiers=attacker_mods or None,
            effective_dice_override=att_effective_dice,
            effective_stat_override=att_attack_override or None,
            exclude_archetypes_from_rolling={"siegework"},
        )
    defender_dice_grouped = group_dice_by_stat(
        defender_units, dice_rolls.get("defender", []), unit_defs, is_attacker=False,
        stat_modifiers=defender_mods or None,
        exclude_archetypes_from_rolling={"siegework"},
    )

    # Build instance_id -> (unit_id, base_health) before combat modifies units
    attacker_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in attacker_units}
    defender_id_to_type_health = {u.instance_id: (u.unit_id, u.base_health) for u in defender_units}

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

    spec_continue = compute_battle_specials_and_modifiers(
        attacker_units,
        defender_units,
        territory_def,
        unit_defs,
        is_sea_raid=bool(sea_zone_id),
        archer_prefire_applicable=False,
    )
    # Units at start of round for frontend (before combat modifies anything)
    attacker_units_at_start = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            attacker_mods.get(u.instance_id, 0), True, combat.attacker_faction,
            territory_def, spec_continue,
            attacker_effective_attack_override=att_attack_override,
            passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
        )
        for u in attacker_units
    ]
    defender_units_at_start = [
        _build_round_unit_display(
            u, unit_defs.get(u.unit_id),
            defender_mods.get(u.instance_id, 0), False, defender_faction,
            territory_def, spec_continue,
            passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
        )
        for u in defender_units
    ]

    # Attacker may update casualty order and must_conquer before this round
    if "casualty_order" in action.payload and action.payload["casualty_order"] in ("best_unit", "best_attack"):
        combat.casualty_order_attacker = action.payload["casualty_order"]
    if "must_conquer" in action.payload and isinstance(action.payload["must_conquer"], bool):
        combat.must_conquer = action.payload["must_conquer"]
    defender_casualty_order = getattr(state, "territory_defender_casualty_order", {}).get(combat.territory_id, "best_unit")
    territory_is_sea = _is_sea_zone(territory_defs.get(combat.territory_id))
    is_naval_combat_attacker = _is_naval_combat_attacker_hit_rules(
        attacker_units, sea_zone_id, territory_is_sea, unit_defs,
    )
    is_naval_combat_defender = territory_is_sea
    # Stronghold soaks attacker hits first (land strongholds only)
    defender_stronghold_hp_cur: int | None = None
    if not territory_is_sea and territory_def:
        base_hp = getattr(territory_def, "stronghold_base_health", 0) or 0
        if getattr(territory_def, "is_stronghold", False) and base_hp > 0:
            ts = territory
            current = getattr(ts, "stronghold_current_health", None)
            defender_stronghold_hp_cur = current if current is not None else base_hp

    # Dedicated siegeworks round: siegework (excl. ladder) + ram attackers vs stronghold; between prefire and round 1
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    siegework_att_dice, siegework_def_dice = get_siegework_dice_counts(
        attacker_units, defender_units, unit_defs, defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp_cur,
        fuse_bomb=fuse_bomb,
    )
    siegeworks_pending = (
        combat.round_number == 0
        and not any(getattr(r, "is_siegeworks_round", False) for r in combat.combat_log)
        and (
            siegework_att_dice > 0
            or siegework_def_dice > 0
        )
    )
    if siegeworks_pending:
        spec_siege_ram = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=bool(sea_zone_id),
            archer_prefire_applicable=False,
            ram_applicable=True,
        )
        att_rolling = get_siegework_attacker_rolling_units(
            attacker_units, unit_defs, defender_territory_is_stronghold,
            defender_stronghold_hp=defender_stronghold_hp_cur,
            fuse_bomb=fuse_bomb,
        )
        def_sw = [u for u in defender_units if _is_siegework_unit(unit_defs.get(u.unit_id))]
        # Run siegeworks round with provided dice (client sends only siegework unit dice)
        round_result, defender_stronghold_hp_after, ladder_count = resolve_siegeworks_round(
            attacker_units, defender_units, unit_defs, dice_rolls,
            stat_modifiers_attacker=attacker_mods or None,
            stat_modifiers_defender=defender_mods or None,
            casualty_order_attacker=combat.casualty_order_attacker,
            casualty_order_defender=defender_casualty_order,
            defender_stronghold_hp=defender_stronghold_hp_cur,
            defender_territory_is_stronghold=defender_territory_is_stronghold,
            fuse_bomb=fuse_bomb,
        )
        combat.ladder_infantry_instance_ids = get_ladder_infantry_instance_ids(
            attacker_units, unit_defs,
        )
        combat.ladder_equipment_count = ladder_count
        if defender_stronghold_hp_after is not None:
            territory.stronghold_current_health = defender_stronghold_hp_after
        # Build dice grouped for event (siegework units only; use pre-round lists)
        siege_att_rolls = dice_rolls.get("attacker", [])
        siege_def_rolls = dice_rolls.get("defender", [])
        siege_att_dice_grouped = group_dice_by_stat(
            att_rolling, siege_att_rolls, unit_defs, is_attacker=True,
            stat_modifiers=attacker_mods or None,
        ) if att_rolling else {}
        siege_att_dice_split_continue = (
            group_siegework_attacker_dice_ram_and_flex(
                att_rolling, siege_att_rolls, unit_defs,
                stat_modifiers=attacker_mods or None,
            )
            if att_rolling else None
        )
        siege_def_dice_grouped = group_dice_by_stat(
            def_sw, siege_def_rolls, unit_defs, is_attacker=False,
            stat_modifiers=defender_mods or None,
        ) if def_sw else {}
        attacker_hits_by_type_sw = hits_by_unit_type(
            round_result.attacker_casualties, round_result.attacker_wounded, attacker_id_to_type_health
        )
        defender_hits_by_type_sw = hits_by_unit_type(
            round_result.defender_casualties, round_result.defender_wounded, defender_id_to_type_health
        )
        disp_att_siege = get_siegework_round_attacker_display_units(
            attacker_units, unit_defs, defender_territory_is_stronghold,
            defender_stronghold_hp=defender_stronghold_hp_cur,
            fuse_bomb=fuse_bomb,
        )
        disp_def_siege = get_siegework_round_defender_display_units(defender_units, unit_defs)
        attacker_units_at_start_siege_evt = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                attacker_mods.get(u.instance_id, 0), True, combat.attacker_faction,
                territory_def, spec_siege_ram,
                attacker_effective_attack_override=att_attack_override,
                passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
            )
            for u in disp_att_siege
        ]
        defender_units_at_start_siege_evt = [
            _build_round_unit_display(
                u, unit_defs.get(u.unit_id),
                defender_mods.get(u.instance_id, 0), False, defender_faction,
                territory_def, spec_siege_ram,
                passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
            )
            for u in disp_def_siege
        ]
        events.append(combat_round_resolved(
            combat.territory_id, 0,
            siege_att_dice_grouped, siege_def_dice_grouped,
            round_result.attacker_hits, round_result.defender_hits,
            round_result.attacker_casualties, round_result.defender_casualties,
            round_result.attacker_wounded, round_result.defender_wounded,
            len(round_result.surviving_attacker_ids), len(round_result.surviving_defender_ids),
            attacker_units_at_start_siege_evt,
            defender_units_at_start_siege_evt,
            attacker_hits_by_unit_type=attacker_hits_by_type_sw,
            defender_hits_by_unit_type=defender_hits_by_type_sw,
            is_siegeworks_round=True,
            attacker_dice_siegework_split=siege_att_dice_split_continue,
        ))
        for casualty_id in round_result.attacker_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
        for casualty_id in round_result.defender_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, defender_faction, combat.territory_id, "combat"))
        passenger_att = _remove_casualties(attacker_territory, round_result.attacker_casualties, unit_defs)
        passenger_def = _remove_casualties(territory, round_result.defender_casualties, unit_defs)
        for pid in passenger_att:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
        for pid in passenger_def:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, defender_faction, combat.territory_id, "combat"))
        _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)
        # "bomb" tag: paired bomb + bombikazi destroyed after siegeworks when fuse_bomb
        attacker_units[:] = [u for u in attacker_units if u.instance_id in round_result.surviving_attacker_ids]
        bomb_pair_casualties: list[str] = []
        if fuse_bomb:
            paired_bombikazi, paired_bombs = get_bombikazi_pairing(attacker_units, unit_defs)
            bomb_pair_casualties = list(paired_bombikazi | paired_bombs)
        if bomb_pair_casualties:
            attacker_units[:] = [u for u in attacker_units if u.instance_id not in bomb_pair_casualties]
            passenger_att_bomb = _remove_casualties(attacker_territory, bomb_pair_casualties, unit_defs)
            for iid in bomb_pair_casualties:
                unit_type = iid.split("_")[1] if "_" in iid else "unknown"
                events.append(unit_destroyed(iid, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
            for pid in passenger_att_bomb:
                unit_type = pid.split("_")[1] if "_" in pid else "unknown"
                events.append(unit_destroyed(pid, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
        siege_log_entry = CombatRoundResult(
            round_number=0,
            attacker_rolls=dice_rolls.get("attacker", []),
            defender_rolls=dice_rolls.get("defender", []),
            attacker_hits=round_result.attacker_hits,
            defender_hits=round_result.defender_hits,
            attacker_casualties=round_result.attacker_casualties + bomb_pair_casualties,
            defender_casualties=round_result.defender_casualties,
            attackers_remaining=len(attacker_units),
            defenders_remaining=len(round_result.surviving_defender_ids),
            is_siegeworks_round=True,
        )
        combat.combat_log.append(siege_log_entry)
        combat.cumulative_hits_received_by_attacker += round_result.defender_hits
        combat.cumulative_hits_received_by_defender += round_result.attacker_hits
        combat.attacker_instance_ids = [u.instance_id for u in attacker_units]
        if round_result.attackers_eliminated or round_result.defenders_eliminated:
            state, end_events = _resolve_combat_end(
                state, combat.attacker_faction, combat.territory_id,
                round_result, combat.combat_log, territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=combat.initial_attacker_instance_ids or None,
                initial_defender_instance_ids=combat.initial_defender_instance_ids or None,
            )
            events.extend(end_events)
            state.active_combat = None
            return state, events
        if len(combat.attacker_instance_ids) == 0:
            # All remaining attackers were bomb + paired bombikazi (self-destructed after siegeworks)
            end_round_result = RoundResult(
                attacker_hits=round_result.attacker_hits,
                defender_hits=round_result.defender_hits,
                attacker_casualties=round_result.attacker_casualties,
                defender_casualties=round_result.defender_casualties,
                attacker_wounded=round_result.attacker_wounded,
                defender_wounded=round_result.defender_wounded,
                surviving_attacker_ids=[],
                surviving_defender_ids=round_result.surviving_defender_ids,
                attackers_eliminated=True,
                defenders_eliminated=round_result.defenders_eliminated,
            )
            state, end_events = _resolve_combat_end(
                state, combat.attacker_faction, combat.territory_id,
                end_round_result, combat.combat_log, territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=combat.initial_attacker_instance_ids or None,
                initial_defender_instance_ids=combat.initial_defender_instance_ids or None,
            )
            events.extend(end_events)
            state.active_combat = None
            return state, events
        return state, events

    defender_archer_units_continue = [
        u for u in defender_units
        if archer_prefire_eligible(unit_defs.get(u.unit_id))
    ]
    archer_prefire_pending = (
        combat.round_number == 0
        and not any(getattr(r, "is_archer_prefire", False) for r in combat.combat_log)
        and not any(getattr(r, "is_stealth_prefire", False) for r in combat.combat_log)
        and bool(defender_archer_units_continue)
    )
    if archer_prefire_pending:
        archer_prefire_penalty_c = _prefire_stat_delta(state)
        prefire_defender_rolls_c = dice_rolls.get("defender", [])
        round_result_ar = resolve_archer_prefire(
            attacker_units, defender_archer_units_continue, unit_defs, prefire_defender_rolls_c,
            stat_modifiers_defender_extra=defender_mods,
            territory_def=territory_def,
            prefire_penalty_delta=archer_prefire_penalty_c,
        )
        archer_stat_modifiers_c = {
            u.instance_id: archer_prefire_penalty_c + defender_mods.get(u.instance_id, 0)
            for u in defender_archer_units_continue
        }
        defender_dice_grouped_ar = group_dice_by_stat(
            defender_archer_units_continue, prefire_defender_rolls_c, unit_defs, is_attacker=False,
            stat_modifiers=archer_stat_modifiers_c,
        )
        prefire_log_entry_ar = CombatRoundResult(
            round_number=0,
            attacker_rolls=[],
            defender_rolls=prefire_defender_rolls_c,
            attacker_hits=0,
            defender_hits=round_result_ar.defender_hits,
            attacker_casualties=round_result_ar.attacker_casualties,
            defender_casualties=[],
            attackers_remaining=len(round_result_ar.surviving_attacker_ids),
            defenders_remaining=len(defender_units),
            is_archer_prefire=True,
        )
        spec_archer_c = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=bool(sea_zone_id),
            archer_prefire_applicable=True,
        )
        attacker_units_at_start_ar = [
            _build_round_unit_display(
                u,
                unit_defs.get(u.unit_id),
                attacker_mods.get(u.instance_id, 0),
                True,
                combat.attacker_faction,
                territory_def,
                spec_archer_c,
                attacker_effective_attack_override=att_attack_override,
                passenger_aboard=_passengers_aboard_on_boat(u, attacker_territory.units, unit_defs),
            )
            for u in attacker_units
        ]
        defender_units_at_start_ar = [
            _build_round_unit_display(
                u,
                unit_defs.get(u.unit_id),
                archer_prefire_penalty_c + defender_mods.get(u.instance_id, 0),
                False,
                defender_faction,
                territory_def,
                spec_archer_c,
                passenger_aboard=_passengers_aboard_on_boat(u, territory.units, unit_defs),
            )
            for u in defender_units
        ]
        events.append(combat_round_resolved(
            combat.territory_id, 0,
            {}, defender_dice_grouped_ar,
            0, round_result_ar.defender_hits,
            round_result_ar.attacker_casualties, [],
            round_result_ar.attacker_wounded, [],
            len(round_result_ar.surviving_attacker_ids), len(defender_units),
            attacker_units_at_start_ar,
            defender_units_at_start_ar,
            is_archer_prefire=True,
        ))
        for casualty_id in round_result_ar.attacker_casualties:
            unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
            events.append(unit_destroyed(casualty_id, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
        passenger_att_ar = _remove_casualties(attacker_territory, round_result_ar.attacker_casualties, unit_defs)
        passenger_def_ar = _remove_casualties(territory, round_result_ar.defender_casualties, unit_defs)
        for pid in passenger_att_ar:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
        for pid in passenger_def_ar:
            unit_type = pid.split("_")[1] if "_" in pid else "unknown"
            events.append(unit_destroyed(pid, unit_type, defender_faction, combat.territory_id, "combat"))
        _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)
        combat.combat_log.append(prefire_log_entry_ar)
        combat.cumulative_hits_received_by_attacker += round_result_ar.defender_hits
        combat.attacker_instance_ids = round_result_ar.surviving_attacker_ids
        combat.attackers_have_rolled = False
        if round_result_ar.attackers_eliminated:
            end_round_result_ar = RoundResult(
                attacker_hits=0,
                defender_hits=round_result_ar.defender_hits,
                attacker_casualties=round_result_ar.attacker_casualties,
                defender_casualties=[],
                attacker_wounded=[],
                defender_wounded=[],
                surviving_attacker_ids=[],
                surviving_defender_ids=[u.instance_id for u in defender_units],
                attackers_eliminated=True,
                defenders_eliminated=False,
            )
            state, end_events = _resolve_combat_end(
                state, combat.attacker_faction, combat.territory_id,
                end_round_result_ar, combat.combat_log, territory_defs, unit_defs,
                faction_defs,
                sea_zone_id=sea_zone_id,
                initial_attacker_instance_ids=combat.initial_attacker_instance_ids or None,
                initial_defender_instance_ids=combat.initial_defender_instance_ids or None,
            )
            events.extend(end_events)
            state.active_combat = None
            return state, events
        return state, events

    # Normal round 1 or later: all units roll except siegework (excluded); ladder bypass applies
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    round_result, defender_stronghold_hp_after = resolve_combat_round(
        attacker_units, defender_units, unit_defs, dice_rolls,
        stat_modifiers_attacker=attacker_mods or None,
        stat_modifiers_defender=defender_mods or None,
        defender_hits_override=action.payload.get("terror_final_defender_hits"),
        attacker_effective_dice_override=att_effective_dice,
        attacker_effective_attack_override=att_attack_override or None,
        bombikazi_self_destruct_ids=att_self_destruct,
        casualty_order_attacker=combat.casualty_order_attacker,
        casualty_order_defender=defender_casualty_order,
        must_conquer=combat.must_conquer,
        is_naval_combat_attacker=is_naval_combat_attacker,
        is_naval_combat_defender=is_naval_combat_defender,
        defender_stronghold_hp=defender_stronghold_hp_cur,
        defender_territory_is_stronghold=defender_territory_is_stronghold,
        exclude_archetypes_from_rolling=["siegework"],
        attacker_ladder_instance_ids=ladder_ids_combat,
    )
    if defender_stronghold_hp_after is not None:
        territory.stronghold_current_health = defender_stronghold_hp_after

    # Hits per unit type this round (for UI hit badges)
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
        terror_reroll_count=action.payload.get("terror_reroll_count") if new_round_number == 1 else None,
        ladder_infantry_instance_ids=ladder_ids_list_combat,
    ))

    # Emit unit destroyed events for casualties
    for casualty_id in round_result.attacker_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
    for casualty_id in round_result.defender_casualties:
        unit_type = casualty_id.split("_")[1] if "_" in casualty_id else "unknown"
        events.append(unit_destroyed(casualty_id, unit_type, defender_faction, combat.territory_id, "combat"))

    # Remove casualties (attackers may be in sea zone for sea raid). Passengers die when their boat is destroyed.
    passenger_att = _remove_casualties(attacker_territory, round_result.attacker_casualties, unit_defs)
    passenger_def = _remove_casualties(territory, round_result.defender_casualties, unit_defs)
    for pid in passenger_att:
        unit_type = pid.split("_")[1] if "_" in pid else "unknown"
        events.append(unit_destroyed(pid, unit_type, combat.attacker_faction, combat.territory_id, "combat"))
    for pid in passenger_def:
        unit_type = pid.split("_")[1] if "_" in pid else "unknown"
        events.append(unit_destroyed(pid, unit_type, defender_faction, combat.territory_id, "combat"))
    _sync_survivor_health(territory, attacker_units, defender_units, attacker_territory=attacker_territory if sea_zone_id else None)

    # Update combat log and cumulative hits
    combat.combat_log.append(combat_log_entry)
    combat.cumulative_hits_received_by_attacker += round_result.defender_hits
    combat.cumulative_hits_received_by_defender += round_result.attacker_hits
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
            faction_defs,
            sea_zone_id=sea_zone_id,
            initial_attacker_instance_ids=combat.initial_attacker_instance_ids or None,
            initial_defender_instance_ids=combat.initial_defender_instance_ids or None,
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
        raise ValueError("Cannot retreat until attackers have rolled (after archer or stealth prefire, click Continue first)")

    if getattr(combat, "sea_zone_id", None):
        raise ValueError("Retreat is not allowed during a sea raid")

    retreat_to = action.payload.get("retreat_to")
    if not retreat_to:
        raise ValueError("Must specify retreat_to territory")

    retreat_territory = state.territories.get(retreat_to)
    if not retreat_territory:
        raise ValueError(f"Invalid retreat territory: {retreat_to}")

    if not _territory_is_friendly_for_retreat(retreat_territory, combat.attacker_faction, faction_defs, unit_defs):
        raise ValueError(
            f"Cannot retreat to {retreat_to} - must be allied territory")

    surviving_ids = set(combat.attacker_instance_ids)
    # Must be adjacent: ground-only if any retreating unit is land, else allow aerial_adjacent
    retreat_adjacent = _get_retreat_adjacent_ids(state, territory_defs, unit_defs)
    if retreat_to not in retreat_adjacent:
        raise ValueError(
            f"Cannot retreat to {retreat_to} - not adjacent to {combat.territory_id}")

    # Move surviving attackers from contested territory to retreat territory.
    combat_territory = state.territories[combat.territory_id]
    units_to_move = [
        u for u in combat_territory.units
        if u.instance_id in surviving_ids
    ]
    combat_territory.units = [
        u for u in combat_territory.units
        if u.instance_id not in surviving_ids
    ]
    retreat_territory.units.extend(units_to_move)
    retreated_ids = [u.instance_id for u in units_to_move]

    # Emit retreat event (for sea raid, retreat_to is not used for move but still required for API)
    events.append(units_retreated(
        combat.attacker_faction,
        combat.territory_id,
        retreat_to,
        retreated_ids,
    ))

    # Emit combat ended event (defender wins by default on retreat)
    territory = state.territories[combat.territory_id]
    defender_ids = [u.instance_id for u in territory.units]
    att_cas = list(set(combat.initial_attacker_instance_ids or []) - set(combat.attacker_instance_ids))
    def_cas = list(set(combat.initial_defender_instance_ids or []) - set(defender_ids))
    events.append(combat_ended(
        combat.territory_id,
        "defender",
        combat.attacker_faction,
        territory.owner,
        [],  # No surviving attackers in territory
        defender_ids,
        len(combat.combat_log),
        attacker_casualty_ids=att_cas,
        defender_casualty_ids=def_cas,
        retreat_to=retreat_to,
        outcome="retreat",
    ))

    # Clear active combat and sea-raid origin for this territory
    state.active_combat = None
    if getattr(state, "territory_sea_raid_from", None):
        state.territory_sea_raid_from.pop(combat.territory_id, None)

    return state, events


def _handle_set_territory_defender_casualty_order(
    state: GameState,
    action: Action,
) -> tuple[GameState, list[GameEvent]]:
    """Set defender casualty order for a territory owned by the current faction."""
    territory_id = action.payload.get("territory_id")
    casualty_order = action.payload.get("casualty_order")
    if not territory_id or not casualty_order:
        raise ValueError("payload must include territory_id and casualty_order")
    if casualty_order not in ("best_unit", "best_defense"):
        raise ValueError("casualty_order must be 'best_unit' or 'best_defense'")
    territory = state.territories.get(territory_id)
    if not territory:
        raise ValueError(f"Unknown territory: {territory_id}")
    if territory.owner != action.faction:
        raise ValueError(f"Only the owner of {territory_id} can set defender casualty order")
    state.territory_defender_casualty_order[territory_id] = casualty_order
    return state, []


def _remove_casualties(
    territory: TerritoryState,
    casualty_ids: list[str],
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> list[str]:
    """
    Remove units with the given instance_ids from a territory.
    When a casualty is a naval unit (boat), also remove all passengers (units with loaded_onto == that boat).
    Returns list of passenger instance_ids that were removed (so caller can emit unit_destroyed for them).
    """
    casualty_set = set(casualty_ids)
    if unit_defs:
        for u in list(territory.units):
            if u.instance_id not in casualty_set:
                continue
            if _is_naval_unit(unit_defs.get(u.unit_id)):
                for p in territory.units:
                    if getattr(p, "loaded_onto", None) == u.instance_id:
                        casualty_set.add(p.instance_id)
    territory.units = [u for u in territory.units if u.instance_id not in casualty_set]
    return [i for i in casualty_set if i not in set(casualty_ids)]


def _sync_survivor_health(
    territory: TerritoryState,
    attacker_units: list[Unit],
    defender_units: list[Unit],
    attacker_territory: TerritoryState | None = None,
) -> None:
    """
    Sync remaining_health from combat-round copies back to territory.units.
    Combat modifies deepcopies; survivors' remaining_health must be written back
    so multi-HP units (e.g. trolls) carry damage across rounds.
    When attacker_territory is set (sea raid), sync attacker_units to attacker_territory and defender_units to territory.
    """
    if attacker_territory is not None:
        for unit in attacker_territory.units:
            for u in attacker_units:
                if u.instance_id == unit.instance_id:
                    unit.remaining_health = u.remaining_health
                    break
        for unit in territory.units:
            for u in defender_units:
                if u.instance_id == unit.instance_id:
                    unit.remaining_health = u.remaining_health
                    break
        return
    survivor_health = {u.instance_id: u.remaining_health for u in attacker_units + defender_units}
    for unit in territory.units:
        if unit.instance_id in survivor_health:
            unit.remaining_health = survivor_health[unit.instance_id]


def _purge_sea_raid_staging_after_lost_naval(
    state: GameState,
    sea_territory_id: str,
    attacker_faction: str,
    unit_defs: dict[str, UnitDefinition],
) -> list[GameEvent]:
    """
    When naval combat in a sea zone ends with all attackers eliminated, clear every
    territory_sea_raid_from entry that staged from that sea and remove stranded land
    attackers (passengers) from those land hexes so the land raid cannot follow.
    """
    events: list[GameEvent] = []
    tsrf = getattr(state, "territory_sea_raid_from", None) or {}
    lands = [lid for lid, sz in tsrf.items() if sz == sea_territory_id]
    if not lands:
        return events
    state.territory_sea_raid_from = {k: v for k, v in tsrf.items() if v != sea_territory_id}

    for lid in lands:
        t = state.territories.get(lid)
        if not t:
            continue
        kept: list[Unit] = []
        for u in t.units:
            ud = unit_defs.get(u.unit_id)
            if (
                get_unit_faction(u, unit_defs) == attacker_faction
                and is_land_unit(ud)
                and not _is_naval_unit(ud)
            ):
                unit_type = u.instance_id.split("_")[1] if "_" in u.instance_id else "unknown"
                events.append(
                    unit_destroyed(u.instance_id, unit_type, attacker_faction, lid, "sea_raid_naval_lost")
                )
                continue
            kept.append(u)
        t.units = kept

    return events


def _liberation_beneficiary_if_allied_original(
    territory_id: str,
    territory: TerritoryState,
    capturer_faction: str,
    faction_defs: dict[str, FactionDefinition],
    state: GameState,
) -> str | None:
    """
    Faction id restored at combat phase end when capturer and original_owner share an alliance.
    Matches pending capture application in _handle_end_phase.
    """
    original_owner = effective_original_owner(territory_id, territory, state)
    if not original_owner or original_owner == capturer_faction:
        return None
    capturer_def = faction_defs.get(capturer_faction)
    original_def = faction_defs.get(original_owner)
    if not capturer_def or not original_def:
        return None
    if capturer_def.alliance == original_def.alliance:
        return original_owner
    return None


def _resolve_combat_end(
    state: GameState,
    attacker_faction: str,
    territory_id: str,
    round_result: RoundResult,
    combat_log: list[CombatRoundResult],
    territory_defs: dict[str, TerritoryDefinition],
    unit_defs: dict[str, UnitDefinition],
    faction_defs: dict[str, FactionDefinition],
    sea_zone_id: str | None = None,
    initial_attacker_instance_ids: list[str] | None = None,
    initial_defender_instance_ids: list[str] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    Resolve the end of combat.
    Both attackers and defenders are in the same contested territory (or, for sea raid, attackers in sea_zone_id).
    - If defenders eliminated AND at least one attacker survived: territory captured by attacker
      (only if surviving attackers include a conquering-capable unit; aerial-only or siegework-only cannot conquer)
      For sea raid: move surviving attackers from sea zone to territory.
    - If attackers eliminated OR both sides eliminated: defender keeps territory (no conquest)
    """
    events: list[GameEvent] = []
    territory = state.territories[territory_id]
    old_owner = territory.owner
    total_rounds = len(combat_log)
    # Casualty ids for one-line battle summary
    att_cas = list(set(initial_attacker_instance_ids or []) - set(round_result.surviving_attacker_ids))
    def_cas = list(set(initial_defender_instance_ids or []) - set(round_result.surviving_defender_ids))
    # Where attackers live: sea zone (before offload) or territory (after phase end / already offloaded)
    sea_zone = state.territories.get(sea_zone_id) if sea_zone_id else None
    surviving_attacker_ids_set = set(round_result.surviving_attacker_ids)
    if sea_zone_id and sea_zone:
        in_sea = [u for u in sea_zone.units if u.instance_id in surviving_attacker_ids_set]
        in_land = [u for u in territory.units if u.instance_id in surviving_attacker_ids_set]
        # Conquer check must see every surviving attacker (land + sea). Previously we only used
        # in_sea when non-empty, so surviving land units were ignored and did_conquer was false
        # (no pending_captures) even though the battle was won — wrong for sea raids/offload splits.
        surviving_attacker_units = in_sea + in_land
        attacker_territory = sea_zone if in_sea else territory
    else:
        attacker_territory = territory
        surviving_attacker_units = [
            u for u in territory.units
            if u.instance_id in surviving_attacker_ids_set
        ]

    # If instance ids say survivors exist but they are not on the contested land/sea lists (stale state),
    # find them anywhere on the board so conquest and ground checks still match the round result.
    if not surviving_attacker_units and surviving_attacker_ids_set:
        seen_ids: set[str] = set()
        for t in state.territories.values():
            for u in t.units:
                if u.instance_id in surviving_attacker_ids_set and u.instance_id not in seen_ids:
                    seen_ids.add(u.instance_id)
                    surviving_attacker_units.append(u)

    # Attacker only wins if defenders are gone AND at least one attacker survived
    if round_result.defenders_eliminated and not round_result.attackers_eliminated:
        has_living_ground_attacker = any(
            can_conquer_territory_as_attacker(unit_defs.get(u.unit_id))
            for u in surviving_attacker_units
        )
        # Pure land (and sea): if board state lost sync with surviving_attacker_ids but the round
        # result lists survivors, infer unit types from instance ids so pending_captures still applies.
        if not has_living_ground_attacker and round_result.surviving_attacker_ids:
            fids = list(faction_defs.keys())
            for iid in round_result.surviving_attacker_ids:
                uid = _unit_id_from_instance_id_pattern(iid, fids)
                if uid and can_conquer_territory_as_attacker(unit_defs.get(uid)):
                    has_living_ground_attacker = True
                    break
        territory_def = territory_defs.get(territory_id)
        did_conquer = (
            has_living_ground_attacker
            and territory_def
            and getattr(territory_def, "ownable", True)
        )
        if did_conquer:
            state.pending_captures[territory_id] = attacker_faction
        else:
            state.pending_captures.pop(territory_id, None)

        # Sea raid (attackers in sea): move only land units (passengers) to territory; boats stay in sea zone.
        # Bomb (self_destruct tag) still moves if it survived the round; paired detonation is handled in combat.
        # If attackers were already on territory (offloaded), nothing to move.
        if sea_zone_id and sea_zone and attacker_territory is sea_zone:
            to_move = [
                u for u in attacker_territory.units
                if (u.instance_id in surviving_attacker_ids_set
                    and is_land_unit(unit_defs.get(u.unit_id))
                    and not _is_naval_unit(unit_defs.get(u.unit_id)))
            ]
            # Remove all surviving attackers from sea; only to_move go to territory
            for u in list(attacker_territory.units):
                if u.instance_id in surviving_attacker_ids_set:
                    attacker_territory.units.remove(u)
                    if u in to_move:
                        setattr(u, "loaded_onto", None)
                        territory.units.append(u)
        else:
            # Land battle: keep surviving attackers (bombikazi/bomb removal is via round casualties only),
            # bystanders, and defender-roster survivors only. When all defenders are eliminated, purge any
            # stray defender-side units so allied garrison / roster drift cannot leave ghosts or wipe the wrong stack.
            aa = getattr(faction_defs.get(attacker_faction), "alliance", None)
            surv_def_set = set(round_result.surviving_defender_ids or [])
            kept: list[Unit] = []
            for u in territory.units:
                uid = u.instance_id
                side = _land_combat_unit_side(u, attacker_faction, aa, unit_defs, faction_defs)
                if side == "defender":
                    if uid in surv_def_set:
                        kept.append(u)
                    continue
                if side == "attacker":
                    if uid in surviving_attacker_ids_set:
                        kept.append(u)
                    continue
                kept.append(u)
            territory.units = kept

        liberated_for = (
            _liberation_beneficiary_if_allied_original(
                territory_id, territory, attacker_faction, faction_defs, state,
            )
            if did_conquer
            else None
        )
        events.append(combat_ended(
            territory_id,
            "attacker",
            attacker_faction,
            old_owner,
            round_result.surviving_attacker_ids,
            [],
            total_rounds,
            attacker_casualty_ids=att_cas,
            defender_casualty_ids=def_cas,
            outcome="conquer" if did_conquer else "victory",
            liberated_for=liberated_for,
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
            attacker_casualty_ids=att_cas,
            defender_casualty_ids=def_cas,
            outcome="defeat",
        ))

    # Clear active combat; if naval battle in a sea zone was lost, cancel any staged land sea raid
    state.active_combat = None
    tsrf = getattr(state, "territory_sea_raid_from", None) or {}
    tdef_end = territory_defs.get(territory_id)
    is_sea_combat = bool(tdef_end and _is_sea_zone(tdef_end))
    if tsrf:
        if is_sea_combat and round_result.attackers_eliminated:
            events.extend(
                _purge_sea_raid_staging_after_lost_naval(
                    state, territory_id, attacker_faction, unit_defs
                )
            )
        else:
            state.territory_sea_raid_from.pop(territory_id, None)

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
    # Cannot end combat phase while contested battles remain (units in enemy territory not yet resolved)
    if state.phase == "combat" and state.active_combat is None and state.current_faction:
        contested = get_contested_territories(
            state, state.current_faction, faction_defs, unit_defs, territory_defs
        )
        if contested:
            raise ValueError(
                "Cannot end combat phase while there are unresolved battles. "
                "Initiate and resolve or retreat from all battles first."
            )

    old_phase = state.phase

    # If ending combat_move phase, apply all pending combat moves first, then check loaded_naval_must_attack_instance_ids
    if state.phase == "combat_move":
        state, move_events = _apply_pending_moves(
            state, "combat_move", unit_defs, territory_defs, faction_defs
        )
        events.extend(move_events)
        # After applying: every boat that received a load this phase must attack (naval combat or sea raid)
        if getattr(state, "loaded_naval_must_attack_instance_ids", []):
            raise ValueError(
                "Every boat that received a load this phase must attack before ending combat move: "
                f"{state.loaded_naval_must_attack_instance_ids!s}. Attack with those fleets (naval combat or sea raid) first."
            )

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
            original_owner = effective_original_owner(territory_id, territory, state)

            if original_owner and original_owner != capturer:
                capturer_def = faction_defs.get(capturer)
                original_def = faction_defs.get(original_owner)
                
                if capturer_def and original_def:
                    if capturer_def.alliance == original_def.alliance:
                        # Liberation! Restore to original owner
                        new_owner = original_owner
            
            territory.owner = new_owner

            # Destroy any camp in this territory when captured/liberated — except camps in capitals (those remain)
            is_capital = any(
                getattr(f, "capital", None) == territory_id for f in (faction_defs or {}).values()
            )
            if not is_capital:
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
        faction_id = state.current_faction or ""
        cd = camp_defs or {}
        camp_cost = int(getattr(state, "camp_cost", 10) or 10)
        queued_mob = {p.camp_index for p in getattr(state, "pending_camp_placements", []) or []}
        for i, p in enumerate(state.pending_camps or []):
            if p.get("placed_territory_id"):
                continue
            if i in queued_mob:
                continue
            if valid_camp_placement_territory_ids(
                state, faction_id, i, cd, territory_defs
            ):
                continue
            old_power = int(state.faction_resources.get(faction_id, {}).get("power", 0) or 0)
            if faction_id not in state.faction_resources:
                state.faction_resources[faction_id] = {}
            new_power = old_power + camp_cost
            state.faction_resources[faction_id]["power"] = new_power
            state.pending_camps[i]["placed_territory_id"] = "__forfeited__"
            events.append(
                resources_changed(
                    faction_id,
                    "power",
                    old_power,
                    new_power,
                    "camp_forfeited_no_valid_territory",
                )
            )
        state, camp_events = _apply_pending_camp_placements(state, camp_defs or {})
        events.extend(camp_events)
        state, mobilize_events = _apply_pending_mobilizations(
            state, unit_defs, territory_defs, faction_defs
        )
        events.extend(mobilize_events)
        events.append(phase_changed(old_phase, "turn_end", state.current_faction))
        state, turn_events = _handle_end_turn(
            state, territory_defs, faction_defs, camp_defs or {}, unit_defs
        )
        events.extend(turn_events)
        return state, events
    
    next_idx = current_idx + 1
    state.phase = phase_order[next_idx]
    if old_phase == "combat_move":
        state.loaded_naval_must_attack_instance_ids = []
        state.avoided_forced_naval_combat_instance_ids = []

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


def _handle_skip_turn(
    state: GameState,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    Force end current faction's turn from any phase. Used by forfeit when a player leaves on their turn.
    Clears phase-specific state (active combat, pending moves, etc.) then runs _handle_end_turn.
    """
    # Clear blockers so _handle_end_turn can run
    state.active_combat = None
    state.pending_moves = []
    state.declared_battles = []
    state.pending_captures = {}
    state.loaded_naval_must_attack_instance_ids = []
    state.avoided_forced_naval_combat_instance_ids = []
    state.naval_mobilization_intruder_instance_ids = []
    return _handle_end_turn(state, territory_defs, faction_defs, camp_defs, unit_defs)


def _handle_end_turn(
    state: GameState,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None = None,
    unit_defs: dict[str, UnitDefinition] | None = None,
) -> tuple[GameState, list[GameEvent]]:
    """
    End the current turn and advance to the next faction.
    Factions with no capital and no units anywhere are skipped (no user interaction).

    At end of turn:
    - Clears purchased units pool (unspent purchases are lost)
    - Calculates and stores pending income based on currently owned territories

    At start of next faction's turn:
    - Applies any pending income they have stored from their previous turn
    """
    unit_defs = unit_defs or {}
    events: list[GameEvent] = []
    old_faction = state.current_faction

    # Clear purchased units for this faction (they must be mobilized before end of turn)
    state.faction_purchased_units[state.current_faction] = []

    # Calculate and store pending income for the ending faction
    # Only if they still own their capital - if capital captured, no income
    income_calculated_event: GameEvent | None = None
    if faction_owns_capital(state, old_faction, faction_defs):
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

        if pending_income:
            income_calculated_event = income_calculated(old_faction, pending_income, contributing_territories)
    else:
        # Capital captured - no income
        state.faction_pending_income[old_faction] = {}

    # Turn end first; income summary last for this faction's turn (after mobilization / end phase)
    events.append(turn_ended(state.turn_number, old_faction))
    if income_calculated_event is not None:
        events.append(income_calculated_event)

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

    # Apply pending income and possibly skip factions with no capital and no units
    camp_defs = camp_defs or {}
    skipped = 0
    while skipped < len(faction_ids):
        new_faction = state.current_faction
        if new_faction in state.faction_pending_income:
            faction_income = state.faction_pending_income[new_faction]
            if faction_income:
                if new_faction not in state.faction_resources:
                    state.faction_resources[new_faction] = {}
                new_totals = {}
                for resource_id, amount in faction_income.items():
                    if resource_id not in state.faction_resources[new_faction]:
                        state.faction_resources[new_faction][resource_id] = 0
                    state.faction_resources[new_faction][resource_id] += amount
                    new_totals[resource_id] = state.faction_resources[new_faction][resource_id]
                events.append(income_collected(new_faction, faction_income, new_totals))
            state.faction_pending_income[new_faction] = {}

        # Skip this faction if they have no capital and no units anywhere (no purchase/mobilize, nothing to move/attack)
        if not faction_owns_capital(state, new_faction, faction_defs) and _faction_unit_count(state, new_faction, unit_defs) == 0:
            events.append(turn_skipped(new_faction))
            skipped += 1
            next_idx = (next_idx + 1) % len(faction_ids)
            if next_idx == 0:
                victory_result = _check_victory(state, territory_defs, faction_defs)
                if victory_result:
                    winner_alliance, stronghold_counts, controlled = victory_result
                    state.winner = winner_alliance
                    strongholds_criteria = state.victory_criteria.get("strongholds") or {}
                    strongholds_required = int(strongholds_criteria.get(winner_alliance, 0)) if isinstance(strongholds_criteria, dict) else 0
                    events.append(victory(winner_alliance, stronghold_counts, strongholds_required, controlled))
                state.turn_number += 1
            state.current_faction = faction_ids[next_idx]
            state.phase = "purchase"
            continue

        # This faction gets a turn: snapshot territories and camps, emit turn_started
        state.faction_territories_at_turn_start[new_faction] = [
            tid for tid, ts in state.territories.items() if ts.owner == new_faction
        ]
        state.pending_camps = []
        state.mobilization_camps = [
            tid for tid, ts in state.territories.items()
            if ts.owner == new_faction and _territory_has_standing_camp(state, tid, camp_defs)
        ]
        events.append(turn_started(state.turn_number, state.current_faction))
        break

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
