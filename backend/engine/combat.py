"""
Combat resolution system.
Simultaneous attacks, defender auto-assigns casualties.
Works with individual Unit instances.
Supports multi-round combat with retreat option.
Defender units with the archer special can prefire before round 1 at defense-1.
Terrain bonuses: units with tag matching terrain get +stat (from terrain_bonuses table).
"""

from dataclasses import dataclass, field
from copy import deepcopy
from itertools import groupby
from typing import TYPE_CHECKING

from backend.engine.state import Unit, CombatRoundResult
from backend.engine.definitions import UnitDefinition
from backend.engine.utils import (
    can_conquer_territory_as_attacker,
    has_unit_special,
    has_unit_tag,
    is_aerial_unit,
    is_siegework_archetype,
)

if TYPE_CHECKING:
    from backend.engine.definitions import TerritoryDefinition

ARCHETYPE_ARCHER = "archer"
ARCHETYPE_CAVALRY = "cavalry"
ARCHETYPE_INFANTRY = "infantry"
ARCHETYPE_SIEGEWORK = "siegework"

# Ram: stronghold-only hits in siegeworks round (normal attack roll). Ladder: no die in siegeworks;
# assigned infantry bypass stronghold in standard combat rounds (see resolve_combat_round).
SIEGEWORK_SPECIAL_RAM = "ram"
# Does not roll; allows 2 infantry (worst to best) to roll toward defender units, bypassing stronghold HP
SIEGEWORK_SPECIAL_LADDER = "ladder"


def _is_siegework_unit(unit_def: UnitDefinition | None) -> bool:
    """True if unit is siegework (archetype). Siegework units only roll in the dedicated siegeworks round."""
    if not unit_def:
        return False
    return getattr(unit_def, "archetype", "") == ARCHETYPE_SIEGEWORK


def _can_climb_ladder(unit_def: UnitDefinition | None) -> bool:
    """True if unit has 'climbs_ladder' in tags. Used for ladder assignment (who can go on ladders)."""
    if not unit_def:
        return False
    return "climbs_ladder" in (getattr(unit_def, "tags", None) or [])


def get_ladder_equipment_units(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> list[Unit]:
    """Attacking siegework units with ladder special (each uses transport_capacity for infantry slots)."""
    return [
        u for u in attacker_units
        if _is_siegework_unit(unit_defs.get(u.unit_id))
        and has_unit_special(unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_LADDER)
    ]


def get_ladder_infantry_capacity(
    ladder_equipment: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> int:
    """Total infantry slots: sum of transport_capacity on each ladder unit."""
    total = 0
    for u in ladder_equipment:
        cap = getattr(unit_defs.get(u.unit_id), "transport_capacity", 0) or 0
        total += max(0, int(cap))
    return total


def get_ladder_infantry_instance_ids(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> list[str]:
    """
    Pick attacker infantry "on ladders" up to total ladder transport_capacity (sum per ladder unit).
    Units with climbs_ladder tag. Ordering between buckets: cost (asc), attack (asc), specials (asc),
    unit_id — same "worst climbers first" meta as before (without instance_id in this key).
    Within one bucket (same type and those stats), instance_id descending so ladder assignment
    skews opposite attacker casualty tie-breaking in _apply_hits (instance_id ascending among ties):
    among identical climbers, higher instance_id is on the ladder and tends to die last.
    Those units' hits in normal combat bypass stronghold HP.
    """
    ladder_equipment = get_ladder_equipment_units(attacker_units, unit_defs)
    total_capacity = get_ladder_infantry_capacity(ladder_equipment, unit_defs)
    if total_capacity <= 0:
        return []
    climbers = [u for u in attacker_units if _can_climb_ladder(
        unit_defs.get(u.unit_id))]
    if not climbers:
        return []

    def meta_key(unit: Unit) -> tuple[int, int, int, str]:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            return (0, 0, 0, unit.unit_id or "")
        cost_dict = getattr(ud, "cost", None) or {}
        total_cost = sum(cost_dict.values()) if isinstance(
            cost_dict, dict) else 0
        attack = getattr(ud, "attack", 0)
        specials_list = getattr(ud, "specials", None) or []
        num_specials = len(specials_list) if isinstance(
            specials_list, list) else 0
        return (total_cost, attack, num_specials, unit.unit_id or "")

    climbers.sort(key=meta_key)
    ordered: list[Unit] = []
    for _, group in groupby(climbers, key=meta_key):
        grp = list(group)
        grp.sort(key=lambda u: u.instance_id or "", reverse=True)
        ordered.extend(grp)
    n = min(total_capacity, len(ordered))
    return [u.instance_id for u in ordered[:n]]


def get_siegework_attacker_rolling_units(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_territory_is_stronghold: bool,
    defender_stronghold_hp: int | None = None,
    *,
    fuse_bomb: bool = True,
) -> list[Unit]:
    """
    Attackers who roll in the siegeworks round, in combat roster order:
    all siegework except ladder, plus non-siegework units with ram only when attacking a stronghold
    that still has stronghold HP (ram hits soak walls only; no roll when walls are gone or vs non-stronghold).
    Bomb carriers roll in siegeworks only when paired with a bombikazi and fuse_bomb is True.
    Unpaired bombs never roll here (0 dice in siegeworks). When fuse_bomb is False, paired bombs
    are omitted too (same as an unpaired bombikazi: no siegework participation).
    """
    ram_ok = (
        defender_territory_is_stronghold
        and defender_stronghold_hp is not None
        and defender_stronghold_hp > 0
    )
    _, paired_bombs = get_bombikazi_pairing(attacker_units, unit_defs)
    out: list[Unit] = []
    for u in attacker_units:
        ud = unit_defs.get(u.unit_id)
        if not ud:
            continue
        if _is_bomb_carrier_unit(ud):
            if u.instance_id not in paired_bombs or not fuse_bomb:
                continue
        if _is_siegework_unit(ud) and has_unit_special(ud, SIEGEWORK_SPECIAL_LADDER):
            continue
        if _is_siegework_unit(ud):
            out.append(u)
        elif has_unit_special(ud, SIEGEWORK_SPECIAL_RAM) and ram_ok:
            out.append(u)
    return out


def get_siegework_dice_counts(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_territory_is_stronghold: bool = False,
    defender_stronghold_hp: int | None = None,
    *,
    fuse_bomb: bool = True,
) -> tuple[int, int]:
    """Return (attacker_siegework_round_dice, defender_siegework_dice).
    Ladder and ram when walls gone / non-stronghold excluded.
    When both are 0, there is no dedicated siegework *dice* round (e.g. siege ladders alone)."""
    rolling = get_siegework_attacker_rolling_units(
        attacker_units, unit_defs, defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp,
        fuse_bomb=fuse_bomb,
    )
    att = sum(getattr(unit_defs.get(u.unit_id), "dice", 1) for u in rolling)

    def def_dice(units: list[Unit]) -> int:
        return sum(
            getattr(unit_defs.get(u.unit_id), "dice", 1)
            for u in units
            if _is_siegework_unit(unit_defs.get(u.unit_id))
            and not has_unit_special(unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_LADDER)
        )

    return att, def_dice(defender_units)


def siegework_dice_round_applies(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_territory_is_stronghold: bool = False,
    defender_stronghold_hp: int | None = None,
    *,
    fuse_bomb: bool = True,
) -> tuple[bool, int, int]:
    """
    True when the engine runs the dedicated siegework *dice* round (at least one die on either side).
    Uses get_siegework_dice_counts only — e.g. siege ladders alone contribute no attacker dice here.
    Returns (applies, attacker_siegework_dice, defender_siegework_dice).
    """
    a, d = get_siegework_dice_counts(
        attacker_units,
        defender_units,
        unit_defs,
        defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp,
        fuse_bomb=fuse_bomb,
    )
    return (a > 0 or d > 0), a, d


def _is_naval_unit(unit_def: UnitDefinition | None) -> bool:
    """True if unit is naval (ship/boat). Used for naval combat casualty order (cargo value)."""
    if not unit_def:
        return False
    arch = getattr(unit_def, "archetype", "") or ""
    tags = getattr(unit_def, "tags", []) or []
    return arch == "naval" or "naval" in tags


# Tags that do NOT count as specials (match frontend getUnitSpecials: exclude land, mounted).
# All other tags (forest, mountain, fearless, terror, aerial, etc.) count as specials for casualty order.
TAGS_NOT_SPECIALS = frozenset({"land", "mounted"})

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

    # Mountain/mountains: unit tag/special "mountain" or "mountains" triggers on either terrain type
    def unit_has_terrain_tag(unit_def: UnitDefinition | None, terr: str) -> bool:
        if not unit_def:
            return False
        if terr in ("mountain", "mountains"):
            return has_unit_special(unit_def, "mountain") or has_unit_special(unit_def, "mountains")
        return has_unit_special(unit_def, terr)

    def apply_for_units(units: list[Unit]) -> dict[str, int]:
        mods: dict[str, int] = {}
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if not unit_def:
                continue
            if unit_has_terrain_tag(unit_def, terrain):
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
    Anti-cavalry:
    - Each combat round, count how many cavalry units are present on the opposing side.
    - Then select that many anti-cavalry units on this side and grant +bonus to each.
    - Selection order is the same key used by captain bonuses (cost asc, relevant stat asc,
      specials count asc, instance_id) so "lesser" anti-cavalry units get boosted first.

    Checked every round so bonuses re-evaluate when cavalry units die.
    Uses unit_defs[unit_id].archetype == ARCHETYPE_CAVALRY to identify cavalry.

    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    if bonus == 0:
        return {}, {}

    def count_cavalry(units: list[Unit]) -> int:
        n = 0
        for unit in units:
            unit_def = unit_defs.get(unit.unit_id)
            if unit_def and getattr(unit_def, "archetype", "") == ARCHETYPE_CAVALRY:
                n += 1
        return n

    def apply_for_side(
        side_units: list[Unit],
        enemy_cavalry_count: int,
        relevant_stat_name: str,
    ) -> dict[str, int]:
        if enemy_cavalry_count <= 0:
            return {}
        if not side_units:
            return {}

        candidates = [
            u
            for u in side_units
            if has_unit_special(unit_defs.get(u.unit_id), "anti_cavalry")
        ]
        if not candidates:
            return {}

        def key(u: Unit) -> tuple:
            ud = unit_defs.get(u.unit_id)
            if not ud:
                return (float("inf"), 0, 0, u.instance_id or "")
            c = getattr(ud, "cost", None) or {}
            cost = sum(c.values()) if isinstance(c, dict) else 0
            st = getattr(ud, relevant_stat_name, 0)
            sp = getattr(ud, "specials", None) or []
            tags = getattr(ud, "tags", None) or []
            num_specials = len(sp) if isinstance(sp, list) else 0
            num_specials += len([t for t in tags if t not in sp]) if isinstance(tags, list) else 0
            return (cost, st, num_specials, u.instance_id or "")

        candidates.sort(key=key)
        mods: dict[str, int] = {}
        for u in candidates[:enemy_cavalry_count]:
            mods[u.instance_id] = bonus
        return mods

    defender_cavalry_count = count_cavalry(defender_units)
    attacker_cavalry_count = count_cavalry(attacker_units)

    # Anti-cavalry boosts:
    # - attacker-side anti-cavalry boosts attack when defender has cavalry
    # - defender-side anti-cavalry boosts defense when attacker has cavalry
    attacker_mods = apply_for_side(attacker_units, defender_cavalry_count, "attack")
    defender_mods = apply_for_side(defender_units, attacker_cavalry_count, "defense")
    return attacker_mods, defender_mods


def compute_captain_stat_modifiers(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    bonus: int = 1,
    max_allies: int = 3,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Captain: units with "captain" special (tags or specials list) grant up to max_allies
    same-archetype allies +bonus. Attack for attackers, defense for defenders.
    Each ally gets at most +1 (two captains cannot both boost the same 3 units; the second
    captain boosts the next 3 unboosted same-archetype allies). Selection order: cost asc,
    then relevant stat (attack/defense) asc, then specials count asc, then instance_id.
    Returns (attacker_modifiers, defender_modifiers) as instance_id -> modifier.
    """
    if bonus == 0 or max_allies <= 0:
        return {}, {}

    def _has_captain(ud: UnitDefinition | None) -> bool:
        return has_unit_special(ud, "captain")

    def apply_for_side(units: list[Unit], stat_name: str) -> dict[str, int]:
        mods: dict[str, int] = {}
        boosted: set[str] = set()
        captains = [u for u in units if _has_captain(unit_defs.get(u.unit_id))]
        captains.sort(key=lambda u: u.instance_id or "")
        for captain_unit in captains:
            unit_def = unit_defs.get(captain_unit.unit_id)
            if not unit_def:
                continue
            archetype = getattr(unit_def, "archetype", "")
            # Same-archetype allies (excluding captains), not already boosted
            candidates = [
                u for u in units
                if u.instance_id != captain_unit.instance_id
                and u.instance_id not in boosted
                and not _has_captain(unit_defs.get(u.unit_id))
            ]
            # Same archetype only
            candidates = [u for u in candidates if getattr(
                unit_defs.get(u.unit_id), "archetype", "") == archetype]
            # Sort: cost asc, stat asc, specials count asc, instance_id

            def key(u: Unit) -> tuple:
                ud = unit_defs.get(u.unit_id)
                if not ud:
                    return (float("inf"), 0, 0, u.instance_id or "")
                c = getattr(ud, "cost", None) or {}
                cost = sum(c.values()) if isinstance(c, dict) else 0
                st = getattr(ud, stat_name, 0)
                sp = getattr(ud, "specials", None) or []
                tags = getattr(ud, "tags", None) or []
                num_specials = len(sp) if isinstance(sp, list) else 0
                num_specials += len([t for t in tags if t not in sp]
                                    ) if isinstance(tags, list) else 0
                return (cost, st, num_specials, u.instance_id or "")
            candidates.sort(key=key)
            count = 0
            for ally in candidates:
                if count >= max_allies:
                    break
                mods[ally.instance_id] = bonus
                boosted.add(ally.instance_id)
                count += 1
        return mods

    attacker_mods = apply_for_side(attacker_units, "attack")
    defender_mods = apply_for_side(defender_units, "defense")
    return attacker_mods, defender_mods


def compute_sea_raider_stat_modifiers(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    is_sea_raid: bool,
    bonus: int = 1,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Sea Raider special: attackers with the sea_raider special get +bonus attack when
    they are fighting as part of a sea raid — i.e. passengers who came ashore from
    ships. The battle itself is still normal land combat (land units roll; boats are
    not attacking units in that fight). In the live game, sea_zone_id on the combat
    marks that staging; in the sim, the caller passes is_sea_raid=True to model the
    same bonus. Does not imply naval combat rules or naval casualty targeting.
    Returns (attacker_modifiers, defender_modifiers); defender_modifiers is always {}.
    """
    if not is_sea_raid or bonus == 0:
        return {}, {}

    mods: dict[str, int] = {}
    for unit in attacker_units:
        unit_def = unit_defs.get(unit.unit_id)
        if unit_def and has_unit_special(unit_def, "sea_raider"):
            mods[unit.instance_id] = bonus
    return mods, {}


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
    attacker_effective_dice_override: dict[str, int] | None = None,
    attacker_effective_attack_override: dict[str, int] | None = None,
    bombikazi_self_destruct_ids: list[str] | None = None,
    casualty_order_attacker: str = "best_unit",
    casualty_order_defender: str = "best_unit",
    must_conquer: bool = False,
    is_naval_combat_attacker: bool = False,
    is_naval_combat_defender: bool = False,
    defender_stronghold_hp: int | None = None,
    defender_territory_is_stronghold: bool = False,
    exclude_archetypes_from_rolling: list[str] | None = None,
    attacker_ladder_instance_ids: set[str] | None = None,
) -> tuple[RoundResult, int | None]:
    """
    Resolve a single combat round.

    Combat rules:
    - Simultaneous: both sides roll and apply hits simultaneously
    - Each unit rolls 1 die (regardless of HP)
    - When exclude_archetypes_from_rolling is set (e.g. ["siegework"]), those units do not roll this round (no dice consumed).
    - Hit if roll <= attack (for attacker) or <= defense (for defender)
    - When defender_stronghold_hp is set, attacker hits soak the stronghold first, then overflow to defender units. When attacker_ladder_instance_ids is set, those units' hits go directly to defender units (bypass stronghold); only non-ladder attacker hits soak the stronghold.
    - stat_modifiers_*: optional instance_id -> modifier (e.g. terrain bonuses)
    - Casualties auto-assigned by priority:
      1. remaining_health desc (soak hits with high health units)
      2. cost asc (lose cheap units first)
      3. attack/defense asc, num_specials asc, remaining_movement asc, instance_id (tiebreaker)

    Returns:
        (RoundResult, defender_stronghold_hp_after) — hp_after is None if stronghold not in use.
    """
    attacker_rolls = dice_rolls.get("attacker", [])
    defender_rolls = dice_rolls.get("defender", [])
    exclude_arch = set(
        exclude_archetypes_from_rolling) if exclude_archetypes_from_rolling else None
    ladder_ids = attacker_ladder_instance_ids or set()

    if ladder_ids:
        ladder_hits, other_attacker_hits = _count_hits_split(
            attacker_units, attacker_rolls, unit_defs, is_attacker=True,
            ladder_instance_ids=ladder_ids,
            stat_modifiers=stat_modifiers_attacker,
            effective_dice_override=attacker_effective_dice_override,
            effective_stat_override=attacker_effective_attack_override,
            exclude_archetypes=exclude_arch,
        )
        attacker_hits = ladder_hits + other_attacker_hits
    else:
        ladder_hits, other_attacker_hits = 0, 0
        attacker_hits = _count_hits(
            attacker_units, attacker_rolls, unit_defs, is_attacker=True,
            stat_modifiers=stat_modifiers_attacker,
            effective_dice_override=attacker_effective_dice_override,
            effective_stat_override=attacker_effective_attack_override,
            exclude_archetypes=exclude_arch,
        )
        other_attacker_hits = attacker_hits

    defender_hits = (
        defender_hits_override
        if defender_hits_override is not None
        else _count_hits(
            defender_units, defender_rolls, unit_defs, is_attacker=False,
            stat_modifiers=stat_modifiers_defender,
            exclude_archetypes=exclude_arch,
        )
    )

    # Stronghold: only non-ladder attacker hits soak it; ladder infantry hits go straight to defender units
    hits_to_stronghold = 0
    defender_stronghold_hp_after: int | None = None
    if defender_stronghold_hp is not None and defender_stronghold_hp > 0 and other_attacker_hits > 0:
        hits_to_stronghold = min(other_attacker_hits, defender_stronghold_hp)
        defender_stronghold_hp_after = max(
            0, defender_stronghold_hp - hits_to_stronghold)
    elif defender_stronghold_hp is not None:
        defender_stronghold_hp_after = defender_stronghold_hp

    hits_to_defender_units = ladder_hits + \
        (other_attacker_hits - hits_to_stronghold)

    # Apply hits simultaneously - each side takes hits from the OTHER side's rolls
    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers_attacker,
        casualty_order=casualty_order_attacker,
        must_conquer=must_conquer,
        is_naval_combat=is_naval_combat_attacker,
    )
    # Defender: when ladder hits exist, apply ladder hits first (non-stronghold first), then remaining (stronghold first)

    def _defender_apply(n: int, from_ladder: bool):
        return _apply_hits(
            defender_units, n, unit_defs, is_attacker=False,
            stat_modifiers=stat_modifiers_defender,
            casualty_order=casualty_order_defender,
            must_conquer=False,
            is_naval_combat=is_naval_combat_defender,
            territory_is_stronghold=defender_territory_is_stronghold,
            hits_from_ladder=from_ladder,
        )
    if ladder_hits > 0 and hits_to_defender_units > 0:
        ladder_cas, ladder_wound = _defender_apply(
            ladder_hits, from_ladder=True)
        rest_hits = hits_to_defender_units - ladder_hits
        rest_cas, rest_wound = _defender_apply(
            rest_hits, from_ladder=False) if rest_hits > 0 else ([], [])
        defender_casualties = ladder_cas + rest_cas
        defender_wounded = list(set(ladder_wound) | set(rest_wound))
    else:
        defender_casualties, defender_wounded = _defender_apply(
            hits_to_defender_units, from_ladder=False)

    # Bombikazi: paired bombikazi + bomb self-destruct (add to casualties, remove from list)
    if bombikazi_self_destruct_ids:
        self_destruct_set = set(bombikazi_self_destruct_ids)
        for uid in bombikazi_self_destruct_ids:
            attacker_casualties.append(uid)
        attacker_units[:] = [
            u for u in attacker_units if u.instance_id not in self_destruct_set]

    result = RoundResult(
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
    return result, defender_stronghold_hp_after


def _count_hits(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
    stat_modifiers: dict[str, int] | None = None,
    effective_dice_override: dict[str, int] | None = None,
    effective_stat_override: dict[str, int] | None = None,
    exclude_archetypes: set[str] | None = None,
) -> int:
    """
    Count hits from dice rolls using actual unit attack/defense values.

    Each unit rolls dice based on its unit definition's 'dice' attribute
    (or effective_dice_override[instance_id] when provided, e.g. for bombikazi).
    A roll is a hit if roll <= (unit stat + mods), or effective_stat_override[instance_id]
    when provided (e.g. bombikazi uses bomb's attack).
    When exclude_archetypes is set (e.g. {"siegework"}), those units do not roll this round:
    they do not consume dice and do not contribute hits.
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}
    stat_override = effective_stat_override or {}
    skip_archetypes = exclude_archetypes or set()

    hits = 0
    roll_idx = 0

    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue

        if getattr(unit_def, "archetype", "") in skip_archetypes:
            continue

        dice_count = (
            effective_dice_override.get(
                unit.instance_id, getattr(unit_def, "dice", 1))
            if effective_dice_override is not None
            else getattr(unit_def, "dice", 1)
        )

        if unit.instance_id in stat_override:
            stat_value = stat_override[unit.instance_id]
        else:
            stat_value = getattr(unit_def, stat_name, 0) + \
                mods.get(unit.instance_id, 0)

        for _ in range(dice_count):
            if roll_idx >= len(rolls):
                break
            if rolls[roll_idx] <= stat_value:
                hits += 1
            roll_idx += 1

    assert hits <= roll_idx <= len(rolls), (
        f"Combat roll mismatch: hits={hits} roll_idx={roll_idx} len(rolls)={len(rolls)}"
    )
    return hits


def _count_hits_split(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
    ladder_instance_ids: set[str],
    stat_modifiers: dict[str, int] | None = None,
    effective_dice_override: dict[str, int] | None = None,
    effective_stat_override: dict[str, int] | None = None,
    exclude_archetypes: set[str] | None = None,
) -> tuple[int, int]:
    """
    Like _count_hits but returns (hits_from_ladder_units, hits_from_other_units).
    Only used for attacker when ladder_instance_ids is non-empty (ladder infantry bypass stronghold).
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}
    stat_override = effective_stat_override or {}
    skip_archetypes = exclude_archetypes or set()
    ladder_hits = 0
    other_hits = 0
    roll_idx = 0

    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue

        if getattr(unit_def, "archetype", "") in skip_archetypes:
            continue

        dice_count = (
            effective_dice_override.get(
                unit.instance_id, getattr(unit_def, "dice", 1))
            if effective_dice_override is not None
            else getattr(unit_def, "dice", 1)
        )
        on_ladder = unit.instance_id in ladder_instance_ids

        if unit.instance_id in stat_override:
            stat_value = stat_override[unit.instance_id]
        else:
            stat_value = getattr(unit_def, stat_name, 0) + \
                mods.get(unit.instance_id, 0)

        for _ in range(dice_count):
            if roll_idx >= len(rolls):
                break
            if rolls[roll_idx] <= stat_value:
                if on_ladder:
                    ladder_hits += 1
                else:
                    other_hits += 1
            roll_idx += 1

    assert ladder_hits + other_hits <= roll_idx <= len(rolls)
    return ladder_hits, other_hits


def _apply_hits(
    units: list[Unit],
    hits: int,
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
    stat_modifiers: dict[str, int] | None = None,
    casualty_order: str = "best_unit",
    must_conquer: bool = False,
    is_naval_combat: bool = False,
    territory_is_stronghold: bool = False,
    hits_from_ladder: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Apply hits to units, returning (destroyed_ids, wounded_ids).

    casualty_order: "best_unit" | "best_attack" (attacker) or "best_unit" | "best_defense" (defender).
    must_conquer: attacker-only; when True, if the next hit would kill the last ground unit, assign that hit to an aerial
    instead (so a ground unit can conquer).
    is_naval_combat: only naval and aerial units can take hits (passengers are not targets). Naval uses cargo_value (asc)
    after best_unit/best_attack in the sort order.
    territory_is_stronghold: defender only; when True, defender units are in a stronghold territory (strongholds take first hits).
    hits_from_ladder: defender only; when True, these hits came from ladder attackers and assign to non-stronghold first
    (is_stronghold asc). When False and territory_is_stronghold, stronghold takes first (is_stronghold desc).

    Priority order for taking hits (re-evaluated after each hit):
    1. is_stronghold: desc for normal hits (stronghold first), asc for hits_from_ladder (non-stronghold first). Attacker: not used.
    2. remaining_health desc (healthiest soaks first)
    3. best_unit / best_attack (cost vs effective stat × unit dice per battle config)
    4. (Naval only) cargo_value asc
    5. num_specials asc, remaining_movement asc, instance_id

    Note: Modifies units list in place (removes dead units).
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}
    destroyed_ids = []
    wounded_ids = set()
    remaining_hits = hits
    use_stat_before_cost = casualty_order in ("best_attack", "best_defense")
    # Defender in stronghold: normal hits → stronghold first (-1); ladder hits → non-stronghold first (1 so stronghold last)
    stronghold_sort = 0
    if not is_attacker and territory_is_stronghold:
        stronghold_sort = 1 if hits_from_ladder else -1

    def _cargo_sort_key(boat_unit: Unit, all_units: list[Unit]) -> tuple[int, int, tuple[int, ...]]:
        """Naval: (cargo_value_sum, num_passengers, tuple(sorted passenger costs)). Asc = sink low-value first."""
        boat_id = boat_unit.instance_id or ""
        costs: list[int] = []
        for u in all_units:
            if getattr(u, "loaded_onto", None) != boat_id:
                continue
            ud = unit_defs.get(u.unit_id)
            if not ud:
                continue
            cost_dict = getattr(ud, "cost", None) or {}
            c = sum(cost_dict.values()) if isinstance(cost_dict, dict) else 0
            costs.append(c)
        costs.sort()
        return (sum(costs), len(costs), tuple(costs))

    def sort_key(unit: Unit):
        """Order: stronghold key, remaining_health desc, cost/stat per config, cargo (naval), num_specials, mov, instance_id."""
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            return (0, -1, 0, 0, (0, 0, ()), 0, unit.remaining_movement, unit.instance_id or '')
        cost_dict = getattr(unit_def, 'cost', None) or {}
        total_cost = sum(cost_dict.values()) if isinstance(
            cost_dict, dict) else 0
        base_stat = getattr(unit_def, stat_name, 0)
        effective_stat = base_stat + mods.get(unit.instance_id or '', 0)
        dice_n = getattr(unit_def, "dice", 1)
        if not isinstance(dice_n, int) or dice_n < 1:
            dice_n = 1
        stat_for_casualty_order = effective_stat * dice_n
        specials_list = getattr(unit_def, 'specials', None) or []
        num_specials = len(specials_list) if isinstance(
            specials_list, list) else 0
        cargo_key = _cargo_sort_key(unit, units) if (
            is_naval_combat and _is_naval_unit(unit_def)) else (0, 0, ())
        if use_stat_before_cost:
            cost_stat = (-unit.remaining_health, stat_for_casualty_order, total_cost, cargo_key,
                         num_specials, unit.remaining_movement, unit.instance_id or '')
        else:
            cost_stat = (-unit.remaining_health, total_cost, stat_for_casualty_order, cargo_key,
                         num_specials, unit.remaining_movement, unit.instance_id or '')
        return (stronghold_sort,) + cost_stat

    # Naval combat: only naval and aerial units can take hits (passengers are not targets)
    if is_naval_combat:
        eligible = [u for u in units if _is_naval_unit(unit_defs.get(
            u.unit_id)) or is_aerial_unit(unit_defs.get(u.unit_id))]
    else:
        eligible = units

    # Apply one hit at a time, re-sorting eligible after each to account for HP changes
    while remaining_hits > 0 and eligible:
        eligible.sort(key=sort_key)
        target = eligible[0]

        # must_conquer (attacker only): protect the last unit that can conquer (infantry/cavalry…);
        # redirect hit to aerial first, else to siegework (same idea as aerial sacrifice).
        if must_conquer and is_attacker:
            conquering = [
                u for u in eligible
                if can_conquer_territory_as_attacker(unit_defs.get(u.unit_id))
            ]
            aerials = [u for u in eligible if is_aerial_unit(unit_defs.get(u.unit_id))]
            sw = [u for u in eligible if is_siegework_archetype(unit_defs.get(u.unit_id))]
            if (
                target in conquering
                and len(conquering) == 1
                and target.remaining_health <= 1
            ):
                if aerials:
                    target = min(aerials, key=sort_key)
                elif sw:
                    target = min(sw, key=sort_key)

        # Apply one hit
        target.remaining_health -= 1
        remaining_hits -= 1

        # Check if unit is destroyed
        if target.remaining_health == 0:
            destroyed_ids.append(target.instance_id)
            wounded_ids.discard(target.instance_id)
            units.remove(target)
            if is_naval_combat and target in eligible:
                eligible.remove(target)
        else:
            wounded_ids.add(target.instance_id)

    return destroyed_ids, list(wounded_ids)


def calculate_required_dice(units: list[Unit], unit_defs: dict[str, UnitDefinition]) -> int:
    """Calculate how many dice rolls are needed for a list of units."""
    total = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        dice_count = getattr(unit_def, 'dice', 1) if unit_def else 1
        total += dice_count
    return total


def sort_attackers_for_ladder_dice_order(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    ladder_instance_ids: set[str],
    stat_modifiers_attacker: dict[str, int] | None,
    attacker_effective_attack_override: dict[str, int] | None,
) -> None:
    """
    In-place sort so dice order matches UI shelves: per (effective_attack, unit_id),
    units not assigned to ladders roll first, then units on ladders.
    Call before attacker dice generation and before resolve when ladder_infantry_instance_ids is set.
    """
    if not ladder_instance_ids:
        return
    mods = stat_modifiers_attacker or {}
    att_ov = attacker_effective_attack_override or {}

    def eff_attack(u: Unit) -> int:
        ud = unit_defs.get(u.unit_id)
        if not ud:
            return 0
        if u.instance_id in att_ov:
            return att_ov[u.instance_id]
        return getattr(ud, "attack", 0) + mods.get(u.instance_id, 0)

    def key(u: Unit) -> tuple[int, str, int, str]:
        on = 1 if u.instance_id in ladder_instance_ids else 0
        return (eff_attack(u), u.unit_id, on, u.instance_id or "")

    attacker_units.sort(key=key)


def group_attacker_dice_with_ladder_segments(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    ladder_instance_ids: set[str],
    stat_modifiers: dict[str, int] | None = None,
    effective_dice_override: dict[str, int] | None = None,
    effective_stat_override: dict[str, int] | None = None,
    exclude_archetypes_from_rolling: set[str] | None = None,
) -> dict[int, dict]:
    """
    Like group_dice_by_stat for attackers, plus segments when ladder climbers share a shelf:
    each segment is { rolls, hits, on_ladder, unit_type, unit_count }.
    Units list must already be sorted via sort_attackers_for_ladder_dice_order.
    """
    mods = stat_modifiers or {}
    stat_override = effective_stat_override or {}
    dice_ov = effective_dice_override or {}
    skip_arch = exclude_archetypes_from_rolling or set()
    ladder_ids = ladder_instance_ids or set()
    result: dict[int, dict] = {}
    roll_idx = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue
        if unit.instance_id in stat_override:
            stat_value = stat_override[unit.instance_id]
        else:
            stat_value = getattr(unit_def, "attack", 0) + mods.get(
                unit.instance_id, 0)
        if getattr(unit_def, "archetype", "") in skip_arch:
            continue
        dice_count = (
            dice_ov.get(unit.instance_id, getattr(unit_def, "dice", 1))
        )
        chunk: list[int] = []
        for _ in range(dice_count):
            if roll_idx < len(rolls):
                chunk.append(rolls[roll_idx])
            roll_idx += 1
        on_ladder = unit.instance_id in ladder_ids
        hits_chunk = sum(1 for r in chunk if r <= stat_value)
        if stat_value not in result:
            result[stat_value] = {
                "rolls": [], "hits": 0, "segments": []}
        bucket = result[stat_value]
        bucket["rolls"].extend(chunk)
        bucket["hits"] += hits_chunk
        segs = bucket["segments"]
        if (
            segs
            and segs[-1]["on_ladder"] == on_ladder
            and segs[-1]["unit_type"] == unit.unit_id
        ):
            segs[-1]["rolls"].extend(chunk)
            segs[-1]["hits"] += hits_chunk
            segs[-1]["unit_count"] += 1
        else:
            segs.append({
                "rolls": list(chunk),
                "hits": hits_chunk,
                "on_ladder": on_ladder,
                "unit_type": unit.unit_id,
                "unit_count": 1,
            })
    return result


def group_dice_by_stat(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    is_attacker: bool,
    stat_modifiers: dict[str, int] | None = None,
    effective_dice_override: dict[str, int] | None = None,
    effective_stat_override: dict[str, int] | None = None,
    exclude_archetypes_from_rolling: set[str] | None = None,
) -> dict[int, dict]:
    """
    Group dice rolls by stat value for UI display. Rolls are tied to units:
    we iterate units in order (same as flat index order) and append each
    unit's dice to that unit's stat bucket. When effective_dice_override is
    provided (e.g. bombikazi), use it instead of unit_def.dice for dice count.
    When effective_stat_override is provided (e.g. bombikazi uses bomb attack), use it for that unit.
    When exclude_archetypes_from_rolling is set (e.g. {"siegework"}), those units get no dice in this
    grouping (they only roll in the siegeworks round).

    Returns dict of {stat_value: {"rolls": [rolls], "hits": count}}
    """
    stat_name = "attack" if is_attacker else "defense"
    mods = stat_modifiers or {}
    stat_override = effective_stat_override or {}
    skip_arch = exclude_archetypes_from_rolling or set()

    result: dict[int, dict] = {}
    roll_idx = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue
        if getattr(unit_def, "archetype", "") in skip_arch:
            continue
        if unit.instance_id in stat_override:
            stat_value = stat_override[unit.instance_id]
        else:
            stat_value = getattr(unit_def, stat_name, 0) + \
                mods.get(unit.instance_id, 0)
        dice_count = (
            effective_dice_override.get(
                unit.instance_id, getattr(unit_def, "dice", 1))
            if effective_dice_override is not None
            else getattr(unit_def, "dice", 1)
        )
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


def group_siegework_attacker_dice_ram_and_flex(
    units: list[Unit],
    rolls: list[int],
    unit_defs: dict[str, UnitDefinition],
    stat_modifiers: dict[str, int] | None = None,
    effective_dice_override: dict[str, int] | None = None,
    effective_stat_override: dict[str, int] | None = None,
) -> dict[int, dict[str, dict]]:
    """
    Same iteration order as group_dice_by_stat (attacker, no archetype skip).
    Split each attack stat into ram (stronghold-only) vs flexible siegework dice for UI.
    Every stat key includes both \"ram\" and \"flex\" buckets (possibly empty rolls).
    """
    stat_name = "attack"
    mods = stat_modifiers or {}
    stat_override = effective_stat_override or {}
    result: dict[int, dict[str, dict]] = {}

    def ensure_stat(stat_val: int) -> dict[str, dict]:
        if stat_val not in result:
            result[stat_val] = {
                "ram": {"rolls": [], "hits": 0},
                "flex": {"rolls": [], "hits": 0},
            }
        return result[stat_val]

    roll_idx = 0
    for unit in units:
        unit_def = unit_defs.get(unit.unit_id)
        if not unit_def:
            continue
        if unit.instance_id in stat_override:
            stat_value = stat_override[unit.instance_id]
        else:
            stat_value = getattr(unit_def, stat_name, 0) + \
                mods.get(unit.instance_id, 0)
        dice_count = (
            effective_dice_override.get(
                unit.instance_id, getattr(unit_def, "dice", 1))
            if effective_dice_override is not None
            else getattr(unit_def, "dice", 1)
        )
        bucket_key = "ram" if has_unit_special(
            unit_def, SIEGEWORK_SPECIAL_RAM) else "flex"
        bucket = ensure_stat(stat_value)[bucket_key]
        for _ in range(dice_count):
            if roll_idx < len(rolls):
                roll = rolls[roll_idx]
                bucket["rolls"].append(roll)
                if roll <= stat_value:
                    bucket["hits"] += 1
            roll_idx += 1
    return result


def _has_special(unit_def: UnitDefinition | None, special: str) -> bool:
    """Check if unit has the given special (in tags or specials)."""
    return has_unit_special(unit_def, special)


def _is_bomb_carrier_unit(unit_def: UnitDefinition | None) -> bool:
    """True for the paired explosive unit: tag 'bomb' or unit id 'bomb' (older setups omitted the tag)."""
    if not unit_def:
        return False
    if has_unit_tag(unit_def, "bomb"):
        return True
    return getattr(unit_def, "id", None) == "bomb"


# --- Bombikazi (attacker-only): paired with bomb carrier; bomb rolls in siegeworks, both destroyed after ---

def get_bombikazi_pairing(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> tuple[set[str], set[str]]:
    """
    When attackers include both bombikazi units and bomb carriers, pair them 1:1.
    Bomb carriers are units with the "bomb" tag or unit id "bomb". Pairing drives destruction after siegeworks.
    Returns (paired_bombikazi_instance_ids, paired_bomb_instance_ids). Pairing is deterministic (sort by instance_id).
    """
    bombikazi_units = sorted(
        [u for u in attacker_units if _has_special(
            unit_defs.get(u.unit_id), "bombikazi")],
        key=lambda u: u.instance_id,
    )
    bomb_units = sorted(
        [u for u in attacker_units if _is_bomb_carrier_unit(
            unit_defs.get(u.unit_id))],
        key=lambda u: u.instance_id,
    )
    n_pairs = min(len(bombikazi_units), len(bomb_units))
    paired_bombikazi = {bombikazi_units[i].instance_id for i in range(n_pairs)}
    paired_bombs = {bomb_units[i].instance_id for i in range(n_pairs)}
    return paired_bombikazi, paired_bombs


def get_siegework_round_attacker_display_units(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_territory_is_stronghold: bool,
    defender_stronghold_hp: int | None = None,
    *,
    fuse_bomb: bool = True,
) -> list[Unit]:
    """
    Units shown on the attacker UI during the dedicated siegeworks round only: siegework
    participants (rolling units + ladder equipment that does not roll), plus paired bombikazi
    next to their bomb when fuse_bomb (fused detonation). Ram units appear only when they roll
    (stronghold with walls HP > 0), same as get_siegework_attacker_rolling_units. Unpaired bombs
    and unpaired bombikazi never appear here. When fuse_bomb is False, paired bomb/bombikazi are
    omitted from this round.
    """
    rolling = get_siegework_attacker_rolling_units(
        attacker_units, unit_defs, defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp,
        fuse_bomb=fuse_bomb,
    )
    rolling_ids = {u.instance_id for u in rolling}
    ladder_ids = {
        u.instance_id for u in attacker_units
        if _is_siegework_unit(unit_defs.get(u.unit_id))
        and has_unit_special(unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_LADDER)
    }
    ram_ok = (
        defender_territory_is_stronghold
        and defender_stronghold_hp is not None
        and defender_stronghold_hp > 0
    )
    ram_attacker_ids: set[str] = set()
    if ram_ok:
        ram_attacker_ids = {
            u.instance_id for u in attacker_units
            if not _is_siegework_unit(unit_defs.get(u.unit_id))
            and has_unit_special(unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_RAM)
        }
    paired_bombikazi, _ = get_bombikazi_pairing(attacker_units, unit_defs)
    bombikazi_ids = set(paired_bombikazi) if fuse_bomb else set()
    keep = rolling_ids | ladder_ids | bombikazi_ids | ram_attacker_ids
    return sorted(
        [u for u in attacker_units if u.instance_id in keep],
        key=lambda u: u.instance_id,
    )


def get_siegework_round_defender_display_units(
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> list[Unit]:
    """Defenders shown during the dedicated siegeworks round: only siegework archetype units."""
    return sorted(
        [u for u in defender_units if _is_siegework_unit(unit_defs.get(u.unit_id))],
        key=lambda u: u.instance_id,
    )


def get_attacker_effective_dice_and_bombikazi_self_destruct(
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    *,
    use_paired_fused_siegework_rules: bool = True,
) -> tuple[dict[str, int], list[str], dict[str, int]]:
    """
    Bombikazi (attacker-only): paired bombikazi + bomb are paired 1:1.
    - Unpaired bomb: 0 dice always. Unpaired bombikazi: normal dice and attack (nothing special).
    - use_paired_fused_siegework_rules True (default): paired bombikazi rolls 1 die at the paired
      bomb's attack; paired bomb 0 dice; both self-destruct after this standard combat round.
      Use when no siegeworks round happened this battle, or after a fused siegework detonation
      (units are gone before standard combat).
    - use_paired_fused_siegework_rules False: after siegeworks with fuse off, paired bombikazi uses
      its own dice and attack like an unpaired bombikazi; paired bomb still 0 dice; no paired
      self-destruct from this rule.
    """
    paired_bombikazi, paired_bombs = get_bombikazi_pairing(
        attacker_units, unit_defs)
    effective_dice: dict[str, int] = {}
    effective_attack_override: dict[str, int] = {}
    # Build bomb instance_id -> attack for each paired bomb
    bomb_attack_by_instance: dict[str, int] = {}
    for unit in attacker_units:
        if unit.instance_id in paired_bombs:
            ud = unit_defs.get(unit.unit_id)
            bomb_attack_by_instance[unit.instance_id] = getattr(
                ud, "attack", 0) if ud else 0
    paired_bombikazi_list = sorted(paired_bombikazi)
    paired_bombs_list = sorted(paired_bombs)
    pair_attack: dict[str, int] = {}
    for i in range(min(len(paired_bombikazi_list), len(paired_bombs_list))):
        bid = paired_bombs_list[i]
        pair_attack[paired_bombikazi_list[i]
                    ] = bomb_attack_by_instance.get(bid, 0)
    for unit in attacker_units:
        ud = unit_defs.get(unit.unit_id)
        base_dice = getattr(ud, "dice", 1) if ud else 1
        if unit.instance_id in paired_bombikazi:
            if use_paired_fused_siegework_rules:
                effective_dice[unit.instance_id] = 1  # Fused: one die at bomb's attack
                effective_attack_override[unit.instance_id] = pair_attack.get(
                    unit.instance_id, 0)
            else:
                effective_dice[unit.instance_id] = base_dice
        elif unit.instance_id in paired_bombs:
            effective_dice[unit.instance_id] = 0  # Bomb doesn't roll in standard combat
        elif _is_bomb_carrier_unit(ud) and unit.instance_id not in paired_bombs:
            # Unpaired bomb carrier doesn't roll
            effective_dice[unit.instance_id] = 0
        else:
            effective_dice[unit.instance_id] = base_dice
    self_destruct_ids = (
        list(paired_bombikazi | paired_bombs) if use_paired_fused_siegework_rules else []
    )
    return effective_dice, self_destruct_ids, effective_attack_override


def get_eff_def_per_flat_index(
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    stat_modifiers_defender: dict[str, int] | None,
    exclude_archetypes: set[str] | None = None,
) -> list[int]:
    """
    Return effective defense for each flat index in defender rolls.
    result[i] = eff_def for the die at index i. Same order as generate_dice_rolls_for_units
    (including exclude_archetypes when provided).
    """
    mods = stat_modifiers_defender or {}
    skip = exclude_archetypes or set()
    result: list[int] = []
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            continue
        if getattr(ud, "archetype", "") in skip:
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
    exclude_archetypes: set[str] | None = None,
) -> set[int]:
    """
    Return the set of flat indices into defender_rolls where the roll is a hit
    (roll <= effective defense for that die). Same iteration order as
    generate_dice_rolls_for_units(defender_units).
    """
    mods = stat_modifiers_defender or {}
    skip = exclude_archetypes or set()
    hit_indices: set[int] = set()
    roll_idx = 0
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            continue
        if getattr(ud, "archetype", "") in skip:
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
    exclude_archetypes_from_rolling: set[str] | None = None,
) -> tuple[list[int], int]:
    """
    Round 1 only, before casualties: which defender dice (by flat index) must re-roll due to terror.

    Terror applies when an attacker has "terror" special. Each defender unit with "hope" cancels
    one terror unit (before capping). Effective terror = max(0, terror_units - hope_units);
    then cap at terror_cap (default 3) DICE total. ONLY dice that rolled a HIT may be re-rolled.
    Units with "fearless" are immune to being selected for reroll.

    Returns (flat_indices, total_dice) where flat_indices are indices into defender_rolls to
    re-roll—every index is guaranteed to be a hit (roll <= eff_def for that slot).
    """
    defender_rolls = dice_rolls.get("defender", [])
    def_mods = stat_modifiers_defender or {}
    skip_arch = exclude_archetypes_from_rolling or set()

    terror_count = sum(
        1 for u in attacker_units
        if _has_special(unit_defs.get(u.unit_id), "terror")
    )
    hope_count = sum(
        1 for u in defender_units
        if _has_special(unit_defs.get(u.unit_id), "hope")
    )
    effective_terror = max(0, terror_count - hope_count)
    effective_cap = min(terror_cap, effective_terror)
    if effective_cap <= 0:
        return [], 0

    # Per unit: (effective_defense, list of flat indices that were hits)
    roll_idx = 0
    candidates: list[tuple[int, list[int]]] = []
    for unit in defender_units:
        ud = unit_defs.get(unit.unit_id)
        if not ud:
            continue
        if getattr(ud, "archetype", "") in skip_arch:
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
                # only ever add hits; never re-roll misses
                hit_indices.append(roll_idx)
            roll_idx += 1
        if hit_indices:
            candidates.append((eff_def, hit_indices))

    # Sort by effective defense ascending (lowest first), then first hit index; coerce so keys never mix bool/list.
    def _terror_candidate_sort_key(x: tuple) -> tuple[int, int]:
        try:
            eff = int(x[0])
        except (TypeError, ValueError):
            eff = 999
        hi = x[1]
        if not hi:
            first_hit = 0
        else:
            try:
                first_hit = int(hi[0])
            except (TypeError, ValueError):
                first_hit = 999
        return (eff, first_hit)

    candidates.sort(key=_terror_candidate_sort_key)
    flat_indices: list[int] = []
    for _eff_def, indices in candidates:
        for idx in indices:
            if len(flat_indices) >= effective_cap:
                break
            flat_indices.append(idx)
        if len(flat_indices) >= effective_cap:
            break
    return flat_indices, len(flat_indices)


def resolve_archer_prefire(
    attacker_units: list[Unit],
    defender_archer_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    defender_rolls: list[int],
    stat_modifiers_defender_extra: dict[str, int] | None = None,
    territory_def: "TerritoryDefinition | None" = None,
    *,
    prefire_penalty_delta: int = -1,
) -> RoundResult:
    """
    Resolve defender archer prefire: only units with the archer special roll at defense-1; hits to attackers only.
    Modifies attacker_units in place (removes dead, decrements health).
    defender_archer_units are not modified (no defender casualties from prefire).
    stat_modifiers_defender_extra: optional instance_id -> extra modifier (e.g. terrain bonus), merged with prefire penalty.
    prefire_penalty_delta: -1 when setup manifest enables prefire penalty; 0 when disabled.
    """
    extra = stat_modifiers_defender_extra or {}
    archer_penalty = prefire_penalty_delta
    stat_modifiers = {
        u.instance_id: archer_penalty + extra.get(u.instance_id, 0) for u in defender_archer_units
    }
    defender_hits = _count_hits(
        defender_archer_units, defender_rolls, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers,
    )
    attacker_hits = 0  # Attackers do not roll in prefire

    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True,
        casualty_order="best_unit",
        must_conquer=False,
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


def resolve_siegeworks_round(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    dice_rolls: dict[str, list[int]],
    stat_modifiers_attacker: dict[str, int] | None = None,
    stat_modifiers_defender: dict[str, int] | None = None,
    casualty_order_attacker: str = "best_unit",
    casualty_order_defender: str = "best_unit",
    defender_stronghold_hp: int | None = None,
    defender_territory_is_stronghold: bool = False,
    *,
    fuse_bomb: bool = True,
) -> tuple[RoundResult, int | None, int]:
    """
    Resolve the dedicated siegeworks round.
    - ram: normal attack/dice; hits only stronghold HP. Inactive without stronghold HP (dice consumed, 0 hits).
    - ladder: does not roll; transport_capacity per ladder = infantry (climbs_ladder) bypassing stronghold in round 1+.
    - Other siegework: normal hits to stronghold then overflow to defender units.
    Non-siegework attackers with ram also roll here vs stronghold only.
    """
    attacker_siegework = [
        u for u in attacker_units if _is_siegework_unit(unit_defs.get(u.unit_id))]
    defender_siegework = [
        u for u in defender_units if _is_siegework_unit(unit_defs.get(u.unit_id))]
    attacker_rolls = list(dice_rolls.get("attacker", []))
    defender_rolls = dice_rolls.get("defender", [])

    attacker_ladders = [u for u in attacker_siegework if has_unit_special(
        unit_defs.get(u.unit_id), SIEGEWORK_SPECIAL_LADDER)]
    ladder_count = len(attacker_ladders)

    rolling_attacker = get_siegework_attacker_rolling_units(
        attacker_units, unit_defs, defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp,
        fuse_bomb=fuse_bomb,
    )
    ram_active = (
        defender_territory_is_stronghold
        and defender_stronghold_hp is not None
        and defender_stronghold_hp > 0
    )
    ram_hits_total = 0
    other_attacker_units: list[Unit] = []
    other_attacker_rolls: list[int] = []
    roll_idx = 0
    for u in rolling_attacker:
        ud = unit_defs.get(u.unit_id)
        dice_count = getattr(ud, "dice", 1)
        rolls_for_unit: list[int] = []
        for _ in range(dice_count):
            if roll_idx < len(attacker_rolls):
                rolls_for_unit.append(attacker_rolls[roll_idx])
                roll_idx += 1
        if has_unit_special(ud, SIEGEWORK_SPECIAL_RAM):
            if ram_active:
                ram_hits_total += _count_hits(
                    [u], rolls_for_unit, unit_defs, is_attacker=True,
                    stat_modifiers=stat_modifiers_attacker,
                )
        else:
            other_attacker_units.append(u)
            other_attacker_rolls.extend(rolls_for_unit)

    normal_attacker_hits = _count_hits(
        other_attacker_units, other_attacker_rolls, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers_attacker,
    ) if other_attacker_units else 0

    # Ram hits apply to stronghold only, capped (no overflow to units)
    stronghold_hp_cur = defender_stronghold_hp if defender_stronghold_hp is not None else 0
    ram_hits_to_stronghold = min(
        stronghold_hp_cur, ram_hits_total) if stronghold_hp_cur > 0 else 0
    stronghold_hp_after_ram = max(
        0, stronghold_hp_cur - ram_hits_to_stronghold)

    # Normal siegework hits: then stronghold (up to remaining HP) then overflow to defender units
    normal_to_stronghold = min(
        stronghold_hp_after_ram, normal_attacker_hits) if stronghold_hp_after_ram > 0 else 0
    hits_to_defender_units = (
        normal_attacker_hits - normal_to_stronghold) if stronghold_hp_after_ram > 0 else normal_attacker_hits
    defender_stronghold_hp_after = (
        max(0, stronghold_hp_after_ram -
            normal_to_stronghold) if defender_stronghold_hp is not None else None
    )
    total_attacker_hits = ram_hits_to_stronghold + \
        normal_to_stronghold + hits_to_defender_units  # for RoundResult

    defender_hits = _count_hits(
        defender_siegework, defender_rolls, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers_defender,
    ) if defender_siegework else 0

    attacker_casualties, attacker_wounded = _apply_hits(
        attacker_units, defender_hits, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers_attacker,
        casualty_order=casualty_order_attacker,
        must_conquer=False,
    )
    defender_casualties, defender_wounded = _apply_hits(
        defender_units, hits_to_defender_units, unit_defs, is_attacker=False,
        stat_modifiers=stat_modifiers_defender,
        casualty_order=casualty_order_defender,
        must_conquer=False,
        territory_is_stronghold=defender_territory_is_stronghold,
        hits_from_ladder=False,
    )

    result = RoundResult(
        attacker_hits=total_attacker_hits,
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
    return result, defender_stronghold_hp_after, ladder_count


def resolve_stealth_prefire(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
    attacker_rolls: list[int],
    stat_modifiers_attacker_extra: dict[str, int] | None = None,
    *,
    prefire_penalty_delta: int = -1,
) -> RoundResult:
    """
    Resolve attacker stealth prefire: only attackers roll at attack-1, hits applied to defenders only.
    Call when EVERY attacker has the stealth special. Modifies defender_units in place.
    prefire_penalty_delta: -1 when setup manifest enables prefire penalty; 0 when disabled.
    """
    extra = stat_modifiers_attacker_extra or {}
    stat_modifiers = {
        u.instance_id: prefire_penalty_delta + extra.get(u.instance_id, 0) for u in attacker_units
    }
    attacker_hits = _count_hits(
        attacker_units, attacker_rolls, unit_defs, is_attacker=True,
        stat_modifiers=stat_modifiers,
    )
    defender_hits = 0  # Defenders do not roll in stealth prefire

    defender_casualties, defender_wounded = _apply_hits(
        defender_units, attacker_hits, unit_defs, is_attacker=False,
        casualty_order="best_unit",
        must_conquer=False,
    )
    attacker_casualties: list[str] = []
    attacker_wounded: list[str] = []

    return RoundResult(
        attacker_hits=attacker_hits,
        defender_hits=defender_hits,
        attacker_casualties=attacker_casualties,
        defender_casualties=defender_casualties,
        attacker_wounded=attacker_wounded,
        defender_wounded=defender_wounded,
        surviving_attacker_ids=[u.instance_id for u in attacker_units],
        surviving_defender_ids=[u.instance_id for u in defender_units],
        attackers_eliminated=False,
        defenders_eliminated=len(defender_units) == 0,
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
