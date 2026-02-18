"""
Static definitions for units, territories, and factions.
All game logic references these definitions.
Loads data from JSON files in the data/ folder.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Path to data folder (relative to this file's location)
DATA_DIR = Path(__file__).parent.parent / "data"


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
    ownable: bool = True  # False for wastelands/neutral territories (no ownership change)


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
    data_dir: Path | str | None = None
) -> tuple[
    dict[str, UnitDefinition],
    dict[str, TerritoryDefinition],
    dict[str, FactionDefinition]
]:
    """
    Load all static game definitions from JSON files.
    
    Args:
        data_dir: Path to data directory. Defaults to ../data relative to this file.
    
    Returns: (unit_definitions, territory_definitions, faction_definitions)
    """
    if data_dir is None:
        data_dir = DATA_DIR
    else:
        data_dir = Path(data_dir)

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

    return units, territories, factions


def load_starting_setup(data_dir: Path | str | None = None) -> dict:
    """
    Load starting game setup from JSON file.
    
    Args:
        data_dir: Path to data directory. Defaults to ../data relative to this file.
    
    Returns: Starting setup dictionary with turn_order, territory_owners, starting_units
    """
    if data_dir is None:
        data_dir = DATA_DIR
    else:
        data_dir = Path(data_dir)

    with open(data_dir / "starting_setup.json", "r") as f:
        return json.load(f)
