"""Territory signal catalog, placement, and alliance visibility."""
from backend.api.main import state_for_response
from backend.engine.state import GameState
from backend.signals import (
    apply_territory_signal,
    classify_territory_for_signal,
    normalize_signal_presets,
    resolve_signal_alliance,
    signal_applies,
    visible_territory_signals,
)


def test_normalize_keeps_valid_presets_and_drops_bad_ones():
    presets = normalize_signal_presets(
        [
            {"id": "attack", "label": "Attack here", "icon": "⚔️", "applies_to": "enemy"},
            {"label": "  Watch this  ", "icon": "👁️", "applies_to": "any"},
            {"id": "nope", "label": "", "icon": "⚑", "applies_to": "neutral"},
            {"id": "bad", "label": "Hi", "icon": "<img>", "applies_to": "enemy"},
        ]
    )
    assert [p["id"] for p in presets] == ["attack", "watch_this"]
    assert presets[1]["label"] == "Watch this"
    assert presets[1]["applies_to"] == "any"
    assert presets[0]["color"] == "#b4332a"
    assert presets[1]["color"] == "#6b5b4b"


def test_classify_and_apply_rules():
    assert (
        classify_territory_for_signal(
            owner="mordor",
            territory_id="mordor",
            ownable=True,
            terrain_type="plains",
            my_alliance="good",
            owner_alliance="evil",
        )
        == "enemy"
    )
    assert (
        classify_territory_for_signal(
            owner="rohan",
            territory_id="rohan",
            ownable=True,
            terrain_type="plains",
            my_alliance="good",
            owner_alliance="good",
        )
        == "allied"
    )
    assert (
        classify_territory_for_signal(
            owner=None,
            territory_id="fangorn",
            ownable=True,
            terrain_type="forest",
            my_alliance="good",
            owner_alliance=None,
        )
        == "neutral"
    )
    assert (
        classify_territory_for_signal(
            owner=None,
            territory_id="sea_zone_1",
            ownable=False,
            terrain_type="sea",
            my_alliance="good",
            owner_alliance=None,
        )
        is None
    )
    attack = {"id": "attack", "label": "Attack here", "icon": "⚔️", "applies_to": "enemy"}
    assert signal_applies(attack, "enemy")
    assert not signal_applies(attack, "allied")
    assert not signal_applies(attack, None)
    assert signal_applies({**attack, "applies_to": "any"}, None)


def test_signal_alliance_follows_the_territory_owner():
    both = ["good", "evil"]
    assert resolve_signal_alliance(owner_alliance="good", my_alliances=both, current_alliance="evil") == "good"
    assert resolve_signal_alliance(owner_alliance="evil", my_alliances=both, current_alliance="good") == "evil"
    assert resolve_signal_alliance(owner_alliance=None, my_alliances=both, current_alliance="evil") == "evil"
    assert resolve_signal_alliance(owner_alliance="evil", my_alliances=["good"], current_alliance="good") == "good"


def test_place_toggle_and_visibility():
    state = GameState.from_dict(
        {
            "turn_number": 1,
            "current_faction": "gondor",
            "phase": "purchase",
            "territories": {},
            "faction_resources": {},
        }
    )
    attack = {"id": "attack", "label": "Attack here", "icon": "⚔️", "applies_to": "enemy"}
    assert apply_territory_signal(
        state, alliance="good", territory_id="mordor", faction_id="gondor", preset=attack
    ) == "set"
    assert apply_territory_signal(
        state, alliance="evil", territory_id="gondor", faction_id="mordor", preset=attack
    ) == "set"
    assert apply_territory_signal(
        state, alliance="good", territory_id="mordor", faction_id="gondor", preset=attack
    ) == "cleared"

    reinforce = {
        "id": "reinforce",
        "label": "Reinforce here",
        "icon": "🛡️",
        "applies_to": "allied",
        "color": "#2c6e9a",
    }
    apply_territory_signal(
        state, alliance="good", territory_id="minas_tirith", faction_id="gondor", preset=reinforce
    )
    visible = visible_territory_signals(state, {"good"})
    assert set(visible) == {"minas_tirith"}
    assert visible["minas_tirith"]["label"] == "Reinforce here"
    assert visible["minas_tirith"]["color"] == "#2c6e9a"
    assert "gondor" not in visible
    evil = visible_territory_signals(state, {"evil"})
    assert set(evil) == {"gondor"}

    again = GameState.from_dict(state.to_dict())
    assert again.territory_signals["evil"]["gondor"]["faction_id"] == "mordor"
    public = state_for_response(state)
    assert "territory_signals" not in public
