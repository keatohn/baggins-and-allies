"""
Game events for UI hooks and logging.
Events describe what happened during action processing.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class GameEvent:
    """Base event class. All events have a type and payload."""
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameEvent":
        return cls(type=data["type"], payload=data["payload"])


# ===== Event Type Constants =====

# Phase/Turn events
PHASE_CHANGED = "phase_changed"
TURN_STARTED = "turn_started"
TURN_ENDED = "turn_ended"

# Resource events
RESOURCES_CHANGED = "resources_changed"
UNITS_PURCHASED = "units_purchased"
INCOME_CALCULATED = "income_calculated"
INCOME_COLLECTED = "income_collected"

# Movement events
UNITS_MOVED = "units_moved"

# Combat events
COMBAT_STARTED = "combat_started"
COMBAT_ROUND_RESOLVED = "combat_round_resolved"
COMBAT_ENDED = "combat_ended"
UNITS_RETREATED = "units_retreated"

# Territory events
TERRITORY_CAPTURED = "territory_captured"

# Unit events
UNIT_DESTROYED = "unit_destroyed"
UNITS_MOBILIZED = "units_mobilized"

# Victory events
VICTORY = "victory"


# ===== Event Factory Functions =====

def phase_changed(old_phase: str, new_phase: str, faction: str) -> GameEvent:
    return GameEvent(PHASE_CHANGED, {
        "old_phase": old_phase,
        "new_phase": new_phase,
        "faction": faction,
    })


def turn_started(turn_number: int, faction: str) -> GameEvent:
    return GameEvent(TURN_STARTED, {
        "turn_number": turn_number,
        "faction": faction,
    })


def turn_ended(turn_number: int, faction: str) -> GameEvent:
    return GameEvent(TURN_ENDED, {
        "turn_number": turn_number,
        "faction": faction,
    })


def resources_changed(
    faction: str,
    resource: str,
    old_value: int,
    new_value: int,
    reason: str,
) -> GameEvent:
    return GameEvent(RESOURCES_CHANGED, {
        "faction": faction,
        "resource": resource,
        "old_value": old_value,
        "new_value": new_value,
        "change": new_value - old_value,
        "reason": reason,
    })


def units_purchased(faction: str, purchases: dict[str, int], total_cost: dict[str, int]) -> GameEvent:
    return GameEvent(UNITS_PURCHASED, {
        "faction": faction,
        "purchases": purchases,  # unit_id -> count
        "total_cost": total_cost,  # resource -> amount
    })


def income_calculated(
    faction: str,
    income: dict[str, int],
    territories: list[str],
) -> GameEvent:
    """Emitted at end of turn when income is calculated based on owned territories."""
    return GameEvent(INCOME_CALCULATED, {
        "faction": faction,
        "income": income,  # resource -> amount
        "territories": territories,  # territory_ids that contributed
    })


def income_collected(
    faction: str,
    income: dict[str, int],
    new_totals: dict[str, int],
) -> GameEvent:
    """Emitted at start of turn when pending income is added to resources."""
    return GameEvent(INCOME_COLLECTED, {
        "faction": faction,
        "income": income,  # resource -> amount added
        "new_totals": new_totals,  # resource -> new total after collection
    })


def units_moved(
    faction: str,
    from_territory: str,
    to_territory: str,
    unit_ids: list[str],
    phase: str,
) -> GameEvent:
    return GameEvent(UNITS_MOVED, {
        "faction": faction,
        "from_territory": from_territory,
        "to_territory": to_territory,
        "unit_ids": unit_ids,
        "phase": phase,
    })


def combat_started(
    territory: str,
    attacker_faction: str,
    attacker_units: list[str],
    defender_faction: str,
    defender_units: list[str],
) -> GameEvent:
    return GameEvent(COMBAT_STARTED, {
        "territory": territory,
        "attacker_faction": attacker_faction,
        "attacker_units": attacker_units,
        "defender_faction": defender_faction,
        "defender_units": defender_units,
    })


def combat_round_resolved(
    territory: str,
    round_number: int,
    attacker_dice: dict[int, dict],
    defender_dice: dict[int, dict],
    attacker_hits: int,
    defender_hits: int,
    attacker_casualties: list[str],
    defender_casualties: list[str],
    attacker_wounded: list[str],
    defender_wounded: list[str],
    attackers_remaining: int,
    defenders_remaining: int,
    attacker_units_at_start: list[dict],
    defender_units_at_start: list[dict],
    *,
    attacker_hits_by_unit_type: dict[str, int] | None = None,
    defender_hits_by_unit_type: dict[str, int] | None = None,
    is_archer_prefire: bool = False,
    terror_applied: bool = False,
) -> GameEvent:
    """
    Emit combat round resolved event.

    This event is the complete UI contract for one combat round. The frontend
    uses it as the single source of truth for that round: no mixing with
    post-round state.

    Payload:
    - territory, round_number: identity.
    - attacker_dice / defender_dice: grouped by stat value,
      { stat: { "rolls": [int], "hits": int } }.
    - attacker_hits / defender_hits: total hits this round.
    - attacker_casualties / defender_casualties: instance_ids destroyed.
    - attacker_wounded / defender_wounded: instance_ids that took damage but survived.
    - attackers_remaining / defenders_remaining: counts after this round.
    - attacker_units_at_start / defender_units_at_start: REQUIRED. Snapshot of every
      unit at round start (before dice), with effective_attack/effective_defense and
      specials (terror, terrain_mountain, etc.). Each item: instance_id, unit_id,
      display_name, attack, defense, effective_attack, effective_defense, health,
      remaining_health, remaining_movement, is_archer, faction, terror,
      terrain_mountain, terrain_forest, captain_bonus, anti_cavalry.
    - attacker_hits_by_unit_type / defender_hits_by_unit_type: hits per unit type
      (stack) for hit badges.
    - is_archer_prefire: true when this is defender archer prefire (before round 1).
    - terror_applied: true when terror forced defender re-rolls (round 1 only).
    """
    payload: dict = {
        "territory": territory,
        "round_number": round_number,
        "attacker_dice": attacker_dice,
        "defender_dice": defender_dice,
        "attacker_hits": attacker_hits,
        "defender_hits": defender_hits,
        "attacker_casualties": attacker_casualties,
        "defender_casualties": defender_casualties,
        "attacker_wounded": attacker_wounded,
        "defender_wounded": defender_wounded,
        "attackers_remaining": attackers_remaining,
        "defenders_remaining": defenders_remaining,
        "attacker_units_at_start": attacker_units_at_start,
        "defender_units_at_start": defender_units_at_start,
    }
    if attacker_hits_by_unit_type is not None:
        payload["attacker_hits_by_unit_type"] = attacker_hits_by_unit_type
    if defender_hits_by_unit_type is not None:
        payload["defender_hits_by_unit_type"] = defender_hits_by_unit_type
    if is_archer_prefire:
        payload["is_archer_prefire"] = True
    if terror_applied:
        payload["terror_applied"] = True
    return GameEvent(COMBAT_ROUND_RESOLVED, payload)


def combat_ended(
    territory: str,
    winner: str,  # "attacker", "defender", or "draw"
    attacker_faction: str,
    defender_faction: str,
    surviving_attacker_ids: list[str],
    surviving_defender_ids: list[str],
    total_rounds: int,
) -> GameEvent:
    return GameEvent(COMBAT_ENDED, {
        "territory": territory,
        "winner": winner,
        "attacker_faction": attacker_faction,
        "defender_faction": defender_faction,
        "surviving_attacker_ids": surviving_attacker_ids,
        "surviving_defender_ids": surviving_defender_ids,
        "total_rounds": total_rounds,
    })


def units_retreated(
    faction: str,
    from_territory: str,
    to_territory: str,
    unit_ids: list[str],
) -> GameEvent:
    return GameEvent(UNITS_RETREATED, {
        "faction": faction,
        "from_territory": from_territory,
        "to_territory": to_territory,
        "unit_ids": unit_ids,
    })


def territory_captured(
    territory: str,
    old_owner: str | None,
    new_owner: str,
    capturing_units: list[str],
) -> GameEvent:
    return GameEvent(TERRITORY_CAPTURED, {
        "territory": territory,
        "old_owner": old_owner,
        "new_owner": new_owner,
        "capturing_units": capturing_units,
    })


def unit_destroyed(
    unit_id: str,
    unit_type: str,
    owner: str,
    territory: str,
    cause: str,  # "combat", "other"
) -> GameEvent:
    return GameEvent(UNIT_DESTROYED, {
        "unit_id": unit_id,
        "unit_type": unit_type,
        "owner": owner,
        "territory": territory,
        "cause": cause,
    })


def units_mobilized(
    faction: str,
    territory: str,
    units: list[dict],  # [{"unit_id": str, "instance_id": str}, ...]
) -> GameEvent:
    return GameEvent(UNITS_MOBILIZED, {
        "faction": faction,
        "territory": territory,
        "units": units,
    })


def victory(
    winner: str,
    stronghold_counts: dict[str, int],
    strongholds_required: int,
    controlled_strongholds: list[str],
) -> GameEvent:
    """
    Emitted when an alliance achieves victory.

    Args:
        winner: The winning alliance ("good" or "evil")
        stronghold_counts: {alliance: count} for all alliances
        strongholds_required: The threshold needed for victory
        controlled_strongholds: List of stronghold territory IDs controlled by winner
    """
    return GameEvent(VICTORY, {
        "winner": winner,
        "stronghold_counts": stronghold_counts,
        "strongholds_required": strongholds_required,
        "controlled_strongholds": controlled_strongholds,
    })
