"""Rings of Power is a manifest rule, not a scenario-id check."""

from types import SimpleNamespace

from backend.engine.definitions import FactionDefinition, TerritoryDefinition
from backend.engine.rings import (
    apply_carried_ring,
    expand_rolls_for_bonus,
    mode_declared,
    on_bearer_destroyed,
    power_for_faction,
    ring_specs,
    spawn_rings,
    sync_ring_movement,
    validate_ring_carry,
)
from backend.engine.state import GameState, PendingMove, TerritoryState, Unit
from backend.engine.utils import initialize_game_state


RULES = [{
    "type": "rings_of_power",
    "rings": [
        {"id": "narya", "name": "Narya", "territory_id": "mithlond", "power": 3},
        {"id": "the_nine", "name": "The Nine", "territory_id": "ost_in_edhil", "power": 4},
    ],
}]


def _faction():
    return {
        "noldor": FactionDefinition(
            id="noldor",
            display_name="Noldor",
            alliance="good",
            capital="mithlond",
            color="#4a7c9b",
        )
    }


def _territories():
    def terr(tid, power):
        return TerritoryDefinition(
            id=tid,
            display_name=tid,
            terrain_type="plains",
            adjacent=[],
            produces={"power": power},
            is_stronghold=True,
        )
    return {"mithlond": terr("mithlond", 10), "ost_in_edhil": terr("ost_in_edhil", 10)}


def test_catalog_keeps_each_rings_own_power_and_clumps_as_one_id():
    specs = ring_specs(RULES)
    assert [(row["id"], row["power"]) for row in specs] == [("narya", 3), ("the_nine", 4)]
    assert mode_declared(RULES)
    assert mode_declared([]) is False


def test_mode_spawns_only_when_enabled_and_heroes_are_on():
    on = initialize_game_state(
        _faction(),
        _territories(),
        special_rules=RULES,
        rings_of_power=True,
        heroes_enabled=True,
    )
    assert on.rings_of_power is True
    assert [ring.id for ring in on.rings] == ["narya", "the_nine"]
    off = initialize_game_state(
        _faction(),
        _territories(),
        special_rules=RULES,
        rings_of_power=False,
        heroes_enabled=True,
    )
    assert off.rings == []
    no_heroes = initialize_game_state(
        _faction(),
        _territories(),
        special_rules=RULES,
        rings_of_power=True,
        heroes_enabled=False,
    )
    assert no_heroes.rings_of_power is False
    assert no_heroes.rings == []


def _carry_state():
    hero = Unit(
        instance_id="noldor_gil_galad_001",
        unit_id="gil_galad",
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )
    spear = Unit(
        instance_id="noldor_warrior_001",
        unit_id="warrior",
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )
    state = GameState(
        turn_number=1,
        current_faction="noldor",
        phase="non_combat_move",
        territories={
            "mithlond": TerritoryState(owner="noldor", original_owner="noldor", units=[hero]),
            "ost_in_edhil": TerritoryState(owner="noldor", original_owner="noldor", units=[spear]),
        },
        faction_resources={"noldor": {"power": 0}},
        rings_of_power=True,
        rings=spawn_rings(RULES),
    )
    defs = {
        "gil_galad": SimpleNamespace(hero_id="gil_galad"),
        "warrior": SimpleNamespace(hero_id=None),
    }
    return state, defs, hero, spear


def test_only_a_hero_whose_move_reaches_the_ring_can_carry_it():
    state, defs, hero, spear = _carry_state()
    assert validate_ring_carry(state, defs, [spear], "ost_in_edhil", [], "the_nine") == (
        "A ring moves only with a single hero"
    )
    assert validate_ring_carry(state, defs, [hero], "mithlond", [], "the_nine") == (
        "A ring leaves only with a hero moving out of its territory"
    )
    assert validate_ring_carry(state, defs, [hero], "mithlond", ["ost_in_edhil"], "the_nine") == (
        "A ring leaves only with a hero moving out of its territory"
    )
    assert validate_ring_carry(state, defs, [hero], "mithlond", [], "narya") is None


def test_applied_move_leaves_the_ring_in_the_heros_destination():
    state, _, _, _ = _carry_state()
    move = PendingMove(
        from_territory="mithlond",
        to_territory="lune",
        unit_instance_ids=["noldor_gil_galad_001"],
        phase="non_combat_move",
        ring_id="narya",
    )
    apply_carried_ring(state, move, "lune")
    narya = next(ring for ring in state.rings if ring.id == "narya")
    assert narya.territory_id == "lune"
    assert narya.bearer_instance_id is None
    assert power_for_faction(state, "noldor") == 4


def test_ring_power_uses_the_value_on_that_ring():
    state, _, _, _ = _carry_state()
    assert power_for_faction(state, "noldor") == 7


def test_required_bearer_death_sends_the_ring_home_and_others_stay():
    state, defs, hero, _ = _carry_state()
    one = spawn_rings([{
        "type": "rings_of_power",
        "rings": [{
            "id": "the_one",
            "name": "The One",
            "territory_id": "mithlond",
            "power": 4,
            "bearer_hero_id": "sauron",
            "returns_to": "ost_in_edhil",
            "attack_boost": 1,
            "defense_boost": 1,
        }],
    }])[0]
    state.rings.append(one)
    state.territories["mithlond"].units.append(Unit(
        instance_id="sauron_001",
        unit_id="sauron",
        remaining_movement=1,
        remaining_health=2,
        base_movement=1,
        base_health=2,
    ))
    defs["sauron"] = SimpleNamespace(hero_id="sauron", faction="mordor", cost={"power": 18}, movement=1)
    defs["gil_galad"] = SimpleNamespace(hero_id="gil_galad", faction="noldor", cost={"power": 12}, movement=1)
    assert validate_ring_carry(state, defs, [hero], "mithlond", [], "the_one") is None
    sauron = state.territories["mithlond"].units[-1]
    assert validate_ring_carry(state, defs, [sauron], "mithlond", [], "the_one") is None
    assert power_for_faction(state, "mordor", defs) == 4
    assert power_for_faction(state, "noldor", defs) == 7
    on_bearer_destroyed(state, sauron, "mithlond", defs)
    assert one.territory_id == "ost_in_edhil"
    assert one.bearer_instance_id is None
    narya = next(ring for ring in state.rings if ring.id == "narya")
    assert narya.territory_id == "mithlond"
    on_bearer_destroyed(state, hero, "mithlond", defs)
    assert narya.territory_id == "mithlond"


def test_bonus_dice_use_the_same_ten_sided_die():
    hero = Unit(
        instance_id="h1",
        unit_id="gil_galad",
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )
    defs = {"gil_galad": SimpleNamespace(dice=1)}
    seen: list[int] = []

    def _roll(_low: int, high: int) -> int:
        seen.append(high)
        return high

    import backend.engine.rings as rings_mod
    original = rings_mod.random.randint
    rings_mod.random.randint = _roll
    try:
        rolls = expand_rolls_for_bonus([4], [hero], defs, {"h1": 2})
    finally:
        rings_mod.random.randint = original
    assert rolls == [4, 10, 10]
    assert seen == [10, 10]


def test_moves_boost_adds_movement_while_the_ring_is_on_the_hero():
    state, defs, hero, _ = _carry_state()
    defs["gil_galad"] = SimpleNamespace(hero_id="gil_galad", faction="noldor", cost={"power": 12}, movement=1)
    narya = next(ring for ring in state.rings if ring.id == "narya")
    narya.moves_boost = 1
    sync_ring_movement(state, defs)
    assert hero.base_movement == 2
    assert hero.remaining_movement == 2
    sync_ring_movement(state, defs)
    assert hero.remaining_movement == 2
    narya.territory_id = "ost_in_edhil"
    sync_ring_movement(state, defs)
    assert hero.base_movement == 1
    assert hero.remaining_movement == 1
