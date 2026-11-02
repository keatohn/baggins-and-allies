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


def _faction_ids_from_reassignments(config: dict, player_id: str) -> list[str]:
    """Faction ids a forfeited player gave away, from the older reassignment notes.

    Notes look like ``\"Mordor → Computer\"``. Games forfeited before faction ids were
    stored have these strings and an empty roster, so this is the only record of
    who played which side.
    """
    notes = config.get("forfeit_reassignments") if isinstance(config, dict) else None
    entries = notes.get(player_id) if isinstance(notes, dict) else None
    if not isinstance(entries, list):
        return []
    defs = config.get("definitions") if isinstance(config.get("definitions"), dict) else {}
    factions = defs.get("factions") if isinstance(defs.get("factions"), dict) else {}
    by_label: dict[str, str] = {}
    for fid, raw in factions.items():
        fid_s = str(fid)
        by_label[fid_s.lower()] = fid_s
        if isinstance(raw, dict):
            name = raw.get("display_name")
            if isinstance(name, str) and name.strip():
                by_label[name.strip().lower()] = fid_s
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        label = entry.split("→", 1)[0].split("->", 1)[0].strip().lower()
        fid_s = by_label.get(label)
        if fid_s and fid_s not in out:
            out.append(fid_s)
    return out


def _difficulty_for_faction(config: dict, faction_id: str) -> str:
    default = config.get("ai_difficulty") if isinstance(config, dict) else None
    default_s = str(default).strip().lower() if isinstance(default, str) and default.strip() else "normal"
    settings = config.get("ai_faction_settings") if isinstance(config, dict) else None
    row = settings.get(str(faction_id)) if isinstance(settings, dict) else None
    if isinstance(row, dict):
        raw = row.get("difficulty")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return default_s


def computer_player_id(difficulty: str) -> str:
    """One identity per difficulty so head-to-head collapses every faction on that level."""
    text = str(difficulty or "normal").strip().lower() or "normal"
    return f"{_COMPUTER_PLAYER_ID}:{text}"


def computer_display_name(player_id: str) -> str | None:
    prefix = f"{_COMPUTER_PLAYER_ID}:"
    if not str(player_id).startswith(prefix):
        return None
    label = str(player_id)[len(prefix):].replace("_", " ").strip()
    pretty = " ".join(word.capitalize() for word in label.split())
    return f"Computer ({pretty})" if pretty else "Computer"


def _computer_roster(config: dict, players_list: Any) -> list[dict]:
    """One row per AI faction. Difficulty comes from per-faction settings, else the game default."""
    faction_ids: list[Any] = []
    raw = config.get("ai_factions") if isinstance(config, dict) else None
    if isinstance(raw, list):
        faction_ids.extend(raw)
    if isinstance(players_list, list):
        for entry in players_list:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("player_id") or "") != _COMPUTER_PLAYER_ID:
                continue
            faction_ids.append(entry.get("faction_id"))
    out: list[dict] = []
    seen: set[str] = set()
    for fid in faction_ids:
        fid_s = str(fid).strip() if fid is not None else ""
        if not fid_s or fid_s in seen:
            continue
        seen.add(fid_s)
        out.append({
            "player_id": computer_player_id(_difficulty_for_faction(config, fid_s)),
            "faction_id": fid_s,
            "is_ai": True,
        })
    return out


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
    computers = _computer_roster(config, players_list)
    forfeited_ids = {
        str(x)
        for x in (config.get("forfeited_player_ids") or [])
        if x is not None and str(x).strip() != ""
    }

    # Forfeited players are removed from the roster at leave time; keep their rows so
    # stats still count the game (forfeited=True). Skip any already in the roster
    # (e.g. rejoined after a lobby forfeit) to avoid duplicate rows.
    roster_pids = {e["player_id"] for e in roster}
    entries = list(roster)
    factions_cfg = config.get("forfeited_player_factions")
    if not isinstance(factions_cfg, dict):
        factions_cfg = {}
    recorded: dict[str, list[str]] = {}
    for pid, fids in factions_cfg.items():
        pid_s = str(pid)
        if isinstance(fids, str):
            fids = [fids]
        if not isinstance(fids, list):
            continue
        recorded[pid_s] = [str(fid).strip() for fid in fids if str(fid).strip()]
    for pid_s in forfeited_ids:
        if pid_s in roster_pids:
            continue
        fids = recorded.get(pid_s) or _faction_ids_from_reassignments(config, pid_s)
        for fid_s in fids:
            if fid_s:
                entries.append({"player_id": pid_s, "faction_id": fid_s})
    entries.extend(computers)

    participant_ids = sorted({
        e["player_id"] for e in entries if not str(e["player_id"]).startswith(f"{_COMPUTER_PLAYER_ID}:")
        and str(e["player_id"]) != _COMPUTER_PLAYER_ID
    })

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

    from backend.progression import (
        forfeit_counts_as_leave,
        forfeit_round_of,
        profile_counts_for_faction,
    )

    now = datetime.utcnow()
    rows: list[GameResult] = []
    for entry in entries:
        alliance = _faction_alliance_from_config(config, entry["faction_id"])
        won = winner is not None and alliance == winner
        pid = entry["player_id"]
        is_ai = bool(entry.get("is_ai"))
        round_at = None if is_ai else forfeit_round_of(config, pid)
        left_early = (not is_ai) and pid in forfeited_ids and forfeit_counts_as_leave(round_at, turn_count)
        # No stored round: older forfeits keep forfeited=true and still count as completed.
        forfeited = left_early or (pid in forfeited_ids and round_at is None and not is_ai)
        if is_ai:
            counts = {"battles_won": 0, "strongholds_captured": 0, "enemy_units_destroyed": 0}
        else:
            counts = profile_counts_for_faction(config, pid, entry["faction_id"], round_at)
        rows.append(
            GameResult(
                id=str(uuid.uuid4()),
                game_id=game_id,
                player_id=pid,
                faction_id=entry["faction_id"],
                alliance=alliance,
                won=won,
                forfeited=forfeited,
                forfeit_round=round_at,
                battles_won=int(counts.get("battles_won") or 0),
                strongholds_captured=int(counts.get("strongholds_captured") or 0),
                units_destroyed=int(counts.get("enemy_units_destroyed") or 0),
                is_ai=is_ai,
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


def _apply_profile_counts(result: GameResult, counts: dict) -> bool:
    battles = int(counts.get("battles_won") or 0)
    strongholds = int(counts.get("strongholds_captured") or 0)
    units = int(counts.get("enemy_units_destroyed") or 0)
    changed = (
        int(result.battles_won or 0) != battles
        or int(result.strongholds_captured or 0) != strongholds
        or int(result.units_destroyed or 0) != units
    )
    if changed:
        result.battles_won = battles
        result.strongholds_captured = strongholds
        result.units_destroyed = units
    return changed


def refresh_game_result_counts(db: Session) -> int:
    """Fill battle, stronghold, and unit totals on finished games that still have them.

    A complete event log is recounted, so strongholds are enemy strongholds only.
    A trimmed log, or a game that only kept xp totals, fills battles and units from
    those totals. Its stronghold number still includes retaking a friendly stronghold,
    because the individual captures are gone. Games with neither record stay at zero.
    """
    from backend.progression import profile_counts_for_faction

    updated = 0
    games = db.query(GameModel).filter(GameModel.status == "finished").all()
    for game in games:
        config = _parse_json(game.config, {})
        if not isinstance(config, dict):
            continue
        results = db.query(GameResult).filter(GameResult.game_id == game.id).all()
        for result in results:
            if result.is_ai:
                continue
            counts = profile_counts_for_faction(
                config,
                str(result.player_id),
                str(result.faction_id),
                int(result.forfeit_round) if result.forfeit_round is not None else None,
            )
            if _apply_profile_counts(result, counts):
                updated += 1
    if updated:
        db.flush()
    return updated


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
    try:
        total += refresh_game_result_counts(db)
    except Exception as e:
        print(f"[game_results] stat refresh failed: {e}", flush=True)
    if total:
        db.commit()
    return total
