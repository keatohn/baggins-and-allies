"""Tests for temporary prod hotfix: add Gondor units to anfalas."""

import json

import pytest

from backend.api.prod_hotfix_anfalas_units import (
    PROD_HOTFIX_GAME_ID,
    apply_add_gondor_units_anfalas,
    hotfix_add_gondor_anfalas_json,
)
from backend.engine.definitions import UnitDefinition


def _defs() -> dict[str, UnitDefinition]:
    return {
        "gondor_knight": UnitDefinition(
            id="gondor_knight",
            display_name="Gondor Knight",
            faction="gondor",
            archetype="infantry",
            tags=["land"],
            attack=5,
            defense=4,
            movement=2,
            health=1,
            cost={"power": 6},
        ),
        "citadel_guard": UnitDefinition(
            id="citadel_guard",
            display_name="Citadel Guard",
            faction="gondor",
            archetype="infantry",
            tags=["land"],
            attack=2,
            defense=4,
            movement=1,
            health=1,
            cost={"power": 4},
        ),
    }


def _minimal_state():
    return {
        "territories": {
            "anfalas": {"owner": "gondor", "units": []},
            "other": {"owner": None, "units": []},
        }
    }


def test_hotfix_appends_three_units():
    raw = _minimal_state()
    mutated, ids = apply_add_gondor_units_anfalas(raw, _defs())
    assert mutated is True
    assert len(ids) == 3
    units = raw["territories"]["anfalas"]["units"]
    assert len(units) == 3
    uids = [u["unit_id"] for u in units]
    assert uids.count("gondor_knight") == 2
    assert uids.count("citadel_guard") == 1
    knights = [u for u in units if u["unit_id"] == "gondor_knight"]
    assert knights[0]["remaining_movement"] == 2
    assert knights[0]["base_health"] == 1
    guard = next(u for u in units if u["unit_id"] == "citadel_guard")
    assert guard["remaining_movement"] == 1


def test_hotfix_idempotent_second_call():
    raw = _minimal_state()
    apply_add_gondor_units_anfalas(raw, _defs())
    snap = json.dumps(raw)
    mutated, _ = apply_add_gondor_units_anfalas(raw, _defs())
    assert mutated is False
    assert json.dumps(raw) == snap


def test_hotfix_rejects_partial_instance_ids():
    raw = _minimal_state()
    raw["territories"]["anfalas"]["units"].append(
        {
            "instance_id": "gondor_gondor_knight_anfalas_hotfix_1",
            "unit_id": "gondor_knight",
            "remaining_movement": 2,
            "remaining_health": 1,
            "base_movement": 2,
            "base_health": 1,
        }
    )
    with pytest.raises(ValueError, match="partial hotfix"):
        apply_add_gondor_units_anfalas(raw, _defs())


def test_hotfix_json_round_trip():
    raw = _minimal_state()
    text_out, mutated, ids = hotfix_add_gondor_anfalas_json(json.dumps(raw), _defs())
    assert mutated is True
    assert len(ids) == 3
    parsed = json.loads(text_out)
    assert len(parsed["territories"]["anfalas"]["units"]) == 3


def test_hotfix_game_id_constant():
    assert PROD_HOTFIX_GAME_ID == "4f0c91c4-42fc-4b66-995e-16fd0e1b42cb"
