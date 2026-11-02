"""Stored AI difficulty/algorithm, and heuristic mobilization staying inside camp-or-home."""

import pytest

from backend.ai.context import AIContext
from backend.ai.decide import _mcts_enabled
from backend.ai.mcts.budget import resolve_game_difficulty
from backend.ai.mobilization import decide_mobilization
from backend.ai.settings import normalize_algorithm, normalize_difficulty
from backend.engine.queries import validate_action
from backend.engine.state import GameState, UnitStack


def test_ai_choice_roundtrips_and_old_saves_default():
    state, *_rest = _gondor_board()
    state.ai_difficulty = "hard"
    state.ai_algorithm = "mcts"
    restored = GameState.from_dict(state.to_dict())
    assert restored.ai_difficulty == "hard"
    assert restored.ai_algorithm == "mcts"
    legacy = state.to_dict()
    legacy.pop("ai_difficulty")
    legacy.pop("ai_algorithm")
    old = GameState.from_dict(legacy)
    assert old.ai_difficulty == "normal"
    assert old.ai_algorithm == "heuristic"


def test_env_difficulty_overrides_the_game(monkeypatch):
    state, *_rest = _gondor_board()
    state.ai_difficulty = "hard"
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    assert resolve_game_difficulty(state) == "easy"
    monkeypatch.delenv("AI_MCTS_DIFFICULTY", raising=False)
    assert resolve_game_difficulty(state) == "hard"


def test_stored_algorithm_is_used_when_env_is_unset(monkeypatch):
    state, *_rest = _gondor_board()
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state.ai_algorithm = "mcts"
    assert _mcts_enabled(state) is True
    monkeypatch.setenv("AI_ALGORITHM", "heuristic")
    assert _mcts_enabled(state) is False


def test_catalog_caps_replace_the_move_phase_override():
    from backend.ai.mcts.budget import SearchBudget, budget_catalog_scope, get_phase_budget

    levels = [
        {"id": "easy", "name": "Casual", "iterations": 40, "seconds": 0.1},
        {"id": "nightmare", "name": "Nightmare", "iterations": 3000, "seconds": 3},
    ]
    with budget_catalog_scope(levels):
        assert get_phase_budget("combat_move", "nightmare") == SearchBudget(3000, 3.0)
        assert get_phase_budget("purchase", "missing") == SearchBudget(40, 0.1)
    assert get_phase_budget("combat_move", "normal").max_iterations == 200


def test_normalize_ai_knobs_rejects_unknown_names():
    assert normalize_difficulty("hard") == "hard"
    assert normalize_difficulty("nightmare") == "normal"
    assert normalize_algorithm("mcts") == "mcts"
    assert normalize_algorithm("deep") == "heuristic"


def test_heuristic_will_not_mobilize_land_onto_a_port(monkeypatch):
    """A port's power is a naval pool. Land units, including home units, must not be dumped there."""
    monkeypatch.delenv("AI_ALGORITHM", raising=False)
    state, ud, td, fd, cd, port_d = _gondor_board()
    state.phase = "mobilization"
    state.current_faction = "gondor"
    state.camps_standing = []
    state.mobilization_camps = []
    state.faction_purchased_units["gondor"] = [
        UnitStack(unit_id="citadel_guard", count=1),
        UnitStack(unit_id="gondor_soldier", count=2),
    ]
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={"can_end_phase": True, "phase": "mobilization", "faction": "gondor"},
    )
    port_territories = {getattr(port, "territory_id", None) for port in port_d.values()}
    for _ in range(6):
        action = decide_mobilization(ctx)
        assert action is not None
        verdict = validate_action(state, action, ud, td, fd, cd, port_d)
        assert verdict.valid, verdict.error
        if action.type != "mobilize_units":
            break
        dest = action.payload.get("destination")
        unit_ids = {row.get("unit_id") for row in (action.payload.get("units") or [])}
        if dest in port_territories:
            for unit_id in unit_ids:
                home = set(getattr(ud.get(unit_id), "home_territory_ids", None) or [])
                specials = set(getattr(ud.get(unit_id), "specials", None) or [])
                assert "home" in specials and dest in home
        state, _events = _apply(state, action, ud, td, fd, cd, port_d)
        ctx = AIContext(
            state=state,
            unit_defs=ud,
            territory_defs=td,
            faction_defs=fd,
            camp_defs=cd,
            port_defs=port_d,
            available_actions={"can_end_phase": True, "phase": "mobilization", "faction": "gondor"},
        )


def _apply(state, action, ud, td, fd, cd, port_d):
    from backend.engine.reducer import apply_action

    return apply_action(state, action, ud, td, fd, cd, port_d)


def _gondor_board():
    from backend.engine.definitions import load_starting_setup, load_static_definitions
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    if "citadel_guard" not in ud or "gondor" not in fd:
        pytest.skip("wotr_exp_1.0 has no gondor citadel guard")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=load_starting_setup(setup_id="wotr_exp_1.0"),
        camp_defs=cd,
    )
    return state, ud, td, fd, cd, port_d
