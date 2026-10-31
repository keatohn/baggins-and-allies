"""Player stats endpoints + finished-games list filter (Phase 1 profile stats)."""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app, save_game
from backend.api.main import games as games_cache
from backend.api.models import Game, GameResult, Player
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
    alice = Player(id=str(uuid.uuid4()), email="alice@example.com", username="alice", password_hash="x")
    bob = Player(id=str(uuid.uuid4()), email="bob@example.com", username="bob", password_hash="x")
    carol = Player(id=str(uuid.uuid4()), email="carol@example.com", username="carol", password_hash="x")
    db_session.add_all([alice, bob, carol])
    db_session.commit()
    return alice, bob, carol


@pytest.fixture()
def make_client(db_session, players):
    alice, _bob, _carol = players
    current = {"player": alice}

    def override_get_db():
        yield db_session

    def override_current_player():
        return current["player"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player

    def _make(as_player=None):
        if as_player is not None:
            current["player"] = as_player
        return TestClient(app)

    try:
        yield _make
    finally:
        app.dependency_overrides.clear()
        current["player"] = alice


FACTION_DEFS = {
    "gondor": {"alliance": "good"},
    "rohan": {"alliance": "good"},
    "mordor": {"alliance": "evil"},
    "isengard": {"alliance": "evil"},
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


def _finish_game(
    session,
    *,
    players_list,
    winner,
    setup_id="wotr_exp_1.0",
    forfeited_player_ids=None,
    turn_number=10,
    created_at=None,
):
    """Create a game and flip it finished via save_game (writes game_results)."""
    state = GameState.from_dict({})
    state.winner = winner
    state.turn_number = turn_number
    created = created_at or (datetime.utcnow() - timedelta(days=1))
    game = Game(
        id=str(uuid.uuid4()),
        name="Stats game",
        status="active",
        created_by=players_list[0]["player_id"] if players_list else None,
        game_state=json.dumps(state.to_dict()),
        players=json.dumps(players_list),
        config=json.dumps(_config(setup_id=setup_id, forfeited_player_ids=forfeited_player_ids or [])),
        created_at=created,
    )
    session.add(game)
    session.commit()
    save_game(game.id, state, session)
    games_cache.pop(game.id, None)
    session.refresh(game)
    return game


def test_stats_overall_excludes_forfeits_but_counts_them(db_session, players, make_client):
    alice, bob, _carol = players
    # alice wins as good vs bob
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
    )
    # alice loses as good vs bob
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "rohan"},
            {"player_id": bob.id, "faction_id": "isengard"},
        ],
        winner="evil",
    )
    # alice forfeits
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="evil",
        forfeited_player_ids=[alice.id],
    )

    client = make_client(alice)
    r = client.get(f"/players/{alice.id}/stats")
    assert r.status_code == 200
    data = r.json()
    overall = data["overall"]
    assert overall["games"] == 2
    assert overall["wins"] == 1
    assert overall["losses"] == 1
    assert overall["win_pct"] == 50.0
    assert overall["forfeits"] == 1
    assert data["by_alliance"]["good"]["games"] == 2
    assert data["by_alliance"]["good"]["wins"] == 1
    assert data["by_alliance"]["evil"]["games"] == 0


def test_stats_scenario_filter_and_display_name(db_session, players, make_client):
    alice, bob, _carol = players
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
        setup_id="wotr_exp_1.0",
    )
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="evil",
        setup_id="fellowship",
    )

    client = make_client(alice)
    r = client.get(f"/players/{alice.id}/stats")
    data = r.json()
    assert data["overall"]["games"] == 2
    assert len(data["by_scenario"]) == 2
    names = {s["scenario_name"] for s in data["by_scenario"]}
    # Snapshot may be None if setup not in DB; setup_id always present for grouping
    sids = {s["setup_id"] for s in data["by_scenario"]}
    assert sids == {"wotr_exp_1.0", "fellowship"}

    r2 = client.get(f"/players/{alice.id}/stats", params={"scenario": "fellowship"})
    d2 = r2.json()
    assert d2["overall"]["games"] == 1
    assert d2["overall"]["losses"] == 1


def test_stats_head_to_head_and_peer_filters(db_session, players, make_client):
    alice, bob, carol = players
    # Game 1: alice(gondor) + carol(rohan) vs bob(mordor) — alice wins
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": carol.id, "faction_id": "rohan"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
    )
    # Game 2: alice(gondor) vs bob(mordor)+carol(isengard) — evil wins, carol is opponent
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
            {"player_id": carol.id, "faction_id": "isengard"},
        ],
        winner="evil",
    )

    client = make_client(alice)
    r = client.get(f"/players/{alice.id}/stats")
    data = r.json()
    h2h = {e["player_id"]: e for e in data["head_to_head"]}
    assert h2h[bob.id]["kind"] == "against"
    assert h2h[bob.id]["games"] == 2
    assert h2h[bob.id]["wins"] == 1
    assert h2h[bob.id]["username"] == "bob"
    assert h2h[carol.id]["games"] == 2
    # carol was with alice once and against once; kind collapses to "against"
    assert h2h[carol.id]["kind"] == "against"

    # opponent=bob: both games
    r_opp = client.get(f"/players/{alice.id}/stats", params={"opponent": bob.id})
    assert r_opp.json()["overall"]["games"] == 2

    # opponent=carol: only game 2 (against)
    r_opp2 = client.get(f"/players/{alice.id}/stats", params={"opponent": carol.id})
    assert r_opp2.json()["overall"]["games"] == 1

    # teammate=carol: only game 1 (with)
    r_team = client.get(f"/players/{alice.id}/stats", params={"teammate": carol.id})
    assert r_team.json()["overall"]["games"] == 1
    assert r_team.json()["overall"]["wins"] == 1


def test_stats_alliance_filter_and_streaks(db_session, players, make_client):
    alice, bob, _carol = players
    # alice as evil wins twice
    for _ in range(2):
        _finish_game(
            db_session,
            players_list=[
                {"player_id": alice.id, "faction_id": "mordor"},
                {"player_id": bob.id, "faction_id": "gondor"},
            ],
            winner="evil",
        )
    # alice as good loses once
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="evil",
    )

    client = make_client(alice)
    r = client.get(f"/players/{alice.id}/stats", params={"alliance": "evil"})
    data = r.json()
    assert data["overall"]["games"] == 2
    assert data["overall"]["wins"] == 2
    assert data["streaks"]["current_win"] == 2
    assert data["streaks"]["best_win_streak"] == 2

    r_all = client.get(f"/players/{alice.id}/stats")
    assert r_all.json()["overall"]["games"] == 3
    assert r_all.json()["streaks"]["best_loss_streak"] == 1


def test_profile_endpoint_public_card(db_session, players, make_client):
    alice, bob, _carol = players
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
    )
    # bob views alice's profile (public)
    client = make_client(bob)
    r = client.get(f"/players/{alice.id}/profile")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == alice.id
    assert data["username"] == "alice"
    assert data["headline"]["games"] == 1
    assert data["headline"]["wins"] == 1
    assert data["tier"] is None
    assert data["achievements"] == []

    r404 = client.get(f"/players/{uuid.uuid4()}/profile")
    assert r404.status_code == 404


def test_list_games_status_finished(db_session, players, make_client):
    alice, bob, _carol = players
    finished = _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
    )
    forfeited = _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="evil",
        forfeited_player_ids=[alice.id],
    )
    active = Game(
        id=str(uuid.uuid4()),
        name="Still going",
        status="active",
        game_state=json.dumps({}),
        players=json.dumps([{"player_id": alice.id, "faction_id": "gondor"}]),
        config=json.dumps(_config()),
    )
    db_session.add(active)
    db_session.commit()

    client = make_client(alice)

    # Default: only active (not finished, not forfeited)
    r = client.get("/games")
    default_ids = {g["id"] for g in r.json()["games"]}
    assert str(active.id) in default_ids
    assert str(finished.id) not in default_ids
    assert str(forfeited.id) not in default_ids

    # status=finished: finished games alice was in, including the one she forfeited
    r_f = client.get("/games", params={"status": "finished"})
    fin_ids = {g["id"] for g in r_f.json()["games"]}
    assert str(finished.id) in fin_ids
    assert str(forfeited.id) in fin_ids
    assert str(active.id) not in fin_ids
    # Winner surfaced on finished items
    by_id = {g["id"]: g for g in r_f.json()["games"]}
    assert by_id[str(finished.id)]["winner"] == "good"
    assert by_id[str(finished.id)]["completed_at"] is not None

    # status=all: everything alice is in
    r_all = client.get("/games", params={"status": "all"})
    all_ids = {g["id"] for g in r_all.json()["games"]}
    assert {str(active.id), str(finished.id), str(forfeited.id)} <= all_ids


def test_multi_faction_rows_collapse_to_one_game(db_session, players, make_client):
    alice, bob, carol = players
    # alice controls two good factions in one game
    _finish_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": alice.id, "faction_id": "rohan"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        winner="good",
    )
    client = make_client(alice)
    data = client.get(f"/players/{alice.id}/stats").json()
    assert data["overall"]["games"] == 1
    assert data["overall"]["wins"] == 1
