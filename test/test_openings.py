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

    assert result["conquer_threshold"] == 0.75
    order = result["factions"]
    assert order
    assert order[0]["faction_id"] == "isengard"
    ids = [row["faction_id"] for row in order]
    turn = [fid for fid in setup["starting_setup"]["turn_order"] if fid in set(ids)]
    assert ids == turn
    opening = order[0]
    assert opening["attacks"], "Isengard should have at least one defended opening"
    seen = set()
    for attack in opening["attacks"]:
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

    origins = {attack["from_territory"] for attack in opening["attacks"]}
    assert "isengard" in origins
    targets = {
        attack["to_territory"]
        for attack in opening["attacks"]
        if attack["from_territory"] == "isengard"
    }
    assert "fords_of_isen" in targets

    for row in order:
        for attack in row["assumed"]:
            assert attack["p_conquer"] >= 0.75
    if len(order) > 1 and order[0]["assumed"]:
        nxt = order[1]["board"]
        for attack in order[0]["assumed"]:
            arrived = nxt["units"].get(attack["to_territory"]) or []
            assert arrived, f"{attack['to_territory']} should hold the units that took it"
            assert nxt["owners"].get(attack["to_territory"])
