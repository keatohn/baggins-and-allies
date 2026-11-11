"""One-way compatibility for pre-canonical database exports. Not an authoring API.

Only JSON already stored in app_settings is read. YAML files are never loaded.
"""
from __future__ import annotations
from copy import deepcopy
from typing import Callable
from backend.scenario_builder import BUNDLE_KEYS, DELETE, _object, delta, merge, prepare_territory_seeds, scenario_map_id
SOURCE_KEYS = {"version", "extends", "map", "manifest", "units", "factions", "territory_ids", "territory_overrides", "camps", "ports", "starting_setup"}

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


def overlay_overrides(base: dict, change: dict) -> dict:
    """Update override expressions, retaining deletion markers as expressions."""
    out = deepcopy(base)
    for key, value in change.items():
        if isinstance(value, dict) and value != DELETE and isinstance(out.get(key), dict) and out[key] != DELETE:
            out[key] = overlay_overrides(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def upgrade_territory_sources(state: dict) -> None:
    """Preserve legacy bundles and extends while lifting map metadata into seeds."""
    library, sources = state["library"], state["sources"]
    before = {key: compile_scenario(source, library, sources.get) for key, source in sources.items()}
    prepare_territory_seeds(library)
    for key in sorted(sources, key=lambda key: bool(sources[key].get("extends"))):
        source = sources[key]
        after = compile_scenario(source, library, sources.get)
        changes = delta(after["territories"], before[key]["territories"])
        if changes:
            source["territory_overrides"] = overlay_overrides(source.get("territory_overrides", {}), changes)


