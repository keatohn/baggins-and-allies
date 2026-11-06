"""
L2 (issue #51 combat audit): stealth prefire must share the bombikazi-aware dice
contract with generation and every other combat round.

Generation (API main.py, AI dice.py, combat_sim) rolls stealth attackers with
get_attacker_effective_dice_and_bombikazi_self_destruct (paired bomb = 0 dice,
paired bombikazi = 1 die at the bomb's attack) and excludes siegework archetypes,
but resolve_stealth_prefire only received a ring merge — no effective dice, no
stat override, no exclusion. Shipped stealth units lack bombikazi/siegework so
nothing visibly broke, but a data change would silently shift or starve dice.

Pins:
- resolver: eff dice + bomb-attack override + siegework exclusion,
- API initiate generation: same contract (roll count == eff total, not base),
- reducer initiate: consumes exactly the generated rolls; grouping and the
  round-start display use the bomb-attack override,
- AI sample_dice_for_initiate: stealth branch before the siegeworks branch
  (matching the resolver) with the same generation contract,
- combat_sim: stealth generation and resolution share the contract.

Run: .venv/bin/pytest test/test_stealth_prefire_dice_contract.py
"""

import random
from types import SimpleNamespace

from backend.engine.actions import initiate_combat
from backend.engine.combat import (
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_siegework_dice_counts,
    resolve_stealth_prefire,
)
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import generate_dice_rolls_for_units, initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"


def _ns_unit(iid: str, unit_id: str, health: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id=iid, unit_id=unit_id, health=health, remaining_health=health,
        remaining_movement=0,
    )


def _resolver_defs() -> dict:
    # Roster: paired bombikazi + bomb + a non-bomb siegework (sw_cat) + a plain sneaker.
    # All attackers carry stealth so prefire applies; sw_cat proves the exclusion.
    return {
        "zerker": SimpleNamespace(
            id="zerker", archetype="infantry", dice=1, attack=5, defense=3,
            tags=["land"], specials=["stealth", "bombikazi"], health=1, cost={"power": 4},
        ),
        "bomb": SimpleNamespace(
            id="bomb", archetype="siegework", dice=6, attack=6, defense=0,
            tags=["land", "bomb"], specials=["stealth"], health=1, cost={"power": 6},
        ),
        "sw_cat": SimpleNamespace(
            id="sw_cat", archetype="siegework", dice=1, attack=3, defense=0,
            tags=["land"], specials=["stealth"], health=1, cost={"power": 5},
        ),
        "sneak": SimpleNamespace(
            id="sneak", archetype="infantry", dice=1, attack=1, defense=1,
            tags=["land"], specials=["stealth"], health=1, cost={"power": 1},
        ),
        "tank": SimpleNamespace(
            id="tank", archetype="infantry", dice=1, attack=1, defense=3,
            tags=["land"], specials=[], health=5, cost={"power": 10},
        ),
    }


def _resolver_attackers() -> list:
    return [
        _ns_unit("a_zerker", "zerker"),
        _ns_unit("a_bomb", "bomb"),
        _ns_unit("a_sneak", "sneak"),
        _ns_unit("a_cat", "sw_cat"),
    ]


def _resolver_defenders() -> list:
    return [_ns_unit("d_1", "tank", health=5)]


def test_resolver_stealth_prefire_honors_bombikazi_contract():
    defs = _resolver_defs()
    attackers = _resolver_attackers()
    eff, _, atk_ov = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, defs)

    # Generation contract: paired bomb rolls 0, bombikazi rolls 1 at the bomb's attack,
    # sw_cat keeps its base die until the exclusion drops it.
    assert eff == {"a_zerker": 1, "a_bomb": 0, "a_sneak": 1, "a_cat": 1}
    assert atk_ov == {"a_zerker": 6}
    contract = generate_dice_rolls_for_units(
        attackers, defs, seed=5, effective_dice_override=eff, exclude_archetypes={"siegework"},
    )
    assert len(contract) == 2  # zerker + sneak; without the exclusion sw_cat steals a roll
    assert len(generate_dice_rolls_for_units(
        attackers, defs, seed=5, effective_dice_override=eff,
    )) == 3

    # Fixed rolls, roster order (zerker, bomb, sneak, cat): zerker's die must resolve at
    # the bomb's attack 6 (own attack 5 with the prefire penalty is 4: roll 6 would miss),
    # sneak hits on 1 (penalty floors at 1), bomb and sw_cat consume nothing.
    result = resolve_stealth_prefire(
        attackers, _resolver_defenders(), defs, [6, 1],
        attacker_effective_dice_override=eff,
        effective_stat_override=atk_ov or None,
        exclude_archetypes={"siegework"},
    )
    assert result.attacker_hits == 2

    # Without the stat override the bombikazi misses at 5-1=4 -> only sneak hits.
    no_override = resolve_stealth_prefire(
        attackers, _resolver_defenders(), defs, [6, 1],
        attacker_effective_dice_override=eff,
        exclude_archetypes={"siegework"},
    )
    assert no_override.attacker_hits == 1


def _synthetic_defs(unit_defs):
    # Controlled stealth attackers (including a non-bomb siegework) + a bulky defender.
    unit_defs["sneakx"] = UnitDefinition(
        id="sneakx", display_name="Sneak X", faction="isengard", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=1, cost={"power": 1},
        dice=1, specials=["stealth"],
    )
    unit_defs["sw_cat"] = UnitDefinition(
        id="sw_cat", display_name="SW Cat", faction="isengard", archetype="siegework",
        tags=["land"], attack=3, defense=0, movement=0, health=1, cost={"power": 5},
        dice=1, specials=["stealth"],
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=1, movement=1, health=10, cost={"power": 10},
        dice=1, specials=[],
    )
    # Prefire requires EVERY attacker to have stealth; shipped bomb/berserker do not,
    # so inject it — the data-change scenario this contract must survive.
    unit_defs["bomb"].specials = list(unit_defs["bomb"].specials) + ["stealth"]
    unit_defs["berserker"].specials = list(unit_defs["berserker"].specials) + ["stealth"]
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


def _stealth_roster_specs() -> list:
    # Instance-id sort: isengard_berserker_* < isengard_bomb_* < isengard_sneakx_* <
    # isengard_sw_cat_* — the same order generation and consumption both use.
    return [
        ("isengard", "berserker"),
        ("isengard", "bomb"),
        ("isengard", "sneakx"),
        ("isengard", "sw_cat"),
    ]


def test_api_initiate_stealth_generation_and_reducer_share_contract():
    state, ud, td, fd, cd, pd = _fresh_state()
    zerker, bomb, sneak, cat = _place(state, ud, _stealth_roster_specs())
    _place(state, ud, [("rohan", "tankx")])

    from backend.api.main import _generate_initiate_combat_payload

    payload = _generate_initiate_combat_payload(state, TERRITORY, None, ud, td, fd)
    rolls = payload["dice_rolls"]["attacker"]
    assert payload["dice_rolls"]["defender"] == []
    # Generation contract: paired bomb 0 + bombikazi 1 + sneak 1, sw_cat excluded -> 2
    # dice (base dice would be 9; eff dice without the exclusion would be 3).
    assert len(rolls) == 2

    state, events = apply_action(
        state,
        initiate_combat("isengard", TERRITORY, {"attacker": list(rolls), "defender": []}),
        ud, td, fd, cd, pd,
    )
    resolved = [e for e in events if e.type == "combat_round_resolved"]
    assert len(resolved) == 1, "stealth prefire must emit exactly one round event"
    ev = resolved[0].payload
    assert ev.get("is_stealth_prefire")

    # Resolver consumed exactly the generated rolls: zerker first (roster order) at the
    # bomb's attack 6, then sneak at 1; sw_cat and bomb contribute no buckets at all.
    grouped = ev["attacker_dice"]
    assert sum(len(b["rolls"]) for b in grouped.values()) == len(rolls) == 2
    assert set(grouped.keys()) == {6, 1}
    assert grouped[6]["rolls"] == [rolls[0]]
    assert grouped[1]["rolls"] == [rolls[1]]

    expected_hits = (1 if rolls[0] <= 6 else 0) + (1 if rolls[1] <= 1 else 0)
    assert ev["attacker_hits"] == expected_hits

    # Round-start display: the paired bombikazi shows the bomb's raw attack (the
    # override replaces stat + prefire penalty, matching _count_hits).
    by_id = {u["instance_id"]: u for u in ev["attacker_units_at_start"]}
    assert by_id[zerker.instance_id]["effective_attack"] == 6
    assert by_id[sneak.instance_id]["effective_attack"] == 1  # 1-1 floored at 1
    assert by_id[cat.instance_id]["effective_attack"] == 2    # 3-1: normal prefire penalty
    assert by_id[bomb.instance_id]["siegework_archetype"] is True


def test_ai_sample_dice_for_initiate_prefers_stealth_branch_with_contract():
    from backend.ai.mcts.dice import sample_dice_for_initiate

    state, ud, td, fd, cd, pd = _fresh_state()
    attackers = _place(state, ud, _stealth_roster_specs())
    (tank,) = _place(state, ud, [("rohan", "tankx")])

    # Precondition: the roster has siegeworks pending (bomb + sw_cat) AND every attacker
    # has stealth. The resolver runs stealth first and cancels the dedicated siegeworks
    # round, so the sampler must too — it used to sample siegeworks first (7 dice).
    sw_att, sw_def = get_siegework_dice_counts(
        attackers, [tank], ud, False, defender_stronghold_hp=None, fuse_bomb=True,
    )
    assert sw_att > 0 and sw_def == 0

    result = sample_dice_for_initiate(
        state, TERRITORY, None, "isengard", ud, td, fd, rng=random.Random(0),
    )
    assert result["defender"] == []
    assert len(result["attacker"]) == 2  # stealth eff total, not the siegeworks round's 7


def test_sim_stealth_prefire_shares_generation_and_resolution_contract(monkeypatch):
    import backend.engine.combat_sim as combat_sim
    from backend.engine.combat_sim import SimOptions, run_one_battle

    ud, td, *_ = load_static_definitions(setup_id=SETUP)
    ud["berserker"].specials = list(ud["berserker"].specials) + ["stealth"]
    ud["bomb"].specials = list(ud["bomb"].specials) + ["stealth"]

    real = combat_sim.resolve_stealth_prefire
    captured: dict = {}

    def spy(*args, **kwargs):
        captured["rolls"] = list(args[3])
        captured["eff"] = kwargs.get("attacker_effective_dice_override")
        captured["stat_ov"] = kwargs.get("effective_stat_override")
        captured["excl"] = kwargs.get("exclude_archetypes")
        return real(*args, **kwargs)

    monkeypatch.setattr(combat_sim, "resolve_stealth_prefire", spy)

    out = run_one_battle(
        [{"unit_id": "berserker", "count": 1}, {"unit_id": "bomb", "count": 1}],
        [{"unit_id": "gondor_soldier", "count": 2}],
        "pelennor", ud, td, options=SimOptions(), seed=3,
    )
    assert "rolls" in captured, "sim must open with stealth prefire"
    # Generation contract: paired bomb 0 + bombikazi 1 -> 1 die (base dice would be 7).
    assert len(captured["rolls"]) == 1
    assert captured["excl"] == {"siegework"}
    assert sorted(captured["eff"].values()) == [0, 1]
    # Bombikazi resolves at the paired bomb's raw attack (not its own attack 5).
    assert list(captured["stat_ov"].values()) == [6]
    assert out.winner in ("attacker", "defender")
