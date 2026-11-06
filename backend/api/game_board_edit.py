"""Admin edit of one game's board: owners, stacks, camps, ports, and territory defs.

The editor speaks the setup-map shape (territory defs, territory_owners, starting_units,
camps, ports). This module projects a live GameState into that shape and writes a draft
back onto that game only — the shared setup is left alone.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.engine.definitions import definitions_from_snapshot
from backend.engine.state import GameState, PendingMove, TerritoryState, Unit

EDIT_GAME_PHRASE = "EDIT GAME"

_INSTANCE_LISTS = (
    "combat_move_moved_ids",
    "loaded_naval_must_attack_instance_ids",
    "combat_move_naval_idle_sail_instance_ids",
    "naval_mobilization_intruder_instance_ids",
    "avoided_forced_naval_combat_instance_ids",
)


class GameEditError(ValueError):
    """The draft cannot be applied. The message is safe to show to the admin."""


class UnitValidationError(GameEditError):
    """Units JSON failed the same checks as the setup editor. `errors` is the list."""

    def __init__(self, errors: list[str]):
        super().__init__(errors[0] if errors else "Units JSON is not valid")
        self.errors = errors


def editor_document(state: GameState, definitions: dict[str, Any]) -> dict[str, Any]:
    """Board the map editor can render: current owners, stacks, standing camps, ports."""
    defs = _definitions(definitions)
    territories = deepcopy(defs["territories"])
    owners: dict[str, str] = {}
    stacks: dict[str, list[dict[str, Any]]] = {}
    for tid, ts in state.territories.items():
        if tid not in territories:
            continue
        if ts.owner:
            owners[tid] = ts.owner
        counts: dict[str, int] = {}
        order: list[str] = []
        for unit in ts.units:
            if unit.unit_id not in counts:
                order.append(unit.unit_id)
            counts[unit.unit_id] = counts.get(unit.unit_id, 0) + 1
        if order:
            stacks[tid] = [{"unit_id": uid, "count": counts[uid]} for uid in order]
        row = territories.get(tid)
        if isinstance(row, dict) and row.get("is_stronghold") is True:
            base = _int(row.get("stronghold_base_health"), _int(row.get("stronghold_health"), 0))
            current = ts.stronghold_current_health if ts.stronghold_current_health is not None else base
            row["stronghold_current_health"] = current

    standing = set(state.camps_standing)
    camps: dict[str, Any] = {}
    for cid, camp in defs["camps"].items():
        if cid in standing and isinstance(camp, dict):
            camps[cid] = dict(camp)
    for cid, tid in (state.dynamic_camps or {}).items():
        if cid in standing and isinstance(tid, str) and tid:
            camps[cid] = {"id": cid, "territory_id": tid}

    return {
        "map_asset": state.map_asset or "",
        "manifest": {
            "map_asset": state.map_asset or "",
            "special_rules": list(state.special_rules or []),
        },
        "territories": territories,
        "starting_setup": {
            "turn_order": list(state.turn_order or []),
            "territory_owners": owners,
            "starting_units": stacks,
        },
        "camps": camps,
        "ports": deepcopy(defs["ports"]),
        "factions": defs["factions"],
        "units": defs["units"],
        "rings": [ring.to_dict() for ring in (state.rings or [])],
        "turn_number": state.turn_number,
        "phase": state.phase,
        "current_faction": state.current_faction,
    }


def apply_unit_document(
    state: GameState,
    definitions: dict[str, Any],
    units: Any,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace this game's unit definitions. Other setup data and the board are unchanged.

    Uses the setup editor's unit checks (ids, stats, faction, archetype, specials). A unit
    type that is still on the board cannot be removed.
    """
    defs = _definitions(definitions)
    if not isinstance(units, dict):
        raise UnitValidationError(["units must be a JSON object"])
    from backend.setup_validation import validate_setup_documents

    errors = [
        message
        for message in validate_setup_documents(
            {"id": "game", "is_active": False},
            units,
            defs["territories"],
            defs["factions"],
            {},
            {},
            {"turn_order": [str(fid) for fid in defs["factions"]]},
            catalog,
        )
        if _is_unit_validation_error(message)
    ]
    for unit_id in _unit_ids_in_play(state):
        if unit_id not in units:
            errors.append(f'unit "{unit_id}" is still on the board and cannot be removed')
    if errors:
        raise UnitValidationError(errors)

    snapshot = deepcopy(defs)
    snapshot["units"] = units
    try:
        definitions_from_snapshot(snapshot)
    except (KeyError, TypeError, ValueError) as e:
        raise UnitValidationError([f"Edited units are not valid: {e}"]) from e
    return snapshot


def _is_unit_validation_error(message: str) -> bool:
    return message.startswith("unit ") or message.startswith("units ") or "unit_id" in message


def _unit_ids_in_play(state: GameState) -> set[str]:
    ids: set[str] = set()
    for territory in state.territories.values():
        for unit in territory.units:
            if unit.unit_id:
                ids.add(unit.unit_id)
    for stacks in (state.faction_purchased_units or {}).values():
        for stack in stacks:
            if stack.unit_id and stack.count > 0:
                ids.add(stack.unit_id)
    for pending in state.pending_mobilizations or []:
        for row in pending.units:
            if isinstance(row, dict):
                unit_id = row.get("unit_id")
                if isinstance(unit_id, str) and unit_id:
                    ids.add(unit_id)
    return ids


def apply_editor_document(
    state: GameState,
    definitions: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    """Write the draft onto `state` and return the definitions snapshot to persist.

    `definitions` is the game's config snapshot. The returned dict is a new snapshot;
    `state` is mutated.
    """
    defs = _definitions(definitions)
    draft_territories = draft.get("territories")
    if not isinstance(draft_territories, dict):
        raise GameEditError("territories must be an object")
    setup = draft.get("starting_setup")
    if not isinstance(setup, dict):
        raise GameEditError("starting_setup must be an object")
    draft_camps = draft.get("camps")
    draft_ports = draft.get("ports")
    if not isinstance(draft_camps, dict) or not isinstance(draft_ports, dict):
        raise GameEditError("camps and ports must be objects")

    known = set(defs["territories"])
    for tid in list(draft_territories) + list(_owners(setup)) + list(_raw_stacks(setup)):
        if tid not in known:
            raise GameEditError(f"Unknown territory {tid}")

    merged_territories: dict[str, Any] = {}
    current_hp: dict[str, Any] = {}
    for tid, original in defs["territories"].items():
        patch = draft_territories.get(tid)
        if not isinstance(patch, dict):
            merged_territories[tid] = deepcopy(original)
            continue
        current_hp[tid] = patch.get("stronghold_current_health", None)
        cleaned = {k: v for k, v in patch.items() if k != "stronghold_current_health"}
        merged_territories[tid] = {**deepcopy(original), **cleaned}

    owners = _owners(setup)
    legal_owners = _owner_ids(defs["factions"])
    for tid, owner in owners.items():
        if owner not in legal_owners:
            raise GameEditError(f"Unknown owner {owner} for {tid}")

    stacks = _stacks(setup, defs["units"])
    _reject_duplicate_uniques(stacks, defs["units"])

    camps, dynamic = _camps(draft_camps, defs["camps"], state.dynamic_camps, known)
    ports = _sites(draft_ports, defs["ports"], known, "port")

    snapshot = deepcopy(defs)
    snapshot["territories"] = merged_territories
    snapshot["camps"] = camps
    snapshot["ports"] = ports
    try:
        definitions_from_snapshot(snapshot)
    except (KeyError, TypeError, ValueError) as e:
        raise GameEditError(f"Edited definitions are not valid: {e}") from e

    before = {
        u.instance_id
        for ts in state.territories.values()
        for u in ts.units
        if u.instance_id
    }
    taken = set(before)
    for tid in known:
        ts = state.territories.get(tid)
        if ts is None:
            ts = TerritoryState(owner=None)
            state.territories[tid] = ts
        ts.owner = owners.get(tid)
        ts.units = _reconcile_units(state, ts, stacks.get(tid, []), defs["units"], taken)
        _apply_stronghold(ts, merged_territories[tid], current_hp.get(tid, _MISSING))

    after = {
        u.instance_id
        for ts in state.territories.values()
        for u in ts.units
        if u.instance_id
    }
    _forget_units(state, before - after)

    state.camps_standing = list(camps) + [cid for cid in dynamic if cid not in camps]
    state.dynamic_camps = dynamic
    if state.phase == "mobilization":
        camp_territories = _standing_camp_territories(camps, dynamic, state.camps_standing)
        state.mobilization_camps = [
            tid
            for tid, ts in state.territories.items()
            if ts.owner == state.current_faction and tid in camp_territories
        ]
    return snapshot


_MISSING = object()


def _definitions(definitions: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(definitions, dict):
        raise GameEditError("This game has no definition snapshot to edit")
    out = dict(definitions)
    for key in ("units", "territories", "factions", "camps", "ports"):
        value = out.get(key)
        if value is None:
            out[key] = {}
        elif not isinstance(value, dict):
            raise GameEditError(f"definitions.{key} is not an object")
    if not out["territories"] or not out["units"] or not out["factions"]:
        raise GameEditError("This game has no definition snapshot to edit")
    return out


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _owners(setup: dict[str, Any]) -> dict[str, str]:
    raw = setup.get("territory_owners") or {}
    if not isinstance(raw, dict):
        raise GameEditError("territory_owners must be an object")
    out: dict[str, str] = {}
    for tid, owner in raw.items():
        if owner is None or owner == "":
            continue
        if not isinstance(owner, str):
            raise GameEditError(f"Owner of {tid} must be a faction id")
        out[str(tid)] = owner
    return out


def _raw_stacks(setup: dict[str, Any]) -> dict[str, Any]:
    raw = setup.get("starting_units") or {}
    if not isinstance(raw, dict):
        raise GameEditError("starting_units must be an object")
    return raw


def _owner_ids(factions: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for fid, faction in factions.items():
        ids.add(str(fid))
        if not isinstance(faction, dict):
            continue
        subs = faction.get("subfactions") or []
        if isinstance(subs, dict):
            subs = list(subs.values())
        if not isinstance(subs, (list, tuple)):
            continue
        for sub in subs:
            if isinstance(sub, dict) and isinstance(sub.get("id"), str) and sub["id"]:
                ids.add(sub["id"])
    return ids


def _stacks(setup: dict[str, Any], units: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = {}
    for tid, rows in _raw_stacks(setup).items():
        if not isinstance(rows, list):
            raise GameEditError(f"Units in {tid} must be a list")
        merged: dict[str, int] = {}
        order: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                raise GameEditError(f"Units in {tid} must be stacks")
            uid = row.get("unit_id")
            if not isinstance(uid, str) or not uid:
                raise GameEditError(f"A stack in {tid} is missing a unit")
            if uid not in units:
                raise GameEditError(f"Unknown unit {uid}")
            unit = units[uid] if isinstance(units.get(uid), dict) else {}
            if _int(unit.get("health"), 1) < 1:
                raise GameEditError(f"{uid} has no health")
            count = _int(row.get("count"), 0)
            if count < 1:
                continue
            if uid not in merged:
                order.append(uid)
                merged[uid] = 0
            merged[uid] += count
        if order:
            out[str(tid)] = [(uid, merged[uid]) for uid in order]
    return out


def _reject_duplicate_uniques(stacks: dict[str, list[tuple[str, int]]], units: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for tid, rows in stacks.items():
        for uid, count in rows:
            unit = units.get(uid) or {}
            if not (isinstance(unit, dict) and unit.get("unique") is True):
                continue
            if count > 1 or uid in seen:
                raise GameEditError(f"{uid} is unique and cannot be placed more than once")
            seen[uid] = tid


def _sites(
    draft: dict[str, Any],
    previous: dict[str, Any],
    territories: set[str],
    kind: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sid, site in draft.items():
        if not isinstance(site, dict):
            raise GameEditError(f"{kind} {sid} must be an object")
        tid = site.get("territory_id")
        if not isinstance(tid, str) or tid not in territories:
            raise GameEditError(f"{kind} {sid} is not on a known territory")
        base = dict(previous[sid]) if isinstance(previous.get(sid), dict) else {}
        base.update(site)
        base["id"] = str(sid)
        base["territory_id"] = tid
        out[str(sid)] = base
    return out


def _camps(
    draft: dict[str, Any],
    previous: dict[str, Any],
    dynamic_camps: dict[str, str],
    territories: set[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    defs: dict[str, Any] = {}
    dynamic: dict[str, str] = {}
    old_dynamic = set(dynamic_camps or {})
    for cid, site in draft.items():
        if not isinstance(site, dict):
            raise GameEditError(f"camp {cid} must be an object")
        tid = site.get("territory_id")
        if not isinstance(tid, str) or tid not in territories:
            raise GameEditError(f"camp {cid} is not on a known territory")
        if str(cid) in old_dynamic:
            dynamic[str(cid)] = tid
            continue
        base = dict(previous[cid]) if isinstance(previous.get(cid), dict) else {}
        base.update(site)
        base["id"] = str(cid)
        base["territory_id"] = tid
        defs[str(cid)] = base
    return defs, dynamic


def _fresh_id(state: GameState, faction_id: str, unit_id: str, taken: set[str]) -> str:
    faction = faction_id.strip() or "unit"
    for _ in range(10000):
        instance_id = state.generate_unit_instance_id(faction, unit_id)
        if instance_id not in taken:
            taken.add(instance_id)
            return instance_id
    raise GameEditError(f"Could not assign an id for {unit_id}")


def _reconcile_units(
    state: GameState,
    territory: TerritoryState,
    stacks: list[tuple[str, int]],
    units: dict[str, Any],
    taken: set[str],
) -> list[Unit]:
    grouped: dict[str, list[Unit]] = {}
    for unit in territory.units:
        grouped.setdefault(unit.unit_id, []).append(unit)
    kept: list[Unit] = []
    for unit_id, count in stacks:
        pool = grouped.get(unit_id) or []
        kept.extend(pool[:count])
        unit_def = units[unit_id] if isinstance(units.get(unit_id), dict) else {}
        health = _int(unit_def.get("health"), 1)
        movement = max(0, _int(unit_def.get("movement"), 0))
        if health < 1:
            raise GameEditError(f"{unit_id} has no health")
        faction = unit_def.get("faction") if isinstance(unit_def.get("faction"), str) else ""
        faction = faction or territory.owner or "unit"
        for _ in range(count - len(pool)):
            kept.append(
                Unit(
                    instance_id=_fresh_id(state, faction, unit_id, taken),
                    unit_id=unit_id,
                    remaining_movement=movement,
                    remaining_health=health,
                    base_movement=movement,
                    base_health=health,
                )
            )
    for unit in kept:
        if unit.loaded_onto and not any(other.instance_id == unit.loaded_onto for other in kept):
            unit.loaded_onto = None
    return kept


def _apply_stronghold(territory: TerritoryState, definition: dict[str, Any], current: Any) -> None:
    if definition.get("is_stronghold") is not True:
        territory.stronghold_current_health = None
        return
    base = _int(definition.get("stronghold_base_health"), _int(definition.get("stronghold_health"), 0))
    if base < 1:
        base = 1
        definition["stronghold_base_health"] = base
    if current is _MISSING or current is None:
        hp = territory.stronghold_current_health if territory.stronghold_current_health is not None else base
    else:
        hp = _int(current, base)
    if hp < 0:
        hp = 0
    if hp > base:
        definition["stronghold_base_health"] = hp
    territory.stronghold_current_health = hp


def _standing_camp_territories(
    camps: dict[str, Any],
    dynamic: dict[str, str],
    standing: list[str],
) -> set[str]:
    territories: set[str] = set()
    for camp_id in standing:
        if camp_id in dynamic:
            territories.add(dynamic[camp_id])
        camp = camps.get(camp_id)
        if isinstance(camp, dict) and isinstance(camp.get("territory_id"), str):
            territories.add(camp["territory_id"])
    return territories


def _forget_units(state: GameState, removed: set[str]) -> None:
    if not removed:
        return
    for territory in state.territories.values():
        for unit in territory.units:
            if unit.loaded_onto in removed:
                unit.loaded_onto = None
    kept_moves: list[PendingMove] = []
    for move in state.pending_moves:
        ids = [iid for iid in move.unit_instance_ids if iid not in removed]
        if not ids:
            continue
        move.unit_instance_ids = ids
        if move.load_onto_boat_instance_id in removed:
            move.load_onto_boat_instance_id = None
        kept_moves.append(move)
    state.pending_moves = kept_moves
    for name in _INSTANCE_LISTS:
        values = getattr(state, name, None)
        if isinstance(values, list):
            setattr(state, name, [iid for iid in values if iid not in removed])
    raid = state.territory_sea_raid_passenger_instance_ids
    if isinstance(raid, dict):
        for tid, ids in list(raid.items()):
            if isinstance(ids, list):
                raid[tid] = [iid for iid in ids if iid not in removed]
    combat = state.active_combat
    if combat is not None:
        combat.attacker_instance_ids = [iid for iid in combat.attacker_instance_ids if iid not in removed]
        combat.initial_attacker_instance_ids = [
            iid for iid in combat.initial_attacker_instance_ids if iid not in removed
        ]
        combat.initial_defender_instance_ids = [
            iid for iid in combat.initial_defender_instance_ids if iid not in removed
        ]
        combat.ladder_infantry_instance_ids = [
            iid for iid in combat.ladder_infantry_instance_ids if iid not in removed
        ]
        embarked = combat.naval_embarked_attacker_loaded_onto or {}
        combat.naval_embarked_attacker_loaded_onto = {
            iid: boat for iid, boat in embarked.items() if iid not in removed and boat not in removed
        }
        if not combat.attacker_instance_ids:
            state.active_combat = None
    for ring in state.rings or []:
        if ring.bearer_instance_id in removed:
            ring.bearer_instance_id = None
        if ring.carried_in_by in removed:
            ring.carried_in_by = None
