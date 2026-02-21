"""
Action definitions for the game.
Actions are immutable, deterministic instructions.
"""

from dataclasses import dataclass


@dataclass
class Action:
    """Base action class. All actions have a type, faction, and payload."""
    type: str  # e.g., "purchase_units", "move_units", "initiate_combat", "end_phase", "end_turn"
    faction: str  # faction_id performing the action
    payload: dict  # Action-specific data


def purchase_camp(faction: str) -> Action:
    """
    Purchase a camp. Cost from state.camp_cost (power).
    Camp is added to pending_camps; territory_options are territories owned at turn start without a camp.
    Placement happens in mobilization phase via place_camp.
    """
    return Action(type="purchase_camp", faction=faction, payload={})


def purchase_units(
    faction: str,
    purchases: dict[str, int],  # unit_id -> count to purchase
) -> Action:
    """
    Purchase units for a faction.
    Example: purchase_units("gondor", {"gondor_infantry": 2, "gondor_knight": 1})
    """
    return Action(
        type="purchase_units",
        faction=faction,
        payload={"purchases": purchases},
    )


def move_units(
    faction: str,
    territory_from: str,
    territory_to: str,
    unit_instance_ids: list[str],  # List of unit instance_ids to move
    charge_through: list[str] | None = None,  # Cavalry: empty enemy territory IDs to conquer (order)
) -> Action:
    """
    Move units from one territory to another.
    Units are specified by their instance_ids for granular control.
    charge_through: for cavalry charging, list of empty enemy territory IDs passed through (conquered when move is applied).
    """
    payload = {
        "from": territory_from,
        "to": territory_to,
        "unit_instance_ids": unit_instance_ids,
    }
    if charge_through:
        payload["charge_through"] = charge_through
    return Action(
        type="move_units",
        faction=faction,
        payload=payload,
    )


def initiate_combat(
    faction: str,
    territory_id: str,  # The contested territory where attackers moved during combat_move
    # "attacker" -> [rolls], "defender" -> [rolls]
    dice_rolls: dict[str, list[int]],
) -> Action:
    """
    Initiate combat in a contested territory.

    During combat_move phase, attacking units move INTO enemy territory (from multiple
    origin territories if desired). This creates a contested territory with both
    attackers and defenders present.

    initiate_combat resolves battle in that territory:
    - Attackers = units owned by current faction in the territory
    - Defenders = units owned by territory owner in the territory

    dice_rolls must be provided for round 1 (deterministic, no RNG in reducer).

    After round 1, if both sides have survivors, attacker must choose:
    - continue_combat (fight another round)
    - retreat(retreat_to) - surviving attackers move to an adjacent friendly territory

    Example: After moving Gondor units into Mordor during combat_move:
             initiate_combat("gondor", "mordor", {"attacker": [3, 4, 5], "defender": [1, 2, 3, 4]})
    """
    return Action(
        type="initiate_combat",
        faction=faction,
        payload={
            "attacker": faction,
            "territory_id": territory_id,
            "dice_rolls": dice_rolls,
        },
    )


def continue_combat(
    faction: str,
    # "attacker" -> [rolls], "defender" -> [rolls]
    dice_rolls: dict[str, list[int]],
) -> Action:
    """
    Continue an active combat with another round.
    Only valid when there is an active_combat in the game state.
    dice_rolls must be provided for this round.

    Example: continue_combat("gondor", {"attacker": [2, 5], "defender": [1, 3, 4]})
    """
    return Action(
        type="continue_combat",
        faction=faction,
        payload={
            "dice_rolls": dice_rolls,
        },
    )


def retreat(
    faction: str,
    retreat_to: str,  # Territory to retreat surviving attackers to
) -> Action:
    """
    Retreat from an active combat.
    All surviving attacking units move to the specified retreat_to territory.
    retreat_to must be:
    - Adjacent to the defender territory
    - Friendly (owned by the attacker's faction or allied)

    Defender keeps control of the defender territory.
    Only valid when there is an active_combat in the game state.

    Example: retreat("gondor", "osgiliath")  # Retreat surviving attackers to Osgiliath
    """
    return Action(
        type="retreat",
        faction=faction,
        payload={
            "retreat_to": retreat_to,
        },
    )


def mobilize_units(
    faction: str,
    destination_territory: str,
    unit_stacks: list[dict],  # [{"unit_id": str, "count": int}, ...]
) -> Action:
    """
    Mobilize purchased units into a stronghold territory.
    Only available in mobilization phase.
    Units must have been purchased in phase 1 of this turn.
    Uses type+count format since purchased units don't have instance_ids yet.
    """
    return Action(
        type="mobilize_units",
        faction=faction,
        payload={
            "destination": destination_territory,
            "units": unit_stacks,
        },
    )


def cancel_move(
    faction: str,
    move_index: int,  # Index of the pending move to cancel
) -> Action:
    """
    Cancel a pending move.
    The move is removed from pending_moves list.
    Only valid during combat_move or non_combat_move phases.
    """
    return Action(
        type="cancel_move",
        faction=faction,
        payload={"move_index": move_index},
    )


def place_camp(
    faction: str,
    camp_index: int,
    territory_id: str,
) -> Action:
    """
    Place a purchased camp on a territory. Valid in mobilization phase.
    territory_id must be in that pending camp's territory_options and must not already have a camp.
    """
    return Action(
        type="place_camp",
        faction=faction,
        payload={"camp_index": camp_index, "territory_id": territory_id},
    )


def cancel_mobilization(
    faction: str,
    mobilization_index: int,
) -> Action:
    """Cancel a pending mobilization. Units return to faction_purchased_units."""
    return Action(
        type="cancel_mobilization",
        faction=faction,
        payload={"mobilization_index": mobilization_index},
    )


def end_phase(faction: str) -> Action:
    """End the current phase and move to the next."""
    return Action(
        type="end_phase",
        faction=faction,
        payload={},
    )


def end_turn(faction: str) -> Action:
    """End the current turn and advance to the next faction."""
    return Action(
        type="end_turn",
        faction=faction,
        payload={},
    )
