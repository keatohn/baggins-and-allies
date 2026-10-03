"""Starting-setup balance: position-discounted unit power, discounted production, victory pressure."""

from pathlib import Path

import pytest

from backend.balance.starting_strength import BalanceConfig, compute_starting_strength
from backend.engine.definitions import SETUPS_DIR
from backend.setup_data import import_setup_folder_to_dicts

FACTOR = 0.8 + 0.8**2 + 0.8**3


def _territory(tid, adjacent, power=0, stronghold=False, terrain="plains", capitalish=False, ford=None):
    del capitalish
    return {
        "id": tid,
        "display_name": tid.replace("_", " "),
        "terrain_type": terrain,
        "produces": {"power": power},
        "is_stronghold": stronghold,
        "ownable": terrain != "sea",
        "adjacent": adjacent,
        "aerial_adjacent": [],
        "ford_adjacent": ford or [],
    }


def _unit(uid, faction, power, movement=1, archetype="infantry", tags=None):
    return {
        "id": uid,
        "display_name": uid,
        "faction": faction,
        "archetype": archetype,
        "movement": movement,
        "cost": {"power": power},
        "tags": tags if tags is not None else ["land"],
    }


def _bundle(
    *,
    territories,
    owners,
    units,
    starting_units,
    good_target=4,
    evil_target=4,
    rules=None,
    extra_factions=None,
):
    factions = {
        "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "border"},
        "evilland": {"id": "evilland", "display_name": "Evilland", "alliance": "evil", "capital": "dark"},
        "neutral": {"id": "neutral", "display_name": "Neutral", "alliance": "neutral", "capital": ""},
    }
    if extra_factions:
        factions.update(extra_factions)
    return {
        "manifest": {
            "victory_criteria": {"strongholds": {"good": good_target, "evil": evil_target}},
            "special_rules": rules or [],
        },
        "units": units,
        "territories": territories,
        "factions": factions,
        "camps": {},
        "ports": {},
        "starting_setup": {
            "turn_order": ["goodland", "evilland"],
            "territory_owners": owners,
            "starting_units": starting_units,
        },
        "specials": {},
    }


def _line_map():
    """rear — mid — border — gate — dark. Dark is the evil stronghold."""
    territories = {
        "rear": _territory("rear", ["mid"]),
        "mid": _territory("mid", ["rear", "border"]),
        "border": _territory("border", ["mid", "gate"], power=1),
        "gate": _territory("gate", ["border", "dark"], power=1),
        "dark": _territory("dark", ["gate"], power=5, stronghold=True),
    }
    owners = {
        "rear": "goodland",
        "mid": "goodland",
        "border": "goodland",
        "gate": "evilland",
        "dark": "evilland",
    }
    return territories, owners


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_rear_units_are_discounted_more_than_the_front():
    territories, owners = _line_map()
    units = {"infantry": _unit("infantry", "goodland", power=10, movement=1)}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units=units,
            starting_units={
                "rear": [{"unit_id": "infantry", "count": 1}],
                "border": [{"unit_id": "infantry", "count": 1}],
            },
        )
    )
    good = _by_id(report["factions"])["goodland"]
    # Border is 2 steps from dark: attack in 1 turn (0.8). Rear is 4 steps: 3 turns (0.4).
    # Border is the threatened capital: defense now (1.0). Rear is 2 steps away: 2 turns (0.6).
    assert good["effective_unit_power_attack"] == pytest.approx(8 + 4, abs=0.02)
    assert good["effective_unit_power_defense"] == pytest.approx(10 + 6, abs=0.02)
    assert good["effective_unit_power"] == pytest.approx(14, abs=0.02)
    assert good["unit_power"] == 20
    assert good["immediate_attack_power"] == 0
    assert good["immediate_defense_power"] == 10
    assert good["power_production"] == 1


def test_faster_units_close_the_same_ground_sooner():
    territories, owners = _line_map()
    units = {
        "horse": _unit("horse", "goodland", power=10, movement=2, archetype="cavalry"),
        "eagle": _unit("eagle", "goodland", power=10, movement=4, archetype="aerial", tags=["aerial"]),
    }
    horse = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units=units,
            starting_units={"rear": [{"unit_id": "horse", "count": 1}]},
        )
    )
    air = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units=units,
            starting_units={"rear": [{"unit_id": "eagle", "count": 1}]},
        )
    )
    horse_row = _by_id(horse["factions"])["goodland"]
    air_row = _by_id(air["factions"])["goodland"]
    # 4 steps at movement 2: attack turns = 1 (0.8). Defense 2 steps: 1 turn (0.8).
    assert horse_row["effective_unit_power"] == pytest.approx(8, abs=0.02)
    # 4 steps at movement 4: can attack immediately. Defense still takes a move.
    assert air_row["effective_unit_power_attack"] == pytest.approx(10, abs=0.02)
    assert air_row["effective_unit_power_defense"] == pytest.approx(8, abs=0.02)
    assert air_row["immediate_attack_power"] == 10


def test_flat_economy_uses_the_three_round_discount():
    territories, owners = _line_map()
    report = compute_starting_strength(
        _bundle(territories=territories, owners=owners, units={}, starting_units={})
    )
    good = _by_id(report["alliances"])["good"]
    evil = _by_id(report["alliances"])["evil"]
    assert good["power_production"] == 1
    assert evil["power_production"] == 6
    assert good["economic_power"] == pytest.approx(1 * FACTOR, abs=0.02)
    assert evil["economic_power"] == pytest.approx(6 * FACTOR, abs=0.02)
    assert report["parameters"]["economic_coefficient"] == pytest.approx(FACTOR, abs=0.02)
    assert good["starting_power_score"] == pytest.approx(good["economic_power"], abs=0.02)


def test_fading_territory_changes_later_rounds_only():
    territories = {"home": _territory("home", [], power=10, stronghold=True)}
    rules = [{
        "type": "fading_territory",
        "territories": [{"territory_id": "home", "fade_per_turn": 2, "floor": 4}],
    }]
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners={"home": "goodland"},
            units={},
            starting_units={},
            rules=rules,
            extra_factions={
                "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "home"},
            },
        )
    )
    # Turns 1–3 produce 10, 8, 6. Evil owns nothing.
    expected = 0.8 * 10 + 0.8**2 * 8 + 0.8**3 * 6
    good = _by_id(report["alliances"])["good"]
    assert good["power_production"] == 10
    assert good["economic_power"] == pytest.approx(expected, abs=0.02)


def test_no_land_route_uses_the_availability_floor():
    territories = {
        "isle": _territory("isle", ["sea"]),
        "sea": _territory("sea", ["isle", "homeland", "coast"], terrain="sea"),
        "homeland": _territory("homeland", ["sea"], power=1),
        "coast": _territory("coast", ["sea"], power=5, stronghold=True),
    }
    units = {"infantry": _unit("infantry", "goodland", power=8, movement=1)}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners={"isle": "goodland", "homeland": "goodland", "coast": "evilland"},
            units=units,
            starting_units={"isle": [{"unit_id": "infantry", "count": 1}]},
            extra_factions={
                "goodland": {
                    "id": "goodland",
                    "display_name": "Goodland",
                    "alliance": "good",
                    "capital": "homeland",
                },
            },
        )
    )
    good = _by_id(report["factions"])["goodland"]
    assert good["unreachable_attack_power"] == 8
    assert good["effective_unit_power"] == pytest.approx(8 * 0.25, abs=0.02)
    assert any("do not board ships" in line for line in report["readings"])


def test_ford_link_counts_as_one_step():
    territories = {
        "camp": _territory("camp", [], ford=["fort"]),
        "fort": _territory("fort", [], power=4, stronghold=True, ford=["camp"]),
    }
    units = {"infantry": _unit("infantry", "goodland", power=10, movement=1)}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners={"camp": "goodland", "fort": "evilland"},
            units=units,
            starting_units={"camp": [{"unit_id": "infantry", "count": 1}]},
            extra_factions={
                "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "camp"},
            },
        )
    )
    good = _by_id(report["factions"])["goodland"]
    assert good["immediate_attack_power"] == 10
    assert good["effective_unit_power_attack"] == pytest.approx(10, abs=0.02)


def test_immobile_garrison_on_a_capital_still_counts_as_defense():
    territories = {
        "capital": _territory("capital", ["road"], power=5, stronghold=True),
        "road": _territory("road", ["capital", "front"]),
        "front": _territory("front", ["road", "foe"], stronghold=True),
        "foe": _territory("foe", ["front"], power=5, stronghold=True),
    }
    owners = {"capital": "goodland", "road": "goodland", "front": "goodland", "foe": "evilland"}
    units = {"engine": _unit("engine", "goodland", power=10, movement=0, archetype="siegework")}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units=units,
            starting_units={"capital": [{"unit_id": "engine", "count": 1}]},
            extra_factions={
                "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "capital"},
                "evilland": {"id": "evilland", "display_name": "Evilland", "alliance": "evil", "capital": "foe"},
            },
        )
    )
    good = _by_id(report["factions"])["goodland"]
    # The front stronghold is the threatened objective, but the engine is already holding the capital.
    assert good["immediate_defense_power"] == 10
    assert good["effective_unit_power_defense"] == pytest.approx(10, abs=0.02)
    assert good["effective_unit_power_attack"] == pytest.approx(2.5, abs=0.02)
    assert report["largest_discounts"][0]["attack_turn_label"] == "immobile"


def test_ship_in_port_uses_the_sea_graph():
    territories = {
        "port": _territory("port", ["sea_near"], power=1),
        "sea_near": _territory("sea_near", ["port", "sea_far"], terrain="sea"),
        "sea_far": _territory("sea_far", ["sea_near", "enemy_coast"], terrain="sea"),
        "enemy_coast": _territory("enemy_coast", ["sea_far"], power=5, stronghold=True),
    }
    units = {"ship": _unit("ship", "goodland", power=8, movement=2, archetype="naval", tags=["naval"])}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners={"port": "goodland", "enemy_coast": "evilland"},
            units=units,
            starting_units={"port": [{"unit_id": "ship", "count": 1}]},
            extra_factions={
                "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "port"},
                "evilland": {"id": "evilland", "display_name": "Evilland", "alliance": "evil", "capital": "enemy_coast"},
            },
        )
    )
    good = _by_id(report["factions"])["goodland"]
    # Embark (1) + one sea step to the coast's sea zone. Movement 2 attacks that turn.
    assert good["immediate_attack_power"] == 8
    assert good["unreachable_attack_power"] == 0


def test_strongholds_to_win_is_the_remaining_burden():
    territories, owners = _line_map()
    territories["border"] = _territory("border", ["mid", "gate"], power=1, stronghold=True)
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units={},
            starting_units={},
            good_target=4,
            evil_target=1,
        )
    )
    alliances = _by_id(report["alliances"])
    assert alliances["good"]["strongholds"] == 1
    assert alliances["good"]["strongholds_to_win"] == 3
    assert alliances["evil"]["strongholds"] == 1
    assert alliances["evil"]["strongholds_to_win"] == 0
    text = " ".join(report["readings"])
    assert "needs 3 more strongholds" in text
    assert "already holds enough strongholds" in text


def test_neutral_units_stay_out_of_the_alliance_split():
    territories, owners = _line_map()
    units = {
        "soldier": _unit("soldier", "goodland", power=10, movement=1),
        "orc": _unit("orc", "evilland", power=10, movement=1),
        "wight": _unit("wight", "neutral", power=50, movement=0),
    }
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units=units,
            starting_units={
                "border": [{"unit_id": "soldier", "count": 1}],
                "dark": [{"unit_id": "orc", "count": 1}],
                "mid": [{"unit_id": "wight", "count": 1}],
            },
        )
    )
    assert report["neutral"]["unit_power"] == 50
    assert "neutral" not in _by_id(report["factions"])
    alliances = _by_id(report["alliances"])
    assert alliances["good"]["unit_power"] == 10
    assert alliances["evil"]["unit_power"] == 10
    assert alliances["good"]["starting_power_share"] + alliances["evil"]["starting_power_share"] == pytest.approx(1, abs=0.0001)


def test_share_uses_effective_power_plus_economy():
    territories, owners = _line_map()
    units = {"infantry": _unit("infantry", "goodland", power=10, movement=1)}
    report = compute_starting_strength(
        _bundle(
            territories=territories,
            owners=owners,
            units={"infantry": units["infantry"], "orc": _unit("orc", "evilland", power=10, movement=1)},
            starting_units={
                "border": [{"unit_id": "infantry", "count": 1}],
                "dark": [{"unit_id": "orc", "count": 1}],
            },
        )
    )
    alliances = _by_id(report["alliances"])
    good_score = alliances["good"]["effective_unit_power"] + alliances["good"]["economic_power"]
    evil_score = alliances["evil"]["effective_unit_power"] + alliances["evil"]["economic_power"]
    assert alliances["good"]["starting_power_score"] == pytest.approx(good_score, abs=0.02)
    assert alliances["evil"]["starting_power_score"] == pytest.approx(evil_score, abs=0.02)
    assert alliances["good"]["starting_power_share"] == pytest.approx(
        good_score / (good_score + evil_score), abs=0.002
    )


def test_real_setups_produce_a_finite_split():
    for setup_id in ("wotr_1.1", "wotla_1.0"):
        bundle = import_setup_folder_to_dicts(Path(SETUPS_DIR) / setup_id)
        assert bundle is not None
        report = compute_starting_strength(bundle)
        alliances = _by_id(report["alliances"])
        assert set(alliances) >= {"good", "evil"}
        for row in alliances.values():
            assert row["effective_unit_power"] <= row["unit_power"] + 0.05
            assert row["economic_power"] >= 0
            assert row["strongholds_to_win"] is not None
            assert row["strongholds_to_win"] <= row["stronghold_target"]
        share = alliances["good"]["starting_power_share"] + alliances["evil"]["starting_power_share"]
        assert share == pytest.approx(1, abs=0.0001)
        assert report["readings"]
        assert report["parameters"]["summary"]


def test_admin_balance_endpoint_returns_the_report():
    from fastapi.testclient import TestClient

    from backend.api.auth import get_current_admin
    from backend.api.main import app

    bundle = import_setup_folder_to_dicts(Path(SETUPS_DIR) / "wotr_1.1")
    assert bundle is not None
    app.dependency_overrides[get_current_admin] = lambda: object()
    try:
        client = TestClient(app)
        response = client.post(
            "/admin/balance",
            json={
                "manifest": bundle["manifest"],
                "units": bundle["units"],
                "territories": bundle["territories"],
                "factions": bundle["factions"],
                "camps": bundle["camps"],
                "ports": bundle["ports"],
                "starting_setup": bundle["starting_setup"],
                "specials": bundle["specials"],
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
    assert response.status_code == 200
    payload = response.json()
    assert payload["alliances"]
    assert "summary" in payload["parameters"]


def test_config_horizon_changes_economic_power():
    territories = {"home": _territory("home", [], power=10)}
    bundle = _bundle(
        territories=territories,
        owners={"home": "goodland"},
        units={},
        starting_units={},
        extra_factions={
            "goodland": {"id": "goodland", "display_name": "Goodland", "alliance": "good", "capital": "home"},
        },
    )
    short = compute_starting_strength(bundle, config=BalanceConfig(horizon_rounds=1))
    assert _by_id(short["alliances"])["good"]["economic_power"] == pytest.approx(8, abs=0.02)
