"""Tests for temporary prod hotfix: haradrim archers sea_zone_10 → dol_amroth."""

import copy
import json

import pytest

from backend.api.prod_hotfix_haradrim import (
    PROD_HOTFIX_GAME_ID,
    apply_haradrim_sea10_to_dol_amroth_hotfix,
    hotfix_game_row_game_state_json,
)


def _minimal_state(*, extra_sea_units=None, archers_lo=None):
    a_a = {
        "instance_id": "harad_haradrim_archer_099",
        "unit_id": "haradrim_archer",
        "remaining_movement": 0,
        "remaining_health": 1,
        "base_movement": 1,
        "base_health": 1,
    }
    a_b = {
        "instance_id": "harad_haradrim_archer_100",
        "unit_id": "haradrim_archer",
        "remaining_movement": 0,
        "remaining_health": 1,
        "base_movement": 1,
        "base_health": 1,
    }
    if archers_lo:
        a_a["loaded_onto"] = archers_lo[0]
        a_b["loaded_onto"] = archers_lo[1]
    sea_units = [{"instance_id": "x_ship", "unit_id": "black_ship", "remaining_movement": 0, "remaining_health": 1, "base_movement": 2, "base_health": 1}]
    if extra_sea_units:
        sea_units.extend(extra_sea_units)
    sea_units.extend([a_a, a_b])
    return {
        "turn_number": 3,
        "current_faction": "harad",
        "phase": "combat_move",
        "territories": {
            "sea_zone_10": {"owner": None, "units": sea_units},
            "dol_amroth": {"owner": "gondor", "units": []},
            "other": {"owner": "mordor", "units": []},
        },
        "pending_moves": [],
        "active_combat": None,
    }


def test_hotfix_moves_two_archers_preserves_other_territories_and_unit_payloads():
    state = _minimal_state()
    other_before = copy.deepcopy(state["territories"]["other"])
    ship_before = copy.deepcopy(state["territories"]["sea_zone_10"]["units"][0])

    moved = apply_haradrim_sea10_to_dol_amroth_hotfix(state)

    assert moved == ["harad_haradrim_archer_099", "harad_haradrim_archer_100"]
    sea_u = state["territories"]["sea_zone_10"]["units"]
    dol_u = state["territories"]["dol_amroth"]["units"]
    assert len(sea_u) == 1
    assert sea_u[0] == ship_before
    assert len(dol_u) == 2
    assert dol_u[0]["instance_id"] == "harad_haradrim_archer_099"
    assert dol_u[1]["instance_id"] == "harad_haradrim_archer_100"
    assert dol_u[0]["remaining_movement"] == 0
    assert state["territories"]["other"] == other_before


def test_hotfix_wrong_count_raises_and_does_not_mutate():
    state = _minimal_state(extra_sea_units=[
        {
            "instance_id": "harad_haradrim_archer_101",
            "unit_id": "haradrim_archer",
            "remaining_movement": 0,
            "remaining_health": 1,
            "base_movement": 1,
            "base_health": 1,
        }
    ])
    snap = json.dumps(state)
    with pytest.raises(ValueError, match="exactly 2"):
        apply_haradrim_sea10_to_dol_amroth_hotfix(state)
    assert json.dumps(state) == snap


def test_hotfix_strips_loaded_onto_when_carrier_in_sea_zone():
    state = _minimal_state(archers_lo=("x_ship", "x_ship"))
    apply_haradrim_sea10_to_dol_amroth_hotfix(state)
    dol_u = state["territories"]["dol_amroth"]["units"]
    assert len(dol_u) == 2
    assert "loaded_onto" not in dol_u[0]
    assert "loaded_onto" not in dol_u[1]


def test_hotfix_rejects_loaded_onto_when_carrier_not_in_sea():
    state = _minimal_state(archers_lo=("missing_ship", "missing_ship"))
    with pytest.raises(ValueError, match="not in sea_zone_10"):
        apply_haradrim_sea10_to_dol_amroth_hotfix(state)


def test_hotfix_game_row_round_trip():
    state = _minimal_state()
    text_in = json.dumps(state)
    text_out, moved = hotfix_game_row_game_state_json(text_in)
    assert moved == ["harad_haradrim_archer_099", "harad_haradrim_archer_100"]
    parsed = json.loads(text_out)
    assert len(parsed["territories"]["dol_amroth"]["units"]) == 2


def test_hotfix_game_id_constant_matches_request():
    assert PROD_HOTFIX_GAME_ID == "4f0c91c4-42fc-4b66-995e-16fd0e1b42cb"
