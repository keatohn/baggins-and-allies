"""
Global audio settings: per-file volume percents and the menu music playlist.

Gains (0–200, default 100) multiply the player's profile volumes; they do not replace them.
Keys are paths under frontend public/assets/audio, e.g. "music/gondor.m4a" or "sfx/drum.m4a".
Menu music is an ordered list of filenames under public/assets/audio/music.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR
from backend.setup_data import is_files_setup_source

AUDIO_GAINS_PATH = DATA_DIR / "audio_gains.json"
AUDIO_GAINS_SETTING_KEY = "audio_gains"
MENU_MUSIC_SETTING_KEY = "menu_music"
DEFAULT_MENU_MUSIC = ["shire.m4a", "adventure.m4a"]
_KEY_RE = re.compile(r"^(music|sfx)/[A-Za-z0-9_.-]+$")
_LEGACY_MUSIC_DIR_RE = re.compile(r"^(turn|menu|lobby)/")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_audio_gain_pct(raw: Any) -> int | None:
    """Return a clamped 0–200 percent, or None to omit (treat as 100)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return int(round(min(200.0, max(0.0, n))))


def normalize_audio_gains(raw: Any) -> dict[str, int]:
    """Keep only valid keys. Omit 100 so missing = default."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            continue
        rel = key.strip().replace("\\", "/").lstrip("/")
        if rel.startswith("assets/audio/"):
            rel = rel[len("assets/audio/") :]
        rel = _LEGACY_MUSIC_DIR_RE.sub("music/", rel)
        if not _KEY_RE.match(rel):
            continue
        pct = parse_audio_gain_pct(val)
        if pct is None or pct == 100:
            continue
        out[rel] = pct
    return dict(sorted(out.items()))


def normalize_menu_music(raw: Any) -> list[str]:
    """Ordered, de-duplicated filenames. Paths are reduced to the filename."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip().replace("\\", "/").split("/")[-1]
        if name and _FILENAME_RE.match(name) and name not in out:
            out.append(name)
    return out


def _read_settings_file() -> dict[str, Any]:
    if not AUDIO_GAINS_PATH.is_file():
        return {}
    try:
        with open(AUDIO_GAINS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_gains_from_file() -> dict[str, int]:
    data = _read_settings_file()
    if isinstance(data.get("gains"), dict):
        return normalize_audio_gains(data["gains"])
    if "menu_music" in data:
        return {}
    return normalize_audio_gains(data)


def _load_menu_music_from_file() -> list[str]:
    data = _read_settings_file()
    if "menu_music" not in data:
        return list(DEFAULT_MENU_MUSIC)
    return normalize_menu_music(data.get("menu_music"))


def _write_settings_file(gains: dict[str, int], menu_music: list[str]) -> None:
    AUDIO_GAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIO_GAINS_PATH, "w", encoding="utf-8") as f:
        json.dump({"gains": gains, "menu_music": menu_music}, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_setting(db: Session, key: str) -> Any:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, key: str, value: Any) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps(value, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=key, value_json=payload))


def load_audio_gains(db: Session | None) -> dict[str, int]:
    if is_files_setup_source() or db is None:
        return _load_gains_from_file()
    data = _read_setting(db, AUDIO_GAINS_SETTING_KEY)
    if data is None:
        return _load_gains_from_file()
    return normalize_audio_gains(data)


def load_menu_music(db: Session | None) -> list[str]:
    if is_files_setup_source() or db is None:
        return _load_menu_music_from_file()
    data = _read_setting(db, MENU_MUSIC_SETTING_KEY)
    if data is None:
        return _load_menu_music_from_file()
    return normalize_menu_music(data)


def save_audio_settings(
    db: Session | None,
    gains_raw: Any,
    menu_music_raw: Any = None,
) -> tuple[dict[str, int], list[str]]:
    """Replace gains; replace menu music only when menu_music_raw is not None."""
    gains = normalize_audio_gains(gains_raw)
    menu_music = (
        normalize_menu_music(menu_music_raw) if menu_music_raw is not None else load_menu_music(db)
    )
    if is_files_setup_source() or db is None:
        _write_settings_file(gains, menu_music)
        return gains, menu_music
    _write_setting(db, AUDIO_GAINS_SETTING_KEY, gains)
    _write_setting(db, MENU_MUSIC_SETTING_KEY, menu_music)
    db.commit()
    return gains, menu_music


def seed_audio_gains_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    changed = False
    if not db.query(AppSetting).filter(AppSetting.key == AUDIO_GAINS_SETTING_KEY).first():
        _write_setting(db, AUDIO_GAINS_SETTING_KEY, _load_gains_from_file())
        changed = True
    if not db.query(AppSetting).filter(AppSetting.key == MENU_MUSIC_SETTING_KEY).first():
        _write_setting(db, MENU_MUSIC_SETTING_KEY, _load_menu_music_from_file())
        changed = True
    if changed:
        db.commit()
