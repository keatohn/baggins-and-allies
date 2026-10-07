"""Global catalog: specials, rule types, game options, terrain types, and archetypes shared by every setup."""

import json
from pathlib import Path

import pytest

from backend import catalog as catalog_module
from backend.catalog import (
    CatalogError,
    catalog_errors,
    load_catalog,
    normalize_catalog,
    rule_descriptions,
    save_catalog,
    specials_for_units,
)
from backend.engine.definitions import SETUPS_DIR
from backend.setup_data import import_setup_folder_to_dicts
from backend.setup_validation import validate_setup_payload


def _setup_folders() -> list[Path]:
    return [d for d in sorted(Path(SETUPS_DIR).iterdir()) if (d / "manifest.json").is_file()]


@pytest.mark.parametrize("folder", _setup_folders(), ids=lambda d: d.name)
def test_every_setup_uses_only_catalog_entries(folder):
    bundle = import_setup_folder_to_dicts(folder)
    errors = validate_setup_payload(bundle, load_catalog(None))
    assert not [e for e in errors if "catalog" in e]


def test_unknown_terrain_archetype_and_special_are_save_errors():
    bundle = import_setup_folder_to_dicts(Path(SETUPS_DIR) / "woteas_1.0")
    tid = next(iter(bundle["territories"]))
    uid = next(iter(bundle["units"]))
    bundle["territories"][tid]["terrain_type"] = "swamp_of_doom"
    bundle["units"][uid]["archetype"] = "dragon"
    bundle["units"][uid]["specials"] = ["flight_of_fancy"]
    errors = validate_setup_payload(bundle, load_catalog(None))
    assert f'territory "{tid}" terrain_type "swamp_of_doom" is not in the catalog terrain types' in errors
    assert f'unit "{uid}" archetype "dragon" is not in the catalog archetypes' in errors
    assert f'unit "{uid}" references special "flight_of_fancy", which is not in the catalog' in errors


def test_old_specials_shape_and_missing_rule_types_are_filled_in():
    catalog = normalize_catalog({
        "specials": {"order": ["terror", "archer"], "archer": {"name": "Archer"}, "terror": {"name": "Terror", "display_code": "T"}},
        "terrain_types": ["Plains", "plains"],
    })
    assert [e["id"] for e in catalog["specials"]] == ["terror", "archer"]
    assert catalog["specials"][0]["display_code"] == "T"
    assert [e["id"] for e in catalog["special_rules"]] == ["rings_of_power", "evolving_territory"]
    assert [e["id"] for e in catalog["game_options"]] == ["shadow_of_war"]
    assert catalog["terrain_types"] == ["plains", "sea"]


def test_strict_save_check_names_each_problem():
    good = load_catalog(None)
    assert catalog_errors(good) == []
    bad = json.loads(json.dumps(good))
    bad["specials"].append(dict(bad["specials"][0]))
    bad["special_rules"] = [r for r in bad["special_rules"] if r["id"] != "rings_of_power"]
    bad["terrain_types"] = [t for t in bad["terrain_types"] if t != "sea"] + ["Bad Type"]
    bad["game_options"][0]["name"] = ""
    errors = catalog_errors(bad)
    assert any("listed twice" in e for e in errors)
    assert 'special_rules must include "rings_of_power".' in errors
    assert 'terrain_types must include "sea".' in errors
    assert any(e.startswith("terrain_types[") for e in errors)
    assert "game_options[0].name must not be empty." in errors


def test_removing_an_entry_a_setup_uses_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", tmp_path / "catalog.json")
    monkeypatch.setattr(catalog_module, "_is_files_source", lambda: True)
    monkeypatch.setattr(
        catalog_module,
        "_setup_documents",
        lambda db: [("tiny", {"u": {"archetype": "infantry", "specials": ["terror"]}}, {"t": {"terrain_type": "forest"}}, {})],
    )
    base = {
        "specials": [{"id": "terror", "name": "Terror"}],
        "special_rules": [{"id": "rings_of_power", "name": "Rings"}, {"id": "evolving_territory", "name": "Evolving"}],
        "game_options": [{"id": "shadow_of_war", "name": "Shadow of War", "description": "Fog."}],
        "terrain_types": ["forest", "sea"],
        "archetypes": ["infantry"],
    }
    saved = save_catalog(None, base)
    assert json.loads((tmp_path / "catalog.json").read_text()) == saved

    without_forest = {**base, "terrain_types": ["sea"]}
    with pytest.raises(CatalogError, match='terrain type "forest" is still used by setup tiny'):
        save_catalog(None, without_forest)
    without_terror = {**base, "specials": []}
    with pytest.raises(CatalogError, match='special "terror" is still used by setup tiny'):
        save_catalog(None, without_terror)


def test_a_game_sees_only_the_specials_its_units_have_in_catalog_order():
    catalog = load_catalog(None)
    units = [{"specials": ["terror", "archer", "not_in_catalog"]}, {"specials": ["archer"]}]
    definitions, order = specials_for_units(catalog, units)
    catalog_order = [e["id"] for e in catalog["specials"]]
    assert order == [sid for sid in catalog_order if sid in ("terror", "archer")]
    assert set(definitions) == {"terror", "archer"}
    assert definitions["terror"]["name"] == "Terror"


def test_rule_descriptions_cover_rules_and_game_options():
    descriptions = rule_descriptions(load_catalog(None))
    assert {"rings_of_power", "evolving_territory", "shadow_of_war"} <= set(descriptions)
