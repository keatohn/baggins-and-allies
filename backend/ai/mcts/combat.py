"""
Combat-phase MCTS Problem: legal-action enumerator + leaf evaluator.

Covers the combat phase decision points:
- No active combat: initiate each contested battle, or end_phase when legal.
- Active combat: continue (with sampled dice), or retreat to a valid destination.

Heuristics from the existing greedy policy (combat sim net value, retreat scoring)
are reused as evaluators/priors, not as sole deciders. Multi-step composition lets
MCTS resolve battles in sequence and then end the phase when no contested remain.
"""

from __future__ import annotations

import os
import random
from typing import Any

from backend.engine.actions import Action, continue_combat, end_phase, initiate_combat, retreat
from backend.engine.queries import (
    get_contested_territories,
    get_retreat_options,
    validate_action,
)

from backend.ai.context import AIContext
from backend.ai.formulas import battle_gain_if_win, get_unit_power_cost
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.dice import sample_dice_for_continue, sample_dice_for_initiate
from backend.ai.mcts.search import Problem

# Cap on branching factor per node. Combat space is small but retreats + multi-battles
# can still grow; keep MCTS search tractable.
MAX_COMBAT_BRANCHES = 32

DEFAULT_DIFFICULTY_ENV = "AI_MCTS_DIFFICULTY"


class CombatProblem(Problem):
    """
    MCTS problem for the combat phase.

    States are GameState snapshots. Actions are engine Action objects:
    - initiate_combat(faction, territory_id, empty dice) for a contested battle
    - continue_combat(faction, empty dice) while active_combat exists
    - retreat(faction, retreat_to) while active_combat exists
    - end_phase(faction) when no active combat and no contested remain (terminal)

    Dice for initiate/continue are sampled internally during `apply` so search can
    branch through the reducer. The action returned to the caller still has empty
    dice (the API fills real dice for live play), matching the heuristic AI.
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
        """
        End of phase:
        - active_combat: continue_combat + each valid retreat destination
        - else: each contested initiate_combat + end_phase when legal
        """
        if state.winner is not None:
            return []
        if (state.phase or "combat") != "combat":
            return []

        actions: list[HashableAction] = []

        if state.active_combat:
            # Continue (dice filled in apply; caller leaves empty for API).
            actions.append(HashableAction(continue_combat(self.faction_id, dice_rolls={"attacker": [], "defender": []})))
            # Retreat destinations.
            destinations = get_retreat_options(state, self.td, self.fd, self.ud)
            for dest in destinations:
                action = retreat(self.faction_id, dest)
                v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
                if v.valid:
                    actions.append(HashableAction(action))
            return actions[:MAX_COMBAT_BRANCHES]

        # No active combat: initiate contested battles.
        contested = get_contested_territories(state, self.faction_id, self.fd, self.ud, self.td)
        for entry in contested:
            tid = entry.get("territory_id")
            if not tid:
                continue
            sea_zone_id = entry.get("sea_zone_id")
            action = initiate_combat(
                self.faction_id,
                tid,
                {"attacker": [], "defender": []},
                sea_zone_id=sea_zone_id,
            )
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(action))

        # end_phase always offered; validation filters it when battles remain.
        end_action = end_phase(self.faction_id)
        v = validate_action(state, end_action, self.ud, self.td, self.fd, self.cd, self.port_d)
        if v.valid:
            actions.append(HashableAction(end_action))

        return actions[:MAX_COMBAT_BRANCHES]

    def apply(self, state: Any, action: HashableAction) -> Any:
        """
        Apply via the reducer. Sample dice for initiate/continue so combat progresses.
        apply_action deep-copies, so `state` is never mutated.
        """
        from backend.engine.reducer import apply_action

        raw = action.unwrap() if isinstance(action, HashableAction) else action
        raw = self._with_sampled_dice(state, raw)
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
        """Episode ends when the phase advances past combat (end_phase applied) or game over."""
        if state.winner is not None:
            return True
        return (state.phase or "combat") != "combat"

    def evaluate(self, state: Any) -> float:
        """Non-terminal leaf: rough board value from our faction's perspective, in [-1, 1]."""
        return self._score_board(state)

    def terminal_value(self, state: Any) -> float:
        """Terminal leaf: winner short-circuits to ±1; otherwise board score."""
        if state.winner is not None:
            our_alliance = ""
            fd_row = self.fd.get(self.faction_id)
            if fd_row:
                our_alliance = getattr(fd_row, "alliance", "") or ""
            if state.winner == our_alliance:
                return 1.0
            return -1.0
        return self._score_board(state)

    def priors(self, state: Any) -> dict[HashableAction, float] | None:
        """
        Heuristic priors:
        - initiate_combat: weight by battle_gain_if_win (objective value).
        - continue_combat: modest baseline (sims refine at leaves).
        - retreat: lower baseline (usually worse than fighting a winning battle).
        - end_phase: modest so it is considered when no battles remain.
        """
        actions = self.legal_actions(state)
        if not actions:
            return None
        priors: dict[HashableAction, float] = {}
        for haction in actions:
            if haction.type == "initiate_combat":
                tid = haction.payload.get("territory_id", "")
                gain = battle_gain_if_win(tid, self.td, self.fd)
                priors[haction] = max(0.05, gain)
            elif haction.type == "continue_combat":
                priors[haction] = 0.6
            elif haction.type == "retreat":
                priors[haction] = 0.25
            elif haction.type == "end_phase":
                priors[haction] = 0.35
            else:
                priors[haction] = 0.1
        return priors

    # ----- Dice injection for search -----

    def _with_sampled_dice(self, state: Any, action: Action) -> Action:
        """Return a copy of action with dice sampled when type is initiate/continue."""
        if action.type == "initiate_combat":
            dice = sample_dice_for_initiate(
                state,
                action.payload.get("territory_id", ""),
                action.payload.get("sea_zone_id"),
                self.faction_id,
                self.ud,
                self.td,
                self.fd,
                rng=self.rng,
            )
            # Mutate a shallow copy of the payload so HashableAction key stays stable
            # for the returned-to-caller action; search only uses the returned Action.
            new_payload = dict(action.payload)
            new_payload["dice_rolls"] = dice
            return Action(type=action.type, faction=action.faction, payload=new_payload)
        if action.type == "continue_combat":
            dice = sample_dice_for_continue(state, self.ud, self.td, self.fd, rng=self.rng)
            new_payload = dict(action.payload)
            new_payload["dice_rolls"] = dice
            return Action(type=action.type, faction=action.faction, payload=new_payload)
        return action

    # ----- Scoring -----

    def _score_board(self, state: Any) -> float:
        """
        Soft material + territory value from our faction's perspective.
        Normalized roughly to [-1, 1] via a tanh-like squash.
        """
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
                owner_alliance = getattr(self.fd.get(owner), "alliance", None) if self.fd.get(owner) else None
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
                # Subfactions: treat controlled under our faction via alliance when possible.
                u_alliance = getattr(self.fd.get(u_faction), "alliance", None) if u_faction and self.fd.get(u_faction) else None
                if u_faction == self.faction_id or u_alliance == our_alliance:
                    # Don't count enemy-alliance units that somehow share our faction id edge cases.
                    if u_alliance is not None and u_alliance != our_alliance:
                        enemy_power += cost
                    else:
                        our_power += cost
                elif u_faction is not None:
                    enemy_power += cost

        raw = (our_power + our_terr) - (enemy_power + enemy_terr)
        return raw / (1.0 + abs(raw))


def decide_combat_mcts(ctx: AIContext, rng: random.Random | None = None) -> Action | None:
    """
    Entry point: run combat-phase MCTS and return the chosen root action.
    Dice are left empty for initiate/continue so the API can fill real rolls.
    Falls back to end_phase if search yields nothing (safety net).
    """
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.search import run_mcts

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None
    if (ctx.state.phase or "combat") != "combat":
        return None

    problem = CombatProblem(ctx, rng=rng)
    budget = get_budget(os.environ.get(DEFAULT_DIFFICULTY_ENV))
    action = run_mcts(problem, ctx.state, budget)
    if action is None:
        return end_phase(ctx.faction_id)
    if isinstance(action, HashableAction):
        return _strip_search_dice(action.unwrap())
    return _strip_search_dice(action)


def _strip_search_dice(action: Action) -> Action:
    """Clear dice from initiate/continue payloads so the API generates real rolls."""
    if action.type in ("initiate_combat", "continue_combat"):
        new_payload = dict(action.payload)
        new_payload["dice_rolls"] = {"attacker": [], "defender": []}
        # Terror fields are search-only artifacts; never emit them to the API.
        new_payload.pop("terror_applied", None)
        new_payload.pop("terror_final_defender_hits", None)
        new_payload.pop("terror_reroll_count", None)
        return Action(type=action.type, faction=action.faction, payload=new_payload)
    return action
