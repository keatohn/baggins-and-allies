"""
Combat resolution system.
Simultaneous attacks, defender auto-assigns casualties.
Works with individual Unit instances.
Supports multi-round combat with retreat option.
"""

from dataclasses import dataclass, field
from copy import deepcopy
from backend.engine.state import Unit, CombatRoundResult
from backend.engine.definitions import UnitDefinition


@dataclass
class RoundResult:
    """Result of a single combat round."""
    attacker_hits: int
    defender_hits: int
    attacker_casualties: list[str]  # instance_ids destroyed this round
    defender_casualties: list[str]  # instance_ids destroyed this round
    attacker_wounded: list[str]  # instance_ids that took damage but survived
    defender_wounded: list[str]  # instance_ids that took damage but survived
    surviving_attacker_ids: list[str]  # instance_ids still alive
    surviving_defender_ids: list[str]  # instance_ids still alive
    attackers_eliminated: bool  # True if all attackers dead
    defenders_eliminated: bool  # True if all defenders dead


def resolve_combat_round(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    dice_rolls: dict[str, list[int]],
) -> RoundResult:
    """
    Resolve a single combat round.

    Combat rules:
    - Simultaneous: both sides roll and apply hits simultaneously
    - Each unit rolls 1 die (regardless of HP)
    - Hit if roll <= attack (for attacker) or <= defense (for defender)
    - Casualties auto-assigned by priority:
      1. remaining_health desc (soak hits with high health units)
      2. cost asc (lose cheap units first)
      3. attack/defense asc (lose weak units first)
      4. remaining_movement asc (lose immobile units first)

    Note: This function MODIFIES the unit lists in place (removes dead units,
    decrements health). Caller should pass copies if originals need preservation.

    Args:
        attacker_units: List of attacking Unit instances (modified in place)
        defender_units: List of defending Unit instances (modified in place)
        unit_defs: Unit definitions
        dice_rolls: {"attacker": [rolls], "defender": [rolls]}

    Returns:
        RoundResult with casualties and survivor info
    """
    attacker_rolls = dice_rolls.get("attacker", [])
    defender_rolls = dice_rolls.get("defender", [])

    # Count hits using actual unit stats
    attacker_hits = _count_hits(
        attacker_units, attacker_rolls, unit_defs, is_attacker=True)
    defender_hits = _count_hits(
        defender_units, defender_rolls, unit_defs, is_attacker=False)

    # Apply hits simultaneously - each side takes hits from the OTHER side's rolls
    # Defenders hit attackers, attackers hit defenders
    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True)
    defender_casualties, defender_wounded = _apply_hits(
        defender_units, attacker_hits, unit_defs, is_attacker=False)

    return RoundResult(
        attacker_hits=attacker_hits,
        defender_hits=defender_hits,
        attacker_casualties=attacker_casualties,
        defender_casualties=defender_casualties,
        attacker_wounded=attacker_wounded,
        defender_wounded=defender_wounded,
        surviving_attacker_ids=[u.instance_id for u in attacker_units],
        surviving_defender_ids=[u.instance_id for u in defender_units],
        attackers_eliminated=len(attacker_units) == 0,
        defenders_eliminated=len(defender_units) == 0,
    )


def _count_hits(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
) -> int:
    """
    Count hits from dice rolls using actual unit attack/defense values.

    Each unit rolls dice based on its unit definition's 'dice' attribute.
    A roll is a hit if roll <= unit's attack (attacker) or defense (defender) value.
    """
    stat_name = "attack" if is_attacker else "defense"

    hits = 0
    roll_idx = 0

    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue

        stat_value = getattr(unit_def, stat_name, 0)
        dice_count = getattr(unit_def, 'dice', 1)

        # Roll dice_count times for this unit
        for _ in range(dice_count):
            if roll_idx < len(rolls):
                if rolls[roll_idx] <= stat_value:
                    hits += 1
                roll_idx += 1

    return hits


def _apply_hits(
    units: list[Unit],
    hits: int,
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
) -> tuple[list[str], list[str]]:
    """
    Apply hits to units, returning (destroyed_ids, wounded_ids).

    Priority order for taking hits (re-evaluated after each hit):
    1. remaining_health desc (high health units soak damage first)
    2. cost asc (lose cheap units first when HP is equal)
    3. attack/defense asc (lose weak units first)
    4. remaining_movement asc (lose immobile units first)

    This ensures high-HP units soak damage until their HP equals others,
    then tiebreakers determine who takes subsequent hits.

    Note: Modifies units list in place (removes dead units).
    
    Returns:
        Tuple of (destroyed_ids, wounded_ids) where wounded_ids are units
        that took damage but survived this round.
    """
    stat_name = "attack" if is_attacker else "defense"
    destroyed_ids = []
    wounded_ids = set()  # Use set to avoid duplicates
    remaining_hits = hits

    def sort_key(unit: Unit):
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            return (-1, float('inf'), float('inf'), float('inf'))
        total_cost = sum(unit_def.cost.values())
        stat_value = getattr(unit_def, stat_name, 0)
        # Negative remaining_health for desc sort, others asc
        return (-unit.remaining_health, total_cost, stat_value, unit.remaining_movement)

    # Apply one hit at a time, re-sorting after each to account for HP changes
    while remaining_hits > 0 and units:
        # Sort to find the best target for this hit
        units.sort(key=sort_key)
        target = units[0]

        # Apply one hit
        target.remaining_health -= 1
        remaining_hits -= 1

        # Check if unit is destroyed
        if target.remaining_health == 0:
            destroyed_ids.append(target.instance_id)
            wounded_ids.discard(target.instance_id)  # Don't count as wounded if destroyed
            units.pop(0)  # Remove from list
        else:
            wounded_ids.add(target.instance_id)  # Survived with damage

    return destroyed_ids, list(wounded_ids)


def calculate_required_dice(units: list[Unit], unit_defs: dict[str, UnitDefinition]) -> int:
    """Calculate how many dice rolls are needed for a list of units."""
    total = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        dice_count = getattr(unit_def, 'dice', 1) if unit_def else 1
        total += dice_count
    return total


def group_dice_by_stat(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
) -> dict[int, dict]:
    """
    Group dice rolls by stat value for UI display.

    Returns dict of {stat_value: {"rolls": [rolls], "hits": count}}

    Example with 2 infantry (attack=2) and 1 knight (attack=5):
    {
        2: {"rolls": [3, 1], "hits": 1},  # 2 dice at attack=2, 1 hit (the "1")
        5: {"rolls": [4], "hits": 1}       # 1 die at attack=5, 1 hit (4 <= 5)
    }
    """
    stat_name = "attack" if is_attacker else "defense"

    # First, figure out how many dice each stat value gets (based on unit's dice attr)
    dice_per_stat: dict[int, int] = {}
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue
        stat_value = getattr(unit_def, stat_name, 0)
        dice_count = getattr(unit_def, 'dice', 1)
        dice_per_stat[stat_value] = dice_per_stat.get(stat_value, 0) + dice_count

    # Now distribute rolls to each stat value and count hits
    result: dict[int, dict] = {}
    roll_idx = 0

    # Process in sorted order for deterministic assignment
    for stat_value in sorted(dice_per_stat.keys()):
        dice_count = dice_per_stat[stat_value]
        stat_rolls = []
        hits = 0

        for _ in range(dice_count):
            if roll_idx < len(rolls):
                roll = rolls[roll_idx]
                stat_rolls.append(roll)
                if roll <= stat_value:
                    hits += 1
                roll_idx += 1

        result[stat_value] = {"rolls": stat_rolls, "hits": hits}

    return result


# Legacy class for backwards compatibility (single-round resolution)
@dataclass
class CombatRoundLog:
    """DEPRECATED: Use CombatRoundResult from state.py instead."""
    attacker_rolls: list[int]
    defender_rolls: list[int]
    attacker_hits: int
    defender_hits: int
    attacker_casualties: list[str]
    defender_casualties: list[str]
