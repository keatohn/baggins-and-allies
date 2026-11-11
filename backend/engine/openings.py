"""Opening moves for each faction on turn 1.

Each faction's combat moves are chosen by the MCTS combat-move search, the
same decision the AI uses. Battles are then fought with the MCTS combat
policy, so the next faction sees the board that search actually left behind.
The pill on each arrow is a combat-sim chance for that move. It is not what
picks the move.

Results are cached for the process so reopening the admin tab does not
search again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import zlib
from collections import Counter, OrderedDict
from dataclasses import asdict, is_dataclass
from typing import Any

from backend.ai.context import AIContext
from backend.ai.mcts.budget import SearchBudget, budget_catalog_scope, get_phase_budget
from backend.ai.combat import decide_mcts_combat_policy
from backend.ai.mcts.combat_move import apply_bundled_moves, decide_combat_move_mcts
from backend.ai.mcts.dice import sample_dice_for_continue, sample_dice_for_initiate
from backend.ai.mcts import tree_cache
from backend.engine.actions import end_phase
from backend.engine.combat_sim import SimOptions, run_simulation
from backend.engine.definitions import (
    CampDefinition,
    FactionDefinition,
    TerritoryDefinition,
    UnitDefinition,
    faction_acts_as,
    controlling_faction_id,
)
from backend.engine.movement import (
    _craft_can_enter,
    _is_water_craft,
    _is_water_zone,
)
from backend.engine.queries import participates_in_sea_hex_naval_combat
from backend.engine.reducer import apply_action
from backend.engine.rings import combat_boosts
from backend.engine.special_rules import select_special_rules
from backend.engine.utils import (
    can_conquer_territory_as_attacker,
    get_unit_faction,
    initialize_game_state,
    is_aerial_unit,
)

_OPENING_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_OPENING_CACHE_MAX = 4
_MAX_COMBAT_MOVE_ACTIONS = 16
_MAX_COMBAT_ACTIONS = 200


def clear_opening_cache() -> None:
    """Drop cached opening plans. Tests use this to force a fresh search."""
    _OPENING_CACHE.clear()


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


def _owner_is_enemy(
    owner: str | None,
    acting_faction: str,
    faction_defs: dict[str, FactionDefinition],
) -> bool:
    if not owner:
        return True
    if faction_acts_as(faction_defs, owner, acting_faction):
        return False
    acting_alliance = _alliance_of(faction_defs, acting_faction)
    owner_alliance = _alliance_of(faction_defs, owner)
    if acting_alliance and owner_alliance and acting_alliance == owner_alliance:
        return False
    return True


def _stacks(units) -> list[dict[str, Any]]:
    counts = Counter(unit.unit_id for unit in units)
    return [{"unit_id": uid, "count": counts[uid]} for uid in sorted(counts)]


def _boost_rows(boosts: dict, units, *, side: str) -> list[dict[str, Any]]:
    by_id = {unit.instance_id: unit for unit in units}
    rows: dict[str, dict[str, Any]] = {}
    for instance_id, parts in boosts.items():
        unit = by_id.get(instance_id)
        if unit is None or not parts:
            continue
        attack, defense, dice, hp = parts
        row = rows.setdefault(
            unit.unit_id,
            {"unit_id": unit.unit_id, "attack": 0, "defense": 0, "dice": 0, "hp": 0, "side": side},
        )
        row["attack"] += int(attack or 0)
        row["defense"] += int(defense or 0)
        row["dice"] += int(dice or 0)
        row["hp"] += int(hp or 0)
    return [row for row in rows.values() if row["attack"] or row["defense"] or row["dice"] or row["hp"]]


def _fight_role(unit, origin_def, dest_def, unit_defs: dict[str, UnitDefinition]) -> str | None:
    """How this unit may fight at dest. None when the move would break a domain rule."""
    unit_def = unit_defs.get(unit.unit_id)
    if unit_def is None or dest_def is None:
        return None
    if _is_water_zone(dest_def):
        if is_aerial_unit(unit_def):
            return "naval"
        if (
            _is_water_craft(unit_def)
            and _craft_can_enter(unit_def, dest_def)
            and participates_in_sea_hex_naval_combat(unit, unit_def)
        ):
            return "naval"
        return None
    if _is_water_craft(unit_def):
        return None
    if _is_water_zone(origin_def):
        return "sea_raid"
    return "land"


def _freeze(value: Any) -> Any:
    if is_dataclass(value):
        return _freeze(asdict(value))
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _opening_cache_key(
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None,
    starting_setup: dict,
    manifest: dict,
    trials: int,
    seed: int,
    budget: SearchBudget,
) -> str:
    payload = {
        "units": _freeze(unit_defs),
        "territories": _freeze(territory_defs),
        "factions": _freeze(faction_defs),
        "camps": _freeze(camp_defs or {}),
        "starting_setup": _freeze(starting_setup),
        "manifest": _freeze(manifest),
        "n_trials": trials,
        "seed": seed,
        "mcts_iterations": budget.max_iterations,
        "mcts_seconds": budget.max_time_seconds,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


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
    mcts_iterations: int | None = None,
    mcts_seconds: float | None = None,
) -> dict[str, Any]:
    """Search each turn-order faction's opening, then leave that board for the next one."""
    manifest = manifest or {}
    trials = max(1, min(int(n_trials), 20_000))
    if mcts_iterations is None and mcts_seconds is None:
        budget = get_phase_budget("combat_move", "normal")
    else:
        budget = SearchBudget(
            max(1, int(mcts_iterations or 1)),
            max(0.05, float(mcts_seconds or 0.4)),
        )
    key = _opening_cache_key(
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        starting_setup,
        manifest,
        trials,
        seed,
        budget,
    )
    cached = _OPENING_CACHE.get(key)
    if cached is not None:
        _OPENING_CACHE.move_to_end(key)
        return copy.deepcopy(cached)

    result = _compute_opening_attacks(
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        starting_setup,
        manifest=manifest,
        n_trials=trials,
        seed=seed,
        budget=budget,
    )
    _OPENING_CACHE[key] = result
    _OPENING_CACHE.move_to_end(key)
    while len(_OPENING_CACHE) > _OPENING_CACHE_MAX:
        _OPENING_CACHE.popitem(last=False)
    return copy.deepcopy(result)


def _compute_opening_attacks(
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
    camp_defs: dict[str, CampDefinition] | None,
    starting_setup: dict,
    *,
    manifest: dict,
    n_trials: int,
    seed: int,
    budget: SearchBudget,
) -> dict[str, Any]:
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
    state.ai_algorithm = "mcts"
    state.ai_difficulty = "normal"
    order = [fid for fid in state.turn_order if fid in faction_defs]
    if not order and state.current_faction:
        order = [state.current_faction]

    levels = [{
        "id": "normal",
        "iterations": budget.max_iterations,
        "seconds": budget.max_time_seconds,
    }]
    tree_cache.clear()
    factions_out: list[dict[str, Any]] = []
    with budget_catalog_scope(levels):
        for faction_index, faction_id in enumerate(order):
            state.current_faction = faction_id
            state.phase = "combat_move"
            _refresh_movement(state, faction_id, unit_defs, faction_defs)
            rng = random.Random(seed + faction_index * 1009)
            faction_def = faction_defs.get(faction_id)
            board = _board_snapshot(state)
            attacks, assumed = _play_faction(
                state,
                faction_id,
                unit_defs,
                territory_defs,
                faction_defs,
                camp_defs,
                rng,
                n_trials=n_trials,
                seed=seed + faction_index * 1009,
            )
            factions_out.append(
                {
                    "faction_id": faction_id,
                    "faction_name": getattr(faction_def, "display_name", None) or faction_id,
                    "board": board,
                    "attacks": attacks,
                    "assumed": assumed,
                }
            )

    return {
        "n_trials": n_trials,
        "mcts_iterations": budget.max_iterations,
        "mcts_seconds": budget.max_time_seconds,
        "factions": factions_out,
    }


def _refresh_movement(state, faction_id: str, unit_defs, faction_defs) -> None:
    for territory in state.territories.values():
        for unit in territory.units:
            if faction_acts_as(faction_defs, get_unit_faction(unit, unit_defs), faction_id):
                unit.remaining_movement = unit.base_movement


def _play_faction(
    state,
    faction_id: str,
    unit_defs,
    territory_defs,
    faction_defs,
    camp_defs,
    rng: random.Random,
    *,
    n_trials: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Search this faction's combat moves, fight the battles, and keep the caller's state current."""
    original = state
    ports: dict = {}
    camps = camp_defs or {}
    ctx = AIContext(
        state=state,
        unit_defs=unit_defs,
        territory_defs=territory_defs,
        faction_defs=faction_defs,
        camp_defs=camps,
        port_defs=ports,
        available_actions={"can_end_phase": True},
    )
    legs: list[dict[str, Any]] = []
    for _step in range(_MAX_COMBAT_MOVE_ACTIONS):
        if state.phase != "combat_move" or state.winner:
            break
        ctx.state = state
        action = decide_combat_move_mcts(ctx, rng)
        if action is None or action.type == "end_phase":
            break
        if action.type != "move_units":
            try:
                state, _events = apply_action(state, action, unit_defs, territory_defs, faction_defs, camps, ports)
            except ValueError:
                break
            continue
        legs.extend(_move_legs(action))
        try:
            state, _events = apply_bundled_moves(state, action, unit_defs, territory_defs, faction_defs, camps, ports)
        except ValueError:
            break
    attacks = _arrows_for_legs(
        state,
        faction_id,
        legs,
        unit_defs,
        territory_defs,
        faction_defs,
        n_trials=n_trials,
        seed=seed,
    )
    if state.phase == "combat_move" and not state.winner:
        try:
            state, _events = apply_action(
                state, end_phase(faction_id), unit_defs, territory_defs, faction_defs, camps, ports
            )
        except ValueError:
            pass
    state = _resolve_combat(state, faction_id, unit_defs, territory_defs, faction_defs, camps, ports, rng)
    _adopt(original, state)
    assumed = _taken(original, faction_id, faction_defs, attacks)
    return attacks, assumed


def _adopt(target, source) -> None:
    """Point the caller's state at the played board when apply_action copied it."""
    if target is source:
        return
    target.__dict__.update(source.__dict__)


def _resolve_combat(state, faction_id, unit_defs, territory_defs, faction_defs, camps, ports, rng):
    ctx = AIContext(
        state=state,
        unit_defs=unit_defs,
        territory_defs=territory_defs,
        faction_defs=faction_defs,
        camp_defs=camps,
        port_defs=ports,
        available_actions={},
    )
    for _step in range(_MAX_COMBAT_ACTIONS):
        if state.phase != "combat" or state.winner:
            return state
        ctx.state = state
        action = decide_mcts_combat_policy(ctx, rng)
        if action is None:
            return state
        if action.type == "initiate_combat":
            payload = action.payload
            payload["dice_rolls"] = sample_dice_for_initiate(
                state,
                payload.get("territory_id"),
                payload.get("sea_zone_id"),
                faction_id,
                unit_defs,
                territory_defs,
                faction_defs,
                rng=rng,
            )
        elif action.type == "continue_combat":
            action.payload["dice_rolls"] = sample_dice_for_continue(
                state, unit_defs, territory_defs, faction_defs, rng=rng
            )
        try:
            state, _events = apply_action(state, action, unit_defs, territory_defs, faction_defs, camps, ports)
        except ValueError:
            return state
    return state


def _move_legs(action) -> list[dict[str, Any]]:
    payload = action.payload or {}
    legs = [{
        "from": payload.get("from"),
        "to": payload.get("to"),
        "ids": list(payload.get("unit_instance_ids") or []),
    }]
    for extra in payload.get("bundle_rest") or []:
        if not isinstance(extra, dict):
            continue
        legs.append({
            "from": extra.get("from"),
            "to": extra.get("to"),
            "ids": list(extra.get("unit_instance_ids") or []),
        })
    return [leg for leg in legs if leg["from"] and leg["to"] and leg["ids"]]


def _units_by_id(state) -> dict[str, Any]:
    found = {}
    for territory in state.territories.values():
        for unit in territory.units:
            found[unit.instance_id] = unit
    return found


def _arrows_for_legs(
    state,
    faction_id: str,
    legs: list[dict[str, Any]],
    unit_defs,
    territory_defs,
    faction_defs,
    *,
    n_trials: int,
    seed: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for leg in legs:
        key = (leg["from"], leg["to"])
        bucket = grouped.setdefault(key, [])
        for instance_id in leg["ids"]:
            if instance_id not in bucket:
                bucket.append(instance_id)
    by_id = _units_by_id(state)
    arrows: list[dict[str, Any]] = []
    for (origin, dest), instance_ids in grouped.items():
        origin_def = territory_defs.get(origin)
        dest_def = territory_defs.get(dest)
        dest_state = state.territories.get(dest)
        if dest_def is None or dest_state is None:
            continue
        fighters = []
        for instance_id in instance_ids:
            unit = by_id.get(instance_id)
            if unit is None:
                continue
            if _fight_role(unit, origin_def, dest_def, unit_defs) is None:
                continue
            fighters.append(unit)
        if not fighters:
            continue
        hostile = [
            unit
            for unit in dest_state.units
            if _is_hostile(get_unit_faction(unit, unit_defs), faction_id, faction_defs)
        ]
        water = _is_water_zone(dest_def)
        if water:
            defenders = [
                unit
                for unit in hostile
                if participates_in_sea_hex_naval_combat(unit, unit_defs.get(unit.unit_id))
            ]
            empty = False
            if not defenders:
                continue
        elif hostile:
            defenders = hostile
            empty = False
        elif (
            not dest_state.units
            and getattr(dest_def, "ownable", True)
            and not water
            and _owner_is_enemy(dest_state.owner, faction_id, faction_defs)
            and any(can_conquer_territory_as_attacker(unit_defs.get(unit.unit_id)) for unit in fighters)
        ):
            defenders = []
            empty = True
        else:
            continue
        sea_raid = bool(origin_def is not None and _is_water_zone(origin_def) and not water)
        if empty:
            p_win = 1.0
            p_conquer = 1.0
        else:
            p_win, p_conquer = _sim_chance(
                state,
                origin,
                dest,
                fighters,
                defenders,
                unit_defs,
                territory_defs,
                sea_raid=sea_raid,
                n_trials=n_trials,
                seed=seed + (zlib.adler32(f"{origin}|{dest}".encode()) % 100_000),
            )
        arrows.append(
            {
                "from_territory": origin,
                "to_territory": dest,
                "attacker_stacks": _stacks(fighters),
                "defender_stacks": _stacks(defenders),
                "p_attacker_win": round(p_win, 4),
                "p_conquer": round(p_conquer, 4),
                "is_sea_raid": sea_raid,
                "empty": empty,
            }
        )
    return arrows


def _sim_chance(
    state,
    origin: str,
    dest: str,
    attackers,
    defenders,
    unit_defs,
    territory_defs,
    *,
    sea_raid: bool,
    n_trials: int,
    seed: int,
) -> tuple[float, float]:
    dest_def = territory_defs.get(dest)
    dest_state = state.territories.get(dest)
    stronghold_hp = None
    if dest_def is not None and getattr(dest_def, "is_stronghold", False) and not _is_water_zone(dest_def):
        current_hp = getattr(dest_state, "stronghold_current_health", None) if dest_state is not None else None
        try:
            current_hp = int(current_hp) if current_hp is not None else None
        except (TypeError, ValueError):
            current_hp = None
        if current_hp is not None and current_hp > 0:
            stronghold_hp = current_hp
        elif current_hp is None:
            base_hp = int(getattr(dest_def, "stronghold_base_health", 0) or 0)
            if base_hp > 0:
                stronghold_hp = base_hp
    boosts = _boost_rows(
        combat_boosts(state, attackers, unit_defs, origin, {unit.instance_id for unit in attackers}),
        attackers,
        side="attacker",
    ) + _boost_rows(combat_boosts(state, defenders, unit_defs, dest), defenders, side="defender")
    result = run_simulation(
        _stacks(attackers),
        _stacks(defenders),
        dest,
        unit_defs,
        territory_defs,
        n_trials=n_trials,
        options=SimOptions(
            casualty_order_attacker="best_attack" if stronghold_hp else "best_unit",
            casualty_order_defender="best_unit",
            must_conquer=bool(stronghold_hp),
            is_sea_raid=sea_raid,
            stronghold_initial_hp=stronghold_hp,
            prefire_penalty=bool(getattr(state, "prefire_penalty", True)),
            ring_boosts=boosts,
        ),
        seed=seed,
    )
    return float(result.p_attacker_win), float(result.p_conquer)


def _taken(state, faction_id: str, faction_defs, attacks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assumed = []
    for attack in attacks:
        dest = state.territories.get(attack["to_territory"])
        if dest is None:
            continue
        if _owner_is_enemy(dest.owner, faction_id, faction_defs):
            continue
        if not dest.owner:
            continue
        assumed.append(
            {
                "from_territory": attack["from_territory"],
                "to_territory": attack["to_territory"],
                "p_conquer": attack["p_conquer"],
            }
        )
    assumed.sort(key=lambda row: (row["from_territory"], row["to_territory"]))
    return assumed


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
