"""Allied liberation: capturing faction matching original owner's alliance restores original owner at end of combat phase."""
import pytest
from backend.engine.actions import end_phase
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.utils import (
    backfill_liberation_metadata,
    effective_original_owner,
    initialize_game_state,
)


@pytest.fixture
def defs():
    ud, td, fd, cd, _port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    start = load_starting_setup(setup_id="wotr_exp_1.0")
    return ud, td, fd, cd, start


def test_effective_original_owner_falls_back_to_snapshot(defs):
    ud, td, fd, cd, start = defs
    state = initialize_game_state(fd, td, ud, starting_setup=start, camp_defs=cd)
    pel = state.territories.get("pelennor")
    assert pel is not None
    assert pel.original_owner == "gondor"
    pel.original_owner = None
    assert effective_original_owner("pelennor", pel, state) == "gondor"


def test_backfill_restores_missing_original_owner(defs):
    ud, td, fd, cd, start = defs
    state = initialize_game_state(fd, td, ud, starting_setup=start, camp_defs=cd)
    pel = state.territories["pelennor"]
    pel.original_owner = None
    state.starting_territory_owners = {}
    backfill_liberation_metadata(state, start)
    assert pel.original_owner == "gondor"
    assert state.starting_territory_owners.get("pelennor") == "gondor"


def test_end_combat_phase_liberates_to_original_allied_owner(defs):
    """Rohan captures Gondor-origin territory from Mordor → ownership goes to Gondor (same alliance)."""
    ud, td, fd, cd, start = defs
    state = initialize_game_state(fd, td, ud, starting_setup=start, camp_defs=cd)
    state.phase = "combat"
    state.current_faction = "rohan"
    pel = state.territories["pelennor"]
    assert pel.original_owner == "gondor"
    pel.owner = "mordor"
    state.pending_captures["pelennor"] = "rohan"

    state, _ = apply_action(state, end_phase("rohan"), ud, td, fd, cd, None)

    assert state.territories["pelennor"].owner == "gondor"
    assert state.pending_captures == {}


def test_conquest_without_alliance_goes_to_capturer(defs):
    ud, td, fd, cd, start = defs
    state = initialize_game_state(fd, td, ud, starting_setup=start, camp_defs=cd)
    state.phase = "combat"
    state.current_faction = "mordor"
    pel = state.territories["pelennor"]
    pel.owner = "rohan"
    state.pending_captures["pelennor"] = "mordor"

    state, _ = apply_action(state, end_phase("mordor"), ud, td, fd, cd, None)

    assert state.territories["pelennor"].owner == "mordor"
