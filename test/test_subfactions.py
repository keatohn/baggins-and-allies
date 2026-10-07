"""Subfactions are owners and unit factions commanded by their parent, not a turn of their own."""

from backend.balance.starting_strength import compute_starting_strength
from backend.engine.actions import end_phase, move_units, purchase_units
from backend.engine.definitions import (
    TerritoryDefinition,
    UnitDefinition,
    factions_from_json,
    faction_acts_as,
)
from backend.engine.queries import get_movable_units, get_unit_move_targets, validate_action
from backend.engine.reducer import _land_combat_unit_side, apply_action
from backend.engine.state import GameState, TerritoryState, Unit
from backend.engine.utils import initialize_game_state
from backend.setup_validation import validate_setup_documents


def _factions():
    return factions_from_json({
        "numenor": {
            "id": "numenor",
            "display_name": "Numenor",
            "alliance": "good",
            "capital": "numenor",
            "color": "#265399",
            "icon": "gondor.png",
            "subfactions": [
                {
                    "id": "faithful",
                    "display_name": "The Faithful",
                    "color": "#3d6bb3",
                },
                {
                    "id": "andunie",
                    "display_name": "Andunie",
                    "color": "#1d3f73",
                    "icon": "andunie.png",
                },
            ],
        },
        "mordor": {
            "id": "mordor",
            "display_name": "Mordor",
            "alliance": "evil",
            "capital": "barad_dur",
            "color": "#8b2500",
            "icon": "mordor.png",
        },
    })


def _unit(uid: str, faction: str) -> UnitDefinition:
    return UnitDefinition(
        id=uid,
        display_name=uid,
        faction=faction,
        archetype="infantry",
        tags=[],
        attack=1,
        defense=1,
        movement=1,
        health=1,
        cost={"power": 3},
    )


def _piece(iid: str, uid: str) -> Unit:
    return Unit(
        instance_id=iid,
        unit_id=uid,
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )


def _docs():
    factions = {
        "numenor": {
            "id": "numenor",
            "display_name": "Numenor",
            "alliance": "good",
            "capital": "numenor",
            "color": "#265399",
            "subfactions": [
                {"id": "faithful", "display_name": "The Faithful", "color": "#3d6bb3"},
            ],
        },
    }
    territories = {
        "numenor": {
            "id": "numenor",
            "display_name": "Numenor",
            "terrain_type": "land",
            "adjacent": ["andustar"],
            "produces": {"power": 5},
        },
        "andustar": {
            "id": "andustar",
            "display_name": "Andustar",
            "terrain_type": "land",
            "adjacent": ["numenor"],
            "produces": {"power": 2},
            "is_stronghold": True,
        },
    }
    units = {
        "faithful_infantry": {
            "id": "faithful_infantry",
            "display_name": "Faithful Infantry",
            "faction": "faithful",
            "archetype": "infantry",
            "attack": 1,
            "defense": 1,
            "movement": 1,
            "health": 1,
            "cost": {"power": 3},
        },
    }
    return factions, territories, units


def test_subfaction_lookup_inherits_alliance_and_icon():
    factions = _factions()
    assert list(factions.keys()) == ["numenor", "mordor"]
    assert "faithful" not in factions
    faithful = factions.get("faithful")
    assert faithful is not None
    assert faithful.alliance == "good"
    assert faithful.parent == "numenor"
    assert faithful.capital == ""
    assert faithful.icon == "gondor.png"
    assert factions["andunie"].icon == "andunie.png"
    assert faction_acts_as(factions, "faithful", "numenor")
    assert not faction_acts_as(factions, "faithful", "mordor")
    assert not faction_acts_as(factions, "numenor", "faithful")


def test_setup_validation_allows_subfaction_owners_and_rejects_turn_order():
    factions, territories, units = _docs()
    manifest = {"id": "wotla_1.0", "subfaction_rules": {"faithful": {"economy": "none"}}}
    errors = validate_setup_documents(
        manifest, units, territories, factions, {}, {},
        {
            "turn_order": ["numenor"],
            "territory_owners": {"andustar": "faithful", "numenor": "numenor"},
        },
        {},
    )
    assert errors == []

    bad_turn = validate_setup_documents(
        manifest, units, territories, factions, {}, {},
        {"turn_order": ["faithful"], "territory_owners": {}},
        {},
    )
    assert any("subfaction" in err and "faithful" in err for err in bad_turn)

    bad_rule = validate_setup_documents(
        {"id": "wotla_1.0", "subfaction_rules": {"missing": {}}},
        units, territories, factions, {}, {},
        {"turn_order": ["numenor"]},
        {},
    )
    assert any("unknown subfaction" in err for err in bad_rule)


def test_parent_moves_subfaction_units_and_cannot_buy_them():
    factions = _factions()
    territories = {
        "numenor": TerritoryDefinition("numenor", "Numenor", "land", ["andustar"], {"power": 5}),
        "andustar": TerritoryDefinition("andustar", "Andustar", "land", ["numenor"], {"power": 2}),
        "barad_dur": TerritoryDefinition("barad_dur", "Barad-dur", "land", [], {"power": 4}),
    }
    unit_defs = {
        "faithful_infantry": _unit("faithful_infantry", "faithful"),
        "numenor_infantry": _unit("numenor_infantry", "numenor"),
    }
    state = GameState(
        turn_number=1,
        current_faction="numenor",
        phase="non_combat_move",
        territories={
            "numenor": TerritoryState(owner="numenor", original_owner="numenor", units=[]),
            "andustar": TerritoryState(
                owner="faithful",
                original_owner="faithful",
                units=[_piece("faithful_infantry_001", "faithful_infantry")],
            ),
            "barad_dur": TerritoryState(owner="mordor", original_owner="mordor", units=[]),
        },
        faction_resources={"numenor": {"power": 10}, "mordor": {"power": 0}},
        turn_order=["numenor", "mordor"],
    )
    movable = get_movable_units(state, "numenor", unit_defs, factions)
    assert [u["instance_id"] for u in movable] == ["faithful_infantry_001"]

    move = move_units("numenor", "andustar", "numenor", ["faithful_infantry_001"])
    assert validate_action(state, move, unit_defs, territories, factions).valid

    state.phase = "purchase"
    state.territories["numenor"].owner = "numenor"
    buy = purchase_units("numenor", {"faithful_infantry": 1})
    bought = validate_action(state, buy, unit_defs, territories, factions)
    assert not bought.valid


def test_subfaction_units_attack_with_the_parent():
    factions = _factions()
    unit_defs = {"faithful_infantry": _unit("faithful_infantry", "faithful")}
    piece = _piece("faithful_infantry_001", "faithful_infantry")
    assert _land_combat_unit_side(piece, "numenor", "good", unit_defs, factions) == "attacker"
    assert _land_combat_unit_side(piece, "mordor", "evil", unit_defs, factions) == "defender"


def test_subfaction_land_does_not_pay_and_still_counts_as_controlled():
    factions = _factions()
    territories = {
        "numenor": TerritoryDefinition("numenor", "Numenor", "land", ["andustar"], {"power": 5}, is_stronghold=True),
        "andustar": TerritoryDefinition("andustar", "Andustar", "land", ["numenor"], {"power": 2}, is_stronghold=True),
        "barad_dur": TerritoryDefinition("barad_dur", "Barad-dur", "land", [], {"power": 4}),
    }
    state = initialize_game_state(
        factions,
        territories,
        unit_defs={"faithful_infantry": _unit("faithful_infantry", "faithful")},
        starting_setup={
            "turn_order": ["numenor", "faithful", "mordor"],
            "territory_owners": {"numenor": "numenor", "andustar": "faithful", "barad_dur": "mordor"},
            "starting_units": {},
        },
    )
    assert state.turn_order == ["numenor", "mordor"]
    assert state.faction_resources["numenor"]["power"] == 5
    assert "faithful" not in state.faction_resources

    report = compute_starting_strength({
        "manifest": {"id": "t", "victory_criteria": {"strongholds": {"good": 2, "evil": 2}}},
        "factions": {
            "numenor": {
                "id": "numenor",
                "display_name": "Numenor",
                "alliance": "good",
                "capital": "numenor",
                "subfactions": [
                    {"id": "faithful", "display_name": "The Faithful", "color": "#3d6bb3"},
                ],
            },
            "mordor": {
                "id": "mordor",
                "display_name": "Mordor",
                "alliance": "evil",
                "capital": "barad_dur",
            },
        },
        "territories": {
            "numenor": {"id": "numenor", "display_name": "Numenor", "terrain_type": "land", "adjacent": ["andustar"], "produces": {"power": 5}, "is_stronghold": True},
            "andustar": {"id": "andustar", "display_name": "Andustar", "terrain_type": "land", "adjacent": ["numenor"], "produces": {"power": 2}, "is_stronghold": True},
            "barad_dur": {"id": "barad_dur", "display_name": "Barad-dur", "terrain_type": "land", "adjacent": [], "produces": {"power": 4}, "is_stronghold": True},
        },
        "units": {
            "faithful_infantry": {
                "id": "faithful_infantry",
                "display_name": "Faithful Infantry",
                "faction": "faithful",
                "archetype": "infantry",
                "cost": {"power": 3},
                "movement": 1,
            },
        },
        "starting_setup": {
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {"numenor": "numenor", "andustar": "faithful", "barad_dur": "mordor"},
            "starting_units": {"andustar": [{"unit_id": "faithful_infantry", "count": 2}]},
        },
    })
    rows = {row["id"]: row for row in report["factions"]}
    assert "faithful" not in rows
    assert rows["numenor"]["territories"] == 2
    assert rows["numenor"]["strongholds"] == 2
    assert rows["numenor"]["power_production"] == 5
    assert rows["numenor"]["units"] == 2


def _board():
    territories = {
        "numenor": TerritoryDefinition("numenor", "Numenor", "land", ["andustar", "bay"], {"power": 5}),
        "andustar": TerritoryDefinition("andustar", "Andustar", "land", ["numenor", "bay", "romenna"], {"power": 2}),
        "romenna": TerritoryDefinition("romenna", "Romenna", "land", ["andustar"], {"power": 2}),
        "bay": TerritoryDefinition("bay", "Bay", "sea", ["numenor", "andustar"], {}),
        "barad_dur": TerritoryDefinition("barad_dur", "Barad-dur", "land", [], {"power": 4}),
    }
    return territories


def test_pool_income_grant_and_must_place_before_ending():
    from backend.engine.actions import end_phase, mobilize_units
    from backend.engine.reducer import apply_action

    factions = _factions()
    territories = _board()
    unit_defs = {"faithful_infantry": _unit("faithful_infantry", "faithful")}
    rules = {
        "faithful": {
            "economy": "pool",
            "recruitment": {"mode": "per_territory", "unit_id": "faithful_infantry", "count": 2},
            "mobilization": "any_home",
        }
    }
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "faithful",
                "bay": None,
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules=rules,
    )
    assert state.faction_resources["numenor"]["power"] == 9

    state.phase = "non_combat_move"
    state, _ = apply_action(state, end_phase("numenor"), unit_defs, territories, factions)
    assert state.phase == "mobilization"
    granted = {s.unit_id: s.count for s in state.faction_purchased_units["numenor"]}
    assert granted == {"faithful_infantry": 1}

    blocked = validate_action(state, end_phase("numenor"), unit_defs, territories, factions)
    assert not blocked.valid

    place = mobilize_units("numenor", "andustar", [{"unit_id": "faithful_infantry", "count": 1}])
    assert validate_action(state, place, unit_defs, territories, factions).valid
    sea = mobilize_units("numenor", "bay", [{"unit_id": "faithful_infantry", "count": 1}])
    assert not validate_action(state, sea, unit_defs, territories, factions).valid
    parent_land = mobilize_units("numenor", "numenor", [{"unit_id": "faithful_infantry", "count": 1}])
    assert not validate_action(state, parent_land, unit_defs, territories, factions).valid

    state, _ = apply_action(state, place, unit_defs, territories, factions)
    assert state.faction_purchased_units["numenor"] == []
    assert validate_action(state, end_phase("numenor"), unit_defs, territories, factions).valid


def _ring_state(economy: str):
    from backend.engine.rings import spawn_rings

    factions = _factions()
    territories = _board()
    hero = UnitDefinition(
        id="elendil",
        display_name="Elendil",
        faction="faithful",
        archetype="infantry",
        tags=[],
        attack=4,
        defense=4,
        movement=1,
        health=2,
        cost={"power": 12},
        hero_id="elendil",
    )
    unit_defs = {"elendil": hero}
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "faithful",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules={"faithful": {"economy": economy}},
        special_rules=[],
    )
    state.rings_of_power = True
    state.rings = spawn_rings([{
        "type": "rings_of_power",
        "rings": [
            {"id": "land_ring", "name": "Land Ring", "territory_id": "romenna", "power": 3},
            {"id": "borne", "name": "Borne", "territory_id": "andustar", "power": 5, "bearer_hero_id": "elendil"},
        ],
    }])
    state.territories["andustar"].units.append(_piece("faithful_elendil_001", "elendil"))
    return state, unit_defs, factions


def test_subfaction_ring_power_follows_its_economy_rule():
    from backend.engine.rings import power_for_faction

    pooled, unit_defs, factions = _ring_state("pool")
    assert power_for_faction(pooled, "numenor", unit_defs, factions) == 8

    pooled.territories["numenor"].owner = "mordor"
    assert power_for_faction(pooled, "numenor", unit_defs, factions) == 0

    kept, unit_defs, factions = _ring_state("none")
    assert power_for_faction(kept, "numenor", unit_defs, factions) == 0


def test_stats_list_each_subfaction_share_of_its_parent():
    from backend.engine.queries import get_faction_stats

    territories = _board()
    pooled, unit_defs, factions = _ring_state("pool")
    stats = get_faction_stats(pooled, territories, factions, unit_defs)
    parent = stats["factions"]["numenor"]
    sub = stats["subfactions"]["faithful"]
    assert sub["economy"] == "pool"
    land = sum(territories[t].produces.get("power", 0) for t in ("andustar", "romenna"))
    assert (sub["territories"], sub["units"], sub["unit_power"]) == (2, 1, 12)
    assert sub["power_per_turn"] == land + 8
    assert parent["territories"] == 3 and parent["units"] == 1
    assert parent["power_per_turn"] == territories["numenor"].produces.get("power", 0) + sub["power_per_turn"]

    kept, unit_defs, factions = _ring_state("none")
    stats = get_faction_stats(kept, territories, factions, unit_defs)
    kept_sub = stats["subfactions"]["faithful"]
    assert kept_sub["economy"] == "none"
    assert (kept_sub["territories"], kept_sub["units"], kept_sub["power_per_turn"]) == (2, 1, 0)
    assert stats["subfactions"]["andunie"]["territories"] == 0
    assert stats["factions"]["numenor"]["territories"] == 3


def test_one_territory_rounds_the_grant_down_and_lost_capital_stops_it():
    from backend.engine.actions import end_phase, end_turn
    from backend.engine.reducer import apply_action

    factions = _factions()
    territories = _board()
    unit_defs = {"faithful_infantry": _unit("faithful_infantry", "faithful")}
    rules = {
        "faithful": {
            "economy": "pool",
            "recruitment": {"mode": "per_territory", "unit_id": "faithful_infantry", "count": 2},
            "mobilization": "any_home",
        }
    }
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "numenor",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules=rules,
    )
    state.phase = "non_combat_move"
    state, _ = apply_action(state, end_phase("numenor"), unit_defs, territories, factions)
    assert state.faction_purchased_units["numenor"] == []

    fallen = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "mordor",
                "andustar": "faithful",
                "romenna": "faithful",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules=rules,
    )
    assert fallen.faction_resources["numenor"].get("power", 0) == 0
    before = fallen.faction_resources["numenor"].get("power", 0)
    fallen.phase = "non_combat_move"
    fallen, _ = apply_action(fallen, end_phase("numenor"), unit_defs, territories, factions)
    assert fallen.faction_purchased_units["numenor"] == []
    fallen, _ = apply_action(fallen, end_turn("numenor"), unit_defs, territories, factions)
    assert fallen.faction_resources["numenor"].get("power", 0) == before


def test_any_home_sea_does_not_need_a_port_and_camps_include_the_parent():
    from backend.engine.actions import mobilize_units
    from backend.engine.definitions import CampDefinition
    from backend.engine.state import UnitStack

    factions = _factions()
    territories = _board()
    unit_defs = {
        "faithful_infantry": _unit("faithful_infantry", "faithful"),
        "faithful_ship": _unit("faithful_ship", "faithful"),
    }
    unit_defs["faithful_ship"].archetype = "naval"
    rules = {"faithful": {"mobilization": "any_home"}}
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "faithful",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules=rules,
    )
    state.phase = "mobilization"
    state.faction_purchased_units["numenor"] = [UnitStack(unit_id="faithful_ship", count=1)]
    sea = mobilize_units("numenor", "bay", [{"unit_id": "faithful_ship", "count": 1}])
    assert validate_action(state, sea, unit_defs, territories, factions).valid

    camps = {"camp_numenor": CampDefinition("camp_numenor", "numenor")}
    camp_rules = {"faithful": {"mobilization": "camps"}}
    camp_state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        camp_defs=camps,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "faithful",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
        subfaction_rules=camp_rules,
    )
    camp_state.phase = "mobilization"
    camp_state.faction_purchased_units["numenor"] = [UnitStack(unit_id="faithful_infantry", count=1)]
    on_parent = mobilize_units("numenor", "numenor", [{"unit_id": "faithful_infantry", "count": 1}])
    assert validate_action(
        camp_state, on_parent, unit_defs, territories, factions, camps,
    ).valid
    on_home = mobilize_units("numenor", "andustar", [{"unit_id": "faithful_infantry", "count": 1}])
    assert not validate_action(
        camp_state, on_home, unit_defs, territories, factions, camps,
    ).valid


def test_parent_home_unit_deploys_to_its_home_on_subfaction_land():
    from backend.engine.actions import mobilize_units
    from backend.engine.queries import get_mobilization_capacity, get_mobilization_territories
    from backend.engine.state import UnitStack

    factions = _factions()
    territories = _board()
    guard = _unit("guard", "numenor")
    guard.specials = ["home"]
    guard.home_territory_ids = ["andustar"]
    unit_defs = {"guard": guard, "foot": _unit("foot", "numenor")}
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {
                "numenor": "numenor",
                "andustar": "faithful",
                "romenna": "faithful",
                "barad_dur": "mordor",
            },
            "starting_units": {},
        },
    )
    state.phase = "mobilization"
    state.faction_purchased_units["numenor"] = [
        UnitStack(unit_id="guard", count=2),
        UnitStack(unit_id="foot", count=1),
    ]

    assert "andustar" in get_mobilization_territories(state, "numenor", territories, {}, {}, unit_defs, factions)
    rows = get_mobilization_capacity(state, "numenor", territories, {}, {}, unit_defs, factions)["territories"]
    assert {"territory_id": "andustar", "power": 0, "home_unit_capacity": {"guard": 1}} in rows
    assert "romenna" not in {row["territory_id"] for row in rows}

    one = mobilize_units("numenor", "andustar", [{"unit_id": "guard", "count": 1}])
    assert validate_action(state, one, unit_defs, territories, factions).valid
    two = mobilize_units("numenor", "andustar", [{"unit_id": "guard", "count": 2}])
    assert not validate_action(state, two, unit_defs, territories, factions).valid
    foot = mobilize_units("numenor", "andustar", [{"unit_id": "foot", "count": 1}])
    assert not validate_action(state, foot, unit_defs, territories, factions).valid

    state, _ = apply_action(state, one, unit_defs, territories, factions)
    assert state.pending_mobilizations[-1].destination == "andustar"
    again = mobilize_units("numenor", "andustar", [{"unit_id": "guard", "count": 1}])
    assert not validate_action(state, again, unit_defs, territories, factions).valid


def test_parent_can_buy_subfaction_units_when_the_rule_allows_it():
    factions = _factions()
    territories = _board()
    unit_defs = {"faithful_infantry": _unit("faithful_infantry", "faithful")}
    state = initialize_game_state(
        factions,
        territories,
        unit_defs=unit_defs,
        starting_setup={
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {"numenor": "numenor", "andustar": "faithful", "barad_dur": "mordor"},
            "starting_units": {},
        },
        subfaction_rules={"faithful": {"purchasable_by_parent": True, "mobilization": "any_home"}},
    )
    state.phase = "purchase"
    buy = purchase_units("numenor", {"faithful_infantry": 1})
    assert validate_action(state, buy, unit_defs, territories, factions).valid


def test_subfaction_rule_fields_are_checked():
    units = {"faithful_infantry": {"id": "faithful_infantry", "faction": "faithful"}}
    territories = {"numenor": {"id": "numenor", "adjacent": []}}
    factions = {
        "numenor": {
            "id": "numenor",
            "display_name": "Numenor",
            "alliance": "good",
            "capital": "numenor",
            "subfactions": [{"id": "faithful", "display_name": "The Faithful", "color": "#3d6bb3"}],
        }
    }
    errors = validate_setup_documents(
        {"id": "wotla_1.0", "subfaction_rules": {"faithful": {"economy": "fold"}}},
        units, territories, factions, {}, {},
        {"turn_order": ["numenor"]},
        {},
    )
    assert any("economy" in err for err in errors)


def test_balance_counts_pooled_subfaction_power():
    report = compute_starting_strength({
        "manifest": {
            "id": "t",
            "victory_criteria": {"strongholds": {"good": 2, "evil": 2}},
            "subfaction_rules": {"faithful": {"economy": "pool"}},
        },
        "factions": {
            "numenor": {
                "id": "numenor",
                "display_name": "Numenor",
                "alliance": "good",
                "capital": "numenor",
                "subfactions": [
                    {"id": "faithful", "display_name": "The Faithful", "color": "#3d6bb3"},
                ],
            },
            "mordor": {
                "id": "mordor",
                "display_name": "Mordor",
                "alliance": "evil",
                "capital": "barad_dur",
            },
        },
        "territories": {
            "numenor": {"id": "numenor", "display_name": "Numenor", "terrain_type": "land", "adjacent": ["andustar"], "produces": {"power": 5}, "is_stronghold": True},
            "andustar": {"id": "andustar", "display_name": "Andustar", "terrain_type": "land", "adjacent": ["numenor"], "produces": {"power": 2}, "is_stronghold": True},
            "barad_dur": {"id": "barad_dur", "display_name": "Barad-dur", "terrain_type": "land", "adjacent": [], "produces": {"power": 4}, "is_stronghold": True},
        },
        "units": {},
        "starting_setup": {
            "turn_order": ["numenor", "mordor"],
            "territory_owners": {"numenor": "numenor", "andustar": "faithful", "barad_dur": "mordor"},
            "starting_units": {},
        },
    })
    rows = {row["id"]: row for row in report["factions"]}
    assert rows["numenor"]["power_production"] == 7


def _capture_board(rules: dict | None):
    factions = _factions()
    territories = {
        "numenor": TerritoryDefinition("numenor", "Numenor", "land", ["andustar"], {"power": 5}),
        "andustar": TerritoryDefinition(
            "andustar", "Andustar", "land", ["numenor", "osgiliath", "pelargir"], {"power": 2},
        ),
        "osgiliath": TerritoryDefinition("osgiliath", "Osgiliath", "land", ["andustar"], {"power": 1}),
        "pelargir": TerritoryDefinition("pelargir", "Pelargir", "land", ["andustar"], {"power": 1}),
        "barad_dur": TerritoryDefinition("barad_dur", "Barad-dur", "land", [], {"power": 4}),
    }
    unit_defs = {
        "faithful_infantry": _unit("faithful_infantry", "faithful"),
        "numenor_infantry": _unit("numenor_infantry", "numenor"),
    }
    state = GameState(
        turn_number=1,
        current_faction="numenor",
        phase="combat_move",
        territories={
            "numenor": TerritoryState(owner="numenor", original_owner="numenor", units=[]),
            "andustar": TerritoryState(
                owner="faithful",
                original_owner="faithful",
                units=[
                    _piece("faithful_infantry_001", "faithful_infantry"),
                    _piece("numenor_infantry_001", "numenor_infantry"),
                ],
            ),
            "osgiliath": TerritoryState(owner="mordor", original_owner="mordor", units=[]),
            "pelargir": TerritoryState(owner="mordor", original_owner="faithful", units=[]),
            "barad_dur": TerritoryState(owner="mordor", original_owner="mordor", units=[]),
        },
        faction_resources={"numenor": {"power": 10}, "mordor": {"power": 0}},
        turn_order=["numenor", "mordor"],
        subfaction_rules=rules or {},
    )
    return state, unit_defs, territories, factions


def _take(state, unit_defs, territories, factions, destination: str, instance_ids: list[str]):
    move = move_units("numenor", "andustar", destination, instance_ids)
    state, _events = apply_action(state, move, unit_defs, territories, factions)
    state, _events = apply_action(state, end_phase("numenor"), unit_defs, territories, factions)
    return state


def test_default_capture_credits_the_parent_and_liberates_the_subfaction():
    state, unit_defs, territories, factions = _capture_board(None)
    taken = _take(state, unit_defs, territories, factions, "osgiliath", ["faithful_infantry_001"])
    assert taken.pending_captures["osgiliath"] == "numenor"
    taken, _events = apply_action(taken, end_phase("numenor"), unit_defs, territories, factions)
    assert taken.territories["osgiliath"].owner == "numenor"

    state, unit_defs, territories, factions = _capture_board(None)
    liberated = _take(state, unit_defs, territories, factions, "pelargir", ["faithful_infantry_001"])
    assert liberated.pending_captures["pelargir"] == "numenor"
    liberated, _events = apply_action(liberated, end_phase("numenor"), unit_defs, territories, factions)
    assert liberated.territories["pelargir"].owner == "faithful"


def test_unit_faction_capture_stays_with_a_pure_subfaction_stack():
    rules = {"faithful": {"capture": "unit_faction"}}
    state, unit_defs, territories, factions = _capture_board(rules)
    taken = _take(state, unit_defs, territories, factions, "osgiliath", ["faithful_infantry_001"])
    assert taken.pending_captures["osgiliath"] == "faithful"
    taken, _events = apply_action(taken, end_phase("numenor"), unit_defs, territories, factions)
    assert taken.territories["osgiliath"].owner == "faithful"

    state, unit_defs, territories, factions = _capture_board(rules)
    mixed = _take(
        state, unit_defs, territories, factions, "osgiliath",
        ["faithful_infantry_001", "numenor_infantry_001"],
    )
    assert mixed.pending_captures["osgiliath"] == "numenor"


def test_home_only_limits_destinations_to_original_territories():
    rules = {"faithful": {"movement": "home_only"}}
    state, unit_defs, territories, factions = _capture_board(rules)
    targets, _routes = get_unit_move_targets(
        state, "faithful_infantry_001", unit_defs, territories, factions,
    )
    assert set(targets) == {"pelargir"}

    parent_targets, _routes = get_unit_move_targets(
        state, "numenor_infantry_001", unit_defs, territories, factions,
    )
    assert "osgiliath" in parent_targets

    blocked = move_units("numenor", "andustar", "osgiliath", ["faithful_infantry_001"])
    assert not validate_action(state, blocked, unit_defs, territories, factions).valid
    allowed = move_units("numenor", "andustar", "pelargir", ["faithful_infantry_001"])
    assert validate_action(state, allowed, unit_defs, territories, factions).valid

    state.phase = "non_combat_move"
    home_friendly, _routes = get_unit_move_targets(
        state, "faithful_infantry_001", unit_defs, territories, factions,
    )
    assert "numenor" not in home_friendly
    parent_friendly, _routes = get_unit_move_targets(
        state, "numenor_infantry_001", unit_defs, territories, factions,
    )
    assert "numenor" in parent_friendly
    onto_parent = move_units("numenor", "andustar", "numenor", ["faithful_infantry_001"])
    assert not validate_action(state, onto_parent, unit_defs, territories, factions).valid

    state.subfaction_rules = {}
    state.phase = "combat_move"
    open_targets, _routes = get_unit_move_targets(
        state, "faithful_infantry_001", unit_defs, territories, factions,
    )
    assert "osgiliath" in open_targets
    assert "pelargir" in open_targets
