"""
Combat-move MCTS Problem: top-K heuristic proposal enumerator + leaf evaluator.

The action space for combat_move is enormous (unit x destination x grouping).
This Problem reuses the existing greedy policy as an *action generator*: enumerate
(from, to) pairs from movable units, apply garrison pruning, rank each defended
battle with the combat sim (the same trials the heuristic uses), and keep the
top-K candidates that are actually worth fighting, plus end_phase when legal.
MCTS then composes multi-front sequences of move_units before ending the phase.

Heuristics remain evaluators/priors, not sole deciders.
"""

from __future__ import annotations

import random
from typing import Any

from backend.engine.actions import Action, cancel_move, end_phase, move_units
from backend.engine.movement import _is_water_zone, resolve_territory_key_in_state
from backend.engine.queries import (
    get_movable_units,
    get_unit_move_targets,
    get_unit_faction,
    filter_unit_instances_that_can_reach,
    get_ladder_only_attack_destinations,
    validate_action,
)
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.definitions import faction_acts_as

from backend.ai.context import AIContext
from backend.engine.combat_sim import SimOptions, run_simulation

from backend.ai.formulas import (
    battle_gain_if_win,
    expected_net_gain,
    get_unit_power_cost,
)
from backend.ai.habits import COMBAT_SIM_TRIALS
from backend.ai.style import scale_combat_gain
from backend.ai.combat_move import (
    _committed_attacker_stacks,
    _defender_stacks,
    _is_attackable_territory,
    _is_cavalry_combat,
    _stacks_for_unit_ids,
    _with_raid_boats,
    is_ladder_instance,
    unit_ids_are_unaccompanied_ladders,
)
from backend.ai.garrison import (
    move_attacks_enemy_stack_that_threatens_origin,
    prune_move_unit_ids_for_garrison_floor,
)
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.eval_weights import board_score, get_eval_weights
from backend.ai.mcts.search import Problem

# Cap on branching factor per node (top-K proposals + optional end_phase).
MAX_COMBAT_MOVE_BRANCHES = 12


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
        # Eval weights fixed for this search (env read once per decide() call).
        self._eval_weights = get_eval_weights()
        # (stacks, territory, options) -> (win rate, attacker casualty cost, defender casualty cost)
        self._battle_cache: dict[tuple, tuple[float, float, float]] = {}
        # Root-phase constraints (API-provided); recomputed per state where needed.
        self._root_can_end_phase = bool(
            (ctx.available_actions or {}).get("can_end_phase", True)
        )
        # Per-instance memo of the top-K enumeration, keyed by id(state) with the
        # state held so the id stays valid. The search calls legal_actions() then
        # priors() on the same node state, and both enumerate from scratch — on the
        # move phases a single enumeration is the dominant per-expansion cost, so
        # sharing it is the difference between ~2x and ~1x that cost per expansion.
        self._enum_memo: dict[int, tuple[Any, list]] = {}

    def _enumerate_top_k_cached(self, state: Any) -> list:
        """_enumerate_top_k memoized per state object (states are treated immutable)."""
        key = id(state)
        hit = self._enum_memo.get(key)
        if hit is not None and hit[0] is state:
            return hit[1]
        result = self._enumerate_top_k(state)
        self._enum_memo[key] = (state, result)
        return result

    # ----- Problem protocol -----

    def legal_actions(self, state: Any) -> list[HashableAction]:
        """Top-K move_units candidates + end_phase when legal."""
        if state.winner is not None:
            return []
        if (state.phase or "combat_move") != "combat_move":
            return []

        rescue = self._ladder_rescue_actions(state)
        if rescue is not None:
            return rescue

        actions: list[HashableAction] = []
        for from_tid, to_tid, unit_ids, charge_through in self._enumerate_top_k_cached(state):
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
        Heuristic priors: weight each top-K move by its combat-sim expected gain;
        end_phase gets a modest baseline so it is considered when no good moves remain.
        """
        if state.winner is not None or (state.phase or "combat_move") != "combat_move":
            return None

        rescue = self._ladder_rescue_actions(state)
        if rescue is not None:
            return {
                action: (1.0 if action.type == "move_units" else 0.4)
                for action in rescue
            }

        scored: list[tuple[HashableAction, float]] = []
        for from_tid, to_tid, unit_ids, charge_through in self._enumerate_top_k_cached(state):
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
        garrison pruning, charge-path intersection). Defended battles are ranked by
        the combat sim. A fight below the minimum win rate, or with negative expected
        gain and no confident win, is not offered.
        """
        faction_id = self.faction_id
        ud, td, fd = self.ud, self.td, self.fd

        # Post-pending snapshot is only for reachability (embark slots, occupancy).
        # A queue that already cannot replay is not a board we can plan on; bail out
        # instead of raising out of the search.
        try:
            if any(getattr(pm, "phase", None) == "combat_move" for pm in (state.pending_moves or [])):
                state_after = get_state_after_pending_moves(state, "combat_move", ud, td, fd)
            else:
                state_after = state  # no same-phase pendings: post-pending board is this state
        except ValueError:
            return []
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
            targets, charge_routes = get_unit_move_targets(state, iid, ud, td, fd, state_after)
            for to_tid in (targets or {}).keys():
                if not to_tid or to_tid == from_tid:
                    continue
                if not _is_attackable_territory(state, to_tid, faction_id, fd, ud, td):
                    continue
                key = (from_tid, to_tid)
                from_to_units.setdefault(key, []).append(iid)
                if charge_routes.get(to_tid):
                    charge_routes_per_unit.setdefault(key, {})[iid] = charge_routes[to_tid]

        # One batched full-board reachability pass for every origin below, instead of
        # a per-candidate threat scan (each of which walked every enemy unit).
        from backend.ai.geography import count_enemies_that_can_reach_territories

        origin_ids = {from_tid for from_tid, _to_tid in from_to_units}
        threat_map = count_enemies_that_can_reach_territories(
            origin_ids, state, faction_id, fd, td, ud
        )

        candidates: list[tuple] = []
        for (from_tid, to_tid), unit_ids in from_to_units.items():
            if not unit_ids:
                continue
            # Garrison pruning: do not strip land from a threatened origin unless
            # this move is a counterattack into the threatening stack.
            threat_relief = move_attacks_enemy_stack_that_threatens_origin(
                from_tid, to_tid, state, faction_id, fd, td, ud
            )
            threat_from = threat_map.get(from_tid, 0)
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
                effective_reach_count=threat_from,
            )
            if not pruned:
                continue
            pruned = filter_unit_instances_that_can_reach(state, to_tid, pruned, ud, td, fd, state_after)
            if not pruned:
                continue
            # Sea -> land: only land units may move (naval cannot go on land).
            from_def = td.get(from_tid)
            to_def = td.get(to_tid)
            sea_raid = bool(
                from_def and to_def and _is_water_zone(from_def) and not _is_water_zone(to_def)
            )
            if sea_raid:
                terr = state.territories.get(from_tid)
                ids_set = set(pruned)
                pruned = [
                    u.instance_id
                    for u in (getattr(terr, "units", []) or [])
                    if u.instance_id in ids_set and not self._is_boat(u.unit_id)
                ]
                if not pruned:
                    continue
            charge_options = self._charge_path_options(
                state, (from_tid, to_tid), pruned, charge_routes_per_unit
            )
            if sea_raid:
                pruned = _with_raid_boats(state, from_tid, pruned, ud)
            if unit_ids_are_unaccompanied_ladders(state, ud, pruned, to_tid):
                continue
            # Rank by the preferred charge; a later option is substituted only
            # when that path does not survive a full pending replay.
            score = self._cheap_move_score(
                state, from_tid, to_tid, pruned, charge_options[0], state_after=state_after
            )
            if score <= 0:
                continue
            candidates.append((score, from_tid, to_tid, pruned, charge_options))

        candidates.sort(key=lambda x: x[0], reverse=True)
        # validate_action checks each move against the pre-pending board. Replaying
        # the whole combat_move queue is stricter: a charge through a hex another
        # pending move occupies, or a sail whose steps sum past remaining_movement
        # (the "need 3, have 2" ship failure), raises inside _apply_pending_moves.
        # Drop those candidates here so the search never queues them.
        accepted: list[tuple[str, str, list[str], list[str]]] = []
        for _score, from_tid, to_tid, unit_ids, charge_options in candidates:
            for charge_through in charge_options:
                if not self._must_replay_candidate(state, from_tid, to_tid, unit_ids, charge_through):
                    accepted.append((from_tid, to_tid, unit_ids, charge_through))
                    break
                if self._pending_sequence_replays(state, from_tid, to_tid, unit_ids, charge_through):
                    accepted.append((from_tid, to_tid, unit_ids, charge_through))
                    break
            if len(accepted) >= MAX_COMBAT_MOVE_BRANCHES:
                break
        return accepted

    def _is_boat(self, unit_id: str) -> bool:
        from backend.engine.queries import _is_water_boat

        return _is_water_boat(self.ud.get(unit_id))

    def _charge_path_options(
        self,
        state: Any,
        key: tuple[str, str],
        unit_ids_for_move: list[str],
        charge_routes_per_unit: dict[tuple[str, str], dict[str, list[list[str]]]],
    ) -> list[list[str]]:
        """
        Charge paths shared by every unit in the move, best conquest first.

        The empty path is always last so a direct move can replace a charge that
        would pass through a hex another pending move fills.
        """
        per_unit = charge_routes_per_unit.get(key, {})
        if not unit_ids_for_move or not per_unit:
            return [[]]
        valid_paths: set[tuple[str, ...]] | None = None
        for uid in unit_ids_for_move:
            paths = per_unit.get(uid, [])
            path_tuples = {tuple(p) for p in paths} if paths else {()}
            valid_paths = path_tuples if valid_paths is None else (valid_paths & path_tuples)
        if not valid_paths:
            return [[]]
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

        ranked = [list(path) for path in sorted(valid_paths, key=path_sort_key) if path]
        ranked.append([])
        return ranked

    def _must_replay_candidate(
        self,
        state: Any,
        from_tid: str,
        to_tid: str,
        unit_ids: list[str],
        charge_through: list[str],
    ) -> bool:
        """
        True when this candidate can disagree with validate_action.

        A lone non-charging land move uses the same shortest path validate_action
        already checked. Charges, sea moves, and anything queued beside existing
        pending moves can fail only when the whole queue is replayed.
        """
        if charge_through:
            return True
        if any(getattr(pm, "phase", None) == "combat_move" for pm in (state.pending_moves or [])):
            return True
        if _is_water_zone(self.td.get(from_tid)) or _is_water_zone(self.td.get(to_tid)):
            return True
        return False

    def _pending_sequence_replays(
        self,
        state: Any,
        from_tid: str,
        to_tid: str,
        unit_ids: list[str],
        charge_through: list[str],
    ) -> bool:
        """True when existing combat_move pending moves plus this one apply cleanly."""
        action = move_units(
            self.faction_id,
            from_tid,
            to_tid,
            list(unit_ids),
            charge_through=list(charge_through) if charge_through else None,
        )
        try:
            new_state, _events = apply_action(
                state,
                action,
                self.ud,
                self.td,
                self.fd,
                self.cd,
                self.port_d,
            )
            get_state_after_pending_moves(new_state, "combat_move", self.ud, self.td, self.fd)
        except ValueError:
            return False
        return True

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

    # ----- Battle scoring (combat sim) -----

    def _board_after_pending(self, state: Any) -> Any:
        """Board after combat-move orders already queued. Falls back to `state` if replay fails."""
        try:
            if any(getattr(pm, "phase", None) == "combat_move" for pm in (state.pending_moves or [])):
                return get_state_after_pending_moves(state, "combat_move", self.ud, self.td, self.fd)
        except ValueError:
            return state
        return state

    def _sim_battle(
        self,
        board: Any,
        att_stacks: list[dict],
        def_stacks: list[dict],
        territory_id: str,
        *,
        sea_raid: bool,
    ) -> tuple[float, float]:
        """Win rate and expected net gain from the combat sim. Cached for this search."""
        tdef = self.td.get(territory_id)
        stronghold = bool(getattr(tdef, "is_stronghold", False))
        terr = board.territories.get(territory_id) if board is not None else None
        hp = None
        if stronghold and tdef is not None:
            base = int(getattr(tdef, "stronghold_base_health", 0) or 0)
            if base > 0:
                cur = getattr(terr, "stronghold_current_health", None) if terr is not None else None
                hp = int(cur) if cur is not None else base
        opts = SimOptions(
            must_conquer=stronghold,
            casualty_order_attacker="best_attack" if stronghold else "best_unit",
            is_sea_raid=sea_raid,
            stronghold_initial_hp=hp,
        )
        key = (
            tuple(sorted((s.get("unit_id"), int(s.get("count") or 0)) for s in att_stacks)),
            tuple(sorted((s.get("unit_id"), int(s.get("count") or 0)) for s in def_stacks)),
            territory_id,
            stronghold,
            sea_raid,
            hp,
        )
        cached = self._battle_cache.get(key)
        if cached is None:
            sim = run_simulation(
                att_stacks,
                def_stacks,
                territory_id,
                self.ud,
                self.td,
                n_trials=COMBAT_SIM_TRIALS,
                options=opts,
                seed=1,
            )
            cached = (
                float(sim.p_attacker_win),
                float(sim.attacker_casualty_cost_mean),
                float(sim.defender_casualty_cost_mean),
            )
            self._battle_cache[key] = cached
        p_win, att_cas, def_cas = cached
        # Killing an unowned garrison is not an economic gain.
        if terr is not None and getattr(terr, "owner", None) is None:
            def_cas = 0.0
        raw = expected_net_gain(
            p_win,
            territory_id,
            self.td,
            self.fd,
            def_cas,
            att_cas,
            self.cd,
            self.port_d,
            self.ud,
        )
        net = scale_combat_gain(raw - def_cas + att_cas, def_cas, att_cas)
        return p_win, net

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
        Rank a candidate by the combat sim's expected gain. A likely loss stays
        when the casualties and the territory still come out ahead. Aggression
        slides the balance between those two. A negative total is dropped because
        ending the phase is worth more.
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

        board = state_after if state_after is not None else self._board_after_pending(state)
        from_def = self.td.get(from_tid)
        to_def = self.td.get(to_tid)
        sea_raid = bool(from_def and to_def and _is_water_zone(from_def) and not _is_water_zone(to_def))

        def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
            combined: dict[str, int] = {}
            for stack in base + extra:
                uid = stack.get("unit_id")
                if not uid:
                    continue
                combined[uid] = combined.get(uid, 0) + int(stack.get("count") or 0)
            return [{"unit_id": uid, "count": count} for uid, count in combined.items()]

        # Charge-through fights the moving stack alone. The destination fight
        # includes attackers already committed there. A via we expect to lose
        # value on lowers the move; it does not delete it on its own.
        via_adjustment = 0.0
        for via in charge_through or []:
            via_def = _defender_stacks(board, via, self.faction_id, self.fd, self.ud)
            if not via_def:
                continue
            _p_via, net_via = self._sim_battle(board, move_stacks, via_def, via, sea_raid=False)
            via_adjustment += net_via

        def_stacks = _defender_stacks(board, to_tid, self.faction_id, self.fd, self.ud)
        if not def_stacks:
            total = 0.0
            if self._is_conquest(to_tid, state):
                total += battle_gain_if_win(to_tid, self.td, self.fd)
            for via in [to_tid, *(charge_through or [])]:
                if self._is_conquest(via, state):
                    total += expected_net_gain(
                        1.0, via, self.td, self.fd, 0.0, 0.0, self.cd, self.port_d, self.ud
                    )
            return total + via_adjustment

        committed = _committed_attacker_stacks(board, to_tid, self.faction_id, self.ud, self.fd)
        att_stacks = _merge(committed, move_stacks)
        _p_win, net = self._sim_battle(board, att_stacks, def_stacks, to_tid, sea_raid=sea_raid)
        return net + via_adjustment

    def _ladder_rescue_actions(self, state: Any) -> list[HashableAction] | None:
        """Partner moves or a cancel when a ladder is already attacking alone.

        Returns None when no ladder is stuck. A stuck ladder is the only thing
        this position may do: the phase cannot end until it is joined or cancelled,
        and another solo ladder is not offered.
        """
        alone = get_ladder_only_attack_destinations(state, self.ud)
        if not alone:
            return None
        pending_unit_ids: set[str] = set()
        for pm in state.pending_moves or []:
            if getattr(pm, "phase", None) == "combat_move":
                pending_unit_ids.update(getattr(pm, "unit_instance_ids", None) or [])
        groups: dict[tuple[str, str], list[str]] = {}
        for unit_info in get_movable_units(state, self.faction_id, self.ud, self.fd):
            iid = unit_info.get("instance_id")
            from_tid = unit_info.get("territory_id")
            if not iid or not from_tid or iid in pending_unit_ids:
                continue
            if is_ladder_instance(state, self.ud, iid):
                continue
            targets, _routes = get_unit_move_targets(state, iid, self.ud, self.td, self.fd)
            for to_tid in alone:
                if to_tid in (targets or {}):
                    groups.setdefault((from_tid, to_tid), []).append(iid)
        actions: list[HashableAction] = []
        for (from_tid, to_tid), unit_ids in groups.items():
            action = move_units(self.faction_id, from_tid, to_tid, unit_ids)
            if validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d).valid:
                actions.append(HashableAction(action))
        if actions:
            return actions
        alone_ids = {iid for ids in alone.values() for iid in ids}
        for index, pm in enumerate(state.pending_moves or []):
            if getattr(pm, "phase", None) != "combat_move":
                continue
            if not alone_ids.intersection(getattr(pm, "unit_instance_ids", None) or []):
                continue
            action = cancel_move(self.faction_id, index)
            if validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d).valid:
                actions.append(HashableAction(action))
        return actions

    # ----- End-phase legality -----

    def _can_end_phase(self, state: Any) -> bool:
        """
        True when combat_move may end: no loaded boats that must attack and no
        unresolved idle sails (mirrors the API / reducer end-phase guards).
        """
        try:
            if any(getattr(pm, "phase", None) == "combat_move" for pm in (state.pending_moves or [])):
                sim = get_state_after_pending_moves(state, "combat_move", self.ud, self.td, self.fd)
            else:
                sim = state  # no same-phase pendings: post-pending board is this state
        except ValueError:
            return False
        if get_ladder_only_attack_destinations(state, self.ud):
            return False
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
        return board_score(state, self.faction_id, self.td, self.fd, self.ud, self._eval_weights)


def decide_combat_move_mcts(ctx: AIContext, rng: random.Random | None = None) -> Action | None:
    """
    Entry point: run combat-move MCTS and return the chosen root action.
    Falls back to None if search yields nothing (caller keeps the heuristic path).
    """
    from backend.ai.mcts.budget import get_phase_budget, resolve_game_difficulty
    from backend.ai.mcts.search import run_mcts_reuse

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None
    if (ctx.state.phase or "combat_move") != "combat_move":
        return None

    problem = CombatMoveProblem(ctx, rng=rng)
    budget = get_phase_budget("combat_move", resolve_game_difficulty(ctx.state))
    # 3.4: moves apply verbatim, so consecutive /ai-step calls chain the same
    # multi-move subtree instead of re-searching the expensive enumerator.
    action = run_mcts_reuse(problem, ctx.state, budget)
    if action is None:
        return None
    if isinstance(action, HashableAction):
        return action.unwrap()
    return action
