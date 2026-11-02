"""XP ledger, tier thresholds, and the admin progression endpoints."""

import json
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player, hash_password
from backend.api.database import Base, get_db
from backend.api.main import app, save_game
from backend.api.models import Game, Player, XpLedger
from backend.engine.state import GameState
from backend.progression import (
    ProgressionError,
    fold_events,
    normalize_progression,
    score_player,
    default_progression,
)


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setattr("backend.progression.is_files_setup_source", lambda: False)
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


def _rules(**overrides):
    rules = dict(default_progression()["rules"])
    rules.update(overrides)
    return rules


def test_defaults_and_threshold_order():
    cfg = default_progression()
    assert cfg["tiers"][0]["min_xp"] == 0
    assert cfg["tiers"][0]["rank"] == 1
    mins = [t["min_xp"] for t in cfg["tiers"]]
    assert mins == sorted(mins)
    assert mins[0] == 0
    assert [t["rank"] for t in cfg["tiers"]] == list(range(1, len(cfg["tiers"]) + 1))
    with pytest.raises(ProgressionError, match="start at 0"):
        normalize_progression({
            **cfg,
            "tiers": [{**cfg["tiers"][0], "min_xp": 10}],
        })
    with pytest.raises(ProgressionError, match="must increase"):
        second = dict(cfg["tiers"][1])
        second["min_xp"] = 0
        normalize_progression({**cfg, "tiers": [cfg["tiers"][0], second]})


def test_frame_path_and_tier_reference():
    cfg = default_progression()
    bad = {
        **cfg,
        "frames": [{"id": "bronze", "label": "Bronze", "src": "../secret.png"}],
    }
    with pytest.raises(ProgressionError, match="avatar-frames"):
        normalize_progression(bad)
    ok_frames = [{"id": "bronze", "label": "Bronze ring", "src": "/avatar-frames/bronze_ring.png"}]
    tier = dict(cfg["tiers"][0])
    tier["visual"] = {"frame_id": "missing", "ring_color": "#8c6a3f", "glow": "soft"}
    with pytest.raises(ProgressionError, match="not in the frame list"):
        normalize_progression({**cfg, "frames": ok_frames, "tiers": [tier]})
    tier["visual"] = {"frame_id": "bronze", "ring_color": "#8c6a3f", "glow": "pulse"}
    saved = normalize_progression({**cfg, "frames": ok_frames, "tiers": [tier]})
    assert saved["tiers"][0]["visual"]["frame_id"] == "bronze"
    https = dict(ok_frames[0])
    https["src"] = "https://cdn.example.com/bronze.png"
    normalize_progression({**cfg, "frames": [https], "tiers": [tier]})


def test_fold_events_counts_strongholds_kills_and_battles():
    stats = {"by_faction": {}}
    fold_events(
        stats,
        [
            {"type": "territory_captured", "payload": {"territory": "minas_tirith", "new_owner": "gondor"}},
            {"type": "territory_captured", "payload": {"territory": "pelennor", "new_owner": "gondor"}},
            {
                "type": "combat_ended",
                "payload": {
                    "winner": "attacker",
                    "attacker_faction": "gondor",
                    "defender_faction": "mordor",
                    "defender_casualty_ids": ["a", "b"],
                    "attacker_casualty_ids": ["c"],
                },
            },
            {"type": "combat_ended", "payload": {"winner": "draw", "attacker_faction": "gondor", "defender_faction": "mordor"}},
        ],
        {"minas_tirith"},
    )
    gondor = stats["by_faction"]["gondor"]
    assert gondor["strongholds_captured"] == 1
    assert gondor["territories_captured"] == 1
    assert gondor["battles_won"] == 1
    assert gondor["enemy_units_destroyed"] == 2
    assert stats["by_faction"]["mordor"]["enemy_units_destroyed"] == 1
    assert stats["by_faction"]["mordor"].get("battles_won", 0) == 0


def test_score_applies_multiplier_cap_rounds_and_forfeit():
    rules = _rules()
    by = {"gondor": {
        "strongholds_captured": 1,
        "territories_captured": 1,
        "enemy_units_destroyed": 2,
        "battles_won": 1,
    }}
    amount, meta = score_player(
        faction_ids=["gondor"],
        alliances={"gondor": "good", "mordor": "evil"},
        winner="good",
        forfeited=False,
        vs_ai=True,
        turn_number=4,
        by_faction=by,
        rules=rules,
    )
    # (50 + 100 + 15 + 2 + 2 + 3) * 0.5
    assert amount == 86
    assert meta["won"] is True
    short, _ = score_player(
        faction_ids=["gondor"],
        alliances={"gondor": "good"},
        winner="good",
        forfeited=False,
        vs_ai=False,
        turn_number=1,
        by_faction={},
        rules=rules,
    )
    assert short == 0
    forfeited, fmeta = score_player(
        faction_ids=["gondor"],
        alliances={"gondor": "good"},
        winner="evil",
        forfeited=True,
        vs_ai=False,
        turn_number=1,
        by_faction=by,
        rules=rules,
    )
    assert forfeited == 0
    assert fmeta["forfeited"] is True
    capped, cmeta = score_player(
        faction_ids=["gondor"],
        alliances={"gondor": "good"},
        winner="good",
        forfeited=False,
        vs_ai=False,
        turn_number=5,
        by_faction={"gondor": {"strongholds_captured": 100, "territories_captured": 0, "enemy_units_destroyed": 0, "battles_won": 0}},
        rules=_rules(stats_cap_per_game=40),
    )
    assert cmeta["stats_xp"] == 40
    assert capped == 50 + 100 + 40


def _definitions():
    return {
        "definitions": {
            "territories": {
                "minas_tirith": {"is_stronghold": True},
                "pelennor": {"is_stronghold": False},
            },
            "factions": {
                "gondor": {"alliance": "good"},
                "mordor": {"alliance": "evil"},
            },
        },
        "ai_factions": ["mordor"],
    }


def _add_game(db, players_list, *, status="active", turn=4, winner=None, config=None, completed_at=None):
    state = {
        "turn_number": turn,
        "current_faction": "gondor",
        "phase": "purchase",
        "territories": {},
        "faction_resources": {},
        "winner": winner,
    }
    row = Game(
        id=str(uuid.uuid4()),
        name="Test game",
        status=status,
        game_state=json.dumps(state),
        players=json.dumps(players_list),
        config=json.dumps(config if config is not None else _definitions()),
        completed_at=completed_at,
    )
    db.add(row)
    db.commit()
    return row


def _finish(db, row, events, turn=4, winner="good"):
    state = GameState.from_dict({
        "turn_number": turn,
        "current_faction": "gondor",
        "phase": "purchase",
        "territories": {},
        "faction_resources": {},
        "winner": winner,
    })
    save_game(row.id, state, db, events)
    db.refresh(row)
    return state


def test_save_game_awards_once_and_keeps_stats_across_saves(db_session, players):
    admin, user = players
    row = _add_game(db_session, [{"player_id": user.id, "faction_id": "gondor"}])
    _finish(db_session, row, [
        {"type": "territory_captured", "payload": {"territory": "minas_tirith", "new_owner": "gondor"}},
    ], winner=None)
    db_session.refresh(row)
    assert row.status == "active"
    config = json.loads(row.config)
    assert config["xp_stats"]["by_faction"]["gondor"]["strongholds_captured"] == 1

    _finish(db_session, row, [
        {"type": "combat_ended", "payload": {
            "winner": "attacker",
            "attacker_faction": "gondor",
            "defender_faction": "mordor",
            "defender_casualty_ids": ["a", "b"],
            "attacker_casualty_ids": ["c"],
        }},
        {"type": "territory_captured", "payload": {"territory": "pelennor", "new_owner": "gondor"}},
    ])
    db_session.refresh(user)
    assert user.xp == 86
    assert db_session.query(XpLedger).filter(XpLedger.player_id == user.id).count() == 1

    _finish(db_session, row, [
        {"type": "territory_captured", "payload": {"territory": "minas_tirith", "new_owner": "gondor"}},
    ])
    db_session.refresh(user)
    assert user.xp == 86
    assert db_session.query(XpLedger).count() == 1


def test_forfeit_gets_flat_xp_and_opponent_gets_the_win(db_session, players):
    admin, user = players
    other = Player(
        id=str(uuid.uuid4()),
        email="other@example.com",
        username="otheruser",
        password_hash="x",
        is_admin=False,
    )
    db_session.add(other)
    db_session.commit()
    config = _definitions()
    config["forfeited_player_ids"] = [user.id]
    config["ai_factions"] = []
    row = _add_game(
        db_session,
        [
            {"player_id": user.id, "faction_id": "gondor"},
            {"player_id": other.id, "faction_id": "mordor"},
        ],
        config=config,
    )
    _finish(db_session, row, [], winner="evil")
    db_session.refresh(user)
    db_session.refresh(other)
    assert user.xp == 0
    assert other.xp == 150


def test_daily_cap_limits_a_second_game_the_same_day(db_session, players, make_client):
    admin, user = players
    client = make_client(admin)
    cfg = default_progression()
    cfg["rules"] = _rules(
        game_completed=500,
        game_won=0,
        min_rounds_to_count=0,
        vs_ai_multiplier=1,
        multiplayer_multiplier=1,
        daily_cap=100,
        stats_cap_per_game=0,
    )
    saved = client.put("/admin/progression", json=cfg)
    assert saved.status_code == 200, saved.text
    first = _add_game(db_session, [{"player_id": user.id, "faction_id": "gondor"}], turn=1)
    _finish(db_session, first, [], turn=1)
    db_session.refresh(user)
    assert user.xp == 100
    meta = json.loads(db_session.query(XpLedger).one().meta_json)
    assert meta["daily_cap_trimmed"] == 400

    second = _add_game(db_session, [{"player_id": user.id, "faction_id": "gondor"}], turn=1)
    _finish(db_session, second, [], turn=1)
    db_session.refresh(user)
    assert user.xp == 100
    assert db_session.query(XpLedger).filter(XpLedger.player_id == user.id).count() == 1


def test_backfill_is_idempotent_and_reads_the_event_log(db_session, players, make_client):
    _admin, user = players
    config = _definitions()
    config["event_log"] = [
        {"type": "territory_captured", "payload": {"territory": "minas_tirith", "new_owner": "gondor"}},
    ]
    row = _add_game(
        db_session,
        [{"player_id": user.id, "faction_id": "gondor"}],
        status="finished",
        turn=4,
        winner="good",
        config=config,
        completed_at=datetime(2026, 1, 1, 8, 0, 0),
    )
    assert "xp_stats" not in json.loads(row.config)
    client = make_client(players[0])
    first = client.post("/admin/progression/backfill")
    assert first.status_code == 200, first.text
    body = first.json()
    # One stronghold (15) plus finishing and winning (150), halved because the only opponent is the computer.
    assert body["awarded"] == 1
    assert body["xp"] == 82
    db_session.refresh(user)
    assert user.xp == 82
    second = client.post("/admin/progression/backfill")
    assert second.json()["awarded"] == 0
    db_session.refresh(user)
    assert user.xp == 82


def test_admin_endpoints_and_profile_tier(db_session, players, make_client):
    admin, user = players
    stranger = make_client(user)
    assert stranger.get("/admin/progression").status_code == 403
    assert stranger.put("/admin/progression", json=default_progression()).status_code == 403

    public = stranger.get("/progression")
    assert public.status_code == 200
    assert "rules" not in public.json()
    assert public.json()["tiers"][0]["name"] == "Hobbit"
    assert "frame_src" in public.json()["tiers"][0]["visual"]

    me = stranger.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["xp"] == 0
    assert me.json()["tier"]["id"] == "hobbit"
    assert me.json()["next_tier"]["min_xp"] == 250

    client = make_client(admin)
    cfg = default_progression()
    cfg["tiers"][0]["name"] = "Shirefolk"
    cfg["tiers"][1]["min_xp"] = 20
    saved = client.put("/admin/progression", json=cfg)
    assert saved.status_code == 200, saved.text
    assert saved.json()["progression"]["tiers"][0]["name"] == "Shirefolk"

    listed = client.get("/admin/users")
    row = next(u for u in listed.json()["users"] if u["id"] == user.id)
    assert row["xp"] == 0
    assert row["tier_name"] == "Shirefolk"

    adjusted = client.post(f"/admin/users/{user.id}/xp", json={"amount": 25, "reason": "event prize"})
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["xp"] == 25
    assert adjusted.json()["tier_name"] == "Wanderer"
    clamped = client.post(f"/admin/users/{user.id}/xp", json={"amount": -1000, "reason": "reset"})
    assert clamped.json()["applied"] == -25
    assert clamped.json()["xp"] == 0
    assert client.post(f"/admin/users/{user.id}/xp", json={"amount": 0, "reason": "nope"}).status_code == 400

    cfg["tiers"][0]["min_xp"] = 5
    rejected = client.put("/admin/progression", json=cfg)
    assert rejected.status_code == 400


def test_avatar_icons_follow_tier(db_session, players, make_client):
    admin, user = players
    client = make_client(user)
    listed = client.get("/profile/avatar-icons")
    assert listed.status_code == 200
    # No saved tier means the highest tier, so a new player sees none of them.
    assert listed.json()["icons"] == []

    cfg = default_progression()
    cfg["icon_min_tier"] = {"hobbit.png": "hobbit", "gondor_soldier.png": "ranger"}
    saved = make_client(admin).put("/admin/progression", json=cfg)
    assert saved.status_code == 200, saved.text
    stored = saved.json()["progression"]["icon_min_tier"]
    assert stored["hobbit.png"] == "hobbit"
    assert stored["gondor_soldier.png"] == "ranger"
    assert "eagle.png" not in stored
    assert saved.json()["progression"]["tiers"][2]["rank"] == 3

    client = make_client(user)
    hidden = client.get("/profile/avatar-icons")
    hidden_names = [row["name"] for row in hidden.json()["icons"]]
    assert "hobbit.png" in hidden_names
    assert "gondor_soldier.png" not in hidden_names
    assert "eagle.png" not in hidden_names
    denied = client.patch("/auth/me", json={"avatar": {"image": "gondor_soldier.png"}})
    assert denied.status_code == 400

    user.xp = 750
    db_session.commit()
    unlocked = client.get("/profile/avatar-icons")
    row = next(item for item in unlocked.json()["icons"] if item["name"] == "gondor_soldier.png")
    assert row["new"] is True
    assert client.patch("/auth/me", json={"avatar": {"image": "gondor_soldier.png"}}).status_code == 200

    user.xp = 1500
    db_session.commit()
    later = client.get("/profile/avatar-icons")
    row = next(item for item in later.json()["icons"] if item["name"] == "gondor_soldier.png")
    assert row["new"] is False

    user.xp = 0
    db_session.commit()
    kept = client.patch("/auth/me", json={"avatar": {"image": "gondor_soldier.png", "color": "#445566"}})
    assert kept.status_code == 200, kept.text

    bad = default_progression()
    bad["icon_min_tier"] = {"hobbit.png": "no-such-tier"}
    assert make_client(admin).put("/admin/progression", json=bad).status_code == 400
    make_client(user)
