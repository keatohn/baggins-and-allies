"""Fading-territory special rule: power drops once per game turn, down to a floor."""

from backend.engine.actions import end_turn
from backend.engine.definitions import load_static_definitions
from backend.engine.reducer import apply_action
from backend.engine.special_rules import effective_territory_power, parse_special_rules
from backend.engine.utils import initialize_game_state
from backend.setup_validation import validate_setup_documents


def _rules(territory_id: str, fade: int, floor: int) -> list[dict]:
    return [{
        "type": "fading_territory",
        "territories": [{
            "territory_id": territory_id,
            "fade_per_turn": fade,
            "floor": floor,
        }],
    }]


def test_power_fades_each_turn_until_floor():
    rules = _rules("moria", 2, 1)
    assert effective_territory_power(8, 1, rules, "moria") == 8
    assert effective_territory_power(8, 2, rules, "moria") == 6
    assert effective_territory_power(8, 4, rules, "moria") == 2
    assert effective_territory_power(8, 5, rules, "moria") == 1
    assert effective_territory_power(8, 9, rules, "moria") == 1
    assert effective_territory_power(8, 3, rules, "other") == 8


def test_floor_does_not_raise_printed_power():
    rules = _rules("x", 1, 5)
    assert effective_territory_power(3, 1, rules, "x") == 3
    assert effective_territory_power(3, 4, rules, "x") == 3


def test_parse_drops_bad_rows_and_keeps_other_rules():
    parsed = parse_special_rules([
        {"type": "fading_territory", "territories": [
            {"territory_id": "a", "fade_per_turn": 1, "floor": 0},
            {"territory_id": "a", "fade_per_turn": 3, "floor": 0},
            {"territory_id": "", "fade_per_turn": 1, "floor": 0},
            "nope",
        ]},
        {"type": "future_rule", "note": "keep"},
        "skip",
    ])
    assert parsed[0]["territories"] == [{"territory_id": "a", "fade_per_turn": 1, "floor": 0}]
    assert parsed[1]["type"] == "future_rule"


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
            special_rules=_rules(capital, 1, 0),
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


def test_manifest_validation_accepts_fading_rule_and_message():
    unit_defs, territory_defs, faction_defs, camp_defs, ports = load_static_definitions(setup_id="wotr_1.1")
    from backend.engine.definitions import load_starting_setup, load_specials
    starting = load_starting_setup(setup_id="wotr_1.1")
    specials, order = load_specials()
    capital = next(iter(faction_defs.values())).capital
    manifest = {
        "id": "wotr_1.1",
        "is_active": False,
        "starting_message": "The beacons are lit.",
        "special_rules": _rules(capital, 1, 0),
    }
    errors = validate_setup_documents(
        manifest,
        {u: {} for u in unit_defs},
        {t: {} for t in territory_defs},
        {f: {"id": f, "capital": faction_defs[f].capital} for f in faction_defs},
        {c: {"territory_id": camp_defs[c].territory_id} for c in camp_defs},
        {p: {"territory_id": ports[p].territory_id} for p in ports},
        starting,
        {"order": order, **specials},
    )
    assert not any("special_rules" in e or "starting_message" in e for e in errors)

    bad = dict(manifest)
    bad["special_rules"] = _rules("not_a_territory", -1, 0)
    bad_errors = validate_setup_documents(
        bad,
        {u: {} for u in unit_defs},
        {t: {} for t in territory_defs},
        {f: {"id": f, "capital": faction_defs[f].capital} for f in faction_defs},
        {c: {"territory_id": camp_defs[c].territory_id} for c in camp_defs},
        {p: {"territory_id": ports[p].territory_id} for p in ports},
        starting,
        {"order": order, **specials},
    )
    assert any("territory_id" in e for e in bad_errors)
    assert any("fade_per_turn" in e for e in bad_errors)
