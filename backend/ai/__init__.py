"""
AI player for single-player and multiplayer games.
Reads game state and definitions only; proposes actions. The API validates and applies them.
"""

from backend.engine.actions import Action
from backend.engine.state import GameState
from backend.engine.definitions import (
    UnitDefinition,
    TerritoryDefinition,
    FactionDefinition,
    CampDefinition,
    PortDefinition,
)

from backend.ai.context import AIContext
from backend.ai.decide import decide

__all__ = [
    "decide",
    "AIContext",
    "Action",
]
