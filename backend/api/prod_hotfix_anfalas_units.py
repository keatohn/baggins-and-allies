"""
TEMPORARY: one-off prod correction — remove after use.

Adds 2 gondor_knight and 1 citadel_guard to anfalas for a fixed game id only.
Uses stable instance_ids so the operation is idempotent (safe to retry).
"""

from __future__ import annotations

import json
from typing import Any

from backend.engine.definitions import UnitDefinition

# Update if this fix targets a different saved game.
PROD_HOTFIX_GAME_ID = "4f0c91c4-42fc-4b66-995e-16fd0e1b42cb"

TARGET_TERRITORY = "anfalas"

# Fixed ids — must not collide with normal gondor_*_{NNN} pattern.
_HOTFIX_INSTANCE_IDS: tuple[str, ...] = (
    "gondor_gondor_knight_anfalas_hotfix_1",
    "gondor_gondor_knight_anfalas_hotfix_2",
    "gondor_citadel_guard_anfalas_hotfix_1",
)
_PLACEMENTS: tuple[tuple[str, str], ...] = (
    ("gondor_knight", _HOTFIX_INSTANCE_IDS[0]),
    ("gondor_knight", _HOTFIX_INSTANCE_IDS[1]),
    ("citadel_guard", _HOTFIX_INSTANCE_IDS[2]),
)


def _unit_payload(unit_def: UnitDefinition, instance_id: str, unit_id: str) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "unit_id": unit_id,
        "remaining_movement": unit_def.movement,
        "remaining_health": unit_def.health,
        "base_movement": unit_def.movement,
        "base_health": unit_def.health,
    }


def _scan_instance_locations(raw_state: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """instance_id -> (territory_id, unit_id)."""
    out: dict[str, tuple[str, str]] = {}
    territories = raw_state.get("territories")
    if not isinstance(territories, dict):
        return out
    for tid, tdata in territories.items():
        if not isinstance(tdata, dict):
            continue
        units = tdata.get("units")
        if not isinstance(units, list):
            continue
        for u in units:
            if not isinstance(u, dict):
                continue
            iid = str(u.get("instance_id") or "").strip()
            if iid:
                out[iid] = (str(tid), str(u.get("unit_id") or ""))
    return out


def apply_add_gondor_units_anfalas(
    raw_state: dict[str, Any],
    unit_defs: dict[str, UnitDefinition],
) -> tuple[bool, list[str]]:
    """
    Mutates `raw_state` only by appending three units to territories.anfalas.units when needed.

    Returns (did_mutate, instance_ids).

    Raises ValueError if preconditions fail.
    """
    territories = raw_state.get("territories")
    if not isinstance(territories, dict):
        raise ValueError("game_state.territories missing or not an object")

    anf = territories.get(TARGET_TERRITORY)
    if not isinstance(anf, dict):
        raise ValueError(f"territories.{TARGET_TERRITORY} missing or not an object")

    expected: dict[str, str] = {iid: uid for uid, iid in _PLACEMENTS}
    hotfix_ids = frozenset(expected.keys())

    locations = _scan_instance_locations(raw_state)
    found_hotfix = {iid: locations[iid] for iid in hotfix_ids if iid in locations}

    if len(found_hotfix) == len(hotfix_ids):
        for iid, (tid, uid) in found_hotfix.items():
            if tid != TARGET_TERRITORY:
                raise ValueError(
                    f"hotfix unit {iid!r} already exists in {tid!r}, expected only in {TARGET_TERRITORY!r}"
                )
            if uid != expected[iid]:
                raise ValueError(
                    f"hotfix unit {iid!r} has unit_id {uid!r}, expected {expected[iid]!r}"
                )
        return False, list(_HOTFIX_INSTANCE_IDS)

    if len(found_hotfix) != 0:
        missing = sorted(hotfix_ids - frozenset(found_hotfix.keys()))
        raise ValueError(
            f"partial hotfix state: found {sorted(found_hotfix.keys())}, missing {missing}; fix manually"
        )

    for unit_id, instance_id in _PLACEMENTS:
        ud = unit_defs.get(unit_id)
        if not ud:
            raise ValueError(f"unit definition not found for {unit_id!r} (wrong setup snapshot?)")

    units_list = anf.get("units")
    if units_list is None:
        units_list = []
        anf["units"] = units_list
    if not isinstance(units_list, list):
        raise ValueError(f"territories.{TARGET_TERRITORY}.units must be a list")

    for unit_id, instance_id in _PLACEMENTS:
        ud = unit_defs[unit_id]
        units_list.append(_unit_payload(ud, instance_id, unit_id))

    return True, list(_HOTFIX_INSTANCE_IDS)


def hotfix_add_gondor_anfalas_json(
    game_state_text: str,
    unit_defs: dict[str, UnitDefinition],
) -> tuple[str, bool, list[str]]:
    """Parse JSON, apply hotfix, return (new_json_text, did_mutate, instance_ids)."""
    raw = json.loads(game_state_text) if isinstance(game_state_text, str) else game_state_text
    if not isinstance(raw, dict):
        raise ValueError("game_state is not a JSON object")
    mutated, ids = apply_add_gondor_units_anfalas(raw, unit_defs)
    return json.dumps(raw), mutated, ids
