#!/usr/bin/env python3
"""
Interactive CLI for testing the LOTR game engine.
Run: python play_cli.py
"""

import sys
from backend.engine.definitions import load_static_definitions
from backend.engine.utils import initialize_game_state, get_default_starting_setup, generate_combat_rolls_for_units
from backend.engine.reducer import apply_action
from backend.engine.actions import (
    purchase_units,
    move_units,
    initiate_combat,
    continue_combat,
    retreat,
    mobilize_units,
    end_phase,
    end_turn,
)
from backend.engine.queries import (
    get_available_action_types,
    get_movable_units,
    get_unit_move_targets,
    get_purchasable_units,
    get_mobilization_territories,
    get_mobilization_capacity,
    get_contested_territories,
    get_retreat_options,
    get_purchased_units,
    get_faction_resources,
    get_territory_units,
    get_game_summary,
    validate_action,
)


def clear_screen():
    print("\n" * 2)


def print_header(state, faction_defs):
    """Print game status header."""
    faction = state.current_faction
    faction_def = faction_defs.get(faction)
    alliance = faction_def.alliance if faction_def else "?"

    print("=" * 60)
    print(
        f"  TURN {state.turn_number} | {faction.upper()} ({alliance}) | Phase: {state.phase.upper()}")
    if state.winner:
        print(f"  *** GAME OVER - {state.winner.upper()} ALLIANCE WINS ***")
    if state.active_combat:
        print(f"  [ACTIVE COMBAT in {state.active_combat.territory_id}]")
    print("=" * 60)


def print_resources(state):
    """Print current faction's resources."""
    resources = get_faction_resources(state, state.current_faction)
    res_str = ", ".join(f"{k}: {v}" for k, v in resources.items())
    print(f"\nResources: {res_str}")


def print_territories(state, territory_defs, faction_defs):
    """Print territory overview."""
    print("\n--- Territories ---")
    for tid, ts in sorted(state.territories.items()):
        td = territory_defs.get(tid)
        owner = ts.owner or "neutral"
        unit_count = len(ts.units)
        stronghold = " [STRONGHOLD]" if td and td.is_stronghold else ""

        # Show pending capture indicator
        pending = ""
        if tid in state.pending_captures:
            new_owner = state.pending_captures[tid]
            pending = f" [CAPTURED by {new_owner}]"

        # Show units summary
        if ts.units:
            unit_summary = {}
            for u in ts.units:
                unit_summary[u.unit_id] = unit_summary.get(u.unit_id, 0) + 1
            units_str = ", ".join(
                f"{v}x {k.split('_')[-1]}" for k, v in unit_summary.items())
            print(f"  {tid}: {owner}{stronghold}{pending} - {units_str}")
        else:
            print(f"  {tid}: {owner}{stronghold}{pending}")


def print_available_actions(state):
    """Print available action types."""
    actions = get_available_action_types(state)
    print(f"\nAvailable actions: {', '.join(actions)}")


def prompt_purchase(state, unit_defs, territory_defs, faction_defs):
    """Handle purchase action."""
    purchasable = get_purchasable_units(
        state, state.current_faction, unit_defs)

    if not purchasable:
        print("No units available to purchase.")
        return None

    # Show mobilization capacity
    mob_cap = get_mobilization_capacity(
        state, state.current_faction, territory_defs)
    print(
        f"\n--- Purchase Units --- (Mobilization capacity: {mob_cap['total_capacity']} units)")
    for t in mob_cap["territories"]:
        print(f"    {t['territory_id']}: {t['power']} units")
    print()

    for i, u in enumerate(purchasable):
        cost_str = ", ".join(f"{v} {k}" for k, v in u["cost"].items())
        print(
            f"  {i+1}. {u['display_name']} (A:{u['attack']} D:{u['defense']} M:{u['movement']}) - {cost_str} [max: {u['max_affordable']}]")
    print("  0. Cancel")

    purchases = {}
    total_purchased = 0
    while True:
        try:
            choice = input(
                f"\nSelect unit # (or 0 to finish) [{total_purchased}/{mob_cap['total_capacity']} capacity]: ").strip()
            if choice == "0" or choice == "":
                break

            idx = int(choice) - 1
            if 0 <= idx < len(purchasable):
                unit = purchasable[idx]
                remaining_cap = mob_cap["total_capacity"] - total_purchased
                count = input(
                    f"How many {unit['display_name']}? (capacity left: {remaining_cap}): ").strip()
                count = int(count) if count else 1
                if count > 0:
                    purchases[unit["unit_id"]] = purchases.get(
                        unit["unit_id"], 0) + count
                    total_purchased += count
                    print(f"  Added {count}x {unit['display_name']}")

                    if total_purchased >= mob_cap["total_capacity"]:
                        print(f"  [At mobilization capacity limit]")
        except ValueError:
            print("Invalid input")

    if purchases:
        return purchase_units(state.current_faction, purchases)
    return None


def prompt_move(state, unit_defs, territory_defs, faction_defs):
    """Handle move action."""
    movable = get_movable_units(state, state.current_faction)

    if not movable:
        print("No units can move.")
        return None

    print("\n--- Move Units ---")
    print("Units that can move:")

    # Group by territory
    by_territory = {}
    for u in movable:
        tid = u["territory_id"]
        if tid not in by_territory:
            by_territory[tid] = []
        by_territory[tid].append(u)

    all_units = []
    for tid, units in sorted(by_territory.items()):
        print(f"\n  {tid}:")
        for u in units:
            all_units.append(u)
            idx = len(all_units)
            unit_type = u["unit_id"].split("_")[-1]
            print(
                f"    {idx}. {u['instance_id']} ({unit_type}, mv={u['remaining_movement']})")

    print("\n  0. Cancel")

    try:
        choice = input("\nSelect unit # to move: ").strip()
        if choice == "0" or choice == "":
            return None

        idx = int(choice) - 1
        if not (0 <= idx < len(all_units)):
            print("Invalid selection")
            return None

        unit = all_units[idx]
        origin = unit["territory_id"]

        # Get move targets
        targets = get_unit_move_targets(
            state, unit["instance_id"], unit_defs, territory_defs, faction_defs
        )

        if not targets:
            print("No valid destinations for this unit.")
            return None

        # Filter destinations based on phase
        if state.phase == "combat_move":
            # During combat_move, only show enemy territories
            current_alliance = faction_defs.get(state.current_faction)
            current_alliance = current_alliance.alliance if current_alliance else None

            filtered_targets = {}
            for tid, cost in targets.items():
                owner = state.territories[tid].owner
                if owner is None:
                    # Neutral territory - can move there
                    filtered_targets[tid] = cost
                elif owner != state.current_faction:
                    owner_def = faction_defs.get(owner)
                    owner_alliance = owner_def.alliance if owner_def else None
                    if owner_alliance != current_alliance:
                        # Enemy territory
                        filtered_targets[tid] = cost
            targets = filtered_targets

            if not targets:
                print(
                    "No enemy territories reachable. Use non-combat move for friendly repositioning.")
                return None

        print(f"\nValid destinations from {origin}:")
        target_list = sorted(targets.items(), key=lambda x: x[1])
        for i, (tid, cost) in enumerate(target_list):
            owner = state.territories[tid].owner or "neutral"
            marker = " [ENEMY]" if owner != state.current_faction and owner != "neutral" else ""
            print(f"  {i+1}. {tid} ({owner}){marker} - cost: {cost}")
        print("  0. Cancel")

        dest_choice = input("\nSelect destination #: ").strip()
        if dest_choice == "0" or dest_choice == "":
            return None

        dest_idx = int(dest_choice) - 1
        if 0 <= dest_idx < len(target_list):
            destination = target_list[dest_idx][0]

            # Ask if moving more units from same origin to same destination
            same_origin_units = [u for u in all_units if u["territory_id"]
                                 == origin and u["instance_id"] != unit["instance_id"]]
            units_to_move = [unit["instance_id"]]

            if same_origin_units:
                print(
                    f"\nMove additional units from {origin} to {destination}?")
                for u in same_origin_units:
                    # Check if this unit can also reach destination
                    u_targets = get_unit_move_targets(
                        state, u["instance_id"], unit_defs, territory_defs, faction_defs
                    )
                    if destination in u_targets:
                        unit_type = u["unit_id"].split("_")[-1]
                        add = input(
                            f"  Also move {u['instance_id']} ({unit_type})? (y/n): ").strip().lower()
                        if add == "y":
                            units_to_move.append(u["instance_id"])

            return move_units(state.current_faction, origin, destination, units_to_move)

    except ValueError:
        print("Invalid input")

    return None


def prompt_combat(state, unit_defs, territory_defs, faction_defs):
    """Handle initiate_combat action."""
    contested = get_contested_territories(
        state, state.current_faction, faction_defs)

    if not contested:
        print("No contested territories to fight in.")
        return None

    print("\n--- Initiate Combat ---")
    for i, t in enumerate(contested):
        print(
            f"  {i+1}. {t['territory_id']} - {t['attacker_count']} attackers vs {t['defender_count']} defenders")
    print("  0. Cancel")

    try:
        choice = input("\nSelect territory #: ").strip()
        if choice == "0" or choice == "":
            return None

        idx = int(choice) - 1
        if 0 <= idx < len(contested):
            territory_id = contested[idx]["territory_id"]

            # Get attacker and defender units for dice roll generation
            territory = state.territories[territory_id]
            attacker_faction = state.current_faction

            attacker_units = [
                u for u in territory.units if u.instance_id.startswith(attacker_faction + "_")]
            defender_units = [u for u in territory.units if not u.instance_id.startswith(
                attacker_faction + "_")]

            # Generate dice rolls
            dice_rolls = generate_combat_rolls_for_units(
                attacker_units, defender_units, unit_defs)

            return initiate_combat(state.current_faction, territory_id, dice_rolls)
    except ValueError:
        print("Invalid input")

    return None


def prompt_continue_combat(state, unit_defs, territory_defs, faction_defs):
    """Handle continue_combat or retreat during active combat."""
    if not state.active_combat:
        return None

    combat = state.active_combat
    territory = state.territories[combat.territory_id]

    # Count remaining attackers and defenders
    attacker_count = len(combat.attacker_instance_ids)
    defender_count = len(
        [u for u in territory.units if u.instance_id not in combat.attacker_instance_ids])

    print(f"\n--- Active Combat in {combat.territory_id} ---")
    print(f"Round {combat.round_number}")
    print(f"Attackers remaining: {attacker_count}")
    print(f"Defenders remaining: {defender_count}")

    retreat_opts = get_retreat_options(state, territory_defs, faction_defs)

    print("\nOptions:")
    print("  1. Continue fighting")
    if retreat_opts:
        print("  2. Retreat")
    print("  0. (Wait - no action)")

    try:
        choice = input("\nChoice: ").strip()

        if choice == "1":
            # Get remaining units from active combat for dice rolls
            attacker_units = [
                u for u in territory.units if u.instance_id in combat.attacker_instance_ids]
            defender_units = [
                u for u in territory.units if u.instance_id not in combat.attacker_instance_ids]

            # Generate dice rolls for this round
            dice_rolls = generate_combat_rolls_for_units(
                attacker_units, defender_units, unit_defs)

            return continue_combat(state.current_faction, dice_rolls)
        elif choice == "2" and retreat_opts:
            print("\nRetreat to:")
            for i, tid in enumerate(retreat_opts):
                owner = state.territories[tid].owner or "neutral"
                print(f"  {i+1}. {tid} ({owner})")

            dest_choice = input("\nSelect destination #: ").strip()
            dest_idx = int(dest_choice) - 1
            if 0 <= dest_idx < len(retreat_opts):
                return retreat(state.current_faction, retreat_opts[dest_idx])
    except ValueError:
        print("Invalid input")

    return None


def prompt_mobilize(state, unit_defs, territory_defs, faction_defs):
    """Handle mobilize_units action."""
    purchased = get_purchased_units(state, state.current_faction)
    territories = get_mobilization_territories(
        state, state.current_faction, territory_defs)

    if not purchased:
        print("No units to mobilize.")
        return None

    if not territories:
        print("No strongholds available for mobilization.")
        return None

    print("\n--- Mobilize Units ---")
    print("Purchased units waiting:")
    for i, p in enumerate(purchased):
        print(f"  {i+1}. {p['count']}x {p['unit_id']}")

    print("\nMobilization territories (with power capacity):")
    for i, tid in enumerate(territories):
        td = territory_defs.get(tid)
        power_cap = td.produces.get("power", 0) if td else 0
        print(f"  {i+1}. {tid} (power: {power_cap})")

    try:
        choice = input("\nSelect territory # (0 to cancel): ").strip()
        if choice == "0" or choice == "":
            return None

        idx = int(choice) - 1
        if not (0 <= idx < len(territories)):
            print("Invalid territory selection")
            return None

        destination = territories[idx]

        # Ask which units and how many to mobilize
        print(f"\nMobilizing to {destination}. Select units:")
        unit_stacks = []

        for p in purchased:
            if p["count"] == 0:
                continue

            unit_def = unit_defs.get(p["unit_id"])
            unit_name = unit_def.display_name if unit_def else p["unit_id"]

            count_str = input(
                f"  How many {unit_name}? (max {p['count']}, Enter for all): ").strip()

            if count_str == "":
                count = p["count"]
            else:
                count = int(count_str)

            if count > 0:
                count = min(count, p["count"])  # Cap at available
                unit_stacks.append({"unit_id": p["unit_id"], "count": count})

        if unit_stacks:
            return mobilize_units(state.current_faction, destination, unit_stacks)
        else:
            print("No units selected")
            return None

    except ValueError:
        print("Invalid input")

    return None


def main_loop():
    """Main game loop."""
    print("\n" + "=" * 60)
    print("  MIDDLE EARTH: TURN-BASED STRATEGY")
    print("  CLI Test Interface")
    print("=" * 60)

    # Load definitions and initialize with starting units
    unit_defs, territory_defs, faction_defs = load_static_definitions()
    starting_setup = get_default_starting_setup()
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs, starting_setup)

    print("\nGame initialized!")
    print(f"Factions: {', '.join(faction_defs.keys())}")
    print(f"Territories: {', '.join(territory_defs.keys())}")

    while True:
        clear_screen()
        print_header(state, faction_defs)
        print_resources(state)
        print_territories(state, territory_defs, faction_defs)
        print_available_actions(state)

        if state.winner:
            print("\nGame over! Press Enter to exit.")
            input()
            break

        # Show action menu based on phase and state
        available = get_available_action_types(state)

        print("\n--- Actions ---")
        menu = []

        if state.active_combat:
            menu.append(("c", "Continue combat / Retreat"))
        else:
            if "purchase_units" in available:
                menu.append(("p", "Purchase units"))
            if "move_units" in available:
                menu.append(("m", "Move units"))
            if "initiate_combat" in available:
                contested = get_contested_territories(
                    state, state.current_faction, faction_defs)
                if contested:
                    menu.append(
                        ("c", f"Initiate combat ({len(contested)} battles)"))
            if "mobilize_units" in available:
                purchased = get_purchased_units(state, state.current_faction)
                if purchased:
                    menu.append(("b", "Mobilize purchased units"))

        if "end_phase" in available and not state.active_combat:
            menu.append(("e", "End phase"))
        if "end_turn" in available:
            menu.append(("t", "End turn"))

        menu.append(("q", "Quit game"))
        menu.append(("s", "Save game"))
        menu.append(("?", "Show detailed unit info"))

        for key, desc in menu:
            print(f"  [{key}] {desc}")

        choice = input("\nAction: ").strip().lower()

        action = None

        if choice == "q":
            print("Thanks for playing!")
            break
        elif choice == "s":
            filename = input(
                "Save filename (default: save.json): ").strip() or "save.json"
            state.save(filename)
            print(f"Game saved to {filename}")
            input("Press Enter to continue...")
            continue
        elif choice == "?":
            # Show detailed territory/unit info
            tid = input("Territory ID (or Enter for all): ").strip()
            if tid and tid in state.territories:
                units = get_territory_units(state, tid)
                print(f"\nUnits in {tid}:")
                for u in units:
                    print(
                        f"  {u['instance_id']}: hp={u['remaining_health']}/{u['base_health']}, mv={u['remaining_movement']}/{u['base_movement']}")
            input("Press Enter to continue...")
            continue
        elif choice == "p" and not state.active_combat:
            action = prompt_purchase(
                state, unit_defs, territory_defs, faction_defs)
        elif choice == "m" and not state.active_combat:
            action = prompt_move(
                state, unit_defs, territory_defs, faction_defs)
        elif choice == "c":
            if state.active_combat:
                action = prompt_continue_combat(
                    state, unit_defs, territory_defs, faction_defs)
            else:
                action = prompt_combat(
                    state, unit_defs, territory_defs, faction_defs)
        elif choice == "b" and not state.active_combat:
            action = prompt_mobilize(
                state, unit_defs, territory_defs, faction_defs)
        elif choice == "e" and not state.active_combat:
            action = end_phase(state.current_faction)
        elif choice == "t":
            action = end_turn(state.current_faction)

        # Apply action if we have one
        if action:
            # Validate first
            result = validate_action(
                state, action, unit_defs, territory_defs, faction_defs)
            if not result.valid:
                print(f"\nInvalid action: {result.error}")
                input("Press Enter to continue...")
                continue

            try:
                state, events = apply_action(
                    state, action, unit_defs, territory_defs, faction_defs)

                # Show events
                if events:
                    print("\n--- Events ---")
                    for e in events:
                        if e.type == "combat_round_resolved":
                            p = e.payload
                            print(
                                f"  Combat round {p['round_number']}: {p['attacker_hits']} attacker hits, {p['defender_hits']} defender hits")
                            if p['attacker_casualties']:
                                print(
                                    f"    Attacker lost: {', '.join(p['attacker_casualties'])}")
                            if p.get('attacker_wounded'):
                                print(
                                    f"    Attacker wounded: {', '.join(p['attacker_wounded'])}")
                            if p['defender_casualties']:
                                print(
                                    f"    Defender lost: {', '.join(p['defender_casualties'])}")
                            if p.get('defender_wounded'):
                                print(
                                    f"    Defender wounded: {', '.join(p['defender_wounded'])}")
                        elif e.type == "combat_ended":
                            p = e.payload
                            print(
                                f"  Combat ended in {p['territory']}: {p['winner']} wins!")
                        elif e.type == "territory_captured":
                            p = e.payload
                            print(
                                f"  {p['new_owner']} captured {p['territory']}!")
                        elif e.type == "victory":
                            p = e.payload
                            print(
                                f"  *** {p['winner'].upper()} ALLIANCE WINS! ***")
                        elif e.type == "units_moved":
                            p = e.payload
                            print(
                                f"  Moved {len(p['unit_ids'])} units to {p['to_territory']}")
                        elif e.type == "units_purchased":
                            p = e.payload
                            print(f"  Purchased units")
                        elif e.type == "income_collected":
                            p = e.payload
                            inc_str = ", ".join(
                                f"{v} {k}" for k, v in p['income'].items())
                            print(
                                f"  {p['faction']} collected income: {inc_str}")
                        elif e.type in ["phase_changed", "turn_started", "turn_ended"]:
                            pass  # Header will show this
                        else:
                            print(f"  {e.type}: {e.payload}")

                    input("\nPress Enter to continue...")

            except Exception as ex:
                print(f"\nError: {ex}")
                input("Press Enter to continue...")


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\n\nGame interrupted. Goodbye!")
        sys.exit(0)
