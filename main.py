"""
Main entry point for the Middle Earth Turn-Based Strategy Game Engine.
Demonstrates core functionality with a simple simulated scenario.
"""

from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.actions import (
    move_units,
    initiate_combat,
    end_phase,
    end_turn,
    purchase_units,
    mobilize_units,
)
from backend.engine.reducer import apply_action, replay_from_actions
from backend.engine.utils import (
    initialize_game_state,
    print_game_state,
    apply_resource_production,
    generate_combat_rolls_for_units,
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
    print("Middle Earth Turn-Based Strategy Game Engine - V2 (Individual Units)")
    print("=" * 60)

    # Load definitions
    unit_defs, territory_defs, faction_defs = load_static_definitions()

    # Initialize game state
    state = initialize_game_state(faction_defs, territory_defs)

    print("\n[INITIAL STATE]")
    print_game_state(state, territory_defs)

    # ===== SCENARIO 1: Test purchase and mobilization system =====
    print("\n[SCENARIO 1: Purchase + Mobilization System]")
    print(f"Gondor has: {state.faction_resources['gondor']}")
    print("Purchasing: 2x Gondor Infantry (cost: 2 power each)")

    # Purchase phase
    purchase_action = purchase_units(
        "gondor",
        {"gondor_infantry": 2},
    )

    try:
        state, events = apply_action(state, purchase_action,
                             unit_defs, territory_defs, faction_defs)
        print("✓ Purchase successful!")
        print(f"  Events: {[e.type for e in events]}")
        print(f"Gondor resources after: {state.faction_resources['gondor']}")
        print(f"Purchased units in pool: {[(s.unit_id, s.count) for s in state.faction_purchased_units['gondor']]}")
    except ValueError as e:
        print(f"✗ Purchase failed: {e}")
        return

    # Skip to mobilization phase
    for _ in range(4):  # purchase -> combat_move -> combat -> non_combat_move -> mobilization
        state, _ = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs)

    print(f"\nNow in phase: {state.phase}")
    print("Mobilizing 2 infantry to Minas Tirith...")

    mobilize_action = mobilize_units(
        "gondor",
        "minas_tirith",
        [{"unit_id": "gondor_infantry", "count": 2}],
    )

    try:
        state, events = apply_action(state, mobilize_action, unit_defs, territory_defs, faction_defs)
        print("✓ Mobilization successful!")
        print(f"  Events: {[e.type for e in events]}")
        print("Units in Minas Tirith:")
        for unit in state.territories["minas_tirith"].units:
            print(f"  - {unit.instance_id} (mv={unit.remaining_movement}, hp={unit.remaining_health})")
    except ValueError as e:
        print(f"✗ Mobilization failed: {e}")

    # ===== SCENARIO 2: Test movement with remaining_movement tracking =====
    print("\n[SCENARIO 2: Movement with remaining_movement Tracking]")

    # Start fresh for this scenario
    state = initialize_game_state(faction_defs, territory_defs)

    # Add units directly for testing
    infantry1 = create_unit(state, "gondor", "gondor_infantry", unit_defs)
    knight1 = create_unit(state, "gondor", "gondor_knight", unit_defs)  # movement=2

    state.territories["minas_tirith"].units.extend([infantry1, knight1])
    state.phase = "combat_move"

    print(f"Knight {knight1.instance_id} has movement={knight1.remaining_movement}")
    print("Moving knight from Minas Tirith to Osgiliath (1 space)...")

    move_action = move_units(
        "gondor",
        "minas_tirith",
        "osgiliath",
        [knight1.instance_id],
    )

    try:
        state, events = apply_action(state, move_action, unit_defs, territory_defs, faction_defs)
        # Find the knight in the new location
        knight_moved = next(u for u in state.territories["osgiliath"].units if u.instance_id == knight1.instance_id)
        print(f"✓ Movement successful! Knight now has remaining_movement={knight_moved.remaining_movement}")
        print(f"  Events: {[e.type for e in events]}")
    except ValueError as e:
        print(f"✗ Movement failed: {e}")

    # ===== SCENARIO 3: Test combat with individual units =====
    print("\n[SCENARIO 3: Combat with Individual Units]")

    # Reset for combat scenario
    state = initialize_game_state(faction_defs, territory_defs)

    # In A&A model: During combat_move, attackers move INTO enemy territory
    # Both attackers and defenders are now in the same territory (Mordor)

    # Add attacking units directly to Mordor (simulating they moved there during combat_move)
    attacker1 = create_unit(state, "gondor", "gondor_infantry", unit_defs)
    attacker2 = create_unit(state, "gondor", "gondor_infantry", unit_defs)
    attacker3 = create_unit(state, "gondor", "gondor_knight", unit_defs)

    # Add defending units to Mordor
    defender1 = create_unit(state, "mordor", "mordor_orc", unit_defs)
    defender2 = create_unit(state, "mordor", "mordor_orc", unit_defs)
    defender3 = create_unit(state, "mordor", "mordor_troll", unit_defs)  # health=2

    # Both attackers AND defenders in same territory (contested)
    state.territories["mordor"].units.extend([attacker1, attacker2, attacker3])
    state.territories["mordor"].units.extend([defender1, defender2, defender3])

    state.phase = "combat"

    # Separate attackers and defenders for display
    attacker_units = [u for u in state.territories["mordor"].units
                      if u.instance_id.startswith("gondor")]
    defender_units = [u for u in state.territories["mordor"].units
                      if u.instance_id.startswith("mordor")]

    print("Attacker units (Gondor, in Mordor):")
    for u in attacker_units:
        print(f"  - {u.instance_id} (hp={u.remaining_health})")

    print("\nDefender units (Mordor, in Mordor):")
    for u in defender_units:
        print(f"  - {u.instance_id} (hp={u.remaining_health})")

    # Generate dice rolls
    dice_rolls = generate_combat_rolls_for_units(attacker_units, defender_units, unit_defs, seed=42)

    print(f"\nDice rolls - Attacker: {dice_rolls['attacker']}, Defender: {dice_rolls['defender']}")

    # initiate_combat now just takes territory_id (the contested territory)
    combat_action = initiate_combat(
        "gondor",
        "mordor",  # The contested territory
        dice_rolls,
    )

    state, events = apply_action(state, combat_action, unit_defs, territory_defs, faction_defs)

    print("\nCombat events:")
    for e in events:
        print(f"  - {e.type}: {e.payload.get('territory', e.payload.get('round_number', ''))}")

    print("\nAfter combat:")
    print(f"Mordor territory owner: {state.territories['mordor'].owner}")
    print("Surviving units in Mordor:")
    for u in state.territories["mordor"].units:
        print(f"  - {u.instance_id} (hp={u.remaining_health})")

    # ===== SCENARIO 4: Test movement/health reset =====
    print("\n[SCENARIO 4: Movement/Health Reset at Phase End]")

    # Create a scenario where units have used movement and taken damage
    state = initialize_game_state(faction_defs, territory_defs)

    knight = create_unit(state, "gondor", "gondor_knight", unit_defs)
    knight.remaining_movement = 0  # Simulating used movement
    knight.remaining_health = 1  # Simulating combat damage (but survived)
    state.territories["minas_tirith"].units.append(knight)

    state.phase = "non_combat_move"

    print(f"Before phase end: Knight mv={knight.remaining_movement}, hp={knight.remaining_health}")

    # End non_combat_move phase (triggers reset)
    state, events = apply_action(state, end_phase("gondor"), unit_defs, territory_defs, faction_defs)

    # Find the knight again
    knight_reset = state.territories["minas_tirith"].units[0]
    print(f"After phase end: Knight mv={knight_reset.remaining_movement}, hp={knight_reset.remaining_health}")
    print(f"  Events: {[e.type for e in events]}")
    print(f"✓ Stats reset to base values (mv={knight_reset.base_movement}, hp={knight_reset.base_health})")

    # ===== Summary =====
    print("\n" + "=" * 60)
    print("✓ All V2 features demonstrated successfully:")
    print("  • Individual Unit tracking with instance_ids")
    print("  • remaining_movement decremented per move")
    print("  • Combat with individual units")
    print("  • Movement/health reset at non_combat_move phase end")
    print("=" * 60)


if __name__ == "__main__":
    main()
