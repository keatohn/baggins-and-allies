"""
Validate setup JSON blobs before persisting (territory symmetry, id references).
"""

from __future__ import annotations

import json
from typing import Any


def _as_obj(raw: str | dict[str, Any] | None, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None:
        return None, f"{label} is missing"
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"{label} is not valid JSON: {e}"
        if not isinstance(v, dict):
            return None, f"{label} must be a JSON object"
        return v, None
    return None, f"{label} must be an object or JSON string"


def _parse_specials_defs(specials_root: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in specials_root.items() if k != "order" and isinstance(v, dict)}


def _faction_capital_territory_id(raw: Any) -> str | None:
    """If set, capital must reference a real territory. None = no capital (e.g. neutral / meta factions)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Legacy JSON used the string "None" instead of null / empty (e.g. neutral faction).
    if s.lower() in ("none", "null", "n/a", "-"):
        return None
    return s


def validate_setup_documents(
    manifest: dict[str, Any],
    units: dict[str, Any],
    territories: dict[str, Any],
    factions: dict[str, Any],
    camps: dict[str, Any],
    ports: dict[str, Any],
    starting_setup: dict[str, Any],
    specials: dict[str, Any],
) -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []

    mid = manifest.get("id")
    if not isinstance(mid, str) or not mid.strip():
        errors.append('manifest.id must be a non-empty string')

    territory_ids = set(territories.keys())
    faction_ids = set(factions.keys())
    unit_ids = set(units.keys())

    for tid, t in territories.items():
        if not isinstance(t, dict):
            errors.append(f'territory "{tid}" must be an object')
            continue
        if t.get("id") != tid:
            errors.append(f'territory "{tid}" id field must match key')
        for label, key in (
            ("adjacent", "adjacent"),
            ("aerial_adjacent", "aerial_adjacent"),
            ("ford_adjacent", "ford_adjacent"),
        ):
            raw = t.get(key, [])
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                errors.append(f'territory "{tid}".{key} must be a list')
                continue
            for other in raw:
                if not isinstance(other, str):
                    errors.append(f'territory "{tid}".{key} must contain only strings')
                    break
                if other not in territory_ids:
                    errors.append(f'territory "{tid}".{key} references unknown territory "{other}"')

    # Enforced undirected symmetry per edge type
    for tid, t in territories.items():
        if not isinstance(t, dict):
            continue
        for key in ("adjacent", "aerial_adjacent", "ford_adjacent"):
            raw = t.get(key) or []
            if not isinstance(raw, list):
                continue
            neighbors = [x for x in raw if isinstance(x, str)]
            for other in neighbors:
                odef = territories.get(other)
                if not isinstance(odef, dict):
                    continue
                back = odef.get(key) or []
                if not isinstance(back, list):
                    errors.append(f'territory "{other}".{key} must be a list (for symmetry with "{tid}")')
                    continue
                if tid not in back:
                    errors.append(
                        f'territory graph asymmetry: "{tid}" lists "{other}" in {key}, but "{other}" does not list "{tid}"'
                    )

    for uid, u in units.items():
        if uid == "":
            errors.append(
                'units contains an entry with an empty id key; remove it under Units or Raw JSON → units.'
            )
            continue
        if not isinstance(u, dict):
            errors.append(f'unit "{uid}" must be an object')
            continue
        if u.get("id") != uid:
            errors.append(f'unit "{uid}" id field must match key')
        fac = u.get("faction")
        if not isinstance(fac, str) or not fac.strip() or fac not in faction_ids:
            errors.append(f'unit "{uid}" faction must be a known faction id')
        dt = u.get("downgrade_to")
        if isinstance(dt, str) and dt.strip() and dt not in unit_ids:
            errors.append(f'unit "{uid}" downgrade_to "{dt}" is not a known unit id')

    spec_defs = _parse_specials_defs(specials)
    spec_keys = set(spec_defs.keys())
    for uid, u in units.items():
        if uid == "":
            continue
        if not isinstance(u, dict):
            continue
        sp = u.get("specials") or []
        if not isinstance(sp, list):
            errors.append(f'unit "{uid}".specials must be a list')
            continue
        for s in sp:
            if not isinstance(s, str):
                errors.append(f'unit "{uid}".specials must contain strings')
                break
            if s and s not in spec_keys:
                errors.append(f'unit "{uid}" references unknown special "{s}"')

    for fid, f in factions.items():
        if not isinstance(f, dict):
            errors.append(f'faction "{fid}" must be an object')
            continue
        if f.get("id") != fid:
            errors.append(f'faction "{fid}" id field must match key')
        cap = _faction_capital_territory_id(f.get("capital"))
        if cap is not None and cap not in territory_ids:
            errors.append(f'faction "{fid}" capital "{cap}" is not a known territory')

    for cid, c in camps.items():
        if not isinstance(c, dict):
            errors.append(f'camp "{cid}" must be an object')
            continue
        tid = c.get("territory_id")
        if not isinstance(tid, str) or tid not in territory_ids:
            errors.append(f'camp "{cid}" territory_id must be a known territory')

    for pid, p in ports.items():
        if not isinstance(p, dict):
            errors.append(f'port "{pid}" must be an object')
            continue
        tid = p.get("territory_id")
        if not isinstance(tid, str) or tid not in territory_ids:
            errors.append(f'port "{pid}" territory_id must be a known territory')

    turn_order = starting_setup.get("turn_order")
    if not isinstance(turn_order, list):
        errors.append("starting_setup.turn_order must be a list")
    else:
        for f in turn_order:
            if not isinstance(f, str) or f not in faction_ids:
                errors.append(f'starting_setup.turn_order contains unknown faction "{f}"')

    owners = starting_setup.get("territory_owners")
    if owners is not None:
        if not isinstance(owners, dict):
            errors.append("starting_setup.territory_owners must be an object")
        else:
            for ter, fac in owners.items():
                if ter not in territory_ids:
                    errors.append(f'starting_setup.territory_owners: unknown territory "{ter}"')
                if not isinstance(fac, str) or fac not in faction_ids:
                    errors.append(f'starting_setup.territory_owners["{ter}"] must be a known faction')

    su = starting_setup.get("starting_units")
    if su is not None:
        if not isinstance(su, dict):
            errors.append("starting_setup.starting_units must be an object")
        else:
            for ter, stacks in su.items():
                if ter not in territory_ids:
                    errors.append(f'starting_setup.starting_units: unknown territory "{ter}"')
                if not isinstance(stacks, list):
                    errors.append(f'starting_setup.starting_units["{ter}"] must be a list')
                    continue
                for i, stack in enumerate(stacks):
                    if not isinstance(stack, dict):
                        errors.append(f'starting_setup.starting_units["{ter}"][{i}] must be an object')
                        continue
                    uk = stack.get("unit_id")
                    if not isinstance(uk, str) or uk not in unit_ids:
                        errors.append(
                            f'starting_setup.starting_units["{ter}"][{i}]: unknown unit_id "{uk}"'
                        )

    ctx = manifest.get("context")
    if manifest.get("is_active") is True:
        if not isinstance(ctx, dict) or not ctx:
            errors.append("manifest.context must be a non-empty object when is_active is true")

    return errors


def validate_setup_payload(payload: dict[str, Any]) -> list[str]:
    """Validate a dict with keys manifest, units, territories, factions, camps, ports, starting_setup, specials."""
    keys = ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup", "specials")
    for k in keys:
        if k not in payload:
            return [f'missing key "{k}"']
    m, e = _as_obj(payload["manifest"], "manifest")
    if e:
        return [e]
    u, e = _as_obj(payload["units"], "units")
    if e:
        return [e]
    t, e = _as_obj(payload["territories"], "territories")
    if e:
        return [e]
    f, e = _as_obj(payload["factions"], "factions")
    if e:
        return [e]
    c, e = _as_obj(payload["camps"], "camps")
    if e:
        return [e]
    p, e = _as_obj(payload["ports"], "ports")
    if e:
        return [e]
    s, e = _as_obj(payload["starting_setup"], "starting_setup")
    if e:
        return [e]
    sp, e = _as_obj(payload["specials"], "specials")
    if e:
        return [e]
    assert m is not None and u is not None and t is not None and f is not None
    assert c is not None and p is not None and s is not None and sp is not None
    return validate_setup_documents(m, u, t, f, c, p, s, sp)
