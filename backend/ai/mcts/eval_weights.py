"""
Evaluation weights for MCTS leaf scoring (MCTS 3.6).

Centralizes the units, stronghold, capital, and power weights used by the shared board scorer (the
four move/combat Problems previously carried four identical private copies)
and the purchase defense/offense blend, so self-play sweeps can vary them via
env without code edits. Defaults reproduce the pre-3.6 hardcoded values
bit-for-bit; the heuristic AI does not read these knobs.

Env knobs (missing, unparseable, or out-of-range values fall back to the
default, so a sweep typo can never crash a game or invert the score):

    AI_MCTS_W_UNITS             unit-power weight in the board score   [1.0]
    AI_MCTS_W_STRONGHOLD        flat value per stronghold              [3.0]
    AI_MCTS_W_CAPITAL           flat value per capital held            [3.0]
    AI_MCTS_W_POWER_PROD        value per power a territory produces   [0.5]
    AI_MCTS_W_BOARD_SCALE       scale applied after soft-normalization [1.0]
    AI_MCTS_W_PURCHASE_ATTACK   purchase attack blend in [0, 1]        [0.5]
                                0 is all defense, 1 is all attack.
                                AI_MCTS_W_PURCHASE_DEFENSE is the old knob
                                and is inverted when the new one is unset.

Weights are captured once per Problem (search time) so one search always
evaluates leaves under a single weight set; env is read at Problem
construction, which is per decide() call, so a per-turn env swap (self-play
matchups) takes effect on the next turn.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from backend.ai.formulas import get_unit_power_cost
from backend.ai.habits import PURCHASE_DEFENSE_WEIGHT_DEFAULT


@dataclass(frozen=True)
class EvalWeights:
    """One immutable set of MCTS evaluation weights."""

    units_weight: float = 1.0
    stronghold_value: float = 3.0
    capital_value: float = 3.0
    power_produce_scale: float = 0.5
    board_scale: float = 1.0
    purchase_attack_weight: float = PURCHASE_DEFENSE_WEIGHT_DEFAULT


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """Read a float from env; any failure or out-of-range value returns default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < lo or value > hi:
        return default
    return value


def _purchase_attack_weight() -> float:
    """0 buys for defense, 1 buys for attack. The old defense env is inverted."""
    new_raw = os.environ.get("AI_MCTS_W_PURCHASE_ATTACK")
    if new_raw is not None and new_raw.strip():
        return _env_float("AI_MCTS_W_PURCHASE_ATTACK", PURCHASE_DEFENSE_WEIGHT_DEFAULT, 0.0, 1.0)
    old_raw = os.environ.get("AI_MCTS_W_PURCHASE_DEFENSE")
    if old_raw is not None and old_raw.strip():
        old = _env_float("AI_MCTS_W_PURCHASE_DEFENSE", PURCHASE_DEFENSE_WEIGHT_DEFAULT, 0.0, 1.0)
        return 1.0 - old
    return PURCHASE_DEFENSE_WEIGHT_DEFAULT


def _units_weight() -> float:
    """Unit-power weight. AI_MCTS_W_MATERIAL is the old name."""
    raw = os.environ.get("AI_MCTS_W_UNITS")
    if raw is not None and raw.strip():
        return _env_float("AI_MCTS_W_UNITS", 1.0, 0.0, math.inf)
    old = os.environ.get("AI_MCTS_W_MATERIAL")
    if old is not None and old.strip():
        return _env_float("AI_MCTS_W_MATERIAL", 1.0, 0.0, math.inf)
    return 1.0


def get_eval_weights() -> EvalWeights:
    """Resolve the current eval weights from env (per-call, never cached)."""
    return EvalWeights(
        units_weight=_units_weight(),
        stronghold_value=_env_float("AI_MCTS_W_STRONGHOLD", 3.0, 0.0, math.inf),
        capital_value=_env_float("AI_MCTS_W_CAPITAL", 3.0, 0.0, math.inf),
        power_produce_scale=_env_float("AI_MCTS_W_POWER_PROD", 0.5, 0.0, math.inf),
        board_scale=_env_float("AI_MCTS_W_BOARD_SCALE", 1.0, 0.0, math.inf),
        purchase_attack_weight=_purchase_attack_weight(),
    )


def _capital_ids(fd) -> set[str]:
    found: set[str] = set()
    for fdef in (fd or {}).values():
        cap = getattr(fdef, "capital", None)
        if isinstance(cap, str) and cap:
            found.add(cap)
    return found


def board_score(
    state,
    faction_id: str,
    td,
    fd,
    ud,
    weights: EvalWeights | None = None,
) -> float:
    """
    Units, strongholds, capitals, and power from faction_id's perspective.

    Shared by the combat / combat_move / non_combat_move / mobilization
    Problems. A capital adds its own flat value on top of stronghold and power.
    The result is squashed toward [-scale, scale].
    """
    w = weights if weights is not None else get_eval_weights()
    our_power = 0.0
    enemy_power = 0.0
    our_land = 0.0
    enemy_land = 0.0
    our_alliance = ""
    capitals = _capital_ids(fd)
    fd_row = fd.get(faction_id)
    if fd_row:
        our_alliance = getattr(fd_row, "alliance", "") or ""

    for tid, terr in (state.territories or {}).items():
        owner = getattr(terr, "owner", None)
        value = 0.0
        tdef = td.get(tid)
        if tid in capitals:
            value += w.capital_value
        if tdef:
            if getattr(tdef, "is_stronghold", False):
                value += w.stronghold_value
            produces = getattr(tdef, "produces", None)
            if isinstance(produces, dict):
                value += float(produces.get("power", 0) or 0) * w.power_produce_scale
        owner_is_ours = owner == faction_id
        owner_is_enemy = False
        if owner is not None and not owner_is_ours:
            owner_alliance = getattr(fd.get(owner), "alliance", None) if fd.get(owner) else None
            owner_is_enemy = owner_alliance != our_alliance
        if owner_is_ours:
            our_land += value
        elif owner_is_enemy:
            enemy_land += value

        for unit in getattr(terr, "units", []) or []:
            udef = ud.get(getattr(unit, "unit_id", ""))
            cost = get_unit_power_cost(udef)
            if cost <= 0:
                continue
            u_faction = getattr(udef, "faction", None)
            u_alliance = (
                getattr(fd.get(u_faction), "alliance", None)
                if u_faction and fd.get(u_faction)
                else None
            )
            if u_faction == faction_id or u_alliance == our_alliance:
                if u_alliance is not None and u_alliance != our_alliance:
                    enemy_power += cost
                else:
                    our_power += cost
            elif u_faction is not None:
                enemy_power += cost

    our_total = w.units_weight * our_power + our_land
    enemy_total = w.units_weight * enemy_power + enemy_land
    raw = our_total - enemy_total
    return raw / (1.0 + abs(raw)) * w.board_scale
