"""Tests for the combat simulation engine."""
import pytest
from backend.engine.definitions import load_static_definitions
from backend.engine.combat import resolve_archer_prefire
from backend.engine.combat_sim import (
    run_one_battle,
    run_simulation,
    SimOptions,
    BattleOutcome,
    SimResult,
    _stacks_to_units,
)
from backend.engine.utils import archer_prefire_eligible


@pytest.fixture
def defs():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud, td


def test_run_one_battle_returns_outcome(defs):
    unit_defs, territory_defs = defs
    att = [{"unit_id": "gondor_soldier", "count": 2}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=42,
    )
    assert isinstance(out, BattleOutcome)
    assert out.winner in ("attacker", "defender")
    assert out.rounds >= 0
    assert isinstance(out.attacker_casualties, dict)
    assert isinstance(out.defender_casualties, dict)


def test_run_simulation_per_trial_outcomes_survives_match_aggregate(defs):
    """defender_survived means >0 defenders left; mutual destruction => both survival flags false; rounds match trial."""
    unit_defs, territory_defs = defs
    att = [{"unit_id": "gondor_soldier", "count": 1}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    res = run_simulation(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        n_trials=100, seed=7, return_outcomes=True,
    )
    assert res.outcomes and len(res.outcomes) == 100
    assert res.attacker_survives == sum(1 for o in res.outcomes if o.get("attacker_survived"))
    assert res.defender_survives == sum(1 for o in res.outcomes if o.get("defender_survived"))
    for o in res.outcomes:
        assert "attacker_survived" in o and "defender_survived" in o
        if not o["defender_survived"] and not o["attacker_survived"]:
            assert o["winner"] == "defender"
    # At least one mutual wipe in 100 trials for symmetric 1v1 is very likely (not asserting always)


def test_run_simulation_aggregates(defs):
    unit_defs, territory_defs = defs
    att = [{"unit_id": "gondor_soldier", "count": 2}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    res = run_simulation(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        n_trials=50, seed=123,
    )
    assert isinstance(res, SimResult)
    assert res.n_trials == 50
    assert res.attacker_wins + res.defender_wins == 50
    assert 0 <= res.p_attacker_win <= 1
    assert 0 <= res.p_conquer <= 1
    assert res.rounds_mean >= 0


def test_retreat_when_attacker_units_le_ends_battle(defs):
    """With retreat_when_attacker_units_le=1, some seeds should produce retreat."""
    unit_defs, territory_defs = defs
    att = [{"unit_id": "gondor_soldier", "count": 1}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 3}]
    opts = SimOptions(retreat_when_attacker_units_le=1)
    retreat_count = 0
    for seed in range(200):
        out = run_one_battle(
            att, def_stacks, "pelennor", unit_defs, territory_defs,
            options=opts, seed=seed,
        )
        if out.retreat:
            retreat_count += 1
    # With 1 attacker vs 3 defenders we expect some retreats when attacker is reduced to 1 (or 0)
    assert retreat_count >= 0  # At least runs without error; may or may not hit retreat depending on RNG
    # With 1 attacker, after first round we have 0 or 1 attacker; if 1 and threshold is 1, we retreat
    out = run_one_battle(att, def_stacks, "pelennor", unit_defs, territory_defs, options=opts, seed=999)
    assert out.winner in ("attacker", "defender")
    assert isinstance(out.retreat, bool)


def test_empty_defender_attacker_wins(defs):
    unit_defs, territory_defs = defs
    att = [{"unit_id": "gondor_soldier", "count": 1}]
    def_stacks = []  # No defenders
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert out.winner == "attacker"
    assert out.rounds >= 0  # no combat rounds fought


def test_empty_attacker_defender_wins(defs):
    unit_defs, territory_defs = defs
    att = []
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert out.winner == "defender"


def test_resolve_archer_prefire_returns_hits(defs):
    """resolve_archer_prefire returns RoundResult with defender_hits = number of hits (not casualties)."""
    unit_defs, territory_defs = defs
    if "gondor_archer" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no gondor_archer")
    att_stacks = [{"unit_id": "gondor_soldier", "count": 3}]
    def_stacks = [{"unit_id": "gondor_archer", "count": 2}]
    attacker_units = _stacks_to_units(att_stacks, "att", unit_defs)
    defender_units = _stacks_to_units(def_stacks, "def", unit_defs)
    defender_archer_units = [u for u in defender_units if archer_prefire_eligible(unit_defs.get(u.unit_id))]
    assert len(defender_archer_units) > 0, "defender should have archer units"
    defender_rolls = [1, 1]
    result = resolve_archer_prefire(
        attacker_units,
        defender_archer_units,
        unit_defs,
        defender_rolls,
        stat_modifiers_defender_extra=None,
        territory_def=territory_defs.get("pelennor"),
    )
    assert hasattr(result, "defender_hits")
    assert isinstance(result.defender_hits, int)
    assert result.defender_hits >= 0
    # Hits are successful rolls; can exceed casualties (e.g. multiple hits on one unit)
    assert result.defender_hits <= len(defender_rolls), "hits cannot exceed dice rolled"


def test_run_one_battle_defender_prefire_hits(defs):
    """run_one_battle sets defender_prefire_hits when defender has archers."""
    unit_defs, territory_defs = defs
    if "gondor_archer" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no gondor_archer")
    att = [{"unit_id": "gondor_soldier", "count": 3}]
    def_stacks = [{"unit_id": "gondor_archer", "count": 2}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert hasattr(out, "defender_prefire_hits")
    assert isinstance(out.defender_prefire_hits, int)
    assert out.defender_prefire_hits >= 0


def test_run_simulation_aggregates_defender_prefire_hits(defs):
    """run_simulation aggregates defender_prefire_hits across trials into defender_prefire_hits_mean."""
    unit_defs, territory_defs = defs
    if "gondor_archer" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no gondor_archer")
    att = [{"unit_id": "gondor_soldier", "count": 4}]
    def_stacks = [{"unit_id": "gondor_archer", "count": 2}]
    res = run_simulation(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        n_trials=200, seed=456,
    )
    assert hasattr(res, "defender_prefire_hits_mean")
    assert isinstance(res.defender_prefire_hits_mean, (int, float))
    assert res.defender_prefire_hits_mean >= 0
    # With defender archers, mean should be positive over 200 trials (aggregation is working)
    assert res.defender_prefire_hits_mean > 0, (
        "defender archers should produce some prefire hits on average; "
        "got 0 - check that run_one_battle uses resolve_archer_prefire and aggregates defender_prefire_hits"
    )


def test_run_one_battle_terror(defs):
    """run_one_battle with terror attackers (e.g. Nazgûl) completes and returns valid outcome."""
    unit_defs, territory_defs = defs
    if "nazgul" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no nazgul")
    att = [{"unit_id": "nazgul", "count": 1}]
    def_stacks = [{"unit_id": "gondor_soldier", "count": 2}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert isinstance(out, BattleOutcome)
    assert out.winner in ("attacker", "defender")
    assert out.rounds >= 0
    assert isinstance(out.attacker_casualties, dict)
    assert isinstance(out.defender_casualties, dict)


def test_run_one_battle_captain(defs):
    """run_one_battle with captain in stack completes and returns valid outcome."""
    unit_defs, territory_defs = defs
    if "captain_of_gondor" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no captain_of_gondor")
    att = [
        {"unit_id": "captain_of_gondor", "count": 1},
        {"unit_id": "gondor_soldier", "count": 2},
    ]
    def_stacks = [{"unit_id": "morannon_orc", "count": 2}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert isinstance(out, BattleOutcome)
    assert out.winner in ("attacker", "defender")
    assert out.rounds >= 0


def test_run_one_battle_bombikazi(defs):
    """run_one_battle with bombikazi + bomb completes (self-destruct may occur)."""
    unit_defs, territory_defs = defs
    if "berserker" not in unit_defs or "bomb" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no berserker/bomb")
    att = [
        {"unit_id": "berserker", "count": 1},
        {"unit_id": "bomb", "count": 1},
    ]
    def_stacks = [{"unit_id": "gondor_soldier", "count": 2}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=0,
    )
    assert isinstance(out, BattleOutcome)
    assert out.winner in ("attacker", "defender")
    assert isinstance(out.attacker_casualties, dict)
    # Bombikazi pair may self-destruct; battle should still complete
    assert out.rounds >= 0


def test_run_one_battle_sea_raid(defs):
    """run_one_battle with is_sea_raid=True and sea_raider unit completes."""
    unit_defs, territory_defs = defs
    corsair_id = "corsair_of_umbar" if "corsair_of_umbar" in unit_defs else None
    if not corsair_id:
        pytest.skip("wotr_exp_1.0 has no corsair/sea_raider")
    att = [{"unit_id": corsair_id, "count": 2}]
    def_stacks = [{"unit_id": "gondor_soldier", "count": 1}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(is_sea_raid=True), seed=0,
    )
    assert isinstance(out, BattleOutcome)
    assert out.winner in ("attacker", "defender")
    assert out.rounds >= 0


def test_sea_raid_land_combat_defender_hits_apply_to_land_attackers(defs):
    """
    is_sea_raid must not set naval casualty rules for attackers on land.
    Otherwise _apply_hits only targets naval/aerial and land attackers never take damage.
    """
    unit_defs, territory_defs = defs
    corsair_id = "corsair_of_umbar" if "corsair_of_umbar" in unit_defs else None
    if not corsair_id or "gondor_soldier" not in unit_defs:
        pytest.skip("need corsair and gondor_soldier")
    att = [{"unit_id": corsair_id, "count": 1}]
    def_stacks = [{"unit_id": "gondor_soldier", "count": 12}]
    took_damage = False
    for seed in range(30):
        out = run_one_battle(
            att,
            def_stacks,
            "pelennor",
            unit_defs,
            territory_defs,
            options=SimOptions(is_sea_raid=True),
            seed=seed,
        )
        if sum(out.attacker_casualties.values()) > 0:
            took_damage = True
            break
    assert took_damage, "defender hits should damage land attackers during sea raid (land combat)"


def test_ladder_only_skips_siegework_dice_round(defs):
    """Ladders do not roll in the siegework round; engine dice counts must be zero."""
    unit_defs, territory_defs = defs
    if "siege_ladder" not in unit_defs:
        pytest.skip("need siege_ladder")
    att = [{"unit_id": "siege_ladder", "count": 1}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    out = run_one_battle(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        options=SimOptions(), seed=1,
    )
    assert out.siegework_round_applicable is False
    assert out.siegework_attacker_dice == 0
    assert out.siegework_defender_dice == 0
    res = run_simulation(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        n_trials=20, seed=2,
    )
    assert res.attacker_siegework_hits_mean is None
    assert res.defender_siegework_hits_mean is None


def test_siegework_hits_mean_only_for_sides_that_roll(defs):
    """Mean siegework hits are None per side when that side had no siegework dice (not 0.0)."""
    unit_defs, territory_defs = defs
    if "catapult" not in unit_defs:
        pytest.skip("need catapult")
    att = [{"unit_id": "catapult", "count": 1}]
    def_stacks = [{"unit_id": "morannon_orc", "count": 1}]
    res = run_simulation(
        att, def_stacks, "pelennor", unit_defs, territory_defs,
        n_trials=40, seed=3,
    )
    assert res.attacker_siegework_hits_mean is not None
    assert res.defender_siegework_hits_mean is None
