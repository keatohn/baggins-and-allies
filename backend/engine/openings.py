"""Opening attacks for each faction on turn 1.

Each arrow is one combat move: every unit that faction commands in a territory,
and that can reach a destination, against the hostile units already there.
Empty conquests are omitted. Win chance comes from the combat simulator.

Later factions are projected from one likely line. After a faction's arrows are
recorded, attacks that conquer at least 75% of the time are applied, best chance
first, and a stack is sent on only one of them. Sea-raid ships stay in their sea
zone. Purchases and non-combat moves are left for the real game.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from backend.engine.combat_sim import SimOptions, apply_sim_ring_boosts, run_simulation
from backend.engine.definitions import (
    CampDefinition,
    FactionDefinition,
    TerritoryDefinition,
    UnitDefinition,
    faction_acts_as,
    controlling_faction_id,
)
from backend.engine.movement import (
    _is_water_craft,
    _is_water_zone,
    get_reachable_territories_for_unit,
)
from backend.engine.rings import combat_boosts
from backend.engine.special_rules import select_special_rules
from backend.engine.subfaction_rules import capture_owner_for_units
from backend.engine.utils import (
    can_conquer_territory_as_attacker,
    effective_original_owner,
    get_unit_faction,
    initialize_game_state,
    is_land_unit,
)

# A prior faction is assumed to take an attack only when conquest is this likely.
LIKELY_CONQUER = 0.75


def _alliance_of(faction_defs: dict[str, FactionDefinition], faction_id: str | None) -> str | None:
    if not faction_id:
        return None
    direct = faction_defs.get(faction_id)
    alliance = getattr(direct, "alliance", None) if direct is not None else None
    if isinstance(alliance, str) and alliance:
        return alliance
    parent_id = controlling_faction_id(faction_defs, faction_id)
    parent = faction_defs.get(parent_id) if parent_id else None
    parent_alliance = getattr(parent, "alliance", None) if parent is not None else None
    return parent_alliance if isinstance(parent_alliance, str) and parent_alliance else None


def _is_hostile(
    unit_faction: str | None,
    acting_faction: str,
    faction_defs: dict[str, FactionDefinition],
) -> bool:
    if faction_acts_as(faction_defs, unit_faction, acting_faction):
        return False
    acting_alliance = _alliance_of(faction_defs, acting_faction)
    unit_alliance = _alliance_of(faction_defs, unit_faction)
    if acting_alliance and unit_alliance and acting_alliance == unit_alliance:
        return False
    return True


def _stacks(units) -> list[dict[str, Any]]:
    counts = Counter(unit.unit_id for unit in units)
    return [{"unit_id": uid, "count": counts[uid]} for uid in sorted(counts)]


def _boost_rows(boosts: dict, units) -> list[dict[str, Any]]:
    by_id = {unit.instance_id: unit for unit in units}
    rows: dict[str, dict[str, Any]] = {}
    for instance_id, parts in boosts.items():
        unit = by_id.get(instance_id)
        if unit is None or not parts:
            continue
        attack, defense, dice, hp = parts
        row = rows.setdefault(
            unit.unit_id,
            {"unit_id": unit.unit_id, "attack": 0, "defense": 0, "dice": 0, "hp": 0},
        )
        row["attack"] += int(attack or 0)
        row["defense"] += int(defense or 0)
        row["dice"] += int(dice or 0)
        row["hp"] += int(hp or 0)
    return [row for row in rows.values() if row["attack"] or row["defense"] or row["dice"] or row["hp"]]


def compute_opening_attacks(
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None,
    starting_setup: dict,
    *,
    manifest: dict | None = None,
    n_trials: int = 2000,
    seed: int = 8,
) -> dict[str, Any]:
    """Simulate each turn-order faction's opening attacks, projecting likely conquests forward."""
    manifest = manifest or {}
    trials = max(1, min(int(n_trials), 20_000))
    rules, rings_on = select_special_rules(manifest.get("special_rules"))
    heroes_raw = manifest.get("heroes_enabled")
    heroes_enabled = True if heroes_raw is None else bool(heroes_raw)
    state = initialize_game_state(
        faction_defs=faction_defs,
        territory_defs=territory_defs,
        unit_defs=unit_defs,
        starting_setup=starting_setup,
        camp_defs=camp_defs,
        victory_criteria=manifest.get("victory_criteria") if isinstance(manifest.get("victory_criteria"), dict) else None,
        camp_cost=manifest.get("camp_cost") if isinstance(manifest.get("camp_cost"), int) else None,
        destroy_camp_on_conquer=manifest.get("destroy_camp_on_conquer") if isinstance(manifest.get("destroy_camp_on_conquer"), bool) else None,
        stronghold_repair_cost=manifest.get("stronghold_repair_cost") if isinstance(manifest.get("stronghold_repair_cost"), int) else None,
        prefire_penalty=manifest.get("prefire_penalty") if isinstance(manifest.get("prefire_penalty"), bool) else None,
        heroes_enabled=heroes_enabled,
        special_rules=rules,
        subfaction_rules=manifest.get("subfaction_rules") if isinstance(manifest.get("subfaction_rules"), dict) else None,
        rings_of_power=rings_on,
    )
    state.phase = "combat_move"
    order = [fid for fid in state.turn_order if fid in faction_defs]
    if not order and state.current_faction:
        order = [state.current_faction]

    factions_out: list[dict[str, Any]] = []
    for faction_index, faction_id in enumerate(order):
        state.current_faction = faction_id
        attacks, rows = _attacks_for_faction(
            state,
            faction_id,
            unit_defs,
            territory_defs,
            faction_defs,
            n_trials=trials,
            seed=seed + faction_index * 1009,
        )
        faction_def = faction_defs.get(faction_id)
        factions_out.append(
            {
                "faction_id": faction_id,
                "faction_name": getattr(faction_def, "display_name", None) or faction_id,
                "board": _board_snapshot(state),
                "attacks": attacks,
                "assumed": _apply_likely_conquests(
                    state, faction_id, rows, unit_defs, territory_defs, faction_defs
                ),
            }
        )

    return {
        "n_trials": trials,
        "conquer_threshold": LIKELY_CONQUER,
        "factions": factions_out,
    }


def _attacks_for_faction(
    state,
    faction_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    *,
    n_trials: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Defended opening attacks for one faction, plus the rows used to project the next board."""
    pending: list[dict[str, Any]] = []
    for origin in sorted(state.territories):
        territory = state.territories[origin]
        commanded = [
            unit
            for unit in territory.units
            if faction_acts_as(faction_defs, get_unit_faction(unit, unit_defs), faction_id)
        ]
        if not commanded:
            continue
        reach_cache: dict[tuple[str, int], dict[str, int]] = {}
        by_dest: dict[str, list] = {}
        for unit in commanded:
            try:
                movement = int(unit.remaining_movement or 0)
            except (TypeError, ValueError):
                movement = 0
            cache_key = (unit.unit_id, movement)
            reachable = reach_cache.get(cache_key)
            if reachable is None:
                reachable, _routes = get_reachable_territories_for_unit(
                    unit,
                    origin,
                    state,
                    unit_defs,
                    territory_defs,
                    faction_defs,
                    "combat_move",
                    acting_faction_id=faction_id,
                )
                reach_cache[cache_key] = reachable
            for dest in reachable:
                if dest == origin:
                    continue
                by_dest.setdefault(dest, []).append(unit)
        for dest, attackers in by_dest.items():
            dest_state = state.territories.get(dest)
            if dest_state is None:
                continue
            defenders = [
                unit
                for unit in dest_state.units
                if _is_hostile(get_unit_faction(unit, unit_defs), faction_id, faction_defs)
            ]
            if not defenders:
                continue
            dest_def = territory_defs.get(dest)
            water = _is_water_zone(dest_def)
            sea_raid = (not water) and all(
                _is_water_craft(unit_defs.get(unit.unit_id)) for unit in attackers
            )
            stronghold_hp = None
            if (
                not water
                and dest_def is not None
                and getattr(dest_def, "is_stronghold", False)
            ):
                current_hp = getattr(dest_state, "stronghold_current_health", None)
                if current_hp is not None:
                    try:
                        current_hp = int(current_hp)
                    except (TypeError, ValueError):
                        current_hp = None
                if current_hp is not None:
                    if current_hp > 0:
                        stronghold_hp = current_hp
                else:
                    base_hp = int(getattr(dest_def, "stronghold_base_health", 0) or 0)
                    if base_hp > 0:
                        stronghold_hp = base_hp
            pending.append(
                {
                    "from_territory": origin,
                    "to_territory": dest,
                    "attackers": attackers,
                    "defenders": defenders,
                    "is_sea_raid": sea_raid,
                    "stronghold_initial_hp": stronghold_hp,
                }
            )

    pending.sort(key=lambda row: (row["from_territory"], row["to_territory"]))
    attacks: list[dict[str, Any]] = []
    for index, row in enumerate(pending):
        attacker_stacks = _stacks(row["attackers"])
        defender_stacks = _stacks(row["defenders"])
        boosts = _boost_rows(
            combat_boosts(
                state,
                row["attackers"],
                unit_defs,
                row["from_territory"],
                {unit.instance_id for unit in row["attackers"]},
            ),
            row["attackers"],
        ) + _boost_rows(
            combat_boosts(state, row["defenders"], unit_defs, row["to_territory"]),
            row["defenders"],
        )
        sim_units = apply_sim_ring_boosts(unit_defs, boosts)
        result = run_simulation(
            attacker_stacks,
            defender_stacks,
            row["to_territory"],
            sim_units,
            territory_defs,
            n_trials=n_trials,
            options=SimOptions(
                casualty_order_attacker="best_unit",
                casualty_order_defender="best_unit",
                must_conquer=False,
                is_sea_raid=bool(row["is_sea_raid"]),
                stronghold_initial_hp=row["stronghold_initial_hp"],
                prefire_penalty=bool(getattr(state, "prefire_penalty", True)),
            ),
            seed=seed + index,
        )
        row["p_conquer"] = result.p_conquer
        attacks.append(
            {
                "from_territory": row["from_territory"],
                "to_territory": row["to_territory"],
                "attacker_stacks": attacker_stacks,
                "defender_stacks": defender_stacks,
                "p_attacker_win": round(result.p_attacker_win, 4),
                "p_conquer": round(result.p_conquer, 4),
                "is_sea_raid": bool(row["is_sea_raid"]),
            }
        )

    return attacks, pending


def _board_snapshot(state) -> dict[str, Any]:
    owners: dict[str, str | None] = {}
    units: dict[str, list[dict[str, Any]]] = {}
    stronghold_hp: dict[str, int] = {}
    for tid in sorted(state.territories):
        territory = state.territories[tid]
        owners[tid] = territory.owner
        stacks = _stacks(territory.units)
        if stacks:
            units[tid] = stacks
        current = getattr(territory, "stronghold_current_health", None)
        if current is not None:
            stronghold_hp[tid] = int(current)
    return {"owners": owners, "units": units, "stronghold_hp": stronghold_hp}


def _conquering_units(units, unit_defs: dict[str, UnitDefinition], *, sea_raid: bool) -> list:
    """Units that would occupy the territory after this attack conquers."""
    if sea_raid:
        return [
            unit
            for unit in units
            if can_conquer_territory_as_attacker(unit_defs.get(unit.unit_id)) and is_land_unit(unit_defs.get(unit.unit_id))
        ]
    return [unit for unit in units if can_conquer_territory_as_attacker(unit_defs.get(unit.unit_id))]


def _apply_likely_conquests(
    state,
    faction_id: str,
    rows: list[dict[str, Any]],
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> list[dict[str, Any]]:
    """Move the likely, non-overlapping conquests onto the board for the next faction."""
    ranked = sorted(
        rows,
        key=lambda row: (-float(row.get("p_conquer") or 0), row["from_territory"], row["to_territory"]),
    )
    used: set[str] = set()
    assumed: list[dict[str, Any]] = []
    for row in ranked:
        chance = float(row.get("p_conquer") or 0)
        if chance < LIKELY_CONQUER:
            continue
        sea_raid = bool(row["is_sea_raid"])
        occupiers = _conquering_units(row["attackers"], unit_defs, sea_raid=sea_raid)
        if not occupiers:
            continue
        attacker_ids = {unit.instance_id for unit in row["attackers"]}
        if attacker_ids & used:
            continue
        # Ships stay in the sea zone. Everyone else on a land attack moves in with the stack.
        movers = occupiers if sea_raid else list(row["attackers"])
        if not _commit_conquest(state, faction_id, row, movers, occupiers, unit_defs, territory_defs, faction_defs):
            continue
        used |= attacker_ids
        assumed.append(
            {
                "from_territory": row["from_territory"],
                "to_territory": row["to_territory"],
                "p_conquer": round(chance, 4),
            }
        )
    assumed.sort(key=lambda row: (row["from_territory"], row["to_territory"]))
    return assumed


def _commit_conquest(
    state,
    faction_id: str,
    row: dict[str, Any],
    movers: list,
    occupiers: list,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> bool:
    dest_id = row["to_territory"]
    dest = state.territories.get(dest_id)
    if dest is None:
        return False
    dest_def = territory_defs.get(dest_id)
    if dest_def is None or not getattr(dest_def, "ownable", True):
        return False
    defender_ids = {unit.instance_id for unit in row["defenders"]}
    dest.units = [unit for unit in dest.units if unit.instance_id not in defender_ids]
    for unit in movers:
        for territory in state.territories.values():
            if unit in territory.units:
                territory.units.remove(unit)
                break
        unit.loaded_onto = None
        dest.units.append(unit)
    capturer = capture_owner_for_units(state, occupiers, faction_id, unit_defs, faction_defs)
    original = effective_original_owner(dest_id, dest, state)
    new_owner = capturer
    if original and original != capturer:
        capturer_def = faction_defs.get(capturer)
        original_def = faction_defs.get(original)
        capturer_alliance = getattr(capturer_def, "alliance", None) if capturer_def is not None else None
        original_alliance = getattr(original_def, "alliance", None) if original_def is not None else None
        if capturer_alliance and capturer_alliance == original_alliance:
            new_owner = original
    dest.owner = new_owner
    if getattr(dest_def, "is_stronghold", False):
        dest.stronghold_current_health = 0
    return True
