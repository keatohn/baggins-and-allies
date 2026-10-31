"""
Non-combat-move MCTS Problem: top-K heuristic proposal enumerator + leaf evaluator.

Mirrors the combat_move Problem: destinations are friendly / allied / neutral
(not enemy attack targets — those belong to combat_move). MCTS sequences multiple
move_units calls (reinforce threatened fronts, stage toward the enemy, rescue
stuck aerials) before ending the phase, composing multi-front repositioning that
the one-move-at-a-time greedy policy cannot.

Heuristics remain evaluators/priors, not sole deciders. Expansion uses cheap
formulas only (no defense sims) so the tree stays affordable.
"""

from __future__ import annotations

import os
import random
from typing import Any

from backend.engine.actions import Action, end_phase, move_units
from backend.engine.movement import _is_water_zone, is_friendly_territory_for_landing
from backend.engine.queries import (
    get_aerial_units_must_move,
    get_movable_units,
    get_unit_move_targets,
    filter_unit_instances_that_can_reach,
    validate_action,
)
from backend.engine.reducer import get_state_after_pending_moves

from backend.ai.context import AIContext
from backend.ai.formulas import get_unit_power_cost, territory_reinforce_base_score
from backend.ai.non_combat_move import (
    _attack_setup_value,
    _enemy_proximity_delta,
    _is_friendly_or_allied_territory,
)
from backend.ai.garrison import prune_move_unit_ids_for_garrison_floor
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.search import Problem

# Cap on branching factor per node (top-K proposals + optional end_phase).
MAX_NON_COMBAT_MOVE_BRANCHES = 12

DEFAULT_DIFFICULTY_ENV = "AI_MCTS_DIFFICULTY"

# Soft-normalize scale for board evaluation (shared with combat_move.py style).
_BOARD_SCALE = 1.0

# Extra prior weight for moves that rescue a stuck aerial (phase cannot end otherwise).
_AERIAL_RESCUE_PRIOR_BONUS = 2.5


class NonCombatMoveProblem(Problem):
    """
    MCTS problem for the non_combat_move phase.

    States are GameState snapshots. Actions are engine Action objects:
    - move_units(faction, from, to, unit_ids) for a top-K reposition candidate
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
        if (state.phase or "non_combat_move") != "non_combat_move":
            return []

        actions: list[HashableAction] = []
        for from_tid, to_tid, unit_ids in self._enumerate_top_k(state):
            action = move_units(self.faction_id, from_tid, to_tid, list(unit_ids))
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(action))

        if self._can_end_phase(state):
            end_action = end_phase(self.faction_id)
            v = validate_action(state, end_action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(end_action))

        return actions[: MAX_NON_COMBAT_MOVE_BRANCHES + 1]

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
        """Episode ends when the phase advances past non_combat_move or the game is over."""
        if state.winner is not None:
            return True
        return (state.phase or "non_combat_move") != "non_combat_move"

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
        Heuristic priors: weight each top-K move by its cheap reposition score;
        stuck-aerial rescue moves get a bonus; end_phase gets a modest baseline.
        """
        if state.winner is not None or (state.phase or "non_combat_move") != "non_combat_move":
            return None

        stuck_ids = self._stuck_aerial_instance_ids(state)
        scored: list[tuple[HashableAction, float]] = []
        for from_tid, to_tid, unit_ids in self._enumerate_top_k(state):
            action = move_units(self.faction_id, from_tid, to_tid, list(unit_ids))
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if not v.valid:
                continue
            score = self._cheap_move_score(state, from_tid, to_tid, unit_ids)
            if stuck_ids and (set(unit_ids) & stuck_ids):
                score += _AERIAL_RESCUE_PRIOR_BONUS
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
    ) -> list[tuple[str, str, list[str]]]:
        """
        Generate up to MAX_NON_COMBAT_MOVE_BRANCHES ranked (from, to, unit_ids) tuples.

        Mirrors the greedy policy's candidate construction (friendly/allied/neutral
        destinations, garrison pruning) but ranks with cheap formulas so expansion
        stays affordable. No defense sims here.
        """
        faction_id = self.faction_id
        ud, td, fd = self.ud, self.td, self.fd

        movable = get_movable_units(state, faction_id, ud, fd)
        pending_unit_ids: set[str] = set()
        for pm in (state.pending_moves or []):
            if getattr(pm, "phase", None) == "non_combat_move":
                pending_unit_ids.update(getattr(pm, "unit_instance_ids", []) or [])

        # Group movable instances by (from, to) for friendly/allied/neutral destinations.
        from_to_units: dict[tuple[str, str], list[str]] = {}
        for unit_info in movable:
            iid = unit_info.get("instance_id")
            if not iid or iid in pending_unit_ids:
                continue
            from_tid = unit_info.get("territory_id")
            targets, _ = get_unit_move_targets(state, iid, ud, td, fd)
            unit_def = ud.get(unit_info.get("unit_id", ""))
            for to_tid in (targets or {}).keys():
                if not to_tid or to_tid == from_tid:
                    continue
                if not self._destination_allowed(state, to_tid, unit_def):
                    continue
                from_to_units.setdefault((from_tid, to_tid), []).append(iid)

        candidates: list[tuple[float, tuple[str, str, list[str]]]] = []
        for (from_tid, to_tid), unit_ids in from_to_units.items():
            if not unit_ids:
                continue
            # Garrison pruning: never strip land from a threatened origin in non-combat
            # (no relief-attack exemption — attacks already happened).
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
                threat_relief_attack=False,
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
            score = self._cheap_move_score(state, from_tid, to_tid, pruned)
            candidates.append((score, (from_tid, to_tid, pruned)))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [cand for _score, cand in candidates[:MAX_NON_COMBAT_MOVE_BRANCHES]]

    def _destination_allowed(self, state: Any, to_tid: str, unit_def: Any) -> bool:
        """Friendly / allied / neutral for land/naval; aerials require friendly-for-landing."""
        from backend.engine.utils import is_aerial_unit

        if is_aerial_unit(unit_def):
            terr = state.territories.get(to_tid)
            if not terr:
                return False
            return is_friendly_territory_for_landing(
                terr, self.faction_id, self.fd, self.ud,
                state=state, territory_id=to_tid,
            )
        return _is_friendly_or_allied_territory(state, to_tid, self.faction_id, self.fd)

    def _threat_count(self, from_tid: str, state: Any) -> int:
        from backend.ai.geography import count_enemies_that_can_reach_territory_combat_move

        return count_enemies_that_can_reach_territory_combat_move(
            from_tid, state, self.faction_id, self.fd, self.td, self.ud
        )

    def _is_boat(self, unit_id: str) -> bool:
        from backend.engine.queries import _is_water_boat

        return _is_water_boat(self.ud.get(unit_id))

    def _stuck_aerial_instance_ids(self, state: Any) -> set[str]:
        """Aerials still on unfriendly land after pending moves (phase cannot end until they move)."""
        sim = get_state_after_pending_moves(state, "non_combat_move", self.ud, self.td, self.fd)
        stuck = get_aerial_units_must_move(sim, self.ud, self.td, self.fd, self.faction_id)
        return {entry.get("instance_id") for entry in stuck if entry.get("instance_id")}

    # ----- Cheap scoring (no sims) -----

    def _cheap_move_score(self, state: Any, from_tid: str, to_tid: str, unit_ids: list[str]) -> float:
        """
        Formula-only rank for a reposition candidate. Mixes destination defense value,
        forward-attack setup, and graph-distance advance toward enemy land. Used for
        both top-K ranking and priors.
        """
        base = territory_reinforce_base_score(to_tid, self.td, self.fd)
        asv = _attack_setup_value(to_tid, state, self.faction_id, self.fd, self.td, self.ud)
        score = 0.55 * base + 0.45 * asv

        prox = _enemy_proximity_delta(from_tid, to_tid, state, self.faction_id, self.fd, self.td)
        if prox != 0.0:
            score += prox * 0.6

        # Penalize retreating from an unthreatened origin with good forward position.
        threat_from = self._threat_count(from_tid, state)
        if threat_from == 0 and prox < 0.0:
            asv_from = _attack_setup_value(from_tid, state, self.faction_id, self.fd, self.td, self.ud)
            if asv_from > asv + 0.25:
                score -= 0.8

        return score

    # ----- End-phase legality -----

    def _can_end_phase(self, state: Any) -> bool:
        """
        True when non_combat_move may end: no aerial units left on unfriendly land
        (mirrors the API / reducer end-phase guard via state after pending moves).
        """
        sim = get_state_after_pending_moves(state, "non_combat_move", self.ud, self.td, self.fd)
        stuck = get_aerial_units_must_move(sim, self.ud, self.td, self.fd, self.faction_id)
        return not stuck

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


def decide_non_combat_move_mcts(ctx: AIContext, rng: random.Random | None = None) -> Action | None:
    """
    Entry point: run non-combat-move MCTS and return the chosen root action.
    Falls back to None if search yields nothing (caller keeps the heuristic path).
    """
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.search import run_mcts

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None
    if (ctx.state.phase or "non_combat_move") != "non_combat_move":
        return None

    problem = NonCombatMoveProblem(ctx, rng=rng)
    budget = get_budget(os.environ.get(DEFAULT_DIFFICULTY_ENV))
    action = run_mcts(problem, ctx.state, budget)
    if action is None:
        return None
    if isinstance(action, HashableAction):
        return action.unwrap()
    return action
