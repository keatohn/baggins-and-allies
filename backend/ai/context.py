"""
Context passed to the AI: read-only state and definitions plus available-actions summary.
Same data the human client gets; AI never modifies engine or DB.
"""

from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

from backend.engine.state import GameState
from backend.engine.definitions import (
    UnitDefinition,
    TerritoryDefinition,
    FactionDefinition,
    CampDefinition,
    PortDefinition,
)

if TYPE_CHECKING:
    from backend.ai.strategic_context import StrategicTurnContext


@dataclass
class AIContext:
    """Read-only snapshot for the AI to decide one action."""

    state: GameState
    unit_defs: dict[str, UnitDefinition]
    territory_defs: dict[str, TerritoryDefinition]
    faction_defs: dict[str, FactionDefinition]
    camp_defs: dict[str, CampDefinition]
    port_defs: dict[str, PortDefinition]
    """Available-actions dict from _build_available_actions (phase, purchasable_units, mobilize_options, etc.)."""
    available_actions: dict[str, Any]
    """Built in decide(); shared blob objectives and pressure for all phase policies."""
    strategic: Optional["StrategicTurnContext"] = None

    @property
    def faction_id(self) -> str:
        return self.state.current_faction or ""

    @property
    def phase(self) -> str:
        return self.state.phase or "purchase"
