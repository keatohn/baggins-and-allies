"""Sim ring boosts are per-instance and side-scoped (audit item Sim M2).

Live combat boosts only the ring's bearer instance on the bearer's side
(rings.combat_boosts -> merge_ring_stat_mods / merge_ring_dice / hp_shield).
The sim used to bake boosts into shared unit defs by unit_id, which boosted
the enemy copy of the same unit type and turned +hp into extra max health.
"""
import pytest
from backend.engine.combat_sim import (
    SimOptions,
    ring_instance_boosts,
    run_one_battle,
    run_simulation,
)
from backend.engine.combat_specials import stacks_to_synthetic_units
from backend.engine.definitions import load_static_definitions
from backend.engine.openings import _boost_rows
from backend.engine.state import Unit


@pytest.fixture
def defs():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud, td


def _two_sides(ud):
    attackers, defenders = stacks_to_synthetic_units(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "gondor_soldier", "count": 1}],
        ud,
    )
    return attackers, defenders


# --- white-box: ring_instance_boosts -----------------------------------------

def test_ring_boost_side_scopes_to_bearer_side(defs):
    ud, _ = defs
    attackers, defenders = _two_sides(ud)
    rows = [{"unit_id": "gondor_soldier", "attack": 2, "side": "attacker"}]
    att, dfn = ring_instance_boosts(rows, attackers, defenders)
    assert att == {attackers[0].instance_id: (2, 0, 0, 0)}
    assert dfn == {}


def test_ring_boost_defender_side_only_touches_defenders(defs):
    ud, _ = defs
    attackers, defenders = _two_sides(ud)
    rows = [{"unit_id": "gondor_soldier", "defense": 3, "side": "defender"}]
    att, dfn = ring_instance_boosts(rows, attackers, defenders)
    assert att == {}
    assert dfn == {defenders[0].instance_id: (0, 3, 0, 0)}


def test_ring_boost_without_side_applies_to_both_sides(defs):
    """Legacy rows (no side) keep old semantics: every matching instance."""
    ud, _ = defs
    attackers, defenders = _two_sides(ud)
    rows = [{"unit_id": "gondor_soldier", "attack": 1, "defense": 1}]
    att, dfn = ring_instance_boosts(rows, attackers, defenders)
    assert att == {attackers[0].instance_id: (1, 1, 0, 0)}
    assert dfn == {defenders[0].instance_id: (1, 1, 0, 0)}


def test_ring_boost_skips_junk_rows_and_unknown_units(defs):
    ud, _ = defs
    attackers, defenders = _two_sides(ud)
    rows = [
        None,
        "not-a-dict",
        {"unit_id": "", "attack": 5},
        {"unit_id": "no_such_unit", "attack": 5, "side": "attacker"},
        {"unit_id": "gondor_soldier", "attack": "x", "defense": -3},
    ]
    att, dfn = ring_instance_boosts(rows, attackers, defenders)
    assert att == {}
    assert dfn == {}


def test_ring_boost_rows_accumulate_per_instance(defs):
    ud, _ = defs
    attackers, defenders = _two_sides(ud)
    rows = [
        {"unit_id": "gondor_soldier", "attack": 1, "side": "attacker"},
        {"unit_id": "gondor_soldier", "attack": 2, "dice": 1, "side": "attacker"},
    ]
    att, dfn = ring_instance_boosts(rows, attackers, defenders)
    assert att == {attackers[0].instance_id: (3, 0, 1, 0)}
    assert dfn == {}


def test_openings_boost_rows_carry_side():
    unit = Unit(
        instance_id="att_0_gondor_soldier",
        unit_id="gondor_soldier",
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )
    rows = _boost_rows({unit.instance_id: (2, 0, 1, 0)}, [unit], side="attacker")
    assert rows == [
        {"unit_id": "gondor_soldier", "attack": 2, "defense": 0, "dice": 1, "hp": 0, "side": "attacker"},
    ]


# --- integration: side-scoped boosts change the right side --------------------

def test_side_scoped_boost_helps_only_the_bearer_side(defs):
    ud, td = defs
    # Mirror match so the same unit_id exists on both sides — the exact case where
    # unit-def baking used to boost the enemy too.
    att = [{"unit_id": "gondor_soldier", "count": 4}]
    dfn = [{"unit_id": "gondor_soldier", "count": 4}]
    base = run_simulation(att, dfn, "pelennor", ud, td, n_trials=300, seed=21)
    att_side = run_simulation(
        att, dfn, "pelennor", ud, td, n_trials=300, seed=21,
        options=SimOptions(ring_boosts=[
            {"unit_id": "gondor_soldier", "attack": 4, "side": "attacker"},
        ]),
    )
    def_side = run_simulation(
        att, dfn, "pelennor", ud, td, n_trials=300, seed=21,
        options=SimOptions(ring_boosts=[
            {"unit_id": "gondor_soldier", "defense": 4, "side": "defender"},
        ]),
    )
    # attacker-side attack boost: attacker always hits, defender still base.
    # defender-side defense boost: defender's defense rolls always hit, attacker base.
    assert att_side.p_attacker_win > base.p_attacker_win + 0.2
    assert def_side.p_attacker_win < base.p_attacker_win - 0.2
    assert att_side.p_attacker_win > def_side.p_attacker_win + 0.4


# --- integration: hp is a shield, dice add a die, siegeworks count ring dice ---

# DICE_SIDES is 10, so attack 6 only hits 60% of the time. Boost the attacker to
# attack 10 in both arms of each comparison so every die is a guaranteed hit and
# the assertions are deterministic.
GUARANTEED_HIT = {"unit_id": "rivendell_knight", "attack": 4, "side": "attacker"}


def test_ring_hp_boost_protects_the_bearer(defs):
    ud, td = defs
    # siege_ladder has 0 dice and 0 defense — it never strikes back.
    att = [{"unit_id": "rivendell_knight", "count": 1}]
    dfn = [{"unit_id": "siege_ladder", "count": 1}]
    base = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=5,
        options=SimOptions(ring_boosts=[GUARANTEED_HIT]),
    )
    shielded = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=5,
        options=SimOptions(ring_boosts=[
            GUARANTEED_HIT,
            {"unit_id": "siege_ladder", "hp": 1, "side": "defender"},
        ]),
    )
    # One guaranteed hit kills a 1-HP ladder in round 1; the ring's shield absorbs
    # that first hit, so the kill lands a round later.
    assert base.rounds == 1
    assert shielded.rounds == 2
    assert base.defender_casualties.get("siege_ladder") == 1
    assert shielded.defender_casualties.get("siege_ladder") == 1


def test_ring_hp_is_a_shield_not_bonus_max_health(defs):
    ud, td = defs
    # One guaranteed hit, two defenders, battle cut after round 1 (max_rounds=1):
    # the cheapest soaks first when both are at full 1 HP. A shield must NOT raise
    # gondor_soldier's remaining health (the old unit-def bake did) — otherwise the
    # soldier would soak the hit and goblin would survive the round.
    att = [{"unit_id": "rivendell_knight", "count": 1}]
    dfn = [
        {"unit_id": "gondor_soldier", "count": 1},
        {"unit_id": "goblin", "count": 1},
    ]
    shielded = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=5,
        options=SimOptions(
            max_rounds=1,
            ring_boosts=[
                GUARANTEED_HIT,
                {"unit_id": "gondor_soldier", "hp": 1, "side": "defender"},
            ],
        ),
    )
    assert shielded.defender_casualties.get("goblin") == 1
    assert "gondor_soldier" not in shielded.defender_casualties


def test_ring_dice_boost_adds_a_die(defs):
    ud, td = defs
    # Guaranteed knight rolls 1 hit/round; +1 dice => 2 hits/round.
    att = [{"unit_id": "rivendell_knight", "count": 1}]
    dfn = [{"unit_id": "siege_ladder", "count": 2}]
    base = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=6,
        options=SimOptions(ring_boosts=[GUARANTEED_HIT]),
    )
    boosted = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=6,
        options=SimOptions(ring_boosts=[
            GUARANTEED_HIT,
            {"unit_id": "rivendell_knight", "dice": 1, "side": "attacker"},
        ]),
    )
    assert base.rounds == 2
    assert boosted.rounds == 1


def test_ring_dice_boost_adds_a_defender_die(defs):
    ud, td = defs
    # Defenders hit on roll <= defense (DICE_SIDES=10): goblin defense 1 + 9 = 10
    # is a guaranteed hit per die. The attacker is two 0-attack ladders that never
    # strike back, so only the defender's dice matter: 1 die kills one ladder per
    # round; +1 ring die kills both in round 1.
    att = [{"unit_id": "siege_ladder", "count": 2}]
    dfn = [{"unit_id": "goblin", "count": 1}]
    def_guaranteed = {"unit_id": "goblin", "defense": 9, "side": "defender"}
    base = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=3,
        options=SimOptions(ring_boosts=[def_guaranteed]),
    )
    boosted = run_one_battle(
        att, dfn, "pelennor", ud, td, seed=3,
        options=SimOptions(ring_boosts=[
            def_guaranteed,
            {"unit_id": "goblin", "dice": 1, "side": "defender"},
        ]),
    )
    assert base.rounds == 2
    assert boosted.rounds == 1


def test_ring_dice_boost_counts_in_siegeworks_round(defs):
    ud, td = defs
    # war_mumakil has the ram special: it rolls in the dedicated siegeworks round
    # vs minas_tirith's walls (base HP 4). Boost its attack to 10 so every die
    # hits: base dice 2 => 2 wall hits; ring +1 die => 3 wall hits.
    att = [{"unit_id": "war_mumakil", "count": 1}]
    dfn = [{"unit_id": "goblin", "count": 1}]
    siege_attack = {"unit_id": "war_mumakil", "attack": 4, "side": "attacker"}
    base = run_one_battle(
        att, dfn, "minas_tirith", ud, td, seed=9,
        options=SimOptions(ring_boosts=[siege_attack]),
    )
    boosted = run_one_battle(
        att, dfn, "minas_tirith", ud, td, seed=9,
        options=SimOptions(ring_boosts=[
            siege_attack,
            {"unit_id": "war_mumakil", "dice": 1, "side": "attacker"},
        ]),
    )
    assert base.siegework_round_applicable
    assert boosted.siegework_round_applicable
    assert base.attacker_siegework_hits == 2
    assert boosted.attacker_siegework_hits == 3
