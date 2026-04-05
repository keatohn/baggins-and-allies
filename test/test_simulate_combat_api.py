"""Test POST /simulate-combat API."""
from fastapi.testclient import TestClient
from backend.api.main import app

client = TestClient(app)


def test_simulate_combat_basic():
    r = client.post(
        "/simulate-combat",
        json={
            "attacker_stacks": [{"unit_id": "gondor_soldier", "count": 2}],
            "defender_stacks": [{"unit_id": "morannon_orc", "count": 1}],
            "territory_id": "pelennor",
            "n_trials": 50,
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert "p_attacker_win" in d
    assert "p_conquer" in d
    assert "rounds_mean" in d
    assert d["n_trials"] == 50
    assert 0 <= d["p_attacker_win"] <= 1


def test_simulate_combat_with_options():
    r = client.post(
        "/simulate-combat",
        json={
            "attacker_stacks": [{"unit_id": "gondor_soldier", "count": 1}],
            "defender_stacks": [{"unit_id": "morannon_orc", "count": 2}],
            "territory_id": "pelennor",
            "n_trials": 20,
            "options": {
                "retreat_when_attacker_units_le": 1,
                "must_conquer": True,
            },
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert "retreats" in d
    assert "p_retreat" in d


def test_simulate_combat_battle_context_includes_stealth_when_all_attackers_stealth():
    """Stealth prefire stats work only if stealth_prefire_applicable is passed into combat_specials."""
    r = client.post(
        "/simulate-combat",
        json={
            "attacker_stacks": [{"unit_id": "ithilien_ranger", "count": 2}],
            "defender_stacks": [{"unit_id": "morannon_orc", "count": 1}],
            "territory_id": "pelennor",
            "n_trials": 5,
        },
    )
    assert r.status_code == 200
    bc = r.json().get("battle_context")
    assert bc is not None
    assert "stealth" in (bc.get("specials_in_battle") or {})


def test_simulate_combat_bad_territory():
    r = client.post(
        "/simulate-combat",
        json={
            "attacker_stacks": [{"unit_id": "gondor_soldier", "count": 1}],
            "defender_stacks": [],
            "territory_id": "no_such_territory",
            "n_trials": 10,
        },
    )
    assert r.status_code == 400
