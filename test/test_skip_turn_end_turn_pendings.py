"""
skip_turn / end_turn must flush queued mobilization work before the turn advances.

PendingMobilization and PendingCampPlacement carry no faction of their own, so if
they survive into the next faction's turn, that faction's end_phase deploys them as
itself (the queuing player's paid units appear under the wrong faction).
"""

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import (
    end_phase,
    end_turn,
    mobilize_units,
    queue_camp_placement,
    skip_turn,
)
from backend.engine.reducer import apply_action
from backend.engine.state import UnitStack
from backend.engine.utils import initialize_game_state


def _defs():
    return load_static_definitions(setup_id="wotr_1.1")


def _state_in_mobilization(defs):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = initialize_game_state(
        faction_defs,
        territory_defs,
        camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="wotr_1.1"),
    )
    state.current_faction = "rohan"
    state.phase = "mobilization"
    # initialize_game_state only snapshots the first faction in turn_order
    state.faction_territories_at_turn_start["rohan"] = [
        tid for tid, ts in state.territories.items() if ts.owner == "rohan"
    ]
    return state


def _queue_mobilization(state, defs):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state.faction_purchased_units["rohan"] = [
        UnitStack(unit_id="rohirrim_soldier", count=2),
    ]
    state, _ = apply_action(
        state,
        mobilize_units(
            "rohan", "dunharrow", [{"unit_id": "rohirrim_soldier", "count": 2}]
        ),
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )
    assert state.pending_mobilizations, "setup: mobilization must be queued"
    return state


def _soldiers_in(state, territory_id):
    return [
        u
        for u in state.territories[territory_id].units
        if u.unit_id == "rohirrim_soldier"
    ]


def test_skip_turn_flushes_queued_mobilizations():
    defs = _defs()
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = _queue_mobilization(_state_in_mobilization(defs), defs)

    state, events = apply_action(
        state, skip_turn("rohan"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )

    assert state.pending_mobilizations == []
    assert state.current_faction != "rohan"
    soldiers = _soldiers_in(state, "dunharrow")
    assert len(soldiers) == 2, "queued units must deploy for the skipping faction"
    assert all(u.instance_id.startswith("rohan_") for u in soldiers)
    assert any(e.type == "units_mobilized" for e in events)


def test_end_turn_flushes_queued_mobilizations():
    defs = _defs()
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = _queue_mobilization(_state_in_mobilization(defs), defs)

    state, events = apply_action(
        state, end_turn("rohan"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )

    assert state.pending_mobilizations == []
    assert state.current_faction != "rohan"
    soldiers = _soldiers_in(state, "dunharrow")
    assert len(soldiers) == 2, "queued units must deploy before the turn advances"
    assert all(u.instance_id.startswith("rohan_") for u in soldiers)
    assert any(e.type == "units_mobilized" for e in events)


def test_skipped_mobilizations_do_not_deploy_as_next_faction():
    """Regression: queued pendings leaked into the next faction's end_phase."""
    defs = _defs()
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = _queue_mobilization(_state_in_mobilization(defs), defs)
    state, _ = apply_action(
        state, skip_turn("rohan"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )
    assert state.pending_mobilizations == []

    # Walk the next faction (mordor) through their whole turn, including end of
    # mobilization where pending_mobilizations would be applied as current_faction.
    for _ in range(4):
        state, _ = apply_action(
            state,
            end_phase(state.current_faction),
            unit_defs,
            territory_defs,
            faction_defs,
            camp_defs,
            port_defs,
        )
    assert state.phase == "mobilization"
    state, _ = apply_action(
        state,
        end_phase(state.current_faction),
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )

    soldiers = _soldiers_in(state, "dunharrow")
    assert len(soldiers) == 2, "the next faction must not re-deploy leaked units"
    assert all(u.instance_id.startswith("rohan_") for u in soldiers)
    assert not any(
        u.instance_id.startswith("mordor_rohirrim_soldier")
        for t in state.territories.values()
        for u in t.units
    )


def test_skip_turn_flushes_queued_camp_placements():
    defs = _defs()
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = _state_in_mobilization(defs)
    # westfold: rohan-owned at turn start, produces power, no standing camp
    state.pending_camps = [
        {"territory_options": ["westfold"], "placed_territory_id": None},
    ]
    state, _ = apply_action(
        state,
        queue_camp_placement("rohan", 0, "westfold"),
        unit_defs,
        territory_defs,
        faction_defs,
        camp_defs,
        port_defs,
    )
    assert state.pending_camp_placements, "setup: placement must be queued"

    state, events = apply_action(
        state, skip_turn("rohan"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )

    assert state.pending_camp_placements == []
    assert state.dynamic_camps.get("purchased_camp_westfold") == "westfold"
    assert "purchased_camp_westfold" in state.camps_standing
    assert any(e.type == "camp_placed" for e in events)
    assert state.current_faction != "rohan"


def test_skip_turn_in_purchase_phase_still_ends_turn():
    defs = _defs()
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    state = _state_in_mobilization(defs)
    state.phase = "purchase"

    state, _ = apply_action(
        state, skip_turn("rohan"), unit_defs, territory_defs, faction_defs, camp_defs, port_defs
    )

    assert state.current_faction != "rohan"
    assert state.phase == "purchase"
    assert state.pending_mobilizations == []
    assert state.pending_camp_placements == []
