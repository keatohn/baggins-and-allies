"""Saved map-builder drawings.

A saved map is the shared-border document plus the territory SVG generated from it.
Built-in maps stay in ``frontend/public/<map_base>.svg``. A saved map is added to the
set of maps setups and the icon/landscape editor can use, and when the id matches a
built-in map the saved SVG is what the game loads.

Persistence follows the setups files-vs-DB rule: ``backend/data/maps/<map_base>/builder.json``
and ``map.svg`` when ``SETUP_SOURCE=files``; the ``map_assets`` table otherwise.
Rows missing from the table are seeded from those files.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.map_builder import MapBuilderError, convert_map_builder
from backend.map_overrides import MAPS_DIR, MapOverridesError, validate_map_base
from backend.setup_data import is_files_setup_source

MAX_NAME_LEN = 120


def _check_base(map_base: Any) -> str:
    try:
        return validate_map_base(map_base)
    except MapOverridesError as e:
        raise MapBuilderError(str(e)) from e


def _display_name(raw: Any, fallback: str) -> str:
    name = raw if isinstance(raw, str) else ""
    name = name.strip() or fallback
    if len(name) > MAX_NAME_LEN:
        raise MapBuilderError(f"display name is too long (max {MAX_NAME_LEN})")
    return name


def _doc_from_payload(payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise MapBuilderError("payload must be a JSON object")
    display = payload.get("display_name")
    doc = payload.get("doc") if isinstance(payload.get("doc"), dict) else payload
    if not isinstance(doc, dict):
        raise MapBuilderError("map drawing is missing")
    # convert_map_builder validates the graph and is what the game will render.
    convert_map_builder(doc)
    name = _display_name(display if display is not None else doc.get("display_name"), "Untitled map")
    stored = {
        "width": doc["width"],
        "height": doc["height"],
        "joints": doc["joints"],
        "borders": doc["borders"],
        "labels": doc["labels"],
    }
    return name, stored


def _svg_for(doc: dict[str, Any]) -> str:
    return convert_map_builder(doc)["svg"]


def _summary(map_base: str, name: str, doc: dict[str, Any], updated: datetime | None) -> dict[str, Any]:
    return {
        "map_base": map_base,
        "display_name": name,
        "width": doc.get("width"),
        "height": doc.get("height"),
        "territories": len(doc.get("labels") or {}),
        "updated_at": updated.isoformat() + "Z" if isinstance(updated, datetime) else None,
    }


def _read_files(map_base: str) -> tuple[str, dict[str, Any], str, datetime] | None:
    directory = MAPS_DIR / map_base
    builder_path = directory / "builder.json"
    if not builder_path.is_file():
        return None
    try:
        raw = json.loads(builder_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise MapBuilderError(f"could not read {builder_path.name}: {e}") from e
    name, doc = _doc_from_payload(raw)
    svg_path = directory / "map.svg"
    svg = svg_path.read_text(encoding="utf-8") if svg_path.is_file() else _svg_for(doc)
    updated = datetime.utcfromtimestamp(builder_path.stat().st_mtime)
    return name, doc, svg, updated


def _write_files(map_base: str, name: str, doc: dict[str, Any], svg: str) -> None:
    directory = MAPS_DIR / map_base
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"display_name": name, **doc}
    (directory / "builder.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (directory / "map.svg").write_text(svg, encoding="utf-8")


def _row(db: Session, map_base: str) -> Any:
    from backend.api.models import MapAsset

    return db.query(MapAsset).filter(MapAsset.map_base == map_base).first()


def list_map_assets(db: Session | None) -> list[dict[str, Any]]:
    if is_files_setup_source() or db is None:
        out: list[dict[str, Any]] = []
        if not MAPS_DIR.is_dir():
            return out
        for directory in sorted(MAPS_DIR.iterdir()):
            builder_path = directory / "builder.json"
            if not directory.is_dir() or not builder_path.is_file():
                continue
            try:
                raw = json.loads(builder_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            name = raw.get("display_name") if isinstance(raw.get("display_name"), str) else directory.name
            updated = datetime.utcfromtimestamp(builder_path.stat().st_mtime)
            out.append(_summary(directory.name, name.strip() or directory.name, raw, updated))
        return out

    from backend.api.models import MapAsset

    rows = db.query(MapAsset).order_by(MapAsset.map_base).all()
    out = []
    for row in rows:
        try:
            raw = json.loads(row.builder_json)
        except json.JSONDecodeError:
            continue
        doc = raw if isinstance(raw, dict) else {}
        out.append(_summary(row.map_base, row.display_name, doc, row.updated_at))
    return out


def stored_map_bases(db: Session | None) -> list[str]:
    return [row["map_base"] for row in list_map_assets(db)]


def load_map_asset(db: Session | None, map_base: str) -> dict[str, Any]:
    base = _check_base(map_base)
    if is_files_setup_source() or db is None:
        loaded = _read_files(base)
        if loaded is None:
            raise MapBuilderError(f"no saved map {base!r}")
        name, doc, _svg, updated = loaded
        return {"map_base": base, "display_name": name, "updated_at": updated.isoformat() + "Z", "doc": doc}

    row = _row(db, base)
    if row is None:
        filed = _read_files(base)
        if filed is None:
            raise MapBuilderError(f"no saved map {base!r}")
        name, doc, svg, _updated = filed
        _write_row(db, base, name, doc, svg)
        row = _row(db, base)
    doc = json.loads(row.builder_json)
    return {
        "map_base": base,
        "display_name": row.display_name,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "doc": doc,
    }


def load_map_svg(db: Session | None, map_base: str) -> str | None:
    """Territory SVG for a saved map, or None when this id has no saved drawing."""
    try:
        base = _check_base(map_base)
    except MapBuilderError:
        return None
    if is_files_setup_source() or db is None:
        svg_path = MAPS_DIR / base / "map.svg"
        if svg_path.is_file():
            return svg_path.read_text(encoding="utf-8")
        return None
    row = _row(db, base)
    if row is not None and isinstance(row.svg_text, str) and row.svg_text.strip():
        return row.svg_text
    svg_path = MAPS_DIR / base / "map.svg"
    if svg_path.is_file():
        return svg_path.read_text(encoding="utf-8")
    return None


def save_map_asset(db: Session | None, map_base: str, payload: Any) -> dict[str, Any]:
    base = _check_base(map_base)
    name, doc = _doc_from_payload(payload)
    svg = _svg_for(doc)
    if is_files_setup_source() or db is None:
        _write_files(base, name, doc, svg)
        updated = datetime.utcnow()
    else:
        _write_row(db, base, name, doc, svg)
        updated = _row(db, base).updated_at
    return {"ok": True, "map_base": base, "display_name": name, "updated_at": updated.isoformat() + "Z", "doc": doc}


def copy_map_asset(db: Session | None, map_base: str, new_base: str, display_name: str | None) -> dict[str, Any]:
    source = load_map_asset(db, map_base)
    dest = _check_base(new_base)
    if dest == source["map_base"]:
        raise MapBuilderError("copy needs a different map id")
    if _exists(db, dest):
        raise MapBuilderError(f"map {dest!r} already exists")
    name = _display_name(display_name, source["display_name"])
    return save_map_asset(db, dest, {"display_name": name, "doc": source["doc"]})


def delete_map_asset(db: Session | None, map_base: str) -> None:
    base = _check_base(map_base)
    if not _exists(db, base):
        raise MapBuilderError(f"no saved map {base!r}")
    if is_files_setup_source() or db is None:
        directory = MAPS_DIR / base
        for name in ("builder.json", "map.svg"):
            path = directory / name
            if path.is_file():
                path.unlink()
        try:
            directory.rmdir()
        except OSError:
            # Icon overrides in this folder stay behind.
            pass
        return
    row = _row(db, base)
    if row is None:
        raise MapBuilderError(f"no saved map {base!r}")
    db.delete(row)
    db.commit()


def seed_map_assets_if_empty(db: Session) -> None:
    if is_files_setup_source() or not MAPS_DIR.is_dir():
        return
    from backend.api.models import MapAsset

    added = False
    for directory in sorted(MAPS_DIR.iterdir()):
        if not directory.is_dir() or not (directory / "builder.json").is_file():
            continue
        if db.query(MapAsset).filter(MapAsset.map_base == directory.name).first():
            continue
        try:
            loaded = _read_files(directory.name)
        except MapBuilderError:
            continue
        if loaded is None:
            continue
        name, doc, svg, _updated = loaded
        _write_row(db, directory.name, name, doc, svg)
        added = True
    if added:
        db.commit()


def _exists(db: Session | None, map_base: str) -> bool:
    if is_files_setup_source() or db is None:
        return (MAPS_DIR / map_base / "builder.json").is_file()
    return _row(db, map_base) is not None


def _write_row(db: Session, map_base: str, name: str, doc: dict[str, Any], svg: str) -> None:
    from backend.api.models import MapAsset

    row = _row(db, map_base)
    payload = json.dumps(doc, ensure_ascii=False)
    now = datetime.utcnow()
    if row is None:
        db.add(
            MapAsset(
                map_base=map_base,
                display_name=name,
                builder_json=payload,
                svg_text=svg,
                updated_at=now,
            )
        )
    else:
        row.display_name = name
        row.builder_json = payload
        row.svg_text = svg
        row.updated_at = now
    db.commit()
