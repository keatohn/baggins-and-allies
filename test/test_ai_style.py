"""Admin style knobs: randomness, combat-move aggression, purchase variety."""

from backend.ai.randomness import pick_from_score_band, softmax_pick
from backend.ai.settings import default_ai_settings, normalize_faction_ai, normalize_style
from backend.ai.style import (
    blend_purchase_choice_scores,
    population_stdev,
    purchase_repeat_penalty,
    roster_evenness,
    scale_combat_gain,
)


def test_defaults_match_the_old_combat_and_purchase_behavior(monkeypatch):
    monkeypatch.delenv("AI_AGGRESSION", raising=False)
    monkeypatch.delenv("AI_PURCHASE_VARIETY", raising=False)
    monkeypatch.delenv("AI_RANDOMNESS", raising=False)
    assert scale_combat_gain(5.0, 2.0, 1.0) == 5.0 + 2.0 - 1.0
    assert purchase_repeat_penalty(4.0, 1) == 0.2
    assert roster_evenness({"a": 3}, ["a", "b", "c"]) == 1.0
    settings = default_ai_settings()
    assert settings["randomness"] == 0
    assert settings["aggression"] == 0.5
    assert settings["purchase_variety"] == 0
    assert pick_from_score_band([("best", 10.0), ("second", 6.0), ("third", 2.0)]) == "best"


def test_aggression_slides_prize_against_losses(monkeypatch):
    monkeypatch.setenv("AI_AGGRESSION", "0")
    cautious = scale_combat_gain(5.0, 2.0, 1.0)
    assert cautious == 2.0 - 2.0 * 1.0
    monkeypatch.setenv("AI_AGGRESSION", "1")
    reckless = scale_combat_gain(5.0, 2.0, 1.0)
    assert reckless == 2.0 * 5.0 + 2.0
    monkeypatch.setenv("AI_PURCHASE_VARIETY", "1")
    stacked = roster_evenness({"a": 3}, ["a", "b", "c"])
    even = roster_evenness({"a": 1, "b": 1, "c": 1}, ["a", "b", "c"])
    assert stacked < even
    assert even == 1.0
    assert population_stdev([3, 0, 0]) > population_stdev([1, 1, 1])
    blended = blend_purchase_choice_scores(
        [(("soldier", 3), 10.0), (("archer", 4), 9.0), (("cavalry", 5), 8.0)],
        lambda choice: choice[0],
        {"soldier": 3},
        ["soldier", "archer", "cavalry"],
    )
    winner = max(blended, key=lambda item: item[1])[0][0]
    assert winner != "soldier"


def test_randomness_can_pick_the_second_and_third_best(monkeypatch):
    options = [("best", 10.0), ("second", 6.0), ("third", 2.0), ("last", 0.1)]
    monkeypatch.setenv("AI_RANDOMNESS", "1")
    seen = {pick_from_score_band(options) for _ in range(60)}
    assert seen == {"best", "second", "third", "last"}
    monkeypatch.setenv("AI_RANDOMNESS", "0")
    assert softmax_pick(options, 0) == "best"


def test_style_values_are_clamped_and_faction_inherits_defaults():
    assert normalize_style({"randomness": 4, "aggression": -1, "purchase_variety": "nope"}) == {
        "randomness": 0.0,
        "aggression": 0.5,
        "purchase_variety": 0.0,
    }
    faction = normalize_faction_ai(
        {"algorithm": "heuristic"},
        style_fallback={"randomness": 0.4, "aggression": 0.2, "purchase_variety": 0.8},
    )
    assert faction["algorithm"] == "heuristic"
    assert faction["randomness"] == 0.4
    assert faction["aggression"] == 0.2
    assert faction["purchase_variety"] == 0.8
    overridden = normalize_faction_ai(
        {"algorithm": "mcts", "randomness": 0.9},
        style_fallback={"randomness": 0.4, "aggression": 0.2, "purchase_variety": 0.8},
    )
    assert overridden["randomness"] == 0.9
    assert overridden["aggression"] == 0.2
