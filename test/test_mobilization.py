"""
Test mobilization phase with deferred unit placement.
Demonstrates purchase → mobilization flow with individual Unit instances.
"""

from collections import Counter
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.state import UnitStack
from backend.engine.actions import (
    purchase_units,
    mobilize_units,
    end_phase,
    end_turn,
)
from backend.engine.reducer import apply_action
from backend.engine.utils import (
    initialize_game_state,
    print_game_state,
)


def main():
    print("="*70)
    print("MOBILIZATION TEST: Purchase → Defer → Mobilize (V2 - Individual Units)")
    print("="*70)

    # Load definitions
    unit_defs, territory_defs, faction_defs, camp_defs = load_static_definitions(setup_id="1.0")

    # Initialize game (setup 1.0); use Rohan for power-cap test (Dunharrow has power=2)
    state = initialize_game_state(
        faction_defs, territory_defs, camp_defs=camp_defs,
        starting_setup=load_starting_setup(setup_id="1.0"),
    )
    state.current_faction = "rohan"
    # Rohan's territories with standing camps (edoras, dunharrow in 1.0; dunharrow produces 2 power)
    state.mobilization_camps = ["edoras", "dunharrow"]
    print("\n[INITIAL STATE]")
    print_game_state(state, territory_defs)

    # Phase 1: PURCHASE
    print("\n" + "="*70)
    print("[PHASE 1: PURCHASE]")
    print("="*70)
    print(f"Rohan resources: {state.faction_resources['rohan']}")

    purchase_action = purchase_units(
        "rohan",
        {"rohirrim_soldier": 3, "rider_of_rohan": 1},
    )
    state, _ = apply_action(
        state, purchase_action, unit_defs, territory_defs, faction_defs, camp_defs
    )
    print(f"✓ Purchased: 3 rohirrim_soldier + 1 rider_of_rohan")
    print(f"Rohan resources after: {state.faction_resources['rohan']}")
    print(
        f"Purchased units (in pool): {[(s.unit_id, s.count) for s in state.faction_purchased_units['rohan']]}")

    # Show units in territory (should be empty since purchases go to pool)
    units_in_dunharrow = state.territories['dunharrow'].units
    if units_in_dunharrow:
        unit_counts = Counter(u.unit_id for u in units_in_dunharrow)
        print(f"Units in Dunharrow: {list(unit_counts.items())}")
    else:
        print(f"Units in Dunharrow: [] (purchases are in pool, not deployed yet)")

    # Skip through phases to reach mobilization
    # Phase order: purchase -> combat_move -> combat -> non_combat_move -> mobilization
    # Need 4 end_phase calls to get from purchase to mobilization
    phase_names = ["COMBAT_MOVE", "COMBAT", "NON_COMBAT_MOVE", "MOBILIZATION"]
    for i, phase_name in enumerate(phase_names, start=2):
        print(f"\n[Skipping to PHASE {i}: {phase_name}]")
        end_phase_action = end_phase("rohan")
        state, _ = apply_action(
            state, end_phase_action, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✓ Now in phase: {state.phase}")

    # Phase 5: MOBILIZATION
    print("\n" + "="*70)
    print("[PHASE 5: MOBILIZATION]")
    print("="*70)
    print(f"Current phase: {state.phase}")
    print(
        f"Purchased units still in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units['rohan']]}")

    # Mobilize to Dunharrow (camp, produces power=2, so can mobilize 2 units)
    mobilize_action = mobilize_units(
        "rohan",
        "dunharrow",
        [
            {"unit_id": "rohirrim_soldier", "count": 2},
        ],
    )
    try:
        state, _ = apply_action(
            state, mobilize_action, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✓ Mobilized 2 rohirrim_soldier to Dunharrow")
    except ValueError as e:
        print(f"✗ Mobilization failed: {e}")
        return

    print(
        f"Purchased units remaining in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units['rohan']]}")

    # Show individual units now in territory (pending until end of phase)
    print(f"Units now in Dunharrow (individual instances, after apply):")
    for unit in state.territories['dunharrow'].units:
        print(f"  - {unit.instance_id} (mv={unit.remaining_movement}, hp={unit.remaining_health})")

    # Test power limit: total mobilized to a territory cannot exceed its power production
    print("\n[POWER LIMIT VALIDATION]")
    print("Attempting to mobilize 2 more to Dunharrow (already 2 pending; power=2)...")

    mobilize_action2 = mobilize_units(
        "rohan",
        "dunharrow",
        [
            {"unit_id": "rohirrim_soldier", "count": 1},
            {"unit_id": "rider_of_rohan", "count": 1},
        ],
    )
    try:
        state, _ = apply_action(
            state, mobilize_action2, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✗ Should have been blocked (2 already pending + 2 = 4 > power 2)")
    except ValueError as e:
        print(f"✓ Mobilization blocked (as expected): {e}")

    # Now test exceeding the per-action limit
    print("\nAttempting to mobilize 3 units in one action (exceeds power=2)...")
    # Purchase more units first for this test
    state2 = state.copy()
    state2.faction_purchased_units["rohan"] = [
        UnitStack(unit_id="rohirrim_soldier", count=3),
    ]
    mobilize_action3 = mobilize_units(
        "rohan",
        "dunharrow",
        [{"unit_id": "rohirrim_soldier", "count": 3}],
    )
    try:
        state2, _ = apply_action(
            state2, mobilize_action3, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✗ Should have been blocked!")
    except ValueError as e:
        print(f"✓ Mobilization blocked (as expected): {e}")

    # End turn
    print("\n[END TURN]")
    end_turn_action = end_turn("rohan")
    state, _ = apply_action(
        state, end_turn_action, unit_defs, territory_defs, faction_defs, camp_defs
    )
    print(f"✓ End turn")
    print(f"Current faction: {state.current_faction}")
    print(f"Current phase: {state.phase}")
    print(
        f"Rohan purchased units (should be empty - unspent purchases lost): {state.faction_purchased_units['rohan']}")

    print("\n" + "="*70)
    print("✓ MOBILIZATION TEST COMPLETED SUCCESSFULLY")
    print("="*70)


if __name__ == "__main__":
    main()
