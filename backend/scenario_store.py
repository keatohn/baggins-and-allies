"""Admin authoring state; gameplay continues to consume compiled Setup rows.

An AppSetting keeps this additive: no existing setup columns or games are migrated.
First use derives defaults from *database* setups, never the historical file catalog.
"""
from __future__ import annotations

from copy import deepcopy
import json

from sqlalchemy.orm import Session

from backend.api.models import AppSetting, Setup
from backend.scenario_builder import (
    DELETE, MAP_TERRITORY_FIELDS, compile_scenario, delta, extract_library, extract_source,
    overlay_overrides as _overlay, prepare_territory_seeds, resolve_source,
    scenario_map_id, upgrade_territory_sources,
)
from backend.setup_validation import validate_setup_payload

SETTING = "scenario_authoring_v1"


def _bundles(db: Session) -> dict[str, dict]:
    from backend.setup_data import get_admin_setup_bundle
    return {row.id: {key: value for key, value in get_admin_setup_bundle(db, row.id).items() if key != "id"}
            for row in db.query(Setup).order_by(Setup.id).all()}


def load_state(db: Session) -> dict:
    row = db.query(AppSetting).filter(AppSetting.key == SETTING).first()
    bundles = _bundles(db)
    if row:
        state = json.loads(row.value_json)
    else:
        state = {"library": extract_library(bundles), "sources": {}}
    library = state["library"]
    # Terrain/topology remain owned by the existing map editor and runtime loader.
    # Reflect edits made there in authoring defaults, including DB-only map assets.
    from backend.map_overrides import apply_map_graph, apply_map_terrain
    for map_id, records in library["maps"].items():
        previous = deepcopy(records)
        apply_map_terrain(records, map_id, db)
        apply_map_graph(records, map_id, db)
        for source in state["sources"].values():
            if source.get("map") != map_id:
                continue
            for tid, patch in source.get("territory_overrides", {}).items():
                for field in ("terrain_type", "adjacent", "aerial_adjacent", "ford_adjacent"):
                    if previous.get(tid, {}).get(field) != records.get(tid, {}).get(field):
                        patch.pop(field, None)
    # New/imported legacy bundles acquire a source without modifying their contents.
    for setup_id, bundle in bundles.items():
        if setup_id not in state["sources"]:
            map_id = scenario_map_id(bundle["manifest"], setup_id)
            library["maps"].setdefault(map_id, {}).update({
                key: value for key, value in bundle["territories"].items() if key not in library["maps"].get(map_id, {})
            })
            source = extract_source(bundle, library)
            if compile_scenario(source, library) != bundle:
                raise ValueError(f"Cannot extract {setup_id} without changing its compiled bundle")
            state["sources"][setup_id] = source
    state["sources"] = {key: value for key, value in state["sources"].items() if key in bundles}
    # Lift legacy per-map metadata into ID-based seeds without changing any setup.
    # Differences become explicit scenario overrides; one-level extends is retained.
    upgrade_territory_sources(state)
    return state


def stage_state(db: Session, state: dict) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == SETTING).first()
    value = json.dumps(state, ensure_ascii=False)
    if row:
        row.value_json = value
    else:
        db.add(AppSetting(key=SETTING, value_json=value))


def initialize(db: Session) -> None:
    """Freeze the first library from current DB data, without rewriting any setup."""
    stage_state(db, load_state(db))
    db.commit()


def source_for_bundle(source: dict, bundle: dict, library: dict, lookup=None) -> dict:
    """Reconcile legacy form edits while preserving explicit overrides (even equal defaults).

    Editing a materialized bundle flattens extends, so future parent edits cannot undo it.
    The dedicated source editor preserves extends instead.
    """
    source = resolve_source(source, lookup)
    previous = compile_scenario(source, library)
    map_id = scenario_map_id(bundle["manifest"], bundle["manifest"]["id"])
    if map_id != source["map"]:
        return extract_source(bundle, library)
    for section in ("units", "factions"):
        for key in list(source.get(section, {})):
            if key not in bundle[section]:
                del source[section][key]
        for key, record in bundle[section].items():
            if key not in previous[section]:
                source.setdefault(section, {})[key] = {
                    **({"seed": key} if key in library[section] else {}),
                    "overrides": delta(library[section].get(key, {}), record),
                }
            elif record != previous[section][key]:
                entry = source[section][key]
                entry["overrides"] = _overlay(entry.get("overrides", {}), delta(previous[section][key], record))
    if "territory_ids" in source or bundle["territories"].keys() != library["maps"].get(map_id, {}).keys():
        source["territory_ids"] = list(bundle["territories"])
    overrides = source.setdefault("territory_overrides", {})
    for key in list(overrides):
        if key not in bundle["territories"]:
            del overrides[key]
    for key, record in bundle["territories"].items():
        if key not in previous["territories"]:
            overrides[key] = delta(library["maps"].get(map_id, {}).get(key, {}), record)
        elif record != previous["territories"][key]:
            overrides[key] = _overlay(overrides.get(key, {}), delta(previous["territories"][key], record))
    for key in ("manifest", "camps", "ports", "starting_setup"):
        source[key] = deepcopy(bundle[key])
    if "map_asset" not in source["manifest"]:
        source["manifest"]["map_asset"] = deepcopy(DELETE)
    return source


def stage_bundle(db: Session, setup_id: str, bundle: dict) -> None:
    from datetime import datetime, timezone
    from backend.scenario_builder import BUNDLE_KEYS
    row = db.query(Setup).filter(Setup.id == setup_id).one()
    for key in BUNDLE_KEYS:
        setattr(row, f"{key}_json", json.dumps(bundle[key], ensure_ascii=False))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def compile_all(state: dict) -> dict[str, dict]:
    return {key: compile_scenario(source, state["library"], state["sources"].get)
            for key, source in state["sources"].items()}


def map_authority_errors(db: Session, bundle: dict) -> list[str]:
    from backend.map_overrides import apply_map_graph, apply_map_terrain
    effective = deepcopy(bundle["territories"])
    map_id = scenario_map_id(bundle["manifest"], bundle["manifest"].get("id", ""))
    apply_map_terrain(effective, map_id, db)
    apply_map_graph(effective, map_id, db)
    return ["Edit map terrain and connections in Maps → Landscape / Graph"] if effective != bundle["territories"] else []


def update_library(db: Session, library: dict, *, apply: bool = False) -> dict:
    from backend.catalog import load_catalog
    for section in ("units", "factions", "maps"):
        if not isinstance(library.get(section), dict):
            raise ValueError(f"library.{section} must be an object")
        for key, record in library[section].items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise ValueError(f"library.{section} entries must be objects with string ids")
    state = load_state(db)
    if not isinstance(library.get("territories"), dict):
        raise ValueError("library.territories must be an object")
    for tid, record in library["territories"].items():
        if not isinstance(record, dict) or record.get("id") != tid:
            raise ValueError(f"Territory seed {tid!r} id must match its key")
        if record.keys() & MAP_TERRITORY_FIELDS:
            raise ValueError("Territory seeds hold metadata; edit terrain and connections in Maps")
    for records in library["maps"].values():
        missing = records.keys() - library["territories"].keys()
        if missing:
            raise ValueError("Territory seeds still used by maps: " + ", ".join(sorted(missing)))
    prepare_territory_seeds(library)
    for map_id, records in library["maps"].items():
        from backend.map_overrides import validate_map_base
        validate_map_base(map_id)
        for tid, record in records.items():
            if not isinstance(tid, str) or not isinstance(record, dict):
                raise ValueError("Map territory defaults must be records keyed by territory id")
            old = state["library"]["maps"].get(map_id, {}).get(tid)
            if old and any(old.get(field) != record.get(field) for field in ("terrain_type", "adjacent", "aerial_adjacent", "ford_adjacent")):
                raise ValueError("Edit map terrain and connections in Maps → Landscape / Graph")
    def library_errors(value):
        territories = {}
        for records in value["maps"].values():
            territories.update(records)
        # Seed references are checked globally; scenario-specific faction remaps,
        # capitals and home territories are still validated on compiled scenarios.
        return set(validate_setup_payload({"manifest": {"id": "seed_catalog"}, "units": value["units"],
            "factions": value["factions"], "territories": territories, "camps": {}, "ports": {},
            "starting_setup": {"turn_order": [], "territory_owners": {}, "starting_units": {}}}, load_catalog(db)))
    new_library_errors = library_errors(library) - library_errors(state["library"])
    if new_library_errors:
        raise ValueError("Invalid defaults: " + "; ".join(sorted(new_library_errors)))
    before = _bundles(db)
    state["library"] = library
    after = compile_all(state)
    catalog = load_catalog(db)
    changed = []
    for setup_id, bundle in after.items():
        if before[setup_id] == bundle:
            continue
        # Historical validation failures do not authorize any new failure.
        new_errors = set(validate_setup_payload(bundle, catalog)) - set(validate_setup_payload(before[setup_id], catalog))
        if new_errors:
            raise ValueError(f"{setup_id}: " + "; ".join(sorted(new_errors)))
        changed.append(setup_id)
    if apply:
        for setup_id in changed:
            stage_bundle(db, setup_id, after[setup_id])
        stage_state(db, state)
        db.commit()
    return {"changed_setups": changed, "changes": {
        key: delta(before[key], after[key]) for key in changed
    }}


def save_source(db: Session, setup_id: str, source: dict) -> dict:
    from backend.catalog import load_catalog
    state = load_state(db)
    if setup_id not in state["sources"]:
        raise ValueError("Setup not found")
    state["sources"][setup_id] = source
    bundles = compile_all(state)
    target = bundles[setup_id]
    if target["manifest"].get("id") != setup_id:
        raise ValueError("manifest.id must match the setup id")
    errors = validate_setup_payload(target, load_catalog(db)) + map_authority_errors(db, target)
    if errors:
        raise ValueError("; ".join(errors))
    before = _bundles(db)
    for key, bundle in bundles.items():
        if key == setup_id or bundle != before[key]:
            new_errors = set(validate_setup_payload(bundle, load_catalog(db))) - set(validate_setup_payload(before[key], load_catalog(db)))
            if new_errors:
                raise ValueError(f"{key}: " + "; ".join(sorted(new_errors)))
            stage_bundle(db, key, bundle)
    stage_state(db, state)
    db.commit()
    return target
