"""
Combat simulation engine.

Runs many combat trials using the same resolution logic as real combat
(resolve_combat_round, prefires, terrain, casualty order, must_conquer, terror, etc.)
without mutating game state. Used to estimate P(attacker wins), P(conquer),
casualty distributions, and round counts.

Retreat: optional retreat_when_attacker_units_le; if set, after each round
when attacker unit count <= that value, the battle ends as retreat (defender holds).

Opening phase order (matches reducer): stealth prefire when all attackers have stealth
(cancels dedicated siegework and archer prefire); else dedicated siegework round when
applicable, then defender archer prefire when applicable, then standard rounds.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

CasualtyCostVarianceCategory = Literal["Predictable", "Moderate", "Unpredictable"]

# CV thresholds for casualty cost variance: < 0.4 Predictable, 0.4–0.8 Moderate, > 0.8 Unpredictable
CV_PREDICTABLE = 0.4
CV_UNPREDICTABLE = 0.8
MEAN_EPSILON = 0.5  # below this mean we use stdev-only rule


def _casualty_cost_variance_category(costs: list[float]) -> CasualtyCostVarianceCategory:
    """Bucket variance of per-trial casualty costs into Predictable / Moderate / Unpredictable."""
    if not costs:
        return "Predictable"
    n = len(costs)
    mean = sum(costs) / n
    if n < 2:
        return "Predictable"
    variance = sum((x - mean) ** 2 for x in costs) / (n - 1)
    stdev = math.sqrt(variance)
    if mean < MEAN_EPSILON:
        return "Unpredictable" if stdev > 0 else "Predictable"
    cv = stdev / mean
    if cv < CV_PREDICTABLE:
        return "Predictable"
    if cv <= CV_UNPREDICTABLE:
        return "Moderate"
    return "Unpredictable"

from backend.engine import DICE_SIDES
from backend.engine.combat import (
    RoundResult,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    compute_sea_raider_stat_modifiers,
    compute_terrain_stat_modifiers,
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_bombikazi_pairing,
    get_ladder_infantry_instance_ids,
    sort_attackers_for_ladder_dice_order,
    get_siegework_attacker_rolling_units,
    get_terror_reroll_targets,
    merge_stat_modifiers,
    resolve_archer_prefire,
    resolve_combat_round,
    resolve_siegeworks_round,
    resolve_stealth_prefire,
    siegework_dice_round_applies,
    _is_siegework_unit,
)
from backend.engine.definitions import TerritoryDefinition, UnitDefinition
from backend.engine.movement import _is_sea_zone
from backend.engine.state import Unit
from backend.engine.utils import (
    archer_prefire_eligible,
    can_conquer_territory_as_attacker,
    generate_combat_rolls_for_units,
)


# ---------------------------------------------------------------------------
# Input / output types
# ---------------------------------------------------------------------------

@dataclass
class SimOptions:
    """Options for a single battle or simulation run."""
    casualty_order_attacker: str = "best_unit"  # "best_unit" | "best_attack"
    casualty_order_defender: str = "best_unit"  # "best_unit" | "best_defense"
    must_conquer: bool = False
    max_rounds: int | None = None  # Cap rounds; if hit, treat as defender wins
    # Land combat only: passengers-from-sea scenario. Applies Sea Raider +attack to
    # units with that special. Not naval combat (no boats as attackers, no naval hit rules).
    is_sea_raid: bool = False
    retreat_when_attacker_units_le: int | None = None  # After round, if attacker count <= this, end as retreat
    stronghold_initial_hp: int | None = None  # When set, defender stronghold starts at this HP (siegeworks + normal rounds soak it)
    # True (default): bomb detonates in siegeworks and removes paired bombikazi after; False matches live fuse_bomb No
    fuse_bomb: bool = True
    # True (default): -1 to stealth and archer prefire target stats; False: 0 (setup manifest prefire_penalty).
    prefire_penalty: bool = True


@dataclass
class BattleOutcome:
    """Result of a single simulated battle."""
    winner: str  # "attacker" | "defender"
    retreat: bool  # True if ended by retreat threshold (defender holds)
    conquered: bool
    rounds: int
    attacker_survived: bool  # >0 attacker units left at end
    defender_survived: bool  # >0 defender units left at end
    attacker_casualties: dict[str, int]  # unit_id -> count lost
    defender_casualties: dict[str, int]
    attacker_prefire_hits: int = 0  # hits dealt by attacker in stealth prefire
    defender_prefire_hits: int = 0  # hits dealt by defender in archer prefire
    attacker_prefire_applicable: bool = False  # True if stealth prefire ran this battle
    defender_prefire_applicable: bool = False  # True if archer prefire ran this battle
    attacker_siegework_hits: int = 0  # hits rolled by attacker in dedicated siegeworks round (incl. stronghold)
    defender_siegework_hits: int = 0  # hits rolled by defender siegework units in that round
    siegework_round_applicable: bool = False  # True if siegework *dice* round ran (siegework_dice_round_applies)
    siegework_attacker_dice: int = 0  # attacker siegework dice count at resolution time (0 => no attacker rolls)
    siegework_defender_dice: int = 0  # defender siegework dice (excludes ladder); 0 => no defender rolls


def _stacks_to_units(
    stacks: list[dict[str, Any]],
    prefix: str,
    unit_defs: dict[str, UnitDefinition],
) -> list[Unit]:
    """Build a list of Unit instances from stacks. Each stack is { "unit_id": str, "count": int }."""
    units: list[Unit] = []
    idx = 0
    for s in stacks:
        unit_id = (s.get("unit_id") or "").strip()
        count = int(s.get("count") or 0)
        if not unit_id or count <= 0:
            continue
        ud = unit_defs.get(unit_id)
        if not ud:
            continue
        base_health = getattr(ud, "health", 1)
        base_movement = getattr(ud, "movement", 0)
        for _ in range(count):
            instance_id = f"{prefix}_{idx}"
            units.append(
                Unit(
                    instance_id=instance_id,
                    unit_id=unit_id,
                    remaining_movement=base_movement,
                    remaining_health=base_health,
                    base_movement=base_movement,
                    base_health=base_health,
                    loaded_onto=None,
                )
            )
            idx += 1
    return units


def _has_special(unit_def: UnitDefinition | None, special: str) -> bool:
    if not unit_def:
        return False
    specials = getattr(unit_def, "specials", []) or []
    return special in specials or special in getattr(unit_def, "tags", [])


def run_one_battle(
    attacker_stacks: list[dict[str, Any]],
    defender_stacks: list[dict[str, Any]],
    territory_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    options: SimOptions | None = None,
    seed: int | None = None,
) -> BattleOutcome:
    """
    Run a single combat to resolution (or retreat / max_rounds).

    Uses the same combat entry points as the reducer so rule changes are
    automatically reflected. Does not mutate any game state.

    Args:
        attacker_stacks: [{"unit_id": str, "count": int}, ...]
        defender_stacks: Same shape.
        territory_id: For terrain, stronghold, ownable.
        unit_defs, territory_defs: From load_static_definitions(setup_id=...).
        options: Casualty orders, must_conquer, max_rounds, retreat threshold, etc.
        seed: Optional RNG seed for this battle (reproducibility).

    Returns:
        BattleOutcome with winner, retreat, conquered, rounds, casualties by unit_id.
    """
    opts = options or SimOptions()
    if seed is not None:
        random.seed(seed)
    pdelta = -1 if opts.prefire_penalty else 0

    att_prefire_hits = 0
    def_prefire_hits = 0
    att_prefire_applicable = False
    def_prefire_applicable = False
    att_siegework_hits = 0
    def_siegework_hits = 0
    siegework_applicable = False
    sw_att_dice_at_resolution = 0
    sw_def_dice_at_resolution = 0

    attacker_units = _stacks_to_units(attacker_stacks, "att", unit_defs)
    defender_units = _stacks_to_units(defender_stacks, "def", unit_defs)

    instance_to_att_uid = {u.instance_id: u.unit_id for u in attacker_units}
    instance_to_def_uid = {u.instance_id: u.unit_id for u in defender_units}

    all_att_casualties: dict[str, int] = defaultdict(int)
    all_def_casualties: dict[str, int] = defaultdict(int)

    territory_def = territory_defs.get(territory_id)
    is_sea = _is_sea_zone(territory_def)
    # Naval hit assignment only for battles in a sea *zone* (ships + aerial). Sea raids
    # are land combat; is_sea_raid does not affect this flag.
    is_naval_attacker = is_sea
    is_naval_defender = is_sea

    defender_casualty_order = opts.casualty_order_defender
    casualty_order_attacker = opts.casualty_order_attacker
    must_conquer = opts.must_conquer

    round_number = 0
    ran_stealth_prefire = False

    # --- Stealth prefire: all attackers have stealth -> attackers roll at attack-1, hits to defenders;
    # cancels dedicated siegework round and defender archer prefire (same as live combat).
    if attacker_units and defender_units and all(
        _has_special(unit_defs.get(u.unit_id), "stealth") for u in attacker_units
    ):
        ran_stealth_prefire = True
        dice = generate_combat_rolls_for_units(attacker_units, defender_units, unit_defs, seed=None)
        terrain_att, terrain_def = compute_terrain_stat_modifiers(
            territory_def, attacker_units, defender_units, unit_defs
        )
        anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
            attacker_units, defender_units, unit_defs
        )
        captain_att, captain_def = compute_captain_stat_modifiers(
            attacker_units, defender_units, unit_defs
        )
        sea_raider_att, _ = compute_sea_raider_stat_modifiers(
            attacker_units, unit_defs, is_sea_raid=opts.is_sea_raid
        )
        attacker_mods = merge_stat_modifiers(
            terrain_att, anticav_att, captain_att, sea_raider_att
        )
        prefire_result = resolve_stealth_prefire(
            attacker_units,
            defender_units,
            unit_defs,
            dice.get("attacker", []),
            stat_modifiers_attacker_extra=attacker_mods,
            prefire_penalty_delta=pdelta,
        )
        att_prefire_applicable = True
        att_prefire_hits = prefire_result.attacker_hits  # stealth: attackers roll; hits = attacker_hits
        for iid in prefire_result.defender_casualties:
            uid = instance_to_def_uid.get(iid)
            if uid:
                all_def_casualties[uid] += 1
        defender_units[:] = [
            u for u in defender_units
            if u.instance_id not in set(prefire_result.defender_casualties)
        ]
        if prefire_result.defenders_eliminated:
            return BattleOutcome(
                winner="attacker",
                retreat=False,
                conquered=_can_conquer(
                    attacker_units, territory_def, unit_defs
                ),
                rounds=0,
                attacker_survived=True,
                defender_survived=False,
                attacker_casualties=dict(all_att_casualties),
                defender_casualties=dict(all_def_casualties),
                attacker_prefire_hits=att_prefire_hits,
                defender_prefire_hits=def_prefire_hits,
                attacker_prefire_applicable=att_prefire_applicable,
                defender_prefire_applicable=def_prefire_applicable,
                attacker_siegework_hits=att_siegework_hits,
                defender_siegework_hits=def_siegework_hits,
                siegework_round_applicable=siegework_applicable,
                siegework_attacker_dice=sw_att_dice_at_resolution,
                siegework_defender_dice=sw_def_dice_at_resolution,
            )

    siegeworks_occurred = False
    stronghold_hp: int | None = getattr(opts, "stronghold_initial_hp", None)
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    fuse_sim = bool(getattr(opts, "fuse_bomb", True))
    siegework_applies, siegework_att_dice, siegework_def_dice = siegework_dice_round_applies(
        attacker_units, defender_units, unit_defs, defender_territory_is_stronghold,
        defender_stronghold_hp=stronghold_hp,
        fuse_bomb=fuse_sim,
    )
    sw_att_dice_at_resolution = siegework_att_dice
    sw_def_dice_at_resolution = siegework_def_dice

    if not ran_stealth_prefire:
        # --- Siegeworks round: before archer prefire and round 1 (stronghold soaks ram/bomb etc.; overflow hits defenders). ---
        if attacker_units and defender_units and siegework_applies:
            siegeworks_occurred = True
            terrain_att, terrain_def = compute_terrain_stat_modifiers(
                territory_def, attacker_units, defender_units, unit_defs
            )
            anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            captain_att, captain_def = compute_captain_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att)
            defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)
            att_rolling = get_siegework_attacker_rolling_units(
                attacker_units, unit_defs, defender_territory_is_stronghold,
                defender_stronghold_hp=stronghold_hp,
                fuse_bomb=fuse_sim,
            )
            def_sw = [u for u in defender_units if _is_siegework_unit(unit_defs.get(u.unit_id))]
            siege_att_rolls = (
                [random.randint(1, DICE_SIDES) for _ in range(siegework_att_dice)] if siegework_att_dice > 0 else []
            )
            siege_def_rolls = [random.randint(1, DICE_SIDES) for _ in range(siegework_def_dice)] if def_sw else []
            siege_dice_rolls = {"attacker": siege_att_rolls, "defender": siege_def_rolls}
            siege_result, stronghold_hp, ladder_count = resolve_siegeworks_round(
                attacker_units,
                defender_units,
                unit_defs,
                siege_dice_rolls,
                stat_modifiers_attacker=attacker_mods or None,
                stat_modifiers_defender=defender_mods or None,
                casualty_order_attacker=casualty_order_attacker,
                casualty_order_defender=defender_casualty_order,
                defender_stronghold_hp=stronghold_hp,
                defender_territory_is_stronghold=defender_territory_is_stronghold,
                fuse_bomb=fuse_sim,
            )
            att_siegework_hits = siege_result.attacker_hits
            def_siegework_hits = siege_result.defender_hits
            siegework_applicable = True
            for iid in siege_result.attacker_casualties:
                uid = instance_to_att_uid.get(iid)
                if uid:
                    all_att_casualties[uid] += 1
            for iid in siege_result.defender_casualties:
                uid = instance_to_def_uid.get(iid)
                if uid:
                    all_def_casualties[uid] += 1
            attacker_units[:] = [u for u in attacker_units if u.instance_id in siege_result.surviving_attacker_ids]
            defender_units[:] = [u for u in defender_units if u.instance_id in siege_result.surviving_defender_ids]
            bomb_pair_casualties: list[str] = []
            if fuse_sim:
                paired_bombikazi, paired_bombs = get_bombikazi_pairing(attacker_units, unit_defs)
                bomb_pair_casualties = list(paired_bombikazi | paired_bombs)
            if bomb_pair_casualties:
                attacker_units[:] = [u for u in attacker_units if u.instance_id not in bomb_pair_casualties]
                for iid in bomb_pair_casualties:
                    uid = instance_to_att_uid.get(iid)
                    if uid:
                        all_att_casualties[uid] += 1
            if siege_result.attackers_eliminated:
                return BattleOutcome(
                    winner="defender",
                    retreat=False,
                    conquered=False,
                    rounds=0,
                    attacker_survived=False,
                    defender_survived=True,
                    attacker_casualties=dict(all_att_casualties),
                    defender_casualties=dict(all_def_casualties),
                    attacker_prefire_hits=att_prefire_hits,
                    defender_prefire_hits=def_prefire_hits,
                    attacker_prefire_applicable=att_prefire_applicable,
                    defender_prefire_applicable=def_prefire_applicable,
                    attacker_siegework_hits=att_siegework_hits,
                    defender_siegework_hits=def_siegework_hits,
                    siegework_round_applicable=siegework_applicable,
                    siegework_attacker_dice=sw_att_dice_at_resolution,
                    siegework_defender_dice=sw_def_dice_at_resolution,
                )
            if len(attacker_units) == 0:
                return BattleOutcome(
                    winner="defender",
                    retreat=False,
                    conquered=False,
                    rounds=0,
                    attacker_survived=False,
                    defender_survived=True,
                    attacker_casualties=dict(all_att_casualties),
                    defender_casualties=dict(all_def_casualties),
                    attacker_prefire_hits=att_prefire_hits,
                    defender_prefire_hits=def_prefire_hits,
                    attacker_prefire_applicable=att_prefire_applicable,
                    defender_prefire_applicable=def_prefire_applicable,
                    attacker_siegework_hits=att_siegework_hits,
                    defender_siegework_hits=def_siegework_hits,
                    siegework_round_applicable=siegework_applicable,
                    siegework_attacker_dice=sw_att_dice_at_resolution,
                    siegework_defender_dice=sw_def_dice_at_resolution,
                )
            if siege_result.defenders_eliminated:
                return BattleOutcome(
                    winner="attacker",
                    retreat=False,
                    conquered=_can_conquer(attacker_units, territory_def, unit_defs),
                    rounds=0,
                    attacker_survived=True,
                    defender_survived=False,
                    attacker_casualties=dict(all_att_casualties),
                    defender_casualties=dict(all_def_casualties),
                    attacker_prefire_hits=att_prefire_hits,
                    defender_prefire_hits=def_prefire_hits,
                    attacker_prefire_applicable=att_prefire_applicable,
                    defender_prefire_applicable=def_prefire_applicable,
                    attacker_siegework_hits=att_siegework_hits,
                    defender_siegework_hits=def_siegework_hits,
                    siegework_round_applicable=siegework_applicable,
                    siegework_attacker_dice=sw_att_dice_at_resolution,
                    siegework_defender_dice=sw_def_dice_at_resolution,
                )

        # --- Archer prefire: after siegework so surviving archers reflect casualties (e.g. bomb overflow). ---
        defender_archer_units = [u for u in defender_units if archer_prefire_eligible(unit_defs.get(u.unit_id))]
        if attacker_units and defender_archer_units:
            dice = generate_combat_rolls_for_units(attacker_units, defender_units, unit_defs, seed=None)
            terrain_att, terrain_def = compute_terrain_stat_modifiers(
                territory_def, attacker_units, defender_units, unit_defs
            )
            anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            captain_att, captain_def = compute_captain_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)
            prefire_result = resolve_archer_prefire(
                attacker_units,
                defender_archer_units,
                unit_defs,
                dice.get("defender", []),
                stat_modifiers_defender_extra=defender_mods,
                territory_def=territory_def,
                prefire_penalty_delta=pdelta,
            )
            def_prefire_applicable = True
            def_prefire_hits = prefire_result.defender_hits
            for iid in prefire_result.attacker_casualties:
                uid = instance_to_att_uid.get(iid)
                if uid:
                    all_att_casualties[uid] += 1
            if prefire_result.attackers_eliminated:
                return BattleOutcome(
                    winner="defender",
                    retreat=False,
                    conquered=False,
                    rounds=0,
                    attacker_survived=False,
                    defender_survived=True,
                    attacker_casualties=dict(all_att_casualties),
                    defender_casualties=dict(all_def_casualties),
                    attacker_prefire_hits=att_prefire_hits,
                    defender_prefire_hits=def_prefire_hits,
                    attacker_prefire_applicable=att_prefire_applicable,
                    defender_prefire_applicable=def_prefire_applicable,
                    attacker_siegework_hits=att_siegework_hits,
                    defender_siegework_hits=def_siegework_hits,
                    siegework_round_applicable=siegework_applicable,
                    siegework_attacker_dice=sw_att_dice_at_resolution,
                    siegework_defender_dice=sw_def_dice_at_resolution,
                )

    ladder_infantry_instance_ids = get_ladder_infantry_instance_ids(
        attacker_units, unit_defs,
    )

    # --- Round loop ---
    while True:
        round_number += 1

        # Retreat check: only after round 1 (game rule: cannot retreat until attackers have rolled). After prefires + round 1, if attacker count <= threshold, retreat.
        if round_number > 1 and opts.retreat_when_attacker_units_le is not None:
            if len(attacker_units) <= opts.retreat_when_attacker_units_le:
                return BattleOutcome(
                    winner="defender",
                    retreat=True,
                    conquered=False,
                    rounds=round_number - 1,
                    attacker_survived=len(attacker_units) > 0,
                    defender_survived=True,
                    attacker_casualties=dict(all_att_casualties),
                    defender_casualties=dict(all_def_casualties),
                    attacker_prefire_hits=att_prefire_hits,
                    defender_prefire_hits=def_prefire_hits,
                    attacker_prefire_applicable=att_prefire_applicable,
                    defender_prefire_applicable=def_prefire_applicable,
                    attacker_siegework_hits=att_siegework_hits,
                    defender_siegework_hits=def_siegework_hits,
                    siegework_round_applicable=siegework_applicable,
                    siegework_attacker_dice=sw_att_dice_at_resolution,
                    siegework_defender_dice=sw_def_dice_at_resolution,
                )

        if not attacker_units or not defender_units:
            break

        if opts.max_rounds is not None and round_number > opts.max_rounds:
            return BattleOutcome(
                winner="defender",
                retreat=False,
                conquered=False,
                rounds=round_number - 1,
                attacker_survived=len(attacker_units) > 0,
                defender_survived=len(defender_units) > 0,
                attacker_casualties=dict(all_att_casualties),
                defender_casualties=dict(all_def_casualties),
                attacker_prefire_hits=att_prefire_hits,
                defender_prefire_hits=def_prefire_hits,
                attacker_prefire_applicable=att_prefire_applicable,
                defender_prefire_applicable=def_prefire_applicable,
                attacker_siegework_hits=att_siegework_hits,
                defender_siegework_hits=def_siegework_hits,
                siegework_round_applicable=siegework_applicable,
                siegework_attacker_dice=sw_att_dice_at_resolution,
                siegework_defender_dice=sw_def_dice_at_resolution,
            )

        use_pair_fusion = (not siegeworks_occurred) or fuse_sim
        att_effective_dice, att_self_destruct, att_attack_override = get_attacker_effective_dice_and_bombikazi_self_destruct(
            attacker_units, unit_defs,
            use_paired_fused_siegework_rules=use_pair_fusion,
        )
        ladder_set = set(ladder_infantry_instance_ids)
        if ladder_set:
            terrain_att, terrain_def = compute_terrain_stat_modifiers(
                territory_def, attacker_units, defender_units, unit_defs
            )
            anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            captain_att, captain_def = compute_captain_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            sea_raider_att, _ = compute_sea_raider_stat_modifiers(
                attacker_units, unit_defs, is_sea_raid=opts.is_sea_raid
            )
            am = merge_stat_modifiers(
                terrain_att, anticav_att, captain_att, sea_raider_att
            )
            sort_attackers_for_ladder_dice_order(
                attacker_units, unit_defs, ladder_set, am, att_attack_override or None,
            )
        dice_rolls = generate_combat_rolls_for_units(
            attacker_units, defender_units, unit_defs, seed=None,
            attacker_effective_dice_override=att_effective_dice,
            exclude_archetypes={"siegework"},
        )

        # Round 1 terror: hope cancels terror then cap at 3 defender hit dice re-rolled
        if round_number == 1:
            terrain_att, terrain_def = compute_terrain_stat_modifiers(
                territory_def, attacker_units, defender_units, unit_defs
            )
            anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            captain_att, captain_def = compute_captain_stat_modifiers(
                attacker_units, defender_units, unit_defs
            )
            defender_mods_r1 = merge_stat_modifiers(terrain_def, anticav_def, captain_def)
            terror_indices, _ = get_terror_reroll_targets(
                attacker_units,
                defender_units,
                unit_defs,
                dice_rolls,
                defender_mods_r1,
                terror_cap=3,
                exclude_archetypes_from_rolling={"siegework"},
            )
            if terror_indices:
                defender_rolls = list(dice_rolls.get("defender", []))
                for idx in terror_indices:
                    if idx < len(defender_rolls):
                        defender_rolls[idx] = random.randint(1, DICE_SIDES)
                dice_rolls = {**dice_rolls, "defender": defender_rolls}

        terrain_att, terrain_def = compute_terrain_stat_modifiers(
            territory_def, attacker_units, defender_units, unit_defs
        )
        anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
            attacker_units, defender_units, unit_defs
        )
        captain_att, captain_def = compute_captain_stat_modifiers(
            attacker_units, defender_units, unit_defs
        )
        sea_raider_att, _ = compute_sea_raider_stat_modifiers(
            attacker_units, unit_defs, is_sea_raid=opts.is_sea_raid
        )
        attacker_mods = merge_stat_modifiers(
            terrain_att, anticav_att, captain_att, sea_raider_att
        )
        defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

        defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
        round_result, stronghold_hp = resolve_combat_round(
            attacker_units,
            defender_units,
            unit_defs,
            dice_rolls,
            stat_modifiers_attacker=attacker_mods or None,
            stat_modifiers_defender=defender_mods or None,
            attacker_effective_dice_override=att_effective_dice,
            attacker_effective_attack_override=att_attack_override or None,
            bombikazi_self_destruct_ids=att_self_destruct,
            casualty_order_attacker=casualty_order_attacker,
            casualty_order_defender=defender_casualty_order,
            must_conquer=must_conquer,
            is_naval_combat_attacker=is_naval_attacker,
            is_naval_combat_defender=is_naval_defender,
            defender_stronghold_hp=stronghold_hp,
            defender_territory_is_stronghold=defender_territory_is_stronghold,
            exclude_archetypes_from_rolling=["siegework"],
            attacker_ladder_instance_ids=set(ladder_infantry_instance_ids),
        )

        for iid in round_result.attacker_casualties:
            uid = instance_to_att_uid.get(iid)
            if uid:
                all_att_casualties[uid] += 1
        for iid in round_result.defender_casualties:
            uid = instance_to_def_uid.get(iid)
            if uid:
                all_def_casualties[uid] += 1

        attacker_units[:] = [
            u for u in attacker_units
            if u.instance_id in round_result.surviving_attacker_ids
        ]
        defender_units[:] = [
            u for u in defender_units
            if u.instance_id in round_result.surviving_defender_ids
        ]

        if round_result.attackers_eliminated:
            return BattleOutcome(
                winner="defender",
                retreat=False,
                conquered=False,
                rounds=round_number,
                attacker_survived=False,
                defender_survived=len(defender_units) > 0,
                attacker_casualties=dict(all_att_casualties),
                defender_casualties=dict(all_def_casualties),
                attacker_prefire_hits=att_prefire_hits,
                defender_prefire_hits=def_prefire_hits,
                attacker_prefire_applicable=att_prefire_applicable,
                defender_prefire_applicable=def_prefire_applicable,
                attacker_siegework_hits=att_siegework_hits,
                defender_siegework_hits=def_siegework_hits,
                siegework_round_applicable=siegework_applicable,
                siegework_attacker_dice=sw_att_dice_at_resolution,
                siegework_defender_dice=sw_def_dice_at_resolution,
            )
        if round_result.defenders_eliminated:
            return BattleOutcome(
                winner="attacker",
                retreat=False,
                conquered=_can_conquer(attacker_units, territory_def, unit_defs),
                rounds=round_number,
                attacker_survived=True,
                defender_survived=False,
                attacker_casualties=dict(all_att_casualties),
                defender_casualties=dict(all_def_casualties),
                attacker_prefire_hits=att_prefire_hits,
                defender_prefire_hits=def_prefire_hits,
                attacker_prefire_applicable=att_prefire_applicable,
                defender_prefire_applicable=def_prefire_applicable,
                attacker_siegework_hits=att_siegework_hits,
                defender_siegework_hits=def_siegework_hits,
                siegework_round_applicable=siegework_applicable,
                siegework_attacker_dice=sw_att_dice_at_resolution,
                siegework_defender_dice=sw_def_dice_at_resolution,
            )

    # Loop exited because one side was empty at start of an iteration (e.g. no units from stacks)
    if not defender_units:
        return BattleOutcome(
            winner="attacker",
            retreat=False,
            conquered=_can_conquer(attacker_units, territory_def, unit_defs),
            rounds=round_number,
            attacker_survived=len(attacker_units) > 0,
            defender_survived=False,
            attacker_casualties=dict(all_att_casualties),
            defender_casualties=dict(all_def_casualties),
            attacker_prefire_hits=att_prefire_hits,
            defender_prefire_hits=def_prefire_hits,
            attacker_prefire_applicable=att_prefire_applicable,
            defender_prefire_applicable=def_prefire_applicable,
            attacker_siegework_hits=att_siegework_hits,
            defender_siegework_hits=def_siegework_hits,
            siegework_round_applicable=siegework_applicable,
            siegework_attacker_dice=sw_att_dice_at_resolution,
            siegework_defender_dice=sw_def_dice_at_resolution,
        )
    return BattleOutcome(
        winner="defender",
        retreat=False,
        conquered=False,
        rounds=round_number,
        attacker_survived=len(attacker_units) > 0,
        defender_survived=len(defender_units) > 0,
        attacker_casualties=dict(all_att_casualties),
        defender_casualties=dict(all_def_casualties),
        attacker_prefire_hits=att_prefire_hits,
        defender_prefire_hits=def_prefire_hits,
        attacker_prefire_applicable=att_prefire_applicable,
        defender_prefire_applicable=def_prefire_applicable,
        attacker_siegework_hits=att_siegework_hits,
        defender_siegework_hits=def_siegework_hits,
        siegework_round_applicable=siegework_applicable,
        siegework_attacker_dice=sw_att_dice_at_resolution,
        siegework_defender_dice=sw_def_dice_at_resolution,
    )


def _can_conquer(
    surviving_attackers: list[Unit],
    territory_def: TerritoryDefinition | None,
    unit_defs: dict[str, UnitDefinition],
) -> bool:
    """True if attacker would conquer: has living ground unit and territory is ownable."""
    if not territory_def or not getattr(territory_def, "ownable", True):
        return False
    return any(
        can_conquer_territory_as_attacker(unit_defs.get(u.unit_id))
        for u in surviving_attackers
    )


# ---------------------------------------------------------------------------
# Monte Carlo aggregation
# ---------------------------------------------------------------------------


@dataclass
class PercentileOutcome:
    """Single battle outcome at a given percentile of attacker casualties (for UI summary)."""
    percentile: int  # 5, 25, 50, 75, 95
    winner: str  # "attacker" | "defender"
    conquered: bool
    retreat: bool
    attacker_casualties: dict[str, int]  # unit_id -> count lost
    defender_casualties: dict[str, int]


@dataclass
class SimResult:
    """Aggregated results over many trials."""
    n_trials: int
    attacker_wins: int
    defender_wins: int
    attacker_survives: int  # trials where attacker had >0 units at end
    defender_survives: int  # trials where defender had >0 units at end
    retreats: int
    conquers: int
    rounds_mean: float
    rounds_p50: int
    rounds_p90: int
    attacker_casualties_mean: dict[str, float]  # unit_id -> mean count lost
    defender_casualties_mean: dict[str, float]
    attacker_casualties_p90: dict[str, float]
    defender_casualties_p90: dict[str, float]
    attacker_prefire_hits_mean: Optional[float]  # None when no stealth prefire in this setup
    defender_prefire_hits_mean: Optional[float]  # None when no archer prefire in this setup
    attacker_siegework_hits_mean: Optional[float]  # None when no trial had attacker siegework dice
    defender_siegework_hits_mean: Optional[float]  # None when no trial had defender siegework dice
    attacker_casualty_cost_mean: float  # mean power cost of attacker casualties across trials
    defender_casualty_cost_mean: float  # mean power cost of defender casualties across trials
    attacker_casualty_cost_variance_category: CasualtyCostVarianceCategory  # Predictable / Moderate / Unpredictable
    defender_casualty_cost_variance_category: CasualtyCostVarianceCategory
    percentile_outcomes: list[PercentileOutcome]  # 10th, 30th, 50th, 70th, 90th by attacker casualties
    outcomes: list[dict[str, Any]] | None = None  # when return_outcomes=True: per-trial merge includes attacker_survived/defender_survived (>0 units left)

    @property
    def p_attacker_win(self) -> float:
        return self.attacker_wins / self.n_trials if self.n_trials else 0.0

    @property
    def p_defender_win(self) -> float:
        return self.defender_wins / self.n_trials if self.n_trials else 0.0

    @property
    def p_attacker_survives(self) -> float:
        return self.attacker_survives / self.n_trials if self.n_trials else 0.0

    @property
    def p_defender_survives(self) -> float:
        return self.defender_survives / self.n_trials if self.n_trials else 0.0

    @property
    def p_retreat(self) -> float:
        return self.retreats / self.n_trials if self.n_trials else 0.0

    @property
    def p_conquer(self) -> float:
        return self.conquers / self.n_trials if self.n_trials else 0.0


def run_simulation(
    attacker_stacks: list[dict[str, Any]],
    defender_stacks: list[dict[str, Any]],
    territory_id: str,
    unit_defs: dict[str, UnitDefinition],
    territory_defs: dict[str, TerritoryDefinition],
    n_trials: int = 1000,
    options: SimOptions | None = None,
    seed: int | None = None,
    return_outcomes: bool = False,
) -> SimResult:
    """
    Run n_trials battles and aggregate outcomes.

    Each trial uses a different RNG state (or seed + trial index if seed is set).
    """
    opts = options or SimOptions()
    attacker_wins = defender_wins = attacker_survives = defender_survives = retreats = conquers = 0
    rounds_list: list[int] = []
    att_casualties_by_trial: list[dict[str, int]] = []
    def_casualties_by_trial: list[dict[str, int]] = []
    winner_by_trial: list[str] = []
    conquered_by_trial: list[bool] = []
    retreat_by_trial: list[bool] = []
    attacker_survived_by_trial: list[bool] = []
    defender_survived_by_trial: list[bool] = []
    att_prefire_sum = 0.0
    att_prefire_count = 0
    def_prefire_sum = 0.0
    def_prefire_count = 0
    att_sw_sum = 0.0
    att_sw_count = 0
    def_sw_sum = 0.0
    def_sw_count = 0
    att_cost_sum = 0.0
    def_cost_sum = 0.0
    att_cost_by_trial: list[float] = []
    def_cost_by_trial: list[float] = []
    att_siegework_hits_by_trial: list[int] = []
    def_siegework_hits_by_trial: list[int] = []
    siegework_applicable_by_trial: list[bool] = []
    att_siegework_dice_by_trial: list[int] = []
    def_siegework_dice_by_trial: list[int] = []

    def _power_cost(casualties: dict[str, int]) -> float:
        total = 0
        for uid, count in casualties.items():
            ud = unit_defs.get(uid)
            if not ud or count <= 0:
                continue
            cost = getattr(ud, "cost", None) or {}
            if isinstance(cost, dict):
                total += count * cost.get("power", 0)
        return total

    for i in range(n_trials):
        trial_seed = (seed + i) if seed is not None else None
        outcome = run_one_battle(
            attacker_stacks,
            defender_stacks,
            territory_id,
            unit_defs,
            territory_defs,
            options=opts,
            seed=trial_seed,
        )
        if outcome.winner == "attacker":
            attacker_wins += 1
        else:
            defender_wins += 1
        if outcome.attacker_survived:
            attacker_survives += 1
        if outcome.defender_survived:
            defender_survives += 1
        if outcome.retreat:
            retreats += 1
        if outcome.conquered:
            conquers += 1
        rounds_list.append(outcome.rounds)
        att_casualties_by_trial.append(outcome.attacker_casualties)
        def_casualties_by_trial.append(outcome.defender_casualties)
        winner_by_trial.append(outcome.winner)
        conquered_by_trial.append(outcome.conquered)
        retreat_by_trial.append(outcome.retreat)
        attacker_survived_by_trial.append(outcome.attacker_survived)
        defender_survived_by_trial.append(outcome.defender_survived)
        att_c = _power_cost(outcome.attacker_casualties)
        def_c = _power_cost(outcome.defender_casualties)
        att_cost_sum += att_c
        def_cost_sum += def_c
        att_cost_by_trial.append(att_c)
        def_cost_by_trial.append(def_c)
        att_siegework_hits_by_trial.append(outcome.attacker_siegework_hits)
        def_siegework_hits_by_trial.append(outcome.defender_siegework_hits)
        siegework_applicable_by_trial.append(outcome.siegework_round_applicable)
        att_siegework_dice_by_trial.append(outcome.siegework_attacker_dice)
        def_siegework_dice_by_trial.append(outcome.siegework_defender_dice)
        if outcome.attacker_prefire_applicable:
            att_prefire_sum += outcome.attacker_prefire_hits
            att_prefire_count += 1
        if outcome.defender_prefire_applicable:
            def_prefire_sum += outcome.defender_prefire_hits
            def_prefire_count += 1
        if outcome.siegework_attacker_dice > 0:
            att_sw_sum += outcome.attacker_siegework_hits
            att_sw_count += 1
        if outcome.siegework_defender_dice > 0:
            def_sw_sum += outcome.defender_siegework_hits
            def_sw_count += 1

    rounds_by_trial = list(rounds_list)
    rounds_list.sort()
    n = len(rounds_list)
    rounds_mean = sum(rounds_list) / n if n else 0.0
    rounds_p50 = rounds_list[n // 2] if n else 0
    rounds_p90 = rounds_list[int(n * 0.9)] if n else 0

    all_att_unit_ids = set()
    for d in att_casualties_by_trial:
        all_att_unit_ids.update(d.keys())
    all_def_unit_ids = set()
    for d in def_casualties_by_trial:
        all_def_unit_ids.update(d.keys())

    att_mean: dict[str, float] = {}
    for uid in all_att_unit_ids:
        att_mean[uid] = sum(d.get(uid, 0) for d in att_casualties_by_trial) / n
    def_mean: dict[str, float] = {}
    for uid in all_def_unit_ids:
        def_mean[uid] = sum(d.get(uid, 0) for d in def_casualties_by_trial) / n

    def _p90_casualties(trials: list[dict[str, int]], unit_ids: set[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for uid in unit_ids:
            vals = sorted(d.get(uid, 0) for d in trials)
            out[uid] = vals[int(n * 0.9)] if n else 0.0
        return out

    att_p90 = _p90_casualties(att_casualties_by_trial, all_att_unit_ids)
    def_p90 = _p90_casualties(def_casualties_by_trial, all_def_unit_ids)

    # Order trials: outcome value (conquer=3, survive=2, retreat=1, defeat=0) desc, then attacker casualties asc, then defender casualties desc
    def _outcome_value(i: int) -> int:
        if conquered_by_trial[i]:
            return 3
        if retreat_by_trial[i]:
            return 1
        if winner_by_trial[i] == "attacker":
            return 2  # survive
        return 0  # defeat

    PERCENTILES = (5, 25, 50, 75, 95)
    sorted_indices = sorted(
        range(n),
        key=lambda i: (
            -_outcome_value(i),
            sum(att_casualties_by_trial[i].values()),
            -sum(def_casualties_by_trial[i].values()),
        ),
    )
    percentile_outcomes_list: list[PercentileOutcome] = []
    for p in PERCENTILES:
        idx = min(int(p / 100.0 * n), n - 1) if n else 0
        trial_i = sorted_indices[idx]
        percentile_outcomes_list.append(
            PercentileOutcome(
                percentile=p,
                winner=winner_by_trial[trial_i],
                conquered=conquered_by_trial[trial_i],
                retreat=retreat_by_trial[trial_i],
                attacker_casualties=dict(att_casualties_by_trial[trial_i]),
                defender_casualties=dict(def_casualties_by_trial[trial_i]),
            )
        )

    outcomes_list: list[dict[str, Any]] | None = None
    if return_outcomes and n > 0:
        outcomes_list = [
            {
                "winner": winner_by_trial[i],
                "conquered": conquered_by_trial[i],
                "retreat": retreat_by_trial[i],
                "rounds": rounds_by_trial[i],
                "attacker_casualties": dict(att_casualties_by_trial[i]),
                "defender_casualties": dict(def_casualties_by_trial[i]),
                "attacker_survived": attacker_survived_by_trial[i],
                "defender_survived": defender_survived_by_trial[i],
                "attacker_siegework_hits": att_siegework_hits_by_trial[i],
                "defender_siegework_hits": def_siegework_hits_by_trial[i],
                "siegework_round_applicable": siegework_applicable_by_trial[i],
                "siegework_attacker_dice": att_siegework_dice_by_trial[i],
                "siegework_defender_dice": def_siegework_dice_by_trial[i],
            }
            for i in range(n)
        ]

    att_variance_cat = _casualty_cost_variance_category(att_cost_by_trial)
    def_variance_cat = _casualty_cost_variance_category(def_cost_by_trial)

    return SimResult(
        n_trials=n_trials,
        attacker_wins=attacker_wins,
        defender_wins=defender_wins,
        attacker_survives=attacker_survives,
        defender_survives=defender_survives,
        retreats=retreats,
        conquers=conquers,
        rounds_mean=rounds_mean,
        rounds_p50=rounds_p50,
        rounds_p90=rounds_p90,
        attacker_casualties_mean=att_mean,
        defender_casualties_mean=def_mean,
        attacker_casualties_p90=att_p90,
        defender_casualties_p90=def_p90,
        attacker_prefire_hits_mean=att_prefire_sum / att_prefire_count if att_prefire_count else None,
        defender_prefire_hits_mean=def_prefire_sum / def_prefire_count if def_prefire_count else None,
        attacker_siegework_hits_mean=att_sw_sum / att_sw_count if att_sw_count else None,
        defender_siegework_hits_mean=def_sw_sum / def_sw_count if def_sw_count else None,
        attacker_casualty_cost_mean=att_cost_sum / n if n else 0.0,
        defender_casualty_cost_mean=def_cost_sum / n if n else 0.0,
        attacker_casualty_cost_variance_category=att_variance_cat,
        defender_casualty_cost_variance_category=def_variance_cat,
        percentile_outcomes=percentile_outcomes_list,
        outcomes=outcomes_list,
    )
