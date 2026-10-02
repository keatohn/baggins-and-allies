"""Tests for the combat specials engine (single source of truth for terror, captain, bombikazi, etc.)."""
from dataclasses import replace

import pytest
from backend.engine.combat import (
    compute_leader_stat_modifiers,
    get_siegework_dice_counts,
    get_terror_reroll_targets,
    siegework_dice_round_applies,
)
from backend.engine.definitions import UnitDefinition, load_static_definitions
from backend.engine.state import Unit
from backend.engine.combat_specials import (
    compute_battle_specials_and_modifiers,
    specials_flags_for_round_payload,
    stacks_to_synthetic_units,
    BattleSpecialsResult,
)


@pytest.fixture
def defs():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud, td


def test_combat_specials_terror(defs):
    """Attackers with terror (e.g. Nazgûl) get terror=True in specials_attacker."""
    unit_defs, territory_defs = defs
    nazgul_id = "nazgul" if "nazgul" in unit_defs else next((k for k in unit_defs if "nazgul" in k.lower()), None)
    if not nazgul_id:
        pytest.skip("wotr_exp_1.0 has no nazgul unit")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": nazgul_id, "count": 1}],
        [{"unit_id": "gondor_soldier", "count": 2}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False,
    )
    assert isinstance(result, BattleSpecialsResult)
    assert len(result.specials_attacker) == 1
    for spec in result.specials_attacker.values():
        assert spec.get("terror") is True


def test_combat_specials_captain(defs):
    """Stack with captain gets captain modifier (+1) on some allies; captain unit has captain=True."""
    unit_defs, territory_defs = defs
    if "captain_of_gondor" not in unit_defs:
        pytest.skip("wotr_exp_1.0 has no captain_of_gondor")
    att_units, def_units = stacks_to_synthetic_units(
        [
            {"unit_id": "captain_of_gondor", "count": 1},
            {"unit_id": "gondor_soldier", "count": 2},
        ],
        [{"unit_id": "morannon_orc", "count": 2}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False,
    )
    assert len(result.specials_attacker) == 3
    # At least one unit should have captain bonus (modifier > 0) or captain flag
    captain_flags = [s.get("captain") for s in result.specials_attacker.values()]
    assert any(captain_flags), "expected at least one captain in stack"
    # Captain gives +1 to allies (not self); so exactly 2 allies should have modifier
    assert len(result.stat_modifiers_attacker) >= 2
    assert all(m >= 1 for m in result.stat_modifiers_attacker.values())


def test_combat_specials_bombikazi(defs):
    """Attackers with paired bombikazi + bomb get bombikazi=True for paired units."""
    unit_defs, territory_defs = defs
    if "bomb" not in unit_defs or "berserker" not in unit_defs:
        berserker_id = next((k for k in unit_defs if "berserker" in k.lower()), None)
        if not berserker_id or "bomb" not in unit_defs:
            pytest.skip("wotr_exp_1.0 has no bomb/berserker")
    else:
        berserker_id = "berserker"
    att_units, def_units = stacks_to_synthetic_units(
        [
            {"unit_id": berserker_id, "count": 1},
            {"unit_id": "bomb", "count": 1},
        ],
        [{"unit_id": "gondor_soldier", "count": 2}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False,
    )
    # One unit should have bombikazi=True (the paired berserker)
    bombikazi_flags = [s.get("bombikazi") for s in result.specials_attacker.values()]
    assert any(bombikazi_flags), "expected paired bombikazi to have bombikazi=True"


def test_combat_specials_sea_raider(defs):
    """With is_sea_raid=True, sea_raider unit (e.g. Corsair) gets seaRaider=True and +1 attack modifier."""
    unit_defs, territory_defs = defs
    corsair_id = "corsair_of_umbar" if "corsair_of_umbar" in unit_defs else next((k for k in unit_defs if "corsair" in k.lower()), None)
    if not corsair_id:
        pytest.skip("wotr_exp_1.0 has no corsair/sea_raider unit")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": corsair_id, "count": 2}],
        [{"unit_id": "gondor_soldier", "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=True, archer_prefire_applicable=False,
    )
    for spec in result.specials_attacker.values():
        assert spec.get("seaRaider") is True
    # Each attacker should have +1 from sea_raider
    for mod in result.stat_modifiers_attacker.values():
        assert mod >= 1


def test_combat_specials_hope_when_attacker_has_terror(defs):
    """Defenders with hope get hope=True when attackers have terror."""
    unit_defs, territory_defs = defs
    if "nazgul" not in unit_defs or "eagle" not in unit_defs:
        pytest.skip("wotr_exp_1.0 needs nazgul and eagle")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": "nazgul", "count": 1}],
        [{"unit_id": "eagle", "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False,
    )
    for spec in result.specials_defender.values():
        assert spec.get("hope") is True


def test_combat_specials_stealth_activated_when_all_have_stealth(defs):
    """When all attackers have stealth, specials show stealth=True for them (and stealth_activated)."""
    unit_defs, territory_defs = defs
    # No unit in wotr_exp_1.0 has stealth; use a minimal mock unit_def with stealth
    from backend.engine.definitions import UnitDefinition
    stealth_unit_id = "test_stealth_unit"
    mock_ud = UnitDefinition(
        id=stealth_unit_id,
        display_name="Stealth Test",
        faction="gondor",
        archetype="infantry",
        tags=["land", "stealth"],
        attack=2,
        defense=1,
        movement=1,
        health=1,
        cost={"power": 1},
        dice=1,
    )
    ud_extended = {**unit_defs, stealth_unit_id: mock_ud}
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": stealth_unit_id, "count": 2}],
        [{"unit_id": "morannon_orc", "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    result = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, ud_extended,
        is_sea_raid=False, archer_prefire_applicable=False,
        stealth_prefire_applicable=True,
    )
    for spec in result.specials_attacker.values():
        assert spec.get("stealth") is True


def test_archer_special_only_when_prefire_applicable(defs):
    """Defender archer flag is True only when archer_prefire_applicable (round payload / prefire UI)."""
    unit_defs, territory_defs = defs
    archer_id = next((k for k in unit_defs if getattr(unit_defs[k], "archetype", "") == "archer"), None)
    if not archer_id:
        pytest.skip("no archer unit in setup")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": archer_id, "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    off = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False,
    )
    iid = def_units[0].instance_id
    assert specials_flags_for_round_payload(iid, False, off)["archer"] is False
    on = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=True,
    )
    assert specials_flags_for_round_payload(iid, False, on)["archer"] is True


def test_stealth_special_only_when_stealth_prefire_applicable(defs):
    """Stealth badge flag only when stealth_prefire_applicable, not whenever all attackers have stealth."""
    unit_defs, territory_defs = defs
    from backend.engine.definitions import UnitDefinition
    stealth_unit_id = "test_stealth_unit_badge"
    mock_ud = UnitDefinition(
        id=stealth_unit_id,
        display_name="Stealth Test",
        faction="gondor",
        archetype="infantry",
        tags=["land", "stealth"],
        attack=2,
        defense=1,
        movement=1,
        health=1,
        cost={"power": 1},
        dice=1,
    )
    ud_extended = {**unit_defs, stealth_unit_id: mock_ud}
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": stealth_unit_id, "count": 2}],
        [{"unit_id": "morannon_orc", "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    off = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, ud_extended,
        is_sea_raid=False, archer_prefire_applicable=False, stealth_prefire_applicable=False,
    )
    for spec in off.specials_attacker.values():
        assert spec.get("stealth") is False
    on = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, ud_extended,
        is_sea_raid=False, archer_prefire_applicable=False, stealth_prefire_applicable=True,
    )
    for spec in on.specials_attacker.values():
        assert spec.get("stealth") is True


def test_ram_special_only_when_ram_applicable(defs):
    """Ram flag is True only when ram_applicable (dedicated siegeworks round), not merely because the unit has ram."""
    unit_defs, territory_defs = defs
    if "battering_ram" not in unit_defs:
        pytest.skip("no battering_ram")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": "battering_ram", "count": 1}],
        [{"unit_id": "gondor_soldier", "count": 1}],
    )
    territory_def = next(iter(territory_defs.values()))
    off = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False, ram_applicable=False,
    )
    iid = att_units[0].instance_id
    assert off.specials_attacker[iid].get("ram") is False
    on = compute_battle_specials_and_modifiers(
        att_units, def_units, territory_def, unit_defs,
        is_sea_raid=False, archer_prefire_applicable=False, ram_applicable=True,
    )
    assert on.specials_attacker[iid].get("ram") is True


def test_stacks_to_synthetic_units():
    """stacks_to_synthetic_units produces correct counts and instance_id pattern."""
    att, def_ = stacks_to_synthetic_units(
        [{"unit_id": "a", "count": 2}, {"unit_id": "b", "count": 1}],
        [{"unit_id": "c", "count": 1}],
    )
    assert len(att) == 3
    assert len(def_) == 1
    assert att[0].unit_id == "a"
    assert att[2].unit_id == "b"
    assert def_[0].unit_id == "c"
    assert all(u.instance_id.startswith("att_") for u in att)
    assert all(u.instance_id.startswith("def_") for u in def_)


def test_paired_bomb_siegework_dice_when_bomb_not_siegework_archetype():
    """
    Regression: paired bombikazi + bomb must count attacker siegework dice even if the bomb
    unit_def archetype is wrong (e.g. legacy MOTW typed as archer). Otherwise no siegeworks
    round runs and bombikazi rolls in standard combat.
    """
    unit_defs, *_ = load_static_definitions(setup_id="motw_1.0")
    if "bomb" not in unit_defs or "berserker" not in unit_defs:
        pytest.skip("motw_1.0 needs bomb and berserker")
    ud = dict(unit_defs)
    ud["bomb"] = replace(unit_defs["bomb"], archetype="archer")
    att_units, def_units = stacks_to_synthetic_units(
        [
            {"unit_id": "berserker", "count": 1},
            {"unit_id": "bomb", "count": 1},
        ],
        [{"unit_id": "gondor_soldier", "count": 1}],
    )
    att_dice, _def_dice = get_siegework_dice_counts(
        att_units,
        def_units,
        ud,
        defender_territory_is_stronghold=True,
        defender_stronghold_hp=4,
        fuse_bomb=True,
    )
    assert att_dice == unit_defs["bomb"].dice


def test_defending_battering_ram_no_siegework_defender_dice(defs):
    """Defending ram is siegework but does not roll in the siegeworks round (walls are attacked, not defended)."""
    unit_defs, territory_defs = defs
    if "battering_ram" not in unit_defs or "gondor_soldier" not in unit_defs:
        pytest.skip("need battering_ram and gondor_soldier")
    minas = territory_defs.get("minas_tirith")
    if not minas or not getattr(minas, "is_stronghold", False):
        pytest.skip("need stronghold territory id minas_tirith")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": "gondor_soldier", "count": 1}],
        [{"unit_id": "battering_ram", "count": 1}],
    )
    att_d, def_d = get_siegework_dice_counts(
        att_units,
        def_units,
        unit_defs,
        defender_territory_is_stronghold=True,
        defender_stronghold_hp=4,
    )
    assert att_d == 0
    assert def_d == 0
    applies, _, _ = siegework_dice_round_applies(
        att_units,
        def_units,
        unit_defs,
        defender_territory_is_stronghold=True,
        defender_stronghold_hp=4,
    )
    assert applies is False


def test_attacker_catapult_defender_ram_still_has_attacker_siegework_dice(defs):
    """Attacker siegework rolls vs stronghold; defender ram still contributes 0 defender dice."""
    unit_defs, territory_defs = defs
    if "battering_ram" not in unit_defs or "catapult" not in unit_defs:
        pytest.skip("need battering_ram and catapult")
    if "minas_tirith" not in territory_defs:
        pytest.skip("need minas_tirith")
    att_units, def_units = stacks_to_synthetic_units(
        [{"unit_id": "catapult", "count": 1}],
        [{"unit_id": "battering_ram", "count": 1}],
    )
    att_d, def_d = get_siegework_dice_counts(
        att_units,
        def_units,
        unit_defs,
        defender_territory_is_stronghold=True,
        defender_stronghold_hp=4,
    )
    assert att_d == unit_defs["catapult"].dice
    assert def_d == 0


def _infantry(unit_id: str, specials: list[str] | None = None) -> UnitDefinition:
    return UnitDefinition(
        id=unit_id,
        display_name=unit_id,
        faction="gondor",
        archetype="infantry",
        tags=["land", "transportable"],
        attack=2,
        defense=2,
        movement=1,
        health=1,
        cost={"power": 3},
        specials=list(specials or []),
    )


def _units(prefix: str, unit_id: str, count: int) -> list[Unit]:
    return [
        Unit(
            instance_id=f"{prefix}_{i}_{unit_id}",
            unit_id=unit_id,
            remaining_movement=1,
            remaining_health=1,
            base_movement=1,
            base_health=1,
        )
        for i in range(count)
    ]


def test_king_and_captain_boost_own_faction_only():
    """Captain stays same-type. King fills same type first, then other types, own faction only."""
    ally = replace(_infantry("ally_soldier"), faction="rohan")
    cheap_archer = replace(_infantry("cheap_archer"), archetype="archer", attack=1, cost={"power": 2})
    mid_knight = replace(_infantry("mid_knight"), archetype="cavalry", attack=4, cost={"power": 4})
    weak_siege = replace(_infantry("weak_siege"), archetype="siegework", attack=1, cost={"power": 2})
    pricey = replace(_infantry("pricey"), archetype="monster", attack=6, cost={"power": 8})
    defs = {
        "king": _infantry("king", ["king"]),
        "captain": _infantry("captain", ["captain"]),
        "soldier": _infantry("soldier"),
        "ally_soldier": ally,
        "cheap_archer": cheap_archer,
        "mid_knight": mid_knight,
        "weak_siege": weak_siege,
        "pricey": pricey,
    }
    king = _units("att", "king", 1)
    soldiers = _units("sol", "soldier", 8)
    allies = _units("ally", "ally_soldier", 3)
    att_mods, _, att_sources, _ = compute_leader_stat_modifiers(king + soldiers + allies, [], defs)
    assert len(att_mods) == 5
    assert set(att_sources.values()) == {"king"}
    assert all(v == 1 for v in att_mods.values())
    assert all(iid.startswith("sol_") for iid in att_mods)
    assert not any(iid.startswith("ally_") for iid in att_mods)

    captain = _units("cap", "captain", 1)
    cap_mods, _, _, _ = compute_leader_stat_modifiers(captain + soldiers + allies, [], defs)
    assert len(cap_mods) == 3
    assert all(iid.startswith("sol_") for iid in cap_mods)

    both_mods, _, both_sources, _ = compute_leader_stat_modifiers(
        king + captain + soldiers, [], defs
    )
    assert len(both_mods) == 8
    assert all(v == 1 for v in both_mods.values())
    assert sum(1 for s in both_sources.values() if s == "king") == 5
    assert sum(1 for s in both_sources.values() if s == "captain") == 3

    others = (
        _units("arc", "cheap_archer", 1)
        + _units("knt", "mid_knight", 1)
        + _units("sw", "weak_siege", 1)
        + _units("mon", "pricey", 1)
    )
    spill_mods, _, spill_sources, _ = compute_leader_stat_modifiers(
        king + soldiers[:2] + others + allies, [], defs
    )
    assert set(spill_sources) == {
        soldiers[0].instance_id,
        soldiers[1].instance_id,
        "sw_0_weak_siege",
        "arc_0_cheap_archer",
        "knt_0_mid_knight",
    }
    assert "mon_0_pricey" not in spill_mods
    assert all(v == 1 for v in spill_mods.values())

    cap_other, _, _, _ = compute_leader_stat_modifiers(captain + others, [], defs)
    assert cap_other == {}

    result = compute_battle_specials_and_modifiers(
        king + soldiers[:6], [], None, defs
    )
    flags = specials_flags_for_round_payload(soldiers[0].instance_id, True, result)
    assert flags["king_bonus"] is True
    assert flags["captain_bonus"] is False


def _plain_unit(unit_id: str, specials: list[str], defense: int = 6) -> UnitDefinition:
    return UnitDefinition(
        id=unit_id,
        display_name=unit_id,
        faction="test",
        archetype="infantry",
        tags=[],
        attack=1,
        defense=defense,
        movement=1,
        health=1,
        cost={"power": 1},
        specials=specials,
    )


def _inst(instance_id: str, unit_id: str) -> Unit:
    return Unit(
        instance_id=instance_id,
        unit_id=unit_id,
        remaining_movement=1,
        remaining_health=1,
        base_movement=1,
        base_health=1,
    )


def test_light_cancels_all_opposing_terror():
    """One defending Light unit wipes attacker terror. Hope still subtracts one. Attacker Light does not cancel its own side."""
    unit_defs = {
        "terror": _plain_unit("terror", ["terror"]),
        "soldier": _plain_unit("soldier", []),
        "hope": _plain_unit("hope", ["hope"]),
        "light": _plain_unit("light", ["light"]),
        "attacker_light": _plain_unit("attacker_light", ["light"]),
    }
    attackers = [_inst("t1", "terror"), _inst("t2", "terror"), _inst("t3", "terror")]
    defenders = [_inst("d1", "soldier"), _inst("d2", "soldier")]
    rolls = {"defender": [1, 1]}

    rerolls, count = get_terror_reroll_targets(attackers, defenders, unit_defs, rolls, None)
    assert count == 2
    assert rerolls == [0, 1]

    hope_defenders = [_inst("d1", "hope"), _inst("d2", "soldier"), _inst("d3", "soldier")]
    _, hope_count = get_terror_reroll_targets(
        attackers, hope_defenders, unit_defs, {"defender": [1, 1, 1]}, None
    )
    assert hope_count == 2  # 3 terror - 1 hope

    light_defenders = [_inst("d1", "light"), _inst("d2", "soldier")]
    light_rerolls, light_count = get_terror_reroll_targets(
        attackers, light_defenders, unit_defs, rolls, None
    )
    assert light_rerolls == []
    assert light_count == 0

    both = [_inst("d1", "light"), _inst("d2", "hope")]
    assert get_terror_reroll_targets(attackers, both, unit_defs, rolls, None) == ([], 0)

    attackers_with_light = attackers + [_inst("a_light", "attacker_light")]
    _, still = get_terror_reroll_targets(attackers_with_light, defenders, unit_defs, rolls, None)
    assert still == 2


def test_light_badge_when_opposing_side_has_terror():
    """Light is flagged only on the side facing terror."""
    unit_defs = {
        "terror": _plain_unit("terror", ["terror"]),
        "light": _plain_unit("light", ["light"]),
        "soldier": _plain_unit("soldier", []),
    }
    attackers = [_inst("t1", "terror"), _inst("a_light", "light")]
    defenders = [_inst("d_light", "light"), _inst("d1", "soldier")]
    result = compute_battle_specials_and_modifiers(attackers, defenders, None, unit_defs)
    assert result.specials_defender["d_light"]["light"] is True
    assert result.specials_attacker["a_light"]["light"] is False
    flags = specials_flags_for_round_payload("d_light", False, result)
    assert flags["light"] is True

    swapped = compute_battle_specials_and_modifiers(
        [_inst("a_light", "light")],
        [_inst("d_terror", "terror")],
        None,
        unit_defs,
    )
    assert swapped.specials_attacker["a_light"]["light"] is True
    assert swapped.specials_defender["d_terror"]["terror"] is False
