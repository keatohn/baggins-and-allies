"""
SQLAlchemy models for players, games, and game setups.
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, String, DateTime, Text, ForeignKey

from .database import Base


class Player(Base):
    __tablename__ = "players"

    id = Column(String(36), primary_key=True)  # uuid
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)  # display name, no spaces/special
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # JSON string: preferences, e.g. { "audio": { "menu_music_volume": 0.5, "game_music_volume": 0.25, "sfx_volume": 0.25, "muted": false } }
    preferences = Column(Text, nullable=True)


class Game(Base):
    __tablename__ = "games"

    id = Column(String(36), primary_key=True)  # uuid
    name = Column(String(128), nullable=False)  # user-defined game name
    game_code = Column(String(8), unique=True, nullable=True, index=True)  # 4-char alphanumeric for multiplayer; null for single-player
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(36), ForeignKey("players.id"), nullable=True)  # creator player_id
    status = Column(String(32), nullable=False, default="lobby")  # lobby | active | finished
    game_state = Column(Text, nullable=False)  # JSON string of full game state
    players = Column(Text, nullable=False)  # JSON array of { "player_id": str, "faction_id": str | null }
    config = Column(Text, nullable=True)  # JSON for future options


class Setup(Base):
    """Authoritative setup content (was JSON under data/setups/<folder>/). id matches manifest id."""

    __tablename__ = "setups"

    id = Column(String(128), primary_key=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    manifest_json = Column(Text, nullable=False)
    units_json = Column(Text, nullable=False)
    territories_json = Column(Text, nullable=False)
    factions_json = Column(Text, nullable=False)
    camps_json = Column(Text, nullable=False)
    ports_json = Column(Text, nullable=False)
    starting_setup_json = Column(Text, nullable=False)
    specials_json = Column(Text, nullable=False)
