"""
H3 (combat audit): bombikazi self-destruct only ever affects units that are still living.

- The pairing is re-derived from attackers alive after this round's hits, so a bombikazi
  already destroyed by defender hits is not appended a second time (no duplicate casualty
  ids / duplicated unit_destroyed events) and its pair member is spared.
- A bombikazi killed in a preceding pre-round (defender archer pre-fire) is not in the
  roster for later rounds, so it neither rolls nor self-destructs there.
"""
from collections import Counter
from types import SimpleNamespace

import pytest

from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.combat import (
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    resolve_combat_round,
)
from backend.engine.definitions import load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state


@pytest.fixture(scope="module")
def defs():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id="wotr_exp_1.0"
    )
    return unit_defs, territory_defs, faction_defs, camp_defs, port_defs


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


def _ns(instance_id: str, unit_id: str, hp: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_health=hp,
        remaining_movement=0,
        base_health=hp,
    )


def _pair() -> list[SimpleNamespace]:
    return [_ns("a_bk", "berserker"), _ns("a_bomb", "bomb")]


def _resolve(
    unit_defs,
    attackers,
    defenders,
    defender_hits,
    *,
    hp_shield=None,
    must_conquer=False,
):
    """One standard round: attacker rolls miss, defender damage forced via override."""
    eff_dice, self_destruct, eff_atk = get_attacker_effective_dice_and_bombikazi_self_destruct(
        attackers, unit_defs
    )
    assert self_destruct, "fused rules expected to be active for these rosters"
    result, _ = resolve_combat_round(
        attackers,
        defenders,
        unit_defs,
        {"attacker": [10], "defender": []},
        attacker_effective_dice_override=eff_dice,
        attacker_effective_attack_override=eff_atk,
        bombikazi_self_destruct_ids=self_destruct,
        defender_hits_override=defender_hits,
        must_conquer=must_conquer,
        hp_shield=hp_shield,
    )
    return result


def _duplicates(casualties):
    return [iid for iid, n in Counter(casualties).items() if n > 1]


def test_bombikazi_killed_by_hits_is_not_appended_twice(defs):
    """Defender kills the bombikazi this round: listed exactly once, pair member spared."""
    unit_defs = defs[0]
    defenders = [_ns("d_1", "rohan_peasant")]

    result = _resolve(unit_defs, _pair(), defenders, defender_hits=1)

    assert _duplicates(result.attacker_casualties) == []
    assert result.attacker_casualties == ["a_bk"]
    assert result.surviving_attacker_ids == ["a_bomb"]


def test_bomb_killed_by_hits_spares_unpaired_bombikazi(defs):
    """Bomb destroyed this round: the surviving bombikazi no longer detonates."""
    unit_defs = defs[0]
    defenders = [_ns("d_1", "rohan_peasant")]

    # must_conquer diverts hits from the conquering infantry to the siegework bomb
    result = _resolve(
        unit_defs, _pair(), defenders, defender_hits=1, must_conquer=True
    )

    assert _duplicates(result.attacker_casualties) == []
    assert result.attacker_casualties == ["a_bomb"]
    assert result.surviving_attacker_ids == ["a_bk"]


def test_pair_member_killed_by_hits_does_not_duplicate_casualties(defs):
    """Both killed by defender hits: each id appears exactly once, nothing appended."""
    unit_defs = defs[0]
    defenders = [_ns("d_1", "rohan_peasant")]

    result = _resolve(unit_defs, _pair(), defenders, defender_hits=2)

    assert _duplicates(result.attacker_casualties) == []
    assert sorted(result.attacker_casualties) == ["a_bk", "a_bomb"]
    assert result.surviving_attacker_ids == []


def test_pair_detonates_when_both_survive_the_round(defs):
    """No defender damage: both paired units self-destruct, each listed exactly once."""
    unit_defs = defs[0]
    defenders = [_ns("d_1", "rohan_peasant")]

    result = _resolve(unit_defs, _pair(), defenders, defender_hits=0)

    assert _duplicates(result.attacker_casualties) == []
    assert sorted(result.attacker_casualties) == ["a_bk", "a_bomb"]
    assert result.surviving_attacker_ids == []


def test_detonated_unit_is_not_also_wounded(defs):
    """A ring-shield absorb leaves the pair member wounded, then it detonates: no overlap."""
    unit_defs = defs[0]
    defenders = [_ns("d_1", "rohan_peasant")]

    result = _resolve(
        unit_defs, _pair(), defenders, defender_hits=1, hp_shield={"a_bk": 1}
    )

    assert _duplicates(result.attacker_casualties) == []
    assert set(result.attacker_casualties) & set(result.attacker_wounded) == set()
    assert "a_bk" not in result.attacker_wounded
    assert sorted(result.attacker_casualties) == ["a_bk", "a_bomb"]


def test_archer_prefire_killed_bombikazi_does_not_roll_or_detonate(defs):
    """Pre-round death: the bombikazi is gone from the roster before round 1 dice."""
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = defs
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=setup,
        camp_defs=camp_defs,
    )
    t = state.territories["gap_of_rohan"]
    t.units.clear()
    t.owner = "rohan"
    archer = _make_unit(state, "rohan", "gondor_archer", unit_defs)
    berserker = _make_unit(state, "isengard", "berserker", unit_defs)
    bomb = _make_unit(state, "isengard", "bomb", unit_defs)
    berserker_id = berserker.instance_id
    t.units.extend([archer, berserker, bomb])
    state.current_faction = "isengard"
    state.phase = "combat"

    # archer pre-fire: defense 2 with prefire penalty needs roll 1 to hit;
    # fuse_bomb=False so the bomb skips the dedicated siegeworks round
    action = initiate_combat(
        "isengard", "gap_of_rohan", {"attacker": [], "defender": [1]},
        fuse_bomb=False,
    )
    state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )

    assert state.active_combat is not None
    assert berserker_id not in state.active_combat.attacker_instance_ids
    destroyed = [e for e in events if e.type == "unit_destroyed"]
    assert [e.payload["unit_id"] for e in destroyed] == [berserker_id]
    prefire_rounds = [
        r for r in state.active_combat.combat_log
        if getattr(r, "is_archer_prefire", False)
    ]
    assert prefire_rounds and berserker_id in prefire_rounds[0].attacker_casualties

    # round-1 dice exactly as the API generates them: the dead bombikazi must not roll
    from backend.api.main import _generate_dice_rolls_for_active_combat

    dice = _generate_dice_rolls_for_active_combat(state, unit_defs, territory_defs, faction_defs)
    assert dice["attacker"] == []

    action2 = continue_combat("isengard", dice)
    state2, events2 = apply_action(
        state, action2, unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )

    destroyed2 = [e for e in events2 if e.type == "unit_destroyed"]
    assert [e.payload["unit_id"] for e in destroyed2] == []
    round1 = state2.active_combat.combat_log[-1]
    assert berserker_id not in round1.attacker_casualties
    assert _duplicates(round1.attacker_casualties) == []
