"""Marching Orders: optional rule flag plumbing (serialization, catalog, create-game)."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Game, Player
from backend.engine.state import GameState


def _empty_state(**overrides) -> GameState:
    return GameState(
        turn_number=1,
        current_faction="gondor",
        phase="purchase",
        territories={},
        faction_resources={},
        **overrides,
    )


def test_off_is_the_default_and_missing_keys_load_clean():
    raw = _empty_state().to_dict()
    assert raw["marching_orders"] is False
    assert raw["combat_move_moved_ids"] == []
    raw.pop("marching_orders")
    raw.pop("combat_move_moved_ids")
    loaded = GameState.from_dict(raw)
    assert loaded.marching_orders is False
    assert loaded.combat_move_moved_ids == []


def test_round_trip_preserves_flag_and_snapshot():
    state = _empty_state(
        marching_orders=True,
        combat_move_moved_ids=["gondor_soldier_001", "gondor_archer_002"],
    )
    loaded = GameState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert loaded.marching_orders is True
    assert loaded.combat_move_moved_ids == ["gondor_soldier_001", "gondor_archer_002"]


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
        password_hash="x",
        is_admin=True,
    )
    db_session.add(admin)
    db_session.commit()
    return admin


@pytest.fixture()
def client(db_session, players):
    def override_get_db():
        yield db_session

    def override_current_player():
        return players

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_player] = override_current_player
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_create_game_persists_marching_orders_flag(client, db_session):
    created = client.post(
        "/games/create",
        json={"name": "Orders", "setup_id": "wotr_exp_1.0", "marching_orders": True},
    )
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["marching_orders"] is True

    row = db_session.query(Game).filter(Game.id == game_id).first()
    stored = json.loads(row.game_state)
    assert stored["marching_orders"] is True
    assert stored["combat_move_moved_ids"] == []


def test_create_game_defaults_marching_orders_off(client, db_session):
    created = client.post("/games/create", json={"name": "No Orders", "setup_id": "wotr_exp_1.0"})
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    assert created.json()["state"]["marching_orders"] is False

    row = db_session.query(Game).filter(Game.id == game_id).first()
    stored = json.loads(row.game_state)
    assert stored["marching_orders"] is False
