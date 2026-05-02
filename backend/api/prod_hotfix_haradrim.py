"""
TEMPORARY: one-off prod correction — remove after the affected game is fixed.

Moves exactly two haradrim_archer units from sea_zone_10 to dol_amroth in a fixed game id,
without touching any other JSON fields or unit payloads (same dict objects, relocated only).
"""

from __future__ import annotations

import json
from typing import Any

# Frozen target — do not generalize without removing this module.
PROD_HOTFIX_GAME_ID = "4f0c91c4-42fc-4b66-995e-16fd0e1b42cb"
FROM_TERRITORY = "sea_zone_10"
TO_TERRITORY = "dol_amroth"
UNIT_TYPE = "haradrim_archer"
REQUIRED_COUNT = 2


def _reject_if_instance_ids_in_structure(obj: Any, blocked: set[str], ctx: str) -> None:
    """Walk nested dict/list JSON and fail if any blocked instance_id string appears as a value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v in blocked:
                raise ValueError(f"{ctx}: references blocked instance_id {v!r}")
            _reject_if_instance_ids_in_structure(v, blocked, ctx)
    elif isinstance(obj, list):
        for item in obj:
            _reject_if_instance_ids_in_structure(item, blocked, ctx)


def apply_haradrim_sea10_to_dol_amroth_hotfix(state: dict[str, Any]) -> list[str]:
    """
    Mutates `state` in place: removes exactly two haradrim_archer dicts from sea_zone_10.units
    and appends them to dol_amroth.units. Unit dicts are unchanged.

    Returns the two instance_ids moved.

    Raises ValueError if preconditions are not met (nothing is mutated).
    """
    territories = state.get("territories")
    if not isinstance(territories, dict):
        raise ValueError("game_state.territories missing or not an object")

    sea = territories.get(FROM_TERRITORY)
    dol = territories.get(TO_TERRITORY)
    if not isinstance(sea, dict):
        raise ValueError(f"territories.{FROM_TERRITORY} missing or not an object")
    if not isinstance(dol, dict):
        raise ValueError(f"territories.{TO_TERRITORY} missing or not an object")

    sea_units = sea.get("units")
    if not isinstance(sea_units, list):
        raise ValueError(f"territories.{FROM_TERRITORY}.units must be a list")

    archers = [
        u
        for u in sea_units
        if isinstance(u, dict) and str(u.get("unit_id") or "") == UNIT_TYPE
    ]
    if len(archers) != REQUIRED_COUNT:
        raise ValueError(
            f"expected exactly {REQUIRED_COUNT} {UNIT_TYPE} in {FROM_TERRITORY}, found {len(archers)}"
        )

    iids: list[str] = []
    for u in archers:
        iid = str(u.get("instance_id") or "").strip()
        if not iid:
            raise ValueError(f"{UNIT_TYPE} in {FROM_TERRITORY} missing instance_id")
        iids.append(iid)
    if len(set(iids)) != REQUIRED_COUNT:
        raise ValueError(f"duplicate instance_id among {UNIT_TYPE} in {FROM_TERRITORY}")

    sea_ids = {
        str(x.get("instance_id") or "").strip()
        for x in sea_units
        if isinstance(x, dict) and str(x.get("instance_id") or "").strip()
    }
    for u in archers:
        lo = u.get("loaded_onto")
        if lo is None or str(lo).strip() == "":
            continue
        carrier_id = str(lo).strip()
        if carrier_id not in sea_ids:
            raise ValueError(
                f"unit {u.get('instance_id')!r} has loaded_onto={carrier_id!r} "
                f"but that carrier is not in {FROM_TERRITORY}"
            )

    blocked = set(iids)

    for pm in state.get("pending_moves") or []:
        if not isinstance(pm, dict):
            continue
        uids = pm.get("unit_instance_ids")
        if isinstance(uids, list) and any(str(x) in blocked for x in uids):
            raise ValueError("pending_moves still references these units; resolve or clear first")

    ac = state.get("active_combat")
    if ac is not None:
        if not isinstance(ac, dict):
            raise ValueError("active_combat is present but not an object")
        for key in (
            "attacker_instance_ids",
            "initial_attacker_instance_ids",
            "initial_defender_instance_ids",
            "ladder_infantry_instance_ids",
        ):
            ids = ac.get(key)
            if isinstance(ids, list) and any(str(x) in blocked for x in ids):
                raise ValueError(f"active_combat.{key} references these units; end combat first")
        _reject_if_instance_ids_in_structure(ac.get("combat_log"), blocked, "active_combat.combat_log")

    dol_units = dol.get("units")
    if dol_units is None:
        dol_units = []
        dol["units"] = dol_units
    if not isinstance(dol_units, list):
        raise ValueError(f"territories.{TO_TERRITORY}.units must be a list")

    for u in dol_units:
        if isinstance(u, dict) and str(u.get("instance_id") or "") in blocked:
            raise ValueError(f"affected units already present in {TO_TERRITORY}")

    # Order preserved as they appeared in sea_zone_10.
    ordered = [u for u in sea_units if isinstance(u, dict) and str(u.get("instance_id") or "") in blocked]
    if len(ordered) != REQUIRED_COUNT:
        raise ValueError("internal: could not resolve archer rows to move")

    new_sea = [
        u
        for u in sea_units
        if not (isinstance(u, dict) and str(u.get("instance_id") or "") in blocked)
    ]
    if len(new_sea) != len(sea_units) - REQUIRED_COUNT:
        raise ValueError("internal: sea zone unit list did not shrink by exactly two")

    sea["units"] = new_sea
    # Ashore: drop sea-transport linkage (same relocation; unit payloads otherwise unchanged).
    for u in ordered:
        if isinstance(u, dict) and "loaded_onto" in u:
            del u["loaded_onto"]
    dol_units.extend(ordered)

    return iids


def hotfix_game_row_game_state_json(game_state_text: str) -> tuple[str, list[str]]:
    """Parse JSON, apply hotfix, return (new_json_text, moved_instance_ids)."""
    raw = json.loads(game_state_text) if isinstance(game_state_text, str) else game_state_text
    if not isinstance(raw, dict):
        raise ValueError("game_state is not a JSON object")
    moved = apply_haradrim_sea10_to_dol_amroth_hotfix(raw)
    return json.dumps(raw), moved
