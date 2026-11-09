"""Declarative authoring compiled to the existing seven-document setup format.

This module is pure: neither compilation nor extraction reads or writes the DB.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable

import yaml

BUNDLE_KEYS = ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup")
SOURCE_KEYS = {"version", "extends", "map", "manifest", "units", "factions", "territory_ids",
               "territory_overrides", "camps", "ports", "starting_setup"}
DELETE = {"$delete": True}


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
    return {
        **{section: {key: _majority(records) for key, records in sorted(groups.items())} for section, groups in grouped.items()},
        "maps": {key: {tid: _majority(records) for tid, records in sorted(groups.items())} for key, groups in sorted(maps.items())},
    }


def extract_source(bundle: dict, library: dict) -> dict:
    map_id = scenario_map_id(bundle["manifest"], bundle["manifest"]["id"])
    source = {"version": 1, "map": map_id, "manifest": deepcopy(bundle["manifest"])}
    if "map_asset" not in source["manifest"]:
        source["manifest"]["map_asset"] = deepcopy(DELETE)
    for section in ("units", "factions"):
        seeds = library[section]
        source[section] = {
            key: {**({"seed": key} if key in seeds else {}), "overrides": delta(seeds.get(key, {}), record)}
            for key, record in bundle[section].items()
        }
    territories = library["maps"].get(map_id, {})
    source["territory_ids"] = list(bundle["territories"])
    source["territory_overrides"] = {
        key: change for key, record in bundle["territories"].items()
        if (change := delta(territories.get(key, {}), record))
    }
    for section in ("camps", "ports", "starting_setup"):
        source[section] = deepcopy(bundle[section])
    return source


def resolve_source(source: dict, lookup: Callable[[str], dict] | None = None) -> dict:
    _object(source, "scenario")
    unknown = source.keys() - SOURCE_KEYS
    if unknown:
        raise ValueError(f"Unknown scenario fields: {', '.join(sorted(unknown))}")
    if type(source.get("version", 1)) is not int or source.get("version", 1) != 1:
        raise ValueError("Unsupported scenario version (expected 1)")
    if "extends" not in source:
        return deepcopy(source)
    parent_id = source["extends"]
    if not isinstance(parent_id, str) or not lookup:
        raise ValueError("extends needs a parent scenario id and a scenario lookup")
    parent = lookup(parent_id)
    if not isinstance(parent, dict) or parent.get("extends"):
        raise ValueError("extends supports exactly one level; parent must exist and have no extends")
    resolve_source(parent)
    return merge(parent, {key: value for key, value in source.items() if key != "extends"})


def compile_scenario(source: dict, library: dict, lookup: Callable[[str], dict] | None = None) -> dict:
    source = resolve_source(source, lookup)
    map_id = source.get("map")
    if not isinstance(map_id, str) or map_id not in library.get("maps", {}):
        raise ValueError(f"Unknown map: {map_id!r}")
    out = {key: deepcopy(_object(source.get(key, {}), key)) for key in ("manifest", "camps", "ports", "starting_setup")}
    # Preserve legacy missing/null/empty map_asset, which resolves to the setup id.
    asset = out["manifest"].get("map_asset")
    if asset == DELETE:
        out["manifest"].pop("map_asset")
    elif "map_asset" not in out["manifest"] or (isinstance(asset, str) and asset):
        out["manifest"]["map_asset"] = map_id
    for section in ("units", "factions"):
        out[section] = {}
        for key, entry in _object(source.get(section, {}), section).items():
            _object(entry, f"{section}.{key}")
            if entry.keys() - {"seed", "overrides"}:
                raise ValueError(f"{section}.{key}: use seed and overrides")
            seed = entry.get("seed")
            if seed is not None and (not isinstance(seed, str) or seed not in library[section]):
                raise ValueError(f"Unknown {section} seed: {seed!r}")
            record = merge(library[section].get(seed, {}), _object(entry.get("overrides", {}), f"{section}.{key}.overrides"))
            # Seed aliases are useful for multiple factions' variants of a unit.
            if seed != key and "id" in record and "id" not in entry.get("overrides", {}):
                record["id"] = key
            out[section][key] = record
    defaults = library["maps"][map_id]
    ids = source.get("territory_ids", list(defaults))
    if not isinstance(ids, list) or any(not isinstance(tid, str) for tid in ids) or len(set(ids)) != len(ids):
        raise ValueError("territory_ids must be a list of unique territory ids")
    overrides = _object(source.get("territory_overrides", {}), "territory_overrides")
    if overrides.keys() - set(ids):
        raise ValueError("territory_overrides contains territories outside territory_ids")
    out["territories"] = {}
    for tid in ids:
        if tid not in defaults:
            raise ValueError(f"Unknown map territory: {tid}")
        out["territories"][tid] = merge(defaults[tid], _object(overrides.get(tid, {}), f"territory_overrides.{tid}"))
    return {key: out[key] for key in BUNDLE_KEYS}


class _NoAliasLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported")
        self._depth += 1
        try:
            if self._depth > 64:
                raise ValueError("Scenario nesting exceeds 64 levels")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("Scenario object keys must be strings")
            if key in keys:
                raise ValueError(f"Duplicate scenario key: {key}")
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_source(text: str) -> dict:
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("Scenario source exceeds 2 MB")
    try:
        value = yaml.load(text, Loader=_NoAliasLoader)
        # Reject YAML-only values such as dates, non-string keys and non-finite numbers.
        json.dumps(value, allow_nan=False)
        return _object(value, "scenario")
    except (yaml.YAMLError, TypeError, RecursionError) as exc:
        raise ValueError(f"Invalid scenario YAML/JSON: {exc}") from exc


def load_library(root: Path) -> dict:
    library = json.loads((root / "scenario_library.json").read_text())
    library["maps"] = {p.parent.name: json.loads(p.read_text()) for p in sorted((root / "maps").glob("*/territories.json"))}
    return library


def write_library(root: Path, library: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scenario_library.json").write_text(json.dumps({key: library[key] for key in ("units", "factions")}, indent=2, ensure_ascii=False) + "\n")
    for map_id, records in library["maps"].items():
        from backend.map_overrides import validate_map_base
        validate_map_base(map_id)
        folder = root / "maps" / map_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "territories.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
