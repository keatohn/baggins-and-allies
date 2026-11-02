"""
Purchase-phase MCTS Problem: legal-action enumerator + leaf evaluator.

Enumerates purchase bundles that spend the power still on hand, plus end_phase.
Mixes are drawn with probability proportional to 1/cost. A single copy of one
type is offered only when nothing else still fits.
"""

from __future__ import annotations

import os
import random
from typing import Any

from backend.engine.actions import Action, end_phase, purchase_units
from backend.engine.queries import (
    get_mobilization_capacity,
    get_purchasable_units,
    validate_action,
)
from backend.engine.queries import _is_naval_unit
from backend.engine.utils import faction_owns_capital

from backend.ai.context import AIContext
from backend.ai.formulas import (
    attack_value_per_power,
    defense_value_per_power,
    get_unit_power_cost,
)
from backend.ai.habits import MIN_POWER_RESERVE
from backend.ai.style import roster_evenness
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.eval_weights import get_eval_weights
from backend.ai.mcts.budget import BRANCH_LIMITS, branch_limit, resolve_game_difficulty
from backend.ai.mcts.search import Problem

# Default cap when no difficulty level is loaded. A level can raise this up to the admin ceiling.
MAX_BUNDLE_ACTIONS = BRANCH_LIMITS["purchase"][0]

def _use_mcts() -> bool:
    """True when the AI_ALGORITHM env flag selects MCTS. Default remains heuristic."""
    return (os.environ.get("AI_ALGORITHM") or "heuristic").strip().lower() == "mcts"


def _power_cost_from_row(row: dict) -> int:
    cost = row.get("cost", {}) or {}
    if isinstance(cost, dict):
        return int(cost.get("power", 0) or 0)
    return 0


class PurchaseProblem(Problem):
    """
    MCTS problem for the purchase phase.

    States are GameState snapshots. Actions are engine Action objects:
    - purchase_units(faction, bundle) to buy a candidate bundle
    - end_phase(faction) to finish purchasing (terminal for this problem)
    """

    def __init__(self, ctx: AIContext) -> None:
        self.ctx = ctx
        self.faction_id = ctx.faction_id
        self.ud = ctx.unit_defs
        self.td = ctx.territory_defs
        self.fd = ctx.faction_defs
        self.cd = ctx.camp_defs
        self.port_d = ctx.port_defs
        # Eval weights fixed for this search (env read once per decide() call).
        self._eval_weights = get_eval_weights()
        # Precompute purchasable rows once (stable across the search).
        self._purchasable = get_purchasable_units(
            ctx.state, self.faction_id, self.ud, self.fd
        ) if ctx.state else []

    # ----- Problem protocol -----

    def legal_actions(self, state: Any) -> list[HashableAction]:
        """end_phase plus candidate purchase bundles that fit budget and capacity."""
        actions: list[HashableAction] = [HashableAction(end_phase(self.faction_id))]

        if state.winner is not None:
            return actions
        if not faction_owns_capital(state, self.faction_id, self.fd):
            return actions

        bundles = self._enumerate_bundles(state)
        for bundle in bundles:
            action = purchase_units(self.faction_id, bundle)
            # Filter through engine validation so we never emit an illegal action.
            v = validate_action(state, action, self.ud, self.td, self.fd, self.cd, self.port_d)
            if v.valid:
                actions.append(HashableAction(action))

        return actions

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
        """Purchase episode ends when the phase advances (end_phase applied)."""
        if state.winner is not None:
            return True
        return (state.phase or "purchase") != "purchase"

    def evaluate(self, state: Any) -> float:
        """Non-terminal leaf: rough value of units bought so far, in [-1, 1]."""
        return self._score_purchased(state)

    def terminal_value(self, state: Any) -> float:
        """Terminal leaf: same scoring; winner state short-circuits to ±1."""
        if state.winner is not None:
            # winner is an alliance string; map to ±1 from our faction's side.
            our_alliance = getattr(self.fd.get(self.faction_id), "alliance", "") if self.fd.get(self.faction_id) else ""
            if state.winner == our_alliance:
                return 1.0
            return -1.0
        return self._score_purchased(state)

    def enumerator_size(self, state: Any) -> int:
        """Branching factor the enumerator yields for this state (3.1 hook)."""
        return len(self.legal_actions(state))

    def priors(self, state: Any) -> dict[HashableAction, float] | None:
        """
        Heuristic priors: end_phase gets a baseline weight; purchase bundles get
        weight proportional to their formula-based score (defense + offense blend).
        """
        actions = self.legal_actions(state)
        if not actions:
            return None
        priors: dict[HashableAction, float] = {}
        # end_phase baseline: modest so it is considered but not dominant.
        priors[HashableAction(end_phase(self.faction_id))] = 0.3
        for haction in actions:
            if haction.type != "purchase_units":
                continue
            bundle = haction.payload.get("purchases", {}) or {}
            score = self._bundle_prior_score(state, bundle)
            priors[haction] = max(0.05, score)
        return priors

    # ----- Bundle enumeration -----

    def _remaining_budget(self, state: Any) -> int:
        power = (state.faction_resources.get(self.faction_id, {}) or {}).get("power", 0)
        return max(0, int(power) - MIN_POWER_RESERVE)

    def _remaining_slots(self, state: Any) -> tuple[int, int]:
        """(land_remaining, sea_remaining) after accounting for already-purchased stacks."""
        capacity_info = get_mobilization_capacity(
            state, self.faction_id, self.td, self.cd, self.port_d, self.ud, self.fd
        )
        territories_list = capacity_info.get("territories", [])
        land_cap = sum(t.get("power", 0) for t in territories_list) + sum(
            1 for t in territories_list if t.get("home_unit_capacity")
        )
        sea_cap = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))
        fpu = (getattr(state, "faction_purchased_units", None) or {}).get(self.faction_id, [])
        already_land = sum(
            int(getattr(s, "count", 0) or 0)
            for s in fpu
            if not _is_naval_unit(self.ud.get(getattr(s, "unit_id", "")))
        )
        already_naval = sum(
            int(getattr(s, "count", 0) or 0)
            for s in fpu
            if _is_naval_unit(self.ud.get(getattr(s, "unit_id", "")))
        )
        return max(0, land_cap - already_land), max(0, sea_cap - already_naval)

    def _enumerate_bundles(self, state: Any) -> list[dict[str, int]]:
        """
        Bundles that spend this turn's power until nothing else fits.

        A stack of one type is offered only when buying more of it, or anything
        else, no longer fits. The rest of the list is mixes drawn one unit at a
        time with probability proportional to 1/cost, so cheaper units show up
        more often. Drawing stops when the leftover power cannot buy another unit.
        """
        budget = self._remaining_budget(state)
        if budget <= 0:
            return []
        land_rem, sea_rem = self._remaining_slots(state)
        if land_rem <= 0 and sea_rem <= 0:
            return []

        candidates = self._affordable_candidates(budget, land_rem, sea_rem)
        if not candidates:
            return []

        power = self._power_on_hand(state)
        cap = branch_limit("purchase", resolve_game_difficulty(state))
        seen: set[tuple] = set()
        unique: list[dict[str, int]] = []

        def add(bundle: dict[str, int]) -> None:
            key = tuple(sorted((uid, cnt) for uid, cnt in bundle.items() if cnt > 0))
            if not key or key in seen:
                return
            if self._bundle_power_cost(bundle) > power:
                return
            if _something_still_fits(candidates, bundle, budget, land_rem, sea_rem):
                return
            seen.add(key)
            unique.append({uid: cnt for uid, cnt in key})

        for candidate in candidates:
            kind_cap = sea_rem if candidate["is_naval"] else land_rem
            count = min(candidate["max_affordable"], kind_cap, budget // candidate["cost"])
            if count <= 0:
                continue
            add({candidate["unit_id"]: count})
            if len(unique) >= cap:
                return unique

        seed = (
            str(self.faction_id),
            int(getattr(state, "turn", 0) or 0),
            budget,
            land_rem,
            sea_rem,
            tuple(c["unit_id"] for c in candidates),
        )
        rng = random.Random(str(seed))
        # Stop once new mixes stop appearing. The limit scales with the option cap
        # and the roster, so a larger cap is allowed to search longer for new mixes.
        misses = 0
        miss_limit = cap + len(candidates)
        while len(unique) < cap and misses < miss_limit:
            before = len(unique)
            add(_fill_inverse_cost(candidates, budget, land_rem, sea_rem, rng))
            misses = 0 if len(unique) > before else misses + 1
        return unique

    def _affordable_candidates(
        self, budget: int, land_rem: int, sea_rem: int
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for row in self._purchasable:
            uid = row.get("unit_id")
            if not uid:
                continue
            cost = get_unit_power_cost(self.ud.get(uid)) or _power_cost_from_row(row)
            if cost <= 0 or cost > budget:
                continue
            max_aff = int(row.get("max_affordable", 0) or 0)
            if max_aff <= 0:
                continue
            is_naval = _is_naval_unit(self.ud.get(uid))
            kind_rem = sea_rem if is_naval else land_rem
            if kind_rem <= 0:
                continue
            candidates.append(
                {
                    "unit_id": uid,
                    "cost": int(cost),
                    "max_affordable": min(max_aff, kind_rem, budget // int(cost)),
                    "is_naval": is_naval,
                }
            )
        candidates.sort(key=lambda c: c["unit_id"])
        return candidates

    def _power_on_hand(self, state: Any) -> int:
        """Power this faction can still spend this purchase phase. Already-bought units have been deducted."""
        resources = (getattr(state, "faction_resources", None) or {}).get(self.faction_id, {}) or {}
        try:
            return max(0, int(resources.get("power", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _bundle_power_cost(self, bundle: dict[str, int]) -> int:
        total = 0
        for uid, count in bundle.items():
            cost = get_unit_power_cost(self.ud.get(uid))
            if not cost:
                continue
            total += int(cost) * int(count)
        return total

    def _score_purchased(self, state: Any) -> float:
        """
        Value of units purchased this turn (on-map purchased pool), blended defense/offense.
        Normalized roughly to [-1, 1] via a soft cap. Unspent power is not rewarded.
        """
        fpu = (getattr(state, "faction_purchased_units", None) or {}).get(self.faction_id, [])
        if not fpu:
            return 0.0
        w_att = self._eval_weights.purchase_attack_weight
        w_def = 1.0 - w_att
        raw = 0.0
        for stack in fpu:
            uid = getattr(stack, "unit_id", "")
            count = int(getattr(stack, "count", 0) or 0)
            if count <= 0 or not uid:
                continue
            udef = self.ud.get(uid)
            cost = get_unit_power_cost(udef)
            if cost <= 0:
                continue
            d = defense_value_per_power(udef, cost, 0)
            a = attack_value_per_power(udef, cost, 0)
            raw += count * (w_def * d + w_att * a)
        raw *= self._purchase_evenness(state, None)
        return raw / (1.0 + abs(raw))

    def _bundle_prior_score(self, state: Any, bundle: dict[str, int]) -> float:
        """Cheap formula-only score for a bundle (no sims) — used as MCTS prior weight."""
        total = 0.0
        w_att = self._eval_weights.purchase_attack_weight
        w_def = 1.0 - w_att
        for uid, count in bundle.items():
            if count <= 0:
                continue
            udef = self.ud.get(uid)
            cost = get_unit_power_cost(udef)
            if cost <= 0:
                continue
            d = defense_value_per_power(udef, cost, 0)
            a = attack_value_per_power(udef, cost, 0)
            total += count * (w_def * d + w_att * a)
        total *= self._purchase_evenness(state, bundle)
        return max(0.05, total)

    def _purchase_evenness(self, state: Any, bundle: dict[str, int] | None) -> float:
        """1 for an even basket. Falls as one purchasable type takes every copy."""
        counts: dict[str, int] = {}
        fpu = (getattr(state, "faction_purchased_units", None) or {}).get(self.faction_id, [])
        for stack in fpu:
            uid = getattr(stack, "unit_id", "")
            count = int(getattr(stack, "count", 0) or 0)
            if uid and count > 0:
                counts[uid] = counts.get(uid, 0) + count
        for uid, count in (bundle or {}).items():
            if uid and count > 0:
                counts[uid] = counts.get(uid, 0) + int(count)
        roster = [
            str(row.get("unit_id"))
            for row in self._purchasable
            if row.get("unit_id")
        ]
        return roster_evenness(counts, roster)


def _something_still_fits(
    candidates: list[dict[str, Any]],
    bundle: dict[str, int],
    budget: int,
    land_rem: int,
    sea_rem: int,
) -> bool:
    """True when leftover power and slots can still buy one more unit."""
    spent = 0
    land_used = 0
    sea_used = 0
    for candidate in candidates:
        count = int(bundle.get(candidate["unit_id"], 0) or 0)
        spent += count * candidate["cost"]
        if candidate["is_naval"]:
            sea_used += count
        else:
            land_used += count
    leftover = budget - spent
    for candidate in candidates:
        have = int(bundle.get(candidate["unit_id"], 0) or 0)
        if have >= candidate["max_affordable"] or candidate["cost"] > leftover:
            continue
        if candidate["is_naval"] and sea_used >= sea_rem:
            continue
        if not candidate["is_naval"] and land_used >= land_rem:
            continue
        return True
    return False


def _fill_inverse_cost(
    candidates: list[dict[str, Any]],
    budget: int,
    land_rem: int,
    sea_rem: int,
    rng: random.Random,
) -> dict[str, int]:
    """Add one unit at a time until nothing else fits. P(unit) is proportional to 1/cost."""
    bundle: dict[str, int] = {}
    spent = 0
    land_used = 0
    sea_used = 0
    while True:
        legal: list[dict[str, Any]] = []
        weights: list[float] = []
        for candidate in candidates:
            have = bundle.get(candidate["unit_id"], 0)
            if have >= candidate["max_affordable"] or candidate["cost"] > budget - spent:
                continue
            if candidate["is_naval"] and sea_used >= sea_rem:
                continue
            if not candidate["is_naval"] and land_used >= land_rem:
                continue
            legal.append(candidate)
            weights.append(1.0 / candidate["cost"])
        if not legal:
            return bundle
        picked = rng.choices(legal, weights=weights, k=1)[0]
        bundle[picked["unit_id"]] = bundle.get(picked["unit_id"], 0) + 1
        spent += picked["cost"]
        if picked["is_naval"]:
            sea_used += 1
        else:
            land_used += 1


def decide_purchase_mcts(ctx: AIContext) -> Action | None:
    """
    Entry point: run purchase-phase MCTS and return the chosen root action.
    Falls back to end_phase if search yields nothing (safety net).
    """
    from backend.ai.mcts.budget import get_phase_budget, resolve_game_difficulty
    from backend.ai.mcts.search import run_mcts_reuse

    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None

    problem = PurchaseProblem(ctx)
    budget = get_phase_budget("purchase", resolve_game_difficulty(ctx.state))
    # 3.4: resumes from the previous call's chosen subtree when this state was
    # already searched (purchase actions apply verbatim, so the next /ai-step
    # state matches the searched child exactly).
    action = run_mcts_reuse(problem, ctx.state, budget)
    if action is None:
        return end_phase(ctx.faction_id)
    # Unwrap hashable proxy back to a plain engine Action.
    if isinstance(action, HashableAction):
        return action.unwrap()
    return action
