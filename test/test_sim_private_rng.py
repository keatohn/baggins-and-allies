"""
Sim M4: private per-battle RNG and stack-count clamp.

- run_one_battle/run_simulation must never seed or read the global `random` module
  (concurrent sims and AI must not bleed RNG state into each other), while `seed`
  stays reproducible.
- stacks_to_synthetic_units must clamp per-stack counts so raw API payloads cannot
  allocate unbounded Unit lists (OOM).
"""
import random

import pytest
from backend.engine.combat_specials import MAX_STACK_COUNT, stacks_to_synthetic_units
from backend.engine.combat_sim import SimOptions, run_one_battle, run_simulation
from backend.engine.definitions import load_static_definitions
from backend.engine.utils import generate_dice_rolls_for_units


@pytest.fixture
def defs():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud, td


ATT = [{"unit_id": "gondor_soldier", "count": 2}]
DEF = [{"unit_id": "morannon_orc", "count": 2}]


def _global_sample(n: int = 8) -> list[float]:
    return [random.random() for _ in range(n)]


def test_seeded_battle_leaves_global_rng_untouched(defs):
    unit_defs, territory_defs = defs
    random.seed(12345)
    expected = _global_sample()
    random.seed(12345)
    run_one_battle(
        ATT, DEF, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=999,
    )
    assert _global_sample() == expected


def test_unseeded_battle_leaves_global_rng_untouched(defs):
    unit_defs, territory_defs = defs
    random.seed(54321)
    expected = _global_sample()
    random.seed(54321)
    run_one_battle(
        ATT, DEF, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=None,
    )
    assert _global_sample() == expected


def test_simulation_leaves_global_rng_untouched(defs):
    unit_defs, territory_defs = defs
    random.seed(777)
    expected = _global_sample()
    random.seed(777)
    run_simulation(
        ATT, DEF, "pelennor", unit_defs, territory_defs,
        n_trials=25, seed=7,
    )
    assert _global_sample() == expected


def test_same_seed_reproduces_identical_outcome(defs):
    unit_defs, territory_defs = defs
    kwargs = dict(options=SimOptions(), seed=42)
    out1 = run_one_battle(ATT, DEF, "pelennor", unit_defs, territory_defs, **kwargs)
    out2 = run_one_battle(ATT, DEF, "pelennor", unit_defs, territory_defs, **kwargs)
    assert out1 == out2


def test_run_simulation_same_seed_same_aggregates(defs):
    unit_defs, territory_defs = defs
    res1 = run_simulation(ATT, DEF, "pelennor", unit_defs, territory_defs, n_trials=40, seed=7)
    res2 = run_simulation(ATT, DEF, "pelennor", unit_defs, territory_defs, n_trials=40, seed=7)
    assert res1.attacker_wins == res2.attacker_wins
    assert res1.defender_wins == res2.defender_wins
    assert res1.conquers == res2.conquers
    assert res1.rounds_mean == res2.rounds_mean
    assert res1.attacker_casualties_mean == res2.attacker_casualties_mean
    assert res1.defender_casualties_mean == res2.defender_casualties_mean


def test_private_rng_matches_legacy_seeded_sequence(defs):
    """Random(seed) inits identically to random.seed(seed): seeded roll sequences are unchanged."""
    unit_defs, _ = defs
    units, _ = stacks_to_synthetic_units(ATT, [], unit_defs)
    state = random.getstate()
    try:
        random.seed(42)
        legacy = generate_dice_rolls_for_units(units, unit_defs, seed=42)
    finally:
        random.setstate(state)
    private = generate_dice_rolls_for_units(units, unit_defs, rng=random.Random(42))
    assert private == legacy


def test_generate_dice_rolls_with_rng_leaves_global_untouched(defs):
    unit_defs, _ = defs
    units, _ = stacks_to_synthetic_units(ATT, [], unit_defs)
    random.seed(99)
    expected = _global_sample()
    random.seed(99)
    generate_dice_rolls_for_units(units, unit_defs, rng=random.Random(1))
    assert _global_sample() == expected


def test_stack_count_clamped_to_max(defs):
    unit_defs, _ = defs
    attackers, defenders = stacks_to_synthetic_units(
        [{"unit_id": "gondor_soldier", "count": 10**9}],
        [{"unit_id": "morannon_orc", "count": 10**9}],
        unit_defs,
    )
    assert len(attackers) == MAX_STACK_COUNT
    assert len(defenders) == MAX_STACK_COUNT


def test_stack_count_negative_and_zero_produce_no_units(defs):
    unit_defs, _ = defs
    attackers, defenders = stacks_to_synthetic_units(
        [{"unit_id": "gondor_soldier", "count": -5}],
        [{"unit_id": "morannon_orc", "count": 0}],
        unit_defs,
    )
    assert attackers == []
    assert defenders == []
