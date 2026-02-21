"""
Test income collection and retreat mechanics.
Demonstrates:
1. Income calculated at end of turn, collected at start of next turn
2. Multi-round combat with retreat option
"""

from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.actions import (
    initiate_combat,
    continue_combat,
    retreat,
    end_phase,
    end_turn,
)
from backend.engine.reducer import apply_action
from backend.engine.utils import (
    initialize_game_state,
    print_game_state,
)


def create_unit(state, faction_id: str, unit_id: str, unit_defs) -> Unit:
    """Helper to create a Unit instance with proper ID generation."""
    unit_def = unit_defs[unit_id]
    instance_id = state.generate_unit_instance_id(faction_id, unit_id)
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=unit_def.movement,
        remaining_health=unit_def.health,
        base_movement=unit_def.movement,
        base_health=unit_def.health,
    )


def main():
    print("=" * 70)
    print("TEST: Income Collection & Retreat Mechanics")
    print("=" * 70)

    unit_defs, territory_defs, faction_defs, camp_defs = load_static_definitions()
    state = initialize_game_state(
        faction_defs, territory_defs, camp_defs=camp_defs
    )

    # Show initial resources
    print("\n[INITIAL STATE]")
    print(f"Gondor resources: {state.faction_resources['gondor']}")
    print(f"Gondor owns: {[t for t, s in state.territories.items() if s.owner == 'gondor']}")

    # Check what Minas Tirith produces
    mt_def = territory_defs['minas_tirith']
    print(f"Minas Tirith produces: {mt_def.produces}")

    # ===== PART 1: Income Collection Test =====
    print("\n" + "=" * 70)
    print("PART 1: Income Collection Test")
    print("=" * 70)

    # Skip through Gondor's turn phases to end turn
    print("\nSkipping through Gondor's turn phases...")
    phases = ["combat_move", "combat", "non_combat_move", "mobilization"]
    for phase in phases:
        state, _ = apply_action(
            state, end_phase("gondor"), unit_defs, territory_defs, faction_defs, camp_defs
        )
        print(f"  → Now in phase: {state.phase}")

    # End Gondor's turn
    print("\n[ENDING GONDOR'S TURN]")
    print(f"Gondor resources before end_turn: {state.faction_resources['gondor']}")
    state, events = apply_action(
        state, end_turn("gondor"), unit_defs, territory_defs, faction_defs, camp_defs
    )

    print("\nEvents emitted:")
    for e in events:
        print(f"  - {e.type}: {e.payload}")

    print(f"\nNow it's {state.current_faction}'s turn")
    print(f"Gondor pending income stored: {state.faction_pending_income.get('gondor', {})}")

    # Skip through Mordor's turn
    print("\n[SKIPPING MORDOR'S TURN]")
    for phase in phases:
        state, _ = apply_action(
            state, end_phase("mordor"), unit_defs, territory_defs, faction_defs, camp_defs
        )

    state, events = apply_action(
        state, end_turn("mordor"), unit_defs, territory_defs, faction_defs, camp_defs
    )

    # Now it's Gondor's turn again - income should be collected!
    print("\n[GONDOR'S TURN STARTS - INCOME COLLECTED]")
    print("Events emitted:")
    for e in events:
        if e.type in ["income_calculated", "income_collected", "turn_started"]:
            print(f"  - {e.type}: {e.payload}")

    print(f"\nGondor resources after income collection: {state.faction_resources['gondor']}")

    # ===== PART 2: Retreat Test =====
    print("\n" + "=" * 70)
    print("PART 2: Multi-Round Combat with Retreat")
    print("=" * 70)

    # Reset for combat test
    state = initialize_game_state(
        faction_defs, territory_defs, camp_defs=camp_defs
    )

    # Setup: Add strong Gondor attackers to Mordor (simulating combat_move)
    # Add multiple units so combat can go multiple rounds
    print("\n[SETUP: Creating contested territory in Mordor]")

    for _ in range(4):
        state.territories["mordor"].units.append(
            create_unit(state, "gondor", "gondor_infantry", unit_defs)
        )
    state.territories["mordor"].units.append(
        create_unit(state, "gondor", "gondor_knight", unit_defs)
    )

    # Add defenders
    for _ in range(3):
        state.territories["mordor"].units.append(
            create_unit(state, "mordor", "mordor_orc", unit_defs)
        )
    state.territories["mordor"].units.append(
        create_unit(state, "mordor", "mordor_troll", unit_defs)
    )

    gondor_units = [u for u in state.territories["mordor"].units if u.instance_id.startswith("gondor")]
    mordor_units = [u for u in state.territories["mordor"].units if u.instance_id.startswith("mordor")]

    print(f"Gondor attackers in Mordor: {len(gondor_units)}")
    print(f"Mordor defenders in Mordor: {len(mordor_units)}")

    # Gondor needs a territory to retreat to - use Ithilien (adjacent to Mordor)
    state.territories["ithilien"].owner = "gondor"
    print(f"Gondor retreat option: Ithilien (set as Gondor-owned for this test)")

    # Move to combat phase
    state.phase = "combat"

    # Round 1: Initiate combat with rigged dice (low rolls = few hits)
    print("\n[ROUND 1: Initiate Combat]")
    dice_rolls = {
        "attacker": [6, 7, 8, 9, 10],  # High rolls = no hits (need to roll UNDER attack value)
        "defender": [6, 7, 8, 9],  # High rolls = no hits
    }

    state, events = apply_action(
        state,
        initiate_combat("gondor", "mordor", dice_rolls),
        unit_defs, territory_defs, faction_defs, camp_defs
    )

    print("Combat events:")
    for e in events:
        if e.type == "combat_round_resolved":
            print(f"  Round {e.payload['round_number']}: "
                  f"attacker_hits={e.payload['attacker_hits']}, "
                  f"defender_hits={e.payload['defender_hits']}")
            print(f"    Remaining: {e.payload['attackers_remaining']} attackers, "
                  f"{e.payload['defenders_remaining']} defenders")

    # Check if combat is still active
    if state.active_combat:
        print(f"\nActive combat: round {state.active_combat.round_number}")
        print(f"Surviving attacker IDs: {state.active_combat.attacker_instance_ids}")

        # Round 2: Continue combat
        print("\n[ROUND 2: Continue Combat]")
        dice_rolls2 = {
            "attacker": [8, 9, 10, 11, 12],  # Still no hits
            "defender": [8, 9, 10, 11],
        }

        state, events = apply_action(
            state,
            continue_combat("gondor", dice_rolls2),
            unit_defs, territory_defs, faction_defs, camp_defs
        )

        for e in events:
            if e.type == "combat_round_resolved":
                print(f"  Round {e.payload['round_number']}: "
                      f"attacker_hits={e.payload['attacker_hits']}, "
                      f"defender_hits={e.payload['defender_hits']}")
                print(f"    Remaining: {e.payload['attackers_remaining']} attackers, "
                      f"{e.payload['defenders_remaining']} defenders")

    # Now retreat!
    if state.active_combat:
        print("\n[ATTACKER DECIDES TO RETREAT TO ITHILIEN]")
        print(f"Units retreating: {state.active_combat.attacker_instance_ids}")

        state, events = apply_action(
            state,
            retreat("gondor", "ithilien"),
            unit_defs, territory_defs, faction_defs, camp_defs
        )

        print("\nRetreat events:")
        for e in events:
            print(f"  - {e.type}: {e.payload}")

        print("\n[AFTER RETREAT]")
        print(f"Mordor owner: {state.territories['mordor'].owner}")
        print(f"Units in Mordor: {[u.instance_id for u in state.territories['mordor'].units]}")
        print(f"Units in Ithilien (retreat destination): {[u.instance_id for u in state.territories['ithilien'].units]}")
        print(f"Active combat: {state.active_combat}")
    else:
        print("\nCombat resolved before retreat option!")

    print("\n" + "=" * 70)
    print("✓ INCOME AND RETREAT TEST COMPLETED")
    print("=" * 70)


if __name__ == "__main__":
    main()
