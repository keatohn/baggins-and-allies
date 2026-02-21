"""
Static definitions for units, territories, and factions.
All setup data lives under data/setups/<setup_id>/: territories.json, factions.json, units.json,
camps.json, starting_setup.json, and optional manifest.json (display_name, map_asset for frontend).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"
SETUPS_DIR = DATA_DIR / "setups"


def _default_setup_id() -> str:
    """Single place for default: backend.config.DEFAULT_SETUP_ID."""
    from backend.config import DEFAULT_SETUP_ID
    return DEFAULT_SETUP_ID


def _setup_dir(setup_id: str) -> Path:
    return SETUPS_DIR / setup_id


def list_setups() -> list[dict]:
    """Return [{ id, display_name, map_asset }, ...] for all setups (subdirs of data/setups/ with starting_setup.json)."""
    out = []
    if not SETUPS_DIR.exists():
        return out
    for d in sorted(SETUPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        setup_id = d.name
        if not (d / "starting_setup.json").exists():
            continue
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, "r") as f:
                    m = json.load(f)
                out.append({
                    "id": m.get("id", setup_id),
                    "display_name": m.get("display_name", setup_id),
                    "map_asset": m.get("map_asset", setup_id),
                })
            except (json.JSONDecodeError, OSError):
                out.append({"id": setup_id, "display_name": setup_id, "map_asset": setup_id})
        else:
            out.append({"id": setup_id, "display_name": setup_id, "map_asset": setup_id})
    return out


def load_setup(setup_id: str) -> dict:
    """Load setup by id. Returns { id, display_name, map_asset, starting_setup }.
    All data read from data/setups/<setup_id>/.
    """
    setup_dir = _setup_dir(setup_id)
    if not setup_dir.exists() or not setup_dir.is_dir():
        raise FileNotFoundError(f"Setup not found: {setup_id}")
    starting_path = setup_dir / "starting_setup.json"
    if not starting_path.exists():
        raise FileNotFoundError(f"starting_setup.json not found in setup: {setup_id}")
    with open(starting_path, "r") as f:
        starting_setup = json.load(f)
    manifest_path = setup_dir / "manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, "r") as f:
                m = json.load(f)
            result = {
                "id": m.get("id", setup_id),
                "display_name": m.get("display_name", setup_id),
                "map_asset": m.get("map_asset", setup_id),
                "starting_setup": starting_setup,
            }
            vc = m.get("victory_criteria")
            if isinstance(vc, dict) and vc:
                result["victory_criteria"] = vc
            try:
                result["camp_cost"] = int(m["camp_cost"])
            except (TypeError, ValueError, KeyError):
                pass
            return result
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "id": setup_id,
        "display_name": setup_id,
        "map_asset": setup_id,
        "starting_setup": starting_setup,
    }


@dataclass
class UnitDefinition:
    """Defines immutable properties of a unit type."""
    id: str
    display_name: str
    faction: str
    archetype: str  # e.g., "infantry", "cavalry", "aerial"
    tags: list[str]
    attack: int
    defense: int
    movement: int
    health: int
    cost: dict[str, int]  # e.g., {"power": 2}
    dice: int = 1  # Number of dice rolled in combat (most units roll 1)
    purchasable: bool = True
    unique: bool = False
    icon: Optional[str] = None  # Filename in frontend/assets/units/

    # Future hooks for features not in V1
    transport_capacity: int = 0
    downgrade_to: Optional[str] = None
    specials: list[str] = field(default_factory=list)


@dataclass
class TerritoryDefinition:
    """Defines immutable properties of a territory."""
    id: str
    display_name: str
    terrain_type: str  # "plains", "forest", "mountain", "city"
    adjacent: list[str]  # IDs of adjacent territories
    produces: dict[str, int]  # {"power": 3} - production per turn
    is_stronghold: bool = False
    # False for wastelands/neutral territories (no ownership change)
    ownable: bool = True


@dataclass
class CampDefinition:
    """Defines a camp (mobilization point) in a territory. Destroyed when the territory is captured or liberated."""
    id: str
    territory_id: str  # Which territory this camp is in


@dataclass
class FactionDefinition:
    """Defines immutable properties of a faction."""
    id: str
    display_name: str
    alliance: str  # "good" or "evil"
    capital: str  # territory_id
    color: str
    icon: Optional[str] = None  # Filename in frontend/assets/factions/


def load_static_definitions(
    data_dir: Path | str | None = None,
    setup_id: str | None = None,
) -> tuple[
    dict[str, UnitDefinition],
    dict[str, TerritoryDefinition],
    dict[str, FactionDefinition],
    dict[str, CampDefinition],
]:
    """
    Load static definitions (territories, factions, units, camps).

    Args:
        data_dir: Path to directory containing the 4 JSON files.
        setup_id: If set, use data/setups/<setup_id>/ (ignored if data_dir is set).

    Returns: (unit_definitions, territory_definitions, faction_definitions, camp_definitions)
    """
    if data_dir is not None:
        data_dir = Path(data_dir)
    elif setup_id is not None:
        data_dir = _setup_dir(setup_id)
    else:
        raise ValueError("Either data_dir or setup_id must be provided")

    # Load units
    with open(data_dir / "units.json", "r") as f:
        units_data = json.load(f)

    units = {}
    for unit_id, data in units_data.items():
        units[unit_id] = UnitDefinition(
            id=data["id"],
            display_name=data["display_name"],
            faction=data["faction"],
            archetype=data["archetype"],
            tags=data["tags"],
            attack=data["attack"],
            defense=data["defense"],
            movement=data["movement"],
            health=data["health"],
            cost=data["cost"],
            dice=data.get("dice", 1),
            purchasable=data.get("purchasable", True),
            unique=data.get("unique", False),
            icon=data.get("icon"),
            transport_capacity=data.get("transport_capacity", 0),
            downgrade_to=data.get("downgrade_to"),
            specials=data.get("specials", []),
        )

    # Load territories
    with open(data_dir / "territories.json", "r") as f:
        territories_data = json.load(f)

    territories = {}
    for territory_id, data in territories_data.items():
        territories[territory_id] = TerritoryDefinition(
            id=data["id"],
            display_name=data["display_name"],
            terrain_type=data["terrain_type"],
            adjacent=data["adjacent"],
            produces=data["produces"],
            is_stronghold=data.get("is_stronghold", False),
            ownable=data.get("ownable", True),
        )

    # Load factions
    with open(data_dir / "factions.json", "r") as f:
        factions_data = json.load(f)

    factions = {}
    for faction_id, data in factions_data.items():
        factions[faction_id] = FactionDefinition(
            id=data["id"],
            display_name=data["display_name"],
            alliance=data["alliance"],
            capital=data["capital"],
            color=data["color"],
            icon=data.get("icon"),
        )

    # Load camps (mobilization points; each has a territory, destroyed when territory is captured)
    camps = {}
    camps_path = data_dir / "camps.json"
    if camps_path.exists():
        with open(camps_path, "r") as f:
            camps_data = json.load(f)
        for camp_id, data in camps_data.items():
            camps[camp_id] = CampDefinition(
                id=data["id"],
                territory_id=data["territory_id"],
            )
    return units, territories, factions, camps


def definitions_from_snapshot(snapshot: dict) -> tuple[
    dict[str, UnitDefinition],
    dict[str, TerritoryDefinition],
    dict[str, FactionDefinition],
    dict[str, CampDefinition],
]:
    """
    Build definition dicts from a snapshot (e.g. stored in game config).
    Snapshot must have keys: units, territories, factions, camps (each id -> dict of fields).
    Used so a game always uses the definitions it was created with.
    """
    units_data = snapshot.get("units") or {}
    territories_data = snapshot.get("territories") or {}
    factions_data = snapshot.get("factions") or {}
    camps_data = snapshot.get("camps") or {}

    units = {}
    for unit_id, data in units_data.items():
        units[unit_id] = UnitDefinition(
            id=data["id"],
            display_name=data["display_name"],
            faction=data["faction"],
            archetype=data["archetype"],
            tags=data.get("tags", []),
            attack=data["attack"],
            defense=data["defense"],
            movement=data["movement"],
            health=data["health"],
            cost=data["cost"],
            dice=data.get("dice", 1),
            purchasable=data.get("purchasable", True),
            unique=data.get("unique", False),
            icon=data.get("icon"),
            transport_capacity=data.get("transport_capacity", 0),
            downgrade_to=data.get("downgrade_to"),
            specials=data.get("specials", []),
        )

    territories = {}
    for territory_id, data in territories_data.items():
        territories[territory_id] = TerritoryDefinition(
            id=data["id"],
            display_name=data["display_name"],
            terrain_type=data["terrain_type"],
            adjacent=data["adjacent"],
            produces=data["produces"],
            is_stronghold=data.get("is_stronghold", False),
            ownable=data.get("ownable", True),
        )

    factions = {}
    for faction_id, data in factions_data.items():
        factions[faction_id] = FactionDefinition(
            id=data["id"],
            display_name=data["display_name"],
            alliance=data["alliance"],
            capital=data["capital"],
            color=data["color"],
            icon=data.get("icon"),
        )

    camps = {}
    for camp_id, data in camps_data.items():
        camps[camp_id] = CampDefinition(
            id=data["id"],
            territory_id=data["territory_id"],
        )

    return units, territories, factions, camps


def load_starting_setup(data_dir: Path | str | None = None, setup_id: str | None = None) -> dict:
    """
    Load starting_setup.json. Give data_dir, or setup_id, or neither to use default setup.

    Returns: Starting setup dict with turn_order, territory_owners, starting_units
    """
    if data_dir is not None:
        path = Path(data_dir) / "starting_setup.json"
    elif setup_id is not None:
        path = _setup_dir(setup_id) / "starting_setup.json"
    else:
        return load_setup(_default_setup_id())["starting_setup"]
    with open(path, "r") as f:
        return json.load(f)
