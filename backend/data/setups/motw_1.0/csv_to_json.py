#!/usr/bin/env python3
"""
Generate JSON setup files for motw_1.0 from CSVs (same column layout as wotr_1.1).

Place in this directory (any of the listed names):
  units.csv or motw_units.csv — columns (row 1 = header):
    id, display_name, faction, archetype, attack, defense, rolls, moves, health,
    transport, purchasable, cost power, tags, specials, home_territory_ids, icon

  terr.csv, territories.csv, or motw_terr.csv — columns:
    id, display_name, terrain_type, produces_power, is_stronghold,
    stronghold_base_health, ownable, adjacent, aerial_adjacent,
    starting_setup_faction, starting_setup_units

starting_setup_units examples: "2 hobbit" or "1 rivendell_knight, 3 rivendell_warrior"

Run from repo root or this folder:
  python3 backend/data/setups/motw_1.0/csv_to_json.py
  python3 backend/data/setups/motw_1.0/csv_to_json.py --units-only   # only units.json (ignore terr CSV if present)

If no terr.csv / territories.csv / motw_terr.csv is present, only units.json is written (no error).

Writes (when territory CSV exists): units.json, territories.json, starting_setup.json, factions.json
Always writes units.json when a units CSV exists.
Does not overwrite: manifest.json, specials.json, camps.json, ports.json (edit those by hand).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parent

UNITS_CSV_NAMES = ("units.csv", "motw_units.csv")
TERR_CSV_NAMES = ("terr.csv", "territories.csv", "motw_terr.csv")

# Default palette for auto-generated faction colors (edit factions.json after if needed)
_FACTION_COLOR_CYCLE = (
    "#2d5a27",
    "#265399",
    "#8b2500",
    "#c9a227",
    "#2a2a2a",
    "#5b3a8c",
    "#8b4513",
    "#1a6e6e",
)


def _first_existing_csv(*candidates: str) -> Path | None:
    for name in candidates:
        p = SETUP_DIR / name
        if p.is_file():
            return p
    return None


def parse_list(s: str) -> list[str]:
    if not s or not str(s).strip():
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]


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
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
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
            id_ = id_.strip()
            if not id_:
                continue
            cost_power_val = parse_int(cost_power, 0)
            if cost_power_val is None:
                cost_power_val = 0
            tags_list = parse_list(tags) if tags else []
            specials_list = parse_list(specials) if specials else []
            home_list = parse_list(home_territory_ids) if home_territory_ids else []
            transport_val = parse_int(transport, 0)
            if transport_val is None:
                transport_val = 0
            units_entry: dict = {
                "id": id_,
                "display_name": display_name.strip(),
                "faction": faction.strip() if faction else "",
                "archetype": archetype.strip(),
                "attack": int(attack.strip()) if str(attack).strip() else 0,
                "defense": int(defense.strip()) if str(defense).strip() else 0,
                "dice": int(rolls.strip()) if str(rolls).strip() else 1,
                "movement": int(moves.strip()) if str(moves).strip() else 0,
                "health": int(health.strip()) if str(health).strip() else 1,
                "transport_capacity": transport_val,
                "purchasable": parse_bool(purchasable),
                "cost": {"power": cost_power_val},
                "tags": tags_list,
                "specials": specials_list,
                "icon": (icon.strip() or f"{id_}.png"),
            }
            if home_list:
                units_entry["home_territory_ids"] = home_list
            rows.append(units_entry)
    return rows


def load_territories_csv(path: Path) -> tuple[list[dict], dict[str, str], dict[str, list[dict]], list[str]]:
    """territory rows, owners, starting units per territory, turn_order (faction first-seen in CSV)."""
    rows: list[dict] = []
    owners: dict[str, str] = {}
    units_by_territory: dict[str, list[dict]] = {}
    turn_order: list[str] = []
    seen_faction: set[str] = set()

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
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
            p = parse_int(produces_power, 0)
            if p is None:
                p = 0
            produces = {"power": p} if p > 0 else {}
            adj_list = parse_list(adjacent) if adjacent else []
            aero_list = parse_list(aerial_adjacent) if aerial_adjacent else []
            is_sh = parse_bool(is_stronghold)
            sh = parse_int(stronghold_base_health, None)
            terr_entry: dict = {
                "id": id_,
                "display_name": display_name.strip(),
                "terrain_type": (terrain_type.strip() or "plains"),
                "produces": produces,
                "is_stronghold": is_sh,
            }
            if is_sh and sh is not None and sh > 0:
                terr_entry["stronghold_base_health"] = sh
            terr_entry["ownable"] = parse_bool(ownable)
            terr_entry["adjacent"] = adj_list
            terr_entry["aerial_adjacent"] = aero_list
            rows.append(terr_entry)

            fam = starting_setup_faction.strip() if starting_setup_faction else ""
            if fam:
                owners[id_] = fam
                if fam not in seen_faction:
                    seen_faction.add(fam)
                    turn_order.append(fam)

            if starting_setup_units and str(starting_setup_units).strip():
                parts = [p.strip() for p in str(starting_setup_units).split(",") if p.strip()]
                unit_list: list[dict] = []
                for part in parts:
                    m = re.match(r"^(\d+)\s+(.+)$", part)
                    if m:
                        count = int(m.group(1))
                        unit_id = m.group(2).strip().replace(" ", "_")
                        unit_list.append({"unit_id": unit_id, "count": count})
                if unit_list:
                    units_by_territory[id_] = unit_list

    return rows, owners, units_by_territory, turn_order


def _faction_ids_from_units(units: list[dict]) -> set[str]:
    out: set[str] = set()
    for u in units:
        f = (u.get("faction") or "").strip()
        if f:
            out.add(f)
    return out


def build_factions_json(
    faction_ids: set[str],
    owners: dict[str, str],
    turn_order: list[str],
) -> dict[str, dict]:
    """Infer capitals = first territory in CSV order owned by each faction."""
    capital_by_faction: dict[str, str] = {}
    for tid, fac in owners.items():
        if fac and fac not in capital_by_faction:
            capital_by_faction[fac] = tid

    ordered = list(turn_order)
    for fid in sorted(faction_ids):
        if fid not in ordered:
            ordered.append(fid)

    out: dict[str, dict] = {}
    color_idx = 0
    for fid in ordered:
        if fid == "neutral":
            out[fid] = {
                "id": "neutral",
                "display_name": "Neutral",
                "alliance": "neutral",
                "capital": "",
                "color": "#888888",
                "icon": "neutral.png",
            }
            continue
        color = _FACTION_COLOR_CYCLE[color_idx % len(_FACTION_COLOR_CYCLE)]
        color_idx += 1
        cap = capital_by_faction.get(fid, "")
        display = fid.replace("_", " ").strip().title() or fid
        out[fid] = {
            "id": fid,
            "display_name": display,
            "alliance": "good",
            "capital": cap,
            "color": color,
            "icon": f"{fid}.png",
            "music": [],
        }
    return out


def main() -> None:
    units_only = "--units-only" in sys.argv

    units_path = _first_existing_csv(*UNITS_CSV_NAMES)
    if units_path is None:
        print(f"ERROR: No units CSV in {SETUP_DIR}. Expected one of: {UNITS_CSV_NAMES}", file=sys.stderr)
        sys.exit(1)
    print(f"Using units CSV: {units_path.name}")
    units_rows = load_units_csv(units_path)
    if not units_rows:
        print("ERROR: No unit rows after header.", file=sys.stderr)
        sys.exit(1)
    units_by_id = {u["id"]: u for u in units_rows}
    with open(SETUP_DIR / "units.json", "w", encoding="utf-8") as f:
        json.dump(units_by_id, f, indent=2)
    print(f"Wrote units.json ({len(units_by_id)} units)")

    if units_only:
        print("Skipping territories, starting_setup, factions (--units-only).")
        return

    terr_csv = _first_existing_csv(*TERR_CSV_NAMES)
    if terr_csv is None:
        print(
            f"No territories CSV (expected one of: {TERR_CSV_NAMES}); "
            "skipping territories.json, starting_setup.json, factions.json."
        )
        return
    print(f"Using territories CSV: {terr_csv.name}")
    terr_rows, owners, units_by_terr, turn_order = load_territories_csv(terr_csv)
    # Neutral is a faction for units / ownership, not a seat in turn_order (matches wotr setups).
    turn_order = [f for f in turn_order if f != "neutral"]
    territories_by_id: dict[str, dict] = {}
    for t in terr_rows:
        tid = t["id"]
        if tid not in territories_by_id:
            territories_by_id[tid] = t
    with open(SETUP_DIR / "territories.json", "w", encoding="utf-8") as f:
        json.dump(territories_by_id, f, indent=2)
    print(f"Wrote territories.json ({len(territories_by_id)} territories)")

    faction_ids = _faction_ids_from_units(units_rows) | set(owners.values())
    faction_ids.discard("")
    if "neutral" in owners.values():
        faction_ids.add("neutral")

    # Append factions that appear only in units.csv (no owned territory row yet)
    for fid in sorted(faction_ids):
        if fid not in turn_order and fid != "neutral":
            turn_order.append(fid)

    setup = {
        "turn_order": turn_order,
        "territory_owners": owners,
        "starting_units": {tid: lst for tid, lst in units_by_terr.items()},
    }
    with open(SETUP_DIR / "starting_setup.json", "w", encoding="utf-8") as f:
        json.dump(setup, f, indent=2)
    print(
        f"Wrote starting_setup.json: turn_order={len(turn_order)} factions, "
        f"{len(owners)} owners, {len(units_by_terr)} territories with stacks"
    )

    factions = build_factions_json(faction_ids, owners, turn_order)
    with open(SETUP_DIR / "factions.json", "w", encoding="utf-8") as f:
        json.dump(factions, f, indent=2)
    print(f"Wrote factions.json ({len(factions)} factions) — review alliance, capitals, icons, and colors.")


if __name__ == "__main__":
    main()
