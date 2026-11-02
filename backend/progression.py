"""
Player XP and tiers.

The catalog (tier names, thresholds, avatar-frame assignments, and how much
each game event is worth) is one admin-edited JSON document. It lives in the
``progression`` app setting, or in ``backend/data/progression.json`` when
``SETUP_SOURCE=files``.

A player's tier is not stored. It is the highest tier whose ``min_xp`` is at
or below their XP, so editing a threshold re-tiers everyone immediately.

XP itself is a cached total on ``players.xp``. The ``xp_ledger`` table is the
source of truth: one ``game`` row per player per finished game (a unique key,
so a game cannot pay out twice) and ``admin_adjust`` rows for manual grants.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR
from backend.setup_data import is_files_setup_source

log = logging.getLogger(__name__)

PROGRESSION_PATH = DATA_DIR / "progression.json"
PROGRESSION_SETTING_KEY = "progression"
PROGRESSION_VERSION = 1
COMPUTER_PLAYER_ID = "__computer__"

MAX_TIERS = 30
MAX_FRAMES = 40
GAME_SOURCE = "game"
ADMIN_SOURCE = "admin_adjust"

STAT_KEYS = (
    "strongholds_captured",
    "territories_captured",
    "enemy_units_destroyed",
    "battles_won",
)
INT_RULES: dict[str, tuple[int, int]] = {
    "game_completed": (0, 100_000),
    "game_won": (0, 100_000),
    "stronghold_captured": (0, 100_000),
    "territory_captured": (0, 100_000),
    "enemy_unit_destroyed": (0, 100_000),
    "battle_won": (0, 100_000),
    "min_rounds_to_count": (0, 10_000),
    "stats_cap_per_game": (0, 1_000_000),
    "daily_cap": (0, 1_000_000),
}
FLOAT_RULES: dict[str, tuple[float, float]] = {
    "multiplayer_multiplier": (0.0, 10.0),
    "vs_ai_multiplier": (0.0, 10.0),
    "forfeit_multiplier": (0.0, 10.0),
}
GLOWS = ("none", "soft", "pulse")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FRAME_SRC_RE = re.compile(r"^/avatar-frames/[A-Za-z0-9][A-Za-z0-9_.+-]{0,80}$")
_HTTPS_RE = re.compile(r"^https://[^\s<>]{1,180}$")

DEFAULT_RULES: dict[str, int | float] = {
    "game_completed": 50,
    "game_won": 100,
    "stronghold_captured": 15,
    "territory_captured": 2,
    "enemy_unit_destroyed": 1,
    "battle_won": 3,
    "multiplayer_multiplier": 1.0,
    "vs_ai_multiplier": 0.5,
    "min_rounds_to_count": 3,
    "stats_cap_per_game": 300,
    "forfeit_multiplier": 0.0,
    "daily_cap": 0,
}

# min_xp is the whole progression. Names and thresholds are admin-editable.
_DEFAULT_TIER_SEED: list[tuple[str, str, int, str]] = [
    ("hobbit", "Hobbit", 0, "#8c6a3f"),
    ("wanderer", "Wanderer", 250, "#6b7c4a"),
    ("ranger", "Ranger", 750, "#3d6b4f"),
    ("warden", "Warden", 1500, "#2c6e9a"),
    ("captain", "Captain", 3000, "#8a3d3d"),
    ("lord", "Lord", 5000, "#c4a35a"),
    ("istar", "Istar", 8000, "#6b4c9a"),
]


class ProgressionError(ValueError):
    """Admin progression document failed validation."""


def _default_tiers() -> list[dict[str, Any]]:
    tiers: list[dict[str, Any]] = []
    for rank, (tid, name, min_xp, color) in enumerate(_DEFAULT_TIER_SEED, start=1):
        tiers.append({
            "id": tid,
            "name": name,
            "rank": rank,
            "min_xp": min_xp,
            "color": color,
            "icon": "",
            "description": "",
            "visual": {"frame_id": "", "ring_color": color, "glow": "none"},
        })
    return tiers


def default_progression() -> dict[str, Any]:
    return {
        "version": PROGRESSION_VERSION,
        "currency_label": "XP",
        "frames": [],
        "tiers": _default_tiers(),
        "icon_min_tier": {},
        "rules": dict(DEFAULT_RULES),
    }


def _clean_color(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    color = raw.strip()
    if not _COLOR_RE.match(color):
        return None
    if len(color) == 4:
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color.lower()


def _clean_text(raw: Any, *, max_len: int, allow_empty: bool) -> str | None:
    if raw is None and allow_empty:
        return ""
    if not isinstance(raw, str):
        return None
    text = re.sub(r"\s+", " ", raw).strip()
    if "<" in text or ">" in text:
        return None
    if not text and not allow_empty:
        return None
    if len(text) > max_len:
        return None
    return text


def _clean_src(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    src = raw.strip()
    if not src or ".." in src or "\\" in src or len(src) > 200:
        return None
    if _FRAME_SRC_RE.match(src) or _HTTPS_RE.match(src):
        return src
    return None


def _clean_int(raw: Any, lo: int, hi: int) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            return None
        raw = int(raw)
    if not isinstance(raw, int):
        return None
    if raw < lo or raw > hi:
        return None
    return raw


def _clean_multiplier(raw: Any, lo: float, hi: float) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    val = float(raw)
    if not math.isfinite(val) or val < lo or val > hi:
        return None
    return round(val, 3)


def _slug(label: str, used: set[str], prefix: str) -> str:
    base = _SLUG_RE.sub("_", label.lower()).strip("_")
    if not base or not base[0].isalpha():
        base = f"{prefix}_{base}" if base else prefix
    base = base[:32]
    candidate = base
    n = 2
    while candidate in used or not _ID_RE.match(candidate):
        suffix = f"_{n}"
        candidate = f"{base[: 32 - len(suffix)]}{suffix}"
        n += 1
        if n > 100:
            candidate = f"{prefix}_{len(used) + 1}"
            break
    return candidate


def _parse_rules(raw: Any) -> dict[str, int | float]:
    if not isinstance(raw, dict):
        raise ProgressionError("XP rules must be a set of numbers.")
    raw = dict(raw)
    # Older configs stored a flat forfeit grant. 0 meant no XP, which this multiplier keeps.
    # A positive flat grant has no equivalent multiplier, so it becomes 0 until an admin sets one.
    if "forfeit_multiplier" not in raw:
        raw["forfeit_multiplier"] = 0
    out: dict[str, int | float] = {}
    for key, (lo, hi) in INT_RULES.items():
        val = _clean_int(raw.get(key), lo, hi)
        if val is None:
            raise ProgressionError(f"Rule '{key}' must be a whole number from {lo} to {hi}.")
        out[key] = val
    for key, (lo, hi) in FLOAT_RULES.items():
        val = _clean_multiplier(raw.get(key), lo, hi)
        if val is None:
            raise ProgressionError(f"Rule '{key}' must be a number from {lo:g} to {hi:g}.")
        out[key] = val
    return out


def _parse_frames(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ProgressionError("Frames must be a list.")
    if len(raw) > MAX_FRAMES:
        raise ProgressionError(f"At most {MAX_FRAMES} frames.")
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProgressionError("Each frame needs a label and an image path.")
        label = _clean_text(item.get("label"), max_len=48, allow_empty=False)
        src = _clean_src(item.get("src"))
        if not label or not src:
            raise ProgressionError(
                "Each frame needs a label and an image path under /avatar-frames/ "
                "(for example /avatar-frames/bronze_ring.png) or an https URL."
            )
        raw_id = item.get("id")
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            fid = _slug(label, used, "frame")
        elif isinstance(raw_id, str) and _ID_RE.match(raw_id.strip()) and raw_id.strip() not in used:
            fid = raw_id.strip()
        else:
            raise ProgressionError(f"Frame '{label}' has a missing or duplicate id.")
        used.add(fid)
        out.append({"id": fid, "label": label, "src": src})
    return out


def _parse_tiers(raw: Any, frame_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ProgressionError("Add at least one tier.")
    if len(raw) > MAX_TIERS:
        raise ProgressionError(f"At most {MAX_TIERS} tiers.")
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ProgressionError("Each tier needs a name and a minimum XP.")
        name = _clean_text(item.get("name"), max_len=48, allow_empty=False)
        if not name:
            raise ProgressionError("Each tier needs a name (1–48 characters).")
        min_xp = _clean_int(item.get("min_xp"), 0, 100_000_000)
        if min_xp is None:
            raise ProgressionError(f"Tier '{name}' needs a minimum XP of 0 or more.")
        color = _clean_color(item.get("color"))
        if color is None:
            raise ProgressionError(f"Tier '{name}' needs a badge color.")
        icon = _clean_text(item.get("icon"), max_len=16, allow_empty=True)
        if icon is None:
            raise ProgressionError(f"Tier '{name}' has an icon that is too long.")
        description = _clean_text(item.get("description"), max_len=160, allow_empty=True)
        if description is None:
            raise ProgressionError(f"Tier '{name}' has a description that is too long.")
        raw_id = item.get("id")
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            tid = _slug(name, used, "tier")
        elif isinstance(raw_id, str) and _ID_RE.match(raw_id.strip()) and raw_id.strip() not in used:
            tid = raw_id.strip()
        else:
            raise ProgressionError(f"Tier '{name}' has a missing or duplicate id.")
        used.add(tid)
        visual_raw = item.get("visual") if isinstance(item.get("visual"), dict) else {}
        frame_id = visual_raw.get("frame_id") or ""
        if frame_id is None or frame_id == "":
            frame_id = ""
        elif not isinstance(frame_id, str) or not _ID_RE.match(frame_id.strip()):
            raise ProgressionError(f"Tier '{name}' has an invalid frame.")
        else:
            frame_id = frame_id.strip()
        if frame_id and frame_id not in frame_ids:
            raise ProgressionError(f"Tier '{name}' uses a frame that is not in the frame list.")
        ring = _clean_color(visual_raw.get("ring_color")) or color
        glow = visual_raw.get("glow") or "none"
        if glow not in GLOWS:
            raise ProgressionError(f"Tier '{name}' has an unknown ring glow. Use none, soft, or pulse.")
        out.append({
            "id": tid,
            "name": name,
            "min_xp": min_xp,
            "color": color,
            "icon": icon,
            "description": description,
            "visual": {"frame_id": frame_id, "ring_color": ring, "glow": glow},
        })
    out.sort(key=lambda t: (t["min_xp"], t["id"]))
    if out[0]["min_xp"] != 0:
        raise ProgressionError("The lowest tier must start at 0 XP.")
    for prev, cur in zip(out, out[1:]):
        if cur["min_xp"] <= prev["min_xp"]:
            raise ProgressionError(
                f"Tier thresholds must increase. '{cur['name']}' ({cur['min_xp']}) "
                f"is not above '{prev['name']}' ({prev['min_xp']})."
            )
    for rank, tier in enumerate(out, start=1):
        tier["rank"] = rank
    return out


def _parse_icon_min_tier(raw: Any, tier_ids: set[str], highest_id: str) -> dict[str, str]:
    """Filename -> tier id. Missing icons use the highest tier, so that tier is not stored."""
    from backend.avatar import IMAGE_RE

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProgressionError("Profile icon tiers must be an object of filename to tier id.")
    if len(raw) > 500:
        raise ProgressionError("At most 500 profile icon tier overrides.")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not IMAGE_RE.match(key.strip()):
            raise ProgressionError(f"Profile icon '{key}' must be a .png filename.")
        name = key.strip()
        if not isinstance(value, str) or value.strip() not in tier_ids:
            raise ProgressionError(f"Profile icon '{name}' uses an unknown tier.")
        tier_id = value.strip()
        if tier_id == highest_id:
            continue
        out[name] = tier_id
    return out


def normalize_progression(raw: Any) -> dict[str, Any]:
    """Validate and return a clean progression document. Raises ProgressionError."""
    if not isinstance(raw, dict):
        raise ProgressionError("Progression settings must be an object.")
    label = _clean_text(raw.get("currency_label") or "XP", max_len=24, allow_empty=False)
    if not label:
        raise ProgressionError("The XP label must be 1–24 characters.")
    frames = _parse_frames(raw.get("frames") if "frames" in raw else [])
    tiers = _parse_tiers(raw.get("tiers"), {f["id"] for f in frames})
    rules = _parse_rules(raw.get("rules"))
    icon_min_tier = _parse_icon_min_tier(
        raw.get("icon_min_tier"),
        {t["id"] for t in tiers},
        tiers[-1]["id"],
    )
    return {
        "version": PROGRESSION_VERSION,
        "currency_label": label,
        "frames": frames,
        "tiers": tiers,
        "icon_min_tier": icon_min_tier,
        "rules": rules,
    }


def _read_file() -> Any:
    if not PROGRESSION_PATH.is_file():
        return None
    try:
        with open(PROGRESSION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_file(cfg: dict[str, Any]) -> None:
    PROGRESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESSION_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_setting(db: Session) -> Any:
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == PROGRESSION_SETTING_KEY).first()
    if not row:
        return None
    try:
        return json.loads(row.value_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _write_setting(db: Session, cfg: dict[str, Any]) -> None:
    from backend.api.models import AppSetting

    payload = json.dumps(cfg, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == PROGRESSION_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=PROGRESSION_SETTING_KEY, value_json=payload))


def progression_from_stored(raw: Any) -> dict[str, Any]:
    if raw is None:
        return default_progression()
    try:
        return normalize_progression(raw)
    except ProgressionError:
        log.warning("Stored progression config is invalid; using defaults")
        return default_progression()


def load_progression(db: Session | None) -> dict[str, Any]:
    if is_files_setup_source() or db is None:
        return progression_from_stored(_read_file())
    data = _read_setting(db)
    if data is None:
        return progression_from_stored(_read_file())
    return progression_from_stored(data)


def save_progression(db: Session | None, raw: Any) -> dict[str, Any]:
    cfg = normalize_progression(raw)
    if is_files_setup_source() or db is None:
        _write_file(cfg)
        return cfg
    _write_setting(db, cfg)
    db.commit()
    return cfg


def seed_progression_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == PROGRESSION_SETTING_KEY).first():
        return
    _write_setting(db, load_progression(None))
    db.commit()


def present_tier(tier: dict[str, Any], frames: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Tier plus the resolved frame image, for clients that draw the profile circle."""
    visual = dict(tier.get("visual") or {})
    frame = frames.get(str(visual.get("frame_id") or ""))
    visual["frame_src"] = frame["src"] if frame else None
    visual["frame_label"] = frame["label"] if frame else None
    out = dict(tier)
    out["visual"] = visual
    return out


def _frames_by_id(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {f["id"]: f for f in cfg.get("frames") or [] if isinstance(f, dict) and f.get("id")}


def public_progression(db: Session | None) -> dict[str, Any]:
    """Tiers and frames only. XP amounts per event stay on the admin endpoint."""
    cfg = load_progression(db)
    frames = _frames_by_id(cfg)
    return {
        "currency_label": cfg["currency_label"],
        "frames": list(cfg["frames"]),
        "tiers": [present_tier(t, frames) for t in cfg["tiers"]],
    }


def tier_for_xp(xp: int, tiers: list[dict[str, Any]]) -> dict[str, Any] | None:
    current: dict[str, Any] | None = None
    xp = max(0, int(xp))
    for tier in tiers:
        if int(tier["min_xp"]) <= xp:
            current = tier
        else:
            break
    return current


def next_tier_for_xp(xp: int, tiers: list[dict[str, Any]]) -> dict[str, Any] | None:
    xp = max(0, int(xp))
    for tier in tiers:
        if int(tier["min_xp"]) > xp:
            return tier
    return None


def profile_avatar_choices(player: Any, db: Session | None) -> list[dict[str, Any]]:
    """Unit icons this player may use, with a flag for ones that unlock at their current tier.

    An icon with no saved tier uses the highest tier. The flag stays off for the lowest tier,
    since those icons are available from the start.
    """
    from backend.setup_data import list_all_unit_icons

    cfg = load_progression(db)
    tiers = cfg["tiers"]
    if not tiers:
        return []
    ranks = {str(t["id"]): int(t["rank"]) for t in tiers}
    lowest_rank = int(tiers[0]["rank"])
    highest_id = str(tiers[-1]["id"])
    highest_rank = int(tiers[-1]["rank"])
    current = tier_for_xp(int(getattr(player, "xp", 0) or 0), tiers)
    player_rank = int(current["rank"]) if current else 0
    unlocks = cfg.get("icon_min_tier") or {}
    choices: list[dict[str, Any]] = []
    for name in list_all_unit_icons(db):
        min_id = str(unlocks.get(name) or highest_id)
        min_rank = ranks.get(min_id, highest_rank)
        if min_rank > player_rank:
            continue
        choices.append({
            "name": name,
            "new": min_rank == player_rank and min_rank > lowest_rank,
        })
    return choices


def player_progression_view(player: Any, db: Session | None) -> dict[str, Any]:
    cfg = load_progression(db)
    frames = _frames_by_id(cfg)
    xp = int(getattr(player, "xp", 0) or 0)
    tier = tier_for_xp(xp, cfg["tiers"])
    nxt = next_tier_for_xp(xp, cfg["tiers"])
    return {
        "xp": xp,
        "tier": present_tier(tier, frames) if tier else None,
        "next_tier": present_tier(nxt, frames) if nxt else None,
        "tier_floor_xp": int(tier["min_xp"]) if tier else 0,
        "next_tier_xp": int(nxt["min_xp"]) if nxt else None,
    }


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def stronghold_ids_from_config(config: dict) -> set[str]:
    defs = config.get("definitions")
    if not isinstance(defs, dict):
        return set()
    territories = defs.get("territories")
    if not isinstance(territories, dict):
        return set()
    return {
        str(tid)
        for tid, raw in territories.items()
        if isinstance(raw, dict) and raw.get("is_stronghold")
    }


def alliances_from_config(config: dict) -> dict[str, str]:
    defs = config.get("definitions")
    if not isinstance(defs, dict):
        return {}
    factions = defs.get("factions")
    if not isinstance(factions, dict):
        return {}
    out: dict[str, str] = {}
    for fid, raw in factions.items():
        if isinstance(raw, dict):
            out[str(fid)] = str(raw.get("alliance") or "neutral")
    return out


def _event_parts(event: Any) -> tuple[str, dict]:
    if isinstance(event, dict):
        payload = event.get("payload")
        return str(event.get("type") or ""), payload if isinstance(payload, dict) else {}
    payload = getattr(event, "payload", None)
    return str(getattr(event, "type", "") or ""), payload if isinstance(payload, dict) else {}


def _bump(by: dict, faction: str, key: str, n: int) -> None:
    if not faction or n <= 0:
        return
    row = by.get(faction)
    if not isinstance(row, dict):
        row = {k: 0 for k in STAT_KEYS}
        by[faction] = row
    for stat in STAT_KEYS:
        row.setdefault(stat, 0)
    try:
        row[key] = int(row.get(key) or 0) + n
    except (TypeError, ValueError):
        row[key] = n


def _list_len(payload: dict, *keys: str) -> int:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, list):
            return len(raw)
    return 0


def fold_events(stats: dict, events: list, stronghold_ids: set[str]) -> dict:
    """Add capture, kill, and battle counts from these events onto ``stats``."""
    by = stats.get("by_faction")
    if not isinstance(by, dict):
        by = {}
        stats["by_faction"] = by
    for event in events:
        etype, payload = _event_parts(event)
        if payload.get("debug_only") is True:
            continue
        if etype == "territory_captured":
            owner = str(payload.get("new_owner") or "").strip()
            territory = str(payload.get("territory") or "")
            key = "strongholds_captured" if territory in stronghold_ids else "territories_captured"
            _bump(by, owner, key, 1)
        elif etype == "combat_ended":
            attacker = str(payload.get("attacker_faction") or "").strip()
            defender = str(payload.get("defender_faction") or "").strip()
            winner = payload.get("winner")
            if winner == "attacker":
                _bump(by, attacker, "battles_won", 1)
            elif winner == "defender":
                _bump(by, defender, "battles_won", 1)
            _bump(by, attacker, "enemy_units_destroyed", _list_len(
                payload, "defender_casualty_ids", "defender_casualties",
            ))
            _bump(by, defender, "enemy_units_destroyed", _list_len(
                payload, "attacker_casualty_ids", "attacker_casualties",
            ))
    return stats


def accumulate_xp_stats(config: dict, events: list) -> None:
    """Fold a save's new events into ``config['xp_stats']`` so the event-log cap cannot drop them."""
    if not isinstance(config, dict):
        return
    stats = config.get("xp_stats")
    if not isinstance(stats, dict):
        stats = {"by_faction": {}}
    fold_events(stats, events, stronghold_ids_from_config(config))
    config["xp_stats"] = stats


def human_factions(row: Any) -> dict[str, list[str]]:
    """player_id -> faction ids they controlled. Skips the computer placeholder."""
    players = _parse_json(getattr(row, "players", None))
    out: dict[str, list[str]] = {}
    if not isinstance(players, list):
        return out
    for entry in players:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("player_id") or "")
        fid = entry.get("faction_id")
        if not pid or pid == COMPUTER_PLAYER_ID:
            continue
        if fid is None or str(fid).strip() == "":
            continue
        faction = str(fid)
        bucket = out.setdefault(pid, [])
        if faction not in bucket:
            bucket.append(faction)
    return out


def _forfeited_ids(config: dict) -> set[str]:
    raw = config.get("forfeited_player_ids")
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if x}


def _forfeited_factions(config: dict, pid: str) -> list[str]:
    """Factions a removed player gave up. Stored ids win; older games only have reassignment notes."""
    recorded = config.get("forfeited_player_factions")
    raw = recorded.get(pid) if isinstance(recorded, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    fids: list[str] = []
    if isinstance(raw, list):
        for fid in raw:
            fid_s = str(fid).strip()
            if fid_s and fid_s not in fids:
                fids.append(fid_s)
    if fids:
        return fids
    from backend.api.game_results import _faction_ids_from_reassignments

    return _faction_ids_from_reassignments(config, pid)


def _stat_counts(faction_ids: list[str], by_faction: dict) -> dict[str, int]:
    counts = {k: 0 for k in STAT_KEYS}
    for fid in faction_ids:
        row = by_faction.get(fid)
        if not isinstance(row, dict):
            continue
        for key in STAT_KEYS:
            try:
                counts[key] += max(0, int(row.get(key) or 0))
            except (TypeError, ValueError):
                continue
    return counts


def score_player(
    *,
    faction_ids: list[str],
    alliances: dict[str, str],
    winner: str | None,
    forfeited: bool,
    vs_ai: bool,
    turn_number: int,
    by_faction: dict,
    rules: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """XP for one player before the daily cap. 0 means write no ledger row."""
    meta: dict[str, Any] = {
        "forfeited": forfeited,
        "vs_ai": vs_ai,
        "turn_number": turn_number,
        "won": False,
    }
    if turn_number < int(rules["min_rounds_to_count"]):
        meta["below_min_rounds"] = True
        return 0, meta
    won = bool(winner) and any(alliances.get(fid) == winner for fid in faction_ids)
    counts = _stat_counts(faction_ids, by_faction)
    stats_xp = (
        counts["strongholds_captured"] * int(rules["stronghold_captured"])
        + counts["territories_captured"] * int(rules["territory_captured"])
        + counts["enemy_units_destroyed"] * int(rules["enemy_unit_destroyed"])
        + counts["battles_won"] * int(rules["battle_won"])
    )
    stats_xp = min(stats_xp, int(rules["stats_cap_per_game"]))
    base = int(rules["game_completed"]) + (int(rules["game_won"]) if won else 0)
    multiplier = float(rules["vs_ai_multiplier"] if vs_ai else rules["multiplayer_multiplier"])
    if forfeited:
        meta["forfeit_multiplier"] = float(rules["forfeit_multiplier"])
        multiplier *= float(rules["forfeit_multiplier"])
    amount = int(round((base + stats_xp) * multiplier))
    meta.update({
        "won": won,
        "base_xp": base,
        "stats_xp": stats_xp,
        "multiplier": multiplier,
        "counts": counts,
    })
    return max(0, amount), meta


def _day_start(when: datetime) -> datetime:
    return when.replace(hour=0, minute=0, second=0, microsecond=0)


def _earned_on_day(db: Session, player_id: str, when: datetime) -> int:
    from backend.api.models import XpLedger

    start = _day_start(when)
    end = start + timedelta(days=1)
    rows = (
        db.query(XpLedger.amount)
        .filter(
            XpLedger.player_id == player_id,
            XpLedger.source == GAME_SOURCE,
            XpLedger.created_at >= start,
            XpLedger.created_at < end,
        )
        .all()
    )
    return sum(max(0, int(amount or 0)) for (amount,) in rows)


def _apply_daily_cap(
    db: Session,
    player_id: str,
    amount: int,
    meta: dict[str, Any],
    rules: dict[str, Any],
    when: datetime,
) -> int:
    cap = int(rules.get("daily_cap") or 0)
    if cap <= 0 or amount <= 0:
        return amount
    room = cap - _earned_on_day(db, player_id, when)
    if room < 0:
        room = 0
    if amount > room:
        meta["daily_cap_trimmed"] = amount - room
        return room
    return amount


def _insert_game_award(
    db: Session,
    player: Any,
    game_id: str,
    amount: int,
    meta: dict[str, Any],
    when: datetime,
) -> bool:
    """Insert the ledger row and add to the cached total. False if this game already paid this player."""
    from backend.api.models import XpLedger

    entry = XpLedger(
        id=str(uuid.uuid4()),
        player_id=player.id,
        game_id=game_id,
        source=GAME_SOURCE,
        amount=amount,
        meta_json=json.dumps(meta, ensure_ascii=False),
        created_at=when,
    )
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
            player.xp = int(player.xp or 0) + amount
        return True
    except IntegrityError:
        return False


def _config_dict(row: Any) -> dict:
    config = _parse_json(getattr(row, "config", None))
    return config if isinstance(config, dict) else {}


def _stats_for_award(row: Any, config: dict) -> dict:
    stats = config.get("xp_stats")
    if isinstance(stats, dict) and isinstance(stats.get("by_faction"), dict):
        return stats
    stats = {"by_faction": {}}
    log_entries = config.get("event_log")
    if isinstance(log_entries, list):
        fold_events(stats, log_entries, stronghold_ids_from_config(config))
    config["xp_stats"] = stats
    row.config = json.dumps(config)
    return stats


def award_game_xp(db: Session, row: Any, state: Any) -> list[dict[str, Any]]:
    """Award every human in a finished game. Safe to call again: the ledger key blocks a second payout."""
    from backend.api.models import Player

    config = _config_dict(row)
    stats = _stats_for_award(row, config)
    by_faction = stats.get("by_faction") if isinstance(stats.get("by_faction"), dict) else {}
    cfg = load_progression(db)
    rules = cfg["rules"]
    alliances = alliances_from_config(config)
    grouped = human_factions(row)
    forfeited = _forfeited_ids(config)
    # A forfeit removes the player from the roster. Their factions are kept so the
    # finished game can still pay the battles and units they earned, scaled by the forfeit multiplier.
    for pid in forfeited:
        if pid in grouped:
            continue
        fids = _forfeited_factions(config, pid)
        if fids:
            grouped[pid] = fids
    vs_ai = len(grouped) <= 1
    turn_number = int(getattr(state, "turn_number", 1) or 1)
    winner = getattr(state, "winner", None)
    when = getattr(row, "completed_at", None) or datetime.utcnow()
    if not isinstance(when, datetime):
        when = datetime.utcnow()
    awarded: list[dict[str, Any]] = []
    for pid, faction_ids in grouped.items():
        player = db.query(Player).filter(Player.id == pid).first()
        if player is None:
            continue
        amount, meta = score_player(
            faction_ids=faction_ids,
            alliances=alliances,
            winner=str(winner) if winner else None,
            forfeited=pid in forfeited,
            vs_ai=vs_ai,
            turn_number=turn_number,
            by_faction=by_faction,
            rules=rules,
        )
        amount = _apply_daily_cap(db, pid, amount, meta, rules, when)
        if amount <= 0:
            continue
        if _insert_game_award(db, player, str(row.id), amount, meta, when):
            awarded.append({"player_id": pid, "amount": amount, "xp": int(player.xp or 0)})
    return awarded


def adjust_player_xp(db: Session, player: Any, amount: int, reason: str, admin_id: str) -> int:
    """Add ``amount`` (negative to remove) and return the applied delta. XP never goes below 0."""
    from backend.api.models import XpLedger

    current = int(player.xp or 0)
    applied = amount
    if current + applied < 0:
        applied = -current
    if applied == 0:
        return 0
    db.add(XpLedger(
        id=str(uuid.uuid4()),
        player_id=player.id,
        game_id=None,
        source=ADMIN_SOURCE,
        amount=applied,
        meta_json=json.dumps(
            {"reason": reason, "admin_id": admin_id, "requested": amount},
            ensure_ascii=False,
        ),
        created_at=datetime.utcnow(),
    ))
    player.xp = current + applied
    return applied


def backfill_finished_games(db: Session) -> dict[str, int]:
    """Award XP for finished games that have none yet. Running it twice changes nothing."""
    from backend.api.models import Game
    from backend.engine.state import GameState

    rows = (
        db.query(Game)
        .filter(Game.status == "finished")
        .order_by(Game.completed_at.asc())
        .all()
    )
    awarded = 0
    xp = 0
    unreadable = 0
    for row in rows:
        raw = _parse_json(row.game_state)
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        try:
            state = GameState.from_dict(raw)
        except Exception:
            log.exception("Skipping unreadable game %s during XP backfill", row.id)
            unreadable += 1
            continue
        if not getattr(state, "winner", None):
            unreadable += 1
            continue
        for item in award_game_xp(db, row, state):
            awarded += 1
            xp += int(item["amount"])
    return {"games": len(rows), "awarded": awarded, "xp": xp, "unreadable": unreadable}
