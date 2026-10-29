"""
Dice sampling for MCTS combat simulation.

The engine reducer is RNG-free: initiate_combat / continue_combat require dice
in the action payload. The API fills dice for real play; MCTS must sample its
own dice when branching search states through apply_action.

This is a pragmatic sampler for search: it covers siegeworks, archer prefire,
stealth prefire, and normal rounds. Terror re-rolls are not simulated (minor
edge case; real play still applies terror via the API path).
"""

from __future__ import annotations

import random

from backend.engine.combat import (
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_siegework_attacker_rolling_units,
    get_siegework_defender_rolling_units,
    get_siegework_dice_counts,
)
from backend.engine.movement import _is_water_zone
from backend.engine.queries import participates_in_sea_hex_naval_combat
from backend.engine.utils import generate_dice_rolls_for_units, has_unit_special

# Archetypes that only roll in the siegeworks round (match API NORMAL_COMBAT_EXCLUDE).
NORMAL_COMBAT_EXCLUDE_ARCHETYPES = frozenset({"siegework"})


def _combat_territory_stronghold_hp(territory, tdef) -> int | None:
    """Stronghold HP for siegework dice counts; None when not a stronghold."""
    if not tdef or not getattr(tdef, "is_stronghold", False):
        return None
    hp = getattr(territory, "stronghold_current_health", None)
    if hp is not None:
        return int(hp)
    max_hp = getattr(tdef, "stronghold_max_health", None)
    return int(max_hp) if max_hp is not None else None


def _fuse_flag(combat) -> bool:
    fuse = getattr(combat, "fuse_bomb", True)
    return fuse if isinstance(fuse, bool) else True


def _get_initiate_rosters(
    state,
    territory_id: str,
    sea_zone_id: str | None,
    attacker_faction: str,
    ud,
    td,
    fd,
) -> tuple[list, list]:
    """Attackers and defenders for an initiate_combat on territory_id (optional sea raid)."""
    from backend.engine.definitions import faction_acts_as
    from backend.engine.utils import get_unit_faction

    territory = state.territories.get(territory_id)
    if not territory:
        return [], []
    attacker_alliance = getattr(fd.get(attacker_faction), "alliance", None) if fd.get(attacker_faction) else None

    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        if not sea_zone:
            return [], []
        # Sea raid passengers: land units loaded in the staging sea zone.
        attackers = sorted(
            [
                u for u in sea_zone.units
                if ud.get(u.unit_id)
                and faction_acts_as(fd, getattr(ud[u.unit_id], "faction", None), attacker_faction)
                and not _looks_naval(ud.get(u.unit_id))
            ],
            key=lambda u: u.instance_id,
        )
        if not attackers:
            attackers = sorted(
                [
                    u for u in territory.units
                    if ud.get(u.unit_id)
                    and faction_acts_as(fd, getattr(ud[u.unit_id], "faction", None), attacker_faction)
                    and not _looks_naval(ud.get(u.unit_id))
                ],
                key=lambda u: u.instance_id,
            )
        defenders = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id)
                and not faction_acts_as(fd, getattr(ud[u.unit_id], "faction", None), attacker_faction)
                and (
                    getattr(fd.get(getattr(ud[u.unit_id], "faction", None)), "alliance", None)
                    if fd.get(getattr(ud[u.unit_id], "faction", None))
                    else None
                ) != attacker_alliance
            ],
            key=lambda u: u.instance_id,
        )
        return attackers, defenders

    is_water = _is_water_zone(td.get(territory_id))
    attackers = []
    defenders = []
    for u in territory.units:
        udef = ud.get(u.unit_id)
        if not udef:
            continue
        if is_water and not participates_in_sea_hex_naval_combat(u, udef):
            continue
        u_faction = get_unit_faction(u, ud)
        if faction_acts_as(fd, u_faction, attacker_faction):
            attackers.append(u)
        elif u_faction is not None:
            u_alliance = getattr(fd.get(u_faction), "alliance", None) if fd.get(u_faction) else None
            if u_alliance != attacker_alliance:
                defenders.append(u)
    attackers.sort(key=lambda u: u.instance_id)
    defenders.sort(key=lambda u: u.instance_id)
    return attackers, defenders


def _looks_naval(unit_def) -> bool:
    if not unit_def:
        return False
    arch = getattr(unit_def, "archetype", "") or ""
    tags = getattr(unit_def, "tags", []) or []
    return arch == "naval" or "naval" in tags or arch == "river" or "river" in tags


def _get_active_rosters(state, ud, td, fd) -> tuple[list, list]:
    """Attackers and defenders for the current active_combat."""
    combat = state.active_combat
    if not combat:
        return [], []
    territory = state.territories.get(combat.territory_id)
    if not territory:
        return [], []
    attacker_ids = set(combat.attacker_instance_ids)
    sea_zone_id = getattr(combat, "sea_zone_id", None)
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        in_sea = [
            u for u in (sea_zone.units if sea_zone else [])
            if getattr(u, "instance_id", "") in attacker_ids
        ]
        attacker_territory = sea_zone if in_sea else territory
    else:
        attacker_territory = territory
    attackers = sorted(
        [u for u in (getattr(attacker_territory, "units", []) or []) if getattr(u, "instance_id", "") in attacker_ids],
        key=lambda u: u.instance_id,
    )
    defenders = sorted(
        [u for u in territory.units if getattr(u, "instance_id", "") not in attacker_ids],
        key=lambda u: u.instance_id,
    )
    tdef = td.get(combat.territory_id)
    if tdef and _is_water_zone(tdef):
        attackers = [u for u in attackers if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))]
        defenders = [u for u in defenders if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))]
    return attackers, defenders


def sample_dice_for_initiate(
    state,
    territory_id: str,
    sea_zone_id: str | None,
    attacker_faction: str,
    ud,
    td,
    fd,
    rng: random.Random | None = None,
) -> dict[str, list[int]]:
    """Sample dice_rolls for an initiate_combat action (round 1 of a new battle)."""
    rng = rng or random.Random()
    attackers, defenders = _get_initiate_rosters(state, territory_id, sea_zone_id, attacker_faction, ud, td, fd)
    if not attackers:
        return {"attacker": [], "defender": []}

    territory = state.territories.get(territory_id)
    tdef = td.get(territory_id)
    def_sh = bool(tdef and getattr(tdef, "is_stronghold", False))
    def_sh_hp = _combat_territory_stronghold_hp(territory, tdef)
    fuse = True  # default for new battles

    all_stealth = all(
        has_unit_special(ud.get(u.unit_id), "stealth") for u in attackers if ud.get(u.unit_id)
    ) and len(attackers) > 0
    defender_has_archers = any(
        has_unit_special(ud.get(u.unit_id), "archer") for u in defenders if ud.get(u.unit_id)
    )

    sw_a, sw_d = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh, defender_stronghold_hp=def_sh_hp, fuse_bomb=fuse
    )
    siegeworks_pending = sw_a > 0 or sw_d > 0

    if siegeworks_pending:
        att_rolling = get_siegework_attacker_rolling_units(
            attackers, ud, def_sh, defender_stronghold_hp=def_sh_hp, fuse_bomb=fuse
        )
        def_sw = get_siegework_defender_rolling_units(defenders, ud)
        return {
            "attacker": generate_dice_rolls_for_units(att_rolling, ud),
            "defender": generate_dice_rolls_for_units(def_sw, ud),
        }

    if all_stealth:
        att_eff, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        return {
            "attacker": generate_dice_rolls_for_units(
                attackers, ud, effective_dice_override=att_eff
            ),
            "defender": [],
        }

    if defender_has_archers:
        defender_archer_units = sorted(
            [u for u in defenders if ud.get(u.unit_id) and has_unit_special(ud[u.unit_id], "archer")],
            key=lambda u: u.instance_id,
        )
        return {
            "attacker": [],
            "defender": generate_dice_rolls_for_units(defender_archer_units, ud),
        }

    att_eff, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
    return {
        "attacker": generate_dice_rolls_for_units(
            attackers, ud, effective_dice_override=att_eff,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
        "defender": generate_dice_rolls_for_units(
            defenders, ud, exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
    }


def sample_dice_for_continue(
    state,
    ud,
    td,
    fd,
    rng: random.Random | None = None,
) -> dict[str, list[int]]:
    """Sample dice_rolls for a continue_combat action on the current active combat."""
    rng = rng or random.Random()
    combat = state.active_combat
    if not combat:
        return {"attacker": [], "defender": []}
    attackers, defenders = _get_active_rosters(state, ud, td, fd)
    if not attackers or not defenders:
        return {"attacker": [], "defender": []}

    combat_log = getattr(combat, "combat_log", []) or []
    tdef = td.get(combat.territory_id)
    def_sh = bool(tdef and getattr(tdef, "is_stronghold", False))
    land_t = state.territories.get(combat.territory_id)
    def_sh_hp = _combat_territory_stronghold_hp(land_t, tdef)
    fuse = _fuse_flag(combat)
    round_number = getattr(combat, "round_number", 0) or 0

    sw_a, sw_d = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh, defender_stronghold_hp=def_sh_hp, fuse_bomb=fuse
    )
    siegeworks_pending = (
        round_number == 0
        and not any(getattr(r, "is_siegeworks_round", False) for r in combat_log)
        and (sw_a > 0 or sw_d > 0)
    )
    archer_prefire_pending = (
        round_number == 0
        and not siegeworks_pending
        and not any(getattr(r, "is_archer_prefire", False) for r in combat_log)
        and not any(getattr(r, "is_stealth_prefire", False) for r in combat_log)
        and any(has_unit_special(ud.get(u.unit_id), "archer") for u in defenders if ud.get(u.unit_id))
    )

    if siegeworks_pending:
        att_rolling = get_siegework_attacker_rolling_units(
            attackers, ud, def_sh, defender_stronghold_hp=def_sh_hp, fuse_bomb=fuse
        )
        def_sw = get_siegework_defender_rolling_units(defenders, ud)
        return {
            "attacker": generate_dice_rolls_for_units(att_rolling, ud),
            "defender": generate_dice_rolls_for_units(def_sw, ud),
        }

    if archer_prefire_pending:
        defender_archer_units = sorted(
            [u for u in defenders if ud.get(u.unit_id) and has_unit_special(ud[u.unit_id], "archer")],
            key=lambda u: u.instance_id,
        )
        return {
            "attacker": [],
            "defender": generate_dice_rolls_for_units(defender_archer_units, ud),
        }

    att_eff, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(
        attackers, ud,
        use_paired_fused_siegework_rules=not any(
            getattr(r, "is_siegeworks_round", False) for r in combat_log
        ) or fuse,
    )
    return {
        "attacker": generate_dice_rolls_for_units(
            attackers, ud, effective_dice_override=att_eff,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
        "defender": generate_dice_rolls_for_units(
            defenders, ud, exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
    }
