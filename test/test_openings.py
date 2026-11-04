"""Opening moves for the admin map."""

from backend.engine.definitions import (
    FactionDefinition,
    TerritoryDefinition,
    UnitDefinition,
    load_setup,
    load_static_definitions,
)
from backend.engine.movement import _is_water_craft, _is_water_zone
from backend.engine.openings import _fight_role, clear_opening_cache, compute_opening_attacks
from backend.engine.state import Unit
from backend.engine.utils import is_aerial_unit


def _assert_legal_attack(attack, units, territories):
    origin = territories[attack["from_territory"]]
    dest = territories[attack["to_territory"]]
    dest_water = _is_water_zone(dest)
    for stack in attack["attacker_stacks"]:
        unit_def = units[stack["unit_id"]]
        if dest_water:
            assert _is_water_craft(unit_def) or is_aerial_unit(unit_def)
        else:
            assert not _is_water_craft(unit_def)
        assert stack["count"] > 0
    if _is_water_zone(origin) and not dest_water:
        assert attack["is_sea_raid"]
    if attack["empty"]:
        assert attack["defender_stacks"] == []
        assert attack["p_conquer"] == 1.0
        assert attack["p_attacker_win"] == 1.0
    else:
        assert attack["defender_stacks"]


def test_fight_role_keeps_boats_and_land_units_in_their_domain():
    land = TerritoryDefinition("shore", "Shore", "land", ["bay"], {"power": 1})
    sea = TerritoryDefinition("bay", "Bay", "sea", ["shore"], {})
    orc_def = UnitDefinition("orc", "Orc", "isengard", "infantry", ["transportable"], 1, 1, 1, 1, {"power": 3})
    boat_def = UnitDefinition("rowboat", "Rowboat", "isengard", "river", ["river"], 1, 1, 1, 1, {"power": 4})
    units = {"orc": orc_def, "rowboat": boat_def}
    orc = Unit("o", "orc", 1, 1, 1, 1)
    boat = Unit("b", "rowboat", 1, 1, 1, 1)

    river = TerritoryDefinition("anduin", "Anduin", "river", ["shore"], {})
    assert _fight_role(orc, land, sea, units) is None
    assert _fight_role(boat, river, land, units) is None
    assert _fight_role(orc, river, land, units) == "sea_raid"
    assert _fight_role(boat, river, river, units) == "naval"


def _small_setup():
    """One stack chooses a stronghold over a poor empty field, and another takes empty land."""
    territories = {
        "camp": TerritoryDefinition("camp", "Camp", "land", ["meadow", "fort"], {"power": 2}),
        "meadow": TerritoryDefinition("meadow", "Meadow", "land", ["camp"], {"power": 1}),
        "fort": TerritoryDefinition(
            "fort",
            "Fort",
            "land",
            ["camp"],
            {"power": 4},
            is_stronghold=True,
            stronghold_base_health=1,
        ),
        "hold": TerritoryDefinition("hold", "Hold", "land", ["wild"], {"power": 1}),
        "wild": TerritoryDefinition("wild", "Wild", "land", ["hold"], {"power": 3}),
        "river_a": TerritoryDefinition("river_a", "Anduin", "river", ["river_b", "shore"], {}),
        "river_b": TerritoryDefinition("river_b", "Lower Anduin", "river", ["river_a", "pasture"], {}),
        "shore": TerritoryDefinition("shore", "Shore", "land", ["river_a"], {"power": 2}),
        "pasture": TerritoryDefinition("pasture", "Pasture", "land", ["river_b"], {"power": 8}),
        "coast": TerritoryDefinition("coast", "Coast", "land", ["bay"], {"power": 1}),
        "bay": TerritoryDefinition("bay", "Bay", "sea", ["coast"], {}),
    }
    factions = {
        "isengard": FactionDefinition("isengard", "Isengard", "evil", "camp", "#222"),
        "rohan": FactionDefinition("rohan", "Rohan", "good", "fort", "#ddd"),
    }
    units = {
        "orc": UnitDefinition(
            "orc", "Orc", "isengard", "infantry", ["transportable"], 5, 5, 1, 2, {"power": 3}
        ),
        "rowboat": UnitDefinition(
            "rowboat", "Rowboat", "isengard", "river", ["river"], 1, 1, 1, 1, {"power": 4}, transport_capacity=2
        ),
        "skiff": UnitDefinition(
            "skiff", "Skiff", "isengard", "naval", ["naval"], 1, 1, 1, 1, {"power": 4}, transport_capacity=2
        ),
        "soldier": UnitDefinition(
            "soldier", "Soldier", "rohan", "infantry", [], 1, 1, 1, 1, {"power": 3}
        ),
        "ship": UnitDefinition(
            "ship", "Ship", "rohan", "naval", ["naval"], 4, 4, 1, 2, {"power": 6}
        ),
    }
    starting = {
        "turn_order": ["isengard", "rohan"],
        "territory_owners": {
            "camp": "isengard",
            "hold": "isengard",
            "coast": "isengard",
            "fort": "rohan",
            "shore": "rohan",
            "pasture": "rohan",
        },
        "starting_units": {
            "camp": [{"unit_id": "orc", "count": 3}],
            "hold": [{"unit_id": "orc", "count": 1}],
            "river_a": [{"unit_id": "rowboat", "count": 1}, {"unit_id": "orc", "count": 1}],
            "coast": [{"unit_id": "orc", "count": 1}],
            "bay": [{"unit_id": "skiff", "count": 1}, {"unit_id": "ship", "count": 1}],
            "fort": [{"unit_id": "soldier", "count": 1}],
            "shore": [{"unit_id": "soldier", "count": 1}],
            "pasture": [{"unit_id": "soldier", "count": 1}],
        },
    }
    return units, territories, factions, starting


def test_openings_take_empty_land_and_prefer_strongholds_without_illegal_moves():
    clear_opening_cache()
    units, territories, factions, starting = _small_setup()
    result = compute_opening_attacks(
        units, territories, factions, None, starting,
        n_trials=20, seed=8, mcts_iterations=40, mcts_seconds=2.0,
    )
    opening = result["factions"][0]
    assert opening["faction_id"] == "isengard"
    taken = {attack["to_territory"] for attack in opening["attacks"]}
    assert "fort" in taken
    assert "meadow" not in taken
    assert "wild" in taken
    wild = next(attack for attack in opening["attacks"] if attack["to_territory"] == "wild")
    assert wild["empty"] is True
    assert wild["from_territory"] == "hold"

    for faction in result["factions"]:
        pairs = set()
        for attack in faction["attacks"]:
            pair = (attack["from_territory"], attack["to_territory"])
            assert pair not in pairs
            pairs.add(pair)
            _assert_legal_attack(attack, units, territories)
            assert "rowboat" not in {stack["unit_id"] for stack in attack["attacker_stacks"]} or _is_water_zone(
                territories[attack["to_territory"]]
            )
            if attack["to_territory"] == "bay":
                assert all(stack["unit_id"] != "orc" for stack in attack["attacker_stacks"])
        assumed_pairs = {(attack["from_territory"], attack["to_territory"]) for attack in faction["assumed"]}
        assert assumed_pairs <= pairs

    nxt = result["factions"][1]["board"]
    assert nxt["owners"]["fort"] == "isengard"
    assert nxt["owners"]["wild"] == "isengard"
    assert nxt["owners"].get("meadow") != "isengard"


def test_opening_cache_does_not_search_again(monkeypatch):
    clear_opening_cache()
    calls = {"n": 0}
    from backend.ai.mcts.combat_move import decide_combat_move_mcts

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return decide_combat_move_mcts(*args, **kwargs)

    monkeypatch.setattr("backend.engine.openings.decide_combat_move_mcts", wrapped)
    units, territories, factions, starting = _small_setup()
    kwargs = {"n_trials": 5, "seed": 3, "mcts_iterations": 8, "mcts_seconds": 1.0}
    first = compute_opening_attacks(units, territories, factions, None, starting, **kwargs)
    assert calls["n"] > 0
    done = calls["n"]
    second = compute_opening_attacks(units, territories, factions, None, starting, **kwargs)
    assert calls["n"] == done
    assert second["factions"][0]["attacks"] == first["factions"][0]["attacks"]
    second["factions"][0]["attacks"].append({"mutated": True})
    third = compute_opening_attacks(units, territories, factions, None, starting, **kwargs)
    assert third["factions"][0]["attacks"] == first["factions"][0]["attacks"]


def test_starting_faction_openings_are_legal_moves():
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
        n_trials=10,
        seed=8,
        mcts_iterations=6,
        mcts_seconds=1.0,
    )

    order = result["factions"]
    assert order
    assert order[0]["faction_id"] == "isengard"
    ids = [row["faction_id"] for row in order]
    turn = [fid for fid in setup["starting_setup"]["turn_order"] if fid in set(ids)]
    assert ids == turn
    opening = order[0]
    assert opening["attacks"], "Isengard should have at least one opening move"
    seen = set()
    for attack in opening["attacks"]:
        pair = (attack["from_territory"], attack["to_territory"])
        assert pair not in seen
        seen.add(pair)
        assert attack["from_territory"] != attack["to_territory"]
        _assert_legal_attack(attack, units, territories)

    for row in order:
        attack_pairs = {(attack["from_territory"], attack["to_territory"]) for attack in row["attacks"]}
        for attack in row["attacks"]:
            _assert_legal_attack(attack, units, territories)
        for attack in row["assumed"]:
            assert (attack["from_territory"], attack["to_territory"]) in attack_pairs
    if len(order) > 1 and order[0]["assumed"]:
        nxt = order[1]["board"]
        for attack in order[0]["assumed"]:
            arrived = nxt["units"].get(attack["to_territory"]) or []
            assert arrived, f"{attack['to_territory']} should hold the units that took it"
            assert nxt["owners"].get(attack["to_territory"])
