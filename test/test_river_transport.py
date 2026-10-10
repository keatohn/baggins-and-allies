"""River zones use the ship transport path with a separate hull and a shared bank power pool."""

from backend.engine.actions import initiate_combat, mobilize_units, move_units, purchase_units
from backend.engine.definitions import (
    CampDefinition,
    FactionDefinition,
    PortDefinition,
    TerritoryDefinition,
    UnitDefinition,
)
from backend.engine.movement import (
    get_reachable_territories_for_unit,
    water_transport_relation,
)
from backend.engine.queries import (
    get_forced_naval_combat_instance_ids,
    get_movable_units,
    get_unit_move_targets,
    limit_embark_destinations_to_capacity,
    river_mobilization_capacity_total,
    validate_action,
)
from backend.engine.reducer import _apply_pending_mobilizations, apply_action
from backend.engine.state import GameState, PendingMobilization, TerritoryState, Unit, UnitStack
from backend.signals import classify_territory_for_signal


def _territory(tid: str, terrain: str, adjacent: list[str], power: int = 0, ownable: bool = True) -> TerritoryDefinition:
    return TerritoryDefinition(
        id=tid,
        display_name=tid,
        terrain_type=terrain,
        adjacent=adjacent,
        produces={"power": power},
        ownable=ownable,
    )


def _unit_def(uid: str, faction: str, archetype: str, *, tags: list[str] | None = None, transport: int = 0) -> UnitDefinition:
    return UnitDefinition(
        id=uid,
        display_name=uid,
        faction=faction,
        archetype=archetype,
        tags=list(tags or []),
        attack=1,
        defense=1,
        movement=2,
        health=1,
        cost={"power": 1},
        transport_capacity=transport,
    )


def _piece(iid: str, uid: str) -> Unit:
    return Unit(
        instance_id=iid,
        unit_id=uid,
        remaining_movement=2,
        remaining_health=1,
        base_movement=2,
        base_health=1,
    )


def _faction(fid: str, alliance: str, capital: str) -> FactionDefinition:
    return FactionDefinition(id=fid, display_name=fid, alliance=alliance, capital=capital, color="#000")


def _board():
    territory_defs = {
        "minas": _territory("minas", "city", ["bank"], power=3),
        "bank": _territory("bank", "plains", ["minas", "river_anduin", "bank_b"], power=2),
        "bank_b": _territory("bank_b", "plains", ["bank", "river_anduin"], power=1),
        "far_bank": _territory("far_bank", "plains", ["river_upper"], power=1),
        "ally_bank": _territory("ally_bank", "plains", ["river_ally"], power=2),
        "captured": _territory("captured", "plains", ["river_side"], power=2),
        "coast": _territory("coast", "plains", ["sea_bay"], power=1),
        "rohan_cap": _territory("rohan_cap", "city", [], power=1),
        "mordor_cap": _territory("mordor_cap", "city", [], power=1),
        "river_anduin": _territory(
            "river_anduin",
            "river",
            ["bank", "bank_b", "river_upper", "sea_bay"],
            ownable=False,
        ),
        "river_upper": _territory("river_upper", "river", ["river_anduin", "far_bank"], ownable=False),
        "river_ally": _territory("river_ally", "river", ["ally_bank"], ownable=False),
        "river_side": _territory("river_side", "river", ["captured"], ownable=False),
        "sea_bay": _territory("sea_bay", "sea", ["river_anduin", "coast"], ownable=False),
    }
    unit_defs = {
        "infantry": _unit_def("infantry", "gondor", "infantry", tags=["transportable"]),
        "ship": _unit_def("ship", "gondor", "naval", transport=2),
        "boat": _unit_def("boat", "gondor", "river", transport=2),
        "eagle": _unit_def("eagle", "gondor", "aerial"),
        "ally_boat": _unit_def("ally_boat", "rohan", "river", transport=2),
        "enemy_boat": _unit_def("enemy_boat", "mordor", "river", transport=1),
    }
    faction_defs = {
        "gondor": _faction("gondor", "good", "minas"),
        "rohan": _faction("rohan", "good", "rohan_cap"),
        "mordor": _faction("mordor", "evil", "mordor_cap"),
    }
    camp_defs = {"camp_bank": CampDefinition(id="camp_bank", territory_id="bank")}
    port_defs = {"port_coast": PortDefinition(id="port_coast", territory_id="coast")}
    owners = {
        "minas": "gondor",
        "bank": "gondor",
        "bank_b": "gondor",
        "far_bank": "gondor",
        "coast": "gondor",
        "ally_bank": "rohan",
        "captured": "gondor",
        "rohan_cap": "rohan",
        "mordor_cap": "mordor",
    }
    territories = {
        tid: TerritoryState(owner=owners.get(tid), original_owner=owners.get(tid), units=[])
        for tid in territory_defs
    }
    state = GameState(
        turn_number=1,
        current_faction="gondor",
        phase="non_combat_move",
        territories=territories,
        faction_resources={"gondor": {"power": 100}, "rohan": {"power": 10}, "mordor": {"power": 10}},
        camps_standing=["camp_bank"],
        faction_territories_at_turn_start={
            "gondor": ["minas", "bank", "bank_b", "far_bank", "coast"],
            "rohan": ["ally_bank", "rohan_cap"],
            "mordor": ["mordor_cap"],
        },
        turn_order=["gondor", "rohan", "mordor"],
    )
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _reach(state, unit, start, unit_defs, territory_defs, faction_defs):
    reachable, _routes = get_reachable_territories_for_unit(
        unit, start, state, unit_defs, territory_defs, faction_defs, state.phase
    )
    return set(reachable)


def test_hulls_stay_in_their_water_and_land_only_loads():
    state, unit_defs, territory_defs, faction_defs, _camps, _ports = _board()
    infantry = _piece("inf_1", "infantry")
    ship = _piece("ship_1", "ship")
    boat = _piece("boat_1", "boat")
    eagle = _piece("eagle_1", "eagle")
    state.territories["bank"].units.append(infantry)
    state.territories["sea_bay"].units.append(ship)
    state.territories["bank"].units.append(eagle)

    assert "river_anduin" not in _reach(state, infantry, "bank", unit_defs, territory_defs, faction_defs)
    assert "far_bank" not in _reach(state, infantry, "bank", unit_defs, territory_defs, faction_defs)
    assert "river_anduin" not in _reach(state, ship, "sea_bay", unit_defs, territory_defs, faction_defs)
    assert "river_anduin" in _reach(state, eagle, "bank", unit_defs, territory_defs, faction_defs)

    state.territories["river_anduin"].units.append(boat)
    boat_reach = _reach(state, boat, "river_anduin", unit_defs, territory_defs, faction_defs)
    assert "river_upper" in boat_reach
    assert "sea_bay" not in boat_reach
    assert "bank" not in boat_reach
    assert "river_anduin" in _reach(state, infantry, "bank", unit_defs, territory_defs, faction_defs)


def test_ally_boat_does_not_carry_your_units():
    state, unit_defs, territory_defs, faction_defs, _camps, _ports = _board()
    infantry = _piece("inf_1", "infantry")
    ally = _piece("ally_1", "ally_boat")
    state.territories["bank"].units.append(infantry)
    state.territories["river_anduin"].units.append(ally)
    assert "river_anduin" not in _reach(state, infantry, "bank", unit_defs, territory_defs, faction_defs)


def test_cross_domain_move_is_rejected():
    state, unit_defs, territory_defs, faction_defs, camps, ports = _board()
    boat = _piece("boat_1", "boat")
    ship = _piece("ship_1", "ship")
    state.territories["river_anduin"].units.append(boat)
    state.territories["sea_bay"].units.append(ship)
    assert water_transport_relation(territory_defs["river_anduin"], territory_defs["sea_bay"]) == "cross"
    boat_move = validate_action(
        state,
        move_units("gondor", "river_anduin", "sea_bay", ["boat_1"]),
        unit_defs,
        territory_defs,
        faction_defs,
        camps,
        ports,
    )
    ship_move = validate_action(
        state,
        move_units("gondor", "sea_bay", "river_anduin", ["ship_1"]),
        unit_defs,
        territory_defs,
        faction_defs,
        camps,
        ports,
    )
    assert not boat_move.valid
    assert "river" in (boat_move.error or "")
    assert not ship_move.valid


def test_river_mobilization_uses_turn_start_banks_and_shared_power():
    state, unit_defs, territory_defs, faction_defs, camps, ports = _board()
    state.phase = "mobilization"
    state.faction_purchased_units["gondor"] = [
        UnitStack(unit_id="boat", count=6),
        UnitStack(unit_id="infantry", count=4),
    ]
    assert river_mobilization_capacity_total(state, "gondor", territory_defs) == 4

    def mobilize(dest: str, unit_id: str, count: int):
        return validate_action(
            state,
            mobilize_units("gondor", dest, [{"unit_id": unit_id, "count": count}]),
            unit_defs,
            territory_defs,
            faction_defs,
            camps,
            ports,
        )

    assert not mobilize("river_ally", "boat", 1).valid
    assert not mobilize("river_side", "boat", 1).valid
    assert not mobilize("sea_bay", "boat", 1).valid
    assert mobilize("river_anduin", "boat", 3).valid
    assert not mobilize("river_anduin", "boat", 4).valid

    state.pending_mobilizations.append(
        PendingMobilization(destination="bank", units=[{"unit_id": "infantry", "count": 2}])
    )
    one_left = mobilize("river_anduin", "boat", 1)
    two_left = mobilize("river_anduin", "boat", 2)
    assert one_left.valid
    assert not two_left.valid


def test_river_purchases_do_not_spend_land_or_sea_capacity():
    state, unit_defs, territory_defs, faction_defs, camps, ports = _board()
    state.phase = "purchase"

    def buy(purchases: dict[str, int]):
        return validate_action(
            state,
            purchase_units("gondor", purchases),
            unit_defs,
            territory_defs,
            faction_defs,
            camps,
            ports,
        )

    assert buy({"boat": 4, "infantry": 2, "ship": 1}).valid
    assert not buy({"boat": 5}).valid
    assert not buy({"ship": 2}).valid
    assert not buy({"infantry": 3}).valid


def test_mobilizing_onto_hostile_river_forces_the_defender_to_fight_or_leave():
    state, unit_defs, territory_defs, faction_defs, _camps, _ports = _board()
    defender = _piece("enemy_1", "enemy_boat")
    state.territories["river_anduin"].units.append(defender)
    state.pending_mobilizations.append(
        PendingMobilization(destination="river_anduin", units=[{"unit_id": "boat", "count": 1}])
    )
    _apply_pending_mobilizations(state, unit_defs, territory_defs, faction_defs)
    placed = [u for u in state.territories["river_anduin"].units if u.unit_id == "boat"]
    assert len(placed) == 1
    assert placed[0].instance_id in state.naval_mobilization_intruder_instance_ids
    forced = get_forced_naval_combat_instance_ids(
        state, "mordor", unit_defs, territory_defs, faction_defs
    )
    assert forced == ["enemy_1"]


def test_river_raid_initiate_accepts_river_zone():
    """A raid staged from a river uses the same initiate path as a sea raid."""
    state, unit_defs, territory_defs, faction_defs, camps, ports = _board()
    unit_defs = dict(unit_defs)
    unit_defs["orc"] = _unit_def("orc", "mordor", "infantry")
    boat = _piece("boat_1", "boat")
    passenger = _piece("inf_1", "infantry")
    passenger.loaded_onto = boat.instance_id
    defender = _piece("orc_1", "orc")
    state.territories["river_upper"].units.extend([boat, passenger])
    state.territories["far_bank"].units.append(defender)
    state.territories["far_bank"].owner = "mordor"
    state.current_faction = "gondor"
    state.phase = "combat"

    state, _events = apply_action(
        state,
        initiate_combat(
            "gondor",
            "far_bank",
            dice_rolls={"attacker": [1], "defender": [6]},
            sea_zone_id="river_upper",
        ),
        unit_defs,
        territory_defs,
        faction_defs,
        camps,
        ports,
    )

    assert state.active_combat is None
    assert any(u.instance_id == "inf_1" for u in state.territories["far_bank"].units)
    assert any(u.instance_id == "boat_1" for u in state.territories["river_upper"].units)
    assert all(u.instance_id != "boat_1" for u in state.territories["far_bank"].units)


def test_embark_destinations_stop_at_rowboat_capacity():
    """Seven capacity-1 rowboats can take 7 of 12 warriors, not the whole stack."""
    state, unit_defs, territory_defs, faction_defs, camps, ports = _board()
    unit_defs = dict(unit_defs)
    unit_defs["skiff"] = _unit_def("skiff", "gondor", "river", transport=1)
    state.phase = "non_combat_move"
    state.current_faction = "gondor"
    for i in range(7):
        state.territories["river_anduin"].units.append(_piece(f"skiff_{i}", "skiff"))
    infantry_ids = [f"inf_{i}" for i in range(12)]
    for iid in infantry_ids:
        state.territories["bank"].units.append(_piece(iid, "infantry"))

    rows = []
    for info in get_movable_units(state, "gondor", unit_defs, faction_defs):
        targets, routes = get_unit_move_targets(
            state, info["instance_id"], unit_defs, territory_defs, faction_defs,
        )
        rows.append({
            "territory": info["territory_id"],
            "unit": info,
            "destinations": targets,
            "charge_routes": routes,
        })
    offered_before = sum(
        1 for row in rows
        if row["unit"]["unit_id"] == "infantry" and "river_anduin" in row["destinations"]
    )
    assert offered_before == 12
    limit_embark_destinations_to_capacity(
        rows, state, "gondor", unit_defs, territory_defs, faction_defs, state.phase,
    )
    offered_after = sum(
        1 for row in rows
        if row["unit"]["unit_id"] == "infantry" and "river_anduin" in row["destinations"]
    )
    assert offered_after == 7

    def load(ids: list[str]):
        return validate_action(
            state,
            move_units("gondor", "bank", "river_anduin", ids, move_type="load"),
            unit_defs,
            territory_defs,
            faction_defs,
            camps,
            ports,
        )

    assert load(infantry_ids[:7]).valid
    too_many = load(infantry_ids[:8])
    assert not too_many.valid
    assert "transport capacity" in (too_many.error or "")


def test_river_is_not_a_signal_target():
    assert classify_territory_for_signal(
        owner=None,
        territory_id="anduin",
        ownable=False,
        terrain_type="river",
        my_alliance="good",
        owner_alliance=None,
    ) is None
