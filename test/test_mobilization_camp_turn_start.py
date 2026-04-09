"""General camp mobilization requires the hex to have been owned at turn start (home special exempt — tested via validate rules)."""

from backend.engine.actions import mobilize_units
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.queries import validate_action
from backend.engine.utils import initialize_game_state
from backend.engine.state import UnitStack


def test_cannot_mobilize_generic_infantry_to_camp_hex_not_owned_at_turn_start():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_1.1"
    )
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="wotr_1.1"),
    )
    state.current_faction = "rohan"
    state.phase = "mobilization"
    # Still own dunharrow (with a camp) on the map, but turn-start snapshot only had edoras — simulates capturing dunharrow this turn.
    state.faction_territories_at_turn_start["rohan"] = ["edoras"]
    assert state.territories["dunharrow"].owner == "rohan"
    state.faction_purchased_units["rohan"] = [UnitStack(unit_id="rohirrim_soldier", count=1)]

    action = mobilize_units(
        "rohan",
        "dunharrow",
        [{"unit_id": "rohirrim_soldier", "count": 1}],
    )
    vr = validate_action(
        state,
        action,
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )
    assert not vr.valid
    assert "not owned at the start" in (vr.error or "").lower()
