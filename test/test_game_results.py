"""game_results foundation: writer at game end, forfeit flag, AI exclusion, idempotency, backfill."""

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.database import Base
from backend.api.game_results import (
    backfill_game_results,
    ensure_game_results_for_row,
)
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
        name="Results game",
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


def _results_for(session, game_id):
    return (
        session.query(GameResult)
        .filter(GameResult.game_id == str(game_id))
        .order_by(GameResult.player_id)
        .all()
    )


def _try_save_game(game_id, state, db):
    """Call main.save_game (imports after fixtures so tests exercise the real hook)."""
    from backend.api.main import save_game

    save_game(game_id, state, db)
    games_cache.pop(game_id, None)


def test_victory_end_writes_rows(db_session, players):
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "good"
    state.turn_number = 12
    state.rings_of_power = True
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
    )
    _try_save_game(game.id, state, db_session)
    db_session.refresh(game)
    assert game.status == "finished"

    rows = _results_for(db_session, game.id)
    assert len(rows) == 2
    by_pid = {r.player_id: r for r in rows}
    assert by_pid[alice.id].won is True
    assert by_pid[alice.id].alliance == "good"
    assert by_pid[alice.id].forfeited is False
    assert by_pid[bob.id].won is False
    assert by_pid[bob.id].alliance == "evil"
    assert by_pid[alice.id].setup_id == "wotr_exp_1.0"
    assert by_pid[alice.id].turn_count == 12
    assert by_pid[alice.id].rings_of_power is True
    assert by_pid[alice.id].is_ai is False
    participants = json.loads(by_pid[alice.id].participant_ids)
    assert set(participants) == {alice.id, bob.id}


def test_save_game_idempotent(db_session, players):
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "evil"
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
    )
    _try_save_game(game.id, state, db_session)
    first_count = len(_results_for(db_session, game.id))
    assert first_count == 2

    # Re-save after already finished: must not duplicate rows.
    _try_save_game(game.id, state, db_session)
    assert len(_results_for(db_session, game.id)) == first_count


def test_forfeit_flagged_and_computer_excluded(db_session, players):
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "evil"  # alice (good) forfeited; opposing alliance wins
    state.turn_number = 5
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
            {"player_id": "__computer__", "faction_id": "rohan"},
        ],
        state=state,
        config=_config(forfeited_player_ids=[alice.id]),
    )
    _try_save_game(game.id, state, db_session)

    rows = _results_for(db_session, game.id)
    by_pid = {r.player_id: r for r in rows}
    assert set(by_pid) == {alice.id, bob.id}  # computer excluded
    assert by_pid[alice.id].forfeited is True
    assert by_pid[alice.id].won is False
    assert by_pid[bob.id].forfeited is False
    assert by_pid[bob.id].won is True


def test_removed_forfeiter_still_gets_row(db_session, players):
    """A player who forfeited out of the roster still gets a forfeited row from forfeited_player_factions."""
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "evil"  # alice (good) forfeited and was removed; evil wins
    state.turn_number = 7
    game = _add_game(
        db_session,
        # alice no longer in roster (removed at leave time)
        players_list=[
            {"player_id": bob.id, "faction_id": "mordor"},
            {"player_id": "__computer__", "faction_id": "gondor"},
        ],
        state=state,
        config=_config(
            forfeited_player_ids=[alice.id],
            forfeited_player_factions={alice.id: ["gondor"]},
        ),
    )
    _try_save_game(game.id, state, db_session)

    rows = _results_for(db_session, game.id)
    by_pid = {r.player_id: r for r in rows}
    assert set(by_pid) == {alice.id, bob.id}
    assert by_pid[alice.id].forfeited is True
    assert by_pid[alice.id].won is False
    assert by_pid[alice.id].alliance == "good"
    assert by_pid[alice.id].faction_id == "gondor"
    assert by_pid[bob.id].forfeited is False
    assert by_pid[bob.id].won is True
    # Both humans are participants
    participants = json.loads(by_pid[alice.id].participant_ids)
    assert set(participants) == {alice.id, bob.id}


def test_removed_forfeiter_multi_faction_rows(db_session, players):
    """One row per faction the forfeiter controlled."""
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "evil"
    game = _add_game(
        db_session,
        players_list=[{"player_id": bob.id, "faction_id": "mordor"}],
        state=state,
        config=_config(
            forfeited_player_ids=[alice.id],
            forfeited_player_factions={alice.id: ["gondor", "rohan"]},
        ),
    )
    _try_save_game(game.id, state, db_session)

    rows = _results_for(db_session, game.id)
    alice_rows = [r for r in rows if r.player_id == alice.id]
    assert len(alice_rows) == 2
    assert {r.faction_id for r in alice_rows} == {"gondor", "rohan"}
    assert all(r.forfeited is True for r in alice_rows)
    assert all(r.won is False for r in alice_rows)


def test_duration_seconds_snapshot(db_session, players):
    alice, bob = players
    created = datetime(2026, 1, 1, 12, 0, 0)
    completed = created + timedelta(hours=3)
    state = GameState.from_dict({})
    state.winner = "good"
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
        created_at=created,
        completed_at=None,
    )
    game.completed_at = completed
    db_session.commit()
    # Direct writer (status already finished)
    game.status = "finished"
    db_session.commit()
    n = ensure_game_results_for_row(db_session, game)
    assert n == 2
    rows = _results_for(db_session, game.id)
    assert rows[0].duration_seconds == int((completed - created).total_seconds())


def test_backfill_existing_finished_games(db_session, players):
    alice, bob = players
    state = GameState.from_dict({})
    state.winner = "good"
    state.shadow_of_war = True
    state.turn_number = 8
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
        status="finished",
        completed_at=datetime.utcnow(),
    )
    # Pre-existing finished game has no results yet
    assert _results_for(db_session, game.id) == []

    n = backfill_game_results(db_session)
    assert n == 2
    rows = _results_for(db_session, game.id)
    assert len(rows) == 2
    by_pid = {r.player_id: r for r in rows}
    assert by_pid[alice.id].won is True
    assert by_pid[alice.id].shadow_of_war is True
    assert by_pid[alice.id].turn_count == 8

    # Second backfill is a no-op
    assert backfill_game_results(db_session) == 0
    assert len(_results_for(db_session, game.id)) == 2


def test_active_game_no_rows(db_session, players):
    alice, bob = players
    state = GameState.from_dict({})
    game = _add_game(
        db_session,
        players_list=[
            {"player_id": alice.id, "faction_id": "gondor"},
            {"player_id": bob.id, "faction_id": "mordor"},
        ],
        state=state,
        status="active",
    )
    assert ensure_game_results_for_row(db_session, game) == 0
    assert _results_for(db_session, game.id) == []
