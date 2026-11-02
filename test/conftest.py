"""Shared fixtures for the backend test suite."""

import pytest

from backend.ai.mcts import tree_cache


@pytest.fixture(autouse=True)
def _clear_mcts_tree_cache():
    """
    Isolate every test from the 3.4 module-global subtree cache.

    Reuse is keyed by game state, so a leftover entry from an earlier test with
    an identical snapshot would silently warm-start the next one. Clearing before
    and after each test keeps every test's first search cold while still letting
    a single test exercise reuse across its own consecutive calls.
    """
    tree_cache.clear()
    yield
    tree_cache.clear()
