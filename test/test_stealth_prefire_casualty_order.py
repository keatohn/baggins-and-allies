"""
M1 (part 1): stealth prefire must honor the territory's defender casualty order.

Stealth prefire victims are defender units, so their hit assignment must use
state.territory_defender_casualty_order (documented default best_defense for
strongholds/capitals/camps/ports) exactly like every other round — previously it
hardcoded "best_unit" (cheap-first), silently giving stronghold defenders the
wrong soak order.

Run: .venv/bin/pytest test/test_stealth_prefire_casualty_order.py
"""

from types import SimpleNamespace

from backend.engine.actions import initiate_combat
from backend.engine.combat import resolve_stealth_prefire
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
    # tankx: expensive, low defense. guardx: cheap, high defense.
    # best_unit dies cheapest-first -> guardx; best_defense dies lowest-defense-first -> tankx.
    return {
        "shadow": SimpleNamespace(attack=3, defense=2, dice=1, health=1, cost={"power": 4}, archetype="infantry", specials=["stealth"]),
        "tankx": SimpleNamespace(attack=1, defense=1, dice=1, health=1, cost={"power": 10}, archetype="infantry", specials=[]),
        "guardx": SimpleNamespace(attack=1, defense=5, dice=1, health=1, cost={"power": 1}, archetype="infantry", specials=[]),
    }


def test_stealth_prefire_default_order_loses_cheap_first():
    defs = _defs()
    attackers = [_unit("s", "shadow")]
    defenders = [_unit("d_tank", "tankx"), _unit("d_guard", "guardx")]
    result = resolve_stealth_prefire(attackers, defenders, defs, [1])
    assert result.attacker_hits == 1
    assert result.defender_casualties == ["d_guard"]
    assert result.defender_wounded == []


def test_stealth_prefire_best_defense_loses_low_defense_first():
    defs = _defs()
    attackers = [_unit("s", "shadow")]
    defenders = [_unit("d_tank", "tankx"), _unit("d_guard", "guardx")]
    result = resolve_stealth_prefire(
        attackers, defenders, defs, [1], casualty_order_defender="best_defense",
    )
    assert result.attacker_hits == 1
    assert result.defender_casualties == ["d_tank"]
    assert result.defender_wounded == []


def _synthetic_defs(unit_defs):
    """Inject controlled attacker/defender unit types into the loaded setup defs."""
    unit_defs["shadowx"] = UnitDefinition(
        id="shadowx", display_name="Shadow X", faction="isengard", archetype="infantry",
        tags=["land"], attack=3, defense=2, movement=2, health=1, cost={"power": 4},
        dice=1, specials=["stealth"],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 10},
    )
    unit_defs["guardx"] = UnitDefinition(
        id="guardx", display_name="Guard X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=5, movement=1, health=1, cost={"power": 1},
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


def _fresh_battle(defender_order: str):
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
    attacker = _make_unit(state, "isengard", "shadowx", unit_defs)
    tank = _make_unit(state, "rohan", "tankx", unit_defs)
    guard = _make_unit(state, "rohan", "guardx", unit_defs)
    t.units.extend([attacker, tank, guard])
    state.current_faction = "isengard"
    state.phase = "combat"
    state.territory_defender_casualty_order[TERRITORY] = defender_order
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _initiate(state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs):
    action = initiate_combat(
        "isengard", TERRITORY, {"attacker": [1], "defender": []},
    )
    return apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _survivor_unit_ids(state):
    return sorted(u.unit_id for u in state.territories[TERRITORY].units)


def test_initiate_stealth_prefire_honors_best_defense():
    """best_defense territory order: the low-defense defender dies in stealth prefire."""
    state, ud, td, fd, cd, pd = _fresh_battle("best_defense")
    state, _ = _initiate(state, ud, td, fd, cd, pd)
    assert state.active_combat is not None
    assert _survivor_unit_ids(state) == ["guardx", "shadowx"]


def test_initiate_stealth_prefire_honors_best_unit():
    """best_unit territory order: the cheap defender dies (behavior unchanged from before)."""
    state, ud, td, fd, cd, pd = _fresh_battle("best_unit")
    state, _ = _initiate(state, ud, td, fd, cd, pd)
    assert state.active_combat is not None
    assert _survivor_unit_ids(state) == ["shadowx", "tankx"]
