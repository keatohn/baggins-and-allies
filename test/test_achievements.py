"""Achievements foundation: criteria DSL validation + admin CRUD.

Phase 2 slice 1: tables, whitelist validation, and admin endpoints.
Evaluation (game-end hook / on-read) is a later slice.
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.achievements import (
    AchievementError,
    create_achievement,
    validate_criteria,
)
from backend.api.auth import get_current_admin, get_current_player, hash_password
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Achievement, Player, PlayerAchievement


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


def _simple_criteria():
    return {"stat": "wins", "op": ">=", "value": 1}


def _nested_criteria():
    return {
        "all": [
            {"stat": "wins_as_evil", "op": ">=", "value": 10},
            {"stat": "best_win_streak", "op": ">=", "value": 5},
        ],
        "game_context": {"won": True, "alliance": "evil"},
    }


class TestValidateCriteria:
    def test_simple_leaf(self):
        out = validate_criteria(_simple_criteria())
        assert out == {"stat": "wins", "op": ">=", "value": 1}

    def test_all_and_game_context(self):
        out = validate_criteria(_nested_criteria())
        assert out["all"][0]["stat"] == "wins_as_evil"
        assert out["game_context"]["won"] is True
        assert out["game_context"]["alliance"] == "evil"

    def test_any_and_not(self):
        out = validate_criteria(
            {
                "any": [
                    {"not": {"stat": "losses", "op": "==", "value": 0}},
                    {"stat": "games_played", "op": ">", "value": 2},
                ]
            }
        )
        assert "any" in out
        assert "not" in out["any"][0]

    def test_rejects_unknown_stat(self):
        with pytest.raises(AchievementError, match="stat"):
            validate_criteria({"stat": "secret_sauce", "op": ">=", "value": 1})

    def test_rejects_bad_op(self):
        with pytest.raises(AchievementError, match="op"):
            validate_criteria({"stat": "wins", "op": "~=", "value": 1})

    def test_rejects_unknown_game_context(self):
        with pytest.raises(AchievementError, match="game_context"):
            validate_criteria({"stat": "wins", "op": ">=", "value": 1, "game_context": {"teleported": True}})

    def test_rejects_non_numeric_value(self):
        with pytest.raises(AchievementError, match="value"):
            validate_criteria({"stat": "wins", "op": ">=", "value": "many"})

    def test_rejects_empty_combinator(self):
        with pytest.raises(AchievementError, match="all"):
            validate_criteria({"all": []})

    def test_rejects_mixed_combinator_and_stat(self):
        with pytest.raises(AchievementError):
            validate_criteria({"all": [{"stat": "wins", "op": ">=", "value": 1}], "stat": "wins"})


class TestAdminAchievementsApi:
    def test_requires_admin(self, make_client, players):
        _admin, user = players
        client = make_client(user)
        assert client.get("/admin/achievements").status_code == 403
        assert client.post("/admin/achievements", json={}).status_code == 403

    def test_create_and_list(self, make_client, players, db_session):
        admin, _user = players
        client = make_client(admin)
        r = client.post(
            "/admin/achievements",
            json={
                "key": "first_victory",
                "name": "First Victory",
                "description": "Win a game",
                "category": "basics",
                "criteria": _simple_criteria(),
                "sort_order": 1,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key"] == "first_victory"
        assert body["criteria"] == _simple_criteria()
        assert body["active"] is True
        assert body["id"]

        r2 = client.get("/admin/achievements")
        assert r2.status_code == 200
        keys = [a["key"] for a in r2.json()["achievements"]]
        assert keys == ["first_victory"]

        row = db_session.query(Achievement).filter(Achievement.key == "first_victory").first()
        assert row is not None

    def test_create_rejects_bad_criteria(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        r = client.post(
            "/admin/achievements",
            json={
                "key": "bad_one",
                "name": "Bad",
                "criteria": {"stat": "nope", "op": ">=", "value": 1},
            },
        )
        assert r.status_code == 400
        assert "stat" in r.json()["detail"]

    def test_create_rejects_duplicate_key(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        payload = {"key": "dupe", "name": "A", "criteria": _simple_criteria()}
        assert client.post("/admin/achievements", json=payload).status_code == 200
        r = client.post("/admin/achievements", json=payload)
        assert r.status_code == 400
        assert "already exists" in r.json()["detail"]

    def test_patch_updates_fields(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        created = client.post(
            "/admin/achievements",
            json={"key": "patch_me", "name": "Old", "criteria": _simple_criteria()},
        ).json()
        r = client.patch(
            f"/admin/achievements/{created['id']}",
            json={
                "name": "New Name",
                "criteria": _nested_criteria(),
                "active": False,
                "tier": "gold",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "New Name"
        assert body["active"] is False
        assert body["tier"] == "gold"
        assert body["criteria"]["all"][1]["stat"] == "best_win_streak"

    def test_patch_unknown_id_404(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        r = client.patch(f"/admin/achievements/{uuid.uuid4()}", json={"name": "x"})
        assert r.status_code == 404

    def test_patch_rejects_bad_tier(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        created = client.post(
            "/admin/achievements",
            json={"key": "tiered", "name": "T", "criteria": _simple_criteria()},
        ).json()
        r = client.patch(f"/admin/achievements/{created['id']}", json={"tier": "obsidian"})
        assert r.status_code == 400
        assert "tier" in r.json()["detail"]

    def test_delete_removes_definition_and_grants(self, make_client, players, db_session):
        admin, user = players
        client = make_client(admin)
        created = client.post(
            "/admin/achievements",
            json={"key": "temp", "name": "Temp", "criteria": _simple_criteria()},
        ).json()
        db_session.add(
            PlayerAchievement(
                id=str(uuid.uuid4()),
                player_id=user.id,
                achievement_id=created["id"],
            )
        )
        db_session.commit()

        r = client.delete(f"/admin/achievements/{created['id']}")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert db_session.query(Achievement).filter(Achievement.id == created["id"]).first() is None
        assert (
            db_session.query(PlayerAchievement)
            .filter(PlayerAchievement.achievement_id == created["id"])
            .first()
            is None
        )

    def test_stats_catalog(self, make_client, players):
        admin, _user = players
        client = make_client(admin)
        r = client.get("/admin/achievements/stats-catalog")
        assert r.status_code == 200
        stats = {s["stat"] for s in r.json()["stats"]}
        assert {"wins", "win_streak", "wins_as_evil"} <= stats

    def test_granted_list(self, make_client, players, db_session):
        admin, user = players
        client = make_client(admin)
        created = client.post(
            "/admin/achievements",
            json={"key": "granted_one", "name": "G", "criteria": _simple_criteria()},
        ).json()
        db_session.add(
            PlayerAchievement(
                id=str(uuid.uuid4()),
                player_id=user.id,
                achievement_id=created["id"],
            )
        )
        db_session.commit()

        r = client.get("/admin/achievements/granted")
        assert r.status_code == 200
        granted = r.json()["granted"]
        assert len(granted) == 1
        assert granted[0]["username"] == "normaluser"
        assert granted[0]["achievement_key"] == "granted_one"

        r2 = client.get(f"/admin/achievements/granted?player={user.id}")
        assert len(r2.json()["granted"]) == 1
        r3 = client.get(f"/admin/achievements/granted?player={uuid.uuid4()}")
        assert r3.json()["granted"] == []


class TestCreateAchievementHelper:
    def test_normalizes_whitespace_and_lowercases_tier(self, db_session):
        row = create_achievement(
            db_session,
            key="  spaced_key  ",
            name="  Spaced  ",
            tier="GOLD",
            criteria=_simple_criteria(),
        )
        assert row.key == "spaced_key"
        assert row.name == "Spaced"
        assert row.tier == "gold"

    def test_rejects_bad_key(self, db_session):
        with pytest.raises(AchievementError, match="key"):
            create_achievement(
                db_session,
                key="Bad Key!",
                name="X",
                criteria=_simple_criteria(),
            )
