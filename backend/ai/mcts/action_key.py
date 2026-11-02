"""
Hashable wrapper around engine Action objects.

Engine `Action` has a dict payload and is therefore unhashable, but MCTS tree
nodes need actions as dict keys. `HashableAction` wraps an Action and derives
hash/eq from a stable serialization of (type, faction, payload).
"""

from __future__ import annotations

import json
from typing import Any

from backend.engine.actions import Action


def _payload_key(payload: dict[str, Any]) -> str:
    """Stable JSON key for a payload dict (sorted keys, no whitespace)."""
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(sorted((str(k), str(v)) for k, v in (payload or {}).items()))


class HashableAction:
    """Immutable hashable proxy around an engine Action."""

    __slots__ = ("action", "_key")

    def __init__(self, action: Action) -> None:
        self.action = action
        self._key = (action.type, action.faction, _payload_key(action.payload))

    @property
    def type(self) -> str:
        return self.action.type

    @property
    def faction(self) -> str:
        return self.action.faction

    @property
    def payload(self) -> dict[str, Any]:
        return self.action.payload

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HashableAction):
            return self._key == other._key
        if isinstance(other, Action):
            return self._key == (other.type, other.faction, _payload_key(other.payload))
        return NotImplemented

    def __repr__(self) -> str:
        return f"HashableAction({self.action.type!r}, {self.action.faction!r}, {self.action.payload!r})"

    def unwrap(self) -> Action:
        return self.action
