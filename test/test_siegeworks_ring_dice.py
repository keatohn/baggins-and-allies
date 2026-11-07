"""
M3 (combat audit): attacker ring bonus dice must count in the dedicated siegeworks round.

resolve_siegeworks_round accepted attacker_effective_dice_override but never read it,
so a ring bearer who rolls in the siegeworks round silently lost its bonus dice (the
defender override was honored). The continue call site also passed att_effective_dice,
which zeroes paired bombs for standard combat — the round needs a ring-only override
so paired bombs keep their base dice.

Run: .venv/bin/pytest test/test_siegeworks_ring_dice.py
"""

from types import SimpleNamespace

from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.combat import resolve_siegeworks_round
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import ActiveCombat, Ring, Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"


def _ns_unit(iid: str, unit_id: str, health: int = 5) -> Unit:
    return Unit(
        instance_id=iid, unit_id=unit_id,
        remaining_movement=0, remaining_health=health,
        base_movement=0, base_health=health,
    )


def test_resolver_honors_attacker_effective_dice_override():
    """The override sets the attacker's dice count, not just the defender's."""
    defs = {
        "sw_cat": SimpleNamespace(
            archetype="siegework", dice=1, attack=3, defense=0,
            specials=[], tags=["land"], cost={"power": 5}, health=1,
        ),
        "defender": SimpleNamespace(
            archetype="infantry", dice=1, attack=1, defense=3,
            specials=[], tags=["land"], cost={"power": 1}, health=5,
        ),
    }

    def _run(override):
        return resolve_siegeworks_round(
            [_ns_unit("a_sw", "sw_cat")],
            [_ns_unit("d_1", "defender")],
            defs,
            {"attacker": [1, 1, 1], "defender": []},
            attacker_effective_dice_override=override,
        )

    base, _, _ = _run(None)
    assert base.attacker_hits == 1  # base dice only: one die consumed

    boosted, _, _ = _run({"a_sw": 3})
    assert boosted.attacker_hits == 3  # override dice: all three dice count


def _synthetic_ring_defs(unit_defs):
    # ringhero: siegework archetype so it rolls in the dedicated siegeworks round, and a
    # hero so it can carry a ring into battle. attack 10 guarantees every roll hits.
    unit_defs["ringhero"] = UnitDefinition(
        id="ringhero", display_name="Ring Hero", faction="isengard", archetype="siegework",
        tags=["land"], attack=10, defense=0, movement=0, health=1, cost={"power": 100},
        dice=1, hero_id="ringhero", specials=[],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=10, cost={"power": 10},
        dice=1, specials=[],
    )
    return unit_defs


def _synthetic_stealth_defs(unit_defs):
    unit_defs["sneakx"] = UnitDefinition(
        id="sneakx", display_name="Sneak X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 1},
        dice=1, specials=["stealth"],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=10, cost={"power": 10},
        dice=1, specials=[],
    )
    # Every attacker must have stealth so the battle opens with a stealth prefire;
    # stealth cancels the dedicated siegeworks round, so the first continue runs a
    # standard round instead.
    unit_defs["bomb"].specials = list(unit_defs["bomb"].specials) + ["stealth"]
    unit_defs["berserker"].specials = list(unit_defs["berserker"].specials) + ["stealth"]
    return unit_defs


def _synthetic_siege_defs(unit_defs):
    """sneakx WITHOUT stealth + tankx: roster for the crafted continue-siegeworks state."""
    unit_defs["sneakx"] = UnitDefinition(
        id="sneakx", display_name="Sneak X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 1},
        dice=1, specials=[],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=10, cost={"power": 10},
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


def _fresh_state(injector=None):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    if injector is not None:
        unit_defs = injector(unit_defs)
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    t = state.territories[TERRITORY]
    t.units.clear()
    t.owner = "rohan"
    state.current_faction = "isengard"
    state.phase = "combat"
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _dice_total(grouped) -> int:
    assert grouped is not None, "siegeworks event must carry grouped attacker dice"
    return sum(len(bucket["rolls"]) for bucket in grouped.values())


def test_initiate_siegeworks_counts_ring_bonus_dice():
    """Full stack: a ring bearer in the siegeworks round rolls base + ring bonus dice."""
    state, ud, td, fd, cd, pd = _fresh_state(_synthetic_ring_defs)
    t = state.territories[TERRITORY]
    hero = _make_unit(state, "isengard", "ringhero", ud)
    tank = _make_unit(state, "rohan", "tankx", ud)
    t.units.extend([hero, tank])
    state.rings_of_power = True
    ring = Ring(id="m3_ring", name="M3 Ring", power=1, territory_id=TERRITORY, rolls_boost=2)
    ring.carried_in_by = hero.instance_id
    state.rings = [ring]

    action = initiate_combat("isengard", TERRITORY, {"attacker": [1], "defender": []})
    state, _ = apply_action(state, action, ud, td, fd, cd, pd)

    assert state.active_combat is not None
    log = state.active_combat.combat_log[0]
    assert log.is_siegeworks_round
    # Base die + 2 ring bonus dice all hit (attack 10); without the override only 1 counts.
    assert log.attacker_hits == 3
    assert _dice_total(log.attacker_dice) == 3  # UI shows the bonus dice too


def test_continue_siegeworks_paired_bomb_keeps_base_dice():
    """The continue call site passes a ring-only override, so bombs are not zeroed.

    Stealth prefire now cancels the siegeworks round outright, so this branch can no
    longer be reached through initiate/continue — it is exercised with a crafted
    round-0 combat whose log has no dedicated round yet (defensive/legacy coverage).
    """
    state, ud, td, fd, cd, pd = _fresh_state(_synthetic_siege_defs)
    state.rings_of_power = False
    state.rings = []
    t = state.territories[TERRITORY]
    bomb = _make_unit(state, "isengard", "bomb", ud)
    zerker = _make_unit(state, "isengard", "berserker", ud)
    sneak = _make_unit(state, "isengard", "sneakx", ud)
    tank = _make_unit(state, "rohan", "tankx", ud)
    t.units.extend([bomb, zerker, sneak, tank])

    attacker_ids = [bomb.instance_id, zerker.instance_id, sneak.instance_id]
    state.active_combat = ActiveCombat(
        attacker_faction="isengard",
        territory_id=TERRITORY,
        attacker_instance_ids=list(attacker_ids),
        round_number=0,
        combat_log=[],
        attackers_have_rolled=True,
        initial_attacker_instance_ids=list(attacker_ids),
        initial_defender_instance_ids=[tank.instance_id],
        fuse_bomb=True,
    )

    # Siegeworks continue: the paired bomb rolls its 6 base dice (attack 6, all misses).
    state, _ = apply_action(
        state,
        continue_combat("isengard", {"attacker": [10] * 6, "defender": []}),
        ud, td, fd, cd, pd,
    )
    assert state.active_combat is not None
    log = state.active_combat.combat_log[-1]
    assert log.is_siegeworks_round
    assert log.attacker_hits == 0
    assert _dice_total(log.attacker_dice) == 6  # a bomb-zeroing override would show 0
    # Paired bomb + bombikazi self-destructed after the fused detonation.
    assert sorted(u.unit_id for u in state.territories[TERRITORY].units) == ["sneakx", "tankx"]


def test_stealth_cancels_siegeworks_continue_runs_standard_round():
    """Stealth prefire logs at round 0 and cancels the dedicated siegeworks round:
    the first continue resolves a standard round 1 instead."""
    state, ud, td, fd, cd, pd = _fresh_state(_synthetic_stealth_defs)
    state.rings_of_power = False
    state.rings = []
    t = state.territories[TERRITORY]
    bomb = _make_unit(state, "isengard", "bomb", ud)
    zerker = _make_unit(state, "isengard", "berserker", ud)
    sneak = _make_unit(state, "isengard", "sneakx", ud)
    tank = _make_unit(state, "rohan", "tankx", ud)
    t.units.extend([bomb, zerker, sneak, tank])

    # Round 0 opens with a stealth prefire; all-miss rolls keep the defender alive.
    state, _ = apply_action(
        state,
        initiate_combat("isengard", TERRITORY, {"attacker": [10, 10], "defender": []}),
        ud, td, fd, cd, pd,
    )
    assert state.active_combat is not None
    assert state.active_combat.combat_log[0].is_stealth_prefire

    # First continue: standard round 1 (siegeworks cancelled by stealth). Attackers
    # send standard-round dice (siegework archetype excluded from standard rolling);
    # defender rolls to keep the tank alive.
    state, _ = apply_action(
        state,
        continue_combat("isengard", {"attacker": [10, 10], "defender": [10]}),
        ud, td, fd, cd, pd,
    )
    assert state.active_combat is not None
    log = state.active_combat.combat_log[-1]
    assert not log.is_siegeworks_round, "stealth must cancel the dedicated siegeworks round"
    assert not log.is_stealth_prefire
    assert state.active_combat.round_number == 1
    # All-miss exchange: the tank survives; the fused bomb + bombikazi pair
    # self-destructed after the standard round (same rule as live combat).
    assert sorted(u.unit_id for u in state.territories[TERRITORY].units) == [
        "sneakx", "tankx",
    ]
