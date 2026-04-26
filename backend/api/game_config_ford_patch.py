"""Mutate games.config definitions snapshot: reciprocal ford_adjacent between two territories."""

from __future__ import annotations

import copy
import json
from typing import Any


def patch_ford_adjacent_pair_in_config(config: dict[str, Any], territory_a: str, territory_b: str) -> bool:
    """
    Mutate config in place: ensure definitions.territories[a|b].ford_adjacent lists include the other id.
    Returns True if any list was modified.
    """
    a = (territory_a or "").strip()
    b = (territory_b or "").strip()
    if not a or not b:
        raise ValueError("territory_a and territory_b must be non-empty")
    defs = config.get("definitions")
    if not isinstance(defs, dict):
        raise ValueError("config missing definitions")
    terr = defs.get("territories")
    if not isinstance(terr, dict):
        raise ValueError("definitions missing territories")
    for tid in (a, b):
        if tid not in terr or not isinstance(terr[tid], dict):
            raise ValueError(f"missing territory in snapshot: {tid!r}")
    changed = False
    for x, y in ((a, b), (b, a)):
        t = terr[x]
        fa = t.get("ford_adjacent")
        if not isinstance(fa, list):
            fa = []
            t["ford_adjacent"] = fa
        if y not in fa:
            fa.append(y)
            changed = True
    return changed


def preview_ford_adjacent_pair(config: dict[str, Any], territory_a: str, territory_b: str) -> dict[str, Any]:
    """Return ford_adjacent for both territories after applying patch to a deep copy (does not mutate input)."""
    cfg = copy.deepcopy(config)
    patch_ford_adjacent_pair_in_config(cfg, territory_a, territory_b)
    terr = cfg["definitions"]["territories"]
    a = (territory_a or "").strip()
    b = (territory_b or "").strip()
    return {
        "ford_adjacent": {
            a: list(terr[a].get("ford_adjacent") or []),
            b: list(terr[b].get("ford_adjacent") or []),
        }
    }


def dump_config_for_storage(config: dict[str, Any]) -> str:
    return json.dumps(config, separators=(",", ":"), ensure_ascii=False)
