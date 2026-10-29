"""
MCTS tree node. Holds visit statistics and lazily-expanded children.
Values are maintained in the ROOT perspective (the faction that started the search),
so backpropagation is a simple running mean regardless of tree depth.
"""

from __future__ import annotations

import math
from typing import Generic, TypeVar

A = TypeVar("A")  # action type
S = TypeVar("S")  # state type


class Node(Generic[S, A]):
    """One search-tree node: a state plus visit statistics for the edge that led here."""

    __slots__ = (
        "state",
        "prior",
        "visit_count",
        "total_value",
        "children",
        "untried_actions",
        "_expanded",
    )

    def __init__(
        self,
        state: S,
        prior: float = 1.0,
        untried_actions: list[A] | None = None,
    ) -> None:
        self.state = state
        self.prior = prior
        self.visit_count = 0
        self.total_value = 0.0
        self.children: dict[A, Node[S, A]] = {}
        self.untried_actions: list[A] = list(untried_actions or [])
        self._expanded = False

    @property
    def value(self) -> float:
        """Mean value in root perspective; 0.0 before any visits."""
        if self.visit_count == 0:
            return 0.0
        return self.total_value / self.visit_count

    @property
    def is_fully_expanded(self) -> bool:
        return self._expanded and not self.untried_actions

    def mark_expanded(self) -> None:
        self._expanded = True

    def select_child(self, c_puct: float) -> tuple[A, "Node[S, A]"]:
        """
        Pick the child maximizing PUCT: Q + U.
        Q is child mean value (root perspective); U = c_puct * prior * sqrt(N_parent) / (1 + N_child).
        A prior floor is applied inside U so no legal action is fully starved.
        """
        if not self.children:
            raise ValueError("select_child called on leaf with no children")
        sqrt_parent = math.sqrt(max(1, self.visit_count))
        best_action: A | None = None
        best_child: Node[S, A] | None = None
        best_score = -math.inf
        for action, child in self.children.items():
            q = child.value
            u = c_puct * child.prior * sqrt_parent / (1.0 + child.visit_count)
            score = q + u
            if score > best_score:
                best_score = score
                best_action = action
                best_child = child
        assert best_action is not None and best_child is not None
        return best_action, best_child

    def best_child_by_visits(self) -> tuple[A, "Node[S, A]"]:
        """Most-visited child (standard MCTS action selection)."""
        if not self.children:
            raise ValueError("best_child_by_visits called with no children")
        best_action: A | None = None
        best_child: Node[S, A] | None = None
        best_visits = -1
        for action, child in self.children.items():
            if child.visit_count > best_visits:
                best_visits = child.visit_count
                best_action = action
                best_child = child
        assert best_action is not None and best_child is not None
        return best_action, best_child

    def add_child(self, action: A, child: "Node[S, A]") -> "Node[S, A]":
        self.children[action] = child
        return child
