"""
M1 (part 2): archer prefire must honor the attacker's casualty order.

Archer prefire victims are the *attackers*, so hit assignment must follow the
active combat's casualty_order_attacker (the order the attacker chose on the
continue payload) exactly like every other round — previously it hardcoded
"best_unit" (cheap-first), silently ignoring the attacker's chosen order.

Reachable when a battle opens with a siegeworks round: the prefire defers to the
round-0 continue, whose payload updates combat.casualty_order_attacker before the
prefire branch runs.

Run: .venv/bin/pytest test/test_archer_prefire_casualty_order.py
"""

from types import SimpleNamespace

from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.combat import resolve_archer_prefire
from backend.engine.combat_sim import SimOptions, run_one_battle
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"


def _unit(iid: str, unit_id: str):
    return SimpleNamespace(
        instance_id=iid, unit_id=unit_id, health=1, remaining_health=1, remaining_movement=0,
    )


def _defs():
    # cheapx: cheap, high attack. costlyx: expensive, low attack.
    # best_unit dies cheapest-first -> cheapx; best_attack dies lowest attack-first -> costlyx.
    return {
        "arch": SimpleNamespace(attack=1, defense=2, dice=1, health=1, cost={"power": 2}, archetype="archer", specials=["archer"]),
        "cheapx": SimpleNamespace(attack=5, defense=3, dice=1, health=1, cost={"power": 1}, archetype="infantry", specials=[]),
        "costlyx": SimpleNamespace(attack=1, defense=3, dice=1, health=1, cost={"power": 10}, archetype="infantry", specials=[]),
    }


def test_archer_prefire_default_order_loses_cheap_first():
    defs = _defs()
    attackers = [_unit("a_cheap", "cheapx"), _unit("a_costly", "costlyx")]
    result = resolve_archer_prefire(attackers, [_unit("d_arch", "arch")], defs, [1])
    assert result.defender_hits == 1
    assert result.attacker_casualties == ["a_cheap"]
    assert result.attacker_wounded == []


def test_archer_prefire_best_attack_loses_low_attack_first():
    defs = _defs()
    attackers = [_unit("a_cheap", "cheapx"), _unit("a_costly", "costlyx")]
    result = resolve_archer_prefire(
        attackers, [_unit("d_arch", "arch")], defs, [1], casualty_order_attacker="best_attack",
    )
    assert result.defender_hits == 1
    assert result.attacker_casualties == ["a_costly"]
    assert result.attacker_wounded == []


def _synthetic_defs(unit_defs):
    """Inject controlled attacker unit types into the loaded setup defs.

    siegebox (siegework, dice 3) forces a siegeworks round at initiate so the
    archer prefire defers to the round-0 continue. Under best_attack its
    attack*dice (27) is the highest, under best_unit its cost (100) the highest,
    so it is never the first victim either way. Attack 9 (not 10) lets an
    in-range roll of 10 miss, so the incidental siegeworks volley is harmless.
    """
    unit_defs["siegebox"] = UnitDefinition(
        id="siegebox", display_name="Siege Box", faction="isengard", archetype="siegework",
        tags=["land"], attack=9, defense=0, movement=0, health=1, cost={"power": 100},
        dice=3, specials=[],
    )
    unit_defs["cheapx"] = UnitDefinition(
        id="cheapx", display_name="Cheap X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=3, movement=1, health=1, cost={"power": 1},
        dice=1, specials=[],
    )
    unit_defs["costlyx"] = UnitDefinition(
        id="costlyx", display_name="Costly X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=3, movement=1, health=1, cost={"power": 10},
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


def _fresh_battle():
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
    t = state.territories[TERRITORY]
    t.units.clear()
    t.owner = "rohan"
    siegebox = _make_unit(state, "isengard", "siegebox", unit_defs)
    cheap = _make_unit(state, "isengard", "cheapx", unit_defs)
    costly = _make_unit(state, "isengard", "costlyx", unit_defs)
    archer = _make_unit(state, "rohan", "rohirrim_archer", unit_defs)
    t.units.extend([siegebox, cheap, costly, archer])
    state.current_faction = "isengard"
    state.phase = "combat"
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _initiate(state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs):
    # All-miss rolls (10 > siegebox attack 9): the siegeworks round exists only
    # to defer archer prefire to the continue, so it must leave survivors intact.
    action = initiate_combat("isengard", TERRITORY, {"attacker": [10, 10, 10], "defender": []})
    return apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _continue(state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs, casualty_order=None):
    action = continue_combat(
        "isengard", {"attacker": [], "defender": [1]}, casualty_order=casualty_order,
    )
    return apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _survivor_unit_ids(state):
    return sorted(u.unit_id for u in state.territories[TERRITORY].units)


def test_continue_archer_prefire_honors_best_attack():
    """best_attack order on the round-0 continue: the low-attack attacker dies in prefire."""
    state, ud, td, fd, cd, pd = _fresh_battle()
    state, _ = _initiate(state, ud, td, fd, cd, pd)
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0
    assert state.active_combat.combat_log[0].is_siegeworks_round
    assert _survivor_unit_ids(state) == ["cheapx", "costlyx", "rohirrim_archer", "siegebox"]
    costly_iid = next(u.instance_id for u in state.territories[TERRITORY].units if u.unit_id == "costlyx")

    state, _ = _continue(state, ud, td, fd, cd, pd, casualty_order="best_attack")
    assert state.active_combat is not None
    assert state.active_combat.casualty_order_attacker == "best_attack"
    assert _survivor_unit_ids(state) == ["cheapx", "rohirrim_archer", "siegebox"]
    log = state.active_combat.combat_log
    assert log[-1].is_archer_prefire
    assert log[-1].defender_hits == 1
    assert log[-1].attacker_casualties == [costly_iid]


def test_continue_archer_prefire_defaults_to_best_unit():
    """No casualty_order on the continue: cheapest attacker dies (behavior unchanged from before)."""
    state, ud, td, fd, cd, pd = _fresh_battle()
    state, _ = _initiate(state, ud, td, fd, cd, pd)
    assert state.active_combat is not None

    state, _ = _continue(state, ud, td, fd, cd, pd)
    assert state.active_combat is not None
    assert state.active_combat.casualty_order_attacker == "best_unit"
    assert _survivor_unit_ids(state) == ["costlyx", "rohirrim_archer", "siegebox"]
    log = state.active_combat.combat_log
    assert log[-1].is_archer_prefire
    assert log[-1].defender_hits == 1


def _sim_defs():
    unit_defs, territory_defs, *_ = load_static_definitions(setup_id=SETUP)
    unit_defs["cheapx"] = UnitDefinition(
        id="cheapx", display_name="Cheap X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=3, movement=1, health=1, cost={"power": 1},
        dice=1, specials=[],
    )
    unit_defs["costlyx"] = UnitDefinition(
        id="costlyx", display_name="Costly X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=3, movement=1, health=1, cost={"power": 10},
        dice=1, specials=[],
    )
    # Defense 7 - prefire penalty 1 = 6: hits on any die face, so the prefire hit is deterministic.
    unit_defs["defarchx"] = UnitDefinition(
        id="defarchx", display_name="Def Arch X", faction="rohan", archetype="archer",
        tags=["land"], attack=1, defense=7, movement=1, health=1, cost={"power": 5},
        dice=1, specials=["archer"],
    )
    return unit_defs, territory_defs


def test_sim_archer_prefire_honors_best_attack():
    """Sim: with casualty_order_attacker=best_attack the low-attack attacker dies in prefire."""
    unit_defs, territory_defs = _sim_defs()
    att = [{"unit_id": "cheapx", "count": 1}, {"unit_id": "costlyx", "count": 1}]
    defs = [{"unit_id": "defarchx", "count": 1}]
    out = run_one_battle(
        att, defs, TERRITORY, unit_defs, territory_defs,
        options=SimOptions(max_rounds=0, casualty_order_attacker="best_attack"), seed=1,
    )
    assert out.winner == "defender"
    assert out.defender_prefire_hits == 1
    assert out.attacker_casualties == {"costlyx": 1}


def test_sim_archer_prefire_defaults_to_best_unit():
    """Sim: default order keeps cheapest-first (unchanged behavior)."""
    unit_defs, territory_defs = _sim_defs()
    att = [{"unit_id": "cheapx", "count": 1}, {"unit_id": "costlyx", "count": 1}]
    defs = [{"unit_id": "defarchx", "count": 1}]
    out = run_one_battle(
        att, defs, TERRITORY, unit_defs, territory_defs,
        options=SimOptions(max_rounds=0), seed=1,
    )
    assert out.winner == "defender"
    assert out.defender_prefire_hits == 1
    assert out.attacker_casualties == {"cheapx": 1}
