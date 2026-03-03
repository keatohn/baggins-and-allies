"""
Combat resolution system.
Simultaneous attacks, defender auto-assigns casualties.
Works with individual Unit instances.
Supports multi-round combat with retreat option.
Defender archers can prefire before round 1 at defense-1 (archetype "archer" or "archer" in tags).
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

# Tags that do NOT count as specials (match frontend getUnitSpecials: exclude ground, mounted).
# All other tags (forest, mountain, fearless, terror, etc.) count as specials for casualty order.
TAGS_NOT_SPECIALS = frozenset({"ground", "mounted"})

# Default terrain bonuses: terrain_type -> bonus (int)
# Unit must have a tag matching the terrain (e.g. "forest") to get the bonus.
# "mountain" and "mountains" are treated as the same (unit tag "mountain" or "mountains" triggers on either terrain).
# Can be overridden via terrain_bonuses passed to compute_terrain_stat_modifiers.
DEFAULT_TERRAIN_BONUSES = {
    "forest": 1,
    "mountain": 1,
    "mountains": 1,
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

    # Mountain/mountains: unit tag "mountain" or "mountains" triggers on either terrain type
    def unit_has_terrain_tag(tags: list, terr: str) -> bool:
        if terr in ("mountain", "mountains"):
            return "mountain" in tags or "mountains" in tags
        return terr in tags

    def apply_for_units(units: list[Unit]) -> dict[str, int]:
        mods: dict[str, int] = {}
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if not unit_def:
                continue
            tags = getattr(unit_def, "tags", []) or []
            if unit_has_terrain_tag(tags, terrain):
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

    attacker_mods = apply_for_units_with_tag(
        attacker_units, defender_has_cavalry)
    defender_mods = apply_for_units_with_tag(
        defender_units, attacker_has_cavalry)
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
    (multiple captains don't stack on same ally). Selection is deterministic: lowest
    (cheapest, then weakest by relevant stat, then instance_id) allies first so backend
    and frontend align. Checked every round.
    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    if bonus == 0 or max_allies <= 0:
        return {}, {}

    def apply_for_side(units: list[Unit], stat_name: str) -> dict[str, int]:
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
            # Same-faction, same-archetype allies (excluding captains); pick lowest first for deterministic alignment with frontend
            captain_faction = getattr(unit_def, "faction", None)
            candidates = [
                u for u in units
                if u.instance_id != unit.instance_id
                and u.instance_id not in boosted
                and "captain" not in (getattr(unit_defs.get(u.unit_id), "tags", None) or [])
                and getattr(unit_defs.get(u.unit_id), "faction", None) == captain_faction
            ]
            # Sort by cost asc, then relevant stat asc, then instance_id
            def key(u: Unit) -> tuple:
                ud = unit_defs.get(u.unit_id)
                if not ud:
                    return (float("inf"), 0, u.instance_id or "")
                c = getattr(ud, "cost", None) or {}
                cost = sum(c.values()) if isinstance(c, dict) else 0
                st = getattr(ud, stat_name, 0)
                return (cost, st, u.instance_id or "")
            candidates.sort(key=key)
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

    attacker_mods = apply_for_side(attacker_units, "attack")
    defender_mods = apply_for_side(defender_units, "defense")
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
    defender_hits_override: int | None = None,
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
      4. num_specials asc (lose units with fewer specials first; unit_def.specials only)
      5. remaining_movement asc (lose immobile units first)
      6. instance_id (tiebreaker)

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
    defender_hits = (
        defender_hits_override
        if defender_hits_override is not None
        else _count_hits(
            defender_units, defender_rolls, unit_defs, is_attacker=False,
            stat_modifiers=stat_modifiers_defender,
        )
    )

    # Apply hits simultaneously - each side takes hits from the OTHER side's rolls
    # Defenders hit attackers, attackers hit defenders. Use effective stat (with modifiers) for loss priority.
    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers_attacker,
    )
    defender_casualties, defender_wounded = _apply_hits(
        defender_units, attacker_hits, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers_defender,
    )

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

        stat_value = getattr(unit_def, stat_name, 0) + \
            mods.get(unit.instance_id, 0)
        dice_count = getattr(unit_def, 'dice', 1)

        # Consume exactly dice_count rolls for this unit (or remaining rolls if fewer)
        for _ in range(dice_count):
            if roll_idx >= len(rolls):
                break
            if rolls[roll_idx] <= stat_value:
                hits += 1
            roll_idx += 1

    # Sanity: we cannot have counted more hits than rolls consumed
    assert hits <= roll_idx <= len(rolls), (
        f"Combat roll mismatch: hits={hits} roll_idx={roll_idx} len(rolls)={len(rolls)}"
    )
    return hits


def _apply_hits(
    units: list[Unit],
    hits: int,
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
    stat_modifiers: dict[str, int] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Apply hits to units, returning (destroyed_ids, wounded_ids).

    Priority order for taking hits (re-evaluated after each hit):
    1. remaining_health desc (high health units soak damage first)
    2. cost asc (lose cheap units first when HP is equal)
    3. effective attack/defense asc (lose weak units first; effective = base + stat_modifiers e.g. captain/terrain)
    4. num_specials asc (lose units with fewer specials first; same logic as frontend getUnitSpecials: tags except ground/mounted + unit_def.specials)
    5. remaining_movement asc (lose immobile units first)
    6. instance_id (deterministic tiebreaker)

    This ensures high-HP units soak damage until their HP equals others,
    then we lose cheap/weak/immobile units before expensive/strong/mobile ones.

    Note: Modifies units list in place (removes dead units).

    Returns:
        Tuple of (destroyed_ids, wounded_ids) where wounded_ids are units
        that took damage but survived this round.
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}
    destroyed_ids = []
    wounded_ids = set()  # Use set to avoid duplicates
    remaining_hits = hits

    def sort_key(unit: Unit):
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            return (-1, float('inf'), float('inf'), float('inf'), float('inf'), unit.instance_id or '')
        cost_dict = getattr(unit_def, 'cost', None) or {}
        total_cost = sum(cost_dict.values()) if isinstance(
            cost_dict, dict) else 0
        base_stat = getattr(unit_def, stat_name, 0)
        effective_stat = base_stat + mods.get(unit.instance_id or '', 0)
        specials_list = getattr(unit_def, 'specials', None) or []
        tags_list = getattr(unit_def, 'tags', None) or []
        # Same as frontend getUnitSpecials: all tags except ground/mounted count as specials, plus unit_def.specials
        num_specials = len(specials_list) if isinstance(specials_list, list) else 0
        if isinstance(tags_list, list):
            num_specials += sum(1 for t in tags_list if t not in TAGS_NOT_SPECIALS)
        # Order: high HP soaks first, then cheap, then weak (effective stat), then fewer specials, then immobile; instance_id for stable tiebreak
        return (-unit.remaining_health, total_cost, effective_stat, num_specials, unit.remaining_movement, unit.instance_id)

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
            # Don't count as wounded if destroyed
            wounded_ids.discard(target.instance_id)
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
    Group dice rolls by stat value for UI display. Rolls are tied to units:
    we iterate units in order (same as flat index order) and append each
    unit's dice to that unit's stat bucket. So each stat row's dice are
    in unit order, matching get_terror_reroll_targets / defender_rerolled_indices_by_stat.

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

    # Build stat -> list of (roll, is_hit) in unit order (same as flat index order)
    result: dict[int, dict] = {}
    roll_idx = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            roll_idx += 1
            continue
        stat_value = getattr(unit_def, stat_name, 0) + mods.get(unit.instance_id, 0)
        dice_count = getattr(unit_def, "dice", 1)
        if stat_value not in result:
            result[stat_value] = {"rolls": [], "hits": 0}
        for _ in range(dice_count):
            if roll_idx < len(rolls):
                roll = rolls[roll_idx]
                result[stat_value]["rolls"].append(roll)
                if roll <= stat_value:
                    result[stat_value]["hits"] += 1
            roll_idx += 1
    return result


def _has_special(unit_def: UnitDefinition | None, special: str) -> bool:
    """Check if unit has the given special (in tags or specials)."""
    if not unit_def:
        return False
    tags = getattr(unit_def, "tags", []) or []
    specials = getattr(unit_def, "specials", []) or []
    return special in tags or special in specials


def get_eff_def_per_flat_index(
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    stat_modifiers_defender: dict[str, int] | None,
) -> list[int]:
    """
    Return effective defense for each flat index in defender rolls.
    result[i] = eff_def for the die at index i. Same order as generate_dice_rolls_for_units.
    """
    mods = stat_modifiers_defender or {}
    result: list[int] = []
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            result.append(0)
            continue
        dice_count = getattr(ud, "dice", 1)
        eff_def = getattr(ud, "defense", 0) + mods.get(unit.instance_id, 0)
        for _ in range(dice_count):
            result.append(eff_def)
    return result


def get_defender_hit_flat_indices(
    defender_units: list[Unit],
    defender_rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    stat_modifiers_defender: dict[str, int] | None,
) -> set[int]:
    """
    Return the set of flat indices into defender_rolls where the roll is a hit
    (roll <= effective defense for that die). Same iteration order as
    generate_dice_rolls_for_units(defender_units).
    """
    mods = stat_modifiers_defender or {}
    hit_indices: set[int] = set()
    roll_idx = 0
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            roll_idx += 1
            continue
        dice_count = getattr(ud, "dice", 1)
        eff_def = getattr(ud, "defense", 0) + mods.get(unit.instance_id, 0)
        for _ in range(dice_count):
            if roll_idx < len(defender_rolls) and defender_rolls[roll_idx] <= eff_def:
                hit_indices.add(roll_idx)
            roll_idx += 1
    return hit_indices


def get_terror_reroll_targets(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    dice_rolls: dict[str, list[int]],
    stat_modifiers_defender: dict[str, int] | None,
    terror_cap: int = 3,
) -> tuple[list[int], int]:
    """
    Round 1 only, before casualties: which defender dice (by flat index) must re-roll due to terror.

    Terror applies when an attacker has "terror" special. ONLY dice that rolled a HIT (roll <=
    effective defense) may be re-rolled. Re-rolling a miss would help the defender (new roll could
    hit). We pick defenders with lowest effective defense first and only mark their hit dice. Cap
    is terror_cap DICE total. If terror would apply to more than the number of defender hits, the
    caller must cap (extra terror does nothing). Units with "fearless" are immune.

    Returns (flat_indices, total_dice) where flat_indices are indices into defender_rolls to
    re-roll—every index is guaranteed to be a hit (roll <= eff_def for that slot).
    """
    defender_rolls = dice_rolls.get("defender", [])
    def_mods = stat_modifiers_defender or {}

    if not any(
        _has_special(unit_defs.get(u.unit_id), "terror") for u in attacker_units
    ):
        return [], 0

    # Per unit: (effective_defense, list of flat indices that were hits)
    roll_idx = 0
    candidates: list[tuple[int, list[int]]] = []
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            roll_idx += 1
            continue
        if _has_special(ud, "fearless"):
            dice_count = getattr(ud, "dice", 1)
            roll_idx += dice_count
            continue
        eff_def = getattr(ud, "defense", 0) + def_mods.get(unit.instance_id, 0)
        dice_count = getattr(ud, "dice", 1)
        hit_indices: list[int] = []
        for _ in range(dice_count):
            if roll_idx < len(defender_rolls) and defender_rolls[roll_idx] <= eff_def:
                hit_indices.append(roll_idx)  # only ever add hits; never re-roll misses
            roll_idx += 1
        if hit_indices:
            candidates.append((eff_def, hit_indices))

    # Sort by effective defense ascending (lowest first), then take hit dice until cap
    candidates.sort(key=lambda x: (x[0], x[1][0] if x[1] else 0))
    flat_indices: list[int] = []
    for _eff_def, indices in candidates:
        for idx in indices:
            if len(flat_indices) >= terror_cap:
                break
            flat_indices.append(idx)
        if len(flat_indices) >= terror_cap:
            break
    return flat_indices, len(flat_indices)


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
