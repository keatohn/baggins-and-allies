"""
Global parchment grain strength for land on every map.

`opacity` is 0–1. Higher is a stronger multiply grain. Missing or invalid
values fall back to the default.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR
from backend.setup_data import is_files_setup_source

MAP_TEXTURE_PATH = DATA_DIR / "map_texture.json"
MAP_TEXTURE_SETTING_KEY = "map_texture"
DEFAULT_MAP_TEXTURE_OPACITY = 0.55


def normalize_map_texture_opacity(raw: Any) -> float:
    if isinstance(raw, bool) or raw is None:
        return DEFAULT_MAP_TEXTURE_OPACITY
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAP_TEXTURE_OPACITY
    if n != n or n in (float("inf"), float("-inf")):
        return DEFAULT_MAP_TEXTURE_OPACITY
    return round(min(1.0, max(0.0, n)), 2)


def _read_file() -> float:
    if not MAP_TEXTURE_PATH.is_file():
        return DEFAULT_MAP_TEXTURE_OPACITY
    try:
        with open(MAP_TEXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return DEFAULT_MAP_TEXTURE_OPACITY
    if not isinstance(data, dict):
        return DEFAULT_MAP_TEXTURE_OPACITY
    return normalize_map_texture_opacity(data.get("opacity"))


def _write_file(opacity: float) -> None:
    MAP_TEXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MAP_TEXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump({"opacity": opacity}, f, indent=2)
        f.write("\n")


def _read_setting(db: Session) -> Any:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == MAP_TEXTURE_SETTING_KEY).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, opacity: float) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps({"opacity": opacity})
    row = db.query(AppSetting).filter(AppSetting.key == MAP_TEXTURE_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=MAP_TEXTURE_SETTING_KEY, value_json=payload))


def load_map_texture_opacity(db: Session | None) -> float:
    if is_files_setup_source() or db is None:
        return _read_file()
    data = _read_setting(db)
    if not isinstance(data, dict):
        return _read_file()
    return normalize_map_texture_opacity(data.get("opacity"))


def save_map_texture_opacity(db: Session | None, raw: Any) -> float:
    opacity = normalize_map_texture_opacity(raw)
    if is_files_setup_source() or db is None:
        _write_file(opacity)
        return opacity
    _write_setting(db, opacity)
    db.commit()
    return opacity


def seed_map_texture_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == MAP_TEXTURE_SETTING_KEY).first():
        return
    _write_setting(db, load_map_texture_opacity(None))
    db.commit()
