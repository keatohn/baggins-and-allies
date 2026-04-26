#!/usr/bin/env python3
"""
Add a reciprocal ford_adjacent pair on one game's stored definitions snapshot (games.config).
Does not touch games.game_state.

Database connection (same as the API — see backend/api/database.py):
  - Postgres: set DATABASE_URL
  - SQLite (e.g. Railway volume): set SQLITE_DATABASE_PATH=/data/game.db
    (SQLITE_DATABASE is an alias). Do not set DATABASE_URL.

Patch (from repo root):
  SQLITE_DATABASE_PATH=/data/game.db python -m backend.scripts.add_ford_adjacent_pair_to_game_config \\
    f82a2ac6-8e66-45de-9d87-d9b35ca35c6a pelennor south_ithilien \\
    --backup-to /tmp/game-f82a-config-backup.json --apply

Rollback that patch:
  SQLITE_DATABASE_PATH=/data/game.db python -m backend.scripts.add_ford_adjacent_pair_to_game_config \\
    f82a2ac6-8e66-45de-9d87-d9b35ca35c6a --restore-from /tmp/game-f82a-config-backup.json --apply

Omit --apply for dry run (patch or restore).

On production, easier: deploy then POST (admin JWT) to
  /admin/games/<game_id>/ford-adjacent-pair
  {"territory_a":"pelennor","territory_b":"south_ithilien","apply":false}  then apply true.
That runs on the server against the real DB and clears caches; no restart needed.

After --apply via this script only, restart the API so in-memory game_defs reloads.

Full DB safety (outside this script): copy the sqlite file or run sqlite3 .backup before risky edits.

Local SQLite: SQLITE_DATABASE_PATH must be a copy of the real prod game.db (not an empty new file).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import inspect as sqla_inspect

from backend.api.database import SessionLocal, DATABASE_URL
from backend.api.game_config_ford_patch import dump_config_for_storage, patch_ford_adjacent_pair_in_config
from backend.api.models import Game as GameModel


def _require_games_table(db) -> None:
    """Fail fast with a clear message when the DB file is empty or not the app schema."""
    if not sqla_inspect(db.get_bind()).has_table("games"):
        extra = ""
        if DATABASE_URL.startswith("sqlite"):
            extra = (
                f"\nThe SQLite file you opened has no `games` table — it is almost certainly not "
                f"a copy of production (or it is brand new). engine: {DATABASE_URL}\n"
                "Fix: replace that file with the real prod `game.db` from Railway (download / backup / "
                "copy from the volume), then rerun."
            )
        print(f"No `games` table in this database.{extra}", file=sys.stderr)
        sys.exit(8)


def main() -> None:
    p = argparse.ArgumentParser(description="Patch or restore games.config ford_adjacent snapshot for one game.")
    p.add_argument("game_id", help="games.id UUID")
    p.add_argument("territory_a", nargs="?", help="e.g. pelennor (not used with --restore-from)")
    p.add_argument("territory_b", nargs="?", help="e.g. south_ithilien (not used with --restore-from)")
    p.add_argument("--apply", action="store_true", help="write to DB (default is dry run)")
    p.add_argument(
        "--backup-to",
        metavar="PATH",
        help="with --apply on patch: write original config JSON text here before updating (for rollback)",
    )
    p.add_argument(
        "--restore-from",
        metavar="PATH",
        help="replace this game's config with the exact JSON text from this file (--apply to commit)",
    )
    args = p.parse_args()

    if args.restore_from:
        _restore(args)
        return
    if not args.territory_a or not args.territory_b:
        p.error("territory_a and territory_b are required unless using --restore-from")
    _patch(args)


def _db_url_hint() -> str:
    if DATABASE_URL.startswith("sqlite"):
        return "sqlite (check SQLITE_DATABASE_PATH / SQLITE_DATABASE)"
    return "postgres (DATABASE_URL)"


def _patch(args: argparse.Namespace) -> None:
    a, b = args.territory_a, args.territory_b
    db = SessionLocal()
    try:
        _require_games_table(db)
        row = db.query(GameModel).filter(GameModel.id == args.game_id).first()
        if not row:
            print(f"No game: {args.game_id} ({_db_url_hint()})", file=sys.stderr)
            sys.exit(2)
        original_text = row.config if isinstance(row.config, str) else json.dumps(row.config, ensure_ascii=False)
        config = json.loads(original_text) if isinstance(original_text, str) else original_text
        if not isinstance(config, dict):
            print("config is not a JSON object", file=sys.stderr)
            sys.exit(3)
        try:
            changed = patch_ford_adjacent_pair_in_config(config, a, b)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(5)

        if not changed:
            print("ford pair already present; nothing to do")
            return

        terr = config["definitions"]["territories"]
        print(f"ford_adjacent[{a!r}] -> {terr[a].get('ford_adjacent')!r}")
        print(f"ford_adjacent[{b!r}] -> {terr[b].get('ford_adjacent')!r}")

        if not args.apply:
            print("Dry run only. Pass --apply to persist.")
            return

        if args.backup_to:
            Path(args.backup_to).parent.mkdir(parents=True, exist_ok=True)
            Path(args.backup_to).write_text(original_text, encoding="utf-8")
            print(f"Wrote pre-patch config backup -> {args.backup_to}")

        row.config = dump_config_for_storage(config)
        db.commit()
        print("Saved. Restart the API so this game reloads definitions from DB.")
    finally:
        db.close()


def _restore(args: argparse.Namespace) -> None:
    path = Path(args.restore_from)
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        sys.exit(6)
    text = path.read_text(encoding="utf-8")
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Backup is not valid JSON: {e}", file=sys.stderr)
        sys.exit(7)

    db = SessionLocal()
    try:
        _require_games_table(db)
        row = db.query(GameModel).filter(GameModel.id == args.game_id).first()
        if not row:
            print(f"No game: {args.game_id} ({_db_url_hint()})", file=sys.stderr)
            sys.exit(2)
        if not args.apply:
            print("Dry run: would restore config from backup (pass --apply to commit).")
            return
        row.config = text
        db.commit()
        print("Config restored from backup. Restart the API.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
