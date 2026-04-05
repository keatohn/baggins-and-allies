"""
Camp forfeit: if a purchased camp has no legal placement left, mobilization can still end.

Separately: mobilizing requires owning your capital (matches reducer). Stale purchased units
from before the capital fell cannot be deployed — AI must end_phase instead of mobilize_units.
"""

from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.actions import end_phase, mobilize_units
from backend.engine.reducer import apply_action
from backend.engine.state import UnitStack
from backend.engine.utils import initialize_game_state
from backend.engine.queries import get_mobilization_capacity, validate_action


def test_mobilization_end_phase_forfeits_camp_when_no_valid_placement():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="1.0"
    )
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="1.0"),
    )
    state.current_faction = "rohan"
    state.phase = "mobilization"
    state.pending_camps = [
        {"territory_options": ["edoras"], "placed_territory_id": None},
    ]
    # All listed options are no longer ours — same situation as losing the capital after purchase
    state.territories["edoras"].owner = "mordor"

    before = int(state.faction_resources.get("rohan", {}).get("power", 0) or 0)
    camp_cost = int(getattr(state, "camp_cost", 10) or 10)

    vr = validate_action(
        state,
        end_phase("rohan"),
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )
    assert vr.valid, vr.error

    state, events = apply_action(
        state,
        end_phase("rohan"),
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )

    after = int(state.faction_resources.get("rohan", {}).get("power", 0) or 0)
    assert after == before + camp_cost
    assert any(e.type == "resources_changed" for e in events)
    assert any(
        getattr(e, "payload", {}).get("reason") == "camp_forfeited_no_valid_territory"
        for e in events
    )
    # Turn advances: pending_camps cleared for the next faction
    assert state.pending_camps == []


def test_mobilize_rejected_when_capital_lost_even_with_purchased_units_in_pool():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="1.0"
    )
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="1.0"),
    )
    state.current_faction = "rohan"
    state.phase = "mobilization"
    state.territories["edoras"].owner = "mordor"
    # Bought earlier in the turn while capital was safe; capital fell before mobilization
    state.faction_purchased_units["rohan"] = [
        UnitStack(unit_id="rohirrim_soldier", count=1),
    ]
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
    assert "capital" in (vr.error or "").lower()


def test_mobilization_home_capacity_excludes_other_factions_home_defs():
    """Map home icon only matches units whose faction owns the territory; capacity must match."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="wotr_exp_1.0"),
    )
    ud = dict(unit_defs)
    ud["_fake_gondor_home_westfold"] = UnitDefinition(
        id="_fake_gondor_home_westfold",
        display_name="Fake",
        faction="gondor",
        archetype="infantry",
        tags=["land"],
        attack=1,
        defense=1,
        movement=1,
        health=1,
        cost={"power": 1},
        specials=["home"],
        home_territory_ids=["westfold"],
    )
    cap = get_mobilization_capacity(state, "rohan", territory_defs, camp_defs, port_defs, ud)
    west = next((t for t in cap["territories"] if t.get("territory_id") == "westfold"), None)
    assert west is not None
    h = west.get("home_unit_capacity") or {}
    assert "_fake_gondor_home_westfold" not in h
    assert "rohan_peasant" in h
