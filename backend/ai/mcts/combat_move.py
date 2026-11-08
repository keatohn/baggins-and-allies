"""
Combat-move MCTS Problem: top-K heuristic proposal enumerator + leaf evaluator.

One option is one attack on one territory. Every origin that can reach it is
part of that attack. Mixes add unit types from weakest to strongest, so the
strongest type is never the first mix.

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
from backend.engine.combat_sim import (
    SimOptions,
    run_simulation,
    sim_prefire_penalty,
    sim_stronghold_initial_hp,
)

from backend.ai.formulas import (
    battle_gain_if_win,
    expected_net_gain,
    get_unit_power_cost,
)
from backend.ai.habits import COMBAT_SIM_TRIALS
from backend.ai.style import attack_spread_adjustment, scale_combat_gain
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
from backend.ai.mcts.budget import BRANCH_LIMITS, branch_limit, resolve_game_difficulty
from backend.ai.mcts.eval_weights import board_score, get_eval_weights
from backend.ai.mcts.search import Problem

# Default cap when no difficulty level is loaded. A level can raise this up to the admin ceiling.
MAX_COMBAT_MOVE_BRANCHES = BRANCH_LIMITS["combat_move"][0]


def attack_stack_variants(groups: list[tuple[str, list[str], float]]) -> list[list[str]]:
    """Mixes that add unit types from weakest attack to strongest.

    `groups` is (unit type, instance ids, attack stat). The first mix is the
    weakest type. The strongest type is only added once every weaker type is
    already in the mix, and it is never offered on its own when a weaker type
    can come along.
    """
    ordered = sorted(groups, key=lambda group: (group[2], group[0]))
    seen: set[tuple[str, ...]] = set()
    mixes: list[list[str]] = []
    running: list[str] = []
    for _unit_id, ids, _attack in ordered:
        running = [*running, *ids]
        key = tuple(sorted(running))
        if not key or key in seen:
            continue
        seen.add(key)
        mixes.append(list(running))
    return mixes


def _bundle_move_action(
    faction_id: str, legs: list[tuple[str, str, list[str], list[str]]]
) -> Action:
    """One engine action for the whole attack. Later origins ride along in bundle_rest."""
    from_tid, to_tid, unit_ids, charge = legs[0]
    action = move_units(
        faction_id,
        from_tid,
        to_tid,
        list(unit_ids),
        charge_through=list(charge) if charge else None,
    )
    rest = []
    for origin, dest, ids, leg_charge in legs[1:]:
        leg = {
            "from": origin,
            "to": dest,
            "unit_instance_ids": list(ids),
        }
        if leg_charge:
            leg["charge_through"] = list(leg_charge)
        rest.append(leg)
    if rest:
        action.payload["bundle_rest"] = rest
    return action


def apply_bundled_moves(state, action: Action, ud, td, fd, cd, port_d):
    """Apply the first move and every origin stored on bundle_rest. The caller's action is not mutated."""
    rest = list(action.payload.get("bundle_rest") or [])
    primary = Action(
        type=action.type,
        faction=action.faction,
        payload={key: value for key, value in action.payload.items() if key != "bundle_rest"},
    )
    new_state, events = apply_action(state, primary, ud, td, fd, cd, port_d)
    for leg in rest:
        follow = move_units(
            action.faction,
            leg["from"],
            leg["to"],
            list(leg.get("unit_instance_ids") or []),
            charge_through=leg.get("charge_through") or None,
        )
        new_state, more = apply_action(new_state, follow, ud, td, fd, cd, port_d)
        events.extend(more)
    return new_state, events


def _bundle_applies(state, action: Action, ud, td, fd, cd, port_d) -> bool:
    """True when every origin in the bundle queues and the combat-move queue replays."""
    from backend.engine.reducer import get_state_after_pending_moves

    try:
        new_state, _events = apply_bundled_moves(state, action, ud, td, fd, cd, port_d)
        if any(getattr(pm, "phase", None) == "combat_move" for pm in (new_state.pending_moves or [])):
            get_state_after_pending_moves(new_state, "combat_move", ud, td, fd)
    except (TypeError, ValueError, KeyError, AttributeError):
        return False
    return True


def omission_rank_score(
    full_score: float, sim_score: float, randomness_knob: float, unit_count: int
) -> float:
    """Rank a leave-one-out attack under the full stack.

    Randomness 0 keeps the score at 0, so the unit stays in the attack. As the
    knob rises the omission can be chosen, and it stays a fraction of the full
    attack: one unit's share, times the knob.
    """
    if randomness_knob <= 0 or unit_count <= 0 or sim_score <= 0 or full_score <= 0:
        return 0.0
    return min(sim_score, full_score) * randomness_knob / unit_count


def _moves_without_instance(
    moves: list[tuple[str, str, list[str], list[str]]], drop: str
) -> list[tuple[str, str, list[str], list[str]]]:
    reduced: list[tuple[str, str, list[str], list[str]]] = []
    for origin, dest, ids, charge in moves:
        kept = [iid for iid in ids if iid != drop]
        if kept:
            reduced.append((origin, dest, kept, charge))
    return reduced


def _merge_stacks(base: list[dict], extra: list[dict]) -> list[dict]:
    combined: dict[str, int] = {}
    for stack in base + extra:
        uid = stack.get("unit_id")
        if not uid:
            continue
        combined[uid] = combined.get(uid, 0) + int(stack.get("count") or 0)
    return [{"unit_id": uid, "count": count} for uid, count in combined.items()]


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
        for _score, legs in self._enumerate_top_k_cached(state):
            action = _bundle_move_action(self.faction_id, legs)
            if _bundle_applies(state, action, self.ud, self.td, self.fd, self.cd, self.port_d):
                actions.append(HashableAction(action))

        if self._can_end_phase(state):
            end_action = end_phase(self.faction_id)
            v = validate_action(state, end_action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(end_action))

        cap = branch_limit("combat_move", resolve_game_difficulty(state))
        return actions[: cap + 1]

    def apply(self, state: Any, action: HashableAction) -> Any:
        """Apply via the reducer. apply_action deep-copies, so `state` is never mutated."""
        from backend.engine.reducer import apply_action

        raw = action.unwrap() if isinstance(action, HashableAction) else action
        if raw.type == "move_units" and raw.payload.get("bundle_rest"):
            new_state, _events = apply_bundled_moves(
                state, raw, self.ud, self.td, self.fd, self.cd, self.port_d
            )
            return new_state
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
        """Board score plus the combat sim of every attack already queued."""
        return self._score_board(state) + self._pending_attack_value(state)

    def terminal_value(self, state: Any) -> float:
        """Terminal leaf: winner short-circuits to ±1; otherwise board plus queued attacks."""
        if state.winner is not None:
            return self._winner_value(state)
        return self._score_board(state) + self._pending_attack_value(state)

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
        for score, legs in self._enumerate_top_k_cached(state):
            action = _bundle_move_action(self.faction_id, legs)
            if not _bundle_applies(state, action, self.ud, self.td, self.fd, self.cd, self.port_d):
                continue
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

        reachable: dict[tuple[str, bool], list[tuple[str, list[str]]]] = {}
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
            reachable.setdefault((to_tid, sea_raid), []).append((from_tid, list(pruned)))

        bundles: list[tuple[float, list[tuple[str, str, list[str], list[str]]]]] = []
        for (to_tid, sea_raid), legs in reachable.items():
            for score, moves in self._attack_bundles(
                state, state_after, to_tid, sea_raid, legs, charge_routes_per_unit
            ):
                if score <= 0:
                    continue
                action = _bundle_move_action(self.faction_id, moves)
                if not _bundle_applies(state, action, ud, td, fd, self.cd, self.port_d):
                    continue
                bundles.append((score, moves))

        for score, moves in self._one_unit_omissions(state, state_after, bundles):
            action = _bundle_move_action(self.faction_id, moves)
            if _bundle_applies(state, action, ud, td, fd, self.cd, self.port_d):
                bundles.append((score, moves))

        bundles.sort(key=lambda item: item[0], reverse=True)
        cap = branch_limit("combat_move", resolve_game_difficulty(state))
        return bundles[:cap]

    def _attack_bundles(
        self,
        state: Any,
        board: Any,
        to_tid: str,
        sea_raid: bool,
        legs: list[tuple[str, list[str]]],
        charge_routes_per_unit: dict,
    ) -> list[tuple[float, list[tuple[str, str, list[str], list[str]]]]]:
        """One battle per mix. Every origin that can reach `to_tid` is in the mix."""
        by_instance: dict[str, str] = {}
        origin_of: dict[str, str] = {}
        for from_tid, unit_ids in legs:
            terr = state.territories.get(from_tid)
            for unit in getattr(terr, "units", []) or []:
                iid = getattr(unit, "instance_id", "")
                if iid in unit_ids:
                    by_instance[iid] = getattr(unit, "unit_id", "")
                    origin_of[iid] = from_tid
        groups: dict[str, list[str]] = {}
        for iid, uid in by_instance.items():
            if uid:
                groups.setdefault(uid, []).append(iid)
        typed: list[tuple[str, list[str], float]] = []
        for uid, ids in groups.items():
            udef = self.ud.get(uid)
            attack = float(getattr(udef, "attack", 0) or 0)
            typed.append((uid, ids, attack))
        bundles: list[tuple[float, list[tuple[str, str, list[str], list[str]]]]] = []
        for mix in attack_stack_variants(typed):
            by_origin: dict[str, list[str]] = {}
            for iid in mix:
                by_origin.setdefault(origin_of[iid], []).append(iid)
            moves: list[tuple[str, str, list[str], list[str]]] = []
            stacks: list[dict] = []
            non_ladder = False
            ladder_moves: list[tuple[str, str, list[str], list[str]]] = []
            for from_tid, ids in sorted(by_origin.items()):
                moving = _with_raid_boats(state, from_tid, ids, self.ud) if sea_raid else list(ids)
                leg_stacks = _stacks_for_unit_ids(state, from_tid, moving, self.ud)
                if not leg_stacks:
                    continue
                charge = self._charge_path_options(
                    state, (from_tid, to_tid), moving, charge_routes_per_unit
                )[0]
                leg = (from_tid, to_tid, moving, charge)
                if unit_ids_are_unaccompanied_ladders(state, self.ud, moving, to_tid):
                    ladder_moves.append(leg)
                else:
                    non_ladder = True
                    moves.append(leg)
                    stacks = _merge_stacks(stacks, leg_stacks)
            if non_ladder:
                for leg in ladder_moves:
                    moves.append(leg)
                    stacks = _merge_stacks(
                        stacks, _stacks_for_unit_ids(state, leg[0], leg[2], self.ud)
                    )
            elif not moves:
                continue
            net = self._joined_battle_net(board, stacks, to_tid, sea_raid)
            bundles.append((net + attack_spread_adjustment(state, to_tid), moves))
        return bundles

    def _one_unit_omissions(
        self,
        state: Any,
        board: Any,
        bundles: list,
    ) -> list[tuple[float, list]]:
        """Leave-one-out mixes for the biggest attack. Ranked under the full stack."""
        from backend.ai.style import randomness

        knob = randomness()
        if knob <= 0 or not bundles:
            return []
        full_score, moves = max(
            bundles, key=lambda item: sum(len(leg[2]) for leg in item[1])
        )
        unit_ids = [iid for _origin, _dest, ids, _charge in moves for iid in ids]
        if len(unit_ids) < 2:
            return []
        by_type: dict[str, list[str]] = {}
        for origin, _dest, ids, _charge in moves:
            terr = state.territories.get(origin)
            for unit in getattr(terr, "units", []) or []:
                iid = getattr(unit, "instance_id", "")
                if iid in ids:
                    by_type.setdefault(getattr(unit, "unit_id", "") or iid, []).append(iid)
        to_tid = moves[0][1]
        from_tid = moves[0][0]
        sea_raid = bool(
            _is_water_zone(self.td.get(from_tid)) and not _is_water_zone(self.td.get(to_tid))
        )
        already = {
            tuple(sorted(iid for _o, _d, ids, _c in item[1] for iid in ids))
            for item in bundles
        }
        omissions: list[tuple[float, list]] = []
        for ids in by_type.values():
            drop = ids[0]
            reduced = _moves_without_instance(moves, drop)
            key = tuple(sorted(iid for _o, _d, kept, _c in reduced for iid in kept))
            if not key or key in already:
                continue
            stacks: list[dict] = []
            for origin, _dest, kept, _charge in reduced:
                stacks = _merge_stacks(stacks, _stacks_for_unit_ids(state, origin, kept, self.ud))
            sim_score = self._joined_battle_net(board, stacks, to_tid, sea_raid)
            sim_score += attack_spread_adjustment(state, to_tid)
            score = omission_rank_score(full_score, sim_score, knob, len(unit_ids))
            if score <= 0:
                continue
            omissions.append((score, reduced))
            already.add(key)
        return omissions

    def _pending_attack_value(self, state: Any) -> float:
        """Squashed sim value of attacks queued this phase, each with every origin that is going."""
        memo = getattr(self, "_pending_value_memo", None)
        if memo is not None and memo[0] is state:
            return memo[1]
        total = 0.0
        try:
            pending = [
                pm
                for pm in (state.pending_moves or [])
                if getattr(pm, "phase", None) == "combat_move"
            ]
            board = (
                get_state_after_pending_moves(state, "combat_move", self.ud, self.td, self.fd)
                if pending
                else state
            )
        except ValueError:
            self._pending_value_memo = (state, 0.0)
            return 0.0
        seen: set[str] = set()
        for move in pending:
            dest = getattr(move, "to_territory", None)
            if not dest or dest in seen:
                continue
            seen.add(dest)
            origin = getattr(move, "from_territory", None)
            sea_raid = bool(
                _is_water_zone(self.td.get(origin)) and not _is_water_zone(self.td.get(dest))
            )
            committed = _committed_attacker_stacks(board, dest, self.faction_id, self.ud, self.fd)
            if not committed:
                continue
            total += self._joined_battle_net(board, [], dest, sea_raid)
        value = total / (1.0 + abs(total)) if total else 0.0
        self._pending_value_memo = (state, value)
        return value

    def _joined_battle_net(
        self,
        board: Any,
        move_stacks: list[dict],
        to_tid: str,
        sea_raid: bool,
    ) -> float:
        """Expected gain of these stacks plus attackers already committed to this destination."""
        committed = _committed_attacker_stacks(board, to_tid, self.faction_id, self.ud, self.fd)
        attackers = _merge_stacks(committed, move_stacks)
        defenders = _defender_stacks(board, to_tid, self.faction_id, self.fd, self.ud)
        if not defenders:
            if not self._is_conquest(to_tid, board):
                return 0.0
            return expected_net_gain(
                1.0, to_tid, self.td, self.fd, 0.0, 0.0, self.cd, self.port_d, self.ud
            )
        _win, net = self._sim_battle(board, attackers, defenders, to_tid, sea_raid=sea_raid)
        return net

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
        hp = sim_stronghold_initial_hp(board, territory_id, tdef)
        opts = SimOptions(
            must_conquer=stronghold,
            casualty_order_attacker="best_attack" if stronghold else "best_unit",
            is_sea_raid=sea_raid,
            stronghold_initial_hp=hp,
            prefire_penalty=sim_prefire_penalty(board),
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
        raises the territory and enemy dead, lowers our dead, and adds a growing
        bonus for each new battle. A negative total is dropped because ending
        the phase is worth more.
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
            return total + via_adjustment + attack_spread_adjustment(state, to_tid)

        committed = _committed_attacker_stacks(board, to_tid, self.faction_id, self.ud, self.fd)
        att_stacks = _merge(committed, move_stacks)
        _p_win, net = self._sim_battle(board, att_stacks, def_stacks, to_tid, sea_raid=sea_raid)
        return net + via_adjustment + attack_spread_adjustment(state, to_tid)

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
        """Units, strongholds, capitals, and power from our perspective."""
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
