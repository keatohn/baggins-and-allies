"""
Cross-call subtree reuse for MCTS (building block 3.4).

Each `/ai-step` call re-searches from scratch today, throwing away the tree that
previous call just paid for. This module keeps a small module-level LRU cache,
keyed by `state_key`, that stores only the **chosen child's subtree** after every
`run_mcts_reuse` search. The next search at that exact state looks the subtree up
and resumes from it — visit counts, priors, and already-expanded children carry
over instead of being rebuilt.

Why only the chosen child: siblings of the chosen child are unrealized
alternatives from the previous decision, so retained memory stays bounded by one
subtree per entry rather than by the whole tree (3.1 measured ~63KB per
GameState node; the LRU cap keeps the worst case small).

Lookups return a *clone* of the cached subtree. The stored entry therefore stays
pristine (a later search's backups cannot corrupt it) and concurrent searches
never mutate shared Node objects.

Trust basis: entries are keyed by the full state hash — the same key the 3.3
in-run transposition table uses. A key match means the same game state, and the
carried child actions were validated against that very state when they were
expanded, so they remain legal for it. Keys are state-only: two different setups
that somehow produced a byte-identical state would share a subtree, which costs
at worst a suboptimal action — never an unvalidated one, because the API still
runs `validate_action` before applying anything.

Known limitation: combat phases will often miss. `decide_combat_mcts` returns
initiate/continue actions with empty dice and the API fills real rolls before
apply, so the state arriving at the next call differs from the searched child.
Reuse benefits purchase, combat_move, non_combat_move, and mobilization most.

Environment:
    AI_MCTS_TREE_CACHE_ENTRIES — max LRU entries (default 4); 0 disables reuse.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Hashable

from backend.ai.mcts.node import Node

DEFAULT_CACHE_ENTRIES = 4
CACHE_ENTRIES_ENV = "AI_MCTS_TREE_CACHE_ENTRIES"

_lock = threading.Lock()
_entries: "OrderedDict[Hashable, Node]" = OrderedDict()
_hits = 0
_misses = 0


def max_entries() -> int:
    """Configured LRU cap; 0 (or negative) disables the cache entirely."""
    raw = os.environ.get(CACHE_ENTRIES_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_ENTRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_CACHE_ENTRIES


def lookup(key: Hashable) -> Node | None:
    """
    Return an isolated clone of the subtree cached for `key`, or None on a miss.

    A disabled cache (AI_MCTS_TREE_CACHE_ENTRIES=0) always misses and does not
    count the attempt, so counters stay meaningful as "reuse opportunities seen".
    """
    global _hits, _misses
    with _lock:
        if max_entries() <= 0:
            return None
        node = _entries.get(key)
        if node is None:
            _misses += 1
            return None
        _entries.move_to_end(key)
        _hits += 1
        return node.clone()


def stash(key: Hashable, node: Node) -> None:
    """
    Store `node`'s subtree under `key`, evicting the least recently used entry
    when over the cap. Re-stashing a key replaces it (the newest search wins).
    """
    with _lock:
        if max_entries() <= 0:
            return
        _entries.pop(key, None)
        _entries[key] = node
        while len(_entries) > max_entries():
            _entries.popitem(last=False)


def discard(key: Hashable) -> None:
    """Drop one entry (used when a cached subtree turns out to be unusable)."""
    with _lock:
        _entries.pop(key, None)


def clear() -> None:
    """Empty the cache and zero the counters (tests call this explicitly)."""
    global _hits, _misses
    with _lock:
        _entries.clear()
        _hits = 0
        _misses = 0


def cache_info() -> dict[str, int]:
    """
    Snapshot counters: entries/hits/misses plus the configured cap.

    `hits` counts lookups that returned a subtree — `SearchStats.reused_root`
    is the authoritative signal that a search actually resumed from it (a hit
    on a state with no legal actions is discarded instead of being used).
    """
    with _lock:
        return {
            "entries": len(_entries),
            "hits": _hits,
            "misses": _misses,
            "max_entries": max_entries(),
        }
