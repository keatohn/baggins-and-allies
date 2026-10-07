"""A unit saved from admin without a cost still loads, as the free unit admin showed."""

import json
from pathlib import Path

from backend.engine.definitions import definitions_from_snapshot
from backend.setup_validation import validate_setup_payload


def _unit(**extra):
    unit = {
        "id": "celebrimbor",
        "display_name": "Celebrimbor",
        "faction": "noldor",
        "archetype": "infantry",
        "attack": 3,
        "defense": 3,
        "movement": 1,
        "health": 1,
    }
    unit.update(extra)
    return unit


def test_missing_cost_loads_as_zero_power():
    units, *_ = definitions_from_snapshot({"units": {"celebrimbor": _unit()}})
    assert units["celebrimbor"].cost == {"power": 0}


def test_set_cost_is_kept():
    units, *_ = definitions_from_snapshot({"units": {"celebrimbor": _unit(cost={"power": 9})}})
    assert units["celebrimbor"].cost == {"power": 9}


def test_save_flags_a_unit_missing_its_stats():
    root = Path(__file__).resolve().parent.parent / "backend" / "data" / "setups" / "woteas_1.0"
    payload = {}
    for key in ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup", "specials"):
        path = root / f"{key}.json"
        payload[key] = json.loads(path.read_text()) if path.exists() else {}
    faction = next(iter(payload["factions"]))
    payload["units"]["celebrimbor"] = {"id": "celebrimbor", "faction": faction}
    errors = validate_setup_payload(payload)
    assert 'unit "celebrimbor" needs display_name' in errors
    assert 'unit "celebrimbor" attack must be a whole number' in errors
