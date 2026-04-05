"""
Tests for the AI player (purchase policy, combat, decide dispatch).
Uses engine read-only; validates that AI returns valid actions.
"""

import pytest
from backend.engine.state import GameState, Unit, UnitStack, TerritoryState, ActiveCombat, PendingMove
from backend.engine.definitions import load_static_definitions
from backend.engine.utils import initialize_game_state
from backend.engine.queries import validate_action, get_unit_faction
from backend.engine.movement import resolve_territory_key_in_state
from backend.engine.reducer import apply_action

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.purchase import decide_purchase
from backend.ai.mobilization import decide_mobilization
from backend.ai import combat_move as ai_combat_move


def _load_wotr_setup():
    """Load wotr_exp_1.0 so we have multiple factions and units."""
    from backend.engine.definitions import load_starting_setup
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
    return state, ud, td, fd, cd, port_d


def test_validate_purchase_rejects_when_capital_lost():
    """Purchase validation must match reducer: no purchase without capital."""
    from backend.engine.actions import purchase_units

    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "purchase"
    state.current_faction = "elves"
    cap = fd["elves"].capital
    state.territories[cap].owner = "mordor"
    action = purchase_units("elves", {"hobbit": 1})
    v = validate_action(state, action, ud, td, fd, cd, port_d)
    assert not v.valid
    assert v.error and "capital" in v.error.lower()


def test_ai_purchase_returns_end_phase_when_capital_lost():
    """AI must not emit purchase_units when the capital is gone (avoids 500 on apply)."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "purchase"
    state.current_faction = "elves"
    state.faction_resources.setdefault("elves", {})["power"] = 30
    cap = fd["elves"].capital
    state.territories[cap].owner = "mordor"
    from backend.engine.queries import get_purchasable_units, get_mobilization_capacity

    purchasable = get_purchasable_units(state, "elves", ud)
    capacity = get_mobilization_capacity(state, "elves", td, cd, port_d, ud)
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
            "faction": "elves",
            "purchasable_units": purchasable,
            "mobilization_capacity": land_cap + sea_cap,
            "mobilization_land_capacity": land_cap,
            "purchased_units_count": 0,
        },
    )
    action = decide_purchase(ctx)
    assert action is not None
    assert action.type == "end_phase"
    assert action.faction == "elves"


def test_ai_purchase_returns_action():
    """AI in purchase phase with power and capacity returns purchase_units or end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "purchase"
    state.current_faction = "elves"
    # Give elves some power
    state.faction_resources.setdefault("elves", {})["power"] = 20
    # Build minimal available_actions for purchase phase
    from backend.engine.queries import get_purchasable_units, get_mobilization_capacity
    purchasable = get_purchasable_units(state, "elves", ud)
    capacity = get_mobilization_capacity(state, "elves", td, cd, port_d, ud)
    land_cap = sum(t.get("power", 0) for t in capacity.get("territories", [])) + sum(
        1 for t in capacity.get("territories", []) if t.get("home_unit_capacity")
    )
    sea_cap = sum(z.get("power", 0) for z in capacity.get("sea_zones", []))
    available_actions = {
        "phase": "purchase",
        "faction": "elves",
        "purchasable_units": purchasable,
        "mobilization_capacity": land_cap + sea_cap,
        "mobilization_land_capacity": land_cap,
        "purchased_units_count": 0,
    }
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )
    action = decide_purchase(ctx)
    assert action is not None
    assert action.type in ("purchase_units", "end_phase")
    assert action.faction == "elves"
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_ai_decide_purchase_phase():
    """Full decide() in purchase phase returns valid action."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "purchase"
    state.current_faction = "gondor"
    state.faction_resources.setdefault("gondor", {})["power"] = 15
    from backend.engine.queries import get_mobilization_capacity
    capacity = get_mobilization_capacity(state, "gondor", td, cd, port_d, ud)
    territories = capacity.get("territories", [])
    land_cap = sum(t.get("power", 0) for t in territories) + sum(1 for t in territories if t.get("home_unit_capacity"))
    available_actions = {
        "phase": "purchase",
        "faction": "gondor",
        "mobilization_land_capacity": land_cap,
    }
    ctx = AIContext(state=state, unit_defs=ud, territory_defs=td, faction_defs=fd, camp_defs=cd, port_defs=port_d, available_actions=available_actions)
    action = decide(ctx)
    assert action is not None
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_ai_decide_combat_move_returns_move_or_end_phase():
    """In combat_move, AI returns move_units (into enemy) or end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "combat_move"
    state.current_faction = "mordor"
    available_actions = {"phase": "combat_move", "faction": "mordor"}
    ctx = AIContext(state=state, unit_defs=ud, territory_defs=td, faction_defs=fd, camp_defs=cd, port_defs=port_d, available_actions=available_actions)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_non_combat_attack_setup_values_empty_neutral_toward_enemy():
    """Neutrals must not get zero \"attack setup\" — otherwise big stacks never stage forward."""
    from backend.ai.non_combat_move import _attack_setup_value

    state, ud, td, fd, _, _ = _load_wotr_setup()
    terr = state.territories.get("harondor")
    assert terr is not None
    assert getattr(terr, "owner", None) is None
    assert _attack_setup_value("harondor", state, "harad", fd, td, ud) > 0.0


def test_ai_decide_non_combat_move_returns_move_or_end_phase():
    """In non_combat_move, AI returns move_units (setup for next turn) or end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "non_combat_move"
    state.current_faction = "mordor"
    available_actions = {"phase": "non_combat_move", "faction": "mordor"}
    ctx = AIContext(state=state, unit_defs=ud, territory_defs=td, faction_defs=fd, camp_defs=cd, port_defs=port_d, available_actions=available_actions)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("move_units", "end_phase")
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_prune_empty_open_space_batches_all_infantry():
    """Open hex: all eligible foot units move in one declaration (not one instance per step)."""
    state, ud, td, fd, _, _ = _load_wotr_setup()
    land_tid = next(
        (
            k
            for k in state.territories
            if td.get(k) and getattr(td[k], "terrain_type", "") != "sea"
        ),
        None,
    )
    if not land_tid:
        pytest.skip("no land territory")
    uid = next(
        (x for x in ud if getattr(ud[x], "faction", "") == "gondor" and getattr(ud[x], "archetype", "") == "infantry"),
        None,
    ) or next((x for x in ud if getattr(ud[x], "faction", "") == "gondor"), None)
    if not uid:
        pytest.skip("no gondor land unit in defs")
    units = [
        Unit(
            instance_id=f"gondor_open_{i}",
            unit_id=uid,
            remaining_movement=2,
            remaining_health=1,
            base_movement=2,
            base_health=1,
            loaded_onto=None,
        )
        for i in range(3)
    ]
    state.territories[land_tid].units = units
    state.territories[land_tid].owner = "gondor"
    ids = [u.instance_id for u in units]
    pruned = ai_combat_move._prune_empty_open_space_move(
        ids,
        land_tid,
        state,
        ud,
        10.0,
        would_hold_frontline=False,
        has_confident_defended_elsewhere=False,
        pending_cavalry_to_destination=0,
        charge_path_for=lambda _ids: None,
    )
    assert sorted(pruned) == sorted(ids)


def test_pending_cavalry_count_to_territory_respects_combat_move_pending():
    state, ud, td, fd, _, _ = _load_wotr_setup()
    land_tid = next(
        (
            k
            for k in state.territories
            if td.get(k) and getattr(td[k], "terrain_type", "") != "sea"
        ),
        None,
    )
    cav_uid = next(
        (
            u
            for u in ud
            if getattr(ud[u], "archetype", "") == "cavalry"
            or "cavalry" in (getattr(ud[u], "tags", None) or [])
        ),
        None,
    )
    if not land_tid or not cav_uid:
        pytest.skip("need land + cavalry unit def")
    dest = land_tid
    u = Unit(
        instance_id="test_cav_1",
        unit_id=cav_uid,
        remaining_movement=2,
        remaining_health=1,
        base_movement=2,
        base_health=1,
        loaded_onto=None,
    )
    state.territories[land_tid].units = [u]
    faction = get_unit_faction(u, ud)
    state.pending_moves = [
        PendingMove(
            from_territory=land_tid,
            to_territory=dest,
            unit_instance_ids=["test_cav_1"],
            phase="combat_move",
            primary_unit_id=cav_uid,
        )
    ]
    to_key = resolve_territory_key_in_state(state, dest, td)
    n = ai_combat_move._pending_cavalry_count_to_territory(
        state, to_key, faction, ud, td
    )
    assert n == 1


def test_is_cavalry_combat_always_bool_for_sort_keys():
    """
    Regression: `return "cavalry" in tags or []` parsed as (`in` then `or []`) and returned
    a list when cavalry was absent, breaking spare.sort(bool vs list).
    """
    class FakeDef:
        archetype = "infantry"
        tags = ["forest"]

    r = ai_combat_move._is_cavalry_combat({"x": FakeDef()}, "x")
    assert r is False
    assert type(r) is bool


def test_ai_decide_combat_phase_returns_continue_or_retreat():
    """When phase is combat and active_combat exists, decide() returns continue_combat or retreat."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    # Pick a territory and add attacker + defender units so sim can run
    tid = next((t for t in state.territories if td.get(t) and getattr(td.get(t), "terrain_type", "") != "sea"), None)
    if not tid:
        pytest.skip("no land territory in setup")
    terr = state.territories[tid]
    # Use gondor vs mordor units (must exist in wotr_exp_1.0)
    gondor_uid = "gondor_infantry" if "gondor_infantry" in ud else next((u for u in ud if getattr(ud[u], "faction", "") == "gondor"), None)
    mordor_uid = "mordor_orc" if "mordor_orc" in ud else next((u for u in ud if getattr(ud[u], "faction", "") == "mordor"), None)
    if not gondor_uid or not mordor_uid:
        pytest.skip("gondor/mordor unit types not found")
    att1 = Unit(instance_id="gondor_att_1", unit_id=gondor_uid, remaining_movement=1, remaining_health=1, base_movement=1, base_health=1, loaded_onto=None)
    att2 = Unit(instance_id="gondor_att_2", unit_id=gondor_uid, remaining_movement=1, remaining_health=1, base_movement=1, base_health=1, loaded_onto=None)
    def1 = Unit(instance_id="mordor_def_1", unit_id=mordor_uid, remaining_movement=1, remaining_health=1, base_movement=1, base_health=1, loaded_onto=None)
    def2 = Unit(instance_id="mordor_def_2", unit_id=mordor_uid, remaining_movement=1, remaining_health=1, base_movement=1, base_health=1, loaded_onto=None)
    terr.units = [att1, att2, def1, def2]
    terr.owner = "mordor"  # defender owns territory
    state.phase = "combat"
    state.current_faction = "gondor"
    state.active_combat = ActiveCombat(
        attacker_faction="gondor",
        territory_id=tid,
        attacker_instance_ids=["gondor_att_1", "gondor_att_2"],
        round_number=1,
    )
    available_actions = {"phase": "combat", "faction": "gondor", "active_combat": state.active_combat.to_dict()}
    ctx = AIContext(state=state, unit_defs=ud, territory_defs=td, faction_defs=fd, camp_defs=cd, port_defs=port_d, available_actions=available_actions)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("continue_combat", "retreat"), f"expected continue_combat or retreat, got {action.type}"
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_ai_decide_combat_sea_raid_attackers_on_land_only():
    """
    Sea raid active_combat keeps sea_zone_id set, but after offload attackers live on the
    territory — AI must still find them (same as reducer), not return end_phase.
    """
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    tid = "harondor"
    sz = "sea_zone_11"
    if tid not in state.territories or sz not in state.territories:
        pytest.skip("harondor/sea_zone_11 not in wotr_exp_1.0 setup")
    terr = state.territories[tid]
    sea = state.territories[sz]
    gondor_uid = "gondor_infantry" if "gondor_infantry" in ud else None
    mordor_uid = "mordor_orc" if "mordor_orc" in ud else None
    if not gondor_uid or not mordor_uid:
        pytest.skip("gondor/mordor unit types not found")
    att1 = Unit(
        instance_id="gondor_sea_1",
        unit_id=gondor_uid,
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
        loaded_onto=None,
    )
    def1 = Unit(
        instance_id="mordor_def_1",
        unit_id=mordor_uid,
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
        loaded_onto=None,
    )
    terr.units = [att1, def1]
    terr.owner = "mordor"
    sea.units = []
    state.phase = "combat"
    state.current_faction = "gondor"
    state.active_combat = ActiveCombat(
        attacker_faction="gondor",
        territory_id=tid,
        attacker_instance_ids=["gondor_sea_1"],
        round_number=1,
        sea_zone_id=sz,
    )
    available_actions = {"phase": "combat", "faction": "gondor", "active_combat": state.active_combat.to_dict()}
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )
    action = decide(ctx)
    assert action is not None
    assert action.type in ("continue_combat", "retreat"), f"got {action.type}"
    assert action.type != "end_phase"
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error


def test_mobilization_splits_forward_camp_and_capital():
    """Large land purchase: first wave can fill a forward camp to its cap; later waves still score all destinations (no forced drain to capital)."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "mobilization"
    state.current_faction = "gondor"
    state.faction_purchased_units["gondor"] = [
        UnitStack(unit_id="gondor_soldier", count=6)
    ]
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={},
    )
    a1 = decide_mobilization(ctx)
    assert a1 is not None and a1.type == "mobilize_units"
    assert a1.payload.get("destination") == "west_osgiliath"
    n1 = sum(u.get("count", 0) for u in (a1.payload.get("units") or []))
    assert n1 == 3  # west_osgiliath power cap limits first wave; split max is 5

    nstate, _ = apply_action(state, a1, ud, td, fd, cd, port_d)
    nstate.phase = "mobilization"
    nstate.current_faction = "gondor"
    ctx2 = AIContext(
        state=nstate,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions={},
    )
    a2 = decide_mobilization(ctx2)
    assert a2 is not None and a2.type == "mobilize_units"
    # West Osgiliath is full after the first mobilization; remaining waves must not be locked to capital only.
    assert a2.payload.get("destination") != "west_osgiliath"
    n2 = sum(u.get("count", 0) for u in (a2.payload.get("units") or []))
    assert n2 >= 1
    v2 = validate_action(nstate, a2, ud, td, fd, cd, port_d)
    assert v2.valid, v2.error


def test_ai_decide_mobilization_phase():
    """In mobilization phase with purchased units, decide() returns mobilize_units or end_phase."""
    state, ud, td, fd, cd, port_d = _load_wotr_setup()
    state.phase = "mobilization"
    state.current_faction = "gondor"
    state.faction_purchased_units["gondor"] = [UnitStack(unit_id="gondor_soldier", count=2)]
    from backend.engine.queries import get_mobilization_capacity, get_mobilization_territories
    cap = get_mobilization_capacity(state, "gondor", td, cd, port_d, ud)
    territories = cap.get("territories", [])
    if not territories or not any(t.get("power", 0) > 0 for t in territories):
        pytest.skip("gondor has no camp/territory with power in this setup")
    available_actions = {
        "phase": "mobilization",
        "faction": "gondor",
        "mobilize_options": {"territories": get_mobilization_territories(state, "gondor", td, cd, port_d, ud), "capacity": cap, "pending_units": [{"unit_id": "gondor_soldier", "count": 2}]},
    }
    ctx = AIContext(state=state, unit_defs=ud, territory_defs=td, faction_defs=fd, camp_defs=cd, port_defs=port_d, available_actions=available_actions)
    action = decide(ctx)
    assert action is not None
    assert action.type in ("mobilize_units", "end_phase")
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    assert validation.valid, validation.error
