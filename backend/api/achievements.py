"""Achievements: admin-defined criteria DSL + CRUD helpers.

Admins compose JSON criteria from a whitelist of stat keys (no game-logic code).
Validation is strict on write; evaluation lives in a later slice (game-end hook
+ on-read recompute).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import Achievement, Player, PlayerAchievement

# Whitelisted stat keys the evaluator will know how to compute over game_results.
# Keep this list in sync with the future stats catalog in the evaluator slice.
WHITELISTED_STATS: frozenset[str] = frozenset(
    {
        "games_played",
        "wins",
        "losses",
        "win_pct",
        "wins_as_good",
        "wins_as_evil",
        "games_as_good",
        "games_as_evil",
        "win_streak",
        "best_win_streak",
        "longest_game_turns",
        "longest_game_seconds",
    }
)

ALLOWED_OPS: frozenset[str] = frozenset({">=", "<=", ">", "<", "==", "!=", "="})

# game_context keys evaluated only at award time (game-end hook).
ALLOWED_GAME_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "won",
        "alliance",
        "scenario_id",
        "min_turns",
        "max_turns",
        "forfeited",
        "opponent_count",
        "min_duration_seconds",
    }
)

_KEY_RE = re.compile(r"^[a-z0-9_]{2,64}$")
_TIER_VALUES = frozenset({"bronze", "silver", "gold", "platinum", "diamond"})


class AchievementError(ValueError):
    """Invalid achievement payload (criteria, key, etc.)."""


def validate_criteria(raw: Any) -> dict[str, Any]:
    """Validate and normalize a criteria DSL document. Returns a plain dict.

    Shapes:
      - {"stat": "...", "op": ">=", "value": N}
      - {"all": [cond, ...]} | {"any": [cond, ...]} | {"not": cond}
      - optional "game_context": {...} on any node that is a map of qualifiers
    """
    if not isinstance(raw, dict):
        raise AchievementError("criteria must be a JSON object")
    return _validate_node(raw, path="criteria")


def _validate_node(node: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise AchievementError(f"{path}: must be an object")

    keys = set(node.keys())
    has_all = "all" in keys
    has_any = "any" in keys
    has_not = "not" in keys
    has_stat = "stat" in keys

    # Combinators
    if has_all or has_any or has_not:
        if has_stat:
            raise AchievementError(f"{path}: cannot mix combinator (all/any/not) with stat condition")
        if sum(1 for k in (has_all, has_any, has_not) if k) != 1:
            raise AchievementError(f"{path}: use exactly one of all, any, or not")

        out: dict[str, Any] = {}
        if has_all:
            children = node["all"]
            if not isinstance(children, list) or not children:
                raise AchievementError(f"{path}.all: must be a non-empty array")
            out["all"] = [_validate_node(c, path=f"{path}.all[{i}]") for i, c in enumerate(children)]
        elif has_any:
            children = node["any"]
            if not isinstance(children, list) or not children:
                raise AchievementError(f"{path}.any: must be a non-empty array")
            out["any"] = [_validate_node(c, path=f"{path}.any[{i}]") for i, c in enumerate(children)]
        else:  # not
            out["not"] = _validate_node(node["not"], path=f"{path}.not")

        unknown = keys - {"all", "any", "not", "game_context"}
        if unknown:
            raise AchievementError(f"{path}: unknown keys {sorted(unknown)}")
        if "game_context" in node:
            out["game_context"] = _validate_game_context(node["game_context"], path=f"{path}.game_context")
        return out

    # Leaf condition
    if not has_stat:
        raise AchievementError(f"{path}: expected all/any/not or a stat condition")
    unknown = keys - {"stat", "op", "value", "game_context"}
    if unknown:
        raise AchievementError(f"{path}: unknown keys {sorted(unknown)}")

    stat = node["stat"]
    if not isinstance(stat, str) or stat not in WHITELISTED_STATS:
        raise AchievementError(
            f"{path}.stat: must be one of {sorted(WHITELISTED_STATS)} (got {stat!r})"
        )

    op = node.get("op")
    if not isinstance(op, str) or op not in ALLOWED_OPS:
        raise AchievementError(f"{path}.op: must be one of {sorted(ALLOWED_OPS)} (got {op!r})")

    value = node.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AchievementError(f"{path}.value: must be a number")

    out = {"stat": stat, "op": op, "value": value}
    if "game_context" in node:
        out["game_context"] = _validate_game_context(node["game_context"], path=f"{path}.game_context")
    return out


def _validate_game_context(raw: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        raise AchievementError(f"{path}: must be a non-empty object")

    unknown = set(raw.keys()) - ALLOWED_GAME_CONTEXT_KEYS
    if unknown:
        raise AchievementError(f"{path}: unknown keys {sorted(unknown)} (allowed: {sorted(ALLOWED_GAME_CONTEXT_KEYS)})")

    out: dict[str, Any] = {}
    for key, val in raw.items():
        p = f"{path}.{key}"
        if key == "won":
            if not isinstance(val, bool):
                raise AchievementError(f"{p}: must be a boolean")
            out[key] = val
        elif key == "alliance":
            if val not in ("good", "evil"):
                raise AchievementError(f"{p}: must be 'good' or 'evil'")
            out[key] = val
        elif key == "scenario_id":
            if not isinstance(val, str) or not val.strip():
                raise AchievementError(f"{p}: must be a non-empty string")
            out[key] = val.strip()
        elif key == "forfeited":
            if not isinstance(val, bool):
                raise AchievementError(f"{p}: must be a boolean")
            out[key] = val
        elif key in ("min_turns", "max_turns", "min_duration_seconds"):
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise AchievementError(f"{p}: must be a non-negative integer")
            out[key] = val
        elif key == "opponent_count":
            if not isinstance(val, dict):
                raise AchievementError(f"{p}: must be {{op, value}}")
            op = val.get("op")
            value = val.get("value")
            if not isinstance(op, str) or op not in ALLOWED_OPS:
                raise AchievementError(f"{p}.op: must be one of {sorted(ALLOWED_OPS)}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AchievementError(f"{p}.value: must be a number")
            out[key] = {"op": op, "value": value}
        else:  # pragma: no cover - guarded by ALLOWED_GAME_CONTEXT_KEYS
            raise AchievementError(f"{p}: unsupported key")
    return out


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key.strip()):
        raise AchievementError("key must be 2-64 chars of lowercase letters, digits, underscores")
    return key.strip()


def validate_tier(tier: str | None) -> str | None:
    if tier is None or (isinstance(tier, str) and not tier.strip()):
        return None
    if not isinstance(tier, str) or tier.strip().lower() not in _TIER_VALUES:
        raise AchievementError(f"tier must be one of {sorted(_TIER_VALUES)}")
    return tier.strip().lower()


def achievement_to_dict(row: Achievement) -> dict[str, Any]:
    try:
        criteria = json.loads(row.criteria_json) if row.criteria_json else {}
    except (TypeError, json.JSONDecodeError):
        criteria = {}
    return {
        "id": row.id,
        "key": row.key,
        "name": row.name,
        "description": row.description or "",
        "icon": row.icon,
        "category": row.category,
        "tier": row.tier,
        "criteria": criteria,
        "sort_order": row.sort_order or 0,
        "active": bool(row.active),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_achievements(db: Session, *, include_inactive: bool = True) -> list[Achievement]:
    q = db.query(Achievement).order_by(Achievement.sort_order, Achievement.key)
    if not include_inactive:
        q = q.filter(Achievement.active.is_(True))
    return q.all()


def get_achievement(db: Session, achievement_id: str) -> Achievement | None:
    return db.query(Achievement).filter(Achievement.id == str(achievement_id)).first()


def create_achievement(
    db: Session,
    *,
    key: str,
    name: str,
    description: str = "",
    icon: str | None = None,
    category: str | None = None,
    tier: str | None = None,
    criteria: Any,
    sort_order: int = 0,
    active: bool = True,
) -> Achievement:
    key = validate_key(key)
    tier = validate_tier(tier)
    criteria = validate_criteria(criteria)
    if not isinstance(name, str) or not name.strip():
        raise AchievementError("name is required")
    if db.query(Achievement.id).filter(Achievement.key == key).first():
        raise AchievementError(f"achievement key already exists: {key}")

    row = Achievement(
        id=str(uuid.uuid4()),
        key=key,
        name=name.strip(),
        description=(description or "").strip(),
        icon=(icon or None),
        category=(category.strip() if isinstance(category, str) and category.strip() else None),
        tier=tier,
        criteria_json=json.dumps(criteria),
        sort_order=int(sort_order or 0),
        active=bool(active),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_achievement(db: Session, row: Achievement, changes: dict[str, Any]) -> Achievement:
    if "key" in changes and changes["key"] is not None:
        new_key = validate_key(changes["key"])
        if new_key != row.key:
            clash = (
                db.query(Achievement.id)
                .filter(Achievement.key == new_key, Achievement.id != row.id)
                .first()
            )
            if clash:
                raise AchievementError(f"achievement key already exists: {new_key}")
            row.key = new_key
    if "name" in changes and changes["name"] is not None:
        name = changes["name"]
        if not isinstance(name, str) or not name.strip():
            raise AchievementError("name is required")
        row.name = name.strip()
    if "description" in changes and changes["description"] is not None:
        row.description = str(changes["description"]).strip()
    if "icon" in changes:
        row.icon = changes["icon"] or None
    if "category" in changes:
        cat = changes["category"]
        row.category = cat.strip() if isinstance(cat, str) and cat.strip() else None
    if "tier" in changes:
        row.tier = validate_tier(changes["tier"])
    if "criteria" in changes and changes["criteria"] is not None:
        row.criteria_json = json.dumps(validate_criteria(changes["criteria"]))
    if "sort_order" in changes and changes["sort_order"] is not None:
        row.sort_order = int(changes["sort_order"])
    if "active" in changes and changes["active"] is not None:
        row.active = bool(changes["active"])
    db.commit()
    db.refresh(row)
    return row


def delete_achievement(db: Session, row: Achievement) -> None:
    """Delete definition and any grants (grants are orphan-safe to remove)."""
    db.query(PlayerAchievement).filter(PlayerAchievement.achievement_id == row.id).delete()
    db.delete(row)
    db.commit()


def list_granted(db: Session, *, player_id: str | None = None) -> list[dict[str, Any]]:
    """Granted rows for debugging (optionally filtered by player). Includes usernames."""
    q = (
        db.query(PlayerAchievement, Achievement, Player)
        .join(Achievement, PlayerAchievement.achievement_id == Achievement.id)
        .join(Player, PlayerAchievement.player_id == Player.id)
        .order_by(PlayerAchievement.awarded_at.desc())
    )
    if player_id:
        q = q.filter(PlayerAchievement.player_id == str(player_id))
    out: list[dict[str, Any]] = []
    for grant, ach, player in q.all():
        out.append(
            {
                "player_id": player.id,
                "username": player.username,
                "achievement_id": ach.id,
                "achievement_key": ach.key,
                "name": ach.name,
                "awarded_at": grant.awarded_at.isoformat() if grant.awarded_at else None,
            }
        )
    return out


def list_stats_catalog() -> list[dict[str, Any]]:
    """Whitelisted stats for admin UI dropdowns (and docs)."""
    return [{"stat": s, "label": s.replace("_", " ")} for s in sorted(WHITELISTED_STATS)]
