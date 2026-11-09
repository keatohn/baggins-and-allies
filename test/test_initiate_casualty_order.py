"""
Modal H1 (issue #51): initiate_combat must honor the round-0 pill choices.

The combat modal shows Casualty Priority and Must Conquer pills before Start, but
the initiate path hardcoded casualty_order_attacker="best_unit" and
must_conquer=False. When no prefire runs, initiate resolves round 1 itself, so the
first (and most common) round ignored the player's choice — and the values were
never persisted on ActiveCombat either, so later continues started from defaults.

Fix: initiate_combat accepts casualty_order / must_conquer, the reducer threads
them into every round it resolves and every ActiveCombat it creates (siegeworks,
stealth prefire, archer prefire, round 1).

Run: .venv/bin/pytest test/test_initiate_casualty_order.py
"""

from backend.engine.actions import initiate_combat
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"  # non-stronghold: no wall soak, no defender siege dice


def _synthetic_defs(unit_defs, stealth: bool = False):
    """Inject controlled unit types.

    cheapx: cheap + hard-hitting; costlyx: expensive + weak. best_unit kills
    cheapx first (cost asc), best_attack kills costlyx first (attack asc) — one
    hit produces opposite victims under the two orders.

    defx/defsw have defense 10, so any defender die (d10) is a hit: one defender
    roll == exactly one hit, no roll-face luck involved.

    stealth=True gives every attacker the stealth special so initiate opens with
    a stealth prefire instead of round 1.
    """
    att_specials = ["stealth"] if stealth else []
    unit_defs["cheapx"] = UnitDefinition(
        id="cheapx", display_name="Cheap X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=1, movement=1, health=1, cost={"power": 1},
        dice=1, specials=list(att_specials),
    )
    unit_defs["costlyx"] = UnitDefinition(
        id="costlyx", display_name="Costly X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 10},
        dice=1, specials=list(att_specials),
    )
    unit_defs["aerialx"] = UnitDefinition(
        id="aerialx", display_name="Aerial X", faction="isengard", archetype="aerial",
        tags=["land"], attack=5, defense=1, movement=4, health=1, cost={"power": 50},
        dice=1, specials=list(att_specials),
    )
    unit_defs["defx"] = UnitDefinition(
        id="defx", display_name="Def X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=10, movement=1, health=1, cost={"power": 5},
        dice=1, specials=[],
    )
    # Defender siegework: rolls in the dedicated siegeworks round (any roll hits).
    unit_defs["defsw"] = UnitDefinition(
        id="defsw", display_name="Def SW", faction="rohan", archetype="siegework",
        tags=["land"], attack=1, defense=10, movement=0, health=1, cost={"power": 5},
        dice=1, specials=[],
    )
    # Attacker siegework with dice: forces the dedicated siegeworks round at open.
    # Attack 9 so a roll of 10 misses (the incidental siege volley stays harmless).
    unit_defs["siegebox"] = UnitDefinition(
        id="siegebox", display_name="Siege Box", faction="isengard", archetype="siegework",
        tags=["land"], attack=9, defense=0, movement=0, health=1, cost={"power": 100},
        dice=3, specials=[],
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


def _fresh_battle(attackers: list[str], defenders: list[str], stealth: bool = False):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    unit_defs = _synthetic_defs(unit_defs, stealth=stealth)
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    t = state.territories[TERRITORY]
    t.units.clear()
    t.owner = "rohan"
    att = [_make_unit(state, "isengard", uid, unit_defs) for uid in attackers]
    dfn = [_make_unit(state, "rohan", uid, unit_defs) for uid in defenders]
    t.units.extend([*att, *dfn])
    state.current_faction = "isengard"
    state.phase = "combat"
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs, att, dfn


def _initiate(state, ud, td, fd, cd, pd, rolls, **kwargs):
    action = initiate_combat("isengard", TERRITORY, rolls, **kwargs)
    return apply_action(state, action, ud, td, fd, cd, pd)


def _iid(units, unit_id: str) -> str:
    return next(u.instance_id for u in units if u.unit_id == unit_id)


def _round_event(events, round_number: int):
    rounds = [e for e in events if e.type == "combat_round_resolved"]
    assert len(rounds) == 1, f"expected one combat_round_resolved, got {len(rounds)}"
    assert rounds[0].payload["round_number"] == round_number
    return rounds[0].payload


def test_initiate_round1_honors_best_attack():
    """best_attack on the Start pill: the low-attack attacker dies in the initiate round."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "costlyx"], ["defx"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
        casualty_order="best_attack",
    )
    ev = _round_event(events, 1)
    assert ev["attacker_casualties"] == [_iid(att, "costlyx")]
    assert state.active_combat is not None
    assert state.active_combat.casualty_order_attacker == "best_attack"


def test_initiate_round1_defaults_to_best_unit():
    """No pill payload: cheapest attacker dies (behavior unchanged from before)."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "costlyx"], ["defx"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
    )
    ev = _round_event(events, 1)
    assert ev["attacker_casualties"] == [_iid(att, "cheapx")]
    assert state.active_combat is not None
    assert state.active_combat.casualty_order_attacker == "best_unit"
    assert state.active_combat.must_conquer is False


def test_initiate_must_conquer_sacrifices_aerial_not_last_ground():
    """must_conquer=True: the hit that would kill the last ground unit goes to the aerial."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "aerialx"], ["defx"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
        must_conquer=True,
    )
    ev = _round_event(events, 1)
    assert ev["attacker_casualties"] == [_iid(att, "aerialx")]
    assert {u.unit_id for u in state.territories[TERRITORY].units} == {"cheapx", "defx"}
    assert state.active_combat is not None
    assert state.active_combat.must_conquer is True


def test_initiate_without_must_conquer_kills_ground_first():
    """Same roster, no pill: the cheap ground unit dies (aerial survives)."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "aerialx"], ["defx"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
    )
    ev = _round_event(events, 1)
    assert ev["attacker_casualties"] == [_iid(att, "cheapx")]
    assert state.active_combat is not None
    assert state.active_combat.must_conquer is False


def test_initiate_archer_prefire_honors_best_attack():
    """Archer prefire (deferred to the same initiate) picks victims with the pill order too."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "costlyx"], ["rohirrim_archer"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
        casualty_order="best_attack",
    )
    ev = _round_event(events, 0)
    assert ev.get("is_archer_prefire")
    assert ev["attacker_casualties"] == [_iid(att, "costlyx")]
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0
    assert state.active_combat.combat_log[-1].is_archer_prefire
    assert state.active_combat.casualty_order_attacker == "best_attack"


def test_initiate_archer_prefire_defaults_to_best_unit():
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(["cheapx", "costlyx"], ["rohirrim_archer"])
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": [1]},
    )
    ev = _round_event(events, 0)
    assert ev["attacker_casualties"] == [_iid(att, "cheapx")]
    assert state.active_combat is not None
    assert state.active_combat.casualty_order_attacker == "best_unit"


def test_initiate_siegeworks_round_honors_best_attack():
    """Siegeworks round at open: attacker victims follow the pill, value is persisted."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(
        ["siegebox", "cheapx", "costlyx"], ["defsw"],
    )
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        # 3 siege dice (siegebox) + 1 defender die (defsw)
        {"attacker": [10, 10, 10], "defender": [1]},
        casualty_order="best_attack",
    )
    ev = _round_event(events, 0)
    assert ev.get("is_siegeworks_round")
    assert ev["attacker_casualties"] == [_iid(att, "costlyx")]
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0
    assert state.active_combat.combat_log[0].is_siegeworks_round
    assert state.active_combat.casualty_order_attacker == "best_attack"


def test_initiate_stealth_prefire_persists_pill_choices():
    """Stealth prefire opens the battle: no attacker casualties there, but the pill
    choices must still be stored for every later round."""
    state, ud, td, fd, cd, pd, att, _ = _fresh_battle(
        ["cheapx", "costlyx"], ["defx"], stealth=True,
    )
    state, events = _initiate(
        state, ud, td, fd, cd, pd,
        {"attacker": [10, 10], "defender": []},
        casualty_order="best_attack",
        must_conquer=True,
    )
    ev = _round_event(events, 0)
    assert ev.get("is_stealth_prefire")
    assert ev["defender_casualties"] == []  # both rolls miss (10 > attack-1)
    assert state.active_combat is not None
    assert state.active_combat.round_number == 0
    assert state.active_combat.combat_log[0].is_stealth_prefire
    assert state.active_combat.casualty_order_attacker == "best_attack"
    assert state.active_combat.must_conquer is True
