"""
AI choice randomness.

Randomness 0 keeps the old near-tie band: only options within 5% of the best
are shared, and the best still wins. Above 0, every option is in a softmax.
The temperature rises with the knob across the whole list. Low values stay
near the best. High values flatten every rank, so a much worse option is chosen
almost as often as the best.
"""

import math
import random

from backend.ai.habits import AI_SCORE_BAND_TOLERANCE


def softmax_pick(options_with_scores, temperature: float, rng=None):
    """Pick one option. temperature 0 is the best score. 1 flattens the field."""
    if not options_with_scores:
        return None
    choose = rng.random if rng is not None else random.random
    best_opt = None
    best_score = None
    scored: list[tuple] = []
    for opt, score in options_with_scores:
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        scored.append((opt, value))
        if best_score is None or value > best_score:
            best_score = value
            best_opt = opt
    if not scored or best_score is None:
        return None
    if temperature <= 0 or len(scored) == 1:
        return best_opt
    if temperature >= 1:
        return scored[int(choose() * len(scored)) % len(scored)][0]
    worst = min(value for _opt, value in scored)
    span = best_score - worst
    if span <= 1e-9:
        return scored[int(choose() * len(scored)) % len(scored)][0]
    # knob 0.5 → temperature equal to the score gap. knob near 1 → almost flat.
    tau = span * temperature / (1.0 - temperature)
    weights = [math.exp((value - best_score) / tau) for _opt, value in scored]
    total = sum(weights)
    pick = choose() * total
    for (opt, _value), weight in zip(scored, weights):
        pick -= weight
        if pick <= 0:
            return opt
    return scored[-1][0]


def pick_from_score_band(options_with_scores, tolerance=None):
    """
    Given (option, score) pairs with higher score = better, return one option.
    If multiple options are within `tolerance` (default 5%) of the best score,
    pick randomly among them with probability proportional to score (higher = more likely).
    Otherwise return the single best.

    Keeps decision-making reasonable while adding a bit of variety when options are close.
    """
    from backend.ai.style import randomness

    knob = randomness()
    if tolerance is None and knob > 0:
        return softmax_pick(options_with_scores, knob)
    if tolerance is None:
        tolerance = AI_SCORE_BAND_TOLERANCE
    if not options_with_scores:
        return None
    normalized: list[tuple] = []
    for opt, s in options_with_scores:
        try:
            normalized.append((opt, float(s)))
        except (TypeError, ValueError):
            continue
    if not normalized:
        return None
    options_with_scores = normalized
    best_score = max(s for _, s in options_with_scores)
    if best_score is None:
        return options_with_scores[0][0]
    try:
        if best_score == float("-inf"):
            return options_with_scores[0][0]
    except TypeError:
        pass

    # "Within tolerance of best": for positive best, threshold = best * (1 - tolerance)
    # For negative best (e.g. least-bad loss), threshold = best * (1 + tolerance)
    if best_score > 0:
        threshold = best_score * (1.0 - tolerance)
    else:
        threshold = best_score * (1.0 + tolerance)

    band = [(opt, s) for opt, s in options_with_scores if s >= threshold]
    if not band:
        band = options_with_scores
    if len(band) == 1:
        return band[0][0]

    opts, scores = zip(*band)
    min_s = min(scores)
    weights = [max(0.0, float(s - min_s)) + 1e-9 for s in scores]
    total = sum(weights)
    r = random.random() * total
    for opt, w in zip(opts, weights):
        r -= w
        if r <= 0:
            return opt
    return opts[-1]
