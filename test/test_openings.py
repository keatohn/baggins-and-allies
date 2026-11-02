"""Opening-attack arrows for the admin map."""

from backend.engine.definitions import load_setup, load_static_definitions
from backend.engine.openings import compute_opening_attacks


def test_starting_faction_openings_are_defended_attacks():
    setup_id = "wotr_exp_1.0"
    units, territories, factions, camps, _ports = load_static_definitions(setup_id=setup_id)
    setup = load_setup(setup_id)
    result = compute_opening_attacks(
        units,
        territories,
        factions,
        camps,
        setup["starting_setup"],
        manifest=setup,
        n_trials=40,
        seed=8,
    )

    assert result["faction_id"] == "isengard"
    assert result["attacks"], "Isengard should have at least one defended opening"
    seen = set()
    for attack in result["attacks"]:
        pair = (attack["from_territory"], attack["to_territory"])
        assert pair not in seen
        seen.add(pair)
        assert attack["from_territory"] != attack["to_territory"]
        assert attack["attacker_stacks"]
        assert attack["defender_stacks"]
        assert all(stack["count"] > 0 for stack in attack["attacker_stacks"])
        assert all(stack["count"] > 0 for stack in attack["defender_stacks"])
        assert 0.0 <= attack["p_attacker_win"] <= 1.0
        assert 0.0 <= attack["p_conquer"] <= 1.0

    origins = {attack["from_territory"] for attack in result["attacks"]}
    assert "isengard" in origins
    targets = {
        attack["to_territory"]
        for attack in result["attacks"]
        if attack["from_territory"] == "isengard"
    }
    assert "fords_of_isen" in targets
