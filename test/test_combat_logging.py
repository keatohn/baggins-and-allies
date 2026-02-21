"""
Test script to demonstrate multi-round combat with retreat option.
Uses individual Unit instances and the new combat system.
"""

from copy import deepcopy
from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.combat import resolve_combat_round, calculate_required_dice
from backend.engine.utils import generate_dice_rolls_for_units


def create_unit_simple(faction_id: str, unit_id: str, unit_defs, counter: list) -> Unit:
    """Simple helper to create a Unit instance for testing."""
    unit_def = unit_defs[unit_id]
    counter[0] += 1
    instance_id = f"{faction_id}_{unit_id}_{counter[0]:03d}"
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=unit_def.movement,
        remaining_health=unit_def.health,
        base_movement=unit_def.movement,
        base_health=unit_def.health,
    )


def main():
    print("="*70)
    print("MULTI-ROUND COMBAT DEMONSTRATION")
    print("="*70)

    # Load definitions
    unit_defs, territory_defs, faction_defs, camp_defs = load_static_definitions()

    # Counter for unique IDs
    counter = [0]

    # Gondor attacking force: 3 infantry + 2 knights (stronger force)
    attacker_units = [
        create_unit_simple("gondor", "gondor_infantry", unit_defs, counter),
        create_unit_simple("gondor", "gondor_infantry", unit_defs, counter),
        create_unit_simple("gondor", "gondor_infantry", unit_defs, counter),
        create_unit_simple("gondor", "gondor_knight", unit_defs, counter),
        create_unit_simple("gondor", "gondor_knight", unit_defs, counter),
    ]

    # Mordor defending force: 4 orcs + 1 troll
    defender_units = [
        create_unit_simple("mordor", "mordor_orc", unit_defs, counter),
        create_unit_simple("mordor", "mordor_orc", unit_defs, counter),
        create_unit_simple("mordor", "mordor_orc", unit_defs, counter),
        create_unit_simple("mordor", "mordor_orc", unit_defs, counter),
        create_unit_simple("mordor", "mordor_troll", unit_defs, counter),
    ]

    print("\n[INITIAL FORCES]")
    print("\nAttacking Force (Gondor):")
    for unit in attacker_units:
        unit_def = unit_defs[unit.unit_id]
        print(f"  {unit.instance_id}: {unit_def.display_name} (attack={unit_def.attack})")

    print("\nDefending Force (Mordor):")
    for unit in defender_units:
        unit_def = unit_defs[unit.unit_id]
        print(f"  {unit.instance_id}: {unit_def.display_name} (defense={unit_def.defense}, hp={unit.remaining_health})")

    # Simulate multi-round combat
    round_num = 0
    seed = 42

    # Make copies for combat (originals would be in territories)
    attackers = deepcopy(attacker_units)
    defenders = deepcopy(defender_units)

    while len(attackers) > 0 and len(defenders) > 0:
        round_num += 1
        print(f"\n{'='*70}")
        print(f"ROUND {round_num}")
        print(f"{'='*70}")

        print(f"\nAttackers: {len(attackers)} units")
        print(f"Defenders: {len(defenders)} units")

        # Generate dice rolls for this round
        attacker_dice_needed = calculate_required_dice(attackers, unit_defs)
        defender_dice_needed = calculate_required_dice(defenders, unit_defs)

        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(attackers, unit_defs, seed + round_num),
            "defender": generate_dice_rolls_for_units(defenders, unit_defs, seed + round_num + 100),
        }

        print(f"\nAttacker rolls ({len(dice_rolls['attacker'])} dice): {dice_rolls['attacker']}")
        print(f"Defender rolls ({len(dice_rolls['defender'])} dice): {dice_rolls['defender']}")

        # Fight round (this modifies attackers/defenders lists in place)
        result = resolve_combat_round(attackers, defenders, unit_defs, dice_rolls)

        print(f"\nAttacker hits: {result.attacker_hits}")
        print(f"Defender hits: {result.defender_hits}")

        if result.attacker_casualties:
            print(f"Attacker casualties: {', '.join(result.attacker_casualties)}")
        else:
            print("Attacker casualties: None")

        if result.defender_casualties:
            print(f"Defender casualties: {', '.join(result.defender_casualties)}")
        else:
            print("Defender casualties: None")

        print(f"\nSurvivors - Attackers: {len(attackers)}, Defenders: {len(defenders)}")

        # Simulate attacker decision after round 2 if both sides have survivors
        if round_num == 2 and len(attackers) > 0 and len(defenders) > 0:
            print("\n[ATTACKER DECISION POINT]")
            print("Attacker could choose to RETREAT here, or CONTINUE...")
            print("(Simulating: Attacker chooses to CONTINUE)")

    # Combat resolved
    print(f"\n{'='*70}")
    print("COMBAT RESOLVED")
    print(f"{'='*70}")

    if len(defenders) == 0:
        print(f"\n✓ ATTACKER WINS after {round_num} rounds!")
        print(f"Surviving attackers ({len(attackers)}):")
        for unit in attackers:
            unit_def = unit_defs[unit.unit_id]
            print(f"  {unit.instance_id}: {unit_def.display_name} (hp={unit.remaining_health})")
    else:
        print(f"\n✗ DEFENDER WINS after {round_num} rounds!")
        print(f"Surviving defenders ({len(defenders)}):")
        for unit in defenders:
            unit_def = unit_defs[unit.unit_id]
            print(f"  {unit.instance_id}: {unit_def.display_name} (hp={unit.remaining_health})")

    print("\n" + "="*70)
    print("Multi-round combat demonstration complete!")
    print("="*70)


if __name__ == "__main__":
    main()
