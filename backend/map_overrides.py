"""
Map-level icon/location overrides: admin-edited anchor points per territory, shared by
every setup that uses the same map asset (unlike setup data, these are keyed by map base).

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

_MAP_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class MapOverridesError(ValueError):
    """Admin map override payload failed validation."""


def validate_map_base(map_base: Any) -> str:
    """A map base is a plain file-safe name (e.g. ``wotr_map_1.2``); rejects path tricks."""
    if not isinstance(map_base, str) or not _MAP_BASE_RE.match(map_base) or ".." in map_base:
        raise MapOverridesError("Invalid map id.")
    return map_base


def _entries_source(raw: Any) -> Any:
    """Accept ``{"territories": {...}}`` or a bare ``{tid: entry}`` mapping."""
    if isinstance(raw, dict) and "territories" in raw:
        return raw.get("territories")
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


def normalize_map_overrides(raw: Any) -> dict[str, Any]:
    """Lenient parse for reads/seed: drop anything malformed, always return the canonical shape."""
    source = _entries_source(raw)
    if not isinstance(source, dict):
        return {"territories": {}}
    out: dict[str, dict[str, Any]] = {}
    for tid, entry in source.items():
        cleaned = _clean_entry(tid, entry)
        if cleaned is not None:
            out[str(tid)] = cleaned
        if len(out) >= MAX_TERRITORY_OVERRIDES:
            break
    return {"territories": out}


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
    return normalize_map_overrides(raw)


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


def load_map_overrides(db: Session | None, map_base: str) -> dict[str, Any]:
    """Stored overrides for a map; ``{"territories": {}}`` when none exist."""
    if is_files_setup_source() or db is None:
        return normalize_map_overrides(_read_file(map_base))
    row = _read_row(db, map_base)
    if row is None:
        return normalize_map_overrides(_read_file(map_base))
    return normalize_map_overrides(row)


def save_map_overrides(db: Session | None, map_base: str, raw: Any) -> dict[str, Any]:
    """Replace a map's overrides. Invalid payloads raise MapOverridesError (HTTP 400)."""
    validate_map_base(map_base)
    data = validate_map_overrides(raw)
    if is_files_setup_source() or db is None:
        _write_file(map_base, data)
        return data
    _write_row(db, map_base, data)
    db.commit()
    return data


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
