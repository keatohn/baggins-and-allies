"""
Live AI style knobs, read from the environment on each decision.

Each difficulty level stores its own values. The API sets the env vars around
decide() from the level that faction is using. Defaults:

    AI_RANDOMNESS        0    take the best decision
    AI_AGGRESSION        0.5  combat-move prize, kills, and our losses weigh evenly
    AI_PURCHASE_VARIETY  0    purchase keeps the small per-copy batch penalty

All three are continuous on [0, 1]. Nothing here is a pass/fail cutoff.
"""

from __future__ import annotations

import math
import os

from backend.ai.habits import PURCHASE_BATCH_DIVERSITY_PENALTY


def _unit(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value < 0.0 or value > 1.0:
        return default
    return value


def randomness() -> float:
    """0 always takes the best option. 1 picks any ranked option about equally."""
    return _unit("AI_RANDOMNESS", 0.0)


def aggression() -> float:
    """0 favors fewer attacks and our own survivors. 1 favors more attacks and enemy dead. 0.5 is even."""
    return _unit("AI_AGGRESSION", 0.5)


def _aggression_tilt() -> float:
    """-1 at aggression 0, 0 at 0.5, +1 at 1."""
    return (aggression() - 0.5) / 0.5


# How far prize, enemy kills, and our losses move from the midpoint. 0.5 means
# ±50%, so the slider never zeroes a term or doubles it.
_AGGRESSION_SWING = 0.5
# Power-points added (or removed) per battle already opened, at the extreme.
_ATTACK_SPREAD_STEP = 1.0


def purchase_variety() -> float:
    """0 stacks the best unit. 1 evens the counts across every unit that can be bought."""
    return _unit("AI_PURCHASE_VARIETY", 0.0)


def scale_combat_gain(prize: float, defender_casualty: float, attacker_casualty: float) -> float:
    """Blend a battle's territory prize against casualties.

    At 0.5 this is prize + enemy dead - our dead. Higher aggression raises the
    prize and the value of killing their units, and lowers the cost of ours.
    Each of those moves by at most half, so a reckless setting still counts our
    losses and a cautious one still counts the territory.
    """
    tilt = _aggression_tilt()
    prize_scale = 1.0 + _AGGRESSION_SWING * tilt
    kill_scale = 1.0 + _AGGRESSION_SWING * tilt
    loss_scale = 1.0 - _AGGRESSION_SWING * tilt
    return prize_scale * prize + kill_scale * defender_casualty - loss_scale * attacker_casualty


def attack_spread_adjustment(state, destination: str) -> float:
    """Score for opening another battle. 0 at aggression 0.5.

    Reinforcing a destination already in the combat-move queue is unchanged.
    A new destination gains a point per battle already open at full aggression,
    and loses that much at zero. The first new battle is one point at the extreme.
    """
    dest = str(destination or "")
    opened: set[str] = set()
    for pending in getattr(state, "pending_moves", None) or []:
        if getattr(pending, "phase", None) != "combat_move":
            continue
        to_tid = str(getattr(pending, "to_territory", "") or "")
        if to_tid:
            opened.add(to_tid)
    if dest and dest in opened:
        return 0.0
    return _aggression_tilt() * _ATTACK_SPREAD_STEP * (len(opened) + 1)


def purchase_repeat_penalty(score: float, already: int) -> float:
    """Small per-copy nudge that stays on even when purchase variety is 0. Score is unused."""
    del score
    if already <= 0:
        return 0.0
    return PURCHASE_BATCH_DIVERSITY_PENALTY * already


def population_stdev(counts: list[float]) -> float:
    """Population standard deviation of a count sample. One value has deviation 0."""
    n = len(counts)
    if n <= 1:
        return 0.0
    mean = sum(counts) / n
    return math.sqrt(sum((count - mean) ** 2 for count in counts) / n)


def roster_evenness(counts_by_id: dict[str, int], roster_ids: list[str]) -> float:
    """1 when the basket is spread evenly, down toward 0 when one type has every copy.

    The sample is one count per unit that can be purchased, including zeros.
    Purchase variety scales how much unevenness is penalized. At 0 this is 1.
    """
    variety = purchase_variety()
    ids = [uid for uid in roster_ids if uid]
    if variety <= 0 or len(ids) <= 1:
        return 1.0
    counts = [float(int(counts_by_id.get(uid, 0) or 0)) for uid in ids]
    total = sum(counts)
    if total <= 0:
        return 1.0
    stdev = population_stdev(counts)
    mean = total / len(ids)
    max_var = ((total - mean) ** 2 + (len(ids) - 1) * (mean ** 2)) / len(ids)
    max_stdev = math.sqrt(max_var) if max_var > 0 else 0.0
    if max_stdev <= 1e-9:
        return 1.0
    uneven = min(1.0, stdev / max_stdev)
    return 1.0 - variety * uneven


def blend_purchase_choice_scores(
    options: list[tuple],
    unit_id_of,
    counts_by_id: dict[str, int],
    roster_ids: list[str],
) -> list[tuple]:
    """Pull each option's score toward the buy that leaves the roster counts more even.

    `options` is `(choice, score)` with higher score better. At variety 0 the
    scores are unchanged. At variety 1 the ranking is the resulting standard
    deviation of counts across `roster_ids`.
    """
    variety = purchase_variety()
    ids = [uid for uid in roster_ids if uid]
    if variety <= 0 or len(options) <= 1 or len(ids) <= 1:
        return options

    def _stdev_after(unit_id: str) -> float:
        bumped = {uid: int(counts_by_id.get(uid, 0) or 0) for uid in ids}
        bumped[unit_id] = bumped.get(unit_id, 0) + 1
        return population_stdev([float(bumped[uid]) for uid in ids])

    scored = [(opt, float(score), _stdev_after(unit_id_of(opt))) for opt, score in options]
    s_lo = min(score for _opt, score, _stdev in scored)
    s_hi = max(score for _opt, score, _stdev in scored)
    d_lo = min(stdev for _opt, _score, stdev in scored)
    d_hi = max(stdev for _opt, _score, stdev in scored)
    span = max(s_hi - s_lo, 1.0)
    blended: list[tuple] = []
    for opt, score, stdev in scored:
        if d_hi <= d_lo + 1e-12:
            balance = score
        else:
            balance = s_hi - (stdev - d_lo) / (d_hi - d_lo) * span
        blended.append((opt, (1.0 - variety) * score + variety * balance))
    return blended
