#!/usr/bin/env python3
"""
Set map_asset (base name, no extension) on a game in the DB. Use for new games (stored
at creation) or to fix legacy games that show the wrong map. Usage (from repo root):
  python -m backend.scripts.set_map_asset <game_id_or_name> <map_base_name>
Example: python -m backend.scripts.set_map_asset 94b3f65e-e65b-447a-9b50-0df935cff4d3 baggins_and_allies_map_0.1
Frontend loads /<base>.svg (and /<base>.png for background). Known bases: test_map, baggins_and_allies_map_0.1.
"""
import json
import sys
import os

# Run from repo root so backend is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.api.database import SessionLocal, get_db_file_path
from backend.api.models import Game as GameModel


def main():
    if len(sys.argv) < 3:
        print("Usage: python -m backend.scripts.set_map_asset <game_id_or_name> <map_base_name>")
        print("Example: python -m backend.scripts.set_map_asset test_game0 test_map")
        sys.exit(1)
    game_id_or_name = sys.argv[1]
    map_base = sys.argv[2].strip().replace(".svg", "").replace(".png", "")
    if not map_base:
        map_base = "test_map"

    db = SessionLocal()
    try:
        row = db.query(GameModel).filter(
            (GameModel.id == game_id_or_name) | (GameModel.name == game_id_or_name)
        ).first()
        if not row:
            print(f"No game found with id or name: {game_id_or_name}")
            sys.exit(2)
        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
        if not isinstance(raw, dict):
            raw = {}
        raw["map_asset"] = map_base
        row.game_state = json.dumps(raw)
        db.commit()
        db_path = get_db_file_path()
        print(f"Updated game id={row.id} name={row.name} -> map_asset={map_base}")
        if db_path:
            print(f"DB file: {db_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
