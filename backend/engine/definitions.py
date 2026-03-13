"""
Static definitions for units, territories, and factions.
All setup data lives under data/setups/<setup_id>/: territories.json, factions.json, units.json,
camps.json, starting_setup.json, and optional manifest.json (display_name, map_asset for frontend).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Resolve to absolute path so list_setups() finds data/setups regardless of process cwd.
# Try multiple candidates: backend/data when run from repo root, or cwd-relative when run from backend/
_definitions_file = Path(__file__).resolve()
_backend_engine = _definitions_file.parent
_backend_dir = _backend_engine.parent
_candidates = [
    _backend_dir / "data",
    Path.cwd() / "backend" / "data",
    Path.cwd() / "data",
]
_DATA_DIR = next((p for p in _candidates if (p / "setups").is_dir()), _backend_dir / "data")
SETUPS_DIR = _DATA_DIR / "setups"
DATA_DIR = _DATA_DIR


def _default_setup_id() -> str:
    """Single place for default: backend.config.DEFAULT_SETUP_ID."""
    from backend.config import DEFAULT_SETUP_ID
    return DEFAULT_SETUP_ID


def _setup_dir(setup_id: str) -> Path:
    return SETUPS_DIR / setup_id


def list_setups() -> list[dict]:
    """Return [{ id, display_name, map_asset, context }, ...] for setups that have a manifest with context (real scenarios only)."""
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
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        ctx = m.get("context")
        if not isinstance(ctx, dict) or not ctx:
            continue
        entry = {
            "id": m.get("id", setup_id),
            "display_name": m.get("display_name", setup_id),
            "map_asset": m.get("map_asset", setup_id),
            "context": ctx,
        }
        out.append(entry)
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
    transport_capacity: int = 0  # Naval units: how many land units they can carry
    transportable: bool = True   # If False (e.g. War Mumakil), cannot be carried by naval transport
    downgrade_to: Optional[str] = None
    specials: list[str] = field(default_factory=list)
    home_territory_id: Optional[str] = None  # When "home" in specials: single territory (backward compat)
    home_territory_ids: Optional[list[str]] = None  # Multiple home territories: can deploy 1 per territory per mobilization


@dataclass
class TerritoryDefinition:
    """Defines immutable properties of a territory."""
    id: str
    display_name: str
    terrain_type: str  # "plains", "forest", "mountain", "city"
    adjacent: list[str]  # IDs of adjacent territories (ground movement)
    produces: dict[str, int]  # {"power": 3} - production per turn
    is_stronghold: bool = False
    # False for wastelands/neutral territories (no ownership change)
    ownable: bool = True
    # Additional adjacent territories for aerial units only (e.g. fly over mountains/rivers)
    aerial_adjacent: list[str] = field(default_factory=list)


@dataclass
class CampDefinition:
    """Defines a camp (mobilization point) in a territory. Destroyed when the territory is captured or liberated."""
    id: str
    territory_id: str  # Which territory this camp is in


@dataclass
class PortDefinition:
    """Defines a port (naval mobilization point) in a territory. Immutable, not destroyed on conquest."""
    id: str
    territory_id: str  # Which territory this port is in


@dataclass
class FactionDefinition:
    """Defines immutable properties of a faction."""
    id: str
    display_name: str
    alliance: str  # "good" or "evil"
    capital: str  # territory_id
    color: str
    icon: Optional[str] = None  # Filename in frontend/assets/factions/


def _parse_home_territories(data: dict) -> dict:
    """Return home_territory_id and home_territory_ids from unit data (supports single or list)."""
    ids = data.get("home_territory_ids")
    single = data.get("home_territory_id")
    if isinstance(ids, list):
        ids = [x for x in ids if isinstance(x, str)]
        return {"home_territory_ids": ids if ids else None, "home_territory_id": ids[0] if ids else None}
    if single is not None and isinstance(single, str):
        return {"home_territory_id": single, "home_territory_ids": [single]}
    return {"home_territory_id": None, "home_territory_ids": None}


def load_static_definitions(
    data_dir: Path | str | None = None,
    setup_id: str | None = None,
) -> tuple[
    dict[str, UnitDefinition],
    dict[str, TerritoryDefinition],
    dict[str, FactionDefinition],
    dict[str, CampDefinition],
    dict[str, "PortDefinition"],
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
            transportable=data.get("transportable", True),
            downgrade_to=data.get("downgrade_to"),
            specials=data.get("specials", []),
            **_parse_home_territories(data),
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
            aerial_adjacent=data.get("aerial_adjacent", []),
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
    # Load ports (naval mobilization points; immutable, not destroyed on conquest)
    ports = {}
    ports_path = data_dir / "ports.json"
    if ports_path.exists():
        with open(ports_path, "r") as f:
            ports_data = json.load(f)
        for port_id, data in ports_data.items():
            ports[port_id] = PortDefinition(
                id=data["id"],
                territory_id=data["territory_id"],
            )
    return units, territories, factions, camps, ports


def definitions_from_snapshot(snapshot: dict) -> tuple[
    dict[str, UnitDefinition],
    dict[str, TerritoryDefinition],
    dict[str, FactionDefinition],
    dict[str, CampDefinition],
    dict[str, "PortDefinition"],
]:
    """
    Build definition dicts from a snapshot (e.g. stored in game config).
    Snapshot must have keys: units, territories, factions, camps, ports (each id -> dict of fields).
    Used so a game always uses the definitions it was created with.
    """
    units_data = snapshot.get("units") or {}
    territories_data = snapshot.get("territories") or {}
    factions_data = snapshot.get("factions") or {}
    camps_data = snapshot.get("camps") or {}
    ports_data = snapshot.get("ports") or {}

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
            transportable=data.get("transportable", True),
            downgrade_to=data.get("downgrade_to"),
            specials=data.get("specials", []),
            **_parse_home_territories(data),
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
            aerial_adjacent=data.get("aerial_adjacent", []),
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

    ports = {}
    for port_id, data in ports_data.items():
        ports[port_id] = PortDefinition(
            id=data["id"],
            territory_id=data["territory_id"],
        )

    return units, territories, factions, camps, ports


def load_specials(
    data_dir: Path | str | None = None,
    setup_id: str | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """
    Load specials.json from a setup directory. Returns (definitions, order).

    definitions: id -> { "name": str, "description": str }
    order: list of special ids for display order (from file "order" key, or sorted keys if missing).

    If specials.json is missing or invalid, returns ({}, []).
    """
    if data_dir is not None:
        path = Path(data_dir) / "specials.json"
    elif setup_id is not None:
        path = _setup_dir(setup_id) / "specials.json"
    else:
        path = _setup_dir(_default_setup_id()) / "specials.json"
    if not path.exists():
        return {}, []
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    order = data.get("order")
    if isinstance(order, list):
        order = [k for k in order if isinstance(k, str)]
    else:
        order = None
    definitions = {
        k: {
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "display_code": v.get("display_code", ""),
        }
        for k, v in data.items()
        if k != "order" and isinstance(v, dict)
    }
    if not order:
        order = sorted(definitions.keys())
    return definitions, order


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
