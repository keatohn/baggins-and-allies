"""Admin map builder: planar-graph → SVG conversion and POST /admin/maps/builder/convert."""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin, get_current_player
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import Player
from backend.map_builder import MapBuilderError, convert_map_builder


def frame_doc(width=1000, height=800, labels=None):
    """A full outer frame: 4 joints, 4 borders."""
    return {
        "width": width,
        "height": height,
        "joints": {
            "tl": {"x": 0, "y": 0},
            "tr": {"x": width, "y": 0},
            "br": {"x": width, "y": height},
            "bl": {"x": 0, "y": height},
        },
        "borders": {
            "top": {"a": "tl", "b": "tr"},
            "right": {"a": "tr", "b": "br"},
            "bottom": {"a": "br", "b": "bl"},
            "left": {"a": "bl", "b": "tl"},
        },
        "labels": labels or {},
    }


def split_doc(labels=None):
    """Frame split down the middle by a vertical border (2 regions)."""
    doc = frame_doc(labels=labels)
    doc["joints"]["tm"] = {"x": 500, "y": 0}
    doc["joints"]["bm"] = {"x": 500, "y": 800}
    doc["borders"]["mid"] = {"a": "tm", "b": "bm"}
    # top and bottom must route through the split joints so both regions close
    doc["borders"]["top"] = {"a": "tl", "b": "tm"}
    doc["borders"]["top2"] = {"a": "tm", "b": "tr"}
    doc["borders"]["bottom"] = {"a": "br", "b": "bm"}
    doc["borders"]["bottom2"] = {"a": "bm", "b": "bl"}
    return doc


# --- unit: convert_map_builder ------------------------------------------------


def test_frame_produces_single_territory():
    doc = frame_doc(labels={"the_shire": {"display_name": "The Shire", "x": 500, "y": 400}})
    out = convert_map_builder(doc)
    assert out["ok"] is True
    assert len(out["territories"]) == 1
    t = out["territories"][0]
    assert t["id"] == "the_shire"
    assert t["label"] == "The Shire"
    assert t["d"].startswith("M ")
    assert t["d"].rstrip().endswith("Z")
    assert out["stats"] == {"joints": 4, "borders": 4, "pruned_borders": 0, "regions": 1, "labelled": 1}
    assert out["warnings"] == []


def test_svg_matches_map_structure():
    doc = frame_doc(labels={"mordor": {"display_name": "Mordor", "x": 100, "y": 100}})
    svg = convert_map_builder(doc)["svg"]
    assert 'inkscape:label="svg_borders"' in svg
    assert '<g inkscape:groupmode="layer"' in svg
    assert 'id="mordor"' in svg
    assert 'inkscape:label="Mordor"' in svg
    assert 'fill:none;stroke:none' in svg
    assert 'viewBox="0 0 1000 800"' in svg
    assert 'width="1000"' in svg
    assert svg.count("<path ") == 1
    # parseable XML
    import xml.etree.ElementTree as ET

    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")


def test_split_regions_get_one_path_each():
    doc = split_doc(
        labels={
            "west": {"display_name": "West", "x": 250, "y": 400},
            "east": {"display_name": "East", "x": 750, "y": 400},
        }
    )
    out = convert_map_builder(doc)
    assert [t["id"] for t in out["territories"]] == ["east", "west"]
    assert out["stats"]["regions"] == 2
    assert out["stats"]["labelled"] == 2
    # each path is closed and distinct
    ds = {t["d"] for t in out["territories"]}
    assert len(ds) == 2
    for d in ds:
        assert d.endswith("Z")
        assert d.count("M") == 1


def test_split_keeps_label_on_each_side():
    doc = split_doc(
        labels={
            "left_side": {"display_name": "Left", "x": 100, "y": 400},
            "right_side": {"display_name": "Right", "x": 900, "y": 400},
        }
    )
    out = convert_map_builder(doc)
    by_id = {t["id"]: t for t in out["territories"]}
    assert set(by_id) == {"left_side", "right_side"}
    # left region stays west of the split; right region reaches x=1000
    assert "1000" not in by_id["left_side"]["d"]
    assert "500" in by_id["left_side"]["d"]
    assert "1000" in by_id["right_side"]["d"]


def test_dangling_stroke_is_pruned():
    doc = frame_doc(labels={"only": {"display_name": "Only", "x": 500, "y": 400}})
    # spike: joint hanging off one border endpoint (degree 1 after attach)
    doc["joints"]["spike"] = {"x": 500, "y": 400}
    doc["borders"]["spike_edge"] = {"a": "spike", "b": "tl"}
    out = convert_map_builder(doc)
    assert out["stats"]["pruned_borders"] == 1
    assert out["stats"]["regions"] == 1
    assert len(out["territories"]) == 1


def test_orphan_label_reports_warning():
    doc = frame_doc()
    # no borders at all → no closed regions
    doc["borders"] = {}
    doc["labels"] = {"ghost": {"display_name": "Ghost", "x": 10, "y": 10}}
    out = convert_map_builder(doc)
    assert out["territories"] == []
    assert out["stats"]["regions"] == 0
    assert any("ghost" in w and "closed region" in w for w in out["warnings"])


def test_unlabelled_region_warning():
    out = convert_map_builder(split_doc(labels={"one": {"display_name": "One", "x": 250, "y": 400}}))
    assert len(out["territories"]) == 1
    assert any("no label" in w for w in out["warnings"])


def test_two_labels_same_region_warns():
    doc = frame_doc(
        labels={
            "a_one": {"display_name": "A", "x": 500, "y": 400},
            "b_two": {"display_name": "B", "x": 501, "y": 401},
        }
    )
    out = convert_map_builder(doc)
    assert len(out["territories"]) == 1
    assert out["territories"][0]["id"] == "a_one"  # sorted: first wins
    assert any("same region" in w for w in out["warnings"])


def test_nested_island_label_goes_to_inner_ring():
    """A lake circle drawn inside a region: label in the lake goes to the lake."""
    doc = frame_doc(
        labels={
            "sea": {"display_name": "Sea", "x": 500, "y": 400},
            "island": {"display_name": "Island", "x": 400, "y": 400},
        }
    )
    doc["joints"].update(
        {
            "i0": {"x": 350, "y": 400},
            "i1": {"x": 400, "y": 350},
            "i2": {"x": 450, "y": 400},
            "i3": {"x": 400, "y": 450},
        }
    )
    doc["borders"].update(
        {
            "c0": {"a": "i0", "b": "i1"},
            "c1": {"a": "i1", "b": "i2"},
            "c2": {"a": "i2", "b": "i3"},
            "c3": {"a": "i3", "b": "i0"},
        }
    )
    out = convert_map_builder(doc)
    by_id = {t["id"]: t for t in out["territories"]}
    assert set(by_id) == {"sea", "island"}
    # island path stays within the circle bounds; sea reaches the frame corner
    assert "0,0" in by_id["sea"]["d"].replace(" ", "")
    assert "1000" in by_id["sea"]["d"]
    assert "350" in by_id["island"]["d"]
    assert "450" in by_id["island"]["d"]
    assert "800" not in by_id["island"]["d"]
    assert out["stats"]["regions"] == 2


def test_interior_faces_are_counterclockwise():
    doc = split_doc(
        labels={
            "west": {"display_name": "West", "x": 250, "y": 400},
            "east": {"display_name": "East", "x": 750, "y": 400},
        }
    )
    out = convert_map_builder(doc)
    import re

    for t in out["territories"]:
        pts = [tuple(map(float, p)) for p in re.findall(r"[ML] (-?[\d.]+),(-?[\d.]+)", t["d"])]
        area = sum(
            pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
            for i in range(len(pts))
        )
        assert area > 0, f"{t['id']} not CCW"


def test_conversion_is_deterministic():
    doc = split_doc(
        labels={
            "west": {"display_name": "West", "x": 250, "y": 400},
            "east": {"display_name": "East", "x": 750, "y": 400},
        }
    )
    a = convert_map_builder(doc)
    b = convert_map_builder(doc)
    assert a["svg"] == b["svg"]
    assert a["territories"] == b["territories"]


def test_label_xml_escaped():
    doc = frame_doc(labels={"x": {"display_name": 'A & B <"C">', "x": 10, "y": 10}})
    svg = convert_map_builder(doc)["svg"]
    assert "A &amp; B &lt;&quot;C&quot;&gt;" in svg
    assert "<\"C\">" not in svg


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(width=0),
        lambda d: d.update(width=-5),
        lambda d: d.update(width=10**9),
        lambda d: d.update(width="wide"),
        lambda d: d.update(height=float("nan")),
        lambda d: d.update(joints="nope"),
        lambda d: d["joints"].update(zz={"x": 50, "y": 9999}),
        lambda d: d["joints"].update(zz={"x": -1, "y": 10}),
        lambda d: d["joints"].update(zz={"x": float("inf"), "y": 10}),
        lambda d: d["joints"].update(zz={"x": "1", "y": 10}),
        lambda d: d["borders"].update(bad={"a": "tl", "b": "ghost"}),
        lambda d: d["borders"].update(bad={"a": "tl", "b": "tl"}),
        lambda d: d.update(borders="nope"),
        lambda d: d.update(labels={"Bad Tid": {"display_name": "x", "x": 1, "y": 1}}),
        lambda d: d["labels"].update(x={"display_name": "", "x": 1, "y": 1}),
        lambda d: d["labels"].update(x={"display_name": "x", "x": 9999, "y": 1}),
        lambda d: d.update(labels="nope"),
    ],
)
def test_invalid_payloads_raise(mutate):
    doc = frame_doc(labels={"ok": {"display_name": "Ok", "x": 10, "y": 10}})
    mutate(doc)
    with pytest.raises(MapBuilderError):
        convert_map_builder(doc)


def test_empty_payload_raises():
    with pytest.raises(MapBuilderError):
        convert_map_builder({})
    with pytest.raises(MapBuilderError):
        convert_map_builder("nope")


def test_labels_accept_list_form():
    doc = frame_doc()
    doc["labels"] = [
        {"territory_id": "the_shire", "display_name": "The Shire", "x": 500, "y": 400}
    ]
    out = convert_map_builder(doc)
    assert out["territories"][0]["id"] == "the_shire"


def test_border_limits_enforced():
    doc = frame_doc()
    doc["borders"] = {f"b{i}": {"a": "tl", "b": "tr"} for i in range(24001)}
    with pytest.raises(MapBuilderError):
        convert_map_builder(doc)


# --- API route ----------------------------------------------------------------


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


def test_convert_requires_admin(make_client, players):
    admin, user = players
    client = make_client(user)
    r = client.post("/admin/maps/builder/convert", json=frame_doc())
    assert r.status_code == 403


def test_convert_route_round_trip(make_client, players):
    admin, _ = players
    client = make_client(admin)
    doc = split_doc(
        labels={
            "west": {"display_name": "West", "x": 250, "y": 400},
            "east": {"display_name": "East", "x": 750, "y": 400},
        }
    )
    r = client.post("/admin/maps/builder/convert", json=doc)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["territories"]) == 2
    assert body["stats"]["regions"] == 2
    assert "svg_borders" in body["svg"]


def test_convert_route_rejects_invalid(make_client, players):
    admin, _ = players
    client = make_client(admin)
    r = client.post("/admin/maps/builder/convert", json={"width": 10})
    assert r.status_code == 400
    assert "detail" in r.json()


# --- closed-path SVG import ---------------------------------------------------

OFFSET_SVG = """<svg xmlns="http://www.w3.org/2000/svg"
    xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 400 200">
  <path id="west" inkscape:label="West" d="M 0,0 L 200,0 L 200,200 L 0,200 Z"/>
  <path id="east" inkscape:label="East" d="M 201.5,0.4 L 400,0 L 400,200 L 201.5,199.6 Z"/>
</svg>"""


def test_import_welds_offset_shared_border():
    from backend.map_svg_import import import_territory_svg

    out = import_territory_svg(OFFSET_SVG, 3)
    assert out["stats"]["territories"] == 2
    assert out["stats"]["shared_borders"] == 1
    assert out["stats"]["borders"] == 7
    assert out["stats"]["joints"] == 6
    conv = convert_map_builder(out["doc"])
    assert conv["stats"]["regions"] == 2
    assert conv["stats"]["labelled"] == 2
    assert conv["warnings"] == []


def test_import_relative_paths_with_midpoint_and_transform():
    from backend.map_svg_import import import_territory_svg

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200">
      <g transform="translate(0,0)">
        <path id="west" d="m 0,0 200,0 0,200 -200,0 z"/>
        <path id="east" transform="translate(1.5,0)" d="m 200,0 198.5,0 0,200 -198.5,0 0,-100 .2,-100 z"/>
      </g>
    </svg>"""
    out = import_territory_svg(svg, 3)
    conv = convert_map_builder(out["doc"])
    assert conv["stats"]["labelled"] == 2
    assert conv["stats"]["regions"] == 2
    assert out["stats"]["shared_borders"] >= 1


def test_import_round_trip_from_builder_svg():
    from backend.map_svg_import import import_territory_svg

    doc = split_doc(
        labels={
            "west": {"display_name": "West", "x": 250, "y": 400},
            "east": {"display_name": "East", "x": 750, "y": 400},
        }
    )
    svg = convert_map_builder(doc)["svg"]
    back = import_territory_svg(svg, 0.5)
    again = convert_map_builder(back["doc"])
    assert again["stats"]["regions"] == 2
    assert again["stats"]["labelled"] == 2
    assert {t["id"] for t in again["territories"]} == {"west", "east"}


def test_import_flattens_a_closed_curve():
    from backend.map_svg_import import import_territory_svg

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
      <path id="lake" inkscape:label="Lake"
        d="M 40,40 C 40,10 360,10 360,40 L 360,260 L 40,260 Z"
        xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"/>
    </svg>"""
    # inkscape namespace on the path is ignored; id is enough
    out = import_territory_svg(svg, 1)
    conv = convert_map_builder(out["doc"])
    assert conv["stats"]["labelled"] == 1
    assert conv["territories"][0]["id"] == "lake"
    assert conv["territories"][0]["label"] == "lake" or conv["territories"][0]["id"] == "lake"


def test_import_rejects_svg_without_territories():
    from backend.map_svg_import import import_territory_svg

    with pytest.raises(MapBuilderError):
        import_territory_svg("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'></svg>")
    with pytest.raises(MapBuilderError):
        import_territory_svg("not xml")


def test_import_route_and_saved_map_library(make_client, players, monkeypatch):
    from backend.map_overrides import list_map_bases

    monkeypatch.setattr("backend.map_assets.is_files_setup_source", lambda: False)
    monkeypatch.setattr("backend.map_overrides.is_files_setup_source", lambda: False)
    admin, user = players
    client = make_client(user)
    assert client.post("/admin/maps/builder/import", json={"svg": OFFSET_SVG, "tolerance": 3}).status_code == 403

    client = make_client(admin)
    imported = client.post("/admin/maps/builder/import", json={"svg": OFFSET_SVG, "tolerance": 3})
    assert imported.status_code == 200
    doc = imported.json()["doc"]
    assert imported.json()["stats"]["shared_borders"] == 1

    saved = client.put(
        "/admin/maps/builder/custom_map_2.0",
        json={"display_name": "Custom Map", "doc": doc},
    )
    assert saved.status_code == 200
    assert saved.json()["map_base"] == "custom_map_2.0"

    listed = client.get("/admin/maps/builder")
    assert listed.status_code == 200
    assert any(row["map_base"] == "custom_map_2.0" and row["display_name"] == "Custom Map" for row in listed.json()["maps"])

    fetched = client.get("/admin/maps/builder/custom_map_2.0")
    assert fetched.status_code == 200
    assert fetched.json()["doc"]["labels"]["west"]["display_name"] == "West"

    svg = client.get("/maps/custom_map_2.0/svg")
    assert svg.status_code == 200
    assert "image/svg" in svg.headers["content-type"]
    assert 'id="west"' in svg.text
    assert client.get("/maps/does_not_exist_9.9/svg").status_code == 404

    # Saved maps join the built-in ids for setups and the icon/landscape editor.
    from backend.api.database import get_db

    session = next(client.app.dependency_overrides[get_db]())
    assert "custom_map_2.0" in list_map_bases(session)

    copied = client.post(
        "/admin/maps/builder/custom_map_2.0/copy",
        json={"map_base": "custom_map_2.1", "display_name": "Custom Copy"},
    )
    assert copied.status_code == 200
    assert client.get("/maps/custom_map_2.1/svg").status_code == 200
    again = client.post(
        "/admin/maps/builder/custom_map_2.0/copy",
        json={"map_base": "custom_map_2.1", "display_name": "Nope"},
    )
    assert again.status_code == 400

    deleted = client.delete("/admin/maps/builder/custom_map_2.0")
    assert deleted.status_code == 200
    assert client.get("/admin/maps/builder/custom_map_2.0").status_code == 404
    assert client.get("/maps/custom_map_2.0/svg").status_code == 404
    # The copy is unaffected.
    assert client.get("/maps/custom_map_2.1/svg").status_code == 200


def test_save_map_asset_files_source(tmp_path, monkeypatch):
    import backend.map_assets as map_assets

    monkeypatch.setattr(map_assets, "MAPS_DIR", tmp_path)
    monkeypatch.setattr(map_assets, "is_files_setup_source", lambda: True)
    doc = frame_doc(labels={"the_shire": {"display_name": "The Shire", "x": 500, "y": 400}})
    saved = map_assets.save_map_asset(None, "files_map_1.0", {"display_name": "Files Map", "doc": doc})
    assert saved["display_name"] == "Files Map"
    assert (tmp_path / "files_map_1.0" / "builder.json").is_file()
    svg = (tmp_path / "files_map_1.0" / "map.svg").read_text(encoding="utf-8")
    assert 'id="the_shire"' in svg
    assert map_assets.load_map_svg(None, "files_map_1.0") == svg
    copied = map_assets.copy_map_asset(None, "files_map_1.0", "files_map_1.1", "Files Copy")
    assert copied["map_base"] == "files_map_1.1"
    assert (tmp_path / "files_map_1.1" / "map.svg").is_file()
    # Overrides in the same folder are not removed with the drawing.
    (tmp_path / "files_map_1.0" / "overrides.json").write_text("{}", encoding="utf-8")
    map_assets.delete_map_asset(None, "files_map_1.0")
    assert not (tmp_path / "files_map_1.0" / "builder.json").exists()
    assert (tmp_path / "files_map_1.0" / "overrides.json").is_file()
    map_assets.delete_map_asset(None, "files_map_1.1")
    assert not (tmp_path / "files_map_1.1").exists()
