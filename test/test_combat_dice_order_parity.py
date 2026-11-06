"""
L1 (issue #51 combat audit): API dice generation must derive ladder order, ring
mods, and bombikazi fuse rules exactly like the reducer's continue path.

The old API helper (_sort_attackers_for_ladder_dice_if_needed) read the PERSISTED
combat.ladder_infantry_instance_ids, ignored ring attack boosts, and always applied
fused bombikazi rules — so rolls could be paired with different units than the
reducer that consumed them. Both sides now share
compute_standard_round_combat_inputs (backend/engine/combat.py).

Each test pins the concrete attacker order the API must produce AND checks the
reducer's combat_round_resolved event grouping equals the API-side grouping for
the same fixed rolls (the dice contract).

Run: .venv/bin/pytest test/test_combat_dice_order_parity.py
"""

from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.combat import (
    compute_standard_round_combat_inputs,
    get_siegework_attacker_rolling_units,
    get_siegework_defender_rolling_units,
    group_attacker_dice_with_ladder_segments,
    group_dice_by_stat,
)
from backend.engine.definitions import (
    UnitDefinition,
    load_static_definitions,
    load_starting_setup,
)
from backend.engine.reducer import apply_action
from backend.engine.state import Ring, Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_1.1"
STRONGHOLD = "rivendell"  # stronghold base HP 2, forest terrain
PLAIN = "gap_of_rohan"  # plains, not a stronghold


def _fresh(territory_id: str, injector=None):
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    if injector is not None:
        unit_defs = injector(unit_defs)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=load_starting_setup(setup_id=SETUP),
        camp_defs=camp_defs,
    )
    t = state.territories[territory_id]
    t.units.clear()
    t.owner = "rohan"
    state.current_faction = "isengard"
    state.phase = "combat"
    state.rings_of_power = False
    state.rings = []
    return state, unit_defs, territory_defs, faction_defs, camp_defs, port_defs


def _place(state, unit_defs, territory_id, specs):
    t = state.territories[territory_id]
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


def _initiate(state, unit_defs, td, fd, cd, pd, territory_id, att_rolls, def_rolls, **kw):
    action = initiate_combat(
        "isengard", territory_id,
        {"attacker": list(att_rolls), "defender": list(def_rolls)},
        **kw,
    )
    state, _ = apply_action(state, action, unit_defs, td, fd, cd, pd)
    assert state.active_combat is not None
    return state


class _FixedRolls:
    """Replace the RNG with fixed rolls: attacker generation first, defender second."""

    def __init__(self, attacker_rolls, defender_rolls):
        self._queues = [list(attacker_rolls), list(defender_rolls)]

    def __call__(self, units, unit_defs, **kwargs):
        assert self._queues, "more dice generations than expected"
        return list(self._queues.pop(0))


def _api_dice_and_expected(state, unit_defs, td, fd, att_rolls, def_rolls, monkeypatch):
    """Run the API's dice generation with fixed rolls; build the expected groupings.

    Returns (api_dice, expected_attacker_grouping, expected_defender_grouping,
    sorted_attackers, round_inputs) where the sorted roster is what the API helper
    produced and round_inputs is the shared per-round derivation.
    """
    from backend.api.main import (
        _generate_dice_rolls_for_active_combat,
        _get_active_combat_units,
        _prepare_attacker_dice_for_round,
    )

    monkeypatch.setattr(
        "backend.api.main.generate_dice_rolls_for_units",
        _FixedRolls(att_rolls, def_rolls),
    )
    api_dice = _generate_dice_rolls_for_active_combat(state, unit_defs, td, fd)
    assert api_dice == {"attacker": list(att_rolls), "defender": list(def_rolls)}, (
        "fixed rolls must flow through the standard-round branch only"
    )

    attackers, defenders = _get_active_combat_units(state, td, unit_defs, fd)
    eff_dice = _prepare_attacker_dice_for_round(state, attackers, defenders, unit_defs, td)
    combat = state.active_combat
    ci = compute_standard_round_combat_inputs(
        state, attackers, defenders, unit_defs, td.get(combat.territory_id), combat,
    )
    ladder = set(ci.ladder_ids)
    if ladder:
        exp_att = group_attacker_dice_with_ladder_segments(
            attackers, api_dice["attacker"], unit_defs, ladder,
            stat_modifiers=ci.attacker_mods or None,
            effective_dice_override=eff_dice,
            effective_stat_override=ci.attack_override or None,
            exclude_archetypes_from_rolling={"siegework"},
        )
    else:
        exp_att = group_dice_by_stat(
            attackers, api_dice["attacker"], unit_defs, is_attacker=True,
            stat_modifiers=ci.attacker_mods or None,
            effective_dice_override=eff_dice,
            effective_stat_override=ci.attack_override or None,
            exclude_archetypes_from_rolling={"siegework"},
        )
    exp_def = group_dice_by_stat(
        defenders, api_dice["defender"], unit_defs, is_attacker=False,
        stat_modifiers=ci.defender_mods or None,
        exclude_archetypes_from_rolling={"siegework"},
    )
    return api_dice, exp_att, exp_def, attackers, ci


def _apply_continue_and_compare(
    state, unit_defs, td, fd, cd, pd, api_dice, exp_att, exp_def, expected_ladder=None,
):
    state, events = apply_action(
        state, continue_combat("isengard", api_dice), unit_defs, td, fd, cd, pd,
    )
    resolved = [e for e in events if e.type == "combat_round_resolved"]
    assert len(resolved) == 1, f"expected one standard round, got {len(resolved)}"
    payload = resolved[0].payload
    assert not payload.get("is_siegeworks_round"), "continue must run a standard round"
    assert payload["attacker_dice"] == exp_att, "API grouping must match reducer grouping"
    assert payload["defender_dice"] == exp_def, "API grouping must match reducer grouping"
    if expected_ladder is not None:
        assert payload["ladder_infantry_instance_ids"] == expected_ladder
    assert payload["attacker_casualties"] == []
    assert payload["defender_casualties"] == []
    return state


def _rolled_unit_ids(attackers, unit_defs, eff_dice):
    """Unit ids that actually consume a roll: eff dice > 0, siegework excluded."""
    return [
        u.unit_id for u in attackers
        if (eff_dice.get(u.instance_id, 0) or 0) > 0
        and getattr(unit_defs.get(u.unit_id), "archetype", "") != "siegework"
    ]


def test_stale_empty_ladder_ids_helper_sorts_fresh_and_matches_reducer(monkeypatch):
    """Persisted ladder ids were wiped: the API must re-derive them like the reducer."""
    state, ud, td, fd, cd, pd = _fresh(STRONGHOLD)
    _place(state, ud, STRONGHOLD, [("isengard", "siege_ladder")])
    atk = _place(state, ud, STRONGHOLD, [
        ("isengard", "berserker"),
        ("isengard", "dunland_wildman"),
        ("isengard", "urukhai_warrior"),
    ])
    _place(state, ud, STRONGHOLD, [("gondor", "gondor_soldier")] * 2)
    berserker, dunland, urukhai = atk

    # Round 1 runs inside initiate (ladder has 0 dice, no siege needed); all rolls miss.
    state = _initiate(state, ud, td, fd, cd, pd, STRONGHOLD, [10, 10, 10], [10, 10])
    assert state.active_combat.round_number == 1
    # Stale: someone (or a round-0 battle) left the persisted list empty.
    state.active_combat.ladder_infantry_instance_ids = []

    api_dice, exp_att, exp_def, attackers, ci = _api_dice_and_expected(
        state, ud, td, fd, [10, 9, 8], [10, 10], monkeypatch,
    )

    # Fresh derivation: both climbers fit (capacity 2), sorted worst-attack first.
    assert ci.ladder_ids, "fresh ladder derivation must be non-empty"
    assert sorted(ci.ladder_ids) == sorted([berserker.instance_id, urukhai.instance_id])
    # Sorted roster: eff attack dunland 2 < urukhai 3 < berserker 5 (ladder unit siegework).
    assert _rolled_unit_ids(attackers, ud, ci.effective_dice) == [
        "dunland_wildman", "urukhai_warrior", "berserker",
    ]

    _apply_continue_and_compare(
        state, ud, td, fd, cd, pd, api_dice, exp_att, exp_def,
        expected_ladder=ci.ladder_ids,
    )


def test_stale_persisted_ladder_ids_do_not_force_a_sort_when_ladder_is_gone(monkeypatch):
    """Persisted ids non-empty but no ladder equipment: fresh derivation wins (no sort)."""
    state, ud, td, fd, cd, pd = _fresh(STRONGHOLD)
    atk = _place(state, ud, STRONGHOLD, [
        ("isengard", "berserker"),
        ("isengard", "dunland_wildman"),
        ("isengard", "urukhai_warrior"),
    ])
    _place(state, ud, STRONGHOLD, [("gondor", "gondor_soldier")] * 2)

    state = _initiate(state, ud, td, fd, cd, pd, STRONGHOLD, [10, 10, 10], [10, 10])
    # Stale: ids from a previous battle linger; this roster has no ladder equipment.
    state.active_combat.ladder_infantry_instance_ids = [
        u.instance_id for u in atk
    ]

    api_dice, exp_att, exp_def, attackers, ci = _api_dice_and_expected(
        state, ud, td, fd, [10, 9, 8], [10, 10], monkeypatch,
    )

    assert ci.ladder_ids == [], "no ladder equipment -> no ladder assignment"
    # No sort: roster stays in instance order (an eff-attack sort would start with dunland).
    assert _rolled_unit_ids(attackers, ud, ci.effective_dice) == [
        "berserker", "dunland_wildman", "urukhai_warrior",
    ]

    _apply_continue_and_compare(
        state, ud, td, fd, cd, pd, api_dice, exp_att, exp_def,
        expected_ladder=[],
    )


def test_fuse_off_after_siegeworks_matches_reducer_unfused_dice_order(monkeypatch):
    """After a fuse-off siegeworks round the API must use unfused bombikazi rules."""
    state, ud, td, fd, cd, pd = _fresh(STRONGHOLD)
    atk = _place(state, ud, STRONGHOLD, [
        ("isengard", "siege_ladder"),
        ("isengard", "berserker"),
        ("isengard", "bomb"),
        ("isengard", "isengard_warg_rider"),
        ("isengard", "battering_ram"),
    ])
    _place(state, ud, STRONGHOLD, [("gondor", "gondor_soldier")] * 2)
    ladder, berserker, bomb, warg, _ram = atk
    t = state.territories[STRONGHOLD]
    hp = getattr(t, "stronghold_current_health", None) or getattr(
        td[STRONGHOLD], "stronghold_base_health", 0,
    )
    # fuse_bomb=False + stronghold HP > 0: the ram forces a siegeworks round in initiate.
    sw_att = get_siegework_attacker_rolling_units(atk, ud, True, defender_stronghold_hp=hp, fuse_bomb=False)
    sw_def = get_siegework_defender_rolling_units(t.units[len(atk):], ud)
    assert [u.unit_id for u in sw_att] == ["battering_ram"], "fuse off: bomb and ladder skip siege"
    state = _initiate(
        state, ud, td, fd, cd, pd, STRONGHOLD,
        [10] * sum(getattr(ud[u.unit_id], "dice", 1) for u in sw_att),
        [10] * sum(getattr(ud[u.unit_id], "dice", 1) for u in sw_def),
        fuse_bomb=False,
    )
    assert state.active_combat.fuse_bomb is False
    assert any(getattr(r, "is_siegeworks_round", False) for r in state.active_combat.combat_log)
    assert state.active_combat.round_number == 0

    api_dice, exp_att, exp_def, attackers, ci = _api_dice_and_expected(
        state, ud, td, fd, [10, 9], [10, 10], monkeypatch,
    )

    # Non-fused rules: paired berserker rolls its own 1 die (no bomb attack override,
    # no paired self-destruct), paired bomb rolls 0.
    assert ci.self_destruct == []
    assert berserker.instance_id not in ci.attack_override
    assert ci.effective_dice[berserker.instance_id] == 1
    assert ci.effective_dice[bomb.instance_id] == 0
    # Fresh ladder: berserker climbs (capacity 2 from the ladder).
    assert ci.ladder_ids == [berserker.instance_id]
    # Rolled order: berserker (5) before warg (6). Fused rules would flip them
    # (berserker would sort at the bomb's attack 7).
    assert _rolled_unit_ids(attackers, ud, ci.effective_dice) == [
        "berserker", "isengard_warg_rider",
    ]
    assert warg.instance_id in state.active_combat.attacker_instance_ids
    assert ladder.instance_id in state.active_combat.attacker_instance_ids

    _apply_continue_and_compare(
        state, ud, td, fd, cd, pd, api_dice, exp_att, exp_def,
        expected_ladder=ci.ladder_ids,
    )


def _inject_ring_units(unit_defs):
    unit_defs["ringhero"] = UnitDefinition(
        id="ringhero", display_name="Ring Hero", faction="isengard", archetype="infantry",
        tags=["land", "climbs_ladder"], attack=3, defense=1, movement=1, health=1,
        cost={"power": 10}, dice=1, hero_id="ringhero", specials=[],
    )
    unit_defs["sidekick"] = UnitDefinition(
        id="sidekick", display_name="Sidekick", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=1, movement=1, health=1,
        cost={"power": 10}, dice=1, specials=[],
    )
    return unit_defs


def test_ring_attack_boost_included_in_api_sort_and_matches_reducer(monkeypatch):
    """A ring attack boost must flip the ladder dice order on the API side too."""
    state, ud, td, fd, cd, pd = _fresh(PLAIN, injector=_inject_ring_units)
    _place(state, ud, PLAIN, [("isengard", "siege_ladder")])
    ringhero, sidekick = _place(state, ud, PLAIN, [
        ("isengard", "ringhero"),
        ("isengard", "sidekick"),
    ])
    _place(state, ud, PLAIN, [("gondor", "gondor_soldier")] * 2)

    state.rings_of_power = True
    ring = Ring(
        id="l1_ring", name="L1 Ring", power=1, territory_id=PLAIN, attack_boost=3,
    )
    ring.carried_in_by = ringhero.instance_id
    state.rings = [ring]

    # Round 1 in initiate: ringhero + sidekick roll (ladder excluded); all miss.
    state = _initiate(state, ud, td, fd, cd, pd, PLAIN, [10, 10], [10, 10])

    api_dice, exp_att, exp_def, attackers, ci = _api_dice_and_expected(
        state, ud, td, fd, [10, 9], [10, 10], monkeypatch,
    )

    assert ci.ladder_ids == [ringhero.instance_id], "ringhero climbs (capacity 2)"
    assert ci.attacker_mods.get(ringhero.instance_id) == 3, "ring attack boost in mods"
    # Effective attack: sidekick 5 < ringhero 3+3. Without the boost the API would
    # sort ringhero first (3 < 5) and pair the rolls the other way.
    assert _rolled_unit_ids(attackers, ud, ci.effective_dice) == ["sidekick", "ringhero"]

    _apply_continue_and_compare(
        state, ud, td, fd, cd, pd, api_dice, exp_att, exp_def,
        expected_ladder=ci.ladder_ids,
    )
