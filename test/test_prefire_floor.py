"""Stealth and archer prefire penalties never take a stat below 1."""
from types import SimpleNamespace

from backend.engine.combat import prefire_stat_modifiers, resolve_archer_prefire, resolve_stealth_prefire


def _unit(iid: str, unit_id: str):
    return SimpleNamespace(
        instance_id=iid, unit_id=unit_id, health=1, remaining_health=1, remaining_movement=0,
    )


def _defs():
    return {
        "one": SimpleNamespace(attack=1, defense=1, dice=1, health=1, cost=1, archetype="infantry", specials=[]),
        "three": SimpleNamespace(attack=3, defense=3, dice=1, health=1, cost=1, archetype="infantry", specials=[]),
        "zero": SimpleNamespace(attack=0, defense=0, dice=1, health=1, cost=1, archetype="infantry", specials=[]),
    }


def test_penalty_floors_at_one():
    defs = _defs()
    units = [_unit("a", "one"), _unit("b", "three"), _unit("c", "zero")]
    mods = prefire_stat_modifiers(units, defs, True, -1)
    assert 1 + mods["a"] == 1
    assert 3 + mods["b"] == 2
    assert 0 + mods["c"] == 0


def test_floor_counts_other_modifiers():
    defs = _defs()
    units = [_unit("a", "one"), _unit("b", "one")]
    mods = prefire_stat_modifiers(units, defs, False, -1, {"a": 1, "b": -1})
    assert 1 + mods["a"] == 1
    assert 1 + mods["b"] == 0


def test_no_penalty_unchanged():
    defs = _defs()
    units = [_unit("a", "one")]
    assert prefire_stat_modifiers(units, defs, True, 0, {"a": 2}) == {"a": 2}


def test_stat_one_still_hits_on_one():
    defs = _defs()
    stealth = resolve_stealth_prefire([_unit("s", "one")], [_unit("d", "three")], defs, [1])
    assert stealth.attacker_hits == 1
    archer = resolve_archer_prefire([_unit("x", "three")], [_unit("r", "one")], defs, [1])
    assert archer.defender_hits == 1
