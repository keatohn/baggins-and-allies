"""A DB that never ran init_db() has no tables and must fall back to file-backed data."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config import DEFAULT_SETUP_ID
from backend.setup_data import db_has_any_setup, try_load_static_definitions


def _schemaless_session():
    engine = create_engine("sqlite:///:memory:")
    return sessionmaker(bind=engine)()


def test_db_has_any_setup_is_false_when_table_missing():
    session = _schemaless_session()
    try:
        assert db_has_any_setup(session) is False
    finally:
        session.close()


def test_try_load_static_definitions_falls_back_to_files_when_schema_missing(monkeypatch):
    monkeypatch.setenv("SETUP_SOURCE", "db")
    session = _schemaless_session()
    try:
        unit_defs, territory_defs, *_ = try_load_static_definitions(DEFAULT_SETUP_ID, session)
    finally:
        session.close()
    assert unit_defs
    assert territory_defs
