"""
SQLAlchemy models for players, games, and game setups.
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, String, DateTime, Integer, Text, ForeignKey, UniqueConstraint

from .database import Base


class Player(Base):
    __tablename__ = "players"

    id = Column(String(36), primary_key=True)  # uuid
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)  # display name, no spaces/special
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)  # set on each successful login; null until first login
    # Forgot-password flow: bcrypt hash of a single-use temp password and its UTC expiry; both cleared on reset.
    temp_password_hash = Column(String(255), nullable=True)
    temp_password_expires_at = Column(DateTime, nullable=True)
    # JSON string: preferences, e.g. { "audio": { "menu_music_volume": 0, "game_music_volume": 0, "sfx_volume": 0, "muted": false } }
    preferences = Column(Text, nullable=True)


class Game(Base):
    __tablename__ = "games"

    id = Column(String(36), primary_key=True)  # uuid
    name = Column(String(128), nullable=False)  # user-defined game name
    game_code = Column(String(8), unique=True, nullable=True, index=True)  # 4-char alphanumeric for multiplayer; null for single-player
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(36), ForeignKey("players.id"), nullable=True)  # creator player_id
    status = Column(String(32), nullable=False, default="lobby")  # lobby | active | finished
    completed_at = Column(DateTime, nullable=True)  # set once when status becomes finished
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


class AppSetting(Base):
    """Singleton JSON blobs (e.g. per-file audio gains)."""

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MapOverride(Base):
    """Map-level icon/location overrides keyed by map asset base name (shared across setups).

    overrides_json: {"territories": {tid: {"focus": "calculated"|"override",
    "positions": {kind: [x, y]}}}} in SVG viewBox coordinates.
    """

    __tablename__ = "map_overrides"

    map_base = Column(String(64), primary_key=True)
    overrides_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GameResult(Base):
    """One denormalized row per human participant per finished game.

    Written once when a game flips to status=finished (idempotent per game).
    AI factions have no roster entries today; is_ai is reserved for future use.
    forfeited=true marks players who left mid-game — exclude from headline W-L.
    """

    __tablename__ = "game_results"
    __table_args__ = (
        UniqueConstraint("game_id", "player_id", "faction_id", name="uq_game_results_game_player_faction"),
    )

    id = Column(String(36), primary_key=True)  # uuid
    game_id = Column(String(36), ForeignKey("games.id"), nullable=False, index=True)
    player_id = Column(String(36), ForeignKey("players.id"), nullable=False, index=True)
    faction_id = Column(String(64), nullable=False)
    alliance = Column(String(16), nullable=False)  # good | evil | neutral
    won = Column(Boolean, nullable=False, default=False)
    forfeited = Column(Boolean, nullable=False, default=False)
    is_ai = Column(Boolean, nullable=False, default=False)  # reserved; human rows only for now
    setup_id = Column(String(128), nullable=True, index=True)
    scenario_name = Column(String(255), nullable=True)  # display-name snapshot at game end
    turn_count = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    rings_of_power = Column(Boolean, nullable=False, default=False)
    shadow_of_war = Column(Boolean, nullable=False, default=False)
    # JSON array of human player_ids in this game (vs/with breakdowns without re-joining games)
    participant_ids = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow)


class Achievement(Base):
    """Admin-defined achievement; criteria_json is a DSL evaluated by the generic engine.

    Admins never write game-logic code — they compose JSON criteria from a whitelist
    of stat keys (see backend/api/achievements.py).
    """

    __tablename__ = "achievements"

    id = Column(String(36), primary_key=True)  # uuid
    key = Column(String(64), unique=True, nullable=False, index=True)  # stable slug, e.g. "first_victory"
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    icon = Column(String(64), nullable=True)  # optional client-side icon key
    category = Column(String(64), nullable=True)  # free-form grouping for the profile
    tier = Column(String(16), nullable=True)  # optional bronze|silver|gold|...
    criteria_json = Column(Text, nullable=False)  # JSON criteria DSL (validated on write)
    sort_order = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PlayerAchievement(Base):
    """Awarded achievement for a player (unique pair). Written by the evaluator only."""

    __tablename__ = "player_achievements"
    __table_args__ = (
        UniqueConstraint("player_id", "achievement_id", name="uq_player_achievements_player_achievement"),
    )

    id = Column(String(36), primary_key=True)  # uuid
    player_id = Column(String(36), ForeignKey("players.id"), nullable=False, index=True)
    achievement_id = Column(String(36), ForeignKey("achievements.id"), nullable=False, index=True)
    awarded_at = Column(DateTime, default=datetime.utcnow)
