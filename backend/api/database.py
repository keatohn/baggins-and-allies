"""
Database setup for Baggins & Allies.
Uses SQLite locally; use DATABASE_URL (e.g. Heroku Postgres) for production.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Heroku sets DATABASE_URL to postgres://; SQLAlchemy 2.x expects postgresql://
_raw_url = os.environ.get("DATABASE_URL")
if _raw_url and _raw_url.startswith("postgres://"):
    DATABASE_URL = _raw_url.replace("postgres://", "postgresql://", 1)
elif _raw_url:
    DATABASE_URL = _raw_url
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


def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)
