"""Guards on POST /simulate-combat request options (audit items L4-L6).

- L4: run_simulation clamps n_trials to >= 1 (zero trials used to IndexError
  in the percentile aggregation).
- L5: options.fuse_bomb is threaded from the request into SimOptions.
- L6: casualty_order_* are validated against their known values -> 422.
"""
from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.api.main import app
from backend.engine.combat_sim import run_simulation
from backend.engine.definitions import load_static_definitions

client = TestClient(app)


def _post(options=None, n_trials=5):
    body = {
        "attacker_stacks": [{"unit_id": "gondor_soldier", "count": 2}],
        "defender_stacks": [{"unit_id": "morannon_orc", "count": 1}],
        "territory_id": "pelennor",
        "n_trials": n_trials,
    }
    if options is not None:
        body["options"] = options
    return client.post("/simulate-combat", json=body)


# --- L4 ---------------------------------------------------------------------

def test_run_simulation_zero_trials_clamps_to_one():
    """n_trials=0 used to raise IndexError; it now runs a single trial."""
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "morannon_orc", "count": 1}],
        "pelennor", ud, td,
        n_trials=0, seed=3,
    )
    assert res.n_trials == 1
    assert len(res.percentile_outcomes) == 5
    assert 0.0 <= res.p_attacker_win <= 1.0


def test_run_simulation_negative_trials_clamps_to_one():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "morannon_orc", "count": 1}],
        "pelennor", ud, td,
        n_trials=-5, seed=3,
    )
    assert res.n_trials == 1


def test_api_zero_trials_is_clamped_not_an_error():
    r = _post(n_trials=0)
    assert r.status_code == 200
    assert r.json()["n_trials"] >= 1


# --- L5 ---------------------------------------------------------------------

def test_fuse_bomb_false_reaches_sim_options(monkeypatch):
    """options.fuse_bomb=false must land in SimOptions (not be silently dropped)."""
    captured = {}
    real_run = api_main.run_simulation

    def spy(*args, **kwargs):
        captured["options"] = kwargs.get("options")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(api_main, "run_simulation", spy)
    r = _post(options={"fuse_bomb": False})
    assert r.status_code == 200
    assert captured["options"].fuse_bomb is False


def test_fuse_bomb_defaults_true_when_omitted(monkeypatch):
    captured = {}
    real_run = api_main.run_simulation

    def spy(*args, **kwargs):
        captured["options"] = kwargs.get("options")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(api_main, "run_simulation", spy)
    r = _post()
    assert r.status_code == 200
    assert captured["options"].fuse_bomb is True


def test_fuse_bomb_true_explicitly_accepted():
    r = _post(options={"fuse_bomb": True})
    assert r.status_code == 200


# --- L6 ---------------------------------------------------------------------

def test_invalid_attacker_casualty_order_is_422():
    r = _post(options={"casualty_order_attacker": "best_defense"})
    assert r.status_code == 422
    assert "casualty_order_attacker" in r.text


def test_invalid_defender_casualty_order_is_422():
    r = _post(options={"casualty_order_defender": "best_attack"})
    assert r.status_code == 422
    assert "casualty_order_defender" in r.text


def test_unknown_casualty_order_value_is_422():
    r = _post(options={"casualty_order_attacker": "bogus"})
    assert r.status_code == 422


def test_valid_casualty_orders_accepted():
    r = _post(options={"casualty_order_attacker": "best_attack", "casualty_order_defender": "best_defense"})
    assert r.status_code == 200
    r = _post(options={"casualty_order_attacker": "best_unit", "casualty_order_defender": "best_unit"})
    assert r.status_code == 200


def test_omitted_casualty_orders_still_default():
    r = _post(options={})
    assert r.status_code == 200
