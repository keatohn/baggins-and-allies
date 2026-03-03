#!/usr/bin/env python3
"""
Set remaining_movement=1 (and base_movement=1) for all units in territory 'dagorlad'.
Usage from repo root: python -m backend.scripts.fix_dagorlad_movement <game_id>
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.api.database import SessionLocal, get_db_file_path
from backend.api.models import Game as GameModel


def main():
    game_id = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if not game_id:
        print("Usage: python -m backend.scripts.fix_dagorlad_movement <game_id>")
        sys.exit(1)

    db = SessionLocal()
    try:
        row = db.query(GameModel).filter(GameModel.id == game_id).first()
        if not row:
            print(f"No game found with id: {game_id}")
            sys.exit(2)
        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
        if not isinstance(raw, dict):
            print("Invalid game state")
            sys.exit(3)

        territories = raw.get("territories") or {}
        if not isinstance(territories, dict):
            territories = {}
        dagorlad = territories.get("dagorlad")
        if not dagorlad or not isinstance(dagorlad, dict):
            print("No territory 'dagorlad' in state")
            sys.exit(4)
        units = dagorlad.get("units") or []
        if not isinstance(units, list):
            units = []
        if not units:
            print("No units in dagorlad")
            sys.exit(5)

        updated = 0
        for u in units:
            if not isinstance(u, dict):
                continue
            u["remaining_movement"] = 1
            if u.get("base_movement", 0) < 1:
                u["base_movement"] = 1
            updated += 1
        row.game_state = json.dumps(raw)
        db.commit()
        print(f"Game {game_id}: set remaining_movement=1 for {updated} unit(s) in dagorlad")
        if get_db_file_path():
            print(f"DB: {get_db_file_path()}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
