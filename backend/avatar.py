"""
Profile avatar defaults and the admin-configurable default avatar color.

Avatar lives in players.preferences JSON as {"image": ..., "color": ...}.
Players who never picked a color resolve to the profile_defaults AppSetting
value at response time, so admin changes affect only users who haven't chosen.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

DEFAULT_AVATAR_IMAGE = "hobbit.png"
DEFAULT_AVATAR_COLOR = "#2d5a27"
PROFILE_DEFAULTS_KEY = "profile_defaults"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9_.-]+\.png$")


def normalize_avatar_color(raw: object) -> str | None:
    """Lowercased #rrggbb, or None when not a valid 6-digit hex color."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not COLOR_RE.match(value):
        return None
    return value.lower()


def normalize_avatar_image(raw: object) -> str | None:
    """A plain .png filename, or None when invalid."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not IMAGE_RE.match(value):
        return None
    return value


def _read_setting(db: Session, key: str) -> object | None:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, key: str, value: object) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps(value, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=key, value_json=payload))


def load_default_avatar_color(db: Session | None) -> str:
    """Admin-configured default avatar color for users who never picked one."""
    if db is None:
        return DEFAULT_AVATAR_COLOR
    data = _read_setting(db, PROFILE_DEFAULTS_KEY)
    if not isinstance(data, dict):
        return DEFAULT_AVATAR_COLOR
    return normalize_avatar_color(data.get("default_avatar_color")) or DEFAULT_AVATAR_COLOR


def save_default_avatar_color(db: Session, color: str) -> str:
    """Persist the default avatar color; returns the normalized value."""
    normalized = normalize_avatar_color(color)
    if normalized is None:
        raise ValueError("default_avatar_color must be a #rrggbb hex value")
    _write_setting(db, PROFILE_DEFAULTS_KEY, {"default_avatar_color": normalized})
    db.commit()
    return normalized


def seed_profile_defaults_if_empty(db: Session) -> None:
    """Insert the profile_defaults AppSetting row when missing (never overwrites)."""
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == PROFILE_DEFAULTS_KEY).first():
        return
    _write_setting(db, PROFILE_DEFAULTS_KEY, {"default_avatar_color": DEFAULT_AVATAR_COLOR})
    db.commit()
