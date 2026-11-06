"""Regression: _count_hits/_count_hits_split raise on dice starvation (audit L4)."""
import pytest
from backend.engine.combat import _count_hits, _count_hits_split
from backend.engine.definitions import UnitDefinition
from backend.engine.state import Unit


def _unit(instance_id: str, unit_id: str) -> Unit:
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )


def _def(unit_id: str, dice: int, attack: int = 3, defense: int = 3,
         archetype: str = "infantry") -> UnitDefinition:
    return UnitDefinition(
        id=unit_id,
        display_name=unit_id,
        faction="gondor",
        archetype=archetype,
        tags=["land"],
        attack=attack,
        defense=defense,
        movement=1,
        health=1,
        cost={"power": 1},
        dice=dice,
    )


def test_count_hits_raises_when_rolls_short():
    unit_defs = {"soldier": _def("soldier", dice=2)}
    units = [_unit("s1", "soldier"), _unit("s2", "soldier")]
    with pytest.raises(ValueError, match="starvation"):
        _count_hits(units, [1, 2, 3], unit_defs, is_attacker=True)


def test_count_hits_exact_rolls_ok():
    unit_defs = {"soldier": _def("soldier", dice=2, attack=3)}
    units = [_unit("s1", "soldier")]
    assert _count_hits(units, [2, 4], unit_defs, is_attacker=True) == 1


def test_count_hits_surplus_rolls_ok():
    unit_defs = {"soldier": _def("soldier", dice=1)}
    units = [_unit("s1", "soldier")]
    assert _count_hits(units, [1, 5, 6], unit_defs, is_attacker=True) == 1


def test_count_hits_missing_def_does_not_consume():
    unit_defs = {"soldier": _def("soldier", dice=1, attack=3)}
    units = [_unit("s1", "soldier"), _unit("s2", "ghost")]
    assert _count_hits(units, [1], unit_defs, is_attacker=True) == 1


def test_count_hits_excluded_archetype_does_not_consume():
    unit_defs = {
        "soldier": _def("soldier", dice=1, attack=3),
        "siegework": _def("siegework", dice=2, archetype="siegework"),
    }
    units = [_unit("s1", "soldier"), _unit("sw1", "siegework")]
    hits = _count_hits(
        units, [1], unit_defs, is_attacker=True,
        exclude_archetypes={"siegework"},
    )
    assert hits == 1


def test_count_hits_zero_dice_unit_does_not_consume():
    unit_defs = {
        "soldier": _def("soldier", dice=1, attack=3),
        "banner": _def("banner", dice=0),
    }
    units = [_unit("s1", "soldier"), _unit("b1", "banner")]
    assert _count_hits(units, [1], unit_defs, is_attacker=True) == 1


def test_count_hits_split_raises_when_rolls_short():
    unit_defs = {"soldier": _def("soldier", dice=2)}
    units = [_unit("s1", "soldier"), _unit("s2", "soldier")]
    with pytest.raises(ValueError, match="starvation"):
        _count_hits_split(
            units, [1, 2], unit_defs, is_attacker=True,
            ladder_instance_ids={"s1"},
        )


def test_count_hits_split_exact_rolls_ok():
    unit_defs = {"soldier": _def("soldier", dice=1, attack=3)}
    units = [_unit("s1", "soldier"), _unit("s2", "soldier")]
    ladder_hits, other_hits = _count_hits_split(
        units, [1, 5], unit_defs, is_attacker=True,
        ladder_instance_ids={"s1"},
    )
    assert ladder_hits == 1
    assert other_hits == 0


def test_count_hits_split_surplus_rolls_ok():
    unit_defs = {"soldier": _def("soldier", dice=1, attack=3)}
    units = [_unit("s1", "soldier"), _unit("s2", "soldier")]
    ladder_hits, other_hits = _count_hits_split(
        units, [1, 5, 6, 6], unit_defs, is_attacker=True,
        ladder_instance_ids={"s2"},
    )
    assert ladder_hits == 0
    assert other_hits == 1
