"""
Game state representation.
All state is immutable; mutations return new state copies.
Includes JSON serialization for save/load functionality.
"""

import json
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any


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
        return cls(
            instance_id=data["instance_id"],
            unit_id=data["unit_id"],
            remaining_movement=data["remaining_movement"],
            remaining_health=data["remaining_health"],
            base_movement=data["base_movement"],
            base_health=data["base_health"],
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
        return cls(unit_id=data["unit_id"], count=data["count"])


@dataclass
class PendingMove:
    """A pending unit movement, stored until phase end."""
    from_territory: str
    to_territory: str
    unit_instance_ids: list[str]
    phase: str  # "combat_move" or "non_combat_move"

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_territory": self.from_territory,
            "to_territory": self.to_territory,
            "unit_instance_ids": self.unit_instance_ids,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingMove":
        return cls(
            from_territory=data["from_territory"],
            to_territory=data["to_territory"],
            unit_instance_ids=data["unit_instance_ids"],
            phase=data["phase"],
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
        return cls(
            destination=data["destination"],
            units=list(data["units"]),
        )


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
        return cls(
            owner=data["owner"],
            original_owner=data.get("original_owner"),
            units=[Unit.from_dict(u) for u in data.get("units", [])],
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

    def to_dict(self) -> dict[str, Any]:
        return {
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CombatRoundResult":
        return cls(
            round_number=data["round_number"],
            attacker_rolls=data["attacker_rolls"],
            defender_rolls=data["defender_rolls"],
            attacker_hits=data["attacker_hits"],
            defender_hits=data["defender_hits"],
            attacker_casualties=data["attacker_casualties"],
            defender_casualties=data["defender_casualties"],
            attackers_remaining=data["attackers_remaining"],
            defenders_remaining=data["defenders_remaining"],
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "attacker_faction": self.attacker_faction,
            "territory_id": self.territory_id,
            "attacker_instance_ids": self.attacker_instance_ids,
            "round_number": self.round_number,
            "combat_log": [r.to_dict() for r in self.combat_log],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveCombat":
        return cls(
            attacker_faction=data["attacker_faction"],
            territory_id=data["territory_id"],
            attacker_instance_ids=data["attacker_instance_ids"],
            round_number=data["round_number"],
            combat_log=[CombatRoundResult.from_dict(r) for r in data.get("combat_log", [])],
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
    # Strongholds available for mobilization this turn (snapshot at purchase phase start)
    # Only strongholds owned at the start of the turn can be used for mobilization
    mobilization_strongholds: list[str] = field(default_factory=list)
    # Pending moves (stored until phase ends, then applied)
    pending_moves: list[PendingMove] = field(default_factory=list)
    # Pending mobilizations (stored until phase ends, then applied)
    pending_mobilizations: list[PendingMobilization] = field(default_factory=list)
    # Winning alliance (None if game ongoing, "good" or "evil" if victory achieved)
    winner: str | None = None

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
            "mobilization_strongholds": self.mobilization_strongholds,
            "pending_moves": [pm.to_dict() for pm in self.pending_moves],
            "pending_mobilizations": [pm.to_dict() for pm in self.pending_mobilizations],
            "winner": self.winner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameState":
        """Create GameState from a dictionary."""
        return cls(
            turn_number=data["turn_number"],
            current_faction=data["current_faction"],
            phase=data["phase"],
            territories={
                tid: TerritoryState.from_dict(ts)
                for tid, ts in data["territories"].items()
            },
            faction_resources=data["faction_resources"],
            faction_purchased_units={
                fid: [UnitStack.from_dict(us) for us in stacks]
                for fid, stacks in data.get("faction_purchased_units", {}).items()
            },
            unit_id_counters=data.get("unit_id_counters", {}),
            active_combat=ActiveCombat.from_dict(data["active_combat"])
            if data.get("active_combat") else None,
            faction_pending_income=data.get("faction_pending_income", {}),
            pending_captures=data.get("pending_captures", {}),
            mobilization_strongholds=data.get("mobilization_strongholds", []),
            pending_moves=[PendingMove.from_dict(pm) for pm in data.get("pending_moves", [])],
            pending_mobilizations=[
                PendingMobilization.from_dict(pm) for pm in data.get("pending_mobilizations", [])
            ],
            winner=data.get("winner"),
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
