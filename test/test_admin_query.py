"""Admin query console: schema listing and SELECT-only execution."""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.admin_query import SelectOnlyError, validate_select_sql
from backend.api.auth import get_current_admin, get_current_player, hash_password
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Player


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    maker = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = maker()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def players(db_session):
    admin = Player(
        id=str(uuid.uuid4()),
        email="admin@example.com",
        username="adminuser",
        password_hash=hash_password("admin-pass"),
        is_admin=True,
    )
    user = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash=hash_password("user-pass"),
        is_admin=False,
    )
    db_session.add_all([admin, user])
    db_session.commit()
    return admin, user


@pytest.fixture()
def make_client(db_session, players):
    admin, user = players
    current = {"player": user}

    def override_get_db():
        yield db_session

    def override_current_player():
        return current["player"]

    def override_current_admin():
        if not getattr(current["player"], "is_admin", False):
            raise HTTPException(status_code=403, detail="Admin access required")
        return current["player"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player
    app.dependency_overrides[get_current_admin] = override_current_admin

    def _make(as_player):
        current["player"] = as_player
        return TestClient(app)

    try:
        yield _make
    finally:
        app.dependency_overrides.clear()
        current["player"] = user


def test_validate_select_allows_reads_and_rejects_writes():
    assert validate_select_sql("  select 1;  ").lower().startswith("select")
    assert validate_select_sql("WITH c AS (SELECT 1) SELECT * FROM c").lower().startswith("with")
    assert validate_select_sql("SELECT replace('ab', 'a', 'A')").lower().startswith("select")
    assert validate_select_sql("SELECT 'delete this' AS note /* drop */").lower().startswith("select")
    assert validate_select_sql("SELECT $$ delete $$").lower().startswith("select")

    rejected = [
        "DELETE FROM players",
        "UPDATE players SET email = email",
        "INSERT INTO players (id) VALUES ('x')",
        "DROP TABLE players",
        "SELECT 1; DROP TABLE players",
        "SELECT 1; SELECT 2",
        "WITH c AS (DELETE FROM players RETURNING *) SELECT * FROM c",
        "WITH c AS (SELECT 1) INSERT INTO players (id) SELECT 'x'",
        "SELECT * INTO copy FROM players",
        "PRAGMA table_info(players)",
        "/* hide */ DROP TABLE players",
        "SELECT load_extension('x')",
        "",
        "   ;  ",
    ]
    for sql in rejected:
        with pytest.raises(SelectOnlyError):
            validate_select_sql(sql)


def test_query_requires_admin(make_client, players):
    _admin, user = players
    client = make_client(user)
    assert client.get("/admin/query/schema").status_code == 403
    assert client.post("/admin/query", json={"sql": "SELECT 1"}).status_code == 403


def test_schema_lists_players_columns_alphabetically(make_client, players):
    admin, _user = players
    client = make_client(admin)
    r = client.get("/admin/query/schema")
    assert r.status_code == 200
    tables = r.json()["tables"]
    names = [t["name"] for t in tables]
    assert names == sorted(names, key=str.lower)
    players_table = next(t for t in tables if t["name"] == "players")
    assert players_table["kind"] == "table"
    col_names = [c["name"] for c in players_table["columns"]]
    assert "email" in col_names
    assert "username" in col_names
    assert "is_admin" in col_names


def test_select_returns_rows_and_writes_do_not_run(make_client, players, db_session):
    admin, user = players
    client = make_client(admin)

    ok = client.post("/admin/query", json={"sql": "SELECT username FROM players ORDER BY username"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["columns"] == ["username"]
    assert body["rows"] == [["adminuser"], ["normaluser"]]
    assert body["truncated"] is False

    fn = client.post("/admin/query", json={"sql": "SELECT replace(username, 'a', 'A') AS name FROM players WHERE id = '%s'" % admin.id})
    assert fn.status_code == 200
    assert fn.json()["rows"] == [["Adminuser"]]

    blocked = client.post("/admin/query", json={"sql": "SELECT 1; DROP TABLE players"})
    assert blocked.status_code == 400
    still = client.post("/admin/query", json={"sql": "SELECT count(*) AS n FROM players"})
    assert still.status_code == 200
    assert still.json()["rows"] == [[2]]

    update = client.post(
        "/admin/query",
        json={"sql": "UPDATE players SET is_admin = 1 WHERE id = '%s'" % user.id},
    )
    assert update.status_code == 400
    db_session.refresh(user)
    assert user.is_admin is False
