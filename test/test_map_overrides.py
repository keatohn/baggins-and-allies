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
from backend.api.models import MapOverride, Player, Setup
from backend.engine.definitions import TerritoryDefinition

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
    # Fallback list covers every frontend MAP_CONFIG / asset map (incl. the live WotLA map).
    for base in ("motw_map_1.2", "wotla_map_1.0", "wotla_map_1.1", "woteas_map_1.0", "wotr_map_1.0", "wotr_map_1.1", "wotr_map_1.2"):
        assert base in maps


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
    assert "landscape" not in out
    assert normalize_map_overrides(None) == {"territories": {}}
    assert normalize_map_overrides("nope") == {"territories": {}}


LANDSCAPE_PAYLOAD = {
    "territories": {
        "the_shire": {"focus": "calculated"},
    },
    "landscape": {
        "territories": {
            "the_shire": [
                {"cat": "forest", "img": "forest_large.png", "x": 10.5, "y": 20, "layer": "interior", "auto": True},
                {"cat": "mountains", "img": "mountain_range.png", "x": 30, "y": 40, "layer": "border", "s": 1.5, "r": 90},
            ],
            "mordor": [
                {"cat": "volcano", "img": "volcano.png", "x": 1, "y": 2},
            ],
        }
    },
}


def test_put_overrides_landscape_round_trip(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=LANDSCAPE_PAYLOAD)
    assert r.status_code == 200
    landscape = r.json()["overrides"]["landscape"]["territories"]
    assert landscape["the_shire"][0]["cat"] == "forest"
    assert landscape["the_shire"][0]["auto"] is True
    assert landscape["the_shire"][1]["layer"] == "border"
    assert landscape["the_shire"][1]["s"] == 1.5
    assert landscape["the_shire"][1]["r"] == 90
    # missing layer defaults to interior; missing s omitted
    assert landscape["mordor"][0]["layer"] == "interior"
    assert "s" not in landscape["mordor"][0]
    # public read returns the same blob
    r2 = client.get("/maps/wotr_map_1.2/overrides")
    assert r2.json()["overrides"]["landscape"] == r.json()["overrides"]["landscape"]


def test_put_overrides_landscape_only_no_territories(make_client, players):
    admin, _ = players
    client = make_client(admin)
    payload = {"landscape": {"territories": {"mordor": [{"cat": "volcano", "img": "volcano.png", "x": 5, "y": 6}]}}}
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert r.status_code == 200
    body = r.json()["overrides"]
    # "landscape" must not be misread as a territory id
    assert body["territories"] == {}
    assert body["landscape"]["territories"]["mordor"][0]["img"] == "volcano.png"


@pytest.mark.parametrize(
    "landscape",
    [
        "nope",
        {"territories": "nope"},
        {"territories": {"the_shire": "nope"}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "evil.jpg", "x": 1, "y": 2}]}},  # not .png
        {"territories": {"the_shire": [{"cat": "forest", "img": "../secret.png", "x": 1, "y": 2}]}},
        {"territories": {"the_shire": [{"cat": "a/b", "img": "x.png", "x": 1, "y": 2}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": 1, "y": 2, "layer": "sideways"}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": "a", "y": 2}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": 1, "y": 2, "s": 99}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": 1, "y": 2, "r": "sideways"}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": 1, "y": 2, "r": True}]}},
        {"territories": {"the_shire": [{"cat": "forest", "img": "x.png", "x": 1e9, "y": 2}]}},
    ],
)
def test_put_overrides_rejects_invalid_landscape(make_client, players, landscape):
    admin, _ = players
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"territories": {}, "landscape": landscape},
    )
    assert r.status_code == 400


def test_put_overrides_rejects_too_many_landscape_placements(make_client, players):
    admin, _ = players
    client = make_client(admin)
    payload = {
        "territories": {},
        "landscape": {
            "territories": {
                "the_shire": [
                    {"cat": "forest", "img": "f.png", "x": i, "y": i} for i in range(51)
                ]
            }
        },
    }
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert r.status_code == 400


def test_landscape_rotation_wraps_and_drops_full_turns():
    from backend.map_overrides import normalize_map_overrides

    out = normalize_map_overrides(
        {
            "landscape": {
                "territories": {
                    "mordor": [
                        {"cat": "landmarks", "img": "minas_tirith.png", "x": 1, "y": 2, "r": -90},
                        {"cat": "forest", "img": "f.png", "x": 1, "y": 2, "r": 360},
                    ]
                }
            }
        }
    )
    rows = out["landscape"]["territories"]["mordor"]
    assert rows[0]["r"] == 270
    assert "r" not in rows[1]


def test_normalize_map_overrides_landscape_lenient():
    from backend.map_overrides import normalize_map_overrides

    raw = {
        "territories": {"the_shire": {"focus": "calculated"}},
        "landscape": {
            "territories": {
                "the_shire": [
                    {"cat": "forest", "img": "forest_large.png", "x": 1, "y": 2, "layer": "interior"},
                    {"cat": "forest", "img": "bad.jpg", "x": 1, "y": 2},
                    {"cat": "forest", "img": "ok.png", "x": "nan", "y": 2},
                    "junk",
                ],
                "broken": "not-a-list",
            }
        },
    }
    out = normalize_map_overrides(raw)
    placements = out["landscape"]["territories"]["the_shire"]
    assert len(placements) == 1
    assert placements[0]["img"] == "forest_large.png"
    assert "broken" not in out["landscape"]["territories"]
    # landscape-only payload keeps empty territories, does not invent a "landscape" tid
    only = normalize_map_overrides({"landscape": {"territories": {"mordor": [{"cat": "volcano", "img": "v.png", "x": 0, "y": 0}]}}})
    assert only["territories"] == {}
    assert only["landscape"]["territories"]["mordor"][0]["cat"] == "volcano"


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


def _setup_row(setup_id: str, map_base: str, terrain: dict[str, str]) -> Setup:
    territories = {
        tid: {"id": tid, "display_name": tid, "terrain_type": name, "adjacent": [], "produces": {"power": 0}}
        for tid, name in terrain.items()
    }
    return Setup(
        id=setup_id,
        manifest_json=json.dumps({"id": setup_id, "map_asset": map_base}),
        units_json="{}",
        territories_json=json.dumps(territories),
        factions_json="{}",
        camps_json="{}",
        ports_json="{}",
        starting_setup_json="{}",
        specials_json="{}",
    )


def test_seed_map_terrain_from_db_setups_and_keeps_edits(db_session):
    from backend.map_overrides import seed_map_terrain_if_missing

    db_session.add(_setup_row("wotr_1.1", "wotr_map_1.2", {"the_shire": "Hills", "mordor": "volcano"}))
    db_session.add(_setup_row("wotr_2.0", "wotr_map_1.2", {"the_shire": "hills", "mordor": "volcano"}))
    db_session.add(
        MapOverride(
            map_base="wotr_map_1.2",
            overrides_json=json.dumps(
                {
                    "territories": {},
                    "landscape": {
                        "territories": {
                            "mordor": [{"cat": "volcano", "img": "volcano.png", "x": 1, "y": 2}],
                        }
                    },
                }
            ),
        )
    )
    db_session.commit()

    assert seed_map_terrain_if_missing(db_session) == 1
    row = db_session.query(MapOverride).filter(MapOverride.map_base == "wotr_map_1.2").one()
    stored = json.loads(row.overrides_json)
    assert stored["terrain"] == {"the_shire": "hills", "mordor": "volcano"}
    assert stored["landscape"]["territories"]["mordor"][0]["img"] == "volcano.png"

    stored["terrain"]["the_shire"] = "forest"
    row.overrides_json = json.dumps(stored)
    db_session.commit()
    assert seed_map_terrain_if_missing(db_session) == 0
    row = db_session.query(MapOverride).filter(MapOverride.map_base == "wotr_map_1.2").one()
    assert json.loads(row.overrides_json)["terrain"]["the_shire"] == "forest"


def test_put_terrain_round_trip_and_mirrors_setups(make_client, players, db_session):
    admin, _ = players
    db_session.add(_setup_row("wotr_1.1", "wotr_map_1.2", {"the_shire": "plains"}))
    db_session.add(_setup_row("wotr_2.0", "wotr_map_1.2", {"the_shire": "plains"}))
    db_session.commit()
    client = make_client(admin)
    payload = {"territories": {}, "terrain": {"the_shire": "hills"}}
    r = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert r.status_code == 200
    assert r.json()["overrides"]["terrain"]["the_shire"] == "hills"
    got = client.get("/maps/wotr_map_1.2/overrides")
    assert got.json()["overrides"]["terrain"]["the_shire"] == "hills"
    for setup_id in ("wotr_1.1", "wotr_2.0"):
        row = db_session.query(Setup).filter(Setup.id == setup_id).one()
        assert json.loads(row.territories_json)["the_shire"]["terrain_type"] == "hills"


def test_put_terrain_rejects_unknown_catalog_type(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"territories": {}, "terrain": {"the_shire": "not_a_terrain"}},
    )
    assert r.status_code == 400


def test_terrain_sibling_is_not_a_territory_id(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"terrain": {"the_shire": "lake"}},
    )
    assert r.status_code == 200
    body = r.json()["overrides"]
    assert body["territories"] == {}
    assert body["terrain"]["the_shire"] == "lake"


def test_apply_map_terrain_overrides_definition(db_session):
    from backend.map_overrides import apply_map_terrain

    db_session.add(
        MapOverride(
            map_base="wotr_map_1.2",
            overrides_json=json.dumps({"territories": {}, "terrain": {"the_shire": "hills"}}),
        )
    )
    db_session.commit()
    defs = {
        "the_shire": TerritoryDefinition(
            id="the_shire",
            display_name="The Shire",
            terrain_type="plains",
            adjacent=[],
            produces={"power": 1},
        )
    }
    apply_map_terrain(defs, "wotr_map_1.2", db_session)
    assert defs["the_shire"].terrain_type == "hills"


def _graph_setup(setup_id: str, map_base: str, adjacent: dict[str, list[str]]) -> Setup:
    territories = {
        tid: {
            "id": tid,
            "display_name": tid,
            "terrain_type": "plains",
            "adjacent": neighbors,
            "aerial_adjacent": [],
            "ford_adjacent": [],
            "produces": {"power": 0},
        }
        for tid, neighbors in adjacent.items()
    }
    return Setup(
        id=setup_id,
        manifest_json=json.dumps({"id": setup_id, "map_asset": map_base}),
        units_json="{}",
        territories_json=json.dumps(territories),
        factions_json="{}",
        camps_json="{}",
        ports_json="{}",
        starting_setup_json="{}",
        specials_json="{}",
    )


def test_graph_sibling_is_not_a_territory_id(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"graph": {"adjacent": {"the_shire": ["bree"]}}},
    )
    assert r.status_code == 200
    body = r.json()["overrides"]
    assert body["territories"] == {}
    assert body["graph"]["adjacent"]["the_shire"] == ["bree"]
    assert body["graph"]["aerial_adjacent"] == {}
    assert body["graph"]["ford_adjacent"] == {}


def test_put_graph_rejects_self_edge(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"graph": {"adjacent": {"the_shire": ["the_shire"]}}},
    )
    assert r.status_code == 400


def test_put_graph_mirrors_setups_on_the_same_map(make_client, players, db_session):
    admin, _ = players
    db_session.add(_graph_setup("wotr_1.1", "wotr_map_1.2", {"the_shire": ["bree"], "bree": ["the_shire"]}))
    db_session.add(_graph_setup("wotr_2.0", "wotr_map_1.2", {"the_shire": ["bree"], "bree": ["the_shire"]}))
    db_session.add(_graph_setup("motw_1.0", "motw_map_1.2", {"the_shire": ["bree"]}))
    db_session.commit()
    client = make_client(admin)
    r = client.put(
        "/admin/maps/wotr_map_1.2/overrides",
        json={"territories": {}, "graph": {"adjacent": {"the_shire": ["mordor"], "bree": []}}},
    )
    assert r.status_code == 200
    for setup_id in ("wotr_1.1", "wotr_2.0"):
        row = db_session.query(Setup).filter(Setup.id == setup_id).one()
        territories = json.loads(row.territories_json)
        assert territories["the_shire"]["adjacent"] == ["mordor"]
        assert territories["bree"]["adjacent"] == []
    other = json.loads(db_session.query(Setup).filter(Setup.id == "motw_1.0").one().territories_json)
    assert other["the_shire"]["adjacent"] == ["bree"]


def test_get_graph_fills_agreed_edges_and_skips_conflicts(make_client, players, db_session):
    admin, _ = players
    db_session.add(_graph_setup("wotr_1.1", "wotr_map_1.2", {"the_shire": ["bree"], "mordor": ["gorgoroth"]}))
    db_session.add(_graph_setup("wotr_2.0", "wotr_map_1.2", {"the_shire": ["bree"], "mordor": ["nurn"]}))
    db_session.commit()
    client = make_client(admin)
    body = client.get("/maps/wotr_map_1.2/overrides").json()["overrides"]
    assert body["graph"]["adjacent"]["the_shire"] == ["bree"]
    assert "mordor" not in body["graph"]["adjacent"]


def test_apply_map_graph_overrides_definition(db_session):
    from backend.map_overrides import apply_map_graph

    db_session.add(
        MapOverride(
            map_base="wotr_map_1.2",
            overrides_json=json.dumps(
                {"territories": {}, "graph": {"adjacent": {"the_shire": ["bree"]}, "aerial_adjacent": {}, "ford_adjacent": {}}}
            ),
        )
    )
    db_session.commit()
    defs = {
        "the_shire": TerritoryDefinition(
            id="the_shire",
            display_name="The Shire",
            terrain_type="plains",
            adjacent=["hobbiton"],
            produces={"power": 1},
        )
    }
    apply_map_graph(defs, "wotr_map_1.2", db_session)
    assert defs["the_shire"].adjacent == ["bree"]



@pytest.mark.parametrize("darkness", [0, 0.5, 1])
def test_mountain_darkness_survives_normalization(darkness):
    from backend.map_overrides import normalize_map_overrides

    raw = {"landscape": {"territories": {"mordor": [
        {"cat": "mountains", "img": "mountain.png", "x": 0, "y": 0, "darkness": darkness}
    ]}}}
    cleaned = normalize_map_overrides(raw)
    assert cleaned["landscape"]["territories"]["mordor"][0]["darkness"] == darkness
    assert normalize_map_overrides(cleaned) == cleaned


@pytest.mark.parametrize("darkness", [-0.1, 1.1, True, "invalid", float("nan"), float("inf")])
def test_invalid_mountain_darkness_is_rejected(darkness):
    from backend.map_overrides import _clean_placement

    assert _clean_placement({"cat": "mountains", "img": "mountain.png", "x": 0, "y": 0,
                             "darkness": darkness}) is None


def test_mountain_darkness_saved_and_publicly_readable(make_client, players):
    admin, _ = players
    client = make_client(admin)
    payload = {"landscape": {"territories": {"mordor": [
        {"cat": "mountains", "img": "mountain.png", "x": 5, "y": 6, "darkness": 0.95}
    ]}}}
    saved = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert saved.status_code == 200
    public = client.get("/maps/wotr_map_1.2/overrides")
    assert public.status_code == 200
    assert public.json()["overrides"]["landscape"]["territories"]["mordor"][0]["darkness"] == 0.95


def test_landmarks_survive_save_reload_and_terrain_edit(make_client, players):
    admin, _ = players
    client = make_client(admin)
    landmarks = [
        {"cat": "landmarks", "img": "minas_tirith.png", "x": 0, "y": 12, "layer": "interior", "s": 1.5},
        {"cat": "landmarks", "img": "barad_dur.png", "x": 150, "y": 200, "layer": "interior", "auto": True},
    ]
    payload = {"landscape": {"territories": {"mordor": landmarks}}}
    saved = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert saved.status_code == 200
    loaded = client.get("/maps/wotr_map_1.2/overrides").json()["overrides"]
    assert loaded["landscape"]["territories"]["mordor"] == landmarks
    loaded["terrain"] = {"mordor": "mountains"}
    loaded["landscape"]["territories"]["mordor"].append(
        {"cat": "mountains", "img": "mountain.png", "x": 15, "y": 25, "darkness": 0.9}
    )
    assert client.put("/admin/maps/wotr_map_1.2/overrides", json=loaded).status_code == 200
    reloaded = client.get("/maps/wotr_map_1.2/overrides").json()["overrides"]
    assert reloaded["landscape"]["territories"]["mordor"][:2] == landmarks


@pytest.mark.parametrize("image", [
    "gray_havens.png", "lothlorien.png", "osgiliath.png", "rivendell.png",
    "annuminas.png", "dwarrowdelf.png", "isengard.png", "minas_ithil.png",
])
def test_reported_missing_landmarks_round_trip(make_client, players, image):
    admin, _ = players
    client = make_client(admin)
    placement = {"cat": "landmarks", "img": image, "x": 0, "y": 12,
                 "layer": "interior", "s": 1.5, "auto": True}
    payload = {"landscape": {"territories": {"mordor": [placement]}}}
    saved = client.put("/admin/maps/wotr_map_1.2/overrides", json=payload)
    assert saved.status_code == 200
    reloaded = client.get("/maps/wotr_map_1.2/overrides").json()["overrides"]
    assert reloaded["landscape"]["territories"]["mordor"] == [placement]
