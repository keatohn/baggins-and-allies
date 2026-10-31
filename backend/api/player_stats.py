"""Aggregate finished-game stats from game_results for profile pages.

Headline W-L excludes forfeited games (kept as a separate abandoned count).
All filters are SQL against game_results; scenario responses use the display-name
snapshot stored at game end.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import GameResult, Player


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


def _win_pct(wins: int, games: int) -> float | None:
    if games <= 0:
        return None
    return round(100.0 * wins / games, 1)


def _game_key(row: GameResult) -> tuple[str, str]:
    return (str(row.game_id), str(row.player_id))


def _apply_filters(
    query,
    *,
    player_id: str,
    scenario: str | None,
    alliance: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    query = query.filter(GameResult.player_id == str(player_id))
    if scenario:
        query = query.filter(GameResult.setup_id == scenario)
    if alliance:
        query = query.filter(GameResult.alliance == alliance)
    if date_from is not None:
        query = query.filter(GameResult.created_at >= date_from)
    if date_to is not None:
        query = query.filter(GameResult.created_at <= date_to)
    return query


def _filter_game_ids_by_peer(
    db: Session,
    game_ids: list[str],
    *,
    subject_id: str,
    peer_id: str,
    kind: str,
) -> set[str]:
    """Keep game_ids where peer_id played against (different alliance) or with (same alliance) subject.

    kind: "against" | "with". Both rows must exist for subject and peer in the game.
    """
    if not game_ids or not peer_id or peer_id == subject_id:
        return set()
    rows = (
        db.query(GameResult.game_id, GameResult.player_id, GameResult.alliance)
        .filter(GameResult.game_id.in_(game_ids))
        .filter(GameResult.player_id.in_([str(subject_id), str(peer_id)]))
        .all()
    )
    by_game: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for game_id, pid, alliance in rows:
        by_game[str(game_id)][str(pid)].add(str(alliance))
    keep: set[str] = set()
    for game_id, players in by_game.items():
        subject_alliances = players.get(str(subject_id)) or set()
        peer_alliances = players.get(str(peer_id)) or set()
        if not subject_alliances or not peer_alliances:
            continue
        # Multi-alliance edge case: treat as against if any alliance differs.
        overlaps = bool(subject_alliances & peer_alliances)
        if kind == "with" and overlaps:
            keep.add(game_id)
        elif kind == "against" and not overlaps:
            keep.add(game_id)
    return keep


def _aggregate_by_game(rows: list[GameResult]) -> list[dict[str, Any]]:
    """Collapse multi-faction rows (same game+player) into one logical game entry."""
    games: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = _game_key(row)
        entry = games.get(key)
        if entry is None:
            games[key] = {
                "game_id": str(row.game_id),
                "won": bool(row.won),
                "forfeited": bool(row.forfeited),
                "alliance": str(row.alliance) if row.alliance else None,
                "setup_id": str(row.setup_id) if row.setup_id else None,
                "scenario_name": str(row.scenario_name) if row.scenario_name else None,
                "turn_count": int(row.turn_count) if row.turn_count is not None else None,
                "duration_seconds": int(row.duration_seconds) if row.duration_seconds is not None else None,
                "rings_of_power": bool(row.rings_of_power),
                "shadow_of_war": bool(row.shadow_of_war),
                "created_at": row.created_at,
                "participant_ids": _parse_participants(row.participant_ids),
                "faction_ids": {str(row.faction_id)} if row.faction_id else set(),
            }
        else:
            entry["won"] = entry["won"] or bool(row.won)
            entry["forfeited"] = entry["forfeited"] or bool(row.forfeited)
            if row.alliance and not entry["alliance"]:
                entry["alliance"] = str(row.alliance)
            if row.faction_id:
                entry["faction_ids"].add(str(row.faction_id))
    # Chronological order for streaks
    ordered = sorted(
        games.values(),
        key=lambda g: (g["created_at"] is None, g["created_at"] or datetime.min),
    )
    return ordered


def _streaks(counted: list[dict[str, Any]]) -> dict[str, int]:
    current_win = 0
    current_loss = 0
    best_win = 0
    best_loss = 0
    for g in counted:
        if g["won"]:
            current_win += 1
            current_loss = 0
            best_win = max(best_win, current_win)
        else:
            current_loss += 1
            current_win = 0
            best_loss = max(best_loss, current_loss)
    return {
        "current_win": current_win,
        "current_loss": current_loss,
        "best_win_streak": best_win,
        "best_loss_streak": best_loss,
    }


def compute_player_stats(
    db: Session,
    player_id: str,
    *,
    scenario: str | None = None,
    opponent: str | None = None,
    teammate: str | None = None,
    alliance: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Compute profile stats for a player from game_results.

    Filters:
      scenario: setup_id match
      opponent: only games where this player faced `opponent` (different alliance)
      teammate: only games where this player was on the same alliance as `teammate`
      alliance: only games where the subject played this alliance
      date_from/date_to: on game_results.created_at (game end time)
    """
    player_id = str(player_id)

    # Base rows for the subject (before peer filters, which need whole-game context).
    base_query = db.query(GameResult).filter(GameResult.player_id == player_id)
    if scenario:
        base_query = base_query.filter(GameResult.setup_id == scenario)
    if alliance:
        base_query = base_query.filter(GameResult.alliance == alliance)
    if date_from is not None:
        base_query = base_query.filter(GameResult.created_at >= date_from)
    if date_to is not None:
        base_query = base_query.filter(GameResult.created_at <= date_to)
    base_rows = base_query.all()

    if not base_rows and not (opponent or teammate):
        return _empty_stats(player_id)

    subject_game_ids = sorted({str(r.game_id) for r in base_rows})

    # Peer filters: narrow to games where the named peer was opponent/teammate.
    if opponent:
        keep = _filter_game_ids_by_peer(
            db, subject_game_ids, subject_id=player_id, peer_id=str(opponent), kind="against"
        )
        base_rows = [r for r in base_rows if str(r.game_id) in keep]
    if teammate:
        keep = _filter_game_ids_by_peer(
            db, subject_game_ids, subject_id=player_id, peer_id=str(teammate), kind="with"
        )
        base_rows = [r for r in base_rows if str(r.game_id) in keep]

    # Opponent/teammate breakdown needs every human row in the counted games.
    counted_game_ids = sorted({str(r.game_id) for r in base_rows if not r.forfeited})
    peer_rows: list[GameResult] = []
    if counted_game_ids:
        peer_rows = (
            db.query(GameResult)
            .filter(GameResult.game_id.in_(counted_game_ids))
            .filter(GameResult.player_id != player_id)
            .all()
        )

    games = _aggregate_by_game(base_rows)
    counted = [g for g in games if not g["forfeited"]]
    forfeited_count = sum(1 for g in games if g["forfeited"])

    wins = sum(1 for g in counted if g["won"])
    games_played = len(counted)

    by_alliance: dict[str, dict[str, Any]] = {}
    for alliance_key in ("good", "evil"):
        subset = [g for g in counted if g["alliance"] == alliance_key]
        a_wins = sum(1 for g in subset if g["won"])
        by_alliance[alliance_key] = {
            "games": len(subset),
            "wins": a_wins,
            "win_pct": _win_pct(a_wins, len(subset)),
        }

    scenario_totals: dict[str, dict[str, Any]] = {}
    for g in counted:
        sid = g["setup_id"] or ""
        bucket = scenario_totals.get(sid)
        if bucket is None:
            bucket = {
                "setup_id": g["setup_id"],
                "scenario_name": g["scenario_name"],
                "games": 0,
                "wins": 0,
            }
            scenario_totals[sid] = bucket
        bucket["games"] += 1
        if g["won"]:
            bucket["wins"] += 1
    by_scenario = sorted(
        (
            {
                **b,
                "scenario_name": b["scenario_name"] or (b["setup_id"] or "Unknown"),
                "win_pct": _win_pct(b["wins"], b["games"]),
            }
            for b in scenario_totals.values()
        ),
        key=lambda b: (-b["games"], b["scenario_name"] or ""),
    )

    # Head-to-head vs/with each other human, from peer rows in counted games.
    subject_alliance_by_game = {g["game_id"]: g["alliance"] for g in counted}
    subject_win_by_game = {g["game_id"]: g["won"] for g in counted}
    h2h: dict[str, dict[str, Any]] = {}
    for row in peer_rows:
        game_id = str(row.game_id)
        if game_id not in subject_alliance_by_game:
            continue
        other_id = str(row.player_id)
        subj_a = subject_alliance_by_game.get(game_id)
        other_a = str(row.alliance) if row.alliance else None
        if subj_a is None or other_a is None:
            continue
        kind = "with" if subj_a == other_a else "against"
        bucket = h2h.get(other_id)
        if bucket is None:
            bucket = {
                "player_id": other_id,
                "kind": kind,
                "games": 0,
                "wins": 0,
                "faction_ids": set(),
            }
            h2h[other_id] = bucket
        elif bucket["kind"] != kind:
            # Same peer across multiple games with different relations: prefer "against".
            bucket["kind"] = "against"
        bucket["games"] += 1
        if subject_win_by_game.get(game_id):
            bucket["wins"] += 1
        if row.faction_id:
            bucket["faction_ids"].add(str(row.faction_id))

    usernames = _usernames_for(db, list(h2h.keys()))
    head_to_head = sorted(
        (
            {
                "player_id": pid,
                "username": usernames.get(pid),
                "kind": b["kind"],
                "games": b["games"],
                "wins": b["wins"],
                "losses": b["games"] - b["wins"],
                "win_pct": _win_pct(b["wins"], b["games"]),
            }
            for pid, b in h2h.items()
        ),
        key=lambda e: (-e["games"], e["username"] or e["player_id"]),
    )

    faction_counts: dict[str, int] = defaultdict(int)
    for g in counted:
        for fid in g["faction_ids"]:
            faction_counts[fid] += 1
    favorite_faction = None
    if faction_counts:
        favorite_faction = max(faction_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    longest_game = max(
        (g["duration_seconds"] for g in counted if g["duration_seconds"] is not None),
        default=None,
    )
    most_played_scenario = by_scenario[0] if by_scenario else None

    streak_data = _streaks(counted)

    return {
        "player_id": player_id,
        "overall": {
            "games": games_played,
            "wins": wins,
            "losses": games_played - wins,
            "win_pct": _win_pct(wins, games_played),
            "forfeits": forfeited_count,
        },
        "by_alliance": by_alliance,
        "by_scenario": by_scenario,
        "head_to_head": head_to_head,
        "streaks": streak_data,
        "most_played_scenario": (
            {
                "setup_id": most_played_scenario["setup_id"],
                "scenario_name": most_played_scenario["scenario_name"],
                "games": most_played_scenario["games"],
            }
            if most_played_scenario
            else None
        ),
        "favorite_faction": favorite_faction,
        "longest_game_seconds": longest_game,
    }


def _empty_stats(player_id: str) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "overall": {"games": 0, "wins": 0, "losses": 0, "win_pct": None, "forfeits": 0},
        "by_alliance": {
            "good": {"games": 0, "wins": 0, "win_pct": None},
            "evil": {"games": 0, "wins": 0, "win_pct": None},
        },
        "by_scenario": [],
        "head_to_head": [],
        "streaks": {
            "current_win": 0,
            "current_loss": 0,
            "best_win_streak": 0,
            "best_loss_streak": 0,
        },
        "most_played_scenario": None,
        "favorite_faction": None,
        "longest_game_seconds": None,
    }


def _usernames_for(db: Session, player_ids: list[str]) -> dict[str, str]:
    if not player_ids:
        return {}
    rows = db.query(Player.id, Player.username).filter(Player.id.in_(player_ids)).all()
    return {str(pid): str(username) for pid, username in rows}


def player_headline_record(db: Session, player_id: str) -> dict[str, Any]:
    """Unfiltered headline record for compact profile cards."""
    return compute_player_stats(db, player_id)["overall"]
