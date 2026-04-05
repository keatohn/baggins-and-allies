"""Ram vs flexible siegework attacker dice grouping (UI contract)."""
import pytest
from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.combat import group_dice_by_stat, group_siegework_attacker_dice_ram_and_flex


def _make_unit(unit_defs, faction: str, unit_id: str, counter: list) -> Unit:
    ud = unit_defs[unit_id]
    counter[0] += 1
    iid = f"{faction}_{unit_id}_{counter[0]:03d}"
    return Unit(
        instance_id=iid,
        unit_id=unit_id,
        remaining_movement=ud.movement,
        remaining_health=ud.health,
        base_movement=ud.movement,
        base_health=ud.health,
    )


@pytest.fixture
def unit_defs():
    ud, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud


def test_siegework_ram_flex_matches_merged_group_dice(unit_defs):
    """Split ram/flex per stat matches flat group_dice_by_stat for the same rolling order."""
    c = [0]
    att_rolling = [
        _make_unit(unit_defs, "mordor", "battering_ram", c),
        _make_unit(unit_defs, "mordor", "catapult", c),
    ]
    rolls = [1, 2, 3, 4, 5, 6]
    merged = group_dice_by_stat(
        att_rolling, rolls, unit_defs, is_attacker=True,
    )
    split = group_siegework_attacker_dice_ram_and_flex(
        att_rolling, rolls, unit_defs,
    )
    stat = 5
    assert merged[stat]["rolls"] == split[stat]["ram"]["rolls"] + split[stat]["flex"]["rolls"]
    assert len(split[stat]["ram"]["rolls"]) == 3
    assert len(split[stat]["flex"]["rolls"]) == 3
