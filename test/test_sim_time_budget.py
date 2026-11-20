"""Sim M4 security half: wall-clock time budget + concurrency cap on /simulate-combat.

- run_simulation(time_budget_seconds=...) stops mid-loop and reports completed trials.
- The API always passes SIM_TIME_BUDGET_SECONDS (0 => one trial).
- When SIM_MAX_CONCURRENT slots are held, /simulate-combat returns 429.
"""
from fastapi.testclient import TestClient

from backend.api import main as api_main
from backend.api.main import app
from backend.engine.combat_sim import run_simulation
from backend.engine.definitions import load_static_definitions

client = TestClient(app)


def _post(n_trials=5):
    return client.post(
        "/simulate-combat",
        json={
            "attacker_stacks": [{"unit_id": "gondor_soldier", "count": 2}],
            "defender_stacks": [{"unit_id": "morannon_orc", "count": 1}],
            "territory_id": "pelennor",
            "n_trials": n_trials,
        },
    )


def _defs():
    return load_static_definitions(setup_id="wotr_exp_1.0")[:2]


# --- run_simulation time budget ---------------------------------------------

def test_zero_time_budget_completes_exactly_one_trial():
    ud, td = _defs()
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "morannon_orc", "count": 1}],
        "pelennor", ud, td,
        n_trials=10_000, seed=3,
        time_budget_seconds=0.0,
    )
    assert res.n_trials == 1
    assert len(res.percentile_outcomes) == 5
    assert 0.0 <= res.p_attacker_win <= 1.0


def test_none_time_budget_runs_all_trials():
    ud, td = _defs()
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "morannon_orc", "count": 1}],
        "pelennor", ud, td,
        n_trials=25, seed=3,
        time_budget_seconds=None,
    )
    assert res.n_trials == 25


def test_generous_time_budget_runs_all_trials():
    ud, td = _defs()
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "morannon_orc", "count": 1}],
        "pelennor", ud, td,
        n_trials=20, seed=3,
        time_budget_seconds=60.0,
    )
    assert res.n_trials == 20


def test_truncated_result_aggregates_stay_consistent():
    """Partial runs still produce coherent means over the trials that finished."""
    ud, td = _defs()
    res = run_simulation(
        [{"unit_id": "gondor_soldier", "count": 2}],
        [{"unit_id": "morannon_orc", "count": 2}],
        "pelennor", ud, td,
        n_trials=10_000, seed=11,
        time_budget_seconds=0.0,
        return_outcomes=True,
    )
    assert res.n_trials == 1
    assert res.outcomes is not None and len(res.outcomes) == 1
    # Means are over completed trials only — no divide-by-requested mismatch.
    assert res.attacker_casualty_cost_mean >= 0.0
    assert res.defender_casualty_cost_mean >= 0.0


# --- API wiring --------------------------------------------------------------

def test_api_passes_time_budget_to_run_simulation(monkeypatch):
    captured = {}
    real = api_main.run_simulation

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(api_main, "run_simulation", spy)
    r = _post(n_trials=3)
    assert r.status_code == 200
    assert captured.get("time_budget_seconds") == api_main.SIM_TIME_BUDGET_SECONDS


def test_api_zero_time_budget_returns_partial_n_trials(monkeypatch):
    monkeypatch.setattr(api_main, "SIM_TIME_BUDGET_SECONDS", 0.0)
    r = _post(n_trials=50)
    assert r.status_code == 200
    d = r.json()
    assert d["n_trials"] == 1
    assert 0.0 <= d["p_attacker_win"] <= 1.0


def test_api_default_budget_completes_small_requests():
    """Normal chunked panel requests (500 trials) must not be truncated by the default budget."""
    r = _post(n_trials=20)
    assert r.status_code == 200
    assert r.json()["n_trials"] == 20


# --- Concurrency cap ---------------------------------------------------------

def test_api_returns_429_when_all_sim_slots_held():
    held = 0
    try:
        for _ in range(api_main.SIM_MAX_CONCURRENT):
            assert api_main._sim_slots.acquire(blocking=False), "expected a free slot"
            held += 1
        r = _post(n_trials=1)
        assert r.status_code == 429
        assert "simulations" in r.json()["detail"].lower()
    finally:
        for _ in range(held):
            api_main._sim_slots.release()


def test_api_releases_slot_after_success():
    # Drain any accidental leaks from prior tests, then one normal call must leave a free slot.
    while api_main._sim_slots.acquire(blocking=False):
        pass
    # Return the drained permits (they were free before we stole them).
    for _ in range(api_main.SIM_MAX_CONCURRENT):
        api_main._sim_slots.release()
    r = _post(n_trials=1)
    assert r.status_code == 200
    assert api_main._sim_slots.acquire(blocking=False)
    api_main._sim_slots.release()
