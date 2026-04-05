"""
Database setup for Baggins & Allies.

- Local: SQLite at backend/api/game.db unless overridden.
- Production SQLite: set SQLITE_DATABASE_PATH (e.g. /data/game.db on Railway volume);
  SQLITE_DATABASE is accepted as an alias if the path was set under that name by mistake.
  leave DATABASE_URL unset. See docs/PRODUCTION_DEPLOYMENT.md.
- Production Postgres: set DATABASE_URL (e.g. Heroku/Railway Postgres).
"""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

# Heroku sets DATABASE_URL to postgres://; SQLAlchemy 2.x expects postgresql://
_raw_url = os.environ.get("DATABASE_URL")
if _raw_url and _raw_url.startswith("postgres://"):
    DATABASE_URL = _raw_url.replace("postgres://", "postgresql://", 1)
elif _raw_url:
    DATABASE_URL = _raw_url
else:
    # Railway/local: optional persistent path (e.g. volume mount `/data/game.db`).
    # Accept SQLITE_DATABASE as alias — easy to misname; only SQLITE_DATABASE_PATH was documented.
    _sqlite_path = os.environ.get("SQLITE_DATABASE_PATH") or os.environ.get("SQLITE_DATABASE")
    if _sqlite_path:
        _sqlite_abs = os.path.abspath(_sqlite_path)
        parent = os.path.dirname(_sqlite_abs)
        if parent:
            os.makedirs(parent, exist_ok=True)
        DATABASE_URL = f"sqlite:///{_sqlite_abs}"
    else:
        DB_DIR = os.path.dirname(os.path.abspath(__file__))
        DATABASE_URL = f"sqlite:///{os.path.join(DB_DIR, 'game.db')}"


# SQLite needs check_same_thread=False; Postgres does not use that arg
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency that yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_file_path() -> str | None:
    """Return the absolute path to the SQLite DB file, or None if not SQLite."""
    if not DATABASE_URL.startswith("sqlite"):
        return None
    # Use engine.url so we get the same path SQLAlchemy uses
    path_part = engine.url.database
    if path_part and not os.path.isabs(path_part):
        path_part = os.path.abspath(path_part)
    return path_part or None


# This row is set to admin on each init (single-row UPDATE only). Other players’ is_admin is never cleared.
ADMIN_PLAYER_EMAIL = "kjhubbs8@gmail.com"


def _ensure_is_admin_column():
    """Add players.is_admin if missing (non-destructive). Never bulk-clear admin flags.

    - ALTER ADD COLUMN only: existing player rows keep all other columns; new column defaults to false/0.
    - Games / game_state are untouched (this migration does not reference them).
    - After ensure: one UPDATE sets admin only for ADMIN_PLAYER_EMAIL so bootstrap admin stays true;
      any other player you set true in the DB is left unchanged.
    """
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(players)")).fetchall()
            names = {row[1] for row in rows}
            if "is_admin" not in names:
                conn.execute(text("ALTER TABLE players ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"))
            conn.execute(
                text("UPDATE players SET is_admin = 1 WHERE lower(email) = lower(:email)"),
                {"email": ADMIN_PLAYER_EMAIL},
            )
    else:
        with engine.begin() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'players'
                      AND column_name = 'is_admin'
                    """
                )
            ).fetchone()
            if exists is None:
                conn.execute(
                    text(
                        "ALTER TABLE players ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false"
                    )
                )
            conn.execute(
                text(
                    "UPDATE players SET is_admin = true WHERE lower(email) = lower(:email)"
                ),
                {"email": ADMIN_PLAYER_EMAIL},
            )


def _ensure_player_preferences_column():
    """Add players.preferences if missing (existing SQLite/Postgres DBs before this column)."""
    if DATABASE_URL.startswith("sqlite"):
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(players)")).fetchall()
            names = {row[1] for row in rows}
            if "preferences" not in names:
                conn.execute(text("ALTER TABLE players ADD COLUMN preferences TEXT"))
    else:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS preferences TEXT"))


def init_db():
    """Create all tables and apply additive schema patches.

    Migrations here only add missing columns (ALTER TABLE ... ADD COLUMN). They do not drop tables,
    truncate rows, or rewrite game_state — player and game data are preserved.
    """
    # Register all models on Base before create_all (setups table, etc.)
    from .models import Game, Player, Setup  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_player_preferences_column()
    _ensure_is_admin_column()
    db = SessionLocal()
    try:
        from backend.setup_data import seed_setups_if_empty

        seed_setups_if_empty(db)
    finally:
        db.close()
