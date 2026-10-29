"""Map-level icon/location overrides: GET /maps/{map_base}/overrides and /admin/maps routes."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import MapOverride, Player

VALID_PAYLOAD = {
    "territories": {
        "the_shire": {
            "focus": "override",
            "positions": {"unit": [100.5, 200], "marker": [110, 210.25]},
        },
        "sea_zone_1": {
            "focus": "override",
            "positions": {"unit": [10, 20], "number": [30, 40]},
        },
        "mordor": {"focus": "calculated"},
    }
}


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
    user = Player(
        id=str(uuid.uuid4()),
        email="user@example.com",
        username="normaluser",
        password_hash="x",
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
            from fastapi import HTTPException

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


def test_get_overrides_defaults_to_empty(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.get("/maps/wotr_map_1.2/overrides")
    assert r.status_code == 200
    body = r.json()
    assert body["map_base"] == "wotr_map_1.2"
    assert body["overrides"] == {"territories": {}}


def test_get_overrides_rejects_bad_map_base(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.get("/maps/bad..map/overrides")
    assert r.status_code == 400


def test_admin_maps_requires_admin(make_client, players):
    admin, user = players
    client = make_client(user)
    assert client.get("/admin/maps").status_code == 403
    client = make_client(admin)
    r = client.get("/admin/maps")
    assert r.status_code == 200
    maps = r.json()["maps"]
    assert maps == sorted(maps)
    assert "wotr_map_1.2" in maps  # known-map fallback + file setups


def test_put_overrides_requires_admin(make_client, players):
    admin, user = players
    client = make_client(user)
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=VALID_PAYLOAD)
    assert r.status_code == 403


def test_put_overrides_round_trip(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=VALID_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["map_base"] == "wotr_map_1.2"
    territories = body["overrides"]["territories"]
    assert territories["the_shire"]["focus"] == "override"
    assert territories["the_shire"]["positions"]["unit"] == [100.5, 200.0]
    # calculated entries are kept without positions
    assert territories["mordor"] == {"focus": "calculated"}

    # Public read returns the same blob (new client without admin — no auth needed)
    r2 = client.get("/maps/wotr_map_1.2/overrides")
    assert r2.status_code == 200
    assert r2.json()["overrides"] == body["overrides"]

    # Separate maps keep separate overrides
    r3 = client.get("/maps/motw_map_1.2/overrides")
    assert r3.status_code == 200
    assert r3.json()["overrides"] == {"territories": {}}


def test_put_overrides_accepts_bare_mapping(make_client, players):
    admin, _ = players
    client = make_client(admin)
    bare = {"the_shire": {"focus": "override", "positions": {"logo": {"x": 5, "y": 6}}}}
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=bare)
    assert r.status_code == 200
    stored = r.json()["overrides"]["territories"]["the_shire"]
    assert stored["positions"]["logo"] == [5.0, 6.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"territories": "nope"},
        {"territories": {"the_shire": {"focus": "sideways"}}},
        {"territories": {"the_shire": "not an object"}},
        {"territories": {"the_shire": {"focus": "override", "positions": {"vibes": [1, 2]}}}},
        {"territories": {"the_shire": {"focus": "override", "positions": {"unit": [1]}}}},
        {"territories": {"the_shire": {"focus": "override", "positions": {"unit": ["a", "b"]}}}},
        {"territories": {"the_shire": {"focus": "override", "positions": {"unit": [1e9, 0]}}}},
        {"territories": {"bad/tid": {"focus": "calculated"}}},
    ],
)
def test_put_overrides_rejects_invalid_payloads(make_client, players, payload):
    admin, _ = players
    client = make_client(admin)
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert r.status_code == 400


def test_put_overrides_rejects_nan_coordinates(make_client, players):
    admin, _ = players
    client = make_client(admin)
    # NaN cannot be sent via json= (json.dumps refuses it); send the literal token.
    raw = b'{"territories": {"the_shire": {"focus": "override", "positions": {"unit": [1, NaN]}}}}'
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_put_overrides_rejects_too_many_entries(make_client, players):
    admin, _ = players
    client = make_client(admin)
    payload = {
        "territories": {f"t{i}": {"focus": "calculated"} for i in range(401)}
    }
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert r.status_code == 400


def test_put_overrides_rejects_bad_map_base(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put("/admin/maps/bad..map/overrides", json=VALID_PAYLOAD)
    assert r.status_code == 400


def test_admin_maps_includes_saved_override_maps(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put("/admin/maps/custom_map_9.9/overrides", json={"territories": {}})
    assert r.status_code == 200
    maps = client.get("/admin/maps").json()["maps"]
    assert "custom_map_9.9" in maps


def test_normalize_map_overrides_lenient():
    from backend.map_overrides import normalize_map_overrides

    raw = {
        "territories": {
            "the_shire": {"focus": "override", "positions": {"unit": [1, 2], "vibes": [3, 4]}},
            "broken": {"focus": "sideways"},
            42: {"focus": "calculated"},
            "mordor": {"focus": "calculated", "positions": {"unit": [9, 9]}},
        }
    }
    out = normalize_map_overrides(raw)
    tids = out["territories"]
    assert set(tids) == {"the_shire", "mordor"}
    assert tids["the_shire"]["positions"] == {"unit": [1.0, 2.0]}
    # calculated entries drop positions entirely
    assert tids["mordor"] == {"focus": "calculated"}
    assert normalize_map_overrides(None) == {"territories": {}}
    assert normalize_map_overrides("nope") == {"territories": {}}


def test_seed_map_overrides_from_files(db_session, tmp_path, monkeypatch):
    import backend.map_overrides as map_overrides

    map_dir = tmp_path / "seeded_map_1.0"
    map_dir.mkdir()
    (map_dir / "overrides.json").write_text(
        json.dumps(
            {
                "territories": {
                    "the_shire": {"focus": "override", "positions": {"unit": [12, 34]}},
                    "junk": "junk",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(map_overrides, "MAPS_DIR", tmp_path)

    map_overrides.seed_map_overrides_if_empty(db_session)
    row = db_session.query(MapOverride).filter(MapOverride.map_base == "seeded_map_1.0").first()
    assert row is not None
    stored = json.loads(row.overrides_json)
    # seed normalizes leniently: junk dropped, valid entry kept
    assert stored["territories"]["the_shire"]["positions"]["unit"] == [12.0, 34.0]
    assert set(stored["territories"]) == {"the_shire"}

    # idempotent: a second run does not duplicate or clobber
    map_overrides.seed_map_overrides_if_empty(db_session)
    assert db_session.query(MapOverride).count() == 1


def test_save_map_overrides_files_source(tmp_path, monkeypatch):
    import backend.map_overrides as map_overrides

    monkeypatch.setattr(map_overrides, "MAPS_DIR", tmp_path)
    monkeypatch.setattr(map_overrides, "is_files_setup_source", lambda: True)

    data = map_overrides.save_map_overrides(
        None, "files_map_1.0", {"territories": {"x1": {"focus": "calculated"}}}
    )
    assert data == {"territories": {"x1": {"focus": "calculated"}}}
    on_disk = json.loads((tmp_path / "files_map_1.0" / "overrides.json").read_text(encoding="utf-8"))
    assert on_disk == data
    assert map_overrides.load_map_overrides(None, "files_map_1.0") == data
