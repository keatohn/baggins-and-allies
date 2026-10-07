"""
Optional setup-manifest rules snapshotted onto a game at creation.

Evolving territory: each time the turn counter advances, that territory's power
production changes by step until it reaches stop_at. A negative step falls.
A positive step rises. Turn 1 uses the printed production. A stop on the wrong
side of printed power does not move it. Older fading_territory rows are read
as a negative step, and a leftover floor key is read as stop_at.
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
    seen_evolving: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if not isinstance(typ, str) or not typ.strip():
            continue
        typ = typ.strip()
        if typ in ("evolving_territory", "fading_territory"):
            territories: list[dict[str, Any]] = []
            rows = item.get("territories")
            if isinstance(rows, list):
                for row in rows:
                    parsed = _parse_evolving_territory_row(row, seen_evolving, legacy=typ == "fading_territory")
                    if parsed:
                        territories.append(parsed)
            if territories:
                out.append(_with_rule_meta({"type": "evolving_territory", "territories": territories}, item))
        else:
            kept = _with_rule_meta(dict(item), item)
            kept["type"] = typ
            out.append(kept)
    return out


def _with_rule_meta(rule: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Every rule carries is_optional. A name is kept only when it is non-empty."""
    rule["is_optional"] = source.get("is_optional") is True
    name = source.get("name")
    if isinstance(name, str) and name.strip():
        rule["name"] = name.strip()
    else:
        rule.pop("name", None)
    return rule


def rule_display_name(rule: dict[str, Any]) -> str:
    """Name shown as a special mode. Falls back to a readable type."""
    name = rule.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    words = [part for part in str(rule.get("type") or "").split("_") if part]
    small = {"of", "the", "and", "a"}
    parts: list[str] = []
    for index, word in enumerate(words):
        parts.append(word if index and word in small else word[:1].upper() + word[1:])
    return " ".join(parts) or "Special rule"


def optional_rule_menu(raw: Any) -> list[dict[str, str]]:
    """Optional rules a create-game scenario card and settings step should offer."""
    menu: list[dict[str, str]] = []
    for rule in parse_special_rules(raw):
        if rule.get("is_optional") is not True:
            continue
        typ = rule.get("type")
        if not isinstance(typ, str) or not typ:
            continue
        menu.append({"type": typ, "name": rule_display_name(rule)})
    return menu


def rings_rule_is_mandatory(raw: Any) -> bool:
    """A Rings of Power catalog that is not a player choice."""
    for rule in parse_special_rules(raw):
        if rule.get("type") == "rings_of_power" and rule.get("is_optional") is not True:
            return True
    return False


def select_special_rules(
    raw: Any,
    optional_rules: dict[str, Any] | None = None,
    *,
    legacy_rings: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Rules that apply to a new game, and whether Rings of Power is on.

    Non-optional rules always apply. Optional rules default on.
    When optional_rules is omitted, a rings catalog still follows legacy_rings
    and every other optional rule stays on.
    """
    kept: list[dict[str, Any]] = []
    rings_on = False
    choices = optional_rules if isinstance(optional_rules, dict) else None
    for rule in parse_special_rules(raw):
        typ = rule.get("type")
        if not isinstance(typ, str):
            continue
        if rule.get("is_optional") is True:
            if choices is not None:
                enabled = True if typ not in choices else bool(choices[typ])
            elif typ == "rings_of_power":
                enabled = bool(legacy_rings)
            else:
                enabled = True
        else:
            enabled = True
        if not enabled:
            continue
        kept.append(rule)
        if typ == "rings_of_power":
            rings_on = True
    return kept, rings_on


def parse_nonneg_int(raw: Any) -> int | None:
    """Whole number >= 0. Rejects bools (which are ints in Python)."""
    if isinstance(raw, bool) or isinstance(raw, str):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw >= 0:
        return int(raw)
    return None


def parse_signed_int(raw: Any) -> int | None:
    """Whole number, positive or negative. Rejects bools."""
    if isinstance(raw, bool) or isinstance(raw, str):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def _parse_evolving_territory_row(row: Any, seen: set[str], *, legacy: bool = False) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    tid = row.get("territory_id")
    if not isinstance(tid, str) or not tid.strip():
        return None
    tid = tid.strip()
    if tid in seen:
        return None
    if legacy:
        fade = parse_nonneg_int(row.get("fade_per_turn"))
        step = None if fade is None or fade == 0 else -fade
    else:
        step = parse_signed_int(row.get("step"))
        if step == 0:
            step = None
    stop = parse_nonneg_int(row.get("stop_at"))
    if stop is None:
        stop = parse_nonneg_int(row.get("floor"))
    if step is None or stop is None:
        return None
    seen.add(tid)
    return {"territory_id": tid, "step": step, "stop_at": stop}


def evolving_territory_index(special_rules: Any) -> dict[str, tuple[int, int]]:
    """territory_id -> (step, stop_at)."""
    index: dict[str, tuple[int, int]] = {}
    for rule in parse_special_rules(special_rules):
        if rule.get("type") != "evolving_territory":
            continue
        for row in rule.get("territories") or []:
            if not isinstance(row, dict):
                continue
            tid = row.get("territory_id")
            if isinstance(tid, str):
                index[tid] = (int(row["step"]), int(row["stop_at"]))
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
    spec = evolving_territory_index(special_rules).get(territory_id)
    if not spec:
        return base
    step, bound = spec
    try:
        turn = int(turn_number)
    except (TypeError, ValueError):
        turn = 1
    elapsed = max(0, turn - 1)
    moved = base + step * elapsed
    if step < 0:
        stop = min(max(0, bound), base)
        return max(stop, moved)
    if step > 0:
        ceiling = max(bound, base)
        return min(ceiling, moved)
    return base


def territory_current_power(state: Any, territory_id: str, territory_def: Any) -> int:
    """Current power production, including evolving-territory special rules."""
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
    Copy of territory defs whose evolving territories use current power.
    Original definitions are not mutated. Unchanged when nothing is evolving.
    """
    index = evolving_territory_index(getattr(state, "special_rules", None) if state is not None else None)
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
