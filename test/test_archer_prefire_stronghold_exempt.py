"""
M4: archer prefire -1 defense penalty does not apply when defending a stronghold
or fortress (catalog archer special: "-1 defense penalty except when defending a
stronghold or fortress"; docs/COMBAT_SIMULATION_SCOPE.md: "defense−1, or +0 if
stronghold/fortress").

Run: .venv/bin/pytest test/test_archer_prefire_stronghold_exempt.py
"""

from types import SimpleNamespace

from backend.engine.actions import initiate_combat
from backend.engine.combat import archer_prefire_penalty_delta, resolve_archer_prefire
from backend.engine.combat_sim import SimOptions, run_one_battle
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"  # manifest prefire_penalty: true
TERRITORY_PLAIN = "gap_of_rohan"  # plains, not stronghold: penalty applies
TERRITORY_STRONGHOLD = "rivendell"  # is_stronghold, forest terrain: exempt
TERRITORY_FORTRESS = "gundabad"  # fortress terrain, not stronghold: exempt


def _unit(iid: str, unit_id: str):
    return SimpleNamespace(
        instance_id=iid, unit_id=unit_id, health=1, remaining_health=1, remaining_movement=0,
    )


def _defs():
    return {
        "arch": SimpleNamespace(
            attack=1, defense=3, dice=1, health=1, cost={"power": 2},
            archetype="archer", specials=["archer"],
        ),
        "three": SimpleNamespace(
            attack=3, defense=3, dice=1, health=1, cost={"power": 1},
            archetype="infantry", specials=[],
        ),
    }


def _terr(is_stronghold: bool, terrain_type: str):
    return SimpleNamespace(is_stronghold=is_stronghold, terrain_type=terrain_type)


def test_helper_exempt_stronghold_and_fortress():
    assert archer_prefire_penalty_delta(-1, _terr(True, "forest")) == 0
    assert archer_prefire_penalty_delta(-1, _terr(False, "fortress")) == 0
    assert archer_prefire_penalty_delta(-1, _terr(False, "plains")) == -1
    assert archer_prefire_penalty_delta(-1, None) == -1
    # Manifest penalty already off: still 0 everywhere.
    assert archer_prefire_penalty_delta(0, _terr(True, "city")) == 0
    assert archer_prefire_penalty_delta(0, _terr(False, "plains")) == 0


def test_resolver_skips_penalty_in_stronghold_or_fortress():
    """Defense 3, roll 3: penalized threshold 2 misses; exempt threshold 3 hits."""
    defs = _defs()
    attackers = [_unit("a", "three")]
    archers = [_unit("d", "arch")]

    penalized = resolve_archer_prefire(
        attackers, archers, defs, [3], territory_def=_terr(False, "plains"),
    )
    assert penalized.defender_hits == 0

    stronghold = resolve_archer_prefire(
        [_unit("a", "three")], [_unit("d", "arch")], defs, [3],
        territory_def=_terr(True, "forest"),
    )
    assert stronghold.defender_hits == 1

    fortress = resolve_archer_prefire(
        [_unit("a", "three")], [_unit("d", "arch")], defs, [3],
        territory_def=_terr(False, "fortress"),
    )
    assert fortress.defender_hits == 1


def _synthetic_defs(unit_defs):
    # Defense 7: threshold 6 with penalty (roll 7 misses), threshold 7 exempt (roll 7 hits).
    unit_defs["defarchx"] = UnitDefinition(
        id="defarchx", display_name="Def Arch X", faction="rohan", archetype="archer",
        tags=["land"], attack=1, defense=7, movement=1, health=1, cost={"power": 5},
        dice=1, specials=["archer"],
    )
    unit_defs["cheapx"] = UnitDefinition(
        id="cheapx", display_name="Cheap X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=3, movement=1, health=1, cost={"power": 1},
        dice=1, specials=[],
    )
    return unit_defs


def _make_unit(state, faction: str, unit_id: str, unit_defs) -> Unit:
    ud = unit_defs[unit_id]
    return Unit(
        instance_id=state.generate_unit_instance_id(faction, unit_id),
        unit_id=unit_id,
        remaining_movement=ud.movement,
        remaining_health=ud.health,
        base_movement=ud.movement,
        base_health=ud.health,
    )


def _run_initiate_prefire(territory_id: str, defender_roll: int = 7):
    """Initiate (no siegeworks) with a fixed defender prefire roll; returns (state, events)."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    unit_defs = _synthetic_defs(unit_defs)
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    t = state.territories[territory_id]
    t.units.clear()
    t.owner = "rohan"
    att1 = _make_unit(state, "isengard", "cheapx", unit_defs)
    att2 = _make_unit(state, "isengard", "cheapx", unit_defs)
    archer = _make_unit(state, "rohan", "defarchx", unit_defs)
    t.units.extend([att1, att2, archer])
    state.current_faction = "isengard"
    state.phase = "combat"
    action = initiate_combat(
        "isengard", territory_id, {"attacker": [], "defender": [defender_roll]},
    )
    return apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _prefire_event(events):
    prefires = [e for e in events if e.payload.get("is_archer_prefire")]
    assert len(prefires) == 1, f"expected one archer prefire event, got {len(prefires)}"
    return prefires[0]


def _archer_display(payload) -> dict:
    return next(
        u for u in payload["defender_units_at_start"] if u["unit_id"] == "defarchx"
    )


def test_stronghold_territory_prefire_uses_full_defense():
    state, events = _run_initiate_prefire(TERRITORY_STRONGHOLD, defender_roll=7)
    ev = _prefire_event(events)
    # No penalty: threshold 7, roll 7 hits; dice grouped under the full defense.
    assert ev.payload["defender_hits"] == 1
    assert list(ev.payload["defender_dice"].keys()) == [7]
    assert ev.payload["defender_dice"][7]["hits"] == 1
    assert _archer_display(ev.payload)["effective_defense"] == 7
    # One of two attackers died; combat continues with the prefire as log entry 0.
    assert state.active_combat is not None
    assert state.active_combat.combat_log[0].is_archer_prefire
    assert state.active_combat.combat_log[0].defender_hits == 1


def test_fortress_territory_prefire_uses_full_defense():
    state, events = _run_initiate_prefire(TERRITORY_FORTRESS, defender_roll=7)
    ev = _prefire_event(events)
    assert ev.payload["defender_hits"] == 1
    assert list(ev.payload["defender_dice"].keys()) == [7]
    assert _archer_display(ev.payload)["effective_defense"] == 7
    assert state.active_combat is not None


def test_plain_territory_prefire_keeps_penalty():
    state, events = _run_initiate_prefire(TERRITORY_PLAIN, defender_roll=7)
    ev = _prefire_event(events)
    # Penalty applies: threshold 6, roll 7 misses; dice grouped under defense-1.
    assert ev.payload["defender_hits"] == 0
    assert list(ev.payload["defender_dice"].keys()) == [6]
    assert ev.payload["defender_dice"][6]["hits"] == 0
    assert _archer_display(ev.payload)["effective_defense"] == 6
    assert state.active_combat is not None
    assert state.active_combat.combat_log[0].is_archer_prefire
    assert state.active_combat.combat_log[0].defender_hits == 0


def _sim_defs():
    unit_defs, territory_defs, *_ = load_static_definitions(setup_id=SETUP)
    unit_defs = _synthetic_defs(unit_defs)
    return unit_defs, territory_defs


def test_sim_stronghold_archer_prefire_no_penalty():
    """Sim: threshold 7 with penalty exemption — every roll hits, regardless of seed."""
    unit_defs, territory_defs = _sim_defs()
    out = run_one_battle(
        [{"unit_id": "cheapx", "count": 2}],
        [{"unit_id": "defarchx", "count": 1}],
        TERRITORY_STRONGHOLD, unit_defs, territory_defs,
        options=SimOptions(max_rounds=0), seed=1,
    )
    assert out.defender_prefire_hits == 1
    assert out.attacker_casualties == {"cheapx": 1}
