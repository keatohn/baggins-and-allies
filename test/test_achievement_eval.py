"""Achievement evaluator: whitelist stats, criteria DSL, game-end + on-read hooks."""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.achievement_eval import (
    compute_achievement_stats,
    evaluate_and_award,
    evaluate_criteria,
    evaluate_for_game,
    player_achievement_grants,
    recompute_for_player,
)
from backend.api.achievements import create_achievement, seed_default_achievements
from backend.api.auth import get_current_admin, get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app, games as games_cache
from backend.api.models import Achievement, Game, GameResult, Player, PlayerAchievement
from backend.engine.state import GameState


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
    alice = Player(
        id=str(uuid.uuid4()),
        email="alice@example.com",
        username="alice",
        password_hash="x",
    )
    bob = Player(
        id=str(uuid.uuid4()),
        email="bob@example.com",
        username="bob",
        password_hash="x",
    )
    db_session.add_all([alice, bob])
    db_session.commit()
    return alice, bob


@pytest.fixture()
def make_client(db_session, players):
    alice, bob = players
    current = {"player": bob}

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
        current["player"] = bob


FACTION_DEFS = {
    "gondor": {"alliance": "good"},
    "mordor": {"alliance": "evil"},
    "rohan": {"alliance": "good"},
}


def _config(**overrides):
    cfg = {
        "setup_id": "wotr_exp_1.0",
        "definitions": {"factions": FACTION_DEFS},
        "forfeited_player_ids": [],
        "ai_factions": [],
    }
    cfg.update(overrides)
    return cfg


def _add_game(
    session,
    *,
    players_list,
    state=None,
    config=None,
    status="active",
    created_at=None,
    completed_at=None,
):
    if state is None:
        state = GameState.from_dict({})
    game = Game(
        id=str(uuid.uuid4()),
        name="Eval game",
        status=status,
        created_by=players_list[0]["player_id"] if players_list else None,
        game_state=json.dumps(state.to_dict() if hasattr(state, "to_dict") else state),
        players=json.dumps(players_list),
        config=json.dumps(config or _config()),
        created_at=created_at or datetime.utcnow() - timedelta(hours=2),
        completed_at=completed_at,
    )
    session.add(game)
    session.commit()
    return game


def _try_save_game(game_id, state, db):
    from backend.api.main import save_game

    save_game(game_id, state, db)
    games_cache.pop(game_id, None)


def _finish_game(session, *, winner, alice, bob, turn_number=10, created_at=None):
    """Add + save a finished game; returns the Game row."""
    state = GameState.from_dict({})
    state.winner = winner
    state.turn_number = turn_number
    game = _add_game(
        session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
        status="active",
        created_at=created_at,
    )
    _try_save_game(game.id, state, session)
    session.refresh(game)
    return game


def _grant_ids(session, player_id):
    rows = (
        session.query(PlayerAchievement.achievement_id)
        .filter(PlayerAchievement.player_id == str(player_id))
        .all()
    )
    return {str(r[0]) for r in rows}


# ----- stats catalog -----

class TestComputeAchievementStats:
    def test_empty_player(self, db_session, players):
        alice, _ = players
        stats = compute_achievement_stats(db_session, alice.id)
        assert stats["games_played"] == 0
        assert stats["wins"] == 0
        assert stats["win_pct"] == 0.0
        assert stats["longest_game_turns"] == 0

    def test_full_catalog_from_results(self, db_session, players):
        alice, bob = players
        base = datetime.utcnow() - timedelta(hours=5)
        g1 = _finish_game(db_session, winner="good", alice=alice, bob=bob, turn_number=12)
        g2 = _finish_game(db_session, winner="good", alice=alice, bob=bob, turn_number=8)
        g3 = _finish_game(db_session, winner="evil", alice=alice, bob=bob, turn_number=30)
        # game_results.created_at is stamped at write time (identical here); force order W,W,L.
        for i, gid in enumerate((g1.id, g2.id, g3.id)):
            for r in db_session.query(GameResult).filter(GameResult.game_id == gid):
                r.created_at = base + timedelta(hours=i)
        db_session.commit()
        stats = compute_achievement_stats(db_session, alice.id)
        assert stats["games_played"] == 3
        assert stats["wins"] == 2  # alice is good in all three
        assert stats["losses"] == 1
        assert stats["win_pct"] == 66.7
        assert stats["wins_as_good"] == 2
        assert stats["wins_as_evil"] == 0
        assert stats["games_as_good"] == 3
        assert stats["best_win_streak"] == 2  # W, W, L
        assert stats["longest_game_turns"] == 30

    def test_evil_wins_counted(self, db_session, players):
        alice, bob = players
        # Flip alliances: alice plays mordor (evil) by swapping roster order
        state = GameState.from_dict({})
        state.winner = "evil"
        state.turn_number = 5
        game = _add_game(
            db_session,
            players_list=[
                {"player_id": alice.id, "faction_id": "mordor"},
                {"player_id": bob.id, "faction_id": "gondor"},
            ],
            state=state,
        )
        _try_save_game(game.id, state, db_session)
        stats = compute_achievement_stats(db_session, alice.id)
        assert stats["wins_as_evil"] == 1
        assert stats["games_as_evil"] == 1


# ----- criteria evaluation -----

class TestEvaluateCriteria:
    def test_leaf_stats(self):
        stats = {"wins": 5, "win_pct": 50.0}
        assert evaluate_criteria({"stat": "wins", "op": ">=", "value": 5}, stats)
        assert not evaluate_criteria({"stat": "wins", "op": ">=", "value": 6}, stats)
        assert evaluate_criteria({"stat": "wins", "op": "==", "value": 5}, stats)
        assert evaluate_criteria({"stat": "wins", "op": "=", "value": 5}, stats)
        assert evaluate_criteria({"stat": "wins", "op": "!=", "value": 6}, stats)
        assert evaluate_criteria({"stat": "win_pct", "op": ">", "value": 49.9}, stats)

    def test_combinators(self):
        stats = {"wins": 5, "losses": 3}
        both = {"all": [
            {"stat": "wins", "op": ">=", "value": 5},
            {"stat": "losses", "op": ">=", "value": 3},
        ]}
        either = {"any": [
            {"stat": "wins", "op": ">=", "value": 10},
            {"stat": "losses", "op": ">=", "value": 3},
        ]}
        negate = {"not": {"stat": "wins", "op": ">=", "value": 10}}
        assert evaluate_criteria(both, stats)
        assert evaluate_criteria(either, stats)
        assert evaluate_criteria(negate, stats)

    def test_game_context_matched(self):
        stats = {"wins": 1}
        criteria = {"stat": "wins", "op": ">=", "value": 1, "game_context": {"won": True, "min_turns": 25}}
        ctx_row = {"won": True, "turn_count": 30}
        assert evaluate_criteria(criteria, stats, ctx_row)
        short_row = {"won": True, "turn_count": 10}
        assert not evaluate_criteria(criteria, stats, short_row)
        loss_row = {"won": False, "turn_count": 30}
        assert not evaluate_criteria(criteria, stats, loss_row)

    def test_game_context_without_row_is_false(self):
        stats = {"wins": 100}
        criteria = {"stat": "wins", "op": ">=", "value": 1, "game_context": {"won": True}}
        assert not evaluate_criteria(criteria, stats, None)

    def test_game_context_opponent_count(self):
        stats = {"wins": 1}
        criteria = {
            "stat": "wins",
            "op": ">=",
            "value": 1,
            "game_context": {"won": True, "opponent_count": {"op": ">=", "value": 3}},
        }
        big_row = {"won": True, "opponent_count": 3}
        small_row = {"won": True, "opponent_count": 2}
        assert evaluate_criteria(criteria, stats, big_row)
        assert not evaluate_criteria(criteria, stats, small_row)


# ----- game-end hook (via save_game) -----

class TestGameEndHook:
    def test_winner_awarded_first_victory(self, db_session, players):
        alice, bob = players
        ach = create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        assert ach.id in _grant_ids(db_session, alice.id)
        assert ach.id not in _grant_ids(db_session, bob.id)  # bob lost

    def test_no_duplicate_on_resave(self, db_session, players):
        alice, bob = players
        ach = create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        state = GameState.from_dict({})
        state.winner = "good"
        game = _add_game(
            db_session,
            players_list=[
                {"player_id": alice.id, "faction_id": "gondor"},
                {"player_id": bob.id, "faction_id": "mordor"},
            ],
            state=state,
        )
        _try_save_game(game.id, state, db_session)
        _try_save_game(game.id, state, db_session)  # idempotent re-save
        grants = (
            db_session.query(PlayerAchievement)
            .filter(PlayerAchievement.player_id == alice.id)
            .all()
        )
        assert len(grants) == 1
        assert grants[0].achievement_id == ach.id

    def test_game_context_awarded_at_game_end(self, db_session, players):
        alice, bob = players
        ach = create_achievement(
            db_session,
            key="marathon",
            name="Marathon",
            criteria={
                "stat": "wins",
                "op": ">=",
                "value": 1,
                "game_context": {"won": True, "min_turns": 25},
            },
        )
        _finish_game(db_session, winner="good", alice=alice, bob=bob, turn_number=30)
        assert ach.id in _grant_ids(db_session, alice.id)

        # A short win must NOT award it (for bob's side: bob lost anyway; use a fresh pair)
        carol = Player(id=str(uuid.uuid4()), email="c@x.com", username="carol", password_hash="x")
        dave = Player(id=str(uuid.uuid4()), email="d@x.com", username="dave", password_hash="x")
        db_session.add_all([carol, dave])
        db_session.commit()
        _finish_game(db_session, winner="good", alice=carol, bob=dave, turn_number=5)
        assert ach.id not in _grant_ids(db_session, carol.id)

    def test_alliance_scoped_stat(self, db_session, players):
        alice, bob = players
        ach = create_achievement(
            db_session,
            key="evil_conqueror",
            name="Evil Conqueror",
            criteria={"stat": "wins_as_evil", "op": ">=", "value": 1},
        )
        # alice plays evil (mordor) and wins
        state = GameState.from_dict({})
        state.winner = "evil"
        game = _add_game(
            db_session,
            players_list=[
                {"player_id": alice.id, "faction_id": "mordor"},
                {"player_id": bob.id, "faction_id": "gondor"},
            ],
            state=state,
        )
        _try_save_game(game.id, state, db_session)
        assert ach.id in _grant_ids(db_session, alice.id)
        assert ach.id not in _grant_ids(db_session, bob.id)


# ----- on-read recompute -----

class TestOnReadRecompute:
    def test_awards_stats_only_after_definition_added(self, db_session, players):
        alice, bob = players
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        # Achievement created AFTER the game was played
        ach = create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        assert ach.id not in _grant_ids(db_session, alice.id)
        recompute_for_player(db_session, alice.id)
        assert ach.id in _grant_ids(db_session, alice.id)

    def test_skips_game_context_on_read(self, db_session, players):
        alice, bob = players
        _finish_game(db_session, winner="good", alice=alice, bob=bob, turn_number=30)
        ach = create_achievement(
            db_session,
            key="marathon",
            name="Marathon",
            criteria={
                "stat": "wins",
                "op": ">=",
                "value": 1,
                "game_context": {"won": True, "min_turns": 25},
            },
        )
        recompute_for_player(db_session, alice.id)
        assert ach.id not in _grant_ids(db_session, alice.id)  # on-read cannot award context

    def test_idempotent(self, db_session, players):
        alice, bob = players
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        recompute_for_player(db_session, alice.id)
        recompute_for_player(db_session, alice.id)
        grants = (
            db_session.query(PlayerAchievement)
            .filter(PlayerAchievement.player_id == alice.id)
            .all()
        )
        assert len(grants) == 1

    def test_no_op_for_unknown_player(self, db_session):
        # No crash for a player with no games / no achievements
        recompute_for_player(db_session, str(uuid.uuid4()))


# ----- default seeds -----

class TestDefaultSeeds:
    def test_seeds_once_and_skips_existing(self, db_session):
        n1 = seed_default_achievements(db_session)
        assert n1 >= 5
        n2 = seed_default_achievements(db_session)
        assert n2 == 0
        rows = db_session.query(Achievement).all()
        assert len(rows) == n1

    def test_seeded_defaults_evaluate(self, db_session, players):
        alice, bob = players
        seed_default_achievements(db_session)
        # first_victory (wins>=1) should award on alice's win
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        grants = player_achievement_grants(db_session, alice.id)
        keys = {g["key"] for g in grants}
        assert "first_victory" in keys
        assert "marathon_battle" not in keys  # turn_number=10 < 25


# ----- profile grant listing -----

class TestPlayerAchievementGrants:
    def test_grants_include_definition_fields(self, db_session, players):
        alice, bob = players
        ach = create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            description="Win once.",
            category="wins",
            tier="bronze",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        grants = player_achievement_grants(db_session, alice.id)
        assert len(grants) == 1
        g = grants[0]
        assert g["key"] == "first_victory"
        assert g["name"] == "First Victory"
        assert g["tier"] == "bronze"
        assert g["awarded_at"] is not None
        assert g["criteria"] == {"stat": "wins", "op": ">=", "value": 1}


# ----- profile endpoint integration -----

class TestProfileEndpoint:
    def test_profile_returns_awarded_achievements(self, db_session, players, make_client):
        alice, bob = players
        create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            description="Win once.",
            category="wins",
            tier="bronze",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        client = make_client(bob)  # bob views alice's profile
        r = client.get(f"/players/{alice.id}/profile")
        assert r.status_code == 200
        data = r.json()
        assert data["achievements"]
        keys = {a["key"] for a in data["achievements"]}
        assert "first_victory" in keys

    def test_profile_recompute_awards_backlogged(self, db_session, players, make_client):
        alice, bob = players
        _finish_game(db_session, winner="good", alice=alice, bob=bob)
        # Achievement defined AFTER the game; profile read triggers on-read recompute.
        create_achievement(
            db_session,
            key="first_victory",
            name="First Victory",
            criteria={"stat": "wins", "op": ">=", "value": 1},
        )
        client = make_client(bob)
        r = client.get(f"/players/{alice.id}/profile")
        assert r.status_code == 200
        keys = {a["key"] for a in r.json()["achievements"]}
        assert "first_victory" in keys

    def test_profile_404_unknown_player(self, db_session, players, make_client):
        _alice, bob = players
        client = make_client(bob)
        r = client.get(f"/players/{uuid.uuid4()}/profile")
        assert r.status_code == 404
