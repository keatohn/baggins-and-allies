"""Generic achievement evaluator: whitelist stats + criteria DSL + awarding.

Two hooks, deliberately small:
  1. Game end — called from save_game after game_results rows are written.
     Evaluates every active achievement for each player in the finished game.
  2. On-read recompute — called from the profile/stats endpoint. Safety net for
     achievements whose inputs changed outside game end (definition edits,
     backfill). Only evaluates stats-only criteria (no game_context), since
     game_context needs a specific triggering game.

Admins never write game-logic code — they compose JSON criteria (see
achievements.py). Unknown stat keys are rejected at write time by the same
WHITELISTED_STATS catalog used here.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .achievements import WHITELISTED_STATS, list_achievements
from .models import Achievement, GameResult, PlayerAchievement


def _parse_participants(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and str(x).strip() != ""]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x is not None and str(x).strip() != ""]
    return []


def _aggregate_by_game(rows: list[GameResult]) -> list[dict[str, Any]]:
    """Collapse multi-faction rows (same game+player) into one logical game entry."""
    games: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.game_id), str(row.player_id))
        entry = games.get(key)
        if entry is None:
            games[key] = {
                "game_id": str(row.game_id),
                "won": bool(row.won),
                "forfeited": bool(row.forfeited),
                "alliance": str(row.alliance) if row.alliance else None,
                "turn_count": int(row.turn_count) if row.turn_count is not None else None,
                "duration_seconds": int(row.duration_seconds) if row.duration_seconds is not None else None,
                "created_at": row.created_at,
            }
        else:
            entry["won"] = entry["won"] or bool(row.won)
            entry["forfeited"] = entry["forfeited"] or bool(row.forfeited)
            if row.alliance and not entry["alliance"]:
                entry["alliance"] = str(row.alliance)
    ordered = sorted(
        games.values(),
        key=lambda g: (g["created_at"] is None, g["created_at"] or datetime.min),
    )
    return ordered


def _streaks(counted: list[dict[str, Any]]) -> dict[str, int]:
    current_win = 0
    best_win = 0
    for g in counted:
        if g["won"]:
            current_win += 1
            best_win = max(best_win, current_win)
        else:
            current_win = 0
    return {"current_win": current_win, "best_win_streak": best_win}


def compute_achievement_stats(db: Session, player_id: str) -> dict[str, Any]:
    """Compute every whitelisted stat value for a player from game_results."""
    player_id = str(player_id)
    rows = (
        db.query(GameResult)
        .filter(GameResult.player_id == player_id)
        .all()
    )
    games = _aggregate_by_game(rows)
    n = len(games)
    wins = sum(1 for g in games if g["won"])
    by_alliance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in games:
        if g["alliance"] in ("good", "evil"):
            by_alliance[g["alliance"]].append(g)
    streaks = _streaks(games)
    turns = [g["turn_count"] for g in games if g["turn_count"] is not None]
    durations = [g["duration_seconds"] for g in games if g["duration_seconds"] is not None]
    return {
        "games_played": n,
        "wins": wins,
        "losses": n - wins,
        "win_pct": round(100.0 * wins / n, 1) if n else 0.0,
        "wins_as_good": sum(1 for g in by_alliance["good"] if g["won"]),
        "wins_as_evil": sum(1 for g in by_alliance["evil"] if g["won"]),
        "games_as_good": len(by_alliance["good"]),
        "games_as_evil": len(by_alliance["evil"]),
        "win_streak": streaks["current_win"],
        "best_win_streak": streaks["best_win_streak"],
        "longest_game_turns": max(turns) if turns else 0,
        "longest_game_seconds": max(durations) if durations else 0,
    }


def _apply_op(op: str, left: float, right: float) -> bool:
    if op in (">=",):
        return left >= right
    if op in ("<=",):
        return left <= right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op in ("==", "="):
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"unsupported op: {op}")


def _match_game_context(ctx: dict[str, Any], game_row: dict[str, Any]) -> bool:
    """Check a validated game_context against a single game_results-derived row."""
    for key, expected in ctx.items():
        if key == "won":
            if bool(game_row["won"]) != bool(expected):
                return False
        elif key == "alliance":
            if game_row["alliance"] != expected:
                return False
        elif key == "scenario_id":
            if game_row.get("setup_id") != expected:
                return False
        elif key == "min_turns":
            tc = game_row.get("turn_count")
            if tc is None or tc < expected:
                return False
        elif key == "max_turns":
            tc = game_row.get("turn_count")
            if tc is None or tc > expected:
                return False
        elif key == "forfeited":
            if bool(game_row["forfeited"]) != bool(expected):
                return False
        elif key == "opponent_count":
            if not _apply_op(expected["op"], game_row.get("opponent_count", 0), expected["value"]):
                return False
        elif key == "min_duration_seconds":
            dur = game_row.get("duration_seconds")
            if dur is None or dur < expected:
                return False
        else:  # pragma: no cover - guarded by ALLOWED_GAME_CONTEXT_KEYS at write time
            return False
    return True


def _evaluate_node(
    node: dict[str, Any],
    stats: dict[str, Any],
    game_row: dict[str, Any] | None,
) -> bool:
    """Evaluate one criteria DSL node against stats (+ optional game_row context)."""
    if "all" in node:
        ok = all(_evaluate_node(c, stats, game_row) for c in node["all"])
    elif "any" in node:
        ok = any(_evaluate_node(c, stats, game_row) for c in node["any"])
    elif "not" in node:
        ok = not _evaluate_node(node["not"], stats, game_row)
    else:  # leaf stat condition
        left = stats.get(node["stat"], 0)
        ok = _apply_op(node["op"], left, node["value"])
    if ok and node.get("game_context"):
        if game_row is None:
            return False
        ok = _match_game_context(node["game_context"], game_row)
    return ok


def evaluate_criteria(
    criteria: dict[str, Any],
    stats: dict[str, Any],
    game_row: dict[str, Any] | None = None,
) -> bool:
    """Evaluate a validated criteria DSL document.

    game_row: optional game_results-derived dict for game_context qualifiers.
    When game_row is None (on-read), any node with game_context evaluates False.
    """
    return _evaluate_node(criteria, stats, game_row)


def _criteria_has_game_context(node: dict[str, Any]) -> bool:
    if node.get("game_context"):
        return True
    if "all" in node:
        return any(_criteria_has_game_context(c) for c in node["all"])
    if "any" in node:
        return any(_criteria_has_game_context(c) for c in node["any"])
    if "not" in node:
        return _criteria_has_game_context(node["not"])
    return False


def _game_row_from_result(row: GameResult) -> dict[str, Any]:
    """Build a game_context-ready dict from one GameResult row."""
    participants = _parse_participants(row.participant_ids)
    return {
        "won": bool(row.won),
        "alliance": str(row.alliance) if row.alliance else None,
        "setup_id": str(row.setup_id) if row.setup_id else None,
        "turn_count": int(row.turn_count) if row.turn_count is not None else None,
        "forfeited": bool(row.forfeited),
        "opponent_count": max(0, len(participants) - 1),
        "duration_seconds": int(row.duration_seconds) if row.duration_seconds is not None else None,
    }


def _already_awarded_ids(db: Session, player_id: str) -> set[str]:
    rows = (
        db.query(PlayerAchievement.achievement_id)
        .filter(PlayerAchievement.player_id == str(player_id))
        .all()
    )
    return {str(r[0]) for r in rows}


def evaluate_and_award(
    db: Session,
    player_id: str,
    *,
    game_row: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> list[Achievement]:
    """Evaluate active achievements for a player; award newly met ones.

    game_row: when provided (game-end hook), game_context qualifiers are matched
    against it. When None (on-read), achievements whose criteria contain any
    game_context are skipped entirely (they can only be awarded at game end).

    Returns the Achievement rows newly awarded (flushed, not committed).
    """
    player_id = str(player_id)
    active = list_achievements(db, include_inactive=False)
    if not active:
        return []
    awarded = _already_awarded_ids(db, player_id)
    if stats is None:
        stats = compute_achievement_stats(db, player_id)

    newly: list[Achievement] = []
    for ach in active:
        if str(ach.id) in awarded:
            continue
        try:
            criteria = json.loads(ach.criteria_json) if ach.criteria_json else None
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(criteria, dict):
            continue
        if game_row is None and _criteria_has_game_context(criteria):
            continue  # game-end-only; cannot award on-read
        try:
            if evaluate_criteria(criteria, stats, game_row):
                newly.append(ach)
        except Exception:
            continue  # never let one bad definition block the rest

    now = datetime.utcnow()
    for ach in newly:
        db.add(
            PlayerAchievement(
                id=str(uuid.uuid4()),
                player_id=player_id,
                achievement_id=str(ach.id),
                awarded_at=now,
            )
        )
        awarded.add(str(ach.id))
    if newly:
        db.flush()
    return newly


def evaluate_for_game(db: Session, game_id: str) -> list[dict[str, Any]]:
    """Game-end hook: evaluate achievements for every player in a finished game.

    Queries the just-written game_results rows for game_id. For each player,
    evaluates all active achievements against their full stats (which include
    this game) with each of their game rows as context (multi-faction players
    get one evaluation per row; award at most once per player per call).

    Returns a list of {"player_id", "achievement_id", "key", "name"} awards.
    """
    rows = (
        db.query(GameResult)
        .filter(GameResult.game_id == str(game_id))
        .all()
    )
    if not rows:
        return []
    by_player: dict[str, list[GameResult]] = defaultdict(list)
    for row in rows:
        by_player[str(row.player_id)].append(row)

    awards: list[dict[str, Any]] = []
    for player_id, player_rows in by_player.items():
        try:
            stats = compute_achievement_stats(db, player_id)
        except Exception as e:
            print(f"[achievements] stats failed for player {player_id}: {e}", flush=True)
            continue
        awarded_ids = _already_awarded_ids(db, player_id)
        for row in player_rows:
            game_row = _game_row_from_result(row)
            try:
                newly = evaluate_and_award(
                    db, player_id, game_row=game_row, stats=stats
                )
            except Exception as e:
                print(
                    f"[achievements] eval failed for player {player_id} game {game_id}: {e}",
                    flush=True,
                )
                continue
            for ach in newly:
                if str(ach.id) not in awarded_ids:
                    awards.append(
                        {
                            "player_id": player_id,
                            "achievement_id": str(ach.id),
                            "key": ach.key,
                            "name": ach.name,
                        }
                    )
                    awarded_ids.add(str(ach.id))
    return awards


def recompute_for_player(db: Session, player_id: str) -> list[Achievement]:
    """On-read hook: award any stats-only achievements the player now qualifies for.

    Skips achievements whose criteria contain game_context (those are game-end
    only). Safe to call on every profile read — already-awarded are skipped.
    """
    try:
        return evaluate_and_award(db, player_id, game_row=None)
    except Exception as e:
        print(f"[achievements] on-read recompute failed for player {player_id}: {e}", flush=True)
        return []


def player_achievement_grants(db: Session, player_id: str) -> list[dict[str, Any]]:
    """Awarded achievements with definition details, for the profile response."""
    q = (
        db.query(PlayerAchievement, Achievement)
        .join(Achievement, PlayerAchievement.achievement_id == Achievement.id)
        .filter(PlayerAchievement.player_id == str(player_id))
        .order_by(Achievement.sort_order, Achievement.key)
    )
    out: list[dict[str, Any]] = []
    for grant, ach in q.all():
        try:
            criteria = json.loads(ach.criteria_json) if ach.criteria_json else {}
        except (TypeError, json.JSONDecodeError):
            criteria = {}
        out.append(
            {
                "id": str(ach.id),
                "key": ach.key,
                "name": ach.name,
                "description": ach.description or "",
                "icon": ach.icon,
                "category": ach.category,
                "tier": ach.tier,
                "criteria": criteria,
                "sort_order": ach.sort_order or 0,
                "awarded_at": grant.awarded_at.isoformat() if grant.awarded_at else None,
            }
        )
    return out
