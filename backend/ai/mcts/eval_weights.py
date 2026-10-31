"""
Evaluation weights for MCTS leaf scoring (MCTS 3.6).

Centralizes the material/territory blend used by the shared board scorer (the
four move/combat Problems previously carried four identical private copies)
and the purchase defense/offense blend, so self-play sweeps can vary them via
env without code edits. Defaults reproduce the pre-3.6 hardcoded values
bit-for-bit; the heuristic AI does not read these knobs.

Env knobs (missing, unparseable, or out-of-range values fall back to the
default, so a sweep typo can never crash a game or invert the score):

    AI_MCTS_W_MATERIAL          unit-power weight in the board score   [1.0]
    AI_MCTS_W_TERRITORY         territory weight in the board score    [1.0]
    AI_MCTS_W_STRONGHOLD        flat value per stronghold              [3.0]
    AI_MCTS_W_POWER_PROD        value per power a territory produces   [0.5]
    AI_MCTS_W_BOARD_SCALE       scale applied after soft-normalization [1.0]
    AI_MCTS_W_PURCHASE_DEFENSE  purchase defense blend in [0, 1]       [0.5]

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

    material_weight: float = 1.0
    territory_weight: float = 1.0
    stronghold_value: float = 3.0
    power_produce_scale: float = 0.5
    board_scale: float = 1.0
    purchase_defense_weight: float = PURCHASE_DEFENSE_WEIGHT_DEFAULT


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


def get_eval_weights() -> EvalWeights:
    """Resolve the current eval weights from env (per-call, never cached)."""
    return EvalWeights(
        material_weight=_env_float("AI_MCTS_W_MATERIAL", 1.0, 0.0, math.inf),
        territory_weight=_env_float("AI_MCTS_W_TERRITORY", 1.0, 0.0, math.inf),
        stronghold_value=_env_float("AI_MCTS_W_STRONGHOLD", 3.0, 0.0, math.inf),
        power_produce_scale=_env_float("AI_MCTS_W_POWER_PROD", 0.5, 0.0, math.inf),
        board_scale=_env_float("AI_MCTS_W_BOARD_SCALE", 1.0, 0.0, math.inf),
        purchase_defense_weight=_env_float(
            "AI_MCTS_W_PURCHASE_DEFENSE", PURCHASE_DEFENSE_WEIGHT_DEFAULT, 0.0, 1.0
        ),
    )


def board_score(
    state,
    faction_id: str,
    td,
    fd,
    ud,
    weights: EvalWeights | None = None,
) -> float:
    """
    Soft material + territory value from faction_id's perspective, in [-1, 1].

    Shared by the combat / combat_move / non_combat_move / mobilization
    Problems. With default weights this is bit-identical to the pre-3.6
    private `_score_board` implementations: material and territory sums are
    formed the same way, then weighted and soft-normalized.
    """
    w = weights if weights is not None else get_eval_weights()
    our_power = 0.0
    enemy_power = 0.0
    our_terr = 0.0
    enemy_terr = 0.0
    our_alliance = ""
    fd_row = fd.get(faction_id)
    if fd_row:
        our_alliance = getattr(fd_row, "alliance", "") or ""

    for tid, terr in (state.territories or {}).items():
        owner = getattr(terr, "owner", None)
        value = 0.0
        tdef = td.get(tid)
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
            our_terr += value
        elif owner_is_enemy:
            enemy_terr += value

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

    # Multiplying by 1.0 is exact, so default weights keep the legacy
    # (our + our_terr) - (enemy + enemy_terr) grouping bit-identical.
    our_total = w.material_weight * our_power + w.territory_weight * our_terr
    enemy_total = w.material_weight * enemy_power + w.territory_weight * enemy_terr
    raw = our_total - enemy_total
    return raw / (1.0 + abs(raw)) * w.board_scale
