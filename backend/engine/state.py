"""
Game state representation.
All state is immutable; mutations return new state copies.
Includes JSON serialization for save/load functionality.
"""

import json
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any


def _ensure_str_list(value: Any) -> list[str]:
    """Ensure value is a list of strings (for camps_standing, mobilization_camps from DB)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def _ensure_faction_territories_at_turn_start(value: Any) -> dict[str, list[str]]:
    """Parse faction_territories_at_turn_start from dict; default {}."""
    if not isinstance(value, dict):
        return {}
    return {
        str(k): _ensure_str_list(v)
        for k, v in value.items()
    }


def _ensure_victory_criteria(value: Any) -> dict[str, Any]:
    """
    Parse victory_criteria from dict.
    Shape: {"strongholds": {"good": 2, "evil": 2}, ...}
    Legacy: accepts old victory_strongholds {"good": 2, "evil": 2} and converts.
    Default: {"strongholds": {"good": 4, "evil": 4}}
    """
    default = {"strongholds": {"good": 4, "evil": 4}}
    if not isinstance(value, dict):
        return default
    # Legacy: flat victory_strongholds
    if "strongholds" not in value and any(k in ("good", "evil") for k in value):
        strongholds = {}
        for k, v in value.items():
            try:
                strongholds[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        if strongholds:
            return {"strongholds": strongholds}
        return default
    # New format
    strongholds_val = value.get("strongholds")
    if isinstance(strongholds_val, dict) and strongholds_val:
        result = {}
        for k, v in strongholds_val.items():
            try:
                result[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        if result:
            return {"strongholds": result}
    return default


@dataclass
class Unit:
    """Individual unit instance with movement and health tracking."""
    instance_id: str  # Unique ID for this unit instance (e.g., "gondor_infantry_001")
    unit_id: str  # Type of unit (e.g., "gondor_infantry")
    remaining_movement: int  # Movement available this turn
    remaining_health: int  # Health remaining (durability during/after combat)
    base_movement: int  # Original movement (restored at turn start)
    base_health: int  # Original health (restored after battle)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "unit_id": self.unit_id,
            "remaining_movement": self.remaining_movement,
            "remaining_health": self.remaining_health,
            "base_movement": self.base_movement,
            "base_health": self.base_health,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Unit":
        if not isinstance(data, dict):
            data = {}
        def _int(v: Any, default: int) -> int:
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            unit_id=str(data.get("unit_id") or ""),
            remaining_movement=_int(data.get("remaining_movement"), 0),
            remaining_health=_int(data.get("remaining_health"), 1),
            base_movement=_int(data.get("base_movement"), 0),
            base_health=_int(data.get("base_health"), 1),
        )


@dataclass
class UnitStack:
    """A stack of identical units in a territory. Used for purchased units before mobilization."""
    unit_id: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"unit_id": self.unit_id, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnitStack":
        if not isinstance(data, dict):
            data = {}
        try:
            count = int(data.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        return cls(unit_id=str(data.get("unit_id") or ""), count=max(0, count))


@dataclass
class PendingMove:
    """A pending unit movement, stored until phase end."""
    from_territory: str
    to_territory: str
    unit_instance_ids: list[str]
    phase: str  # "combat_move" or "non_combat_move"
    # Cavalry charging: empty enemy territory IDs to conquer when move is applied (order matters)
    charge_through: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "from_territory": self.from_territory,
            "to_territory": self.to_territory,
            "unit_instance_ids": self.unit_instance_ids,
            "phase": self.phase,
        }
        if self.charge_through:
            out["charge_through"] = self.charge_through
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingMove":
        if not isinstance(data, dict):
            data = {}
        uids = data.get("unit_instance_ids")
        if not isinstance(uids, list):
            uids = []
        ct = data.get("charge_through")
        if not isinstance(ct, list):
            ct = []
        return cls(
            from_territory=str(data.get("from_territory") or ""),
            to_territory=str(data.get("to_territory") or ""),
            unit_instance_ids=[str(x) for x in uids],
            phase=str(data.get("phase") or "combat_move"),
            charge_through=[str(x) for x in ct],
        )


@dataclass
class PendingMobilization:
    """A pending deployment to a stronghold, applied at end of mobilization phase."""
    destination: str
    units: list[dict[str, Any]]  # [{"unit_id": str, "count": int}, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "units": self.units,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingMobilization":
        if not isinstance(data, dict):
            data = {}
        u = data.get("units")
        if not isinstance(u, list):
            u = []
        return cls(destination=str(data.get("destination") or ""), units=list(u))


@dataclass
class TerritoryState:
    """State of a single territory."""
    owner: str | None  # faction_id or None if unowned
    # Original owner at game start (for liberation mechanic)
    # None if territory started unowned
    original_owner: str | None = None
    # Individual unit instances
    units: list[Unit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "original_owner": self.original_owner,
            "units": [u.to_dict() for u in self.units],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TerritoryState":
        if not isinstance(data, dict):
            data = {}
        units_raw = data.get("units") or []
        if not isinstance(units_raw, list):
            units_raw = []
        return cls(
            owner=data.get("owner"),
            original_owner=data.get("original_owner"),
            units=[Unit.from_dict(u) for u in units_raw if isinstance(u, dict)],
        )


@dataclass
class CombatRoundResult:
    """Result of a single combat round (for combat log)."""
    round_number: int
    attacker_rolls: list[int]
    defender_rolls: list[int]
    attacker_hits: int
    defender_hits: int
    attacker_casualties: list[str]  # instance_ids destroyed
    defender_casualties: list[str]  # instance_ids destroyed
    attackers_remaining: int  # count after this round
    defenders_remaining: int  # count after this round
    is_archer_prefire: bool = False  # True when this entry is defender archer prefire before round 1

    def to_dict(self) -> dict[str, Any]:
        out = {
            "round_number": self.round_number,
            "attacker_rolls": self.attacker_rolls,
            "defender_rolls": self.defender_rolls,
            "attacker_hits": self.attacker_hits,
            "defender_hits": self.defender_hits,
            "attacker_casualties": self.attacker_casualties,
            "defender_casualties": self.defender_casualties,
            "attackers_remaining": self.attackers_remaining,
            "defenders_remaining": self.defenders_remaining,
        }
        if self.is_archer_prefire:
            out["is_archer_prefire"] = True
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CombatRoundResult":
        if not isinstance(data, dict):
            data = {}
        def _int(v: Any, d: int) -> int:
            try:
                return int(v) if v is not None else d
            except (TypeError, ValueError):
                return d
        def _list(v: Any, d: list) -> list:
            return list(v) if isinstance(v, list) else d
        def _bool(v: Any, d: bool) -> bool:
            return bool(v) if v is not None else d
        return cls(
            round_number=_int(data.get("round_number"), 0),
            attacker_rolls=_list(data.get("attacker_rolls"), []),
            defender_rolls=_list(data.get("defender_rolls"), []),
            attacker_hits=_int(data.get("attacker_hits"), 0),
            defender_hits=_int(data.get("defender_hits"), 0),
            attacker_casualties=_list(data.get("attacker_casualties"), []),
            defender_casualties=_list(data.get("defender_casualties"), []),
            attackers_remaining=_int(data.get("attackers_remaining"), 0),
            defenders_remaining=_int(data.get("defenders_remaining"), 0),
            is_archer_prefire=_bool(data.get("is_archer_prefire"), False),
        )


@dataclass
class ActiveCombat:
    """
    Tracks an ongoing multi-round combat.
    Both attackers and defenders are in the same territory (the contested territory).
    Attackers moved INTO the territory during combat_move phase.
    """
    attacker_faction: str
    territory_id: str  # The contested territory where combat is happening
    # Instance IDs of attacking units still alive (for tracking who can retreat)
    attacker_instance_ids: list[str]
    round_number: int
    combat_log: list[CombatRoundResult] = field(default_factory=list)
    # False only after defender archer prefire, until round 1 is resolved (retreat disallowed until then)
    attackers_have_rolled: bool = True

    def to_dict(self) -> dict[str, Any]:
        out = {
            "attacker_faction": self.attacker_faction,
            "territory_id": self.territory_id,
            "attacker_instance_ids": self.attacker_instance_ids,
            "round_number": self.round_number,
            "combat_log": [r.to_dict() for r in self.combat_log],
        }
        if not self.attackers_have_rolled:
            out["attackers_have_rolled"] = False
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveCombat":
        if not isinstance(data, dict):
            data = {}
        aids = data.get("attacker_instance_ids")
        if not isinstance(aids, list):
            aids = []
        log = data.get("combat_log") or []
        if not isinstance(log, list):
            log = []
        try:
            rn = int(data.get("round_number", 0))
        except (TypeError, ValueError):
            rn = 0
        # Default True for backward compatibility (old saves without this field)
        have_rolled = data.get("attackers_have_rolled", True)
        if not isinstance(have_rolled, bool):
            have_rolled = True
        return cls(
            attacker_faction=str(data.get("attacker_faction") or ""),
            territory_id=str(data.get("territory_id") or ""),
            attacker_instance_ids=[str(x) for x in aids],
            round_number=rn,
            combat_log=[CombatRoundResult.from_dict(r) for r in log if isinstance(r, dict)],
            attackers_have_rolled=have_rolled,
        )


@dataclass
class GameState:
    """Complete game state."""
    turn_number: int
    current_faction: str  # faction_id
    phase: str  # "purchase", "combat_move", "combat", "non_combat_move", "mobilization"
    territories: dict[str, TerritoryState]  # territory_id -> TerritoryState
    # faction_id -> {resource_id -> count}
    faction_resources: dict[str, dict[str, int]]
    # faction_id -> [UnitStack] - units purchased this turn, waiting for mobilization phase
    faction_purchased_units: dict[str, list[UnitStack]] = field(
        default_factory=dict)
    # Counter for generating unique unit instance IDs (faction_id -> next_id)
    unit_id_counters: dict[str, int] = field(default_factory=dict)
    # Currently active combat (None if no combat in progress)
    active_combat: ActiveCombat | None = None
    # Pending income to be collected at start of faction's next turn
    # Calculated at end of their turn based on owned territories
    # faction_id -> {resource_id -> amount}
    faction_pending_income: dict[str, dict[str, int]] = field(default_factory=dict)
    # Pending territory captures (applied at end of combat phase)
    # territory_id -> new_owner_faction_id
    pending_captures: dict[str, str] = field(default_factory=dict)
    # Camp definition IDs that are still standing (camps are destroyed when their territory is captured/liberated)
    camps_standing: list[str] = field(default_factory=list)
    # Territory IDs available for mobilization this turn (snapshot at turn start: owned territories with a camp)
    mobilization_camps: list[str] = field(default_factory=list)
    # Pending moves (stored until phase ends, then applied)
    pending_moves: list[PendingMove] = field(default_factory=list)
    # Pending mobilizations (stored until phase ends, then applied)
    pending_mobilizations: list[PendingMobilization] = field(default_factory=list)
    # Winning alliance (None if game ongoing, "good" or "evil" if victory achieved)
    winner: str | None = None
    # Map base name for this game (e.g. "test_map"). Frontend loads <base>.png and <base>.svg. None = use frontend default (legacy).
    map_asset: str | None = None
    # Victory criteria: {"strongholds": {"good": 2, "evil": 2}, ...} - extensible for future criteria
    victory_criteria: dict[str, Any] = field(
        default_factory=lambda: {"strongholds": {"good": 4, "evil": 4}}
    )
    # Camp purchase cost (from setup manifest). Used in purchase phase.
    camp_cost: int = 0  # From setup manifest; 0 = not set / no camp purchase
    # Faction territories at start of their turn (set when turn starts). Used for camp placement options.
    faction_territories_at_turn_start: dict[str, list[str]] = field(default_factory=dict)
    # Purchased camps this turn: list of {territory_options: [tid, ...], placed_territory_id: None | str}
    pending_camps: list[dict[str, Any]] = field(default_factory=list)
    # Purchased camps that were placed: camp_id -> territory_id (camp_id e.g. "purchased_camp_pelennor")
    dynamic_camps: dict[str, str] = field(default_factory=dict)

    def copy(self) -> "GameState":
        """Return a deep copy of this game state."""
        return deepcopy(self)

    def generate_unit_instance_id(self, faction_id: str, unit_id: str) -> str:
        """Generate a unique instance ID for a unit."""
        if faction_id not in self.unit_id_counters:
            self.unit_id_counters[faction_id] = 0
        self.unit_id_counters[faction_id] += 1
        return f"{faction_id}_{unit_id}_{self.unit_id_counters[faction_id]:03d}"

    # ===== Serialization Methods =====

    def to_dict(self) -> dict[str, Any]:
        """Convert GameState to a dictionary for JSON serialization."""
        return {
            "turn_number": self.turn_number,
            "current_faction": self.current_faction,
            "phase": self.phase,
            "territories": {
                tid: ts.to_dict() for tid, ts in self.territories.items()
            },
            "faction_resources": self.faction_resources,
            "faction_purchased_units": {
                fid: [us.to_dict() for us in stacks]
                for fid, stacks in self.faction_purchased_units.items()
            },
            "unit_id_counters": self.unit_id_counters,
            "active_combat": self.active_combat.to_dict() if self.active_combat else None,
            "faction_pending_income": self.faction_pending_income,
            "pending_captures": self.pending_captures,
            "camps_standing": self.camps_standing,
            "mobilization_camps": self.mobilization_camps,
            "pending_moves": [pm.to_dict() for pm in self.pending_moves],
            "pending_mobilizations": [pm.to_dict() for pm in self.pending_mobilizations],
            "winner": self.winner,
            "map_asset": self.map_asset,
            "victory_criteria": self.victory_criteria,
            "camp_cost": self.camp_cost,
            "faction_territories_at_turn_start": self.faction_territories_at_turn_start,
            "pending_camps": self.pending_camps,
            "dynamic_camps": self.dynamic_camps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameState":
        """Create GameState from a dictionary (handles missing/None for backwards compat)."""
        territories_data = data.get("territories") or {}
        if not isinstance(territories_data, dict):
            territories_data = {}
        fr = data.get("faction_resources") or {}
        if not isinstance(fr, dict):
            fr = {}
        fr = {k: v if isinstance(v, dict) else {} for k, v in fr.items()}
        fpu = data.get("faction_purchased_units") or {}
        if not isinstance(fpu, dict):
            fpu = {}
        uic = data.get("unit_id_counters") or {}
        if not isinstance(uic, dict):
            uic = {}
        fpi = data.get("faction_pending_income") or {}
        if not isinstance(fpi, dict):
            fpi = {}
        pc = data.get("pending_captures") or {}
        if not isinstance(pc, dict):
            pc = {}
        return cls(
            turn_number=int(data.get("turn_number", 1)) if data.get("turn_number") is not None else 1,
            current_faction=str(data.get("current_faction", "") or ""),
            phase=str(data.get("phase", "purchase") or "purchase"),
            territories={
                tid: TerritoryState.from_dict(ts)
                for tid, ts in territories_data.items()
                if isinstance(ts, dict)
            },
            faction_resources=fr,
            faction_purchased_units={
                fid: [UnitStack.from_dict(us) for us in stacks if isinstance(us, dict)]
                for fid, stacks in fpu.items()
                if isinstance(stacks, list)
            },
            unit_id_counters=uic,
            active_combat=ActiveCombat.from_dict(data["active_combat"])
            if data.get("active_combat") else None,
            faction_pending_income=fpi,
            pending_captures=pc,
            camps_standing=_ensure_str_list(data.get("camps_standing")),
            mobilization_camps=_ensure_str_list(
                data.get("mobilization_camps") or data.get("mobilization_strongholds")
            ),
            pending_moves=[PendingMove.from_dict(pm) for pm in (data.get("pending_moves") or []) if isinstance(pm, dict)],
            pending_mobilizations=[
                PendingMobilization.from_dict(pm) for pm in (data.get("pending_mobilizations") or []) if isinstance(pm, dict)
            ],
            winner=data.get("winner"),
            map_asset=data.get("map_asset") if isinstance(data.get("map_asset"), str) else None,
            victory_criteria=_ensure_victory_criteria(
                data.get("victory_criteria") or data.get("victory_strongholds")
            ),
            camp_cost=int(data["camp_cost"]) if data.get("camp_cost") is not None else 0,
            faction_territories_at_turn_start=_ensure_faction_territories_at_turn_start(
                data.get("faction_territories_at_turn_start")
            ),
            pending_camps=list(data.get("pending_camps") or []) if isinstance(data.get("pending_camps"), list) else [],
            dynamic_camps=dict(data.get("dynamic_camps") or {}) if isinstance(data.get("dynamic_camps"), dict) else {},
        )

    def to_json(self, indent: int = 2) -> str:
        """Serialize GameState to a JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "GameState":
        """Deserialize GameState from a JSON string."""
        return cls.from_dict(json.loads(json_str))

    def save(self, filepath: str) -> None:
        """Save GameState to a JSON file."""
        with open(filepath, "w") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, filepath: str) -> "GameState":
        """Load GameState from a JSON file."""
        with open(filepath, "r") as f:
            return cls.from_json(f.read())
