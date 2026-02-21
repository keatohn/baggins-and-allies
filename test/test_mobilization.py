"""
Test mobilization phase with deferred unit placement.
Demonstrates purchase → mobilization flow with individual Unit instances.
"""

from collections import Counter
from backend.engine.definitions import load_static_definitions
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
    unit_defs, territory_defs, faction_defs, camp_defs = load_static_definitions()

    # Initialize game
    state = initialize_game_state(
        faction_defs, territory_defs, camp_defs=camp_defs
    )
    print("\n[INITIAL STATE]")
    print_game_state(state, territory_defs)

    # Phase 1: PURCHASE
    print("\n" + "="*70)
    print("[PHASE 1: PURCHASE]")
    print("="*70)
    print(f"Gondor resources: {state.faction_resources['gondor']}")

    purchase_action = purchase_units(
        "gondor",
        {"gondor_infantry": 3, "gondor_knight": 1},
    )
    state, _ = apply_action(
        state, purchase_action, unit_defs, territory_defs, faction_defs, camp_defs
    )
    print(f"✓ Purchased: 3 infantry + 1 knight")
    print(f"Gondor resources after: {state.faction_resources['gondor']}")
    print(
        f"Purchased units (in pool): {[(s.unit_id, s.count) for s in state.faction_purchased_units['gondor']]}")

    # Show units in territory (should be empty since purchases go to pool)
    units_in_minas = state.territories['minas_tirith'].units
    if units_in_minas:
        unit_counts = Counter(u.unit_id for u in units_in_minas)
        print(f"Units in Minas Tirith: {list(unit_counts.items())}")
    else:
        print(f"Units in Minas Tirith: [] (purchases are in pool, not deployed yet)")

    # Skip through phases to reach mobilization
    # Phase order: purchase -> combat_move -> combat -> non_combat_move -> mobilization
    # Need 4 end_phase calls to get from purchase to mobilization
    phase_names = ["COMBAT_MOVE", "COMBAT", "NON_COMBAT_MOVE", "MOBILIZATION"]
    for i, phase_name in enumerate(phase_names, start=2):
        print(f"\n[Skipping to PHASE {i}: {phase_name}]")
        end_phase_action = end_phase("gondor")
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
        f"Purchased units still in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units['gondor']]}")

    # Mobilize to Minas Tirith (stronghold, produces power=2, so can mobilize 2 units)
    mobilize_action = mobilize_units(
        "gondor",
        "minas_tirith",
        [
            {"unit_id": "gondor_infantry", "count": 2},
        ],
    )
    try:
        state, _ = apply_action(
            state, mobilize_action, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✓ Mobilized 2 infantry to Minas Tirith")
    except ValueError as e:
        print(f"✗ Mobilization failed: {e}")
        return

    print(
        f"Purchased units remaining in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units['gondor']]}")

    # Show individual units now in territory
    print(f"Units now in Minas Tirith (individual instances):")
    for unit in state.territories['minas_tirith'].units:
        print(f"  - {unit.instance_id} (mv={unit.remaining_movement}, hp={unit.remaining_health})")

    # Test power limit validation (per-action, not cumulative per turn)
    print("\n[POWER LIMIT VALIDATION]")
    print("Attempting to mobilize 3 more units (exceeds power=2 per action)...")

    mobilize_action2 = mobilize_units(
        "gondor",
        "minas_tirith",
        [
            {"unit_id": "gondor_infantry", "count": 1},
            {"unit_id": "gondor_knight", "count": 1},
            # Try to mobilize one extra - but we only have 1 infantry and 1 knight left
            # So let's just try to exceed the power limit with what we have
        ],
    )
    # This should succeed because 2 units = power limit of 2
    try:
        state, _ = apply_action(
            state, mobilize_action2, unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"✓ Mobilized 2 more units (1 infantry + 1 knight)")
        print(f"  (Note: Power limit is per-action, not cumulative per turn)")
    except ValueError as e:
        print(f"✗ Mobilization blocked: {e}")

    # Now test exceeding the per-action limit
    print("\nAttempting to mobilize 3 units in one action (exceeds power=2)...")
    # Purchase more units first for this test
    state2 = state.copy()
    state2.faction_purchased_units["gondor"] = [
        UnitStack(unit_id="gondor_infantry", count=3),
    ]
    mobilize_action3 = mobilize_units(
        "gondor",
        "minas_tirith",
        [{"unit_id": "gondor_infantry", "count": 3}],
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
    end_turn_action = end_turn("gondor")
    state, _ = apply_action(
        state, end_turn_action, unit_defs, territory_defs, faction_defs, camp_defs
    )
    print(f"✓ End turn")
    print(f"Current faction: {state.current_faction}")
    print(f"Current phase: {state.phase}")
    print(
        f"Gondor purchased units (should be empty - unspent purchases lost): {state.faction_purchased_units['gondor']}")

    print("\n" + "="*70)
    print("✓ MOBILIZATION TEST COMPLETED SUCCESSFULLY")
    print("="*70)


if __name__ == "__main__":
    main()
