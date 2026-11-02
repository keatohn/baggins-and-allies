"""
Live AI style knobs, read from the environment on each decision.

The admin Difficulty tab stores these and the API sets the env vars around
decide(). A sim can set them per faction for that faction's turn. Defaults:

    AI_RANDOMNESS        0    take the best decision
    AI_AGGRESSION        0.5  combat-move prize and casualties weigh as before
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
    """0 weighs our combat-move losses heavily. 1 weighs the territory. 0.5 is even."""
    return _unit("AI_AGGRESSION", 0.5)


def purchase_variety() -> float:
    """0 stacks the best unit. 1 evens the counts across every unit that can be bought."""
    return _unit("AI_PURCHASE_VARIETY", 0.0)


def scale_combat_gain(prize: float, defender_casualty: float, attacker_casualty: float) -> float:
    """Blend a battle's territory prize against casualties.

    At 0.5 this is prize + defender losses - attacker losses, the usual expected
    gain. Lower aggression shrinks the prize and charges more for our dead.
    Higher aggression does the opposite. A fight we will probably lose stays in
    the ranking when the trade is still worth it.
    """
    knob = aggression()
    prize_scale = knob / 0.5
    loss_scale = (1.0 - knob) / 0.5
    return prize_scale * prize + defender_casualty - loss_scale * attacker_casualty


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
