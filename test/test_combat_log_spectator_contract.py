"""
Spectator combat_log contract: combat_log entries must carry everything the combat
modal gets from combat_round_resolved, so read-only spectators shelf dice on the
exact effective stats the modal shows.

Pins:
- archer prefire entry: grouped defender_dice + both *_units_at_start snapshots
  (previously only flat defender_rolls, so spectators fell back to a fake stat-1
  row with target 10 and rebuilt shelves from drifted live state),
- stealth prefire entry: grouped attacker_dice + both snapshots,
- standard round entry: both snapshots (dice were already grouped),
- every entry's dice stat keys match the effective stats of its round-start units,
- log entry == the combat_round_resolved event payload (single source of truth),
- to_dict/from_dict round trip preserves dice and unit snapshots.

Run: .venv/bin/pytest test/test_combat_log_spectator_contract.py
"""

from backend.engine.actions import initiate_combat
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import CombatRoundResult, Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"


def _synthetic_defs(unit_defs):
    # cheapx: plain attacker (no siege, no stealth) so battles open straight into
    # archer prefire or round 1. sneakx: stealth-only attacker for the stealth branch.
    # tankx: bulky non-archer defender so prefire/archer branches stay controlled.
    unit_defs["cheapx"] = UnitDefinition(
        id="cheapx", display_name="Cheap X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=3, movement=1, health=1, cost={"power": 1},
        dice=1, specials=[],
    )
    unit_defs["sneakx"] = UnitDefinition(
        id="sneakx", display_name="Sneak X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 1},
        dice=1, specials=["stealth"],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=3, movement=1, health=10, cost={"power": 10},
        dice=1, specials=[],
    )
    return unit_defs


def _fresh_state():
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    unit_defs = _synthetic_defs(unit_defs)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=load_starting_setup(setup_id=SETUP),
        camp_defs=camp_defs,
    )
    t = state.territories[TERRITORY]
    t.units.clear()
    t.owner = "rohan"
    state.current_faction = "isengard"
    state.phase = "combat"
    state.rings_of_power = False
    state.rings = []
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _place(state, unit_defs, specs) -> list:
    t = state.territories[TERRITORY]
    units = []
    for faction, unit_id in specs:
        udef = unit_defs[unit_id]
        units.append(Unit(
            instance_id=state.generate_unit_instance_id(faction, unit_id),
            unit_id=unit_id,
            remaining_movement=udef.movement,
            remaining_health=udef.health,
            base_movement=udef.movement,
            base_health=udef.health,
        ))
    t.units.extend(units)
    return units


def _initiate(state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs, rolls):
    action = initiate_combat("isengard", TERRITORY, rolls)
    return apply_action(state, action, unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _round_event(events):
    resolved = [e for e in events if e.type == "combat_round_resolved"]
    assert len(resolved) == 1
    return resolved[0].payload


def _assert_entry_matches_event(entry: CombatRoundResult, ev: dict, side: str):
    """The combat_log entry must be the same data the combat modal renders."""
    if side in ("attacker", "both"):
        ev_att = ev.get("attacker_dice") or {}
        if ev_att:
            assert entry.attacker_dice == {str(k): v for k, v in ev_att.items()}
        else:
            assert not entry.attacker_dice
        assert entry.attacker_units_at_start == ev["attacker_units_at_start"]
    if side in ("defender", "both"):
        ev_def = ev.get("defender_dice") or {}
        if ev_def:
            assert entry.defender_dice == {str(k): v for k, v in ev_def.items()}
        else:
            assert not entry.defender_dice
        assert entry.defender_units_at_start == ev["defender_units_at_start"]


def test_archer_prefire_log_entry_has_grouped_dice_and_round_units():
    state, ud, td, fd, cd, pd = _fresh_state()
    _place(state, ud, [("isengard", "cheapx")])
    _place(state, ud, [("rohan", "rohirrim_archer")])

    # Archer prefire: only the defender rolls (1 die), penalty -1 on defense 2 -> target 1.
    # Miss (10) so the 1-HP attacker survives and combat stays active.
    state, events = _initiate(
        state, ud, td, fd, cd, pd, {"attacker": [1], "defender": [10]},
    )
    entry = state.active_combat.combat_log[0]
    assert entry.is_archer_prefire is True
    ev = _round_event(events)

    # Grouped dice + snapshots (the old entry had only flat defender_rolls).
    assert entry.defender_dice == {"1": {"rolls": [10], "hits": 0}}
    assert entry.attacker_dice is None  # attackers do not roll in prefire
    assert entry.attacker_units_at_start and entry.defender_units_at_start
    _assert_entry_matches_event(entry, ev, "both")

    # Shelves: dice bucket keys are exactly the round-start effective stats.
    def_eff = {u["instance_id"]: u["effective_defense"] for u in entry.defender_units_at_start}
    assert set(def_eff.values()) == {1}  # 2 - prefire penalty (plains, no leaders)
    assert {int(k) for k in entry.defender_dice} == set(def_eff.values())
    att_eff = {u["instance_id"]: u["effective_attack"] for u in entry.attacker_units_at_start}
    assert set(att_eff.values()) == {5}  # cheapx attack, no modifiers on plains

    # Round trip through persistence keeps the spectator data.
    rt = CombatRoundResult.from_dict(entry.to_dict())
    assert rt.defender_dice == entry.defender_dice
    assert rt.attacker_units_at_start == entry.attacker_units_at_start
    assert rt.defender_units_at_start == entry.defender_units_at_start


def test_stealth_prefire_log_entry_has_grouped_dice_and_round_units():
    state, ud, td, fd, cd, pd = _fresh_state()
    _place(state, ud, [("isengard", "sneakx"), ("isengard", "sneakx")])
    _place(state, ud, [("rohan", "tankx")])

    # All attackers have stealth -> prefire at attack-1 (1-1 floored at 1).
    state, events = _initiate(
        state, ud, td, fd, cd, pd, {"attacker": [1, 5], "defender": []},
    )
    entry = state.active_combat.combat_log[0]
    assert entry.is_stealth_prefire is True
    ev = _round_event(events)

    assert entry.attacker_dice == {"1": {"rolls": [1, 5], "hits": 1}}
    assert entry.defender_dice is None  # defenders do not roll in prefire
    assert entry.attacker_units_at_start and entry.defender_units_at_start
    _assert_entry_matches_event(entry, ev, "both")

    att_eff = {u["instance_id"]: u["effective_attack"] for u in entry.attacker_units_at_start}
    assert set(att_eff.values()) == {1}
    assert {int(k) for k in entry.attacker_dice} == set(att_eff.values())
    def_eff = {u["instance_id"]: u["effective_defense"] for u in entry.defender_units_at_start}
    assert set(def_eff.values()) == {3}  # tankx defense, no modifiers

    rt = CombatRoundResult.from_dict(entry.to_dict())
    assert rt.attacker_dice == entry.attacker_dice
    assert rt.attacker_units_at_start == entry.attacker_units_at_start
    assert rt.defender_units_at_start == entry.defender_units_at_start


def test_standard_round_log_entry_has_round_unit_snapshots():
    state, ud, td, fd, cd, pd = _fresh_state()
    _place(state, ud, [("isengard", "cheapx")])
    _place(state, ud, [("rohan", "tankx")])

    # Defender misses (10) so the 1-HP attacker survives and combat stays active.
    state, events = _initiate(
        state, ud, td, fd, cd, pd, {"attacker": [5], "defender": [10]},
    )
    entry = state.active_combat.combat_log[0]
    assert entry.round_number == 1
    ev = _round_event(events)

    assert entry.attacker_dice and entry.defender_dice
    assert entry.attacker_units_at_start and entry.defender_units_at_start
    _assert_entry_matches_event(entry, ev, "both")

    att_eff = {u["effective_attack"] for u in entry.attacker_units_at_start}
    def_eff = {u["effective_defense"] for u in entry.defender_units_at_start}
    assert att_eff == {5}
    assert def_eff == {3}
    assert {int(k) for k in entry.attacker_dice} <= att_eff
    assert {int(k) for k in entry.defender_dice} <= def_eff

    rt = CombatRoundResult.from_dict(entry.to_dict())
    assert rt.attacker_units_at_start == entry.attacker_units_at_start
    assert rt.defender_units_at_start == entry.defender_units_at_start
