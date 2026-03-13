#!/usr/bin/env python3
"""
Read territories.csv and units.csv; update territories.json, units.json, and starting_setup.json.
Run from repo root: python backend/data/setups/wotr_exp_1.0/csv_to_json.py
"""
import csv
import json
import os
import re

SETUP_DIR = os.path.dirname(os.path.abspath(__file__))

def faction_csv_to_id(s: str):
    if not s or not s.strip():
        return None
    t = s.strip().lower().replace(" ", "_")
    if t in ("none", "n/a"):
        return None
    # CSV may have "Elves", "Gondor", etc. -> ids are lowercase
    return t

def norm_terrain(s: str) -> str:
    if not s or s == "-":
        return "plains"
    t = s.strip().lower()
    if t == "forest":
        return "forest"
    if t == "mountains":
        return "mountains"
    if t in ("port", "hills", "city", "valley", "fortress", "wasteland", "plains", "lake", "desert", "marsh", "volcano", "sea", "grassland"):
        return t
    return t.replace(" ", "_")

def parse_adjacent(s: str) -> list[str]:
    if not s or not s.strip():
        return []
    # Remove quotes and split by comma
    s = s.strip().strip('"').strip()
    return [a.strip() for a in s.split(",") if a.strip()]

def parse_starting_units(s: str) -> list[dict]:
    """Parse e.g. '2 hobbit' or '1 rivendell_knight, 2 rivendell_warrior' -> [{ unit_id, count }, ...]"""
    if not s or not s.strip():
        return []
    out = []
    # Split by comma for multiple stacks
    for part in s.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s+(.+)$", part)
        if m:
            count = int(m.group(1))
            unit_id = m.group(2).strip().lower().replace(" ", "_")
            out.append({"unit_id": unit_id, "count": count})
    return out

def load_territories_csv() -> tuple[dict, dict, dict]:
    """Returns (territories_dict, territory_owners, starting_units)."""
    path = os.path.join(SETUP_DIR, "territories.csv")
    territories = {}
    territory_owners = {}
    starting_units = {}
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            tid = row["ID"].strip()
            if not tid:
                continue
            display_name = row["Display Name"].strip()
            power_s = row["Power Production"].strip()
            power = int(power_s) if power_s and power_s != "-" else 0
            terrain = norm_terrain(row["Terrain"].strip())
            stronghold = row["Stronghold"].strip().lower() in ("yes", "1", "true")
            faction_id = faction_csv_to_id(row.get("Faction", ""))
            ownable = row["Ownable"].strip().lower() in ("yes", "1", "true")
            adjacent = parse_adjacent(row["Adjacent Territories"].strip())
            aerial = parse_adjacent(row["Aerial Adjacent"].strip())
            units = parse_starting_units(row.get("Starting Units", "").strip())

            produces = {}
            if power is not None and power > 0:
                produces["power"] = power

            territories[tid] = {
                "id": tid,
                "display_name": display_name,
                "terrain_type": terrain,
                "adjacent": adjacent,
                "produces": produces,
                "is_stronghold": stronghold,
                "ownable": ownable,
            }
            if aerial:
                territories[tid]["aerial_adjacent"] = aerial

            if faction_id:
                territory_owners[tid] = faction_id
            if units:
                starting_units[tid] = units

    return territories, territory_owners, starting_units

def load_units_csv() -> dict:
    path = os.path.join(SETUP_DIR, "units.csv")
    units = {}
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            unit_id = row["Unit ID"].strip()
            if not unit_id:
                continue
            faction_s = (row.get("Faction") or "").strip()
            if faction_s == "Free Peoples":
                faction = "elves"
            elif faction_s:
                faction = faction_s.lower().replace(" ", "_")
            else:
                faction = "neutral"  # Required: loader expects every unit to have "faction"
            display_name = (row.get("Unit") or row.get("Display Name") or unit_id).strip()
            archetype = (row.get("Archetype") or "infantry").strip().lower()
            attack = int(row.get("Attack") or 0)
            defense = int(row.get("Defense") or 0)
            dice = int(row.get("Rolls") or 1)
            movement = int(row.get("Moves") or 0)
            health = int(row.get("Health") or 1)
            cost_raw = row.get("Cost (in power)") or row.get("Cost") or 0
            try:
                cost_val = int(float(str(cost_raw).strip()))
            except (ValueError, TypeError):
                cost_val = 0
            special_s = (row.get("Special") or "").strip()

            is_naval = archetype == "naval" or "naval" in special_s.lower()
            is_aerial = archetype == "aerial"
            tags = []
            if not is_naval and not is_aerial:
                tags.append("land")
            if special_s:
                for t in special_s.split(","):
                    t = t.strip().lower().replace(" ", "_").replace("-", "_")
                    if t and t not in tags:
                        tags.append(t)
            if is_aerial and "aerial" not in tags:
                tags.append("aerial")
            if is_naval and "naval" not in tags:
                tags.append("naval")

            obj = {
                "id": unit_id,
                "display_name": display_name,
                "faction": faction,
                "archetype": archetype,
                "tags": tags,
                "attack": attack,
                "defense": defense,
                "movement": movement,
                "health": health,
                "dice": dice,
                "cost": {"power": cost_val},
                "purchasable": cost_val > 0,
                "icon": f"{unit_id}.png",
            }
            units[unit_id] = obj
    return units

def main():
    # Load existing JSONs to preserve turn_order and merge any units not in CSV
    starting_path = os.path.join(SETUP_DIR, "starting_setup.json")
    existing_starting = {}
    if os.path.exists(starting_path):
        with open(starting_path, encoding="utf-8") as f:
            existing_starting = json.load(f)

    units_path = os.path.join(SETUP_DIR, "units.json")
    existing_units = {}
    if os.path.exists(units_path):
        with open(units_path, encoding="utf-8") as f:
            existing_units = json.load(f)

    # Build from CSV
    territories, territory_owners, starting_units = load_territories_csv()
    units = load_units_csv()

    # Add any unit_id referenced in starting_units but missing from CSV (from existing or minimal stub)
    def make_stub(uid: str, faction: str | None) -> dict:
        name = uid.replace("_", " ").title()
        return {
            "id": uid,
            "display_name": name,
            "faction": faction or "neutral",
            "archetype": "infantry",
            "tags": ["land"],
            "attack": 1,
            "defense": 1,
            "movement": 1,
            "health": 1,
            "dice": 1,
            "cost": {"power": 0},
            "purchasable": False,
            "icon": f"{uid}.png",
        }
    for tid, stacks in starting_units.items():
        faction = territory_owners.get(tid)
        for s in stacks:
            uid = s["unit_id"]
            if uid not in units:
                if uid in existing_units:
                    units[uid] = existing_units[uid]
                else:
                    units[uid] = make_stub(uid, faction)

    # territories.json
    with open(os.path.join(SETUP_DIR, "territories.json"), "w", encoding="utf-8") as f:
        json.dump(territories, f, indent=4)

    # units.json
    with open(units_path, "w", encoding="utf-8") as f:
        json.dump(units, f, indent=4)

    # starting_setup.json: keep turn_order from existing, set territory_owners and starting_units from CSV
    turn_order = existing_starting.get("turn_order", [
        "isengard", "rohan", "mordor", "gondor", "rhun", "erebor", "harad", "elves"
    ])
    out_starting = {
        "turn_order": turn_order,
        "territory_owners": territory_owners,
        "starting_units": starting_units,
    }
    with open(starting_path, "w", encoding="utf-8") as f:
        json.dump(out_starting, f, indent=4)

    print("Updated territories.json, units.json, starting_setup.json from CSV.")

if __name__ == "__main__":
    main()
