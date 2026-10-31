"""
Combat-move MCTS Problem: top-K heuristic proposal enumerator + leaf evaluator.

The action space for combat_move is enormous (unit x destination x grouping).
This Problem reuses the existing greedy policy as an *action generator*: enumerate
(from, to) pairs from movable units, apply garrison pruning, rank with cheap
formula scores (no combat sims during expansion), and keep the top-K candidates
plus end_phase when legal. MCTS then composes multi-front sequences of move_units
before ending the phase.

Heuristics remain evaluators/priors, not sole deciders.
"""

from __future__ import annotations

import os
import random
from typing import Any

from backend.engine.actions import Action, end_phase, move_units
from backend.engine.movement import _is_water_zone, resolve_territory_key_in_state
from backend.engine.queries import (
    get_movable_units,
    get_unit_move_targets,
    get_unit_faction,
    filter_unit_instances_that_can_reach,
    validate_action,
)
from backend.engine.reducer import get_state_after_pending_moves
from backend.engine.definitions import faction_acts_as

from backend.ai.context import AIContext
from backend.ai.formulas import (
    battle_gain_if_win,
    expected_net_gain,
    get_unit_power_cost,
)
from backend.ai.combat_move import (
    _defender_stacks,
    _is_attackable_territory,
    _is_cavalry_combat,
    _stacks_for_unit_ids,
)
from backend.ai.garrison import (
    move_attacks_enemy_stack_that_threatens_origin,
    prune_move_unit_ids_for_garrison_floor,
)
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.search import Problem

# Cap on branching factor per node (top-K proposals + optional end_phase).
MAX_COMBAT_MOVE_BRANCHES = 12

DEFAULT_DIFFICULTY_ENV = "AI_MCTS_DIFFICULTY"

# Soft-normalize scale for board evaluation (shared with combat.py style).
_BOARD_SCALE = 1.0


class CombatMoveProblem(Problem):
    """
    MCTS problem for the combat_move phase.

    States are GameState snapshots. Actions are engine Action objects:
    - move_units(faction, from, to, unit_ids, charge_through?) for a top-K attack candidate
    - end_phase(faction) when legal (terminal for this problem)

    Multi-step composition: MCTS sequences several move_units calls (each queues a
    pending move and reduces remaining_movement), then ends the phase.
    """

    def __init__(self, ctx: AIContext, rng: random.Random | None = None) -> None:
        self.ctx = ctx
        self.faction_id = ctx.faction_id
        self.ud = ctx.unit_defs
        self.td = ctx.territory_defs
        self.fd = ctx.faction_defs
        self.cd = ctx.camp_defs
        self.port_d = ctx.port_defs
        self.rng = rng if rng is not None else random.Random()
        # Root-phase constraints (API-provided); recomputed per state where needed.
        self._root_can_end_phase = bool(
            (ctx.available_actions or {}).get("can_end_phase", True)
        )

    # ----- Problem protocol -----

    def legal_actions(self, state: Any) -> list[HashableAction]:
        """Top-K move_units candidates + end_phase when legal."""
        if state.winner is not None:
            return []
        if (state.phase or "combat_move") != "combat_move":
            return []

        actions: list[HashableAction] = []
        for from_tid, to_tid, unit_ids, charge_through in self._enumerate_top_k(state):
            action = move_units(
                self.faction_id,
                from_tid,
                to_tid,
                list(unit_ids),
                charge_through=charge_through or None,
            )
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(action))

        if self._can_end_phase(state):
            end_action = end_phase(self.faction_id)
            v = validate_action(state, end_action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(end_action))

        return actions[: MAX_COMBAT_MOVE_BRANCHES + 1]

    def apply(self, state: Any, action: HashableAction) -> Any:
        """Apply via the reducer. apply_action deep-copies, so `state` is never mutated."""
        from backend.engine.reducer import apply_action

        raw = action.unwrap() if isinstance(action, HashableAction) else action
        new_state, _events = apply_action(
            state,
            raw,
            self.ud,
            self.td,
            self.fd,
            self.cd,
            self.port_d,
        )
        return new_state

    def is_terminal(self, state: Any) -> bool:
        """Episode ends when the phase advances past combat_move or the game is over."""
        if state.winner is not None:
            return True
        return (state.phase or "combat_move") != "combat_move"

    def evaluate(self, state: Any) -> float:
        """Non-terminal leaf: soft material + territory value from our perspective, in [-1, 1]."""
        return self._score_board(state)

    def terminal_value(self, state: Any) -> float:
        """Terminal leaf: winner short-circuits to ±1; otherwise board score."""
        if state.winner is not None:
            return self._winner_value(state)
        return self._score_board(state)

    def enumerator_size(self, state: Any) -> int:
        """Branching factor the enumerator yields for this state (3.1 hook)."""
        return len(self.legal_actions(state))

    def priors(self, state: Any) -> dict[HashableAction, float] | None:
        """
        Heuristic priors: weight each top-K move by its cheap formula score;
        end_phase gets a modest baseline so it is considered when no good moves remain.
        """
        if state.winner is not None or (state.phase or "combat_move") != "combat_move":
            return None

        scored: list[tuple[HashableAction, float]] = []
        for from_tid, to_tid, unit_ids, charge_through in self._enumerate_top_k(state):
            action = move_units(
                self.faction_id,
                from_tid,
                to_tid,
                list(unit_ids),
                charge_through=charge_through or None,
            )
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if not v.valid:
                continue
            score = self._cheap_move_score(state, from_tid, to_tid, unit_ids, charge_through)
            scored.append((HashableAction(action), max(0.05, score)))

        priors: dict[HashableAction, float] = {}
        for haction, weight in scored:
            priors[haction] = weight
        if self._can_end_phase(state):
            priors[HashableAction(end_phase(self.faction_id))] = 0.35
        return priors if priors else None

    # ----- Enumeration (top-K proposal generator) -----

    def _enumerate_top_k(
        self, state: Any
    ) -> list[tuple[str, str, list[str], list[str]]]:
        """
        Generate up to MAX_COMBAT_MOVE_BRANCHES ranked (from, to, unit_ids, charge_through) tuples.

        Mirrors the greedy policy's candidate construction (attackable destinations,
        garrison pruning, charge-path intersection) but ranks with pure formulas so
        expansion stays cheap. No combat sims here.
        """
        faction_id = self.faction_id
        ud, td, fd = self.ud, self.td, self.fd

        state_after = get_state_after_pending_moves(state, "combat_move", ud, td, fd)
        movable = get_movable_units(state, faction_id, ud, fd)
        pending_unit_ids: set[str] = set()
        for pm in (state.pending_moves or []):
            if getattr(pm, "phase", None) == "combat_move":
                pending_unit_ids.update(getattr(pm, "unit_instance_ids", []) or [])

        # Group movable instances by (from, to) for attackable destinations.
        from_to_units: dict[tuple[str, str], list[str]] = {}
        charge_routes_per_unit: dict[tuple[str, str], dict[str, list[list[str]]]] = {}
        for unit_info in movable:
            iid = unit_info.get("instance_id")
            if not iid or iid in pending_unit_ids:
                continue
            from_tid = unit_info.get("territory_id")
            targets, charge_routes = get_unit_move_targets(state, iid, ud, td, fd)
            for to_tid in (targets or {}).keys():
                if not to_tid or to_tid == from_tid:
                    continue
                if not _is_attackable_territory(state, to_tid, faction_id, fd, ud, td):
                    continue
                key = (from_tid, to_tid)
                from_to_units.setdefault(key, []).append(iid)
                if charge_routes.get(to_tid):
                    charge_routes_per_unit.setdefault(key, {})[iid] = charge_routes[to_tid]

        candidates: list[tuple[float, tuple[str, str, list[str], list[str]]]] = []
        for (from_tid, to_tid), unit_ids in from_to_units.items():
            if not unit_ids:
                continue
            # Garrison pruning: do not strip land from a threatened origin unless
            # this move is a counterattack into the threatening stack.
            threat_relief = move_attacks_enemy_stack_that_threatens_origin(
                from_tid, to_tid, state, faction_id, fd, td, ud
            )
            threat_from = self._threat_count(from_tid, state)
            pruned = prune_move_unit_ids_for_garrison_floor(
                state,
                from_tid,
                unit_ids,
                faction_id,
                ud,
                td,
                fd,
                threat_from_count=threat_from,
                threat_relief_attack=threat_relief,
            )
            if not pruned:
                continue
            pruned = filter_unit_instances_that_can_reach(state, to_tid, pruned, ud, td, fd)
            if not pruned:
                continue
            # Sea -> land: only land units may move (naval cannot go on land).
            from_def = td.get(from_tid)
            to_def = td.get(to_tid)
            if from_def and to_def and _is_water_zone(from_def) and not _is_water_zone(to_def):
                terr = state.territories.get(from_tid)
                ids_set = set(pruned)
                pruned = [
                    u.instance_id
                    for u in (getattr(terr, "units", []) or [])
                    if u.instance_id in ids_set and not self._is_boat(u.unit_id)
                ]
                if not pruned:
                    continue
            charge_through = self._best_charge_path(
                state, (from_tid, to_tid), pruned, charge_routes_per_unit
            )
            score = self._cheap_move_score(
                state, from_tid, to_tid, pruned, charge_through, state_after=state_after
            )
            candidates.append((score, (from_tid, to_tid, pruned, charge_through)))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [cand for _score, cand in candidates[:MAX_COMBAT_MOVE_BRANCHES]]

    def _threat_count(self, from_tid: str, state: Any) -> int:
        from backend.ai.geography import count_enemies_that_can_reach_territory_combat_move

        return count_enemies_that_can_reach_territory_combat_move(
            from_tid, state, self.faction_id, self.fd, self.td, self.ud
        )

    def _is_boat(self, unit_id: str) -> bool:
        from backend.engine.queries import _is_water_boat

        return _is_water_boat(self.ud.get(unit_id))

    def _best_charge_path(
        self,
        state: Any,
        key: tuple[str, str],
        unit_ids_for_move: list[str],
        charge_routes_per_unit: dict[tuple[str, str], dict[str, list[list[str]]]],
    ) -> list[str]:
        """Intersection of charge paths valid for ALL units in the move; pick by conquered value."""
        per_unit = charge_routes_per_unit.get(key, {})
        if not unit_ids_for_move or not per_unit:
            return []
        valid_paths: set[tuple[str, ...]] | None = None
        for uid in unit_ids_for_move:
            paths = per_unit.get(uid, [])
            path_tuples = {tuple(p) for p in paths} if paths else {()}
            valid_paths = path_tuples if valid_paths is None else (valid_paths & path_tuples)
        if not valid_paths:
            return []
        _, to_tid = key

        def path_sort_key(path: tuple[str, ...]) -> tuple[int, int]:
            conquered = int(
                sum(
                    self._territory_power(t)
                    for t in path
                    if self._is_conquest(t, state)
                )
                + (self._territory_power(to_tid) if self._is_conquest(to_tid, state) else 0)
            )
            return (-conquered, len(path) + 1)

        best = min(valid_paths, key=path_sort_key)
        return list(best)

    def _territory_power(self, tid: str) -> int:
        tdef = self.td.get(tid)
        if not tdef or not getattr(tdef, "produces", None) or not isinstance(tdef.produces, dict):
            return 0
        return int(tdef.produces.get("power", 0) or 0)

    def _is_conquest(self, tid: str, state: Any) -> bool:
        terr = state.territories.get(tid)
        if not terr:
            return False
        owner = getattr(terr, "owner", None)
        if owner == self.faction_id:
            return False
        if owner is not None:
            our_fd = self.fd.get(self.faction_id)
            owner_fd = self.fd.get(owner)
            if our_fd and owner_fd and getattr(owner_fd, "alliance", "") == getattr(our_fd, "alliance", ""):
                return False
            return True
        tdef = self.td.get(tid)
        return bool(tdef and getattr(tdef, "ownable", True))

    # ----- Cheap scoring (no sims) -----

    def _cheap_move_score(
        self,
        state: Any,
        from_tid: str,
        to_tid: str,
        unit_ids: list[str],
        charge_through: list[str] | None,
        *,
        state_after: Any | None = None,
    ) -> float:
        """
        Formula-only rank for a candidate move. Rough win rate from power ratio;
        no combat sims so expansion stays cheap. Used for both top-K ranking and priors.
        """
        move_stacks = _stacks_for_unit_ids(state, from_tid, unit_ids, self.ud)
        if not move_stacks:
            return -1.0
        our_power = sum(
            int(s.get("count", 0) or 0) * get_unit_power_cost(self.ud.get(s.get("unit_id")))
            for s in move_stacks
        )
        if our_power <= 0:
            return -1.0

        board = state_after if state_after is not None else state
        def_stacks = _defender_stacks(board, to_tid, self.faction_id, self.fd, self.ud)

        # Destination gain (objective + power), only for territories we actually conquer.
        dest_gain = 0.0
        if self._is_conquest(to_tid, state):
            dest_gain += battle_gain_if_win(to_tid, self.td, self.fd)
        for via in charge_through or []:
            if self._is_conquest(via, state):
                dest_gain += battle_gain_if_win(via, self.td, self.fd)

        if not def_stacks:
            # Empty / free conquest.
            total = dest_gain
            for via in [to_tid, *(charge_through or [])]:
                if self._is_conquest(via, state):
                    total += expected_net_gain(
                        1.0, via, self.td, self.fd, 0.0, 0.0, self.cd, self.port_d, self.ud
                    )
            # Mild preference for moving toward enemy strongholds (distance not computed here).
            return total

        def_power = sum(
            int(s.get("count", 0) or 0) * get_unit_power_cost(self.ud.get(s.get("unit_id")))
            for s in def_stacks
        )
        # Conservative power-ratio win estimate (no sim).
        p = our_power / (our_power + def_power + 1e-6)
        p = min(0.95, p * 0.9)
        if p < 0.2:
            return -5.0  # suicide attack: strongly discourage
        net = expected_net_gain(
            p,
            to_tid,
            self.td,
            self.fd,
            def_power * p * 0.5,  # expected defender casualty value
            our_power * (1.0 - p) * 0.35,  # expected attacker casualty value
            self.cd,
            self.port_d,
            self.ud,
        )
        return net + dest_gain * p

    # ----- End-phase legality -----

    def _can_end_phase(self, state: Any) -> bool:
        """
        True when combat_move may end: no loaded boats that must attack and no
        unresolved idle sails (mirrors the API / reducer end-phase guards).
        """
        sim = get_state_after_pending_moves(state, "combat_move", self.ud, self.td, self.fd)
        if getattr(sim, "loaded_naval_must_attack_instance_ids", None):
            return False
        if getattr(sim, "combat_move_naval_idle_sail_instance_ids", None):
            return False
        return True

    # ----- Board scoring -----

    def _winner_value(self, state: Any) -> float:
        our_alliance = ""
        fd_row = self.fd.get(self.faction_id)
        if fd_row:
            our_alliance = getattr(fd_row, "alliance", "") or ""
        return 1.0 if state.winner == our_alliance else -1.0

    def _score_board(self, state: Any) -> float:
        """Soft material + territory value from our perspective, in [-1, 1]."""
        our_power = 0.0
        enemy_power = 0.0
        our_terr = 0.0
        enemy_terr = 0.0
        our_alliance = ""
        fd_row = self.fd.get(self.faction_id)
        if fd_row:
            our_alliance = getattr(fd_row, "alliance", "") or ""

        for tid, terr in (state.territories or {}).items():
            owner = getattr(terr, "owner", None)
            value = 0.0
            tdef = self.td.get(tid)
            if tdef:
                if getattr(tdef, "is_stronghold", False):
                    value += 3.0
                produces = getattr(tdef, "produces", None)
                if isinstance(produces, dict):
                    value += float(produces.get("power", 0) or 0) * 0.5
            owner_is_ours = owner == self.faction_id
            owner_is_enemy = False
            if owner is not None and not owner_is_ours:
                owner_alliance = (
                    getattr(self.fd.get(owner), "alliance", None) if self.fd.get(owner) else None
                )
                owner_is_enemy = owner_alliance != our_alliance
            if owner_is_ours:
                our_terr += value
            elif owner_is_enemy:
                enemy_terr += value

            for unit in getattr(terr, "units", []) or []:
                udef = self.ud.get(getattr(unit, "unit_id", ""))
                cost = get_unit_power_cost(udef)
                if cost <= 0:
                    continue
                u_faction = getattr(udef, "faction", None)
                u_alliance = (
                    getattr(self.fd.get(u_faction), "alliance", None)
                    if u_faction and self.fd.get(u_faction)
                    else None
                )
                if u_faction == self.faction_id or u_alliance == our_alliance:
                    if u_alliance is not None and u_alliance != our_alliance:
                        enemy_power += cost
                    else:
                        our_power += cost
                elif u_faction is not None:
                    enemy_power += cost

        raw = (our_power + our_terr) - (enemy_power + enemy_terr)
        return raw / (1.0 + abs(raw)) * _BOARD_SCALE


def decide_combat_move_mcts(ctx: AIContext, rng: random.Random | None = None) -> Action | None:
    """
    Entry point: run combat-move MCTS and return the chosen root action.
    Falls back to None if search yields nothing (caller keeps the heuristic path).
    """
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.search import run_mcts_reuse

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None
    if (ctx.state.phase or "combat_move") != "combat_move":
        return None

    problem = CombatMoveProblem(ctx, rng=rng)
    budget = get_budget(os.environ.get(DEFAULT_DIFFICULTY_ENV))
    # 3.4: moves apply verbatim, so consecutive /ai-step calls chain the same
    # multi-move subtree instead of re-searching the expensive enumerator.
    action = run_mcts_reuse(problem, ctx.state, budget)
    if action is None:
        return None
    if isinstance(action, HashableAction):
        return action.unwrap()
    return action
