"""
Validate setup JSON blobs before persisting (territory symmetry, id references).
"""

from __future__ import annotations

import json
from typing import Any

from backend.engine.definitions import timeline_image_filename
from backend.engine.special_rules import parse_nonneg_int, parse_signed_int


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
    catalog: dict[str, Any] | None = None,
) -> list[str]:
    """Return a list of human-readable errors; empty means valid.

    With a catalog, unit specials, unit archetypes, and territory terrain types must come from it.
    """
    errors: list[str] = []
    known_specials = {e["id"] for e in catalog.get("specials") or []} if catalog else None
    known_terrain = set(catalog.get("terrain_types") or []) if catalog else None
    known_archetypes = set(catalog.get("archetypes") or []) if catalog else None

    mid = manifest.get("id")
    if not isinstance(mid, str) or not mid.strip():
        errors.append('manifest.id must be a non-empty string')

    territory_ids = set(territories.keys())
    faction_ids = set(factions.keys())
    unit_ids = set(units.keys())
    subfaction_ids: set[str] = set()
    subfaction_parents: dict[str, str] = {}

    for tid, t in territories.items():
        if not isinstance(t, dict):
            errors.append(f'territory "{tid}" must be an object')
            continue
        if t.get("id") != tid:
            errors.append(f'territory "{tid}" id field must match key')
        terrain = t.get("terrain_type")
        if known_terrain is not None and terrain not in known_terrain:
            errors.append(f'territory "{tid}" terrain_type "{terrain}" is not in the catalog terrain types')
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
        if not isinstance(fac, str) or not fac.strip():
            errors.append(f'unit "{uid}" faction must be a known faction id')
        for key in ("display_name", "archetype"):
            if key not in u:
                errors.append(f'unit "{uid}" needs {key}')
        archetype = u.get("archetype")
        if known_archetypes is not None and "archetype" in u and archetype not in known_archetypes:
            errors.append(f'unit "{uid}" archetype "{archetype}" is not in the catalog archetypes')
        for key in ("attack", "defense", "movement", "health"):
            if isinstance(u.get(key), bool) or not isinstance(u.get(key), int):
                errors.append(f'unit "{uid}" {key} must be a whole number')
        # Subfaction ids are collected below; a second pass checks the reference.
        dt = u.get("downgrade_to")
        if isinstance(dt, str) and dt.strip() and dt not in unit_ids:
            errors.append(f'unit "{uid}" downgrade_to "{dt}" is not a known unit id')
        hid = u.get("hero_id")
        if hid is not None and hid != "":
            if not isinstance(hid, str) or not hid.strip():
                errors.append(f'unit "{uid}" hero_id must be a non-empty string when set')

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
            if s and known_specials is not None and s not in known_specials:
                errors.append(f'unit "{uid}" references special "{s}", which is not in the catalog')

    for fid, f in factions.items():
        if not isinstance(f, dict):
            errors.append(f'faction "{fid}" must be an object')
            continue
        if f.get("id") != fid:
            errors.append(f'faction "{fid}" id field must match key')
        cap = _faction_capital_territory_id(f.get("capital"))
        if cap is not None and cap not in territory_ids:
            errors.append(f'faction "{fid}" capital "{cap}" is not a known territory')
        subs = f.get("subfactions")
        if subs is None:
            continue
        if not isinstance(subs, list):
            errors.append(f'faction "{fid}" subfactions must be a list')
            continue
        for i, sub in enumerate(subs):
            prefix = f'faction "{fid}" subfactions[{i}]'
            if not isinstance(sub, dict):
                errors.append(f"{prefix} must be an object")
                continue
            sid = sub.get("id")
            if not isinstance(sid, str) or not sid.strip():
                errors.append(f"{prefix}.id must be a non-empty string")
                continue
            sid = sid.strip()
            if sid in faction_ids:
                errors.append(f'{prefix}.id "{sid}" collides with a faction id')
            elif sid in subfaction_ids:
                errors.append(f'{prefix}.id "{sid}" is already used by another subfaction')
            else:
                subfaction_ids.add(sid)
                subfaction_parents[sid] = fid
            name = sub.get("display_name")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{prefix}.display_name must be a non-empty string")
            color = sub.get("color")
            if not isinstance(color, str) or not color.strip():
                errors.append(f"{prefix}.color must be a non-empty string")
            icon = sub.get("icon")
            if icon is not None and not isinstance(icon, str):
                errors.append(f"{prefix}.icon must be a string when set")

    owner_ids = faction_ids | subfaction_ids
    for uid, u in units.items():
        if uid == "" or not isinstance(u, dict):
            continue
        fac = u.get("faction")
        if isinstance(fac, str) and fac.strip() and fac not in owner_ids:
            errors.append(f'unit "{uid}" faction must be a known faction id')

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
                if isinstance(f, str) and f in subfaction_ids:
                    errors.append(
                        f'starting_setup.turn_order contains subfaction "{f}" '
                        f'(controlled by "{subfaction_parents.get(f, "")}")'
                    )
                else:
                    errors.append(f'starting_setup.turn_order contains unknown faction "{f}"')

    owners = starting_setup.get("territory_owners")
    if owners is not None:
        if not isinstance(owners, dict):
            errors.append("starting_setup.territory_owners must be an object")
        else:
            for ter, fac in owners.items():
                if ter not in territory_ids:
                    errors.append(f'starting_setup.territory_owners: unknown territory "{ter}"')
                if not isinstance(fac, str) or fac not in owner_ids:
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

    rules = manifest.get("subfaction_rules")
    if rules is not None:
        if not isinstance(rules, dict):
            errors.append("manifest.subfaction_rules must be an object")
        else:
            for sid, rule in rules.items():
                if sid not in subfaction_ids:
                    errors.append(f'manifest.subfaction_rules references unknown subfaction "{sid}"')
                elif not isinstance(rule, dict):
                    errors.append(f'manifest.subfaction_rules["{sid}"] must be an object')
                else:
                    from backend.engine.subfaction_rules import subfaction_rule_errors
                    errors.extend(subfaction_rule_errors(sid, rule, units))

    ctx = manifest.get("context")
    if manifest.get("is_active") is True:
        if not isinstance(ctx, dict) or not ctx:
            errors.append("manifest.context must be a non-empty object when is_active is true")

    mo = manifest.get("menu_order")
    if mo is not None:
        try:
            int(mo)
        except (TypeError, ValueError):
            errors.append("manifest.menu_order must be an integer when set")

    if "timeline_image" in manifest and manifest.get("timeline_image") not in (None, ""):
        if timeline_image_filename(manifest) is None:
            errors.append(
                "manifest.timeline_image must be an image filename (png, jpg, jpeg, webp, or gif) with no directory"
            )

    starting_message = manifest.get("starting_message")
    if starting_message is not None and not isinstance(starting_message, str):
        errors.append("manifest.starting_message must be a string when set")

    special_rules = manifest.get("special_rules")
    if special_rules is not None:
        if not isinstance(special_rules, list):
            errors.append("manifest.special_rules must be a list when set")
        else:
            seen_evolving: set[str] = set()
            for i, rule in enumerate(special_rules):
                if not isinstance(rule, dict):
                    errors.append(f"manifest.special_rules[{i}] must be an object")
                    continue
                typ = rule.get("type")
                if not isinstance(typ, str) or not typ.strip():
                    errors.append(f'manifest.special_rules[{i}].type must be a non-empty string')
                    continue
                _validate_special_rule_option(rule, i, errors)
                if typ == "rings_of_power":
                    _validate_rings_of_power(rule, i, territory_ids, errors)
                    continue
                if typ == "fading_territory":
                    errors.append(
                        f'manifest.special_rules[{i}].type fading_territory is now evolving_territory'
                    )
                    continue
                if typ != "evolving_territory":
                    continue
                rows = rule.get("territories")
                if not isinstance(rows, list) or not rows:
                    errors.append(
                        f'manifest.special_rules[{i}].territories must be a non-empty list'
                    )
                    continue
                for j, row in enumerate(rows):
                    prefix = f"manifest.special_rules[{i}].territories[{j}]"
                    if not isinstance(row, dict):
                        errors.append(f"{prefix} must be an object")
                        continue
                    tid = row.get("territory_id")
                    known = isinstance(tid, str) and tid in territory_ids
                    if not known:
                        errors.append(f'{prefix}.territory_id must be a known territory')
                    elif tid in seen_evolving:
                        errors.append(f'{prefix} duplicates evolving territory "{tid}"')
                    else:
                        seen_evolving.add(tid)
                    step = parse_signed_int(row.get("step"))
                    if step is None or step == 0:
                        errors.append(f"{prefix}.step must be a non-zero integer")
                        step = None
                    stop = parse_nonneg_int(row.get("stop_at"))
                    if stop is None:
                        errors.append(f"{prefix}.stop_at must be an integer >= 0")
                    elif known and step is not None:
                        printed = _printed_territory_power(territories.get(tid))
                        if step > 0 and stop <= printed:
                            errors.append(
                                f"{prefix}.stop_at must be above printed power {printed} when step is positive"
                            )
                        elif step < 0 and stop >= printed:
                            errors.append(
                                f"{prefix}.stop_at must be below printed power {printed} when step is negative"
                            )

    return errors


def _validate_special_rule_option(rule: dict[str, Any], index: int, errors: list[str]) -> None:
    prefix = f"manifest.special_rules[{index}]"
    if "is_optional" in rule and not isinstance(rule.get("is_optional"), bool):
        errors.append(f"{prefix}.is_optional must be a boolean")
    name = rule.get("name")
    if rule.get("is_optional") is True:
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name must be a non-empty string when is_optional is true")
    elif "name" in rule and name not in (None, ""):
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name must be a non-empty string when set")
    if "description" in rule and not isinstance(rule.get("description"), (str, type(None))):
        errors.append(f"{prefix}.description must be a string when set")


def _printed_territory_power(territory: Any) -> int:
    if not isinstance(territory, dict):
        return 0
    produces = territory.get("produces")
    if not isinstance(produces, dict):
        return 0
    power = parse_nonneg_int(produces.get("power"))
    return power if power is not None else 0


def _validate_rings_of_power(
    rule: dict[str, Any],
    index: int,
    territory_ids: set[str],
    errors: list[str],
) -> None:
    rows = rule.get("rings")
    prefix = f"manifest.special_rules[{index}]"
    if not isinstance(rows, list) or not rows:
        errors.append(f"{prefix}.rings must be a non-empty list")
        return
    seen: set[str] = set()
    for j, row in enumerate(rows):
        row_prefix = f"{prefix}.rings[{j}]"
        if not isinstance(row, dict):
            errors.append(f"{row_prefix} must be an object")
            continue
        rid = row.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"{row_prefix}.id must be a non-empty string")
        elif rid.strip() in seen:
            errors.append(f'{row_prefix} duplicates ring id "{rid.strip()}"')
        else:
            seen.add(rid.strip())
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{row_prefix}.name must be a non-empty string")
        tid = row.get("territory_id")
        if not isinstance(tid, str) or tid not in territory_ids:
            errors.append(f"{row_prefix}.territory_id must be a known territory")
        power = row.get("power")
        if isinstance(power, bool) or not isinstance(power, int) or power < 0:
            errors.append(f"{row_prefix}.power must be an integer >= 0")
        for key in ("bearer_hero_id", "returns_to"):
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            if not isinstance(raw, str) or not raw.strip():
                errors.append(f"{row_prefix}.{key} must be a non-empty string when set")
        home = row.get("returns_to")
        if isinstance(home, str) and home.strip() and home.strip() not in territory_ids:
            errors.append(f"{row_prefix}.returns_to must be a known territory")
        for key in ("attack_boost", "defense_boost", "rolls_boost", "hp_boost", "moves_boost"):
            raw = row.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                errors.append(f"{row_prefix}.{key} must be an integer >= 0")


def validate_setup_payload(payload: dict[str, Any], catalog: dict[str, Any] | None = None) -> list[str]:
    """Validate a dict with keys manifest, units, territories, factions, camps, ports, starting_setup.

    A leftover specials key from an older export is ignored; the catalog holds specials.
    """
    keys = ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup")
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
    assert m is not None and u is not None and t is not None and f is not None
    assert c is not None and p is not None and s is not None
    return validate_setup_documents(m, u, t, f, c, p, s, catalog)
