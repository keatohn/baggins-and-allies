"""
Casualty order contract (docs/CASUALTY_ORDERS.md):

Step 0 — the stronghold wall is a territory HP pool: non-ladder attacker hits soak it first,
ladder-climber hits bypass it. Ladder vs non-ladder does NOT change unit-level casualty order.
Also guards the M2 regression: a defender must never appear in both defender_casualties and
defender_wounded from the same round (ladder hit wounded it, overflow hit finished it).
"""
import pytest
from backend.engine.definitions import load_static_definitions
from backend.engine.state import Unit
from backend.engine.combat import resolve_combat_round


SETUP_ID = "motw_1.0"


@pytest.fixture
def unit_defs():
    ud, *_ = load_static_definitions(setup_id=SETUP_ID)
    if "urukhai_warrior" not in ud or "armored_troll" not in ud:
        pytest.skip(f"{SETUP_ID} lacks urukhai_warrior/armored_troll")
    return ud


def _make(unit_defs, faction: str, unit_id: str, counter: list) -> Unit:
    ud = unit_defs[unit_id]
    counter[0] += 1
    return Unit(
        instance_id=f"{faction}_{unit_id}_{counter[0]:03d}",
        unit_id=unit_id,
        remaining_movement=ud.movement,
        remaining_health=ud.health,
        base_movement=ud.movement,
        base_health=ud.health,
    )


def _rolls(units, unit_defs, value: int) -> list[int]:
    return [value] * sum(getattr(unit_defs[u.unit_id], "dice", 1) for u in units)


def test_normal_attacker_hits_soak_wall_before_reaching_units(unit_defs):
    """No ladder: both attacker hits go to the wall; defender units take nothing."""
    c = [0]
    attackers = [
        _make(unit_defs, "isengard", "dunland_wildman", c),
        _make(unit_defs, "isengard", "uruk_crossbowman", c),
    ]
    troll = _make(unit_defs, "mordor", "armored_troll", c)
    rolls = {"attacker": _rolls(attackers, unit_defs, 1),
             "defender": _rolls([troll], unit_defs, 6)}

    result, hp_after = resolve_combat_round(
        attackers, [troll], unit_defs, rolls,
        defender_stronghold_hp=2,
    )

    assert hp_after == 0
    assert result.defender_casualties == []
    assert result.defender_wounded == []
    assert troll.remaining_health == 2


def test_ladder_hits_bypass_wall_and_land_on_units(unit_defs):
    """Ladder climber's hit skips the wall; only the non-ladder hit soaks it."""
    c = [0]
    climber = _make(unit_defs, "isengard", "urukhai_warrior", c)
    footman = _make(unit_defs, "isengard", "dunland_wildman", c)
    troll = _make(unit_defs, "mordor", "armored_troll", c)
    rolls = {"attacker": _rolls([climber, footman], unit_defs, 1),
             "defender": _rolls([troll], unit_defs, 6)}

    result, hp_after = resolve_combat_round(
        [climber, footman], [troll], unit_defs, rolls,
        defender_stronghold_hp=2,
        attacker_ladder_instance_ids={climber.instance_id},
    )

    assert hp_after == 1
    assert result.defender_casualties == []
    assert result.defender_wounded == [troll.instance_id]
    assert troll.remaining_health == 1


def test_ladder_and_overflow_hits_never_share_a_defender_casualty_and_wound(unit_defs):
    """M2 regression: wounded by the ladder hit, finished by the overflow hit -> casualty only."""
    c = [0]
    climber = _make(unit_defs, "isengard", "urukhai_warrior", c)
    footman1 = _make(unit_defs, "isengard", "dunland_wildman", c)
    footman2 = _make(unit_defs, "isengard", "uruk_crossbowman", c)
    troll = _make(unit_defs, "mordor", "armored_troll", c)
    rolls = {"attacker": _rolls([climber, footman1, footman2], unit_defs, 1),
             "defender": _rolls([troll], unit_defs, 6)}

    result, hp_after = resolve_combat_round(
        [climber, footman1, footman2], [troll], unit_defs, rolls,
        defender_stronghold_hp=1,
        attacker_ladder_instance_ids={climber.instance_id},
    )

    # 1 ladder hit + 2 normal hits; wall eats 1 normal -> 2 hits reach the 2 HP troll.
    assert hp_after == 0
    assert result.defender_casualties == [troll.instance_id]
    assert troll.instance_id not in result.defender_wounded
    assert result.defender_wounded == []


def test_ladder_hits_assign_identically_to_normal_hits_among_units(unit_defs):
    """No per-unit stronghold sort key: same hits to units pick the same victim either way."""
    def run(with_ladder: bool) -> tuple[list[str], list[str]]:
        c = [0]
        climber = _make(unit_defs, "isengard", "urukhai_warrior", c)
        footman = _make(unit_defs, "isengard", "dunland_wildman", c)
        troll = _make(unit_defs, "mordor", "armored_troll", c)
        orc = _make(unit_defs, "mordor", "orc_warrior", c)
        ladder = {climber.instance_id} if with_ladder else None
        rolls = {"attacker": _rolls([climber, footman], unit_defs, 1),
                 "defender": _rolls([troll, orc], unit_defs, 6)}
        result, _ = resolve_combat_round(
            [climber, footman], [troll, orc], unit_defs, rolls,
            defender_stronghold_hp=1,
            attacker_ladder_instance_ids=ladder,
        )
        return result.defender_casualties, result.defender_wounded

    from_ladder = run(with_ladder=True)
    from_normal = run(with_ladder=False)

    assert from_ladder == from_normal
    # Healthiest defender soaks first: 2 HP troll takes the single hit.
    assert from_ladder[0] == []
    assert len(from_ladder[1]) == 1
    assert "armored_troll" in from_ladder[1][0]
