"""Hero uniqueness: one in play per hero_id (map, cargo, purchased pool, pending mobilize)."""

import pytest

from backend.engine.actions import purchase_units
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.queries import (
    count_hero_family_instances,
    count_unit_instances,
    get_purchasable_units,
    validate_action,
)
from backend.engine.reducer import apply_action
from backend.engine.state import PendingMobilization, Unit, UnitStack
from backend.engine.utils import initialize_game_state


HERO_ID = "captain_of_gondor"
VARIANT_ID = "captain_of_gondor_mounted"
FAMILY = "captain"
FACTION = "gondor"


def _mark_hero(ud, unit_id: str = HERO_ID, hero_id: str | None = None) -> None:
    ud[unit_id].hero_id = hero_id if hero_id is not None else unit_id


def _add_hero_variant(ud, hero_id: str = FAMILY) -> None:
    base = ud[HERO_ID]
    ud[VARIANT_ID] = UnitDefinition(
        id=VARIANT_ID,
        display_name="Captain of Gondor (mounted)",
        faction=FACTION,
        archetype="cavalry",
        tags=list(base.tags or []),
        attack=base.attack,
        defense=base.defense,
        movement=int(base.movement) + 1,
        health=base.health,
        cost=dict(base.cost or {"power": 1}),
        dice=getattr(base, "dice", 1),
        purchasable=True,
        hero_id=hero_id,
    )
    ud[HERO_ID].hero_id = hero_id


def _load():
    ud, td, fd, cd, port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    setup = load_starting_setup(setup_id="wotr_exp_1.0")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
    )
    _mark_hero(ud)
    state.phase = "purchase"
    state.current_faction = FACTION
    state.faction_resources.setdefault(FACTION, {})["power"] = 80
    return state, ud, td, fd, cd, port_d


def _make_unit(state, unit_id: str, unit_defs, loaded_onto=None) -> Unit:
    udef = unit_defs[unit_id]
    return Unit(
        instance_id=state.generate_unit_instance_id(FACTION, unit_id),
        unit_id=unit_id,
        remaining_movement=udef.movement,
        remaining_health=udef.health,
        base_movement=udef.movement,
        base_health=udef.health,
        loaded_onto=loaded_onto,
    )


def _gondor_land_tid(state, td) -> str:
    for tid, terr in state.territories.items():
        if terr.owner != FACTION:
            continue
        tdef = td.get(tid)
        if tdef and getattr(tdef, "terrain_type", "").lower() != "sea":
            return tid
    raise AssertionError("no Gondor land territory")


def _sea_tid(state, td) -> str:
    for tid, tdef in td.items():
        if getattr(tdef, "terrain_type", "").lower() == "sea" and tid in state.territories:
            return tid
    raise AssertionError("no sea zone")


def test_count_includes_map_cargo_purchased_and_pending_mobilize():
    state, ud, td, fd, cd, port_d = _load()
    assert count_unit_instances(state, HERO_ID) == 0

    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))
    assert count_unit_instances(state, HERO_ID) == 1

    sea = _sea_tid(state, td)
    boat = _make_unit(state, "gondor_ship", ud)
    cargo = _make_unit(state, HERO_ID, ud, loaded_onto=boat.instance_id)
    state.territories[sea].units.extend([boat, cargo])
    assert count_unit_instances(state, HERO_ID) == 2

    state.faction_purchased_units[FACTION] = [UnitStack(unit_id=HERO_ID, count=1)]
    assert count_unit_instances(state, HERO_ID) == 3

    state.pending_mobilizations = [
        PendingMobilization(destination=land, units=[{"unit_id": HERO_ID, "count": 1}])
    ]
    assert count_unit_instances(state, HERO_ID) == 4


def test_purchase_rejects_count_two_and_second_copy():
    state, ud, td, fd, cd, port_d = _load()
    two = purchase_units(FACTION, {HERO_ID: 2})
    v = validate_action(state, two, ud, td, fd, cd, port_d)
    assert not v.valid
    assert v.error and "unique" in v.error.lower()

    with pytest.raises(ValueError, match="unique"):
        apply_action(state, two, ud, td, fd, cd, port_d)

    one = purchase_units(FACTION, {HERO_ID: 1})
    v = validate_action(state, one, ud, td, fd, cd, port_d)
    assert v.valid, v.error
    state, _ = apply_action(state, one, ud, td, fd, cd, port_d)
    assert count_unit_instances(state, HERO_ID) == 1

    again = purchase_units(FACTION, {HERO_ID: 1})
    v = validate_action(state, again, ud, td, fd, cd, port_d)
    assert not v.valid
    assert v.error and "already in play" in v.error.lower()


def test_purchase_blocked_when_on_map_cargo_or_pending_mobilize():
    state, ud, td, fd, cd, port_d = _load()
    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))
    action = purchase_units(FACTION, {HERO_ID: 1})
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert not v.valid

    state, ud, td, fd, cd, port_d = _load()
    sea = _sea_tid(state, td)
    boat = _make_unit(state, "gondor_ship", ud)
    cargo = _make_unit(state, HERO_ID, ud, loaded_onto=boat.instance_id)
    state.territories[sea].units.extend([boat, cargo])
    v = validate_action(state, purchase_units(FACTION, {HERO_ID: 1}), ud, td, fd, cd, port_d)
    assert not v.valid

    state, ud, td, fd, cd, port_d = _load()
    state.pending_mobilizations = [
        PendingMobilization(destination=land, units=[{"unit_id": HERO_ID, "count": 1}])
    ]
    v = validate_action(state, purchase_units(FACTION, {HERO_ID: 1}), ud, td, fd, cd, port_d)
    assert not v.valid


def test_repurchase_allowed_after_removed_from_play():
    state, ud, td, fd, cd, port_d = _load()
    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))
    state.territories[land].units = [
        u for u in state.territories[land].units if u.unit_id != HERO_ID
    ]
    v = validate_action(state, purchase_units(FACTION, {HERO_ID: 1}), ud, td, fd, cd, port_d)
    assert v.valid, v.error


def test_get_purchasable_units_clamps_unique_max_affordable():
    state, ud, td, fd, cd, port_d = _load()
    row = next(p for p in get_purchasable_units(state, FACTION, ud) if p["unit_id"] == HERO_ID)
    assert row["unique"] is True
    assert row["hero_id"] == HERO_ID
    assert row["max_affordable"] == 1

    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))
    row = next(p for p in get_purchasable_units(state, FACTION, ud) if p["unit_id"] == HERO_ID)
    assert row["max_affordable"] == 0
    assert row["unique"] is True


def test_shared_hero_id_caps_all_versions():
    state, ud, td, fd, cd, port_d = _load()
    _add_hero_variant(ud)
    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))
    assert count_hero_family_instances(state, FAMILY, ud) == 1

    v = validate_action(state, purchase_units(FACTION, {VARIANT_ID: 1}), ud, td, fd, cd, port_d)
    assert not v.valid
    assert v.error and "already in play" in v.error.lower()

    row = next(p for p in get_purchasable_units(state, FACTION, ud) if p["unit_id"] == VARIANT_ID)
    assert row["hero_id"] == FAMILY
    assert row["max_affordable"] == 0

    state, ud, td, fd, cd, port_d = _load()
    _add_hero_variant(ud)
    v = validate_action(
        state,
        purchase_units(FACTION, {HERO_ID: 1, VARIANT_ID: 1}),
        ud,
        td,
        fd,
        cd,
        port_d,
    )
    assert not v.valid
    assert v.error and "unique" in v.error.lower()


def test_ai_does_not_buy_unique_already_in_play():
    state, ud, td, fd, cd, port_d = _load()
    for uid, udef in ud.items():
        if udef.faction == FACTION and uid != HERO_ID:
            udef.purchasable = False
    land = _gondor_land_tid(state, td)
    state.territories[land].units.append(_make_unit(state, HERO_ID, ud))

    action = _decide_purchase(state, ud, td, fd, cd, port_d)
    assert action is not None
    if action.type == "purchase_units":
        assert action.payload.get("purchases", {}).get(HERO_ID, 0) == 0
    else:
        assert action.type == "end_phase"


def test_ai_buys_at_most_one_unique_in_a_batch():
    state, ud, td, fd, cd, port_d = _load()
    for uid, udef in ud.items():
        if udef.faction == FACTION and uid != HERO_ID:
            udef.purchasable = False
    action = _decide_purchase(state, ud, td, fd, cd, port_d)
    assert action is not None
    assert action.type == "purchase_units"
    assert action.payload.get("purchases", {}).get(HERO_ID, 0) == 1


def test_ai_buys_at_most_one_across_hero_id_variants():
    state, ud, td, fd, cd, port_d = _load()
    _add_hero_variant(ud)
    for uid, udef in ud.items():
        if udef.faction == FACTION and uid not in (HERO_ID, VARIANT_ID):
            udef.purchasable = False
    action = _decide_purchase(state, ud, td, fd, cd, port_d)
    assert action is not None
    assert action.type == "purchase_units"
    buys = action.payload.get("purchases", {})
    assert buys.get(HERO_ID, 0) + buys.get(VARIANT_ID, 0) == 1


def _decide_purchase(state, ud, td, fd, cd, port_d):
    from backend.ai.context import AIContext
    from backend.ai.purchase import decide_purchase
    from backend.engine.queries import get_mobilization_capacity

    purchasable = get_purchasable_units(state, FACTION, ud)
    capacity = get_mobilization_capacity(state, FACTION, td, cd, port_d, ud)
    land_cap = sum(t.get("power", 0) for t in capacity.get("territories", [])) + sum(
        1 for t in capacity.get("territories", []) if t.get("home_unit_capacity")
    )
    sea_cap = sum(z.get("power", 0) for z in capacity.get("sea_zones", []))
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={
            "phase": "purchase",
            "faction": FACTION,
            "purchasable_units": purchasable,
            "mobilization_capacity": land_cap + sea_cap,
            "mobilization_land_capacity": land_cap,
            "purchased_units_count": 0,
        },
    )
    return decide_purchase(ctx)


def test_heroes_disabled_hides_unique_from_purchase_and_starting_setup():
    from copy import deepcopy

    from backend.engine.definitions import load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id="wotr_exp_1.0")
    setup = deepcopy(load_starting_setup(setup_id="wotr_exp_1.0"))
    ud[HERO_ID].unique = False
    _mark_hero(ud)
    land = "minas_tirith"
    setup.setdefault("starting_units", {}).setdefault(land, []).append(
        {"unit_id": HERO_ID, "count": 1}
    )

    on = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
        heroes_enabled=True,
    )
    assert on.heroes_enabled is True
    assert count_unit_instances(on, HERO_ID) == 1
    assert any(p["unit_id"] == HERO_ID for p in get_purchasable_units(on, FACTION, ud))

    off = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria={"strongholds": {"good": 4, "evil": 4}},
        heroes_enabled=False,
    )
    assert off.heroes_enabled is False
    assert count_unit_instances(off, HERO_ID) == 0
    assert all(p["unit_id"] != HERO_ID for p in get_purchasable_units(off, FACTION, ud))

    off.phase = "purchase"
    off.current_faction = FACTION
    off.faction_resources.setdefault(FACTION, {})["power"] = 80
    v = validate_action(off, purchase_units(FACTION, {HERO_ID: 1}), ud, td, fd, cd, port_d)
    assert not v.valid
    assert v.error and "disabled" in v.error.lower()

    roundtrip = off.to_dict()
    assert roundtrip.get("heroes_enabled") is False
    restored = type(off).from_dict(roundtrip)
    assert restored.heroes_enabled is False
    missing = {k: val for k, val in roundtrip.items() if k != "heroes_enabled"}
    assert type(off).from_dict(missing).heroes_enabled is True


def test_missing_hero_id_is_not_a_hero():
    state, ud, td, fd, cd, port_d = _load()
    ud[HERO_ID].hero_id = None
    ud[HERO_ID].tags = list(ud[HERO_ID].tags or []) + ["hero"]
    ud[HERO_ID].unique = True
    state.heroes_enabled = False
    assert any(p["unit_id"] == HERO_ID for p in get_purchasable_units(state, FACTION, ud))
    v = validate_action(state, purchase_units(FACTION, {HERO_ID: 1}), ud, td, fd, cd, port_d)
    assert v.valid, v.error
