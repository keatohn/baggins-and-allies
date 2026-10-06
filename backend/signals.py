"""
Cross-setup territory signals: preset callouts an alliance can pin on the map.

The catalog (labels, icons, which territories each preset applies to) is global,
like audio settings. Pins themselves live on the game and are visible only to
factions on the placing alliance.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR
from backend.setup_data import is_files_setup_source

SIGNALS_PATH = DATA_DIR / "signals.json"
SIGNALS_SETTING_KEY = "signals"
MAX_PRESETS = 24
APPLIES_TO = ("enemy", "allied", "neutral", "any")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SEA_RE = re.compile(r"^sea_zone_?\d+$", re.IGNORECASE)
_ICON_BAD = re.compile(r"[<>&\n\r\t]")
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DEFAULT_FLAG_COLOR = {
    "enemy": "#b4332a",
    "allied": "#2c6e9a",
    "neutral": "#c4922a",
    "any": "#6b5b4b",
}

DEFAULT_SIGNAL_PRESETS: list[dict[str, str]] = [
    {"id": "attack", "label": "Attack here", "icon": "⚔️", "applies_to": "enemy", "color": "#b4332a"},
    {"id": "reinforce", "label": "Reinforce here", "icon": "🛡️", "applies_to": "allied", "color": "#2c6e9a"},
    {"id": "conquer", "label": "Conquer here", "icon": "⚑", "applies_to": "neutral", "color": "#c4922a"},
]


class SignalPresetError(ValueError):
    """Admin catalog failed validation."""


def _clean_label(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    label = re.sub(r"\s+", " ", raw).strip()
    if not label or len(label) > 48 or "<" in label or ">" in label:
        return None
    return label


def _clean_icon(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    icon = raw.strip()
    if not icon or len(icon) > 16 or _ICON_BAD.search(icon):
        return None
    return icon


def _clean_color(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    color = raw.strip()
    if not _COLOR_RE.match(color):
        return None
    if len(color) == 4:
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color.lower()


def _slug_id(label: str, used: set[str]) -> str:
    base = _SLUG_RE.sub("_", label.lower()).strip("_")
    if not base or not base[0].isalpha():
        base = f"signal_{base}" if base else "signal"
    base = base[:32]
    candidate = base
    n = 2
    while candidate in used or not _ID_RE.match(candidate):
        suffix = f"_{n}"
        candidate = f"{base[: 32 - len(suffix)]}{suffix}"
        n += 1
        if n > 100:
            candidate = f"signal_{len(used) + 1}"
            break
    return candidate


def normalize_signal_presets(raw: Any) -> list[dict[str, str]]:
    """Keep valid presets. A bare list or ``{"presets": [...]}`` are both accepted."""
    if isinstance(raw, dict):
        raw = raw.get("presets")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = _clean_label(item.get("label"))
        icon = _clean_icon(item.get("icon"))
        applies = item.get("applies_to")
        if not label or not icon or applies not in APPLIES_TO:
            continue
        preset_id = item.get("id")
        if isinstance(preset_id, str) and _ID_RE.match(preset_id.strip()) and preset_id.strip() not in used:
            pid = preset_id.strip()
        else:
            pid = _slug_id(label, used)
        used.add(pid)
        out.append({
            "id": pid,
            "label": label,
            "icon": icon,
            "applies_to": applies,
            "color": _clean_color(item.get("color")) or _DEFAULT_FLAG_COLOR[applies],
        })
        if len(out) >= MAX_PRESETS:
            break
    return out


def _read_file() -> Any:
    if not SIGNALS_PATH.is_file():
        return None
    try:
        with open(SIGNALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_file(presets: list[dict[str, str]]) -> None:
    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_PATH, "w", encoding="utf-8") as f:
        json.dump({"presets": presets}, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_setting(db: Session) -> Any:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == SIGNALS_SETTING_KEY).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, presets: list[dict[str, str]]) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps(presets, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == SIGNALS_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=SIGNALS_SETTING_KEY, value_json=payload))


def _presets_from_stored(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return [dict(p) for p in DEFAULT_SIGNAL_PRESETS]
    presets = normalize_signal_presets(raw)
    if not presets and raw not in ([], {"presets": []}):
        return [dict(p) for p in DEFAULT_SIGNAL_PRESETS]
    return presets


def load_signal_presets(db: Session | None) -> list[dict[str, str]]:
    if is_files_setup_source() or db is None:
        return _presets_from_stored(_read_file())
    data = _read_setting(db)
    if data is None:
        return _presets_from_stored(_read_file())
    return _presets_from_stored(data)


def _preset_fields_ok(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    color = item.get("color")
    color_ok = color is None or color == "" or _clean_color(color) is not None
    return bool(
        _clean_label(item.get("label"))
        and _clean_icon(item.get("icon"))
        and item.get("applies_to") in APPLIES_TO
        and color_ok
    )


def save_signal_presets(db: Session | None, raw: Any) -> list[dict[str, str]]:
    """Replace the catalog. Invalid entries are an error so an admin edit is not silently dropped."""
    source = raw.get("presets") if isinstance(raw, dict) else raw
    if not isinstance(source, list):
        raise SignalPresetError("Signals must be a list.")
    if len(source) > MAX_PRESETS:
        raise SignalPresetError(f"At most {MAX_PRESETS} signals.")
    if any(not _preset_fields_ok(item) for item in source):
        raise SignalPresetError(
            "One or more signals are invalid. Each needs a label (1–48 characters), "
            "a short icon, a flag color, and where it applies: enemy, allied, neutral, or any."
        )
    presets = normalize_signal_presets(source)
    if is_files_setup_source() or db is None:
        _write_file(presets)
        return presets
    _write_setting(db, presets)
    db.commit()
    return presets


def seed_signals_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == SIGNALS_SETTING_KEY).first():
        return
    _write_setting(db, load_signal_presets(None))
    db.commit()


def classify_territory_for_signal(
    *,
    owner: str | None,
    territory_id: str,
    ownable: bool,
    terrain_type: str | None,
    my_alliance: str,
    owner_alliance: str | None,
) -> str | None:
    """
    ``enemy`` / ``allied`` / ``neutral``, or None when the territory is not a
    land target (sea, unownable). ``any`` presets still match a None relation.
    """
    terrain = (terrain_type or "").strip().lower()
    if not ownable or terrain in ("sea", "river") or _SEA_RE.match(territory_id or ""):
        return None
    owner_name = (owner or "").strip()
    if not owner_name or owner_name.lower() in ("neutral", "none"):
        return "neutral"
    oa = (owner_alliance or "").strip()
    if not oa or oa == "neutral":
        return "neutral"
    if oa == my_alliance:
        return "allied"
    return "enemy"


def signal_applies(preset: dict[str, str], relation: str | None) -> bool:
    target = preset.get("applies_to")
    if target == "any":
        return True
    return relation is not None and target == relation


def apply_territory_signal(
    state: Any,
    *,
    alliance: str,
    territory_id: str,
    faction_id: str,
    preset: dict[str, str] | None,
    clear: bool = False,
) -> str:
    """Pin ``preset`` for ``alliance``, or clear it. Sending the same preset again clears it."""
    book = state.territory_signals.setdefault(alliance, {})
    current = book.get(territory_id)
    same = (
        preset is not None
        and isinstance(current, dict)
        and current.get("preset_id") == preset["id"]
    )
    if clear or preset is None or same:
        book.pop(territory_id, None)
        if not book:
            state.territory_signals.pop(alliance, None)
        return "cleared"
    book[territory_id] = {
        "preset_id": preset["id"],
        "label": preset["label"],
        "icon": preset["icon"],
        "color": preset.get("color") or "#6b5b4b",
        "faction_id": faction_id,
    }
    return "set"


def visible_territory_signals(state: Any, alliances: set[str]) -> dict[str, dict[str, str]]:
    """Flat territory_id -> pin, only for the given alliances."""
    out: dict[str, dict[str, str]] = {}
    stored = getattr(state, "territory_signals", None) or {}
    if not isinstance(stored, dict):
        return out
    for alliance in sorted(alliances):
        book = stored.get(alliance)
        if not isinstance(book, dict):
            continue
        for tid, sig in book.items():
            if not isinstance(sig, dict):
                continue
            out[str(tid)] = {
                "preset_id": str(sig.get("preset_id") or ""),
                "label": str(sig.get("label") or ""),
                "icon": str(sig.get("icon") or ""),
                "color": str(sig.get("color") or "#6b5b4b"),
                "faction_id": str(sig.get("faction_id") or ""),
                "alliance": alliance,
            }
    return out
