"""Canonical compact setup JSON plus generated gameplay snapshots.

The library and setup definitions are authoritative. Setup rows contain generated
runtime data only; editing never reconciles two competing documents.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from backend.api.models import AppSetting, Setup
from backend.scenario_builder import (
    BUNDLE_KEYS, MAP_TERRITORY_FIELDS, canonical_setup, compile_setup, delta,
    extract_library, load_library, migrate_legacy_setups, prepare_territory_seeds,
    read_setup_bundle, scenario_map_id, setup_from_bundle, write_library, write_setup, migrate_seed_terrain,
)
from backend.setup_validation import validate_setup_payload

SETTING = "scenario_authoring_v1"

def _include_map_seed_choices(db: Session, state: dict) -> None:
    """Offer matching SVG regions without expanding existing scenario selections."""
    from backend.map_assets import load_map_svg
    from backend.map_overrides import apply_map_graph
    library = state["library"]
    label_key = "{http://www.inkscape.org/namespaces/inkscape}label"
    for map_id, records in library["maps"].items():
        svg = load_map_svg(db, map_id)
        if not svg:
            path = Path(__file__).resolve().parents[1] / "frontend" / "public" / f"{map_id}.svg"
            if path.is_file():
                svg = path.read_text(encoding="utf-8")
        if not svg:
            continue
        try:
            root = ET.fromstring(svg)
        except ET.ParseError:
            continue
        region_root = next((el for el in root.iter() if el.tag.rsplit('}', 1)[-1] == 'g' and el.get(label_key) == 'svg_borders'), root)
        matches = set()
        for el in region_root.iter():
            if el.tag.rsplit('}', 1)[-1] != 'path' or not el.get('d'):
                continue
            tid = (el.get('id') or '').strip()
            if not tid or re.fullmatch(r'path\d+', tid, re.I):
                tid = re.sub(r'\s+', '_', el.get(label_key, '').lower()).replace("'", '').replace('-', '_')
            matches.add(tid.lower())
        additions = (matches & library.get("territories", {}).keys()) - records.keys()
        if not additions:
            continue
        from backend.catalog import load_catalog
        terrain_types = load_catalog(db)["terrain_types"]
        default_terrain = "plains" if "plains" in terrain_types else next((t for t in terrain_types if t != "sea"), "sea")
        for tid in sorted(additions):
            records[tid] = {"terrain_type": default_terrain, **deepcopy(library["territories"][tid]),
                            "adjacent": [], "aerial_adjacent": [], "ford_adjacent": []}
        apply_map_graph(records, map_id, db)


def _bundles(db: Session) -> dict[str, dict]:
    from backend.setup_data import is_files_setup_source
    from backend.engine.definitions import SETUPS_DIR
    if is_files_setup_source():
        return {folder.name: read_setup_bundle(folder) for folder in sorted(SETUPS_DIR.iterdir()) if (folder / "setup.json").is_file()}
    return {row.id: {section: json.loads(getattr(row, f"{section}_json")) for section in BUNDLE_KEYS}
            for row in db.query(Setup).order_by(Setup.id).all()}


def _migrate_saved_map_seeds(db: Session, state: dict) -> None:
    """Preserve legacy saved drawings that have never been used by a setup."""
    from backend.api.models import MapAsset
    from backend.map_builder import MapBuilderError, convert_map_builder
    from backend.map_overrides import load_map_overrides
    from backend.setup_data import is_files_setup_source
    from backend.engine.definitions import DATA_DIR
    library = state["library"]
    drawings = {}
    for path in (DATA_DIR / "maps").glob("*/builder.json"):
        try:
            drawings[path.parent.name] = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
    if not is_files_setup_source():
        for asset in db.query(MapAsset).all():
            try:
                doc = json.loads(asset.builder_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("labels", {}), dict):
                continue
            fallback = drawings.get(asset.map_base, {}).get("labels", {})
            for tid, label in doc.get("labels", {}).items():
                if isinstance(label, dict) and "display_name" not in label and fallback.get(tid, {}).get("display_name"):
                    label["display_name"] = fallback[tid]["display_name"]
            drawings[asset.map_base] = doc
    for map_id, doc in drawings.items():
        if not isinstance(doc, dict) or not isinstance(doc.get("labels", {}), dict):
            continue
        if map_id in library["maps"] and all(tid in library["territories"] for tid in doc.get("labels", {})):
            continue
        try:
            converted = convert_map_builder(doc)
        except MapBuilderError:
            # An invalid saved drawing can still be opened/repaired independently.
            continue
        legacy_terrain = load_map_overrides(db, map_id).get("terrain", {})
        records = library["maps"].setdefault(map_id, {})
        for territory in converted["territories"]:
            tid = territory["id"]
            records.setdefault(tid, {"id": tid, "display_name": territory["label"], "terrain_type": legacy_terrain.get(tid, "plains"),
                "produces": {"power": 0}, "ownable": legacy_terrain.get(tid) != "sea",
                **{field: converted["graph"].get(field, {}).get(tid, []) for field in MAP_TERRITORY_FIELDS}})


def load_state(db: Session) -> dict:
    from backend.setup_data import is_files_setup_source
    from backend.engine.definitions import DATA_DIR, SETUPS_DIR
    from backend.map_overrides import apply_map_graph, apply_map_terrain
    if is_files_setup_source():
        state = {"library": load_library(DATA_DIR, prepare=False), "setups": {
            folder.name: canonical_setup(json.loads((folder / "setup.json").read_text()))
            for folder in sorted(SETUPS_DIR.iterdir()) if (folder / "setup.json").is_file()}}
    else:
        row = db.query(AppSetting).filter(AppSetting.key == SETTING).first()
        bundles = _bundles(db)
        state = migrate_legacy_setups(json.loads(row.value_json)) if row else {"library": extract_library(bundles), "setups": {}}
        for key, bundle in bundles.items():
            if key in state["setups"]:
                continue
            map_id = scenario_map_id(bundle["manifest"], key)
            state["library"]["maps"].setdefault(map_id, {}).update({
                tid: value for tid, value in bundle["territories"].items() if tid not in state["library"]["maps"].get(map_id, {})})
            state["setups"][key] = setup_from_bundle(bundle, state["library"])
        state["setups"] = {key: value for key, value in state["setups"].items() if key in bundles}
    _migrate_saved_map_seeds(db, state)
    # Preserve the last effective map terrain once when migrating old seeds.
    missing = {tid for tid, seed in state["library"].get("territories", {}).items() if "terrain_type" not in seed}
    for map_id, records in state["library"]["maps"].items():
        legacy = {tid: deepcopy(record) for tid, record in records.items() if tid in missing}
        if legacy:
            apply_map_terrain(legacy, map_id, db)
            for tid, record in legacy.items():
                records[tid]["terrain_type"] = record.get("terrain_type", "plains")
    migrate_seed_terrain(state)
    for map_id, records in state["library"]["maps"].items():
        apply_map_graph(records, map_id, db)
    _include_map_seed_choices(db, state)
    return state


def stage_state(db: Session, state: dict) -> None:
    from backend.setup_data import is_files_setup_source
    from backend.engine.definitions import DATA_DIR, SETUPS_DIR
    state = {"library": state["library"], "setups": {key: canonical_setup(value) for key, value in state["setups"].items()}}
    if is_files_setup_source():
        write_library(DATA_DIR, state["library"])
        for key, setup in state["setups"].items():
            write_setup(SETUPS_DIR / key, setup)
        return
    row = db.query(AppSetting).filter(AppSetting.key == SETTING).first()
    value = json.dumps(state, ensure_ascii=False)
    if row:
        row.value_json = value
    else:
        db.add(AppSetting(key=SETTING, value_json=value))


def initialize(db: Session) -> None:
    from backend.setup_data import is_files_setup_source
    state = load_state(db)
    stage_state(db, state)
    db.flush()
    if not is_files_setup_source():
        for map_id in state["library"]["maps"]:
            sync_map(db, map_id)
    db.commit()


def stage_bundle(db: Session, setup_id: str, bundle: dict) -> None:
    from datetime import datetime, timezone
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if row is None:
        return
    for key in BUNDLE_KEYS:
        setattr(row, f"{key}_json", json.dumps(bundle[key], ensure_ascii=False))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def compile_all(state: dict) -> dict[str, dict]:
    return {key: compile_setup(setup, state["library"]) for key, setup in state["setups"].items()}


def map_authority_errors(db: Session, bundle: dict) -> list[str]:
    # Geography can only enter resolved data through the map library.
    return []


def with_map(setup: dict, library: dict, map_id: str) -> dict:
    if map_id not in library["maps"]:
        raise ValueError(f"Unknown map: {map_id}. Save the map in Maps first.")
    out = canonical_setup(setup)
    out["manifest"]["map_asset"] = map_id
    ids = library["maps"][map_id].keys()
    out["territories"] = {tid: out["territories"].get(tid, {"seed": tid}) for tid in ids}
    for field in ("territory_owners", "starting_units"):
        out["starting_setup"][field] = {tid: value for tid, value in out["starting_setup"].get(field, {}).items() if tid in ids}
    for section in ("camps", "ports"):
        out[section] = {key: value for key, value in out[section].items() if value.get("territory_id") in ids}
    for entry in out["factions"].values():
        faction = library["factions"].get(entry.get("seed"), {})
        capital = entry.get("overrides", {}).get("capital", faction.get("capital"))
        if isinstance(capital, str) and capital and capital not in ids:
            entry.setdefault("overrides", {})["capital"] = None
    return canonical_setup(out)


def save_definition(db: Session, setup_id: str, setup: dict) -> dict:
    from backend.catalog import load_catalog
    state = load_state(db)
    if setup_id not in state["setups"]:
        raise ValueError("Setup not found")
    setup = canonical_setup(setup)
    setup["manifest"]["id"] = setup_id
    map_id = scenario_map_id(setup["manifest"], setup_id)
    previous_map = scenario_map_id(state["setups"][setup_id]["manifest"], setup_id)
    if map_id != previous_map:
        setup = with_map(setup, state["library"], map_id)
    bundle = compile_setup(setup, state["library"])
    errors = validate_setup_payload(bundle, load_catalog(db))
    if errors:
        raise ValueError("; ".join(errors))
    state["setups"][setup_id] = setup
    stage_bundle(db, setup_id, bundle)
    stage_state(db, state)
    db.commit()
    return {"setup": setup, "bundle": bundle}


def sync_map(db: Session, map_id: str) -> None:
    """Update geography without applying unrelated pending/invalid seed changes."""
    state = load_state(db)
    snapshots = _bundles(db)
    for key, setup in state["setups"].items():
        if scenario_map_id(setup["manifest"], key) == map_id:
            snapshot = snapshots.get(key)
            if snapshot is None:
                continue
            selected = setup["territories"].keys()
            removed = snapshot["territories"].keys() - selected
            resolved = compile_setup(setup, state["library"])
            snapshot["territories"] = {tid: snapshot["territories"].get(tid, deepcopy(resolved["territories"][tid])) for tid in selected}
            for field in ("territory_owners", "starting_units"):
                snapshot["starting_setup"][field] = {tid: value for tid, value in snapshot["starting_setup"].get(field, {}).items() if tid not in removed}
            for section in ("camps", "ports"):
                snapshot[section] = {cid: value for cid, value in snapshot[section].items() if value.get("territory_id") not in removed}
            for faction in snapshot["factions"].values():
                if isinstance(faction.get("capital"), str) and faction["capital"] in removed:
                    faction["capital"] = None
            for tid, record in snapshot["territories"].items():
                geography = state["library"]["maps"][map_id].get(tid, {})
                for field in MAP_TERRITORY_FIELDS:
                    if field in geography:
                        record[field] = deepcopy(geography[field])
            stage_bundle(db, key, snapshot)
    stage_state(db, state)


def update_library(db: Session, library: dict, *, apply: bool = False) -> dict:
    from backend.catalog import load_catalog
    for section in ("units", "factions", "territories", "maps"):
        if not isinstance(library.get(section), dict):
            raise ValueError(f"library.{section} must be an object")
        if any(not isinstance(key, str) or not isinstance(record, dict) for key, record in library[section].items()):
            raise ValueError(f"library.{section} entries must be objects with string ids")
    state = load_state(db)
    for tid, record in library["territories"].items():
        if record.get("id") != tid or record.keys() & MAP_TERRITORY_FIELDS:
            raise ValueError(f"Territory seed {tid}: use matching id and metadata only; edit geometry in Maps")
    for map_id, records in library["maps"].items():
        from backend.map_overrides import validate_map_base
        validate_map_base(map_id)
        missing = records.keys() - library["territories"].keys()
        if missing:
            raise ValueError("Territory seeds still used by maps: " + ", ".join(sorted(missing)))
        for tid, record in records.items():
            if not isinstance(record, dict):
                raise ValueError("Map territory defaults must be records keyed by territory id")
            old = state["library"]["maps"].get(map_id, {}).get(tid)
            if old and any(old.get(field) != record.get(field) for field in MAP_TERRITORY_FIELDS):
                raise ValueError("Edit connections in Maps; terrain is edited on territory seeds")
    prepare_territory_seeds(library)
    before = _bundles(db)
    state["library"] = library
    after = compile_all(state)
    catalog = load_catalog(db)
    territories = library["territories"]
    seed_errors = validate_setup_payload({"manifest": {"id": "seed_catalog"}, "units": library["units"], "factions": library["factions"],
        "territories": territories, "camps": {}, "ports": {}, "starting_setup": {}}, catalog)
    warnings = {"seeds": seed_errors} if seed_errors else {}
    changed, skipped = [], []
    for key, bundle in after.items():
        errors = validate_setup_payload(bundle, catalog)
        if errors:
            warnings[key] = errors
        if before.get(key) == bundle:
            continue
        changed.append(key)
        if errors:
            skipped.append(key)
        elif apply:
            stage_bundle(db, key, bundle)
    if apply:
        stage_state(db, state)
        db.commit()
    return {"changed_setups": changed, "skipped_setups": skipped, "warnings": warnings,
            "changes": {key: delta(before.get(key, {}), after[key]) for key in changed}}


def save_territory_seed(db: Session, tid: str, record: dict, *, create: bool = False) -> dict:
    from backend.catalog import load_catalog
    from backend.map_builder import _TID_RE
    if not isinstance(tid, str) or not _TID_RE.fullmatch(tid):
        raise ValueError("Territory ID must be 1–64 characters: letters, digits, _ . -")
    if not isinstance(record, dict) or record.get("id") != tid:
        raise ValueError("Territory seed ID must match its key")
    if not isinstance(record.get("display_name"), str) or not record["display_name"].strip():
        raise ValueError("Display name is required")
    if record.get("terrain_type") not in load_catalog(db)["terrain_types"]:
        raise ValueError("Select a known terrain type")
    state = load_state(db)
    seeds = state["library"]["territories"]
    if create and tid in seeds:
        raise ValueError(f"Territory seed {tid} already exists; select it instead")
    if not create and tid not in seeds:
        raise ValueError(f"Territory seed {tid} not found")
    seeds[tid] = deepcopy(record)
    result = update_library(db, state["library"], apply=True)
    return {"seed": seeds[tid], **result}


def sync_map_membership(state: dict, map_id: str, previous_ids: set[str]) -> None:
    """Region reassignment adds new seeds and removes retired IDs from setups."""
    current = state["library"]["maps"][map_id].keys()
    if current == previous_ids:
        return
    added = current - previous_ids
    for key, setup in list(state["setups"].items()):
        if scenario_map_id(setup["manifest"], key) != map_id:
            continue
        selected = (setup["territories"].keys() & current) | added
        normalized = with_map(setup, state["library"], map_id)
        normalized["territories"] = {tid: entry for tid, entry in normalized["territories"].items() if tid in selected}
        state["setups"][key] = normalized
