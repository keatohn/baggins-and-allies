"""
Combat simulation engine.

Runs many combat trials using the same resolution logic as real combat
(resolve_combat_round, prefires, terrain, casualty order, must_conquer, terror, etc.)
without mutating game state. Used to estimate P(attacker wins), P(conquer),
casualty distributions, and round counts.

Everything is sourced from the combat engine's single-source helpers so the sim and
live combat can never drift:
- units: combat_specials.stacks_to_synthetic_units (same builder as the API battle context)
- effective stats + applied specials: combat_specials.compute_battle_specials_and_modifiers
- prefires / dedicated rounds / eligibility: combat resolvers + utils.has_unit_special,
  archer_prefire_eligible, get_siegework_dice_counts, get_ladder_infantry_instance_ids
- strongholds: default wall HP from the territory definition (live always soaks)

Retreat: optional retreat_when_attacker_units_le; if set, after each round
when attacker unit count <= that value, the battle ends as retreat (defender holds).

Opening phase order (matches reducer): stealth prefire when all attackers have stealth
(cancels the dedicated siegeworks round and defender archer prefire); else dedicated
siegework round when applicable, then defender archer prefire when applicable, then
standard rounds.
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

# Hard cap on rounds per battle so degenerate matchups (e.g. both sides attack 0)
# cannot loop forever. A battle hitting the cap is scored as a defender hold.
DEFAULT_MAX_ROUNDS = 500


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
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_bombikazi_pairing,
    get_ladder_infantry_instance_ids,
    sort_attackers_for_ladder_dice_order,
    get_siegework_attacker_rolling_units,
    get_siegework_defender_rolling_units,
    get_terror_reroll_targets,
    resolve_archer_prefire,
    resolve_combat_round,
    resolve_siegeworks_round,
    resolve_stealth_prefire,
    siegework_dice_round_applies,
)
from backend.engine.combat_specials import (
    compute_battle_specials_and_modifiers,
    stacks_to_synthetic_units,
)
from backend.engine.definitions import TerritoryDefinition, UnitDefinition
from backend.engine.movement import _is_sea_zone, _is_water_zone
from backend.engine.rings import (
    hp_shield_from_boosts,
    merge_ring_dice,
    merge_ring_stat_mods,
)
from backend.engine.state import Unit
from backend.engine.utils import (
    archer_prefire_eligible,
    can_conquer_territory_as_attacker,
    generate_combat_rolls_for_units,
    generate_dice_rolls_for_units,
    has_unit_special,
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
    # Cap rounds; if hit, treat as defender wins. None (or omitted) = DEFAULT_MAX_ROUNDS —
    # a battle can never loop forever (e.g. both sides attack 0).
    max_rounds: int | None = DEFAULT_MAX_ROUNDS
    # Land combat only: passengers-from-sea scenario. Applies Sea Raider +attack to
    # units with that special. Not naval combat (no boats as attackers, no naval hit rules).
    is_sea_raid: bool = False
    retreat_when_attacker_units_le: int | None = None  # After round, if attacker count <= this, end as retreat
    # Defender stronghold starts at this HP (siegeworks + normal rounds soak it).
    # None (or omitted) defaults to the territory's stronghold_base_health — live combat
    # always soaks; explicit 0 still means "walls down".
    stronghold_initial_hp: int | None = None
    # True (default): bomb detonates in siegeworks and removes paired bombikazi after; False matches live fuse_bomb No
    fuse_bomb: bool = True
    # True (default): -1 to stealth and archer prefire target stats; False: 0 (setup manifest prefire_penalty).
    prefire_penalty: bool = True
    # Optional sim-only boosts: {"unit_id", "attack", "defense", "dice", "hp", "side"?}.
    # Applied per bearer instance on the given side (rings.combat_boosts parity) — never
    # baked into shared unit defs, so the enemy copy of the same unit_id is not boosted.
    ring_boosts: list[dict[str, Any]] | None = None


def sim_stronghold_initial_hp(state: Any, territory_id: str, tdef: Any) -> int | None:
    """Wall HP a sim should soak against for `territory_id` (live combat parity).

    Repairs raise `stronghold_current_health`, prior siegeworks rounds lower it, so a
    partially battered (or already breached) wall must not be simulated at full base HP.
    Falls back to the territory's base health when the state carries no value; None when
    the territory is not a simmable stronghold (SimOptions then applies its own default).
    """
    if not tdef or not getattr(tdef, "is_stronghold", False):
        return None
    base = int(getattr(tdef, "stronghold_base_health", 0) or 0)
    if base <= 0:
        return None
    territories = getattr(state, "territories", None) or {}
    terr = territories.get(territory_id)
    cur = getattr(terr, "stronghold_current_health", None) if terr is not None else None
    try:
        cur = int(cur) if cur is not None else None
    except (TypeError, ValueError):
        cur = None
    return cur if cur is not None else base


def sim_prefire_penalty(state: Any) -> bool:
    """Setup manifest `prefire_penalty` for sims (live parity): True => -1 on prefire stats."""
    return bool(getattr(state, "prefire_penalty", True))


def ring_instance_boosts(
    ring_boosts: list[dict[str, Any]] | None,
    attacker_units: list[Unit],
    defender_units: list[Unit],
) -> tuple[dict[str, tuple[int, int, int, int]], dict[str, tuple[int, int, int, int]]]:
    """Map sim ring-boost rows to per-instance boosts, scoped to the bearer's side.

    Row shape: {unit_id, attack, defense, dice, hp, side?} — the same tuple parts as
    live `rings.combat_boosts`: (attack, defense, extra dice, extra hp) per instance.

    `side` ("attacker" | "defender", case-insensitive) restricts the boost to that
    side's instances of unit_id — live combat only boosts the ring's bearer instance,
    never the enemy copy of the same unit type. Any other/omitted side keeps the
    legacy behavior: every matching instance on either side (only ambiguous when the
    same unit_id is stacked on both sides).

    Returns (attacker_boosts, defender_boosts): instance_id -> (atk, def, dice, hp).
    """
    att: dict[str, tuple[int, int, int, int]] = {}
    dfn: dict[str, tuple[int, int, int, int]] = {}
    if not ring_boosts:
        return att, dfn
    for row in ring_boosts:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("unit_id") or "").strip()
        if not uid:
            continue

        def _n(key: str, source: dict[str, Any] = row) -> int:
            try:
                return max(0, int(source.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        parts = (_n("attack"), _n("defense"), _n("dice"), _n("hp"))
        if parts == (0, 0, 0, 0):
            continue
        side = str(row.get("side") or "").strip().lower()
        if side in ("attacker", "attack", "att"):
            targets: list[tuple[list[Unit], dict[str, tuple[int, int, int, int]]]] = [
                (attacker_units, att),
            ]
        elif side in ("defender", "defense", "def"):
            targets = [(defender_units, dfn)]
        else:
            targets = [(attacker_units, att), (defender_units, dfn)]
        for units, out in targets:
            for u in units:
                if u.unit_id != uid:
                    continue
                prev = out.get(u.instance_id, (0, 0, 0, 0))
                out[u.instance_id] = tuple(prev[i] + parts[i] for i in range(4))
    return att, dfn


def _ring_mods(
    mods: dict[str, int],
    boosts: dict[str, tuple[int, int, int, int]],
    *,
    attacking: bool,
) -> dict[str, int]:
    """Merge ring stat boosts into specials mods (same slot rule as the reducer)."""
    if not boosts:
        return mods
    return merge_ring_stat_mods(mods, boosts, attacking=attacking)


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
    # Private per-battle RNG: never seeds or reads the global `random` module, so
    # concurrent simulations cannot bleed state into each other (or into the AI),
    # while `seed` stays fully reproducible (Random(seed) inits identically to
    # random.seed(seed), so seeded trial sequences are unchanged).
    rng = random.Random(seed)
    pdelta = -1 if opts.prefire_penalty else 0
    max_rounds_cap = opts.max_rounds if opts.max_rounds is not None else DEFAULT_MAX_ROUNDS

    att_prefire_hits = 0
    def_prefire_hits = 0
    att_prefire_applicable = False
    def_prefire_applicable = False
    att_siegework_hits = 0
    def_siegework_hits = 0
    siegework_applicable = False
    sw_att_dice_at_resolution = 0
    sw_def_dice_at_resolution = 0

    attacker_units, defender_units = stacks_to_synthetic_units(
        attacker_stacks, defender_stacks, unit_defs,
    )

    # Ring boosts: per-instance, side-scoped (live rings.combat_boosts parity). The HP
    # part is a hit-absorbing shield (hp_shield), not extra max health — computed once
    # and depleted in place across phases, same as combat.ring_hp_shield in the reducer.
    ring_att_boosts, ring_def_boosts = ring_instance_boosts(
        opts.ring_boosts, attacker_units, defender_units,
    )
    ring_hp_shield = hp_shield_from_boosts({**ring_att_boosts, **ring_def_boosts})

    instance_to_att_uid = {u.instance_id: u.unit_id for u in attacker_units}
    instance_to_def_uid = {u.instance_id: u.unit_id for u in defender_units}

    all_att_casualties: dict[str, int] = defaultdict(int)
    all_def_casualties: dict[str, int] = defaultdict(int)

    territory_def = territory_defs.get(territory_id)
    is_sea = _is_water_zone(territory_def)
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
    # cancels the dedicated siegework round and defender archer prefire (same as live combat).
    if attacker_units and defender_units and all(
        has_unit_special(unit_defs.get(u.unit_id), "stealth") for u in attacker_units
    ):
        ran_stealth_prefire = True
        stealth_eff_dice, _, stealth_attack_ov = get_attacker_effective_dice_and_bombikazi_self_destruct(
            attacker_units, unit_defs,
            use_paired_fused_siegework_rules=True,
        )
        stealth_eff_dice = merge_ring_dice(
            stealth_eff_dice, attacker_units, unit_defs, ring_att_boosts,
        )
        dice = generate_combat_rolls_for_units(
            attacker_units, defender_units, unit_defs,
            attacker_effective_dice_override=stealth_eff_dice,
            exclude_archetypes={"siegework"},
            rng=rng,
        )
        spec_stealth = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=opts.is_sea_raid,
            stealth_prefire_applicable=True,
        )
        attacker_mods = _ring_mods(
            spec_stealth.stat_modifiers_attacker, ring_att_boosts, attacking=True,
        )
        prefire_result = resolve_stealth_prefire(
            attacker_units,
            defender_units,
            unit_defs,
            dice.get("attacker", []),
            stat_modifiers_attacker_extra=attacker_mods,
            prefire_penalty_delta=pdelta,
            hp_shield=ring_hp_shield,
            attacker_effective_dice_override=stealth_eff_dice,
            effective_stat_override=stealth_attack_ov or None,
            exclude_archetypes={"siegework"},
            casualty_order_defender=defender_casualty_order,
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
    defender_territory_is_stronghold = bool(territory_def and getattr(territory_def, "is_stronghold", False))
    stronghold_hp: int | None = getattr(opts, "stronghold_initial_hp", None)
    if stronghold_hp is None and defender_territory_is_stronghold:
        # Live combat always soaks against the territory's wall HP; default when the
        # caller didn't pass one (explicit 0 still means "walls down").
        base_wall_hp = int(getattr(territory_def, "stronghold_base_health", 0) or 0)
        if base_wall_hp > 0:
            stronghold_hp = base_wall_hp
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
            spec_sw = compute_battle_specials_and_modifiers(
                attacker_units,
                defender_units,
                territory_def,
                unit_defs,
                is_sea_raid=opts.is_sea_raid,
                ram_applicable=True,
            )
            attacker_mods = _ring_mods(
                spec_sw.stat_modifiers_attacker, ring_att_boosts, attacking=True,
            )
            defender_mods = _ring_mods(
                spec_sw.stat_modifiers_defender, ring_def_boosts, attacking=False,
            )
            att_rolling = get_siegework_attacker_rolling_units(
                attacker_units, unit_defs, defender_territory_is_stronghold,
                defender_stronghold_hp=stronghold_hp,
                fuse_bomb=fuse_sim,
            )
            def_sw = get_siegework_defender_rolling_units(defender_units, unit_defs)
            # Ring-only dice override: the siegeworks round rolls base dice for paired
            # bombs (unlike att_effective_dice, which zeroes them) — same as the reducer.
            ring_att_siege_dice = merge_ring_dice(
                None, attacker_units, unit_defs, ring_att_boosts,
            )
            ring_def_siege_dice = merge_ring_dice(
                None, defender_units, unit_defs, ring_def_boosts,
            )
            # Roll counts = base siegework dice + ring bonus dice on the rolling units
            # (live expands the roll list the same way before resolution).
            att_siege_extra = sum(
                ring_att_boosts.get(u.instance_id, (0, 0, 0, 0))[2] for u in att_rolling
            )
            def_siege_extra = sum(
                ring_def_boosts.get(u.instance_id, (0, 0, 0, 0))[2] for u in def_sw
            )
            siege_att_total = siegework_att_dice + att_siege_extra
            siege_def_total = siegework_def_dice + def_siege_extra
            siege_att_rolls = (
                [rng.randint(1, DICE_SIDES) for _ in range(siege_att_total)]
                if siege_att_total > 0 else []
            )
            siege_def_rolls = (
                [rng.randint(1, DICE_SIDES) for _ in range(siege_def_total)]
                if def_sw else []
            )
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
                hp_shield=ring_hp_shield,
                attacker_effective_dice_override=ring_att_siege_dice,
                defender_effective_dice_override=ring_def_siege_dice,
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
                bomb_pair_casualties = sorted(paired_bombikazi | paired_bombs)
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
            ring_def_dice = merge_ring_dice(
                None, defender_units, unit_defs, ring_def_boosts,
            )
            dice = generate_combat_rolls_for_units(
                attacker_units, defender_units, unit_defs,
                defender_effective_dice_override=ring_def_dice,
                rng=rng,
            )
            spec_archer = compute_battle_specials_and_modifiers(
                attacker_units,
                defender_units,
                territory_def,
                unit_defs,
                is_sea_raid=opts.is_sea_raid,
                archer_prefire_applicable=True,
            )
            defender_mods = _ring_mods(
                spec_archer.stat_modifiers_defender, ring_def_boosts, attacking=False,
            )
            # resolve_archer_prefire applies the stronghold/fortress penalty exemption
            # (archer special) to this delta internally — same contract as the reducer.
            prefire_result = resolve_archer_prefire(
                attacker_units,
                defender_archer_units,
                unit_defs,
                dice.get("defender", []),
                stat_modifiers_defender_extra=defender_mods,
                territory_def=territory_def,
                prefire_penalty_delta=pdelta,
                hp_shield=ring_hp_shield,
                defender_effective_dice_override=ring_def_dice,
                casualty_order_attacker=casualty_order_attacker,
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

        if round_number > max_rounds_cap:
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

        # Effective stats + specials for this round from the combat engine's single source
        # (terrain, anti-cavalry, leader, sea raider — recomputed fresh each round like the reducer).
        spec_round = compute_battle_specials_and_modifiers(
            attacker_units,
            defender_units,
            territory_def,
            unit_defs,
            is_sea_raid=opts.is_sea_raid,
        )
        attacker_mods = _ring_mods(
            spec_round.stat_modifiers_attacker, ring_att_boosts, attacking=True,
        )
        defender_mods = _ring_mods(
            spec_round.stat_modifiers_defender, ring_def_boosts, attacking=False,
        )

        use_pair_fusion = (not siegeworks_occurred) or fuse_sim
        att_effective_dice, att_self_destruct, att_attack_override = get_attacker_effective_dice_and_bombikazi_self_destruct(
            attacker_units, unit_defs,
            use_paired_fused_siegework_rules=use_pair_fusion,
        )
        att_effective_dice = merge_ring_dice(
            att_effective_dice, attacker_units, unit_defs, ring_att_boosts,
        )
        ring_def_dice = merge_ring_dice(
            None, defender_units, unit_defs, ring_def_boosts,
        )
        # Ladder climbers are re-derived every round (same as live combat): members die
        # between rounds, so a stale set would misroute wall-bypass hits.
        ladder_set = set(get_ladder_infantry_instance_ids(attacker_units, unit_defs))
        if ladder_set:
            sort_attackers_for_ladder_dice_order(
                attacker_units, unit_defs, ladder_set, attacker_mods, att_attack_override or None,
            )
        dice_rolls = generate_combat_rolls_for_units(
            attacker_units, defender_units, unit_defs,
            attacker_effective_dice_override=att_effective_dice,
            defender_effective_dice_override=ring_def_dice,
            exclude_archetypes={"siegework"},
            rng=rng,
        )

        # Round 1 terror: light cancels all opposing terror, else hope cancels 1 each, then cap at 3 defender hit dice re-rolled
        if round_number == 1:
            terror_indices, _ = get_terror_reroll_targets(
                attacker_units,
                defender_units,
                unit_defs,
                dice_rolls,
                defender_mods,
                terror_cap=3,
                exclude_archetypes_from_rolling={"siegework"},
            )
            if terror_indices:
                defender_rolls = list(dice_rolls.get("defender", []))
                for idx in terror_indices:
                    if idx < len(defender_rolls):
                        defender_rolls[idx] = rng.randint(1, DICE_SIDES)
                dice_rolls = {**dice_rolls, "defender": defender_rolls}

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
            exclude_archetypes_from_rolling=["siegework"],
            attacker_ladder_instance_ids=ladder_set,
            defender_effective_dice_override=ring_def_dice,
            hp_shield=ring_hp_shield,
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

    n_trials is clamped to >= 1: zero trials would crash the percentile aggregation
    (empty trial list) and matches the API's own max(1, ...) clamp.
    """
    n_trials = max(1, int(n_trials))
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
    for uid in sorted(all_att_unit_ids):
        att_mean[uid] = sum(d.get(uid, 0) for d in att_casualties_by_trial) / n
    def_mean: dict[str, float] = {}
    for uid in sorted(all_def_unit_ids):
        def_mean[uid] = sum(d.get(uid, 0) for d in def_casualties_by_trial) / n

    def _p90_casualties(trials: list[dict[str, int]], unit_ids: set[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for uid in sorted(unit_ids):
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
