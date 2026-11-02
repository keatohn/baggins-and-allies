"""
Ladder-only attacks are blocked at end of combat move (not non-combat move).

A ladder rolls no dice and cannot conquer alone, so a ladder moved into a territory
during combat move needs at least one other unit type in the pending attack on that
same territory. Declaration stays allowed; ending the phase is gated (API can_end_phase
flag + validate/apply end_phase), mirroring the naval must-attack obligations.

Run: .venv/bin/pytest test/test_ladder_only_attack.py
"""

from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.actions import move_units, end_phase
from backend.engine.movement import get_reachable_territories_for_unit
from backend.engine.queries import (
    validate_action,
    get_ladder_only_attack_destinations,
    get_ladder_only_attack_instance_ids,
)
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state

SETUP = "wotr_1.1"
LADDER_TERRITORY = "isengard"
LADDER_ID = "isengard_siege_ladder_017"
# Reachable from isengard in combat_move (freepeoples-owned, ent-occupied).
ATTACK_DEST = "fangorn"


def _fresh_state(phase: str = "combat_move"):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id=SETUP)
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    state.current_faction = "isengard"
    state.phase = phase
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _ladder(state):
    return next(
        u for u in state.territories[LADDER_TERRITORY].units
        if u.instance_id == LADDER_ID
    )


def _first_non_ladder(state, territory_id, exclude=()):
    return next(
        u for u in state.territories[territory_id].units
        if u.unit_id != "siege_ladder" and u.instance_id not in exclude
    )


def test_ladder_only_declaration_allowed_but_phase_end_blocked():
    """Dragging a ladder into an attack alone is allowed; ending combat move is not."""
    state, ud, td, fd, cd, pd = _fresh_state()

    action = move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [LADDER_ID])
    v = validate_action(state, action, ud, td, fd, cd, pd)
    assert v.valid, f"ladder-only declaration must stay allowed: {v.error}"

    state, _ = apply_action(state, action, ud, td, fd, cd, pd)
    assert get_ladder_only_attack_instance_ids(state, ud) == [LADDER_ID]
    assert get_ladder_only_attack_destinations(state, ud) == {ATTACK_DEST: [LADDER_ID]}

    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert not ve.valid
    assert "cannot attack alone" in (ve.error or "")

    # apply_action enforces the same rule even if validation were skipped
    try:
        apply_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    except ValueError as e:
        assert "cannot attack alone" in str(e)
    else:
        raise AssertionError("apply end_phase must raise for a ladder-only attack")


def test_ladder_accompanied_by_separate_pending_move_can_end_phase():
    """Another unit type moving to the same destination in its own pending move unblocks the phase."""
    state, ud, td, fd, cd, pd = _fresh_state()

    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [LADDER_ID]),
        ud, td, fd, cd, pd,
    )
    assert get_ladder_only_attack_instance_ids(state, ud) == [LADDER_ID]

    partner = _first_non_ladder(state, LADDER_TERRITORY, exclude=[LADDER_ID])
    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [partner.instance_id]),
        ud, td, fd, cd, pd,
    )
    assert get_ladder_only_attack_instance_ids(state, ud) == []

    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert ve.valid, ve.error
    state, _ = apply_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert state.phase != "combat_move"


def test_ladder_accompanied_in_same_pending_move():
    """A single pending move carrying a ladder plus another unit type passes the rule."""
    state, ud, td, fd, cd, pd = _fresh_state()

    ladder = _ladder(state)
    partner = _first_non_ladder(state, LADDER_TERRITORY, exclude=[LADDER_ID])
    ladder.remaining_movement = 1
    partner.remaining_movement = 1

    state, _ = apply_action(
        state,
        move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [LADDER_ID, partner.instance_id]),
        ud, td, fd, cd, pd,
    )
    assert get_ladder_only_attack_instance_ids(state, ud) == []
    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert ve.valid, ve.error


def test_rule_ignored_in_non_combat_move():
    """The rule is combat-move only: a lone ladder reposition in non-combat move ends fine."""
    state, ud, td, fd, cd, pd = _fresh_state(phase="non_combat_move")

    ladder = _ladder(state)
    ladder.remaining_movement = 1
    reachable, _ = get_reachable_territories_for_unit(
        ladder, LADDER_TERRITORY, state, ud, td, fd, "non_combat_move",
    )
    dest = next(d for d in reachable if d != LADDER_TERRITORY)

    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, dest, [LADDER_ID]),
        ud, td, fd, cd, pd,
    )
    assert get_ladder_only_attack_instance_ids(state, ud) == []

    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert ve.valid, ve.error
    state, _ = apply_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert state.phase == "mobilization"


def _ai_context(state, ud, td, fd, cd, pd, can_end_phase: bool):
    from backend.ai.context import AIContext

    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=pd,
        available_actions={
            "can_end_phase": can_end_phase,
            "loaded_naval_must_attack_instance_ids": [],
        },
    )


def test_ai_resolves_ladder_only_attack_with_partner_move():
    """With a ladder-only attack pending, the AI adds a non-ladder partner instead of stalling."""
    from backend.ai.combat_move import decide_combat_move

    state, ud, td, fd, cd, pd = _fresh_state()
    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [LADDER_ID]),
        ud, td, fd, cd, pd,
    )

    action = decide_combat_move(_ai_context(state, ud, td, fd, cd, pd, can_end_phase=False))
    assert action is not None
    assert action.type == "move_units"
    assert action.payload["to"] == ATTACK_DEST
    partner_ids = action.payload["unit_instance_ids"]
    assert partner_ids, "partner move must carry units"
    assert LADDER_ID not in partner_ids
    assert all(uid != LADDER_ID for uid in partner_ids)

    # Applying the partner move unblocks the phase.
    state, _ = apply_action(state, action, ud, td, fd, cd, pd)
    assert get_ladder_only_attack_instance_ids(state, ud) == []
    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert ve.valid, ve.error


def test_ai_cancels_ladder_only_attack_when_no_partner_available():
    """If no other unit can reach the attacked territory, the AI cancels the ladder move."""
    from backend.ai.combat_move import decide_combat_move

    state, ud, td, fd, cd, pd = _fresh_state()
    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [LADDER_ID]),
        ud, td, fd, cd, pd,
    )
    # Nobody else can move anymore: no partner candidate exists.
    for terr in state.territories.values():
        for u in terr.units:
            if u.instance_id != LADDER_ID:
                u.remaining_movement = 0

    action = decide_combat_move(_ai_context(state, ud, td, fd, cd, pd, can_end_phase=False))
    assert action is not None
    assert action.type == "cancel_move"
    assert action.payload["move_index"] == 0

    state, _ = apply_action(state, action, ud, td, fd, cd, pd)
    assert get_ladder_only_attack_instance_ids(state, ud) == []
    ve = validate_action(state, end_phase("isengard"), ud, td, fd, cd, pd)
    assert ve.valid, ve.error


def test_drop_unaccompanied_ladder_moves_prevention():
    """AI never declares a ladder-only candidate move unless a partner exists for the destination."""
    from backend.ai.combat_move import drop_unaccompanied_ladder_moves

    state, ud, td, fd, cd, pd = _fresh_state()
    uruk = _first_non_ladder(state, LADDER_TERRITORY, exclude=[LADDER_ID])

    # 1) Ladder-only candidate into a destination with no partner anywhere: dropped.
    moves = {(LADDER_TERRITORY, ATTACK_DEST): [LADDER_ID]}
    routes = {(LADDER_TERRITORY, ATTACK_DEST): {LADDER_ID: [[LADDER_TERRITORY]]}}
    drop_unaccompanied_ladder_moves(state, ud, moves, routes)
    assert moves == {}
    assert routes == {}

    # 2) Mixed candidate (ladder + other unit type) to the same destination: kept.
    moves = {(LADDER_TERRITORY, ATTACK_DEST): [LADDER_ID, uruk.instance_id]}
    routes = {}
    drop_unaccompanied_ladder_moves(state, ud, moves, routes)
    assert (LADDER_TERRITORY, ATTACK_DEST) in moves

    # 3) Ladder-only candidate whose destination already receives a pending non-ladder move: kept.
    state, _ = apply_action(
        state, move_units("isengard", LADDER_TERRITORY, ATTACK_DEST, [uruk.instance_id]),
        ud, td, fd, cd, pd,
    )
    moves = {(LADDER_TERRITORY, ATTACK_DEST): [LADDER_ID]}
    drop_unaccompanied_ladder_moves(state, ud, moves, routes)
    assert (LADDER_TERRITORY, ATTACK_DEST) in moves

    # 4) Fresh state: ladder-only candidate kept when another candidate move carries a
    #    non-ladder unit to the same destination.
    state, ud, td, fd, cd, pd = _fresh_state()
    uruk = _first_non_ladder(state, LADDER_TERRITORY, exclude=[LADDER_ID])
    moves = {
        (LADDER_TERRITORY, ATTACK_DEST): [LADDER_ID],
        ("south_dunland", ATTACK_DEST): [uruk.instance_id],
    }
    routes = {}
    drop_unaccompanied_ladder_moves(state, ud, moves, routes)
    assert (LADDER_TERRITORY, ATTACK_DEST) in moves
