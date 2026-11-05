"""Shadow of war: one-step alliance sight, with no cavalry exception, widened by foresight."""

from types import SimpleNamespace

from backend.engine.shadow import (
    apply_shadow_view,
    choose_viewer_alliance,
    filter_charge_routes,
    filter_events_for_sight,
    visible_territory_ids,
)
from backend.engine.state import GameState, TerritoryState, Unit


def _terr(owner: str | None) -> TerritoryState:
    return TerritoryState(owner=owner, original_owner=owner)


def _tdef(adjacent: list[str], ford: list[str] | None = None, aerial: list[str] | None = None):
    return SimpleNamespace(
        adjacent=adjacent,
        ford_adjacent=ford or [],
        aerial_adjacent=aerial or [],
    )


def _board():
    state = GameState(
        turn_number=1,
        current_faction="gondor",
        phase="combat_move",
        territories={
            "home": _terr("gondor"),
            "near": _terr("mordor"),
            "far": _terr("mordor"),
            "air": _terr("mordor"),
            "sea": _terr(None),
            "ford": _terr("mordor"),
        },
        faction_resources={},
        shadow_of_war=True,
    )
    territory_defs = {
        "home": _tdef(["near", "sea"], ford=["ford"], aerial=["air"]),
        "near": _tdef(["home", "far"]),
        "far": _tdef(["near"]),
        "air": _tdef([]),
        "sea": _tdef(["home"]),
        "ford": _tdef([]),
    }
    faction_defs = {
        "gondor": SimpleNamespace(alliance="good", parent=None),
        "mordor": SimpleNamespace(alliance="evil", parent=None),
    }
    return state, territory_defs, faction_defs


def test_visible_ring_is_one_ground_step():
    state, territory_defs, faction_defs = _board()
    visible = visible_territory_ids(state, territory_defs, "good", faction_defs)
    assert visible == {"home", "near", "sea", "ford"}


def _chain_board():
    """a-b-c-d-e-f chain. Good owns only a; a foresight unit on b reaches e, not f."""
    state = GameState(
        turn_number=1,
        current_faction="gondor",
        phase="combat_move",
        territories={
            "a": _terr("gondor"),
            "b": _terr("mordor"),
            "c": _terr("mordor"),
            "d": _terr("mordor"),
            "e": _terr("mordor"),
            "f": _terr("mordor"),
        },
        faction_resources={},
        shadow_of_war=True,
    )
    territory_defs = {
        "a": _tdef(["b"]),
        "b": _tdef(["a", "c"]),
        "c": _tdef(["b", "d"]),
        "d": _tdef(["c", "e"]),
        "e": _tdef(["d", "f"]),
        "f": _tdef(["e"]),
    }
    faction_defs = {
        "gondor": SimpleNamespace(alliance="good", parent=None),
        "mordor": SimpleNamespace(alliance="evil", parent=None),
    }
    unit_defs = {
        "gondor_scout": SimpleNamespace(faction="gondor", specials=["foresight"], tags=[]),
        "mordor_scout": SimpleNamespace(faction="mordor", specials=["foresight"], tags=[]),
        "gondor_infantry": SimpleNamespace(faction="gondor", specials=[], tags=[]),
    }
    return state, territory_defs, faction_defs, unit_defs


def _place(state: GameState, tid: str, unit_id: str) -> None:
    state.territories[tid].units.append(
        Unit(
            instance_id=f"{unit_id}_1",
            unit_id=unit_id,
            remaining_movement=2,
            remaining_health=1,
            base_movement=2,
            base_health=1,
        )
    )


def test_foresight_lifts_shadow_three_hops_and_no_further():
    state, territory_defs, faction_defs, unit_defs = _chain_board()
    assert visible_territory_ids(state, territory_defs, "good", faction_defs, unit_defs) == {"a", "b"}
    _place(state, "b", "gondor_scout")
    visible = visible_territory_ids(state, territory_defs, "good", faction_defs, unit_defs)
    assert visible == {"a", "b", "c", "d", "e"}
    assert "f" not in visible


def test_foresight_only_helps_the_viewers_alliance():
    state, territory_defs, faction_defs, unit_defs = _chain_board()
    _place(state, "b", "mordor_scout")
    visible = visible_territory_ids(state, territory_defs, "good", faction_defs, unit_defs)
    assert visible == {"a", "b"}


def test_foresight_unit_without_the_special_does_not_widen_the_ring():
    state, territory_defs, faction_defs, unit_defs = _chain_board()
    _place(state, "b", "gondor_infantry")
    visible = visible_territory_ids(state, territory_defs, "good", faction_defs, unit_defs)
    assert visible == {"a", "b"}


def test_apply_shadow_view_redacts_only_past_the_foresight_ring():
    state, territory_defs, faction_defs, unit_defs = _chain_board()
    _place(state, "b", "gondor_scout")
    out = {
        "territories": {
            tid: {"owner": terr.owner, "units": [{"instance_id": tid}]}
            for tid, terr in state.territories.items()
        },
    }
    apply_shadow_view(out, state, territory_defs, faction_defs, "good", unit_defs)
    assert out["shadowed_territories"] == ["f"]
    assert out["territories"]["e"]["units"]
    assert out["territories"]["f"]["units"] == []


def test_foresight_gate_is_closed_outside_shadow_of_war():
    from backend.api.main import shadow_viewer_alliance

    state = GameState(
        turn_number=1,
        current_faction="gondor",
        phase="purchase",
        territories={},
        faction_resources={},
    )
    assert state.shadow_of_war is False
    assert shadow_viewer_alliance("missing-game", object(), None, state) is None


def test_redaction_clears_units_and_casualty_order():
    state, territory_defs, faction_defs = _board()
    out = {
        "territories": {
            tid: {"owner": terr.owner, "units": [{"instance_id": tid}]}
            for tid, terr in state.territories.items()
        },
        "territory_defender_casualty_order": {"near": "best_defense", "far": "best_defense"},
        "pending_moves": [
            {"from_territory": "home", "to_territory": "far"},
            {"from_territory": "far", "to_territory": "air"},
        ],
    }
    apply_shadow_view(out, state, territory_defs, faction_defs, "good")
    assert out["territories"]["near"]["units"]
    assert out["territories"]["far"]["units"] == []
    assert out["territories"]["air"]["units"] == []
    assert out["territory_defender_casualty_order"] == {"near": "best_defense"}
    assert out["shadowed_territories"] == ["far", "air"] or set(out["shadowed_territories"]) == {"far", "air"}
    assert out["pending_moves"] == [{"from_territory": "home", "to_territory": "far"}]


def test_mode_off_is_the_default_on_old_saves():
    raw = GameState(
        turn_number=1,
        current_faction="gondor",
        phase="purchase",
        territories={},
        faction_resources={},
    ).to_dict()
    assert raw["shadow_of_war"] is False
    raw.pop("shadow_of_war")
    loaded = GameState.from_dict(raw)
    assert loaded.shadow_of_war is False


def test_viewer_alliance_follows_turn_only_when_the_player_holds_both():
    assert choose_viewer_alliance({"good"}, "evil") == "good"
    assert choose_viewer_alliance({"good", "evil"}, "evil") == "evil"
    assert choose_viewer_alliance(set(), "good") is None


def test_event_log_hides_moves_outside_sight_and_keeps_purchases():
    visible = {"home", "near"}
    events = [
        {"type": "units_purchased", "payload": {"faction": "gondor"}},
        {"type": "units_moved", "payload": {"from_territory": "home", "to_territory": "near"}},
        {"type": "units_moved", "payload": {"from_territory": "near", "to_territory": "far"}},
        {"type": "combat_started", "payload": {"territory": "far"}},
    ]
    kept = filter_events_for_sight(events, visible)
    assert [event["type"] for event in kept] == ["units_purchased", "units_moved"]


def test_charge_routes_do_not_name_a_hidden_through_tile():
    actions = {
        "moveable_units": [
            {
                "charge_routes": {
                    "far": [["near"], ["hidden_gap"]],
                    "past": [["far"]],
                }
            }
        ]
    }
    filter_charge_routes(actions, {"near", "home"})
    routes = actions["moveable_units"][0]["charge_routes"]
    assert routes == {"far": [["near"]]}
