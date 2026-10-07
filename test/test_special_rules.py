"""Evolving-territory special rule: power steps until it reaches stop_at."""

from backend.engine.actions import end_turn
from backend.engine.definitions import load_static_definitions
from backend.engine.reducer import apply_action
from backend.engine.definitions import scenario_menu_entry
from backend.engine.special_rules import (
    effective_territory_power,
    optional_rule_menu,
    parse_special_rules,
    select_special_rules,
)
from backend.engine.utils import initialize_game_state
from backend.setup_validation import validate_setup_documents


def _rules(territory_id: str, step: int, stop_at: int) -> list[dict]:
    return [{
        "type": "evolving_territory",
        "territories": [{
            "territory_id": territory_id,
            "step": step,
            "stop_at": stop_at,
        }],
    }]


def test_power_falls_each_turn_until_floor():
    rules = _rules("moria", -2, 1)
    assert effective_territory_power(8, 1, rules, "moria") == 8
    assert effective_territory_power(8, 2, rules, "moria") == 6
    assert effective_territory_power(8, 4, rules, "moria") == 2
    assert effective_territory_power(8, 5, rules, "moria") == 1
    assert effective_territory_power(8, 9, rules, "moria") == 1
    assert effective_territory_power(8, 3, rules, "other") == 8


def test_power_rises_each_turn_until_ceiling():
    rules = _rules("osgiliath", 2, 9)
    assert effective_territory_power(3, 1, rules, "osgiliath") == 3
    assert effective_territory_power(3, 2, rules, "osgiliath") == 5
    assert effective_territory_power(3, 4, rules, "osgiliath") == 9
    assert effective_territory_power(3, 6, rules, "osgiliath") == 9


def test_bound_on_the_wrong_side_does_not_move_printed_power():
    falling = _rules("x", -1, 5)
    assert effective_territory_power(3, 1, falling, "x") == 3
    assert effective_territory_power(3, 4, falling, "x") == 3
    rising = _rules("x", 1, 2)
    assert effective_territory_power(5, 1, rising, "x") == 5
    assert effective_territory_power(5, 4, rising, "x") == 5


def test_parse_drops_bad_rows_and_keeps_other_rules():
    parsed = parse_special_rules([
        {"type": "evolving_territory", "territories": [
            {"territory_id": "a", "step": -1, "stop_at": 0},
            {"territory_id": "a", "step": 3, "stop_at": 9},
            {"territory_id": "", "step": 1, "stop_at": 4},
            {"territory_id": "b", "step": 0, "stop_at": 1},
            "nope",
        ]},
        {"type": "fading_territory", "territories": [
            {"territory_id": "c", "fade_per_turn": 2, "floor": 1},
        ]},
        {"type": "future_rule", "note": "keep"},
        "skip",
    ])
    assert parsed[0]["type"] == "evolving_territory"
    assert parsed[0]["territories"] == [{"territory_id": "a", "step": -1, "stop_at": 0}]
    assert parsed[1]["territories"] == [{"territory_id": "c", "step": -2, "stop_at": 1}]
    assert parsed[2]["type"] == "future_rule"


def test_end_turn_income_uses_faded_power():
    unit_defs, territory_defs, faction_defs, camp_defs, _ports = load_static_definitions(setup_id="wotr_1.1")
    faction_id = ""
    capital = ""
    base = 0
    for fid, fdef in faction_defs.items():
        cap = getattr(fdef, "capital", None)
        if not isinstance(cap, str) or cap not in territory_defs:
            continue
        power = int(territory_defs[cap].produces.get("power", 0) or 0)
        if power > base:
            faction_id, capital, base = fid, cap, power
    assert base >= 2

    def income_for(turn_number: int) -> int:
        state = initialize_game_state(
            faction_defs,
            territory_defs,
            unit_defs,
            camp_defs=camp_defs,
            special_rules=_rules(capital, -1, 0),
        )
        state.current_faction = faction_id
        state.phase = "mobilization"
        state.turn_number = turn_number
        before = state.faction_resources.get(faction_id, {}).get("power", 0)
        state, _events = apply_action(
            state,
            end_turn(faction_id),
            unit_defs,
            territory_defs,
            faction_defs,
            camp_defs,
        )
        after = state.faction_resources.get(faction_id, {}).get("power", 0)
        return after - before

    # Default init owns only capitals, so this faction's income is the capital's power.
    assert income_for(1) == base
    assert income_for(2) == base - 1
    assert income_for(base + 3) == 0


def test_manifest_validation_accepts_evolving_rule_and_message():
    unit_defs, territory_defs, faction_defs, camp_defs, ports = load_static_definitions(setup_id="wotr_1.1")
    from backend.engine.definitions import load_starting_setup, load_specials
    starting = load_starting_setup(setup_id="wotr_1.1")
    specials, order = load_specials()
    capital = next(iter(faction_defs.values())).capital
    printed = int(territory_defs[capital].produces.get("power", 0) or 0)
    assert printed >= 1
    territories = {t: {} for t in territory_defs}
    territories[capital] = {"produces": {"power": printed}}
    manifest = {
        "id": "wotr_1.1",
        "is_active": False,
        "starting_message": "The beacons are lit.",
        "special_rules": _rules(capital, -1, 0),
    }
    errors = validate_setup_documents(
        manifest,
        {u: {} for u in unit_defs},
        territories,
        {f: {"id": f, "capital": faction_defs[f].capital} for f in faction_defs},
        {c: {"territory_id": camp_defs[c].territory_id} for c in camp_defs},
        {p: {"territory_id": ports[p].territory_id} for p in ports},
        starting,
        {"order": order, **specials},
    )
    assert not any("special_rules" in e or "starting_message" in e for e in errors)

    def errors_for(rules: list[dict]) -> list[str]:
        bad = dict(manifest)
        bad["special_rules"] = rules
        return validate_setup_documents(
            bad,
            {u: {} for u in unit_defs},
            territories,
            {f: {"id": f, "capital": faction_defs[f].capital} for f in faction_defs},
            {c: {"territory_id": camp_defs[c].territory_id} for c in camp_defs},
            {p: {"territory_id": ports[p].territory_id} for p in ports},
            starting,
            {"order": order, **specials},
        )

    bad_errors = errors_for(_rules("not_a_territory", -1, 0))
    assert any("territory_id" in e for e in bad_errors)
    assert any("stop_at must be below" in e for e in errors_for(_rules(capital, -1, printed)))
    assert any("stop_at must be above" in e for e in errors_for(_rules(capital, 1, printed)))
    assert any("step must be a non-zero integer" in e for e in errors_for(_rules(capital, 0, 0)))
    unnamed = _rules(capital, -1, 0)
    unnamed[0]["is_optional"] = True
    assert any("name must be a non-empty string" in e for e in errors_for(unnamed))
    unnamed[0]["is_optional"] = "yes"
    assert any("is_optional must be a boolean" in e for e in errors_for(unnamed))


def test_optional_rules_can_be_turned_off_and_others_always_apply():
    rules = [
        {
            "type": "rings_of_power",
            "name": "Rings of Power",
            "is_optional": True,
            "rings": [{"id": "narya", "name": "Narya", "territory_id": "mithlond", "power": 1}],
        },
        {
            "type": "evolving_territory",
            "territories": [{"territory_id": "moria", "step": -1, "stop_at": 0}],
        },
    ]
    kept, rings_on = select_special_rules(rules, {"rings_of_power": False})
    assert rings_on is False
    assert [rule["type"] for rule in kept] == ["evolving_territory"]

    kept_on, rings_on = select_special_rules(rules, {})
    assert rings_on is True
    assert [rule["type"] for rule in kept_on] == ["rings_of_power", "evolving_territory"]

    forced, rings_on = select_special_rules(
        [{"type": "rings_of_power", "rings": [{"id": "narya", "name": "Narya", "territory_id": "mithlond", "power": 1}]}],
        {"rings_of_power": False},
    )
    assert rings_on is True
    assert forced[0]["type"] == "rings_of_power"

    legacy, rings_on = select_special_rules(rules, None, legacy_rings=False)
    assert rings_on is False
    assert [rule["type"] for rule in legacy] == ["evolving_territory"]


def test_scenario_card_lists_only_optional_rules():
    optional = scenario_menu_entry(
        {
            "id": "x",
            "display_name": "X",
            "map_asset": "m",
            "special_rules": [
                {"type": "rings_of_power", "name": "Rings of Power", "is_optional": True, "rings": []},
                {
                    "type": "evolving_territory",
                    "territories": [{"territory_id": "a", "step": 1, "stop_at": 4}],
                },
            ],
        },
        "x",
    )
    assert optional["optional_rules"] == [{"type": "rings_of_power", "name": "Rings of Power"}]
    assert "rings_of_power" not in optional

    unnamed = optional_rule_menu([{
        "type": "evolving_territory",
        "is_optional": True,
        "territories": [{"territory_id": "a", "step": 1, "stop_at": 4}],
    }])
    assert unnamed == [{"type": "evolving_territory", "name": "Evolving Territory"}]

    mandatory = scenario_menu_entry(
        {
            "id": "y",
            "display_name": "Y",
            "map_asset": "m",
            "special_rules": [{"type": "rings_of_power", "rings": []}],
        },
        "y",
    )
    assert mandatory["rings_of_power"] is True
    assert "optional_rules" not in mandatory
