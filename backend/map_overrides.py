"""
Map-level data shared by every setup that uses the same map asset: icon anchor points,
landscape art, and territory terrain types.

Positions are in the map SVG's viewBox coordinate space (the same space the in-SVG
``hint_<tid>_<kind>`` circles live in). A territory entry is either ``calculated``
(frontend uses its computed centroid/special cases) or ``override`` (frontend uses the
stored points for the listed spot kinds; omitted kinds still fall back to calculated).

Persistence: ``map_overrides`` table (one row per map base); seeded from
``backend/data/maps/<map_base>/overrides.json`` when the table has no row yet.
Follows the catalog/signals files-vs-DB rule (SETUP_SOURCE).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR, SETUPS_DIR
from backend.setup_data import is_files_setup_source

MAPS_DIR = DATA_DIR / "maps"

# Fallback list (frontend MAP_CONFIG keys) so admin map discovery works even with no setups.
KNOWN_MAP_BASES = (
    "motw_map_1.2",
    "wotla_map_1.0",
    "wotla_map_1.1",
    "woteas_map_1.0",
    "wotr_map_1.0",
    "wotr_map_1.1",
    "wotr_map_1.2",
)

# Spot kinds an override may pin. "unit" is the stack anchor (the SVG "units" hint feeds it);
# "marker" is the icon/badge anchor; "center" the signal/ring anchor; "number"/"icons"/"logo"
# are the sea-number, territory-type-icon, and faction-logo anchors.
SPOT_KINDS = ("unit", "marker", "center", "number", "icons", "logo")

FOCUS_VALUES = ("calculated", "override")

MAX_TERRITORY_OVERRIDES = 400
MAX_COORD = 100_000.0

# Landscape decorations (admin-placed terrain art). Stored as a sibling of "territories".
LANDSCAPE_LAYERS = ("interior", "border")
MIN_LANDSCAPE_SCALE = 0.25
MAX_LANDSCAPE_SCALE = 4.0
MAX_LANDSCAPE_PER_TERRITORY = 50
MAX_LANDSCAPE_TOTAL = 2500

# Terrain names match the catalog id shape. Stored per territory on the map, not the setup.
MAX_TERRAIN_ENTRIES = 500
_TERRAIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

_MAP_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LANDSCAPE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class MapOverridesError(ValueError):
    """Admin map override payload failed validation."""


def validate_map_base(map_base: Any) -> str:
    """A map base is a plain file-safe name (e.g. ``wotr_map_1.2``); rejects path tricks."""
    if not isinstance(map_base, str) or not _MAP_BASE_RE.match(map_base) or ".." in map_base:
        raise MapOverridesError("Invalid map id.")
    return map_base


def _entries_source(raw: Any) -> Any:
    """Accept ``{"territories": {...}}`` or a bare ``{tid: entry}`` mapping.

    A dict that carries the ``landscape`` or ``terrain`` sibling but no ``territories``
    key is not a bare tid-mapping — treat it as an empty territory map so those keys
    are not misread as territory ids.
    """
    if isinstance(raw, dict):
        if "territories" in raw:
            return raw.get("territories")
        if "landscape" in raw or "terrain" in raw:
            return {}
    return raw


def _clean_point(pt: Any) -> list[float] | None:
    if isinstance(pt, dict) and "x" in pt and "y" in pt:
        x, y = pt["x"], pt["y"]
    elif isinstance(pt, (list, tuple)) and len(pt) == 2:
        x, y = pt[0], pt[1]
    else:
        return None
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fx) or not math.isfinite(fy):
        return None
    if abs(fx) > MAX_COORD or abs(fy) > MAX_COORD:
        return None
    return [fx, fy]


def _clean_positions(raw: Any) -> dict[str, list[float]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[float]] = {}
    for kind, pt in raw.items():
        if kind not in SPOT_KINDS:
            continue
        point = _clean_point(pt)
        if point is not None:
            out[kind] = point
    return out


def _clean_entry(tid: Any, entry: Any) -> dict[str, Any] | None:
    if not isinstance(tid, str) or not _TID_RE.match(tid):
        return None
    if not isinstance(entry, dict):
        return None
    focus = entry.get("focus")
    if focus not in FOCUS_VALUES:
        return None
    if focus == "calculated":
        return {"focus": "calculated"}
    return {"focus": "override", "positions": _clean_positions(entry.get("positions"))}


def _clean_placement(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cat = raw.get("cat")
    img = raw.get("img")
    if not isinstance(cat, str) or not _LANDSCAPE_NAME_RE.match(cat) or ".." in cat:
        return None
    if not isinstance(img, str) or not _LANDSCAPE_NAME_RE.match(img) or ".." in img:
        return None
    if not img.lower().endswith(".png"):
        return None
    layer = raw.get("layer", "interior")
    if layer not in LANDSCAPE_LAYERS:
        return None
    x, y = raw.get("x"), raw.get("y")
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fx) or not math.isfinite(fy):
        return None
    if abs(fx) > MAX_COORD or abs(fy) > MAX_COORD:
        return None
    out: dict[str, Any] = {"cat": cat, "img": img, "x": fx, "y": fy, "layer": layer}
    scale = raw.get("s")
    if scale is not None:
        if isinstance(scale, bool):
            return None
        try:
            fscale = float(scale)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(fscale) or not (MIN_LANDSCAPE_SCALE <= fscale <= MAX_LANDSCAPE_SCALE):
            return None
        out["s"] = fscale
    if raw.get("auto") is True:
        out["auto"] = True
    return out


def _clean_landscape_section(raw: Any) -> dict[str, Any] | None:
    """Lenient parse of the landscape sibling; returns None when empty/unusable."""
    if not isinstance(raw, dict):
        return None
    source = raw.get("territories")
    if not isinstance(source, dict):
        return None
    out: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for tid, placements in source.items():
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            continue
        if not isinstance(placements, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for item in placements:
            if total >= MAX_LANDSCAPE_TOTAL or len(cleaned) >= MAX_LANDSCAPE_PER_TERRITORY:
                break
            one = _clean_placement(item)
            if one is not None:
                cleaned.append(one)
                total += 1
        if cleaned:
            out[tid] = cleaned
    return out or None


def _raw_landscape_section(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw.get("landscape")
    return None


def _clean_terrain_name(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    if not _TERRAIN_RE.match(name):
        return None
    return name


def _clean_terrain(raw: Any) -> dict[str, str]:
    """Lenient terrain map. Unknown ids and names are dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for tid, name in raw.items():
        if len(out) >= MAX_TERRAIN_ENTRIES:
            break
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            continue
        cleaned = _clean_terrain_name(name)
        if cleaned is not None:
            out[tid] = cleaned
    return out


def _validate_terrain(raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise MapOverridesError("Terrain must be an object mapping territory ids to terrain types.")
    if len(raw) > MAX_TERRAIN_ENTRIES:
        raise MapOverridesError(f"At most {MAX_TERRAIN_ENTRIES} terrain entries.")
    for tid, name in raw.items():
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            raise MapOverridesError(f"Invalid terrain territory id: {tid!r}.")
        if _clean_terrain_name(name) is None:
            raise MapOverridesError(
                f"Terrain for {tid} must be a catalog terrain id (lowercase letters, digits, underscores)."
            )


def normalize_map_overrides(raw: Any) -> dict[str, Any]:
    """Lenient parse for reads/seed: drop anything malformed, always return the canonical shape."""
    source = _entries_source(raw)
    out: dict[str, Any] = {"territories": {}}
    if isinstance(source, dict):
        tids: dict[str, dict[str, Any]] = {}
        for tid, entry in source.items():
            cleaned = _clean_entry(tid, entry)
            if cleaned is not None:
                tids[str(tid)] = cleaned
            if len(tids) >= MAX_TERRITORY_OVERRIDES:
                break
        out["territories"] = tids
    landscape = _clean_landscape_section(_raw_landscape_section(raw))
    if landscape is not None:
        out["landscape"] = {"territories": landscape}
    if isinstance(raw, dict) and "terrain" in raw:
        out["terrain"] = _clean_terrain(raw.get("terrain"))
    return out


def validate_map_overrides(raw: Any) -> dict[str, Any]:
    """Strict parse for admin saves: any malformed entry is an error, not a silent drop."""
    source = _entries_source(raw)
    if not isinstance(source, dict):
        raise MapOverridesError("Map overrides must be an object mapping territory ids to entries.")
    if len(source) > MAX_TERRITORY_OVERRIDES:
        raise MapOverridesError(f"At most {MAX_TERRITORY_OVERRIDES} territory overrides.")
    for tid, entry in source.items():
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            raise MapOverridesError(f"Invalid territory id: {tid!r}.")
        if not isinstance(entry, dict):
            raise MapOverridesError(f"Entry for {tid} must be an object.")
        focus = entry.get("focus")
        if focus not in FOCUS_VALUES:
            raise MapOverridesError(
                f"Entry for {tid} needs focus 'calculated' or 'override' (got {focus!r})."
            )
        if focus != "override":
            continue
        positions = entry.get("positions")
        if positions is None:
            continue
        if not isinstance(positions, dict):
            raise MapOverridesError(f"Positions for {tid} must be an object.")
        for kind, pt in positions.items():
            if kind not in SPOT_KINDS:
                raise MapOverridesError(
                    f"Unknown spot kind {kind!r} for {tid}. Allowed: {', '.join(SPOT_KINDS)}."
                )
            if _clean_point(pt) is None:
                raise MapOverridesError(
                    f"Position for {tid}.{kind} must be two finite numbers [x, y]."
                )
    _validate_landscape_section(_raw_landscape_section(raw))
    if isinstance(raw, dict):
        _validate_terrain(raw.get("terrain"))
    return normalize_map_overrides(raw)


def _validate_landscape_section(raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise MapOverridesError("Landscape overrides must be an object with a 'territories' map.")
    source = raw.get("territories")
    if not isinstance(source, dict):
        raise MapOverridesError("Landscape 'territories' must be an object mapping territory ids to lists.")
    total = 0
    for tid, placements in source.items():
        if not isinstance(tid, str) or not _TID_RE.match(tid):
            raise MapOverridesError(f"Invalid landscape territory id: {tid!r}.")
        if not isinstance(placements, list):
            raise MapOverridesError(f"Landscape placements for {tid} must be a list.")
        if len(placements) > MAX_LANDSCAPE_PER_TERRITORY:
            raise MapOverridesError(
                f"At most {MAX_LANDSCAPE_PER_TERRITORY} landscape placements per territory ({tid})."
            )
        total += len(placements)
        if total > MAX_LANDSCAPE_TOTAL:
            raise MapOverridesError(f"At most {MAX_LANDSCAPE_TOTAL} landscape placements per map.")
        for item in placements:
            if _clean_placement(item) is None:
                raise MapOverridesError(
                    f"Landscape placement for {tid} needs cat/img (.png), layer "
                    f"({'/'.join(LANDSCAPE_LAYERS)}), finite x/y, and optional s in "
                    f"[{MIN_LANDSCAPE_SCALE}, {MAX_LANDSCAPE_SCALE}]."
                )


def _read_file(map_base: str) -> Any:
    path = MAPS_DIR / map_base / "overrides.json"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_file(map_base: str, data: dict[str, Any]) -> None:
    directory = MAPS_DIR / map_base
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "overrides.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_row(db: Session, map_base: str) -> Any:
    from backend.api.models import MapOverride

    row = db.query(MapOverride).filter(MapOverride.map_base == map_base).first()
    if not row:
        return None
    try:
        return json.loads(row.overrides_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_row(db: Session, map_base: str, data: dict[str, Any]) -> None:
    from backend.api.models import MapOverride

    payload = json.dumps(data, ensure_ascii=False)
    row = db.query(MapOverride).filter(MapOverride.map_base == map_base).first()
    if row:
        row.overrides_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(MapOverride(map_base=map_base, overrides_json=payload))


def _stored_raw(db: Session | None, map_base: str) -> Any:
    if is_files_setup_source() or db is None:
        return _read_file(map_base)
    row = _read_row(db, map_base)
    if row is None:
        return _read_file(map_base)
    return row


def load_map_overrides(db: Session | None, map_base: str) -> dict[str, Any]:
    """Stored overrides for a map; ``{"territories": {}}`` when none exist.

    When the stored document has no ``terrain`` key yet, terrain is filled from the
    setups that use this map (only where they agree). That fill is not written here.
    """
    raw = _stored_raw(db, map_base)
    data = normalize_map_overrides(raw)
    if not (isinstance(raw, dict) and "terrain" in raw):
        filled = agreed_terrain_for_map(db, map_base)
        if filled:
            data["terrain"] = filled
    return data


def _require_known_terrain(db: Session | None, terrain: dict[str, str]) -> None:
    if not terrain:
        return
    from backend.catalog import load_catalog

    known = {t for t in (load_catalog(db).get("terrain_types") or []) if isinstance(t, str)}
    if not known:
        return
    for tid, name in terrain.items():
        if name not in known:
            raise MapOverridesError(f'Terrain "{name}" for {tid} is not in the catalog.')


def save_map_overrides(db: Session | None, map_base: str, raw: Any) -> dict[str, Any]:
    """Replace a map's overrides. Invalid payloads raise MapOverridesError (HTTP 400)."""
    validate_map_base(map_base)
    data = validate_map_overrides(raw)
    terrain = data.get("terrain") or {}
    _require_known_terrain(db, terrain)
    if is_files_setup_source() or db is None:
        _write_file(map_base, data)
        _mirror_terrain_files(map_base, terrain)
        return data
    _write_row(db, map_base, data)
    _mirror_terrain_db(db, map_base, terrain)
    db.commit()
    return data


def apply_map_terrain(territories: dict[str, Any] | None, map_base: str | None, db: Session | None) -> None:
    """Copy this map's terrain onto territory records. Missing map entries stay as they are."""
    if not territories or not isinstance(map_base, str) or not map_base:
        return
    try:
        base = validate_map_base(map_base)
    except MapOverridesError:
        return
    terrain = load_map_overrides(db, base).get("terrain") or {}
    if not terrain:
        return
    for tid, record in territories.items():
        name = terrain.get(tid)
        if not isinstance(name, str) or not name:
            continue
        if isinstance(record, dict):
            record["terrain_type"] = name
        elif hasattr(record, "terrain_type"):
            record.terrain_type = name


def publish_setup_terrain(db: Session | None, map_base: str | None, territories: dict[str, Any] | None) -> None:
    """Write one setup's terrain onto the map, then mirror it to every setup on that map."""
    if not isinstance(map_base, str) or not map_base or not isinstance(territories, dict):
        return
    try:
        validate_map_base(map_base)
    except MapOverridesError:
        return
    updates: dict[str, str] = {}
    for tid, row in territories.items():
        if not isinstance(row, dict):
            continue
        name = _clean_terrain_name(row.get("terrain_type"))
        if name is not None and isinstance(tid, str):
            updates[tid] = name
    if not updates:
        return
    current = load_map_overrides(db, map_base)
    terrain = dict(current.get("terrain") or {})
    if all(terrain.get(tid) == name for tid, name in updates.items()):
        return
    terrain.update(updates)
    current["terrain"] = terrain
    save_map_overrides(db, map_base, current)


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _entry_from(setup_id: str, manifest_raw: Any, territories_raw: Any) -> tuple[str, set[str]]:
    manifest = manifest_raw
    if isinstance(manifest_raw, str):
        try:
            manifest = json.loads(manifest_raw)
        except json.JSONDecodeError:
            manifest = None
    if not isinstance(manifest, dict):
        manifest = {}
    territories = territories_raw
    if isinstance(territories_raw, str):
        try:
            territories = json.loads(territories_raw)
        except json.JSONDecodeError:
            territories = None
    tids = set(territories.keys()) if isinstance(territories, dict) else set()
    # Same fallback as engine.definitions: missing map_asset means the setup id is the asset.
    map_base = manifest.get("map_asset") or setup_id
    if not isinstance(map_base, str) or not map_base:
        map_base = setup_id
    return map_base, tids


def _iter_setup_maps(db: Session | None) -> Iterator[tuple[str, set[str]]]:
    """(map_base, territory ids) for every known setup — DB rows first, files as fallback."""
    if db is not None and not is_files_setup_source():
        from backend.api.models import Setup

        rows = db.query(Setup.id, Setup.manifest_json, Setup.territories_json).all()
        if rows:
            for setup_id, manifest_json, territories_json in rows:
                yield _entry_from(setup_id, manifest_json, territories_json)
            return
    if not SETUPS_DIR.is_dir():
        return
    for d in sorted(SETUPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = _read_json(d / "manifest.json")
        territories = _read_json(d / "territories.json")
        yield _entry_from(d.name, manifest if isinstance(manifest, dict) else None, territories)


def _terrain_from_setup_territories(territories: Any) -> dict[str, str]:
    if not isinstance(territories, dict):
        return {}
    raw: dict[str, Any] = {}
    for tid, row in territories.items():
        if isinstance(row, dict):
            raw[tid] = row.get("terrain_type")
    return _clean_terrain(raw)


def _decode_territories(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def _iter_setup_terrain(db: Session | None) -> Iterator[tuple[str, dict[str, str]]]:
    """(map_base, {territory id: terrain}) for every setup. DB rows when the DB is the source."""
    if db is not None and not is_files_setup_source():
        from backend.api.models import Setup

        rows = db.query(Setup.id, Setup.manifest_json, Setup.territories_json).all()
        for setup_id, manifest_json, territories_json in rows:
            map_base, _tids = _entry_from(setup_id, manifest_json, territories_json)
            yield map_base, _terrain_from_setup_territories(_decode_territories(territories_json))
        return
    if not SETUPS_DIR.is_dir():
        return
    for d in sorted(SETUPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = _read_json(d / "manifest.json")
        territories = _read_json(d / "territories.json")
        map_base, _tids = _entry_from(d.name, manifest if isinstance(manifest, dict) else None, territories)
        yield map_base, _terrain_from_setup_territories(territories)


def agreed_terrain(db: Session | None) -> dict[str, dict[str, str]]:
    """Terrain per map, keeping a territory only when every setup that has it agrees."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for map_base, terrain in _iter_setup_terrain(db):
        grouped.setdefault(map_base, []).append(terrain)
    out: dict[str, dict[str, str]] = {}
    for map_base, maps in grouped.items():
        keys: set[str] = set()
        for one in maps:
            keys.update(one)
        agreed: dict[str, str] = {}
        for tid in keys:
            vals = {one[tid] for one in maps if tid in one}
            if len(vals) == 1:
                agreed[tid] = next(iter(vals))
        if agreed:
            out[map_base] = agreed
    return out


def agreed_terrain_for_map(db: Session | None, map_base: str) -> dict[str, str]:
    return agreed_terrain(db).get(map_base) or {}


def _mirror_terrain_db(db: Session, map_base: str, terrain: dict[str, str]) -> None:
    """Copy map terrain onto each setup row that uses this map. Does not commit."""
    if not terrain:
        return
    from backend.api.models import Setup

    for row in db.query(Setup).all():
        base, _tids = _entry_from(row.id, row.manifest_json, row.territories_json)
        if base != map_base:
            continue
        territories = _decode_territories(row.territories_json)
        if not isinstance(territories, dict):
            continue
        touched = False
        for tid, name in terrain.items():
            record = territories.get(tid)
            if isinstance(record, dict) and record.get("terrain_type") != name:
                record["terrain_type"] = name
                touched = True
        if touched:
            row.territories_json = json.dumps(territories, ensure_ascii=False)
            row.updated_at = datetime.utcnow()


def _mirror_terrain_files(map_base: str, terrain: dict[str, str]) -> None:
    """Files-source only. Production reads the database and never takes this path."""
    if not terrain or not SETUPS_DIR.is_dir():
        return
    for d in sorted(SETUPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = _read_json(d / "manifest.json")
        territories = _read_json(d / "territories.json")
        base, _tids = _entry_from(d.name, manifest if isinstance(manifest, dict) else None, territories)
        if base != map_base or not isinstance(territories, dict):
            continue
        touched = False
        for tid, name in terrain.items():
            record = territories.get(tid)
            if isinstance(record, dict) and record.get("terrain_type") != name:
                record["terrain_type"] = name
                touched = True
        if touched:
            with open(d / "territories.json", "w", encoding="utf-8") as f:
                json.dump(territories, f, indent=2, ensure_ascii=False)
                f.write("\n")


def list_map_bases(db: Session | None) -> list[str]:
    """Distinct map asset names (sorted): setup map_asset values, stored override keys,
    and the known-map fallback list."""
    bases = set(KNOWN_MAP_BASES)
    for map_base, _tids in _iter_setup_maps(db):
        bases.add(map_base)
    if db is not None and not is_files_setup_source():
        from backend.api.models import MapOverride

        for (stored_base,) in db.query(MapOverride.map_base).all():
            bases.add(stored_base)
    elif MAPS_DIR.is_dir():
        for d in MAPS_DIR.iterdir():
            if d.is_dir():
                bases.add(d.name)
    return sorted(bases)


def seed_map_overrides_if_empty(db: Session) -> None:
    """Insert backend/data/maps/<map_base>/overrides.json rows that are missing from the table."""
    if is_files_setup_source():
        return
    if not MAPS_DIR.is_dir():
        return
    from backend.api.models import MapOverride

    added = False
    for d in sorted(MAPS_DIR.iterdir()):
        if not d.is_dir() or not _MAP_BASE_RE.match(d.name) or ".." in d.name:
            continue
        map_base = d.name
        if db.query(MapOverride).filter(MapOverride.map_base == map_base).first():
            continue
        raw = _read_file(map_base)
        if raw is None:
            continue
        data = normalize_map_overrides(raw)
        db.add(
            MapOverride(
                map_base=map_base,
                overrides_json=json.dumps(data, ensure_ascii=False),
                updated_at=datetime.utcnow(),
            )
        )
        added = True
    if added:
        db.commit()


def seed_map_terrain_if_missing(db: Session) -> int:
    """Copy agreed setup terrain onto map rows that do not have a terrain key yet.

    Production stores this on the map_overrides row. Repo setup files are only read
    when the database has no setups. An existing terrain key is left alone.
    """
    if is_files_setup_source():
        return 0
    added = 0
    for map_base, terrain in agreed_terrain(db).items():
        try:
            validate_map_base(map_base)
        except MapOverridesError:
            continue
        raw = _read_row(db, map_base)
        if isinstance(raw, dict) and "terrain" in raw:
            continue
        if raw is None:
            filed = _read_file(map_base)
            if isinstance(filed, dict) and "terrain" in filed:
                continue
            raw = filed
        data = normalize_map_overrides(raw)
        data["terrain"] = terrain
        _write_row(db, map_base, data)
        added += 1
    if added:
        db.commit()
    return added
