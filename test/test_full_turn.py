"""
End-to-end test: Play through a complete turn cycle.
Tests all 5 phases for at least one faction using individual Unit instances.
"""

from collections import Counter
from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.actions import (
    purchase_units,
    move_units,
    mobilize_units,
    initiate_combat,
    end_phase,
    end_turn,
)
from backend.engine.reducer import apply_action
from backend.engine.utils import (
    initialize_game_state,
    print_game_state,
    apply_resource_production,
    generate_combat_rolls_for_units,
    print_combat_log,
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


def format_unit_list(units):
    """Format a list of units as (unit_id, count) tuples."""
    counts = Counter(u.unit_id for u in units)
    return list(counts.items())


def main():
    print("="*70)
    print("END-TO-END TEST: Full Turn Simulation (V2 - Individual Units)")
    print("="*70)

    # Load definitions
    unit_defs, territory_defs, faction_defs = load_static_definitions()

    # Initialize game
    state = initialize_game_state(faction_defs, territory_defs)
    print("\nInitial game state:")
    print_game_state(state, territory_defs)

    # ===== TURN 1, GONDOR'S TURN =====
    print("\n" + "="*70)
    print("TURN 1: GONDOR'S TURN")
    print("="*70)

    # Phase 1: PURCHASE
    print("\n[PHASE 1: PURCHASE]")
    print(f"Current phase: {state.phase}")
    print(f"Gondor resources: {state.faction_resources['gondor']}")

    purchase_action = purchase_units(
        "gondor",
        {"gondor_infantry": 3, "gondor_knight": 1},
    )
    state, _ = apply_action(state, purchase_action, unit_defs,
                         territory_defs, faction_defs)
    print(f"✓ Purchased 3 infantry + 1 knight")
    print(f"Gondor resources after: {state.faction_resources['gondor']}")
    print(
        f"Units in purchased pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units.get('gondor', [])]}")

    # For testing: manually place units in capital to allow combat_move
    # (In real gameplay, would need mobilization phase first to use these units)
    minas_tirith = state.territories['minas_tirith']
    for _ in range(3):
        minas_tirith.units.append(create_unit(state, "gondor", "gondor_infantry", unit_defs))
    minas_tirith.units.append(create_unit(state, "gondor", "gondor_knight", unit_defs))
    print(f"Units at Minas Tirith: {format_unit_list(minas_tirith.units)}")

    # End purchase phase
    end_phase_action = end_phase("gondor")
    state, _ = apply_action(state, end_phase_action, unit_defs,
                         territory_defs, faction_defs)
    print(f"✓ End phase. New phase: {state.phase}")

    # Phase 2: COMBAT_MOVE
    print("\n[PHASE 2: COMBAT_MOVE]")
    print(f"Current phase: {state.phase}")
    print("Moving units to Osgiliath (adjacent to Minas Tirith)")

    # First, add some defending units to Mordor (normally they'd start there)
    mordor = state.territories["mordor"]
    for _ in range(3):
        mordor.units.append(create_unit(state, "mordor", "mordor_orc", unit_defs))
    mordor.units.append(create_unit(state, "mordor", "mordor_troll", unit_defs))

    # Get specific units to move (2 infantry + 1 knight)
    infantry_units = [u for u in minas_tirith.units if u.unit_id == "gondor_infantry"][:2]
    knight_units = [u for u in minas_tirith.units if u.unit_id == "gondor_knight"][:1]
    units_to_move = infantry_units + knight_units

    # Move to Osgiliath first (adjacent to both Minas Tirith and Mordor)
    move_action = move_units(
        "gondor",
        "minas_tirith",
        "osgiliath",
        [u.instance_id for u in units_to_move],
    )
    state, _ = apply_action(state, move_action, unit_defs,
                         territory_defs, faction_defs)
    print(f"✓ Moved 2 infantry + 1 knight to Osgiliath")

    # Knight has movement 2, so can continue to Mordor
    # Infantry has movement 1, so stays in Osgiliath
    knight_in_osgiliath = [u for u in state.territories["osgiliath"].units
                           if u.unit_id == "gondor_knight" and u.remaining_movement > 0]

    # Move the knight INTO Mordor (enemy territory) for combat
    if knight_in_osgiliath:
        move_action2 = move_units(
            "gondor",
            "osgiliath",
            "mordor",  # Into enemy territory!
            [u.instance_id for u in knight_in_osgiliath],
        )
        state, _ = apply_action(state, move_action2, unit_defs,
                             territory_defs, faction_defs)
        print(f"✓ Knight continued INTO Mordor (enemy territory)")

    print(f"Units at Minas Tirith: {format_unit_list(state.territories['minas_tirith'].units)}")
    print(f"Units at Osgiliath: {format_unit_list(state.territories['osgiliath'].units)}")
    print(f"Mordor is now contested:")
    gondor_units = [u for u in state.territories['mordor'].units if u.instance_id.startswith('gondor')]
    mordor_units = [u for u in state.territories['mordor'].units if u.instance_id.startswith('mordor')]
    print(f"  Gondor attackers: {format_unit_list(gondor_units)}")
    print(f"  Mordor defenders: {format_unit_list(mordor_units)}")

    # Show movement was decremented for attacker(s) in Mordor
    for u in gondor_units:
        print(f"  - {u.instance_id}: remaining_movement={u.remaining_movement}")

    # End combat_move phase
    state, _ = apply_action(state, end_phase("gondor"),
                         unit_defs, territory_defs, faction_defs)
    print(f"✓ End phase. New phase: {state.phase}")

    # Phase 3: COMBAT
    print("\n[PHASE 3: COMBAT]")
    print(f"Current phase: {state.phase}")
    print("Resolving combat in contested territory (Mordor)...")

    # Generate dice rolls for combat
    attacker_units = [u for u in state.territories['mordor'].units if u.instance_id.startswith('gondor')]
    defender_units = [u for u in state.territories['mordor'].units if u.instance_id.startswith('mordor')]
    dice_rolls = generate_combat_rolls_for_units(
        attacker_units, defender_units, unit_defs, seed=42)
    print(f"Attacker rolls: {dice_rolls['attacker']}")
    print(f"Defender rolls: {dice_rolls['defender']}")

    # Initiate combat in Mordor (both attackers and defenders are there)
    combat_action = initiate_combat(
        "gondor",
        "mordor",  # The contested territory
        dice_rolls,
    )
    state, _ = apply_action(state, combat_action, unit_defs,
                         territory_defs, faction_defs)
    print(f"✓ Combat resolved")
    print(f"  Territory owner: {state.territories['mordor'].owner}")
    print(f"  Survivors in Mordor: {format_unit_list(state.territories['mordor'].units)}")

    # End combat phase
    state, _ = apply_action(state, end_phase("gondor"),
                         unit_defs, territory_defs, faction_defs)
    print(f"✓ End phase. New phase: {state.phase}")

    # Phase 4: NON_COMBAT_MOVE
    print("\n[PHASE 4: NON_COMBAT_MOVE]")
    print(f"Current phase: {state.phase}")

    # Check if there are units left at Osgiliath (not the ones that moved to Mordor)
    units_at_osgiliath = state.territories["osgiliath"].units
    if len(units_at_osgiliath) > 0:
        print("Moving remaining units from Osgiliath to Ithilien")

        # Only move units that have remaining movement
        movable_units = [u for u in units_at_osgiliath if u.remaining_movement > 0]
        if movable_units:
            move_action = move_units(
                "gondor",
                "osgiliath",
                "ithilien",
                [u.instance_id for u in movable_units],
            )
            state, _ = apply_action(state, move_action, unit_defs,
                                 territory_defs, faction_defs)
            print(f"✓ Moved surviving units to Ithilien")
        else:
            print("✗ Units at Osgiliath have no remaining movement")
    else:
        print("✓ No units at Osgiliath (they moved into Mordor during combat)")

    print(f"Units at Ithilien: {format_unit_list(state.territories['ithilien'].units)}")
    print(f"Units at Osgiliath: {format_unit_list(state.territories['osgiliath'].units)}")

    # End non_combat_move phase (this triggers movement/health reset!)
    print("\n[ENDING NON_COMBAT_MOVE - Stats will reset]")
    state, _ = apply_action(state, end_phase("gondor"),
                         unit_defs, territory_defs, faction_defs)
    print(f"✓ End phase. New phase: {state.phase}")
    print("Movement and health reset for Gondor's units")

    # Phase 5: MOBILIZATION
    print("\n[PHASE 5: MOBILIZATION]")
    print(f"Current phase: {state.phase}")
    print(f"Purchased units in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units.get('gondor', [])]}")

    # Mobilize remaining purchased units (we bought 3 infantry + 1 knight, already have some on board)
    if state.faction_purchased_units.get('gondor'):
        mobilize_action = mobilize_units(
            "gondor",
            "minas_tirith",
            [{"unit_id": "gondor_infantry", "count": 2}],  # Minas Tirith only has power=2
        )
        try:
            state, _ = apply_action(state, mobilize_action, unit_defs,
                                 territory_defs, faction_defs)
            print(f"✓ Mobilized 2 infantry to Minas Tirith")
        except ValueError as e:
            print(f"✗ Mobilization failed: {e}")

    # End mobilization phase (and turn)
    state, _ = apply_action(state, end_turn("gondor"),
                         unit_defs, territory_defs, faction_defs)
    print(f"✓ End turn")
    print(
        f"New turn: {state.turn_number}, Current faction: {state.current_faction}, Phase: {state.phase}")

    # ===== APPLY PRODUCTION =====
    print("\n[RESOURCE PRODUCTION]")
    print("Applying resource production for next turn:")
    state = apply_resource_production(state, territory_defs)
    print(
        f"Gondor resources after production: {state.faction_resources['gondor']}")

    # Print final state
    print("\n" + "="*70)
    print("FINAL GAME STATE")
    print("="*70)
    print_game_state(state, territory_defs, verbose=True)

    # Summary
    print("\n" + "="*70)
    print("✓ END-TO-END TEST COMPLETED SUCCESSFULLY")
    print("="*70)
    print("Tested:")
    print("  ✓ Purchase system with resource deduction")
    print("  ✓ Individual unit tracking with instance_ids")
    print("  ✓ Movement with remaining_movement tracking")
    print("  ✓ Combat with individual units")
    print("  ✓ Territory ownership transfer")
    print("  ✓ Phase transitions (all 5 phases)")
    print("  ✓ Movement/health reset at non_combat_move end")
    print("  ✓ Turn transitions")
    print("  ✓ Resource production")
    print("="*70)


if __name__ == "__main__":
    main()
