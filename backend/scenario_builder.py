"""Resolve one canonical setup JSON against shared seeds and map geometry.

Database snapshots are generated consumers, never a second editable document.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any


BUNDLE_KEYS = ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup")
DELETE = {"$delete": True}
MAP_TERRITORY_FIELDS = {"adjacent", "aerial_adjacent", "ford_adjacent"}


def prepare_territory_seeds(library: dict) -> None:
    """Match map geometry to shared territory metadata by id. Preserve geometry."""
    seeds = _object(library.setdefault("territories", {}), "library.territories")
    grouped: dict[str, list[dict]] = {}
    for map_id, records in _object(library.get("maps"), "library.maps").items():
        _object(records, f"maps.{map_id}")
        for tid, record in records.items():
            _object(record, f"maps.{map_id}.{tid}")
            grouped.setdefault(tid, []).append({k: v for k, v in record.items() if k not in MAP_TERRITORY_FIELDS})
    for tid, records in grouped.items():
        if tid not in seeds:
            seeds[tid] = {**_majority(records), "id": tid}
        if "terrain_type" not in seeds[tid]:
            terrains = [record for record in records if record.get("terrain_type")]
            seeds[tid]["terrain_type"] = _majority(terrains).get("terrain_type", "plains")
    for seed in seeds.values():
        seed.setdefault("terrain_type", "plains")
    for map_records in library["maps"].values():
        for tid, record in map_records.items():
            _object(seeds[tid], f"territories.{tid}")
            resolved = {**deepcopy(seeds[tid]), **{k: deepcopy(v) for k, v in record.items() if k in MAP_TERRITORY_FIELDS}, "id": tid}
            map_records[tid] = {key: resolved[key] for key in sorted(resolved)}


def scenario_map_id(manifest: dict, setup_id: str) -> str:
    raw = manifest.get("map_asset")
    return raw if isinstance(raw, str) and raw else setup_id


def merge(base: Any, patch: Any) -> Any:
    """Objects deep-merge, arrays/scalars/null replace. {$delete: true} removes a key."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return deepcopy(patch)
    out = deepcopy(base)
    for key, value in patch.items():
        if value == DELETE:
            out.pop(key, None)
        else:
            out[key] = merge(out.get(key), value)
    return out


def delta(base: dict, value: dict) -> dict:
    out = {key: deepcopy(v) for key, v in value.items() if key not in base}
    out.update({key: deepcopy(DELETE) for key in base if key not in value})
    for key in base.keys() & value.keys():
        if base[key] != value[key]:
            out[key] = delta(base[key], value[key]) if isinstance(base[key], dict) and isinstance(value[key], dict) else deepcopy(value[key])
    return out


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return value


def _majority(records: list[dict]) -> dict:
    """Vote per field; canonical JSON breaks ties reproducibly, independent of input order."""
    out = {}
    for key in sorted(set().union(*(record.keys() for record in records))):
        values = [record[key] for record in records if key in record]
        if all(isinstance(v, dict) for v in values):
            out[key] = _majority(values)
        else:
            counts = Counter(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in values)
            out[key] = json.loads(min(counts, key=lambda v: (-counts[v], v)))
    return out


def extract_library(bundles: dict[str, dict]) -> dict:
    grouped: dict[str, dict[str, list[dict]]] = {"units": {}, "factions": {}}
    maps: dict[str, dict[str, list[dict]]] = {}
    for setup_id, bundle in sorted(bundles.items()):
        for section in grouped:
            for key, record in _object(bundle[section], section).items():
                grouped[section].setdefault(key, []).append(_object(record, f"{section}.{key}"))
        map_id = scenario_map_id(bundle["manifest"], setup_id)
        for tid, record in bundle["territories"].items():
            maps.setdefault(map_id, {}).setdefault(tid, []).append(_object(record, f"territories.{tid}"))
        maps.setdefault(map_id, {})
    library = {
        **{section: {key: _majority(records) for key, records in sorted(groups.items())} for section, groups in grouped.items()},
        "maps": {key: {tid: _majority(records) for tid, records in sorted(groups.items())} for key, groups in sorted(maps.items())},
    }
    prepare_territory_seeds(library)
    return library


def parse_setup_json(text: str) -> dict:
    """JSON only. Legacy YAML is deliberately no longer an authoring format."""
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("Setup JSON exceeds 2 MB")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"Duplicate setup key: {key}")
            out[key] = value
        return out
    def constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")
    try:
        return _object(json.loads(text, object_pairs_hook=pairs, parse_constant=constant), "setup")
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"Invalid setup JSON: {exc}") from exc


def load_library(root: Path, *, prepare: bool = True) -> dict:
    library = json.loads((root / "scenario_library.json").read_text())
    library["maps"] = {p.parent.name: json.loads(p.read_text()) for p in sorted((root / "maps").glob("*/territories.json"))}
    if prepare:
        prepare_territory_seeds(library)
    return library


def write_library(root: Path, library: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scenario_library.json").write_text(json.dumps({key: library[key] for key in ("units", "factions", "territories") if key in library}, indent=2, ensure_ascii=False) + "\n")
    for map_id, records in library["maps"].items():
        from backend.map_overrides import validate_map_base
        validate_map_base(map_id)
        folder = root / "maps" / map_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "territories.json").write_text(json.dumps({tid: {field: value for field, value in record.items() if field in MAP_TERRITORY_FIELDS} for tid, record in records.items()}, indent=2, ensure_ascii=False) + "\n")


def canonical_setup(setup: dict) -> dict:
    """Validate and compact the single editable setup document."""
    _object(setup, "setup")
    unknown = setup.keys() - set(BUNDLE_KEYS)
    if unknown:
        raise ValueError("Unknown setup fields: " + ", ".join(sorted(unknown)))
    out = {section: deepcopy(_object(setup.get(section, {}), section)) for section in BUNDLE_KEYS}
    for field in ("territory_owners", "starting_units"):
        if field in out["starting_setup"]:
            _object(out["starting_setup"][field], f"starting_setup.{field}")
    for section in ("camps", "ports"):
        for key, record in out[section].items():
            _object(record, f"{section}.{key}")
    for section in ("units", "factions", "territories"):
        for key, entry in out[section].items():
            _object(entry, f"{section}.{key}")
            if entry.get("seed") is not None and not isinstance(entry["seed"], str):
                raise ValueError(f"{section}.{key}.seed must be a string")
            if entry.keys() - {"seed", "overrides"}:
                raise ValueError(f"{section}.{key}: use seed and optional overrides")
            patch = _object(entry.get("overrides", {}), f"{section}.{key}.overrides")
            if section == "territories":
                patch = {field: value for field, value in patch.items() if field not in MAP_TERRITORY_FIELDS}
            out[section][key] = {**({"seed": entry["seed"]} if entry.get("seed") is not None else {}), **({"overrides": patch} if patch else {})}
    return out


def compile_setup(setup: dict, library: dict) -> dict:
    setup = canonical_setup(setup)
    map_id = scenario_map_id(setup["manifest"], setup["manifest"].get("id", ""))
    if map_id not in library["maps"]:
        raise ValueError(f"Unknown map: {map_id}")
    out = {section: deepcopy(setup[section]) for section in ("manifest", "camps", "ports", "starting_setup")}
    for section in ("units", "factions", "territories"):
        out[section] = {}
        for key, entry in setup[section].items():
            seed = entry.get("seed")
            if section == "territories":
                if key not in library["maps"][map_id] or seed not in (None, key):
                    raise ValueError(f"Unknown map territory: {key}")
                base = library["maps"][map_id][key]
            else:
                if seed is not None and seed not in library[section]:
                    raise ValueError(f"Unknown {section} seed: {seed}")
                base = library[section].get(seed, {})
            record = merge(base, entry.get("overrides", {}))
            if "id" in record and "id" not in entry.get("overrides", {}):
                record["id"] = key
            out[section][key] = record
    return {section: out[section] for section in BUNDLE_KEYS}


def setup_from_bundle(bundle: dict, library: dict, previous: dict | None = None) -> dict:
    """One-way import of legacy resolved data, preserving pinned override choices."""
    out = {section: deepcopy(bundle[section]) for section in ("manifest", "camps", "ports", "starting_setup")}
    map_id = scenario_map_id(bundle["manifest"], bundle["manifest"].get("id", ""))
    for section in ("units", "factions", "territories"):
        out[section] = {}
        for key, record in bundle[section].items():
            entry = (previous or {}).get(section, {}).get(key)
            seed = entry.get("seed") if entry is not None else (key if section == "territories" or key in library[section] else None)
            base = library["maps"].get(map_id, {}).get(key, {}) if section == "territories" else deepcopy(library[section].get(seed, {}))
            if section != "territories" and "id" in base:
                base["id"] = key
            patch = delta(base, record)
            if entry:
                # Explicit equal overrides remain deliberate choices.
                old_patch = entry.get("overrides", {})
                def retain(default, old, value):
                    result = delta(default, value)
                    for field, item in old.items():
                        if field in value and merge({field: default.get(field)}, {field: item}).get(field) == value[field]:
                            result[field] = deepcopy(item)
                        elif isinstance(item, dict) and item != DELETE and isinstance(value.get(field), dict):
                            nested = retain(default.get(field, {}), item, value[field])
                            if nested:
                                result[field] = nested
                    return result
                patch = retain(base, old_patch, record)
            out[section][key] = {**({"seed": seed} if seed is not None else {}), **({"overrides": patch} if patch else {})}
    return canonical_setup(out)


def migrate_legacy_setups(state: dict) -> dict:
    """Flatten the old source/extends representation once, without importing files."""
    if "setups" in state:
        return {"library": state["library"], "setups": {key: canonical_setup(value) for key, value in state["setups"].items()}}
    from backend.legacy_setup_migration import compile_scenario, resolve_source, upgrade_territory_sources
    library, sources = state["library"], state.get("sources", {})
    upgrade_territory_sources(state)
    setups = {}
    for key, source in sources.items():
        resolved = resolve_source(source, sources.get)
        bundle = compile_scenario(resolved, library)
        previous = {section: resolved.get(section, {}) for section in ("units", "factions")}
        previous["territories"] = {tid: {"seed": tid, "overrides": resolved.get("territory_overrides", {}).get(tid, {})} for tid in bundle["territories"]}
        setups[key] = setup_from_bundle(bundle, library, previous)
    return {"library": library, "setups": setups}


def read_setup_bundle(folder: Path) -> dict:
    """Resolve canonical files; legacy folders remain supported for imports only."""
    path = folder / "setup.json"
    if path.is_file():
        setup = parse_setup_json(path.read_text())
        state = {"library": load_library(folder.parent.parent, prepare=False), "setups": {folder.name: setup}}
        migrate_seed_terrain(state)
        return compile_setup(state["setups"][folder.name], state["library"])
    return {key: json.loads((folder / f"{key}.json").read_text()) if (folder / f"{key}.json").is_file() else {} for key in BUNDLE_KEYS}


def write_setup(folder: Path, setup: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "setup.json").write_text(json.dumps(canonical_setup(setup), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def migrate_seed_terrain(state: dict) -> None:
    """Lift legacy per-map terrain into shared seeds, preserving scenario differences."""
    library = state["library"]
    missing = {tid for tid, seed in library.get("territories", {}).items() if "terrain_type" not in seed}
    original = {map_id: {tid: record.get("terrain_type") for tid, record in records.items()}
                for map_id, records in library["maps"].items()}
    prepare_territory_seeds(library)
    for key, setup in state.get("setups", {}).items():
        map_id = scenario_map_id(setup["manifest"], key)
        for tid, entry in setup["territories"].items():
            terrain = original.get(map_id, {}).get(tid)
            if tid in missing and terrain and terrain != library["territories"][tid]["terrain_type"]:
                entry.setdefault("overrides", {}).setdefault("terrain_type", terrain)
