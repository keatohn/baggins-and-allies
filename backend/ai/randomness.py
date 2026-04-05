"""
AI choice randomness: when several options are nearly tied, pick with slight favor to the best.
"""

import random

from backend.ai.habits import AI_SCORE_BAND_TOLERANCE


def pick_from_score_band(options_with_scores, tolerance=None):
    """
    Given (option, score) pairs with higher score = better, return one option.
    If multiple options are within `tolerance` (default 5%) of the best score,
    pick randomly among them with probability proportional to score (higher = more likely).
    Otherwise return the single best.

    Keeps decision-making reasonable while adding a bit of variety when options are close.
    """
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

    # Weight proportional to score (shift so min in band is 0, so all have non-negative weight)
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
