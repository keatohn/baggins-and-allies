"""
Optional setup-manifest rules snapshotted onto a game at creation.

Fading territory: each time the turn counter advances, that territory's power
production drops by fade_per_turn until it reaches floor. Turn 1 uses the
printed production (no fade yet). Floor never raises production above the
printed value.
"""

from dataclasses import replace
from typing import Any


def parse_starting_message(raw: Any) -> str | None:
    """Non-empty manifest starting_message, or None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def parse_special_rules(raw: Any) -> list[dict[str, Any]]:
    """
    Canonical special_rules list. Drops malformed entries.
    Unknown rule types are kept so future rules round-trip.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen_fading: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if not isinstance(typ, str) or not typ.strip():
            continue
        typ = typ.strip()
        if typ == "fading_territory":
            territories: list[dict[str, Any]] = []
            rows = item.get("territories")
            if isinstance(rows, list):
                for row in rows:
                    parsed = _parse_fading_territory_row(row, seen_fading)
                    if parsed:
                        territories.append(parsed)
            if territories:
                out.append({"type": "fading_territory", "territories": territories})
        else:
            kept = dict(item)
            kept["type"] = typ
            out.append(kept)
    return out


def parse_nonneg_int(raw: Any) -> int | None:
    """Whole number >= 0. Rejects bools (which are ints in Python)."""
    if isinstance(raw, bool) or isinstance(raw, str):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw >= 0:
        return int(raw)
    return None


def _parse_fading_territory_row(row: Any, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    tid = row.get("territory_id")
    if not isinstance(tid, str) or not tid.strip():
        return None
    tid = tid.strip()
    if tid in seen:
        return None
    fade = parse_nonneg_int(row.get("fade_per_turn"))
    floor = parse_nonneg_int(row.get("floor"))
    if fade is None or floor is None:
        return None
    seen.add(tid)
    return {"territory_id": tid, "fade_per_turn": fade, "floor": floor}


def fading_territory_index(special_rules: Any) -> dict[str, tuple[int, int]]:
    """territory_id -> (fade_per_turn, floor)."""
    index: dict[str, tuple[int, int]] = {}
    for rule in parse_special_rules(special_rules):
        if rule.get("type") != "fading_territory":
            continue
        for row in rule.get("territories") or []:
            if not isinstance(row, dict):
                continue
            tid = row.get("territory_id")
            if isinstance(tid, str):
                index[tid] = (int(row["fade_per_turn"]), int(row["floor"]))
    return index


def effective_territory_power(
    base_power: int,
    turn_number: int,
    special_rules: Any,
    territory_id: str,
) -> int:
    """Power this territory produces on turn_number (1-based game turn)."""
    try:
        base = int(base_power or 0)
    except (TypeError, ValueError):
        base = 0
    if base < 0:
        base = 0
    spec = fading_territory_index(special_rules).get(territory_id)
    if not spec:
        return base
    fade, floor = spec
    try:
        turn = int(turn_number)
    except (TypeError, ValueError):
        turn = 1
    elapsed = max(0, turn - 1)
    lowered = base - max(0, fade) * elapsed
    floor_n = min(max(0, floor), base)
    return max(floor_n, lowered)


def territory_current_power(state: Any, territory_id: str, territory_def: Any) -> int:
    """Current power production, including fading-territory special rules."""
    base = 0
    produces = getattr(territory_def, "produces", None) if territory_def is not None else None
    if isinstance(produces, dict):
        try:
            base = int(produces.get("power", 0) or 0)
        except (TypeError, ValueError):
            base = 0
    turn = 1
    rules = None
    if state is not None:
        turn = getattr(state, "turn_number", 1)
        rules = getattr(state, "special_rules", None)
    return effective_territory_power(base, turn, rules, territory_id)


def territory_defs_with_current_power(territory_defs: dict, state: Any) -> dict:
    """
    Copy of territory defs whose fading territories use current power.
    Original definitions are not mutated. Unchanged when nothing is fading.
    """
    index = fading_territory_index(getattr(state, "special_rules", None) if state is not None else None)
    if not index or not territory_defs:
        return territory_defs
    out = dict(territory_defs)
    changed = False
    for tid in index:
        tdef = territory_defs.get(tid)
        if tdef is None:
            continue
        produces = getattr(tdef, "produces", None)
        base_map = dict(produces) if isinstance(produces, dict) else {}
        current = territory_current_power(state, tid, tdef)
        try:
            printed = int(base_map.get("power", 0) or 0)
        except (TypeError, ValueError):
            printed = 0
        if printed == current and "power" in base_map:
            continue
        base_map["power"] = current
        out[tid] = replace(tdef, produces=base_map)
        changed = True
    return out if changed else territory_defs


def attach_manifest_rules(result: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Copy optional starting_message and special_rules from a manifest onto a setup dict."""
    message = parse_starting_message(manifest.get("starting_message"))
    if message:
        result["starting_message"] = message
    rules = parse_special_rules(manifest.get("special_rules"))
    if rules:
        result["special_rules"] = rules
    from backend.engine.subfaction_rules import parse_subfaction_rules
    subfaction_rules = parse_subfaction_rules(manifest.get("subfaction_rules"))
    if subfaction_rules:
        result["subfaction_rules"] = subfaction_rules
