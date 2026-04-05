"""
Single source of truth for combat specials and stat modifiers.
Given attacker units, defender units, territory, and options, returns per-instance
specials and stat modifiers. Used by real combat (reducer/API) and combat sim.
"""

from dataclasses import dataclass, field
from typing import Any

from backend.engine.state import Unit
from backend.engine.definitions import UnitDefinition
from backend.engine.utils import has_unit_special
from backend.engine.combat import (
    compute_terrain_stat_modifiers,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    compute_sea_raider_stat_modifiers,
    merge_stat_modifiers,
    get_bombikazi_pairing,
    get_siegework_dice_counts,
)


@dataclass
class BattleSpecialsResult:
    """Per-instance specials and stat modifiers for a battle."""
    stat_modifiers_attacker: dict[str, int] = field(default_factory=dict)  # instance_id -> modifier
    stat_modifiers_defender: dict[str, int] = field(default_factory=dict)
    specials_attacker: dict[str, dict[str, bool]] = field(default_factory=dict)  # instance_id -> {terror: bool, ...}
    specials_defender: dict[str, dict[str, bool]] = field(default_factory=dict)


def compute_battle_specials_and_modifiers(
    attacker_units: list[Unit],
    defender_units: list[Unit],
    territory_def: Any,  # TerritoryDefinition | None
    unit_defs: dict[str, UnitDefinition],
    *,
    is_sea_raid: bool = False,
    archer_prefire_applicable: bool = False,
    stealth_prefire_applicable: bool = False,
    ram_applicable: bool = False,
) -> BattleSpecialsResult:
    """
    Single source of truth: compute which specials apply to each unit and stat modifiers.

    Uses same rules as real combat: terrain, anti-cavalry, captain, sea raider,
    terror/fearless/hope, stealth, bombikazi (paired), archer (defenders, when applicable).

    stealth: only when stealth_prefire_applicable (dedicated stealth prefire snapshot — not standard combat).

    ram: only when ram_applicable (dedicated siegeworks round vs stronghold — ram has no effect in standard rounds).

    is_sea_raid: land combat only; enables Sea Raider +attack for units with that special
    (passengers ashore). Not naval combat and does not affect who can be hit.

    Returns per-instance specials (so stacks can have some units with a special, some without)
    and merged stat modifiers for attack/defense.
    """
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
        attacker_units, unit_defs, is_sea_raid=is_sea_raid
    )
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att, sea_raider_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    terrain_type = (getattr(territory_def, "terrain_type", None) or "").lower()
    is_mountain = terrain_type in ("mountain", "mountains")
    is_forest = terrain_type == "forest"

    attackers_have_terror = any(
        has_unit_special(unit_defs.get(u.unit_id), "terror") for u in attacker_units if unit_defs.get(u.unit_id)
    )
    stealth_activated = (
        len(attacker_units) > 0
        and all(has_unit_special(unit_defs.get(u.unit_id), "stealth") for u in attacker_units if unit_defs.get(u.unit_id))
    )
    paired_bombikazi_ids = set(get_bombikazi_pairing(attacker_units, unit_defs)[0]) if attacker_units else set()

    def build_specials(
        units: list[Unit],
        captain_mods: dict[str, int],
        anticav_mods: dict[str, int],
        terrain_mods: dict[str, int],
        is_attacker: bool,
        sea_raider_mods: dict[str, int] | None = None,
    ) -> dict[str, dict[str, bool]]:
        sea_raider_mods = sea_raider_mods or {}
        out: dict[str, dict[str, bool]] = {}
        for u in units:
            unit_def = unit_defs.get(u.unit_id)
            if not unit_def:
                continue
            out[u.instance_id] = {
                "terror": is_attacker and has_unit_special(unit_def, "terror"),
                "terrainMountain": bool(terrain_mods.get(u.instance_id) and is_mountain),
                "terrainForest": bool(terrain_mods.get(u.instance_id) and is_forest),
                "captain": bool(captain_mods.get(u.instance_id, 0) > 0),
                "antiCavalry": bool(anticav_mods.get(u.instance_id, 0) > 0),
                "seaRaider": bool(sea_raider_mods.get(u.instance_id, 0) > 0),
                "archer": (not is_attacker) and has_unit_special(unit_def, "archer") and archer_prefire_applicable,
                "stealth": (
                    is_attacker
                    and has_unit_special(unit_def, "stealth")
                    and stealth_activated
                    and stealth_prefire_applicable
                ),
                "bombikazi": is_attacker and u.instance_id in paired_bombikazi_ids,
                "fearless": (not is_attacker) and has_unit_special(unit_def, "fearless") and attackers_have_terror,
                "hope": (not is_attacker) and has_unit_special(unit_def, "hope") and attackers_have_terror,
                "ram": is_attacker and has_unit_special(unit_def, "ram") and ram_applicable,
            }
        return out

    specials_attacker = build_specials(
        attacker_units, captain_att, anticav_att, terrain_att, True, sea_raider_att
    )
    specials_defender = build_specials(
        defender_units, captain_def, anticav_def, terrain_def, False
    )

    return BattleSpecialsResult(
        stat_modifiers_attacker=dict(attacker_mods),
        stat_modifiers_defender=dict(defender_mods),
        specials_attacker=specials_attacker,
        specials_defender=specials_defender,
    )


# Engine internal keys -> JSON keys on combat_round_resolved unit snapshots (matches frontend / events contract).
_ENGINE_SPECIAL_TO_PAYLOAD: tuple[tuple[str, str], ...] = (
    ("terror", "terror"),
    ("terrainMountain", "terrain_mountain"),
    ("terrainForest", "terrain_forest"),
    ("captain", "captain_bonus"),
    ("antiCavalry", "anti_cavalry"),
    ("seaRaider", "sea_raider"),
    ("archer", "archer"),
    ("stealth", "stealth"),
    ("bombikazi", "bombikazi"),
    ("fearless", "fearless"),
    ("hope", "hope"),
    ("ram", "ram"),
)


def specials_flags_for_round_payload(
    instance_id: str,
    is_attacker: bool,
    spec_result: BattleSpecialsResult,
) -> dict[str, bool]:
    """
    Map compute_battle_specials_and_modifiers output to booleans for combat_round_resolved
    attacker_units_at_start / defender_units_at_start. Single source of truth for combat UI badges.

    Archer special (archer=True) only when archer_prefire_applicable was True at compute time
    (defender archer prefire round), not during standard combat rounds.

    Ram (ram=True) only when ram_applicable was True (dedicated siegeworks round snapshot).

    Stealth (stealth=True) only when stealth_prefire_applicable was True (stealth prefire round snapshot).
    """
    raw = (
        spec_result.specials_attacker.get(instance_id)
        if is_attacker
        else spec_result.specials_defender.get(instance_id)
    ) or {}
    return {json_key: bool(raw.get(eng_key)) for eng_key, json_key in _ENGINE_SPECIAL_TO_PAYLOAD}


def empty_round_special_payload() -> dict[str, bool]:
    """All-false specials (unknown unit def or missing instance)."""
    return {json_key: False for _, json_key in _ENGINE_SPECIAL_TO_PAYLOAD}


def stacks_to_synthetic_units(
    attacker_stacks: list[dict[str, Any]],  # [{"unit_id": str, "count": int}, ...]
    defender_stacks: list[dict[str, Any]],
) -> tuple[list[Unit], list[Unit]]:
    """
    Convert stack payloads to lists of Unit with synthetic instance_ids.
    Used by combat sim so we can run the same specials engine as real combat.
    """
    attacker_units: list[Unit] = []
    idx = 0
    for s in attacker_stacks:
        unit_id = (s.get("unit_id") or "").strip()
        count = max(0, int(s.get("count") or 0))
        for _ in range(count):
            attacker_units.append(Unit(
                instance_id=f"att_{idx}_{unit_id}",
                unit_id=unit_id,
                remaining_movement=0,
                remaining_health=1,
                base_movement=0,
                base_health=1,
            ))
            idx += 1
    defender_units: list[Unit] = []
    idx = 0
    for s in defender_stacks:
        unit_id = (s.get("unit_id") or "").strip()
        count = max(0, int(s.get("count") or 0))
        for _ in range(count):
            defender_units.append(Unit(
                instance_id=f"def_{idx}_{unit_id}",
                unit_id=unit_id,
                remaining_movement=0,
                remaining_health=1,
                base_movement=0,
                base_health=1,
            ))
            idx += 1
    return attacker_units, defender_units


def ram_special_applicable_for_active_combat(
    combat_log: list,
    round_number: int,
    attacker_units: list[Unit],
    defender_units: list[Unit],
    territory_def: Any,
    defender_stronghold_hp: int | None,
    territory_is_sea: bool,
    unit_defs: dict[str, UnitDefinition],
    *,
    fuse_bomb: bool = True,
) -> bool:
    """
    True when the next combat round to resolve is the dedicated siegeworks round (UI ram badge on lobby poll).
    Mirrors reducer continue_combat siegeworks_pending (round_number==0, no siegework in log yet, siege applies).
    """
    if round_number != 0:
        return False
    if any(getattr(r, "is_siegeworks_round", False) for r in combat_log):
        return False
    if territory_is_sea:
        return False
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    sa, sd = get_siegework_dice_counts(
        attacker_units,
        defender_units,
        unit_defs,
        defender_territory_is_stronghold,
        defender_stronghold_hp=defender_stronghold_hp,
        fuse_bomb=fuse_bomb,
    )
    return sa > 0 or sd > 0


def stealth_prefire_applicable_for_active_combat(
    combat_log: list,
    round_number: int,
    attacker_units: list[Unit],
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    """
    True when the next combat round to resolve is attacker stealth prefire (UI stealth badge on poll).
    Stealth prefire only runs at combat open when every attacker has stealth; once logged, never again.
    """
    if not attacker_units:
        return False
    if any(getattr(r, "is_stealth_prefire", False) for r in combat_log):
        return False
    if round_number != 0:
        return False
    return all(has_unit_special(unit_defs.get(u.unit_id), "stealth") for u in attacker_units)

