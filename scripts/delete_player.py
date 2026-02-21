#!/usr/bin/env python3
"""
Delete a player by email so you can re-register with the same email/username.
Usage: python scripts/delete_player.py <email>
From repo root with PYTHONPATH=. or from backend: python -m scripts.delete_player <email>
"""
import sys
import os

# Allow running from repo root or backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.api.database import SessionLocal
from backend.api.models import Player, Game


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/delete_player.py <email>", file=sys.stderr)
        sys.exit(1)
    email = sys.argv[1].strip()
    if not email:
        print("Error: provide an email.", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        player = db.query(Player).filter(Player.email == email).first()
        if not player:
            print(f"No player found with email: {email!r}")
            return
        player_id = player.id
        username = player.username
        # Unlink games created by this player so FK doesn't block delete
        for game in db.query(Game).filter(Game.created_by == player_id):
            game.created_by = None
        db.delete(player)
        db.commit()
        print(f"Deleted player {username!r} ({email}). You can now register again.")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
