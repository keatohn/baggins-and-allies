"""
Static definitions for units, territories, and factions.
All setup data lives under data/setups/<setup_id>/: territories.json, factions.json, units.json,
camps.json, starting_setup.json, and optional manifest.json (display_name, map_asset for frontend).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


_TIMELINE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def timeline_image_filename(manifest: dict[str, Any]) -> str | None:
    """Basename of a create-game timeline image under public/assets/scenarios, or None."""
    raw = manifest.get("timeline_image")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or name != Path(name).name or name in {".", ".."}:
        return None
    if Path(name).suffix.lower() not in _TIMELINE_IMAGE_EXTS:
        return None
    return name


def alliance_counts(context: dict[str, Any], factions: dict[str, Any]) -> tuple[int, int]:
    """Good and evil counts for the factions named in manifest context."""
    by_label: dict[str, str] = {}
    for fid, raw in factions.items():
        if not isinstance(raw, dict):
            continue
        alliance = str(raw.get("alliance") or "").strip().lower()
        by_label[str(fid)] = alliance
        display = raw.get("display_name")
        if isinstance(display, str) and display.strip():
            by_label[display.strip()] = alliance
    names = context.get("factions")
    listed = [name.strip() for name in names if isinstance(name, str) and name.strip()] if isinstance(names, list) else []
    good = evil = 0
    if listed:
        for name in listed:
            alliance = by_label.get(name)
            if alliance == "good":
                good += 1
            elif alliance == "evil":
                evil += 1
        return good, evil
    for raw in factions.values():
        if not isinstance(raw, dict):
            continue
        alliance = str(raw.get("alliance") or "").strip().lower()
        if alliance == "good":
            good += 1
        elif alliance == "evil":
            evil += 1
    return good, evil


def scenario_menu_entry(
    manifest: dict[str, Any],
    folder_id: str,
    factions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One create-game menu row. Includes timeline_image when the manifest names a scenario asset."""
    sid = manifest.get("id", folder_id)
    if not isinstance(sid, str) or not sid:
        sid = folder_id
    ctx = manifest.get("context")
    if isinstance(ctx, dict):
        ctx = dict(ctx)
        if isinstance(factions, dict):
            good, evil = alliance_counts(ctx, factions)
            ctx["good_count"] = good
            ctx["evil_count"] = evil
    entry: dict[str, Any] = {
        "id": sid,
        "display_name": manifest.get("display_name", folder_id),
        "map_asset": manifest.get("map_asset", folder_id),
        "context": ctx,
    }
    image = timeline_image_filename(manifest)
    if image is not None:
        entry["timeline_image"] = image
    return entry


def menu_order_sort_value(manifest: dict[str, Any]) -> int:
    """
    Create-game menu / GET /setups ordering: lower values list first.
    Defaults to 1000 when omitted so explicit small orders (0, 1, …) float to the top.
    Non-numeric values are treated as 1000.
    """
    raw = manifest.get("menu_order")
    if raw is None:
        return 1000
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1000


def parse_prefire_penalty_from_manifest(raw: Any) -> bool:
    """
    Manifest `prefire_penalty` is a boolean: True applies -1 to stealth/archer prefire, False uses 0.
    Missing / None defaults to True (older saves or manifests without the key).
    Legacy string values ("Yes"/"No") are still accepted when loading old data.
    """
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    s = str(raw).strip().lower()
    if s in ("no", "false", "0"):
        return False
    if s in ("yes", "true", "1"):
        return True
    return True

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


def read_setup_manifest(setup_id: str) -> dict[str, Any] | None:
    """Load manifest.json for a setup folder. Returns None if missing or invalid."""
    manifest_path = _setup_dir(setup_id) / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def lobby_music_filenames(manifest: dict[str, Any]) -> list[str] | None:
    """Filenames under public/assets/audio/music. None when the manifest has no lobby_music key."""
    if "lobby_music" not in manifest:
        return None
    raw = manifest.get("lobby_music")
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, list):
        return [x.strip() for x in raw if isinstance(x, str) and x.strip()]
    return []


def scenario_display_from_setup_id(setup_id: str) -> dict[str, Any] | None:
    """display_name + context from manifest for lobby/meta. Ignores is_active (inactive setups still show for existing games)."""
    m = read_setup_manifest(setup_id)
    if not m and SETUPS_DIR.exists():
        for d in sorted(SETUPS_DIR.iterdir()):
            if not d.is_dir():
                continue
            cand = read_setup_manifest(d.name)
            if cand and cand.get("id") == setup_id:
                m = cand
                break
    if not m:
        return None
    ctx = m.get("context")
    out: dict[str, Any] = {
        "display_name": m.get("display_name", setup_id),
        "context": ctx if isinstance(ctx, dict) else None,
    }
    lobby_music = lobby_music_filenames(m)
    if lobby_music is not None:
        out["lobby_music"] = lobby_music
    return out


def list_setups() -> list[dict]:
    """Return [{ id, display_name, map_asset, context, timeline_image? }, ...] for the create-game menu.

    Requires manifest `is_active` to be exactly true (no default). Also requires non-empty context.
    Sort order: ``manifest.menu_order`` (ascending, lower first), then setup id for stability.
    """
    rows: list[tuple[int, str, dict]] = []
    if not SETUPS_DIR.exists():
        return []
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
        if m.get("is_active") is not True:
            continue
        ctx = m.get("context")
        if not isinstance(ctx, dict) or not ctx:
            continue
        factions: dict[str, Any] = {}
        factions_path = d / "factions.json"
        if factions_path.is_file():
            try:
                with open(factions_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    factions = loaded
            except (json.JSONDecodeError, OSError):
                factions = {}
        entry = scenario_menu_entry(m, setup_id, factions)
        rows.append((menu_order_sort_value(m), str(entry["id"]), entry))
    rows.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in rows]


def load_setup(setup_id: str) -> dict:
    """Load setup by id. Returns id, display_name, map_asset, starting_setup, and manifest fields
    (victory_criteria, camp_cost, stronghold_repair_cost when present).
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
            try:
                result["stronghold_repair_cost"] = int(m["stronghold_repair_cost"])
            except (TypeError, ValueError, KeyError):
                pass
            result["prefire_penalty"] = parse_prefire_penalty_from_manifest(m.get("prefire_penalty"))
            from backend.engine.special_rules import attach_manifest_rules
            attach_manifest_rules(result, m)
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
    # Non-empty: this unit is a hero; same id on multiple defs = versions of one hero (one in play).
    hero_id: Optional[str] = None
    icon: Optional[str] = None  # Filename in frontend/assets/units/

    # Future hooks for features not in V1
    transport_capacity: int = 0  # Naval: land units carried; ladder siegework: climbs_ladder slots per ladder unit
    downgrade_to: Optional[str] = None
    specials: list[str] = field(default_factory=list)
    home_territory_id: Optional[str] = None  # Deprecated: use home_territory_ids only
    home_territory_ids: Optional[list[str]] = None  # Home territories: can deploy 1 per territory per mobilization


@dataclass
class TerritoryDefinition:
    """Defines immutable properties of a territory."""
    id: str
    display_name: str
    terrain_type: str  # "plains", "forest", "mountain", "city"
    adjacent: list[str]  # IDs of adjacent territories (ground movement)
    produces: dict[str, int]  # {"power": 3} - production per turn
    is_stronghold: bool = False
    # Base HP for strongholds (only when is_stronghold); restored via repairs, not on conquest
    stronghold_base_health: int = 0  # 0 = not a stronghold or legacy
    # False for wastelands/neutral territories (no ownership change)
    ownable: bool = True
    # Additional adjacent territories for aerial units only (e.g. fly over mountains/rivers)
    aerial_adjacent: list[str] = field(default_factory=list)
    # River fords: land units with ford_crosser (or escort capacity) may use these edges
    ford_adjacent: list[str] = field(default_factory=list)
    # Optional panel art under public/assets/territories/. Empty = no image.
    image: Optional[str] = None


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


def _faction_capital_from_json(raw: Any) -> str:
    """Board capital territory id, or "" if none (null / empty / legacy \"None\" string)."""
    if raw is None or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s or s.lower() in ("none", "null", "n/a", "-"):
        return ""
    return s


def _coerce_faction_music(raw: Any) -> str | list[str] | None:
    """Legacy single string or list of filenames for assets/audio/music (playlist order for a turn)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s if s else None
    if isinstance(raw, list):
        out = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
        return out if out else None
    return None


@dataclass
class SubfactionDefinition:
    """A faction-owned side with its own territories and units, and no turn of its own."""
    id: str
    display_name: str
    color: str
    icon: Optional[str] = None  # Filename in frontend/assets/factions/; None uses the parent icon


@dataclass
class FactionDefinition:
    """Defines immutable properties of a faction."""
    id: str
    display_name: str
    alliance: str  # "good" or "evil"
    capital: str  # territory_id
    color: str
    icon: Optional[str] = None  # Filename in frontend/assets/factions/
    # Turn music: one filename or ordered list under public/assets/audio/music/ (may differ from id).
    music: str | list[str] | None = None
    # Set on a resolved subfaction view. Playable factions leave this empty.
    parent: str | None = None
    subfactions: tuple[SubfactionDefinition, ...] = ()


class FactionTable(dict[str, FactionDefinition]):
    """
    Playable factions only, for iteration, turn order, and seats.

    ``get`` and ``[]`` also resolve a subfaction id to a view that inherits alliance
    from its parent. Membership (``id in table``) stays playable factions, so a
    subfaction is never treated as a seat.
    """

    def __init__(self, factions: dict[str, FactionDefinition]):
        super().__init__(factions)
        resolved: dict[str, FactionDefinition] = dict(factions)
        for parent in factions.values():
            for sub in parent.subfactions:
                resolved[sub.id] = FactionDefinition(
                    id=sub.id,
                    display_name=sub.display_name,
                    alliance=parent.alliance,
                    capital="",
                    color=sub.color,
                    icon=sub.icon or parent.icon,
                    music=None,
                    parent=parent.id,
                    subfactions=(),
                )
        self._resolved = resolved

    def get(self, key: str, default: Any = None) -> FactionDefinition | Any:
        if key in self._resolved:
            return self._resolved[key]
        return default

    def __getitem__(self, key: str) -> FactionDefinition:
        try:
            return self._resolved[key]
        except KeyError:
            raise KeyError(key) from None


def faction_acts_as(
    faction_defs: dict[str, FactionDefinition] | None,
    subject_faction_id: str | None,
    acting_faction_id: str | None,
) -> bool:
    """True when subject is the acting faction, or a subfaction controlled by it."""
    if not subject_faction_id or not acting_faction_id:
        return False
    if subject_faction_id == acting_faction_id:
        return True
    if not faction_defs:
        return False
    subject = faction_defs.get(subject_faction_id)
    parent = getattr(subject, "parent", None) if subject else None
    return isinstance(parent, str) and parent == acting_faction_id


def controlling_faction_id(
    faction_defs: dict[str, FactionDefinition] | None,
    faction_id: str | None,
) -> str | None:
    """Playable faction that commands this id. A faction with no parent returns itself."""
    if not faction_id:
        return None
    if not faction_defs:
        return faction_id
    subject = faction_defs.get(faction_id)
    parent = getattr(subject, "parent", None) if subject else None
    if isinstance(parent, str) and parent:
        return parent
    return faction_id


def _parse_subfactions(raw: Any) -> tuple[SubfactionDefinition, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[SubfactionDefinition] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        if not isinstance(sid, str) or not sid.strip():
            continue
        icon = item.get("icon")
        icon_s = icon.strip() if isinstance(icon, str) and icon.strip() else None
        name = item.get("display_name")
        color = item.get("color")
        out.append(SubfactionDefinition(
            id=sid.strip(),
            display_name=name.strip() if isinstance(name, str) and name.strip() else sid.strip(),
            color=color.strip() if isinstance(color, str) and color.strip() else "#888888",
            icon=icon_s,
        ))
    return tuple(out)


def _faction_from_json(faction_id: str, data: dict) -> FactionDefinition:
    return FactionDefinition(
        id=data["id"],
        display_name=data["display_name"],
        alliance=data["alliance"],
        capital=_faction_capital_from_json(data.get("capital")),
        color=data["color"],
        icon=data.get("icon"),
        music=_coerce_faction_music(data.get("music")),
        subfactions=_parse_subfactions(data.get("subfactions")),
    )


def factions_from_json(factions_data: dict) -> FactionTable:
    factions: dict[str, FactionDefinition] = {}
    for faction_id, data in factions_data.items():
        factions[faction_id] = _faction_from_json(faction_id, data)
    return FactionTable(factions)


def is_transportable(ud: "UnitDefinition | None") -> bool:
    """True if unit can be carried by naval transport. Derived from 'transportable' in tags."""
    if not ud:
        return False
    return "transportable" in (getattr(ud, "tags", None) or [])


def _parse_optional_str(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _parse_hero_id(data: dict) -> Optional[str]:
    return _parse_optional_str(data.get("hero_id"))


def _parse_home_territories(data: dict) -> dict:
    """Return home_territory_ids from unit data (list only; supports legacy home_territory_id single key)."""
    ids = data.get("home_territory_ids")
    single = data.get("home_territory_id")
    if isinstance(ids, list):
        ids = [x for x in ids if isinstance(x, str)]
        return {"home_territory_ids": ids if ids else None}
    if single is not None and isinstance(single, str):
        return {"home_territory_ids": [single]}
    return {"home_territory_ids": None}


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
        tags_list = list(data.get("tags", []))
        units[unit_id] = UnitDefinition(
            id=data["id"],
            display_name=data["display_name"],
            faction=data["faction"],
            archetype=data["archetype"],
            tags=tags_list,
            attack=data["attack"],
            defense=data["defense"],
            movement=data["movement"],
            health=data["health"],
            cost=data["cost"],
            dice=data.get("dice", 1),
            purchasable=data.get("purchasable", True),
            unique=data.get("unique", False),
            hero_id=_parse_hero_id(data),
            icon=data.get("icon"),
            transport_capacity=data.get("transport_capacity", 0),
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
            stronghold_base_health=int(data["stronghold_base_health"]) if data.get("stronghold_base_health") is not None else int(data["stronghold_health"]) if data.get("stronghold_health") is not None else 0,
            ownable=data.get("ownable", True),
            aerial_adjacent=data.get("aerial_adjacent", []),
            ford_adjacent=data.get("ford_adjacent", []),
            image=_parse_optional_str(data.get("image")),
        )

    # Load factions
    with open(data_dir / "factions.json", "r") as f:
        factions_data = json.load(f)

    factions = factions_from_json(factions_data)

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
        tags_list = list(data.get("tags", []))
        units[unit_id] = UnitDefinition(
            id=data["id"],
            display_name=data["display_name"],
            faction=data["faction"],
            archetype=data["archetype"],
            tags=tags_list,
            attack=data["attack"],
            defense=data["defense"],
            movement=data["movement"],
            health=data["health"],
            cost=data["cost"],
            dice=data.get("dice", 1),
            purchasable=data.get("purchasable", True),
            unique=data.get("unique", False),
            hero_id=_parse_hero_id(data),
            icon=data.get("icon"),
            transport_capacity=data.get("transport_capacity", 0),
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
            stronghold_base_health=int(data["stronghold_base_health"]) if data.get("stronghold_base_health") is not None else int(data["stronghold_health"]) if data.get("stronghold_health") is not None else 0,
            ownable=data.get("ownable", True),
            aerial_adjacent=data.get("aerial_adjacent", []),
            ford_adjacent=data.get("ford_adjacent", []),
            image=_parse_optional_str(data.get("image")),
        )

    factions = factions_from_json(factions_data)

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
