"""Denormalized finished-game results (write-once at game end).

One row per human participant per finished game. Written from save_game when
status flips to finished; also used by startup backfill for existing games.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .models import Game as GameModel, GameResult

# Must match main.LOBBY_COMPUTER_PLAYER_ID (avoid circular import of main).
_COMPUTER_PLAYER_ID = "__computer__"


def _parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return default
    return default


def _faction_alliance_from_config(config: dict, faction_id: str) -> str:
    defs = config.get("definitions") if isinstance(config, dict) else None
    factions = defs.get("factions") if isinstance(defs, dict) else None
    fdef = factions.get(faction_id) if isinstance(factions, dict) else None
    if isinstance(fdef, dict):
        return str(fdef.get("alliance") or "neutral")
    return "neutral"


def _scenario_display_name(config: dict, db: Session) -> str | None:
    setup_id = config.get("setup_id") if isinstance(config, dict) else None
    if not setup_id or not isinstance(setup_id, str):
        return None
    try:
        from backend.setup_data import try_scenario_display

        display = try_scenario_display(setup_id, db)
        if isinstance(display, dict):
            name = display.get("display_name") or display.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return None


def _duration_seconds(row: GameModel) -> int | None:
    created = getattr(row, "created_at", None)
    completed = getattr(row, "completed_at", None)
    if not created or not completed:
        return None
    try:
        delta = completed - created
        return max(0, int(delta.total_seconds()))
    except (TypeError, ValueError):
        return None


def _human_roster(players_list: Any) -> list[dict]:
    """Roster entries with a real human player_id (not computer / empty)."""
    out: list[dict] = []
    if not isinstance(players_list, list):
        return out
    for p in players_list:
        if not isinstance(p, dict):
            continue
        pid = p.get("player_id")
        fid = p.get("faction_id")
        if pid is None or str(pid).strip() == "" or str(pid) == _COMPUTER_PLAYER_ID:
            continue
        if fid is None or str(fid).strip() == "":
            continue
        out.append({"player_id": str(pid), "faction_id": str(fid)})
    return out


def game_results_exist(db: Session, game_id: str) -> bool:
    return (
        db.query(GameResult.id).filter(GameResult.game_id == str(game_id)).first()
        is not None
    )


def ensure_game_results_for_row(db: Session, row: GameModel) -> int:
    """Write game_results for a finished game if not already present.

    Idempotent: no-ops when status is not finished or rows already exist for game_id.
    Returns number of rows written.
    """
    if not row or str(row.status) != "finished":
        return 0
    game_id = str(row.id)
    if game_results_exist(db, game_id):
        return 0

    state = _parse_json(getattr(row, "game_state", None), {}) or {}
    if not isinstance(state, dict):
        state = {}
    config = _parse_json(getattr(row, "config", None), {}) or {}
    if not isinstance(config, dict):
        config = {}
    players_list = _parse_json(getattr(row, "players", None), []) or []

    winner = state.get("winner")
    if winner is not None:
        winner = str(winner)

    roster = _human_roster(players_list)
    participant_ids = sorted({e["player_id"] for e in roster})
    forfeited_ids = {
        str(x)
        for x in (config.get("forfeited_player_ids") or [])
        if x is not None and str(x).strip() != ""
    }

    setup_id = config.get("setup_id") if isinstance(config.get("setup_id"), str) else None
    scenario_name = _scenario_display_name(config, db)

    turn_number = state.get("turn_number")
    try:
        turn_count = int(turn_number) if turn_number is not None else None
    except (TypeError, ValueError):
        turn_count = None

    duration = _duration_seconds(row)
    rings_of_power = bool(state.get("rings_of_power"))
    shadow_of_war = bool(state.get("shadow_of_war"))
    participant_json = json.dumps(participant_ids)

    now = datetime.utcnow()
    rows: list[GameResult] = []
    for entry in roster:
        alliance = _faction_alliance_from_config(config, entry["faction_id"])
        won = winner is not None and alliance == winner
        forfeited = entry["player_id"] in forfeited_ids
        rows.append(
            GameResult(
                id=str(uuid.uuid4()),
                game_id=game_id,
                player_id=entry["player_id"],
                faction_id=entry["faction_id"],
                alliance=alliance,
                won=won,
                forfeited=forfeited,
                is_ai=False,
                setup_id=setup_id,
                scenario_name=scenario_name,
                turn_count=turn_count,
                duration_seconds=duration,
                rings_of_power=rings_of_power,
                shadow_of_war=shadow_of_war,
                participant_ids=participant_json,
                created_at=now,
            )
        )

    if not rows:
        return 0
    db.add_all(rows)
    db.flush()
    return len(rows)


def backfill_game_results(db: Session) -> int:
    """Write missing results for all finished games. Safe to run on every startup."""
    total = 0
    rows = (
        db.query(GameModel)
        .filter(GameModel.status == "finished")
        .all()
    )
    for row in rows:
        try:
            total += ensure_game_results_for_row(db, row)
        except Exception as e:
            print(f"[game_results] backfill failed for game {row.id}: {e}", flush=True)
    if total:
        db.commit()
    return total
