"""
Combat resolution system.
Simultaneous attacks, defender auto-assigns casualties.
Works with individual Unit instances.
Supports multi-round combat with retreat option.
Defender archers can prefire before round 1 at defense-1 (archetype "archer").
Terrain bonuses: units with tag matching terrain get +stat (from terrain_bonuses table).
"""

from dataclasses import dataclass, field
from copy import deepcopy
from typing import TYPE_CHECKING

from backend.engine.state import Unit, CombatRoundResult
from backend.engine.definitions import UnitDefinition

if TYPE_CHECKING:
    from backend.engine.definitions import TerritoryDefinition

ARCHETYPE_ARCHER = "archer"
ARCHETYPE_CAVALRY = "cavalry"

# Default terrain bonuses: terrain_type -> bonus (int)
# Unit must have a tag matching the terrain (e.g. "forest") to get the bonus.
# Bonus applies to whatever stat they roll: attack for attackers, defense for defenders.
# Can be overridden via terrain_bonuses passed to compute_terrain_stat_modifiers.
DEFAULT_TERRAIN_BONUSES = {
    "forest": 1,
    "mountain": 1,
    "city": 1,
}


def compute_terrain_stat_modifiers(
    territory_def: "TerritoryDefinition | None",
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    terrain_bonuses: dict[str, int] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Compute stat modifiers for terrain bonuses.

    Units with a tag matching the territory's terrain_type get the terrain bonus.
    Bonus applies to attack for attackers, defense for defenders - same bonus either way.
    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    table = terrain_bonuses if terrain_bonuses is not None else DEFAULT_TERRAIN_BONUSES
    attacker_mods: dict[str, int] = {}
    defender_mods: dict[str, int] = {}

    if not territory_def:
        return attacker_mods, defender_mods

    terrain = getattr(territory_def, "terrain_type", None)
    if not terrain or terrain not in table:
        return attacker_mods, defender_mods

    bonus = table[terrain] if isinstance(table[terrain], int) else 0
    if bonus == 0:
        return attacker_mods, defender_mods

    def apply_for_units(units: list[Unit]) -> dict[str, int]:
        mods: dict[str, int] = {}
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if not unit_def:
                continue
            tags = getattr(unit_def, "tags", []) or []
            if terrain in tags:
                mods[unit.instance_id] = bonus
        return mods

    attacker_mods = apply_for_units(attacker_units)
    defender_mods = apply_for_units(defender_units)
    return attacker_mods, defender_mods


def compute_anti_cavalry_stat_modifiers(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    bonus: int = 1,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Anti-cavalry: units with "anti_cavalry" tag get +bonus when opposing side has cavalry.

    Checked every round so bonus goes away when all cavalry die. Uses archetype for cavalry.
    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    if bonus == 0:
        return {}, {}

    def has_cavalry(units: list[Unit]) -> bool:
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if unit_def and getattr(unit_def, "archetype", "") == ARCHETYPE_CAVALRY:
                return True
        return False

    def apply_for_units_with_tag(units: list[Unit], has_opposing_cavalry: bool) -> dict[str, int]:
        if not has_opposing_cavalry:
            return {}
        mods: dict[str, int] = {}
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if not unit_def:
                continue
            tags = getattr(unit_def, "tags", []) or []
            if "anti_cavalry" in tags:
                mods[unit.instance_id] = bonus
        return mods

    defender_has_cavalry = has_cavalry(defender_units)
    attacker_has_cavalry = has_cavalry(attacker_units)

    attacker_mods = apply_for_units_with_tag(attacker_units, defender_has_cavalry)
    defender_mods = apply_for_units_with_tag(defender_units, attacker_has_cavalry)
    return attacker_mods, defender_mods


def compute_captain_stat_modifiers(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    bonus: int = 1,
    max_allies: int = 3,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Captain: units with "captain" tag grant up to max_allies same-archetype allies +bonus.

    Bonus applies to attack for attackers, defense for defenders. Each unit gets max +1
    (multiple captains don't stack on same ally). Checked every round.
    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    if bonus == 0 or max_allies <= 0:
        return {}, {}

    def apply_for_side(units: list[Unit]) -> dict[str, int]:
        mods: dict[str, int] = {}
        boosted: set[str] = set()
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if not unit_def:
                continue
            tags = getattr(unit_def, "tags", []) or []
            if "captain" not in tags:
                continue
            archetype = getattr(unit_def, "archetype", "")
            # Same-archetype allies (excluding captains; only base units get boosted)
            candidates = [
                u for u in units
                if u.instance_id != unit.instance_id
                and u.instance_id not in boosted
                and "captain" not in (getattr(unit_defs.get(u.unit_id), "tags", None) or [])
            ]
            count = 0
            for ally in candidates:
                if count >= max_allies:
                    break
                ally_def = unit_defs.get(ally.unit_id)
                if ally_def and getattr(ally_def, "archetype", "") == archetype:
                    mods[ally.instance_id] = bonus
                    boosted.add(ally.instance_id)
                    count += 1
        return mods

    attacker_mods = apply_for_side(attacker_units)
    defender_mods = apply_for_side(defender_units)
    return attacker_mods, defender_mods


def merge_stat_modifiers(*mod_dicts: dict[str, int] | None) -> dict[str, int]:
    """Merge multiple modifier dicts by adding values for same instance_id."""
    result: dict[str, int] = {}
    for d in mod_dicts:
        if not d:
            continue
        for iid, val in d.items():
            result[iid] = result.get(iid, 0) + val
    return result


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
    stat_modifiers_attacker: dict[str, int] | None = None,
    stat_modifiers_defender: dict[str, int] | None = None,
) -> RoundResult:
    """
    Resolve a single combat round.

    Combat rules:
    - Simultaneous: both sides roll and apply hits simultaneously
    - Each unit rolls 1 die (regardless of HP)
    - Hit if roll <= attack (for attacker) or <= defense (for defender)
    - stat_modifiers_*: optional instance_id -> modifier (e.g. terrain bonuses)
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
        stat_modifiers_attacker: instance_id -> attack modifier (e.g. terrain)
        stat_modifiers_defender: instance_id -> defense modifier (e.g. terrain)

    Returns:
        RoundResult with casualties and survivor info
    """
    attacker_rolls = dice_rolls.get("attacker", [])
    defender_rolls = dice_rolls.get("defender", [])

    # Count hits using actual unit stats (with optional modifiers)
    attacker_hits = _count_hits(
        attacker_units, attacker_rolls, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers_attacker,
    )
    defender_hits = _count_hits(
        defender_units, defender_rolls, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers_defender,
    )

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
    stat_modifiers: dict[str, int] | None = None,
) -> int:
    """
    Count hits from dice rolls using actual unit attack/defense values.

    Each unit rolls dice based on its unit definition's 'dice' attribute.
    A roll is a hit if roll <= (unit stat + stat_modifiers.get(instance_id, 0)).
    stat_modifiers: optional instance_id -> modifier to add to the stat for this roll (e.g. -1 for archer prefire).
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}

    hits = 0
    roll_idx = 0

    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue

        stat_value = getattr(unit_def, stat_name, 0) + mods.get(unit.instance_id, 0)
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
    stat_modifiers: dict[str, int] | None = None,
) -> dict[int, dict]:
    """
    Group dice rolls by stat value for UI display.

    Returns dict of {stat_value: {"rolls": [rolls], "hits": count}}
    stat_modifiers: optional instance_id -> modifier (e.g. -1 for archer prefire defense).

    Example with 2 infantry (attack=2) and 1 knight (attack=5):
    {
        2: {"rolls": [3, 1], "hits": 1},  # 2 dice at attack=2, 1 hit (the "1")
        5: {"rolls": [4], "hits": 1}       # 1 die at attack=5, 1 hit (4 <= 5)
    }
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}

    # First, figure out how many dice each (stat_value) gets (based on unit's dice + modifier)
    dice_per_stat: dict[int, int] = {}
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue
        stat_value = getattr(unit_def, stat_name, 0) + mods.get(unit.instance_id, 0)
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


def resolve_archer_prefire(
    attacker_units: list[Unit],
    defender_archer_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_rolls: list[int],
    stat_modifiers_defender_extra: dict[str, int] | None = None,
) -> RoundResult:
    """
    Resolve defender archer prefire: only archers roll at defense-1, hits applied to attackers only.
    Modifies attacker_units in place (removes dead, decrements health).
    defender_archer_units are not modified (no defender casualties from prefire).
    stat_modifiers_defender_extra: optional instance_id -> extra modifier (e.g. terrain bonus), merged with -1.
    """
    extra = stat_modifiers_defender_extra or {}
    stat_modifiers = {
        u.instance_id: -1 + extra.get(u.instance_id, 0) for u in defender_archer_units
    }
    defender_hits = _count_hits(
        defender_archer_units, defender_rolls, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers,
    )
    attacker_hits = 0  # Attackers do not roll in prefire

    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True
    )
    defender_casualties: list[str] = []
    defender_wounded: list[str] = []

    return RoundResult(
        attacker_hits=attacker_hits,
        defender_hits=defender_hits,
        attacker_casualties=attacker_casualties,
        defender_casualties=defender_casualties,
        attacker_wounded=attacker_wounded,
        defender_wounded=defender_wounded,
        surviving_attacker_ids=[u.instance_id for u in attacker_units],
        surviving_defender_ids=[u.instance_id for u in defender_archer_units],
        attackers_eliminated=len(attacker_units) == 0,
        defenders_eliminated=False,
    )


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
