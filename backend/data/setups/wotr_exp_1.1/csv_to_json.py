#!/usr/bin/env python3
"""
Generate units.json, territories.json, and update starting_setup.json from the two CSVs.
Uses exact column order; types in parentheses are not part of field names.

wotr_exp_1.1: prefers exp_units.csv + exp_terr.csv; falls back to legacy "Baggins & Allies - …" names.
"""
import csv
import json
import re
import sys
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent

UNITS_CSV_NAMES = ("exp_units.csv", "Baggins & Allies - exp units csv.csv")
TERR_CSV_NAMES = ("exp_terr.csv", "Baggins & Allies - exp terr csv.csv")


def _first_existing_csv(*candidates: str) -> Path | None:
    for name in candidates:
        p = SETUP_DIR / name
        if p.is_file():
            return p
    return None


def _require_units_csv() -> Path:
    p = _first_existing_csv(*UNITS_CSV_NAMES)
    if p is None:
        print(
            f"ERROR: No units CSV in {SETUP_DIR}. Tried: {', '.join(UNITS_CSV_NAMES)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return p


def parse_list(s: str) -> list[str]:
    if not s or not s.strip():
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def parse_bool(s: str) -> bool:
    return str(s).strip().upper() == "TRUE"


def parse_int(s: str, default: int | None = None) -> int | None:
    s = str(s).strip()
    if s in ("", "-"):
        return default
    try:
        return int(s)
    except ValueError:
        return default


def load_units_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        # Column names: id, display_name, faction, archetype, attack, defense, rolls, moves, health, transport, purchasable, cost power, tags, specials, home_territory_ids, icon (no transportable; derived from tags)
        for row in reader:
            if len(row) < 16:
                row.extend([""] * (16 - len(row)))
            (
                id_,
                display_name,
                faction,
                archetype,
                attack,
                defense,
                rolls,
                moves,
                health,
                transport,
                purchasable,
                cost_power,
                tags,
                specials,
                home_territory_ids,
                icon,
            ) = row[0:16]
            cost_power_val = parse_int(cost_power, 0)
            if cost_power_val is None:
                cost_power_val = 0
            tags_list = parse_list(tags) if tags else []
            specials_list = parse_list(specials) if specials else []
            home_list = parse_list(home_territory_ids) if home_territory_ids else []
            transport_val = parse_int(transport, 0)
            if transport_val is None:
                transport_val = 0
            units_entry = {
                "id": id_.strip(),
                "display_name": display_name.strip(),
                "faction": faction.strip() if faction else "",
                "archetype": archetype.strip(),
                "attack": int(attack.strip()) if attack.strip() else 0,
                "defense": int(defense.strip()) if defense.strip() else 0,
                "dice": int(rolls.strip()) if rolls.strip() else 1,
                "movement": int(moves.strip()) if moves.strip() else 0,
                "health": int(health.strip()) if health.strip() else 1,
                "transport_capacity": transport_val,
                "purchasable": parse_bool(purchasable),
                "cost": {"power": cost_power_val},
                "tags": tags_list,
                "specials": specials_list,
                "icon": (icon.strip() or f"{id_.strip()}.png") if id_ else "",
            }
            if home_list:
                units_entry["home_territory_ids"] = home_list
            rows.append(units_entry)
    return rows


def load_territories_csv(path: Path) -> tuple[list[dict], dict, dict]:
    """Returns (territory_rows, territory_owners for starting_setup, starting_units for starting_setup)."""
    rows = []
    owners = {}  # territory_id -> faction
    units_by_territory = {}  # territory_id -> [{"unit_id": str, "count": int}, ...]
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            # CSV: id, display_name, terrain_type, produces_power, is_stronghold,
            #      stronghold_base_health, ownable, adjacent, aerial_adjacent, starting_setup_faction, starting_setup_units
            if len(row) < 11:
                row.extend([""] * (11 - len(row)))
            (
                id_,
                display_name,
                terrain_type,
                produces_power,
                is_stronghold,
                stronghold_base_health,
                ownable,
                adjacent,
                aerial_adjacent,
                starting_setup_faction,
                starting_setup_units,
            ) = row[0:11]
            id_ = id_.strip()
            if not id_:
                continue
            # Skip duplicate typo row: sea_zone12 (no underscore) duplicates sea_zone_12
            if id_ == "sea_zone12":
                continue
            # Produces: dict with key "power" (int). "-" or empty -> 0 for sea
            p = parse_int(produces_power, 0)
            if p is None:
                p = 0
            produces = {"power": p} if p > 0 else {}
            adj_list = parse_list(adjacent) if adjacent else []
            aero_list = parse_list(aerial_adjacent) if aerial_adjacent else []
            # Fix typo: helps_deep -> helms_deep
            if "helps_deep" in adj_list:
                adj_list = [x if x != "helps_deep" else "helms_deep" for x in adj_list]
            is_sh = parse_bool(is_stronghold)
            sh = parse_int(stronghold_base_health, None)
            # Key order matches CSV: id, display_name, terrain_type, produces, is_stronghold,
            # stronghold_base_health, ownable, adjacent, aerial_adjacent
            terr_entry: dict = {
                "id": id_,
                "display_name": display_name.strip(),
                "terrain_type": terrain_type.strip() or "plains",
                "produces": produces,
                "is_stronghold": is_sh,
            }
            if is_sh and sh is not None and sh > 0:
                terr_entry["stronghold_base_health"] = sh
            terr_entry["ownable"] = parse_bool(ownable)
            terr_entry["adjacent"] = adj_list
            terr_entry["aerial_adjacent"] = aero_list
            rows.append(terr_entry)
            if starting_setup_faction and starting_setup_faction.strip():
                owners[id_] = starting_setup_faction.strip()
            if starting_setup_units and starting_setup_units.strip():
                # Parse "2 hobbit" or "1 rivendell_knight, 3 rivendell_warrior"
                parts = [p.strip() for p in starting_setup_units.split(",") if p.strip()]
                unit_list = []
                for part in parts:
                    m = re.match(r"^(\d+)\s+(.+)$", part)
                    if m:
                        count = int(m.group(1))
                        unit_id = m.group(2).strip().replace(" ", "_")  # fix "easterling pikeman" -> easterling_pikeman
                        unit_list.append({"unit_id": unit_id, "count": count})
                if unit_list:
                    units_by_territory[id_] = unit_list
    return rows, owners, units_by_territory


def main():
    units_path = _require_units_csv()
    print(f"Using units CSV: {units_path.name}")

    # Units
    units_rows = load_units_csv(units_path)
    units_by_id = {u["id"]: u for u in units_rows}
    with open(SETUP_DIR / "units.json", "w", encoding="utf-8") as f:
        json.dump(units_by_id, f, indent=2)
    print(f"Wrote units.json with {len(units_by_id)} units")

    # Territories (optional: skip if CSV missing)
    terr_csv = _first_existing_csv(*TERR_CSV_NAMES)
    if terr_csv is not None:
        print(f"Using territories CSV: {terr_csv.name}")
        terr_rows, owners, units_by_terr = load_territories_csv(terr_csv)
        territories_by_id = {}
        for t in terr_rows:
            tid = t["id"]
            if tid not in territories_by_id:  # first occurrence only to avoid sea_zone12 overwriting sea_zone_12
                territories_by_id[tid] = t
        with open(SETUP_DIR / "territories.json", "w", encoding="utf-8") as f:
            json.dump(territories_by_id, f, indent=2)
        print(f"Wrote territories.json with {len(territories_by_id)} territories")

        # Starting setup: keep turn_order and structure, replace territory_owners and starting_units
        setup_path = SETUP_DIR / "starting_setup.json"
        with open(setup_path, "r", encoding="utf-8") as f:
            setup = json.load(f)
        setup["territory_owners"] = owners
        setup["starting_units"] = {
            tid: units for tid, units in units_by_terr.items()
        }
        with open(setup_path, "w", encoding="utf-8") as f:
            json.dump(setup, f, indent=2)
        print(f"Updated starting_setup.json: {len(owners)} territory owners, {len(units_by_terr)} territories with starting units")
    else:
        print("Skipped territories and starting_setup (territories CSV not found)")


if __name__ == "__main__":
    main()
