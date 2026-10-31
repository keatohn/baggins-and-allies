"""
Mobilization-phase MCTS Problem: top-K destination assignment enumerator + leaf evaluator.

Actions:
- queue_camp_placement(faction, camp_index, territory_id) for unplaced purchased camps
- mobilize_units(faction, destination, unit_stacks) for top-K destination assignments
- end_phase(faction) when legal (terminal for this problem)

Multi-step composition: MCTS sequences camp queueing and multiple mobilize_units
calls (home slots, camp destinations, sea zones, river zones) before ending the
phase — composing multi-destination deployment the one-action-at-a-time greedy
policy cannot.

Note: camps use queue_camp_placement (applied at end of phase) rather than
immediate place_camp — place_camp is not in the reducer's mobilization
PHASE_ALLOWED_ACTIONS, so only queueing is valid through apply_action.

Heuristics remain evaluators/priors, not sole deciders. Expansion uses cheap
formulas only (no defense sims) so the tree stays affordable.
"""

from __future__ import annotations

import os
import random
from typing import Any

from backend.engine.actions import Action, end_phase, mobilize_units, queue_camp_placement
from backend.engine.queries import (
    _is_naval_unit,
    _is_river_unit,
    _purchase_kind,
    get_mobilization_capacity,
    get_purchased_units,
    valid_camp_placement_territory_ids,
    validate_action,
)
from backend.engine.utils import faction_owns_capital

from backend.ai.context import AIContext
from backend.ai.formulas import get_unit_power_cost, territory_reinforce_base_score
from backend.ai.mobilization import (
    _can_mobilize_home_unit,
    _distance_to_nearest_enemy_territory_with_units,
    _remaining_land_capacity,
    _remaining_sea_capacity,
)
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.search import Problem

# Cap on branching factor per node (top-K proposals + optional end_phase).
MAX_MOBILIZATION_BRANCHES = 12

DEFAULT_DIFFICULTY_ENV = "AI_MCTS_DIFFICULTY"

# Soft-normalize scale for board evaluation (shared with combat_move style).
_BOARD_SCALE = 1.0

# Extra prior weight for queue_camp_placement (camps block end_phase until placed).
_CAMP_PRIOR_BONUS = 1.5

# Spread penalty for already pending this phase (same as greedy).
_SPREAD_PENALTY = 8.0


class MobilizationProblem(Problem):
    """
    MCTS problem for the mobilization phase.

    States are GameState snapshots. Actions are engine Action objects:
    - queue_camp_placement(faction, camp_index, territory_id) for an unplaced camp option
    - mobilize_units(faction, destination, unit_stacks) for a top-K assignment
    - end_phase(faction) when legal (terminal for this problem)

    Multi-step composition: MCTS sequences camp queueing and several mobilize_units
    calls, then ends the phase.
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

    # ----- Problem protocol -----

    def legal_actions(self, state: Any) -> list[HashableAction]:
        """Top-K queue_camp_placement / mobilize_units candidates + end_phase when legal."""
        if state.winner is not None:
            return []
        if (state.phase or "mobilization") != "mobilization":
            return []

        actions: list[HashableAction] = []
        for raw in self._enumerate_candidates(state):
            v = validate_action(state, raw, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(raw))

        if self._can_end_phase(state):
            end_action = end_phase(self.faction_id)
            v = validate_action(state, end_action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(end_action))

        return actions[: MAX_MOBILIZATION_BRANCHES + 1]

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
        """Episode ends when the phase advances past mobilization or the game is over."""
        if state.winner is not None:
            return True
        return (state.phase or "mobilization") != "mobilization"

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
        Heuristic priors: weight each top-K candidate by its cheap destination score;
        queue_camp_placement gets a bonus (camps block end_phase); end_phase gets a modest baseline.
        """
        if state.winner is not None or (state.phase or "mobilization") != "mobilization":
            return None

        priors: dict[HashableAction, float] = {}
        for raw in self._enumerate_candidates(state):
            v = validate_action(state, raw, self.ud, self.td, self.fd, self.cd, self.port_d)
            if not v.valid:
                continue
            score = self._cheap_action_score(state, raw)
            if raw.type == "queue_camp_placement":
                score += _CAMP_PRIOR_BONUS
            priors[HashableAction(raw)] = max(0.05, score)

        if self._can_end_phase(state):
            priors[HashableAction(end_phase(self.faction_id))] = 0.35
        return priors if priors else None

    # ----- Enumeration (top-K proposal generator) -----

    def _enumerate_candidates(self, state: Any) -> list[Action]:
        """
        Generate up to MAX_MOBILIZATION_BRANCHES ranked Action candidates.

        Order of generation (cheap formulas only, no defense sims):
        1. queue_camp_placement for unplaced camps (scored by destination value)
        2. home mobilize (unit matches home slot)
        3. land camp destinations with remaining power
        4. sea zones (naval)
        5. river zones (river)
        """
        candidates: list[tuple[float, Action]] = []

        # 1) Camp placements (queued; applied at end of phase — only action the reducer allows)
        for camp_index, tid in self._enumerate_camp_placements(state):
            candidates.append(
                (
                    self._cheap_destination_score(state, tid) + _CAMP_PRIOR_BONUS,
                    queue_camp_placement(self.faction_id, camp_index, tid),
                )
            )

        # 2-5) Mobilize units (requires capital)
        if faction_owns_capital(state, self.faction_id, self.fd):
            candidates.extend(self._enumerate_mobilize(state))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [act for _score, act in candidates[:MAX_MOBILIZATION_BRANCHES]]

    def _enumerate_camp_placements(self, state: Any) -> list[tuple[int, str]]:
        """(camp_index, territory_id) for unplaced camps, ranked by cheap score."""
        pending_camps = getattr(state, "pending_camps", []) or []
        queued_indices = {
            p.camp_index for p in (getattr(state, "pending_camp_placements", []) or [])
        }
        out: list[tuple[float, tuple[int, str]]] = []
        for camp_index, entry in enumerate(pending_camps):
            if entry.get("placed_territory_id"):
                continue
            if camp_index in queued_indices:
                continue
            options = valid_camp_placement_territory_ids(
                state, self.faction_id, camp_index, self.cd, self.td
            )
            if not options:
                continue
            for tid in options:
                score = self._cheap_destination_score(state, tid)
                out.append((score, (camp_index, tid)))
        out.sort(key=lambda x: x[0], reverse=True)
        # Dedupe by camp_index, keep best option per camp (MCTS composes multiple camps).
        seen_camps: set[int] = set()
        result: list[tuple[int, str]] = []
        for _score, (ci, tid) in out:
            if ci in seen_camps:
                continue
            seen_camps.add(ci)
            result.append((ci, tid))
        return result

    def _enumerate_mobilize(self, state: Any) -> list[tuple[float, Action]]:
        """Ranked mobilize_units candidates for home / land / sea / river."""
        out: list[tuple[float, Action]] = []
        purchased = get_purchased_units(state, self.faction_id)
        if not purchased:
            return out

        capacity = get_mobilization_capacity(
            state, self.faction_id, self.td, self.cd, self.port_d, self.ud, self.fd
        )
        territories = list(capacity.get("territories", []))
        port_territories = capacity.get("port_territories", [])
        for pt in port_territories:
            tid = pt.get("territory_id")
            if not tid:
                continue
            # Port rows only contribute home slots (e.g. Corsair at Umbar). Generic land
            # mobilization needs a standing camp, not a bare port — so skip power-only ports.
            home_cap = pt.get("home_unit_capacity") or {}
            if not home_cap:
                continue
            row: dict = {"territory_id": tid, "power": 0, "home_unit_capacity": dict(home_cap)}
            territories.append(row)
        sea_zones = capacity.get("sea_zones", [])
        river_zones = capacity.get("river_zones", [])

        # Split purchased by kind
        land_stacks = [
            (p["unit_id"], p["count"])
            for p in purchased
            if not _is_naval_unit(self.ud.get(p["unit_id"]))
            and not _is_river_unit(self.ud.get(p["unit_id"]))
        ]
        naval_stacks = [
            (p["unit_id"], p["count"])
            for p in purchased
            if _is_naval_unit(self.ud.get(p["unit_id"]))
        ]
        river_stacks = [
            (p["unit_id"], p["count"])
            for p in purchased
            if _is_river_unit(self.ud.get(p["unit_id"]))
        ]

        # Home destinations first (unit matches home slot)
        for t_info in territories:
            tid = t_info.get("territory_id")
            if not tid:
                continue
            home_cap = t_info.get("home_unit_capacity") or {}
            if not home_cap:
                continue
            for unit_id, count in land_stacks:
                if count <= 0 or unit_id not in home_cap:
                    continue
                if not _can_mobilize_home_unit(state, tid, t_info, unit_id):
                    continue
                score = self._cheap_destination_score(state, tid)
                out.append(
                    (
                        score,
                        mobilize_units(self.faction_id, tid, [{"unit_id": unit_id, "count": 1}]),
                    )
                )

        # Land camp/port with remaining power
        for t_info in territories:
            tid = t_info.get("territory_id")
            if not tid:
                continue
            if t_info.get("home_unit_capacity") and t_info.get("power", 0) == 0:
                continue  # pure home handled above
            rem = _remaining_land_capacity(state, tid, t_info, self.ud)
            if rem <= 0 or not land_stacks:
                continue
            score = self._cheap_destination_score(state, tid)
            unit_stacks = []
            left = rem
            for unit_id, count in land_stacks:
                if left <= 0:
                    break
                take = min(count, left)
                if take <= 0:
                    continue
                unit_stacks.append({"unit_id": unit_id, "count": take})
                left -= take
            if unit_stacks:
                out.append(
                    (score, mobilize_units(self.faction_id, tid, unit_stacks))
                )

        # Sea zones (naval)
        for z_info in sea_zones:
            zid = z_info.get("sea_zone_id")
            if not zid or not naval_stacks:
                continue
            rem = _remaining_sea_capacity(state, self.faction_id, zid, z_info, self.td, self.port_d, self.ud)
            if rem <= 0:
                continue
            score = self._cheap_destination_score(state, zid)
            unit_stacks = []
            left = rem
            for unit_id, count in naval_stacks:
                if left <= 0:
                    break
                take = min(count, left)
                if take <= 0:
                    continue
                unit_stacks.append({"unit_id": unit_id, "count": take})
                left -= take
            if unit_stacks:
                out.append(
                    (score, mobilize_units(self.faction_id, zid, unit_stacks))
                )

        # River zones (river)
        for z_info in river_zones:
            zid = z_info.get("river_zone_id")
            if not zid or not river_stacks:
                continue
            pending_river: dict[str, int] = {}
            for pm in getattr(state, "pending_mobilizations", []) or []:
                dest = getattr(pm, "destination", "") or ""
                for item in getattr(pm, "units", None) or []:
                    if _is_river_unit(self.ud.get(item.get("unit_id"))):
                        pending_river[dest] = pending_river.get(dest, 0) + int(item.get("count", 0) or 0)
            rem = int(z_info.get("power", 0) or 0) - pending_river.get(zid, 0)
            if rem <= 0:
                continue
            score = self._cheap_destination_score(state, zid)
            unit_stacks = []
            left = rem
            for unit_id, count in river_stacks:
                if left <= 0:
                    break
                take = min(count, left)
                if take <= 0:
                    continue
                unit_stacks.append({"unit_id": unit_id, "count": take})
                left -= take
            if unit_stacks:
                out.append(
                    (score, mobilize_units(self.faction_id, zid, unit_stacks))
                )

        return out

    # ----- Cheap scoring (no sims) -----

    def _cheap_destination_score(self, state: Any, tid: str) -> float:
        """
        Formula-only rank for a destination. Stronghold/capital + power base,
        plus bonus for being closer to enemy units; minus spread penalty
        (already mobilizing here this phase). No defense sims.
        """
        tdef = self.td.get(tid)
        if not tdef:
            return 0.0
        score = territory_reinforce_base_score(tid, self.td, self.fd)

        # Closer to enemy = higher (distance in territory steps)
        dist = _distance_to_nearest_enemy_territory_with_units(
            tid, state, self.faction_id, self.fd, self.td, self.ud
        )
        if dist < 999:
            score += (6 - min(dist, 6)) * 5.0

        # Spread penalty: prefer not to pile everything into one zone
        already = sum(
            sum(u.get("count", 0) for u in pm.units)
            for pm in (getattr(state, "pending_mobilizations", []) or [])
            if getattr(pm, "destination", None) == tid
        )
        score -= already * _SPREAD_PENALTY
        return score

    def _cheap_action_score(self, state: Any, action: Action) -> float:
        """Cheap prior score for an action (destination-based)."""
        if action.type == "queue_camp_placement":
            tid = action.payload.get("territory_id", "")
            return max(0.05, self._cheap_destination_score(state, tid))
        if action.type == "mobilize_units":
            tid = action.payload.get("destination", "")
            return max(0.05, self._cheap_destination_score(state, tid))
        return 0.1

    # ----- End-phase legality -----

    def _can_end_phase(self, state: Any) -> bool:
        """
        True when mobilization may end: no unplaced camps with valid options and
        no purchased units still with a legal destination (mirrors reducer guard).
        Engine validate_action on end_phase is the final filter; this prunes early.
        """
        pending_camps = getattr(state, "pending_camps", []) or []
        queued_indices = {
            p.camp_index for p in (getattr(state, "pending_camp_placements", []) or [])
        }
        for camp_index, entry in enumerate(pending_camps):
            if entry.get("placed_territory_id"):
                continue
            if camp_index in queued_indices:
                continue
            if valid_camp_placement_territory_ids(
                state, self.faction_id, camp_index, self.cd, self.td
            ):
                return False
        # Units still deployable block end (engine mobilization_block_reason).
        purchased = get_purchased_units(state, self.faction_id)
        if purchased and faction_owns_capital(state, self.faction_id, self.fd):
            capacity = get_mobilization_capacity(
                state, self.faction_id, self.td, self.cd, self.port_d, self.ud, self.fd
            )
            territories = list(capacity.get("territories", []))
            for pt in capacity.get("port_territories", []):
                tid = pt.get("territory_id")
                if not tid:
                    continue
                home_cap = pt.get("home_unit_capacity") or {}
                if not home_cap:
                    continue
                territories.append(
                    {"territory_id": tid, "power": 0, "home_unit_capacity": dict(home_cap)}
                )
            for stack in purchased:
                uid = stack.get("unit_id")
                count = int(stack.get("count", 0) or 0)
                if count <= 0 or not uid:
                    continue
                udef = self.ud.get(uid)
                kind = _purchase_kind(udef)
                if kind == "naval":
                    for z in capacity.get("sea_zones", []):
                        if int(z.get("power", 0) or 0) > 0:
                            return False
                elif kind == "river":
                    for z in capacity.get("river_zones", []):
                        if int(z.get("power", 0) or 0) > 0:
                            return False
                else:
                    for t_info in territories:
                        tid = t_info.get("territory_id")
                        if not tid:
                            continue
                        home_cap = t_info.get("home_unit_capacity") or {}
                        if uid in home_cap and _can_mobilize_home_unit(state, tid, t_info, uid):
                            return False
                        rem = _remaining_land_capacity(state, tid, t_info, self.ud)
                        if rem > 0:
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


def decide_mobilization_mcts(ctx: AIContext, rng: random.Random | None = None) -> Action | None:
    """
    Entry point: run mobilization MCTS and return the chosen root action.
    Falls back to None if search yields nothing (caller keeps the heuristic path).
    """
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.search import run_mcts

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None
    if (ctx.state.phase or "mobilization") != "mobilization":
        return None

    problem = MobilizationProblem(ctx, rng=rng)
    budget = get_budget(os.environ.get(DEFAULT_DIFFICULTY_ENV))
    action = run_mcts(problem, ctx.state, budget)
    if action is None:
        return None
    if isinstance(action, HashableAction):
        return action.unwrap()
    return action
