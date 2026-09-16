"""
Per-audio-file volume percents (0–200, default 100).

These multiply the player's profile volumes; they do not replace them.
Keys are paths under frontend public/assets/audio, e.g. "turn/gondor.m4a".
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
_KEY_RE = re.compile(r"^(turn|menu|lobby|sfx)/[A-Za-z0-9_.-]+$")


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
        if not _KEY_RE.match(rel):
            continue
        pct = parse_audio_gain_pct(val)
        if pct is None or pct == 100:
            continue
        out[rel] = pct
    return dict(sorted(out.items()))


def _load_gains_from_file() -> dict[str, int]:
    if not AUDIO_GAINS_PATH.is_file():
        return {}
    try:
        with open(AUDIO_GAINS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("gains"), dict):
        data = data["gains"]
    return normalize_audio_gains(data)


def _write_gains_to_file(gains: dict[str, int]) -> None:
    AUDIO_GAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIO_GAINS_PATH, "w", encoding="utf-8") as f:
        json.dump(gains, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_audio_gains(db: Session | None) -> dict[str, int]:
    if is_files_setup_source() or db is None:
        return _load_gains_from_file()
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == AUDIO_GAINS_SETTING_KEY).first()
    if not row:
        return _load_gains_from_file()
    try:
        data = json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return _load_gains_from_file()
    return normalize_audio_gains(data)


def save_audio_gains(db: Session | None, raw: Any) -> dict[str, int]:
    gains = normalize_audio_gains(raw)
    if is_files_setup_source() or db is None:
        _write_gains_to_file(gains)
        return gains
    from backend.api.models import AppSetting

    payload = json.dumps(gains, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == AUDIO_GAINS_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=AUDIO_GAINS_SETTING_KEY, value_json=payload))
    db.commit()
    return gains


def seed_audio_gains_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == AUDIO_GAINS_SETTING_KEY).first()
    if row:
        return
    gains = _load_gains_from_file()
    db.add(AppSetting(key=AUDIO_GAINS_SETTING_KEY, value_json=json.dumps(gains, ensure_ascii=False)))
    db.commit()
