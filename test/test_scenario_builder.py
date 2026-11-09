"""Lossless authoring migration, compile semantics, CLI, and DB/admin integration."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_admin
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import AppSetting, Setup
from backend.engine.definitions import DATA_DIR, SETUPS_DIR
from backend.scenario_builder import (
    BUNDLE_KEYS, DELETE, compile_scenario, delta, extract_library, extract_source,
    load_library, merge, parse_source,
)
from backend.scenario_store import SETTING, compile_all, initialize, load_state, save_source, update_library
from backend.setup_data import get_admin_setup_bundle, save_setup_bundle
from backend.setup_validation import validate_setup_payload
from backend.tools.scenario import main, read_bundle


def example(setup_id="alpha", attack=5):
    return {
        "manifest": {"id": setup_id, "display_name": setup_id, "map_asset": "world", "is_active": False},
        "units": {"guard": {"id": "guard", "display_name": "Guard", "archetype": "infantry", "faction": "good",
                            "attack": attack, "defense": 3, "movement": 1, "health": 1, "specials": [], "cost": {"power": 3}}},
        "territories": {"shire": {"id": "shire", "display_name": "The Shire", "terrain_type": "plains", "ownable": True,
                                   "adjacent": [], "produces": {"power": 2}}},
        "factions": {"good": {"id": "good", "display_name": "Good", "alliance": "good", "capital": "shire"}},
        "camps": {}, "ports": {},
        "starting_setup": {"turn_order": ["good"], "territory_owners": {"shire": "good"},
                           "starting_units": {"shire": [{"unit_id": "guard", "count": 2}]}},
    }


def test_merge_and_delta_preserve_null_empty_lists_missing_and_nested_fields():
    base = {"cost": {"power": 3, "other": 2}, "specials": ["heal"], "home_territory_ids": ["shire"], "gone": 1}
    target = {"cost": {"power": 5}, "specials": [], "home_territory_ids": None, "new": False}
    patch = delta(base, target)
    assert patch["gone"] == DELETE
    assert merge(base, patch) == target
    assert base["specials"] == ["heal"]


def test_majority_is_per_field_deterministic_and_lossless():
    bundles = {"a": example("a", 6), "b": example("b"), "c": example("c")}
    bundles["a"]["units"]["guard"]["specials"] = None
    bundles["a"]["units"]["guard"].pop("cost")
    bundles["a"]["territories"]["shire"]["produces"]["power"] = 4
    library = extract_library(bundles)
    assert library == extract_library(dict(reversed(list(bundles.items()))))
    assert library["units"]["guard"]["attack"] == 5
    assert library["maps"]["world"]["shire"]["produces"]["power"] == 2
    for bundle in bundles.values():
        assert compile_scenario(extract_source(bundle, library), library) == bundle


def test_structural_overrides_aliases_and_custom_units():
    bundle = example()
    library = extract_library({"alpha": bundle})
    source = extract_source(bundle, library)
    source["units"] = {
        "ally": {"seed": "guard", "overrides": {"faction": "elves", "specials": [], "home_territory_ids": None}},
        "custom": {"overrides": {"id": "custom", "attack": 7}},
    }
    compiled = compile_scenario(source, library)
    assert compiled["units"]["ally"]["id"] == "ally"
    assert compiled["units"]["ally"]["faction"] == "elves"
    assert compiled["units"]["ally"]["home_territory_ids"] is None
    assert compiled["units"]["custom"] == {"id": "custom", "attack": 7}


@pytest.mark.parametrize("asset", [None, "", False, 123, DELETE])
def test_legacy_missing_and_empty_map_assets_roundtrip(asset):
    bundle = example()
    if asset == DELETE:
        del bundle["manifest"]["map_asset"]
    else:
        bundle["manifest"]["map_asset"] = asset
    library = extract_library({"alpha": bundle})
    assert compile_scenario(extract_source(bundle, library), library) == bundle


def test_one_level_inheritance_lists_replace_and_records_can_be_removed():
    library = extract_library({"alpha": example()})
    parent = extract_source(example(), library)
    child = {"extends": "alpha", "manifest": {"id": "beta"}, "units": {"guard": {"overrides": {"attack": 6}}}, "starting_setup": {"turn_order": []}}
    result = compile_scenario(child, library, {"alpha": parent}.get)
    assert result["manifest"]["id"] == "beta"
    assert result["units"]["guard"]["attack"] == 6
    assert result["starting_setup"]["turn_order"] == []
    assert compile_scenario({**child, "units": {"guard": DELETE}}, library, {"alpha": parent}.get)["units"] == {}
    with pytest.raises(ValueError, match="one level"):
        compile_scenario({"extends": "beta"}, library, {"beta": child}.get)
    with pytest.raises(ValueError):
        compile_scenario({"extends": "missing"}, library, {}.get)


@pytest.mark.parametrize("text", ["[]", "version: 1\nversion: 2", "x: &a [1]\ny: *a", "x: !!python/object:foo {}", "1: value", "x: 2020-01-01", "x: .nan", "x: " + "[" * 70 + "0" + "]" * 70])
def test_rejects_ambiguous_or_unsafe_yaml(text):
    with pytest.raises(ValueError):
        parse_source(text)


def test_bad_references_and_source_fields_report_errors():
    library = extract_library({"alpha": example()})
    source = extract_source(example(), library)
    for bad in ({**source, "map": "missing"}, {**source, "version": 2}, {**source, "typo": 1},
                {**source, "units": {"guard": {"seed": "missing"}}}, {**source, "territory_ids": ["missing"]},
                {**source, "territory_ids": ["shire", "shire"]}):
        with pytest.raises(ValueError):
            compile_scenario(bad, library)


@pytest.mark.parametrize("folder", sorted(path for path in SETUPS_DIR.iterdir() if (path / "manifest.json").is_file()), ids=lambda p: p.name)
def test_committed_sources_compile_identically_and_validation_does_not_regress(folder):
    library = load_library(DATA_DIR)
    source = parse_source((folder / "setup.yaml").read_text())
    bundle = compile_scenario(source, library, lambda key: parse_source((SETUPS_DIR / key / "setup.yaml").read_text()))
    assert bundle == read_bundle(folder), "Compiled JSON is stale; rebuild or extract intentionally"
    catalog = json.loads((DATA_DIR / "catalog.json").read_text())
    baseline = json.loads((Path(__file__).parent / "fixtures" / "scenario_validation_baseline.json").read_text())
    assert sorted(validate_setup_payload(bundle, catalog)) == baseline[folder.name]


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("SETUP_SOURCE", "db")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    for key, attack in (("alpha", 5), ("beta", 5), ("gamma", 6)):
        bundle = example(key, attack)
        session.add(Setup(id=key, specials_json="{}", **{f"{name}_json": json.dumps(bundle[name]) for name in BUNDLE_KEYS}))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_initialization_uses_live_db_and_never_replaces_setup_values(db):
    before = {row.id: row.units_json for row in db.query(Setup).all()}
    initialize(db)
    state = load_state(db)
    assert set(state["library"]["units"]) == {"guard"}
    assert state["library"]["units"]["guard"]["attack"] == 5
    assert {row.id: row.units_json for row in db.query(Setup).all()} == before
    assert compile_all(state) == {key: {k: v for k, v in get_admin_setup_bundle(db, key).items() if k != "id"} for key in before}
    initialize(db)
    assert load_state(db) == state


def test_default_updates_preview_then_rebuild_inheritors_and_preserve_overrides(db):
    initialize(db)
    library = deepcopy(load_state(db)["library"])
    library["units"]["guard"]["attack"] = 7
    library["factions"]["good"]["display_name"] = "Allies"
    library["territories"]["shire"]["produces"]["power"] = 4
    impact = update_library(db, library)
    assert set(impact["changed_setups"]) == {"alpha", "beta", "gamma"}
    assert json.loads(db.get(Setup, "alpha").units_json)["guard"]["attack"] == 5
    update_library(db, library, apply=True)
    for key, attack in (("alpha", 7), ("beta", 7), ("gamma", 6)):
        bundle = get_admin_setup_bundle(db, key)
        assert bundle["units"]["guard"]["attack"] == attack
        assert bundle["factions"]["good"]["display_name"] == "Allies"
        assert bundle["territories"]["shire"]["produces"]["power"] == 4


def test_invalid_library_is_atomic_and_unused_bad_seeds_are_rejected(db):
    initialize(db)
    before = db.get(AppSetting, SETTING).value_json
    library = deepcopy(load_state(db)["library"])
    library["units"]["guard"]["faction"] = "missing"
    with pytest.raises(ValueError, match="known faction"):
        update_library(db, library, apply=True)
    assert db.get(AppSetting, SETTING).value_json == before
    assert json.loads(db.get(Setup, "alpha").units_json)["guard"]["faction"] == "good"
    library = deepcopy(load_state(db)["library"])
    library["units"]["unused"] = {"id": "unused", "attack": "bad"}
    with pytest.raises(ValueError, match="Invalid defaults"):
        update_library(db, library, apply=True)


def test_legacy_form_edits_sync_source_and_explicit_equal_overrides_stay_pinned(db):
    initialize(db)
    state = load_state(db)
    source = state["sources"]["alpha"]
    source["units"]["guard"]["overrides"]["attack"] = 5
    bundle = compile_scenario(source, state["library"])
    bundle["units"]["guard"]["cost"]["power"] = 8
    save_setup_bundle(db, "alpha", bundle, source)
    stored = load_state(db)
    assert stored["sources"]["alpha"]["units"]["guard"]["overrides"] == {"attack": 5, "cost": {"power": 8}}
    library = deepcopy(stored["library"])
    library["units"]["guard"]["attack"] = 9
    update_library(db, library, apply=True)
    assert json.loads(db.get(Setup, "alpha").units_json)["guard"]["attack"] == 5


def test_parent_source_save_rebuilds_children(db):
    initialize(db)
    state = load_state(db)
    child = {"version": 1, "extends": "alpha", "manifest": {"id": "beta"}}
    save_source(db, "beta", child)
    state = load_state(db)
    parent = state["sources"]["alpha"]
    parent["units"]["guard"]["overrides"]["attack"] = 10
    save_source(db, "alpha", parent)
    assert json.loads(db.get(Setup, "beta").units_json)["guard"]["attack"] == 10


@pytest.fixture()
def client(db):
    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_admin] = lambda: object()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_dry_run_source_import_and_save(client, db):
    response = client.get("/admin/setups/alpha/source")
    assert response.status_code == 200
    source = response.json()["source"]
    original = db.get(Setup, "alpha").units_json
    source["units"]["guard"]["overrides"]["attack"] = 8
    check = client.post("/admin/setups/validate", json={"source": json.dumps(source)})
    assert check.json()["valid"] is True
    assert db.get(Setup, "alpha").units_json == original
    assert client.put("/admin/setups/alpha/source", json={"source": source}).status_code == 200
    assert json.loads(db.get(Setup, "alpha").units_json)["guard"]["attack"] == 8
    imported = client.post("/admin/setups", json={"id": "imported", "source": json.dumps(source)})
    assert imported.status_code == 200, imported.text
    stored = client.get("/admin/setups/imported/source").json()["source"]
    assert stored["manifest"]["id"] == "imported"
    assert stored["manifest"]["is_active"] is False
    assert client.post("/admin/setups/validate", json={"source": "units: ["}).json()["valid"] is False
    assert client.post("/admin/setups/validate", json=example()).json()["valid"] is True
    exported = client.get("/admin/scenarios/export").json()
    assert "imported" in exported["sources"]


def test_new_endpoints_require_admin(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        for method, path in (("GET", "/admin/scenario-library"), ("PUT", "/admin/scenario-library"),
                             ("POST", "/admin/scenario-library/preview"), ("POST", "/admin/setups/validate"),
                             ("GET", "/admin/setups/alpha/source"), ("PUT", "/admin/setups/alpha/source"),
                             ("GET", "/admin/scenarios/export")):
            assert client.request(method, path, json={} if method != "GET" else None).status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_cli_validation_errors_no_write_and_explain(tmp_path, capsys):
    library = extract_library({"alpha": example()})
    from backend.scenario_builder import write_library
    write_library(tmp_path, library)
    (tmp_path / "catalog.json").write_text((DATA_DIR / "catalog.json").read_text())
    source = tmp_path / "setup.yaml"
    source.write_text(json.dumps(extract_source(example(), library)))
    args = ["--data-root", str(tmp_path)]
    assert main([*args, "build", str(source), "--check"]) == 0
    assert main([*args, "explain", "guard", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["resolved"]["attack"] == 5
    assert main([*args, "diff", str(source), str(source)]) == 0
    assert capsys.readouterr().out == ""
    output = tmp_path / "bundle.json"
    assert main([*args, "build", str(source), "--output", str(output)]) == 0
    assert main([*args, "validate", str(output)]) == 0
    assert json.loads(output.read_text()) == example()
    output.unlink()
    bad = parse_source(source.read_text())
    bad["units"]["guard"]["overrides"]["faction"] = "missing"
    source.write_text(json.dumps(bad))
    assert main([*args, "build", str(source), "--output", str(output)]) == 1
    assert not output.exists()
    assert "known faction" in capsys.readouterr().err


def test_export_unpacks_to_a_lossless_offline_collection(client, tmp_path):
    snapshot = client.get("/admin/scenarios/export").json()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    root = tmp_path / "offline"
    assert main(["unpack", str(path), "--output-root", str(root)]) == 0
    for key, source in snapshot["sources"].items():
        assert read_bundle(root / "setups" / key) == compile_scenario(source, snapshot["library"], snapshot["sources"].get)
        assert main(["--data-root", str(root), "build", str(root / "setups" / key / "setup.yaml"), "--check"]) == 0


def test_empty_map_draft_and_deleted_setup_id_can_be_reused(client):
    assert client.post("/admin/setups", json={"id": "fresh"}).status_code == 200
    assert client.get("/admin/setups/fresh/source").status_code == 200
    assert client.delete("/admin/setups/fresh").status_code == 200
    assert client.post("/admin/setups", json={"id": "fresh", "bundle": example("fresh", 9)}).status_code == 200
    source = client.get("/admin/setups/fresh/source").json()["source"]
    assert compile_scenario(source, client.get("/admin/scenario-library").json())["units"]["guard"]["attack"] == 9


def test_library_http_preview_and_save(client):
    library = client.get("/admin/scenario-library").json()
    library["units"]["guard"]["attack"] = 8
    preview = client.post("/admin/scenario-library/preview", json=library)
    assert preview.status_code == 200, preview.text
    assert set(preview.json()["changed_setups"]) == {"alpha", "beta"}
    assert client.get("/admin/setups/alpha").json()["units"]["guard"]["attack"] == 5
    saved = client.put("/admin/scenario-library", json=library)
    assert saved.status_code == 200, saved.text
    assert client.get("/admin/setups/alpha").json()["units"]["guard"]["attack"] == 8


def test_form_save_updates_inheriting_children_and_prevents_parent_deletion(client):
    assert client.put("/admin/setups/beta/source", json={"source": {"extends": "alpha", "manifest": {"id": "beta"}}}).status_code == 200
    bundle = client.get("/admin/setups/alpha").json()
    bundle["units"]["guard"]["attack"] = 11
    assert client.put("/admin/setups/alpha", json=bundle).status_code == 200
    assert client.get("/admin/setups/beta").json()["units"]["guard"]["attack"] == 11
    assert client.delete("/admin/setups/alpha").status_code == 409
    assert client.get("/admin/setups/alpha").status_code == 200


def test_map_authority_is_reflected_in_sources_and_checked_in_dry_run(client, db):
    initialize(db)
    from backend.map_overrides import save_map_overrides
    save_map_overrides(db, "world", {"territories": {}, "terrain": {"shire": "forest"}})
    response = client.get("/admin/setups/alpha/source").json()
    assert response["library"]["maps"]["world"]["shire"]["terrain_type"] == "forest"
    source = response["source"]
    source.setdefault("territory_overrides", {})["shire"] = {"terrain_type": "plains"}
    assert client.post("/admin/setups/validate", json={"source": source}).json()["valid"] is False
    assert client.put("/admin/setups/alpha/source", json={"source": source}).status_code == 400


def test_territory_seeds_update_matching_maps_by_id_and_keep_scenario_overrides(db):
    initialize(db)
    source = load_state(db)["sources"]["beta"]
    source.setdefault("territory_overrides", {})["shire"] = {"produces": {"power": 9}}
    save_source(db, "beta", source)
    library = deepcopy(load_state(db)["library"])
    library["maps"]["another_map"] = deepcopy(library["maps"]["world"])
    library["territories"]["shire"]["produces"]["power"] = 7
    impact = update_library(db, library)
    assert set(impact["changed_setups"]) == {"alpha", "gamma"}
    assert json.loads(db.get(Setup, "alpha").territories_json)["shire"]["produces"]["power"] == 2
    update_library(db, library, apply=True)
    state = load_state(db)
    for records in state["library"]["maps"].values():
        assert records["shire"]["produces"]["power"] == 7
    assert get_admin_setup_bundle(db, "alpha")["territories"]["shire"]["produces"]["power"] == 7
    assert get_admin_setup_bundle(db, "beta")["territories"]["shire"]["produces"]["power"] == 9
    bad = deepcopy(state["library"])
    del bad["territories"]["shire"]
    with pytest.raises(ValueError, match="still used by maps"):
        update_library(db, bad, apply=True)


def test_legacy_map_metadata_becomes_lossless_scenario_overrides(db):
    beta = db.get(Setup, "beta")
    manifest = json.loads(beta.manifest_json)
    manifest["map_asset"] = "other"
    beta.manifest_json = json.dumps(manifest)
    territories = json.loads(beta.territories_json)
    territories["shire"]["produces"]["power"] = 5
    beta.territories_json = json.dumps(territories)
    db.commit()
    bundles = {key: {k: v for k, v in get_admin_setup_bundle(db, key).items() if k != "id"} for key in ("alpha", "beta", "gamma")}
    legacy = extract_library(bundles)
    del legacy["territories"]
    legacy["maps"]["other"] = deepcopy(bundles["beta"]["territories"])
    sources = {key: extract_source(bundle, legacy) for key, bundle in bundles.items()}
    sources["beta"] = {"extends": "alpha", "map": "other", "manifest": bundles["beta"]["manifest"]}
    db.add(AppSetting(key=SETTING, value_json=json.dumps({"library": legacy, "sources": sources})))
    db.commit()
    original_rows = {row.id: row.territories_json for row in db.query(Setup).all()}
    initialize(db)
    state = load_state(db)
    assert compile_all(state) == bundles
    assert {row.id: row.territories_json for row in db.query(Setup).all()} == original_rows
    assert state["sources"]["beta"]["extends"] == "alpha"
    assert state["sources"]["beta"]["territory_overrides"]["shire"]["produces"]["power"] == 5


def test_new_map_region_uses_existing_territory_seed_automatically(db):
    from backend.map_assets import save_map_asset
    initialize(db)
    library = deepcopy(load_state(db)["library"])
    library["territories"]["future"] = {"id": "future", "display_name": "Seed name", "produces": {"power": 4}, "ownable": True}
    update_library(db, library, apply=True)
    drawing = {"width": 100, "height": 100,
        "joints": {"a": {"x": 0, "y": 0}, "b": {"x": 100, "y": 0}, "c": {"x": 100, "y": 100}, "d": {"x": 0, "y": 100}},
        "borders": {"ab": {"a": "a", "b": "b"}, "bc": {"a": "b", "b": "c"}, "cd": {"a": "c", "b": "d"}, "da": {"a": "d", "b": "a"}},
        "labels": {"future": {"display_name": "Drawing name", "x": 50, "y": 50}}}
    save_map_asset(db, "future_map", {"doc": drawing})
    record = load_state(db)["library"]["maps"]["future_map"]["future"]
    assert record["display_name"] == "Seed name"
    assert record["produces"] == {"power": 4}
    assert record["terrain_type"] == "plains"


def test_rebuild_compares_resolved_sources_to_saved_scenario_definitions(db):
    initialize(db)
    state = load_state(db)
    state["sources"]["alpha"]["units"]["guard"]["overrides"]["attack"] = 8
    # A drawing/source can be newer than the saved gameplay definition.
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    assert update_library(db, deepcopy(state["library"]))["changed_setups"] == ["alpha"]
    assert get_admin_setup_bundle(db, "alpha")["units"]["guard"]["attack"] == 5
    update_library(db, deepcopy(state["library"]), apply=True)
    assert get_admin_setup_bundle(db, "alpha")["units"]["guard"]["attack"] == 8


def test_cli_unpack_preserves_legacy_map_metadata(tmp_path):
    bundles = {"alpha": example("alpha"), "beta": example("beta")}
    bundles["beta"]["manifest"]["map_asset"] = "other"
    bundles["beta"]["territories"]["shire"]["produces"]["power"] = 5
    library = extract_library(bundles)
    del library["territories"]
    library["maps"]["other"] = deepcopy(bundles["beta"]["territories"])
    state = {"library": library, "sources": {key: extract_source(bundle, library) for key, bundle in bundles.items()},
             "catalog": json.loads((DATA_DIR / "catalog.json").read_text())}
    path = tmp_path / "legacy-export.json"
    path.write_text(json.dumps(state))
    root = tmp_path / "offline"
    assert main(["unpack", str(path), "--output-root", str(root)]) == 0
    current_library = load_library(root)
    assert "shire" in current_library["territories"]
    for key, bundle in bundles.items():
        source = parse_source((root / "setups" / key / "setup.yaml").read_text())
        assert compile_scenario(source, current_library) == bundle
        assert read_bundle(root / "setups" / key) == bundle
