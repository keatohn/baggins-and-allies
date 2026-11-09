"""active_combat pending-round flags must reach the state payload.

The combat modal reads combat_siegeworks_pending / combat_archer_prefire_pending
from state.active_combat (App.tsx), but only available_actions.active_combat ever
carried them — so both pending chips were permanently false. Both payloads now go
through _enrich_active_combat_ui_fields and must agree.
"""

import pytest

from backend.api.main import _build_available_actions, game_defs, state_for_response
from backend.engine.actions import initiate_combat
from backend.engine.combat_specials import pending_combat_round
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import ActiveCombat, TerritoryState, Unit
from backend.engine.utils import initialize_game_state

SETUP_ID = "wotr_exp_1.0"
GAME_ID = "__pending_flags_test__"
STRONGHOLD = "minas_tirith"
FLAGS = (
    "combat_siegeworks_pending",
    "combat_archer_prefire_pending",
    "combat_siegeworks_dice",
)


@pytest.fixture(scope="module")
def defs():
    ud, td, fd, cd, port_d = load_static_definitions(setup_id=SETUP_ID)
    setup = load_starting_setup(setup_id=SETUP_ID)
    return ud, td, fd, cd, port_d, setup


@pytest.fixture()
def actions_payload():
    """Call _build_available_actions with registered defs, then clean up game_defs."""
    registered = []

    def _call(state, ud, td, fd, cd, port_d):
        game_defs[GAME_ID] = (ud, td, fd, cd, port_d)
        registered.append(GAME_ID)
        return _build_available_actions(state, GAME_ID, None)

    yield _call
    for gid in registered:
        game_defs.pop(gid, None)


def _unit(unit_id: str, iid: str, ud) -> Unit:
    d = ud[unit_id]
    return Unit(
        instance_id=iid,
        unit_id=unit_id,
        remaining_movement=0,
        remaining_health=d.health,
        base_movement=0,
        base_health=d.health,
        loaded_onto=None,
    )


def _open_battle(defs, attacker_ids, defender_ids, *, craft_log=None):
    """Build a state with mordor attacking a gondor stronghold, then initiate_combat."""
    ud, td, fd, cd, port_d, setup = defs
    state = initialize_game_state(
        faction_defs=fd, territory_defs=td, unit_defs=ud,
        starting_setup=setup, camp_defs=cd,
    )
    attackers = [_unit(uid, f"att_{uid}", ud) for uid in attacker_ids]
    defenders = [_unit(uid, f"def_{uid}", ud) for uid in defender_ids]
    state.territories[STRONGHOLD] = TerritoryState(
        owner="gondor", original_owner="gondor", units=attackers + defenders,
    )
    state.territories[STRONGHOLD].stronghold_current_health = td[STRONGHOLD].stronghold_base_health
    state.phase = "combat"
    state.current_faction = "mordor"

    if craft_log is None:
        state, _ = apply_action(
            state,
            initiate_combat("mordor", STRONGHOLD, {
                "attacker": [10] * 40,
                "defender": [10] * 40,
            }),
            ud, td, fd, cd, port_d,
        )
        return state

    state.active_combat = ActiveCombat(
        attacker_faction="mordor",
        territory_id=STRONGHOLD,
        attacker_instance_ids=[u.instance_id for u in attackers],
        round_number=0,
        combat_log=craft_log,
        attackers_have_rolled=False,
        initial_attacker_instance_ids=[u.instance_id for u in attackers],
        initial_defender_instance_ids=[u.instance_id for u in defenders],
        fuse_bomb=True,
    )
    return state


def _both_payloads(state, defs, actions_payload):
    ud, td, fd, cd, port_d, _ = defs
    from_state = state_for_response(state).get("active_combat") or {}
    from_actions = (actions_payload(state, ud, td, fd, cd, port_d) or {}).get("active_combat") or {}
    return from_state, from_actions


def test_state_payload_reports_archer_prefire_pending(defs, actions_payload):
    """After a siegeworks round, defender archers are still owed a prefire round."""
    state = _open_battle(defs, ["catapult", "morgul_orc"], ["gondor_soldier", "galadhrim_archer"])
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0

    from_state, from_actions = _both_payloads(state, defs, actions_payload)

    assert from_state["combat_archer_prefire_pending"] is True
    assert from_actions["combat_archer_prefire_pending"] is True


def test_state_payload_reports_siegeworks_pending(defs, actions_payload):
    """Round 0 with no dedicated round resolved yet and siege dice to be thrown."""
    state = _open_battle(
        defs,
        ["catapult", "morgul_orc"],
        ["gondor_soldier"],
        craft_log=[],
    )
    ud, td, fd, cd, port_d, _ = defs
    expected = pending_combat_round(
        [], 0,
        [u for u in state.territories[STRONGHOLD].units if u.unit_id in ("catapult", "morgul_orc")],
        [u for u in state.territories[STRONGHOLD].units if u.unit_id == "gondor_soldier"],
        ud,
        defender_territory_is_stronghold=True,
        defender_stronghold_hp=td[STRONGHOLD].stronghold_base_health,
        fuse_bomb=True,
    )
    assert expected.siegeworks is True

    from_state, from_actions = _both_payloads(state, defs, actions_payload)

    assert from_state["combat_siegeworks_pending"] is True
    assert from_actions["combat_siegeworks_pending"] is True


def test_state_and_available_actions_payloads_agree(defs, actions_payload):
    """Both payloads are built from one helper, so they can never drift apart."""
    battles = [
        ("siege+archer", ["catapult", "morgul_orc"], ["gondor_soldier", "galadhrim_archer"]),
        ("archer only", ["morgul_orc"], ["gondor_soldier", "galadhrim_archer"]),
        ("siege only", ["catapult", "morgul_orc"], ["gondor_soldier"]),
        ("plain round 1", ["morgul_orc"], ["gondor_soldier"]),
    ]
    for name, attackers, defenders in battles:
        state = _open_battle(defs, attackers, defenders)
        from_state, from_actions = _both_payloads(state, defs, actions_payload)
        for key in FLAGS:
            assert key in from_state, f"{name}: {key} missing from state payload"
            assert key in from_actions, f"{name}: {key} missing from actions payload"
            assert from_state[key] == from_actions[key], (
                f"{name}: {key} drifted state={from_state[key]!r} actions={from_actions[key]!r}"
            )


def test_pending_flags_are_false_once_round_one_ran(defs, actions_payload):
    """No dedicated round is pending after the first standard round resolved."""
    state = _open_battle(defs, ["morgul_orc"], ["gondor_soldier"])
    assert state.active_combat is not None
    assert state.active_combat.round_number == 1

    from_state, from_actions = _both_payloads(state, defs, actions_payload)

    for source, payload in (("state", from_state), ("actions", from_actions)):
        assert payload["combat_siegeworks_pending"] is False, source
        assert payload["combat_archer_prefire_pending"] is False, source
        assert payload["combat_siegeworks_dice"] == {"attacker": 0, "defender": 0}, source
