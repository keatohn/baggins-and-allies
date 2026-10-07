"""
Global catalog shared by every setup: specials, special rule types, game options,
territory terrain types, and unit archetypes.

Names, display codes, and descriptions live here, not in setups. A setup's units
name the specials they have, its manifest names the special rules it applies (with
their data, such as the ring list), and its territories and units must use terrain
types and archetypes from this list.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR, SETUPS_DIR

CATALOG_PATH = DATA_DIR / "catalog.json"
CATALOG_SETTING_KEY = "catalog"
SPECIAL_RULE_IDS = ("rings_of_power", "evolving_territory")
GAME_OPTION_IDS = ("shadow_of_war",)
REQUIRED_TERRAIN_TYPES = ("sea",)
ENTRY_SECTIONS = ("specials", "special_rules", "game_options")
LIST_SECTIONS = ("terrain_types", "archetypes")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

_SECTION_LABELS = {
    "specials": "special",
    "special_rules": "special rule",
    "game_options": "game option",
    "terrain_types": "terrain type",
    "archetypes": "archetype",
}


class CatalogError(ValueError):
    """Admin catalog failed validation."""


def _is_files_source() -> bool:
    from backend.setup_data import is_files_setup_source

    return is_files_setup_source()


def _readable(entry_id: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in entry_id.split("_") if part)


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _entries(raw: Any, *, with_code: bool) -> list[dict[str, str]]:
    """Ordered entries. Also reads the old specials.json shape ({"order": [...], id: {...}})."""
    if isinstance(raw, dict):
        order = [k for k in raw.get("order") or [] if isinstance(k, str)]
        keys = [k for k in order if isinstance(raw.get(k), dict)]
        keys += sorted(k for k, v in raw.items() if k != "order" and isinstance(v, dict) and k not in keys)
        raw = [{"id": k, **raw[k]} for k in keys]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry_id = _text(item.get("id"))
        if not _ID_RE.match(entry_id) or entry_id in seen:
            continue
        seen.add(entry_id)
        entry = {
            "id": entry_id,
            "name": _text(item.get("name")) or _readable(entry_id),
            "description": _text(item.get("description")),
        }
        if with_code:
            entry["display_code"] = _text(item.get("display_code"))
        out.append(entry)
    return out


def _ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        value = _text(item).lower()
        if _ID_RE.match(value) and value not in out:
            out.append(value)
    return out


def _with_required(entries: list[dict[str, str]], required: Iterable[str]) -> list[dict[str, str]]:
    have = {e["id"] for e in entries}
    return entries + [{"id": rid, "name": _readable(rid), "description": ""} for rid in required if rid not in have]


def normalize_catalog(raw: Any) -> dict[str, Any]:
    """Lenient read: drops malformed entries and fills in the rule types and options the engine knows."""
    data = raw if isinstance(raw, dict) else {}
    terrain = _ids(data.get("terrain_types"))
    terrain += [t for t in REQUIRED_TERRAIN_TYPES if t not in terrain]
    return {
        "specials": _entries(data.get("specials"), with_code=True),
        "special_rules": _with_required(_entries(data.get("special_rules"), with_code=False), SPECIAL_RULE_IDS),
        "game_options": _with_required(_entries(data.get("game_options"), with_code=False), GAME_OPTION_IDS),
        "terrain_types": terrain,
        "archetypes": _ids(data.get("archetypes")),
    }


def catalog_errors(raw: Any) -> list[str]:
    """Strict check for an admin save. Empty means the catalog can be stored as sent."""
    if not isinstance(raw, dict):
        return ["Catalog must be an object."]
    errors: list[str] = []
    for section in ENTRY_SECTIONS:
        items = raw.get(section)
        if not isinstance(items, list):
            errors.append(f"{section} must be a list.")
            continue
        seen: set[str] = set()
        for index, item in enumerate(items):
            label = f"{section}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object.")
                continue
            entry_id = _text(item.get("id"))
            if not _ID_RE.match(entry_id):
                errors.append(f"{label}.id must be lowercase letters, digits, and underscores, starting with a letter.")
            elif entry_id in seen:
                errors.append(f'{label}.id "{entry_id}" is listed twice.')
            seen.add(entry_id)
            if not _text(item.get("name")):
                errors.append(f"{label}.name must not be empty.")
            for key in ("description", "display_code"):
                if key in item and not isinstance(item[key], (str, type(None))):
                    errors.append(f"{label}.{key} must be text.")
    for section, required in (("special_rules", SPECIAL_RULE_IDS), ("game_options", GAME_OPTION_IDS)):
        listed = {_text(i.get("id")) for i in raw.get(section) or [] if isinstance(i, dict)}
        for rid in required:
            if rid not in listed:
                errors.append(f'{section} must include "{rid}".')
    for section in LIST_SECTIONS:
        items = raw.get(section)
        if not isinstance(items, list):
            errors.append(f"{section} must be a list.")
            continue
        seen = set()
        for index, item in enumerate(items):
            value = _text(item).lower()
            if not _ID_RE.match(value):
                errors.append(f"{section}[{index}] must be lowercase letters, digits, and underscores, starting with a letter.")
            elif value in seen:
                errors.append(f'{section} lists "{value}" twice.')
            seen.add(value)
    terrain = {_text(t).lower() for t in raw.get("terrain_types") or [] if isinstance(t, str)}
    for required in REQUIRED_TERRAIN_TYPES:
        if required not in terrain:
            errors.append(f'terrain_types must include "{required}".')
    return errors


def _read_file() -> Any:
    if not CATALOG_PATH.is_file():
        return None
    try:
        with open(CATALOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_file(catalog: dict[str, Any]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_setting(db: Session) -> Any:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == CATALOG_SETTING_KEY).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, catalog: dict[str, Any]) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps(catalog, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == CATALOG_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=CATALOG_SETTING_KEY, value_json=payload))


def load_catalog(db: Session | None) -> dict[str, Any]:
    if _is_files_source() or db is None:
        return normalize_catalog(_read_file())
    data = _read_setting(db)
    if data is None:
        return normalize_catalog(_read_file())
    return normalize_catalog(data)


def _setup_documents(db: Session | None) -> Iterable[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """(setup id, units, territories, legacy specials) for every stored setup."""
    if _is_files_source() or db is None:
        if not SETUPS_DIR.is_dir():
            return
        for folder in sorted(SETUPS_DIR.iterdir()):
            docs = []
            for name in ("units.json", "territories.json"):
                try:
                    with open(folder / name, encoding="utf-8") as f:
                        loaded = json.load(f)
                except (OSError, json.JSONDecodeError):
                    loaded = {}
                docs.append(loaded if isinstance(loaded, dict) else {})
            if docs[0] or docs[1]:
                yield folder.name, docs[0], docs[1], {}
        return
    from backend.api.models import Setup

    for row in db.query(Setup).all():
        docs = []
        for raw in (row.units_json, row.territories_json, row.specials_json):
            try:
                loaded = json.loads(raw or "{}")
            except json.JSONDecodeError:
                loaded = {}
            docs.append(loaded if isinstance(loaded, dict) else {})
        yield row.id, docs[0], docs[1], docs[2]


def _usage(units: dict[str, Any], territories: dict[str, Any]) -> dict[str, set[str]]:
    used: dict[str, set[str]] = {"specials": set(), "terrain_types": set(), "archetypes": set()}
    for unit in units.values():
        if not isinstance(unit, dict):
            continue
        for special in unit.get("specials") or []:
            if isinstance(special, str) and special:
                used["specials"].add(special)
        archetype = unit.get("archetype")
        if isinstance(archetype, str) and archetype:
            used["archetypes"].add(archetype)
    for territory in territories.values():
        if isinstance(territory, dict) and isinstance(territory.get("terrain_type"), str) and territory["terrain_type"]:
            used["terrain_types"].add(territory["terrain_type"])
    return used


def save_catalog(db: Session | None, raw: Any) -> dict[str, Any]:
    """Replace the catalog. Removing an entry a setup still uses is an error."""
    errors = catalog_errors(raw)
    if errors:
        raise CatalogError("; ".join(errors[:12]))
    catalog = normalize_catalog(raw)
    available = {
        "specials": {e["id"] for e in catalog["specials"]},
        "terrain_types": set(catalog["terrain_types"]),
        "archetypes": set(catalog["archetypes"]),
    }
    in_use: list[str] = []
    for setup_id, units, territories, _legacy in _setup_documents(db):
        for section, used in _usage(units, territories).items():
            for value in sorted(used - available[section]):
                in_use.append(f'{_SECTION_LABELS[section]} "{value}" is still used by setup {setup_id}')
    if in_use:
        raise CatalogError("; ".join(in_use[:12]))
    if _is_files_source() or db is None:
        _write_file(catalog)
        return catalog
    _write_setting(db, catalog)
    db.commit()
    return catalog


def seed_catalog_if_empty(db: Session) -> None:
    """First run on a database: start from the file catalog plus anything stored setups already use."""
    if _is_files_source():
        return
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == CATALOG_SETTING_KEY).first():
        return
    catalog = normalize_catalog(_read_file())
    known = {e["id"] for e in catalog["specials"]}
    for _setup_id, units, territories, legacy in _setup_documents(db):
        for entry in _entries(legacy, with_code=True):
            if entry["id"] not in known:
                catalog["specials"].append(entry)
                known.add(entry["id"])
        used = _usage(units, territories)
        for special in sorted(used["specials"] - known):
            if _ID_RE.match(special):
                catalog["specials"].append({"id": special, "name": _readable(special), "description": "", "display_code": ""})
                known.add(special)
        for section in LIST_SECTIONS:
            for value in sorted(used[section]):
                if _ID_RE.match(value) and value not in catalog[section]:
                    catalog[section].append(value)
    _write_setting(db, catalog)
    db.commit()


def specials_for_units(catalog: dict[str, Any], units: Iterable[Any]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Catalog specials that at least one of these units has, as (id -> name/description/display_code, order)."""
    used: set[str] = set()
    for unit in units:
        raw = unit.get("specials") if isinstance(unit, dict) else getattr(unit, "specials", None)
        for special in raw or []:
            if isinstance(special, str):
                used.add(special)
    definitions: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for entry in catalog.get("specials") or []:
        if entry["id"] not in used:
            continue
        definitions[entry["id"]] = {
            "name": entry["name"],
            "description": entry.get("description", ""),
            "display_code": entry.get("display_code", ""),
        }
        order.append(entry["id"])
    return definitions, order


def rule_descriptions(catalog: dict[str, Any]) -> dict[str, str]:
    """Rule type or game option id -> description, for any that have one."""
    out: dict[str, str] = {}
    for section in ("special_rules", "game_options"):
        for entry in catalog.get(section) or []:
            if entry.get("description"):
                out[entry["id"]] = entry["description"]
    return out
