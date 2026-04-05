"""
Defensive combat sim for AI reinforcement and mobilization.

For each enemy (non-allied) faction independently: all units that could reach this territory on a
combat_move are grouped; we simulate that faction attacking alone (worst-case for the defender is
the minimum P(hold) across factions).

Marginal value of reinforcing: sim P(hold) with current garrison vs with garrison + units in the
candidate move — diminishing returns when absolute hold is already high or marginal ΔP is tiny.
Garrison for these sims includes allied factions (same non-empty alliance) on the hex, not only
our units, so reinforcing ally-owned tiles is scored against the full coalition defense.
Saturation thresholds differ by stronghold / capital / default (tuned in habits, not hardcoded here).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from backend.engine.combat_sim import SimOptions, run_simulation
from backend.engine.movement import _is_sea_zone, get_reachable_territories_for_unit
from backend.engine.queries import _is_naval_unit
from backend.engine.state import Unit
from backend.engine.utils import get_unit_faction

from backend.ai.garrison import move_attacks_enemy_stack_that_threatens_origin
from backend.ai.formulas import territory_loss_cost
from backend.ai.geography import count_enemies_that_can_reach_territory_combat_move
from backend.ai.habits import (
    COMBAT_SIM_TRIALS,
    COMBAT_MOVE_HOLD_VS_COUNTERATTACK_MARGIN,
    MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE,
    MOBILIZATION_DEFENSE_SIM_TRIALS,
    MOBILIZATION_NEED_MAP_MAX_TERRITORIES,
    NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON,
    NON_COMBAT_DEFENSE_MARGINAL_HOLD_SCORE_SCALE,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_CAPITAL,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT,
    NON_COMBAT_DEFENSE_SATURATION_HOLD_STRONGHOLD,
    PURCHASE_DEFENSE_INTEREST_MAX,
    PURCHASE_DEFENSE_SIM_TRIALS,
)

if TYPE_CHECKING:
    from backend.engine.definitions import FactionDefinition, TerritoryDefinition, UnitDefinition
    from backend.engine.state import GameState


def _units_to_stacks(units: list, ud: dict[str, "UnitDefinition"]) -> list[dict]:
    counts = Counter(
        getattr(u, "unit_id", "")
        for u in units
        if getattr(u, "unit_id", "") and ud.get(getattr(u, "unit_id", ""))
    )
    return [{"unit_id": uid, "count": c} for uid, c in counts.items()]


def _our_land_defender_units(state, territory_id: str, faction_id: str, ud) -> list:
    terr = state.territories.get(territory_id)
    if not terr:
        return []
    out = []
    for u in getattr(terr, "units", []) or []:
        if get_unit_faction(u, ud) != faction_id:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
            continue
        out.append(u)
    return out


def _same_alliance_non_empty(a: str, b: str, fd: dict[str, "FactionDefinition"]) -> bool:
    """True if both factions exist and share a non-empty alliance id."""
    fa = fd.get(a)
    fb = fd.get(b)
    if not fa or not fb:
        return False
    sa = getattr(fa, "alliance", "") or ""
    sb = getattr(fb, "alliance", "") or ""
    return bool(sa) and sa == sb


def _coalition_land_defender_units(
    state,
    territory_id: str,
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    ud: dict[str, "UnitDefinition"],
) -> list:
    """
    Land defenders that fight with us on this hex: our units plus allied factions' units
    (non-empty matching alliance). Naval excluded. Used for hold sims when reinforcing
    ally-owned tiles or when coalition garrison matters.
    """
    terr = state.territories.get(territory_id)
    if not terr:
        return []
    out: list = []
    for u in getattr(terr, "units", []) or []:
        uf = get_unit_faction(u, ud)
        if not uf:
            continue
        if _is_naval_unit(ud.get(u.unit_id)):
            continue
        if uf == faction_id or _same_alliance_non_empty(uf, faction_id, fd):
            out.append(u)
    return out


def count_coalition_land_units_on_territory(
    state,
    territory_id: str,
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    ud: dict[str, "UnitDefinition"],
) -> int:
    """Land units on this hex that count toward coalition defense (us + allies)."""
    return len(_coalition_land_defender_units(state, territory_id, faction_id, fd, ud))


def _land_defenders_for_hold_sim(
    state,
    territory_id: str,
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    ud: dict[str, "UnitDefinition"],
) -> list:
    """
    Units to stack for P(hold): our garrison, or coalition (us + allies) when the tile is
    owned by an allied faction so sims match ally reinforcement semantics.
    """
    terr = state.territories.get(territory_id)
    owner = getattr(terr, "owner", None) if terr else None
    if owner and owner != faction_id and _same_alliance_non_empty(owner, faction_id, fd):
        return _coalition_land_defender_units(state, territory_id, faction_id, fd, ud)
    return _our_land_defender_units(state, territory_id, faction_id, ud)


def _sim_options_for_territory(tdef) -> SimOptions:
    if tdef and getattr(tdef, "is_stronghold", False):
        return SimOptions(
            must_conquer=True,
            casualty_order_attacker="best_attack",
            casualty_order_defender="best_unit",
        )
    return SimOptions()


def enemy_units_reaching_by_faction(
    territory_id: str,
    state: "GameState",
    defending_faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
) -> dict[str, list]:
    """
    Map enemy faction_id -> unit instances (on the board) that can reach territory_id as a
    combat_move destination next turn. One coalition per faction (not mixed).
    """
    our_fd = fd.get(defending_faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    by_f: dict[str, list] = defaultdict(list)
    saved_faction = getattr(state, "current_faction", None)
    try:
        for tid, terr in (state.territories or {}).items():
            for u in getattr(terr, "units", []) or []:
                uf = get_unit_faction(u, ud)
                if uf == defending_faction_id:
                    continue
                if uf and fd.get(uf) and getattr(fd.get(uf), "alliance", "") == our_alliance:
                    continue
                if not uf:
                    continue
                base_mov = getattr(u, "base_movement", 0) or 0
                if base_mov <= 0:
                    continue
                unit_full = Unit(
                    instance_id=u.instance_id,
                    unit_id=u.unit_id,
                    remaining_movement=base_mov,
                    remaining_health=getattr(u, "remaining_health", 1),
                    base_movement=base_mov,
                    base_health=getattr(u, "base_health", 1),
                    loaded_onto=getattr(u, "loaded_onto", None),
                )
                state.current_faction = uf
                reachable, _ = get_reachable_territories_for_unit(
                    unit_full,
                    tid,
                    state,
                    ud,
                    td,
                    fd,
                    "combat_move",
                )
                if territory_id in (reachable or {}):
                    by_f[uf].append(u)
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return dict(by_f)


def is_faction_capital_territory(
    territory_id: str, fd: dict[str, "FactionDefinition"]
) -> bool:
    return any(
        getattr(f, "capital", None) == territory_id for f in (fd or {}).values()
    )


def defense_hold_saturation_threshold(
    territory_id: str,
    td: dict[str, "TerritoryDefinition"],
    fd: dict[str, "FactionDefinition"],
) -> float:
    """
    P(hold) above which we treat the tile as defensively saturated.
    Capitals are the top defensive priority; a territory can be both capital and stronghold — we take
    the max of every applicable tier. Threshold constants live only in habits.py
    (NON_COMBAT_DEFENSE_SATURATION_HOLD_*); do not duplicate them elsewhere.
    """
    tdef = td.get(territory_id)
    if not tdef:
        return float(NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT)
    thr = float(NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT)
    if getattr(tdef, "is_stronghold", False):
        thr = max(thr, float(NON_COMBAT_DEFENSE_SATURATION_HOLD_STRONGHOLD))
    if is_faction_capital_territory(territory_id, fd):
        thr = max(thr, float(NON_COMBAT_DEFENSE_SATURATION_HOLD_CAPITAL))
    return thr


def _defender_stack_tuple(stacks: list[dict]) -> tuple[tuple[str, int], ...]:
    """Stable key for caching hold-prob sims."""
    items = []
    for s in stacks or []:
        uid = str(s.get("unit_id") or "")
        c = int(s.get("count", 0) or 0)
        if uid and c > 0:
            items.append((uid, c))
    return tuple(sorted(items))


def worst_case_defender_hold_probability(
    def_stacks: list[dict],
    territory_id: str,
    by_faction_units: dict[str, list],
    ud: dict[str, "UnitDefinition"],
    td: dict[str, "TerritoryDefinition"],
    *,
    n_trials: int | None = None,
    result_cache: dict[tuple, float | None] | None = None,
) -> float | None:
    """
    Minimum P(defender wins) when each enemy faction attacks alone with its full reaching coalition.
    None only on sim failure.
    """
    n = n_trials if n_trials is not None else COMBAT_SIM_TRIALS
    cache_key = (territory_id, _defender_stack_tuple(def_stacks), n)
    if result_cache is not None and cache_key in result_cache:
        return result_cache[cache_key]

    tdef = td.get(territory_id)
    if not tdef:
        return None
    if not by_faction_units:
        if result_cache is not None:
            result_cache[cache_key] = 1.0
        return 1.0
    has_any_attackers = any(bool(u) for u in by_faction_units.values())
    if not def_stacks:
        val = 0.0 if has_any_attackers else 1.0
        if result_cache is not None:
            result_cache[cache_key] = val
        return val

    opts = _sim_options_for_territory(tdef)
    p_min = 1.0
    ran = False
    for _fac, units in by_faction_units.items():
        if not units:
            continue
        att = _units_to_stacks(units, ud)
        if not att:
            continue
        ran = True
        try:
            sim = run_simulation(
                att,
                def_stacks,
                territory_id,
                ud,
                td,
                n_trials=n,
                options=opts,
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            if result_cache is not None:
                result_cache[cache_key] = None
            return None
        p_hold = max(0.0, min(1.0, 1.0 - float(sim.p_attacker_win)))
        p_min = min(p_min, p_hold)
    out = p_min if ran else 1.0
    if result_cache is not None:
        result_cache[cache_key] = out
    return out


def defender_stacks_after_hypothetical_move(
    state,
    from_tid: str,
    to_tid: str,
    unit_instance_ids: list[str],
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    ud: dict[str, "UnitDefinition"],
) -> list[dict]:
    """Coalition land defenders on to_tid after our land units march from from_tid -> to_tid."""
    on_to = _coalition_land_defender_units(state, to_tid, faction_id, fd, ud)
    ids_set = set(unit_instance_ids)
    extra: list = []
    if from_tid != to_tid:
        from_terr = state.territories.get(from_tid)
        if from_terr:
            for u in getattr(from_terr, "units", []) or []:
                if getattr(u, "instance_id", "") not in ids_set:
                    continue
                if get_unit_faction(u, ud) != faction_id:
                    continue
                if _is_naval_unit(ud.get(u.unit_id)):
                    continue
                extra.append(u)
    return _units_to_stacks(on_to + extra, ud)


def defender_marginal_hold_metrics(
    state,
    from_tid: str,
    to_tid: str,
    unit_instance_ids: list[str],
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    by_faction_cache: dict[str, dict[str, list]] | None = None,
    p_before_cache: dict[str, float] | None = None,
    n_trials: int | None = None,
) -> tuple[float | None, float | None, float | None]:
    """
    (p_hold_before, p_hold_after, marginal_delta) using worst-case across enemy factions.
    Nones if territory not simmable.
    """
    tdef = td.get(to_tid)
    if not tdef or _is_sea_zone(tdef):
        return (None, None, None)

    if by_faction_cache is not None and to_tid not in by_faction_cache:
        by_faction_cache[to_tid] = enemy_units_reaching_by_faction(
            to_tid, state, faction_id, fd, td, ud
        )
    bf = (
        by_faction_cache[to_tid]
        if by_faction_cache is not None
        else enemy_units_reaching_by_faction(to_tid, state, faction_id, fd, td, ud)
    )

    def_before = _units_to_stacks(
        _coalition_land_defender_units(state, to_tid, faction_id, fd, ud), ud
    )
    def_after = defender_stacks_after_hypothetical_move(
        state, from_tid, to_tid, unit_instance_ids, faction_id, fd, ud
    )

    if p_before_cache is not None and to_tid in p_before_cache:
        pb = p_before_cache[to_tid]
    else:
        pb = worst_case_defender_hold_probability(
            def_before, to_tid, bf, ud, td, n_trials=n_trials
        )
        if p_before_cache is not None and pb is not None:
            p_before_cache[to_tid] = pb

    pa = worst_case_defender_hold_probability(
        def_after, to_tid, bf, ud, td, n_trials=n_trials
    )
    if pb is None or pa is None:
        return (None, None, None)
    return (pb, pa, pa - pb)


def defender_hold_probability_after_hypothetical_departure(
    state: "GameState",
    from_tid: str,
    unit_instance_ids: list[str],
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    by_faction_cache: dict[str, dict[str, list]] | None = None,
    n_trials: int | None = None,
) -> float | None:
    """
    Worst-case P(hold) at from_tid if listed land units leave: same per-enemy-faction sims as
    reinforcement scoring, so we do not march away from a hex that collapses without those units.
    """
    tdef = td.get(from_tid)
    if not tdef or _is_sea_zone(tdef):
        return None
    if by_faction_cache is not None and from_tid not in by_faction_cache:
        by_faction_cache[from_tid] = enemy_units_reaching_by_faction(
            from_tid, state, faction_id, fd, td, ud
        )
    bf = (
        by_faction_cache[from_tid]
        if by_faction_cache is not None and from_tid in by_faction_cache
        else enemy_units_reaching_by_faction(
            from_tid, state, faction_id, fd, td, ud
        )
    )
    ids_set = set(unit_instance_ids)
    remaining = [
        u
        for u in _coalition_land_defender_units(state, from_tid, faction_id, fd, ud)
        if get_unit_faction(u, ud) != faction_id
        or getattr(u, "instance_id", "") not in ids_set
    ]
    def_stacks = _units_to_stacks(remaining, ud)
    return worst_case_defender_hold_probability(
        def_stacks, from_tid, bf, ud, td, n_trials=n_trials
    )


def defender_hold_probability_sim(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    n_trials: int | None = None,
) -> float | None:
    """
    Current worst-case P(hold) with garrison only (mobilization / absolute saturation).
    Uses coalition garrison when the territory owner is an allied faction (same alliance id).
    """
    tdef = td.get(territory_id)
    if not tdef or _is_sea_zone(tdef):
        return None
    bf = enemy_units_reaching_by_faction(
        territory_id, state, faction_id, fd, td, ud
    )
    def_stacks = _units_to_stacks(
        _land_defenders_for_hold_sim(state, territory_id, faction_id, fd, ud), ud
    )
    return worst_case_defender_hold_probability(
        def_stacks, territory_id, bf, ud, td, n_trials=n_trials
    )


def merge_combat_move_attacker_stacks(a: list[dict], b: list[dict]) -> list[dict]:
    """Merge {unit_id, count} stack dicts (combat_move committed + this move)."""
    merged: dict[str, int] = {}
    for s in (a or []) + (b or []):
        uid = s.get("unit_id")
        n = int(s.get("count", 0) or 0)
        if uid and n > 0:
            merged[uid] = merged.get(uid, 0) + n
    return [{"unit_id": k, "count": v} for k, v in merged.items()]


def holding_origin_beats_counterattack_into_threat(
    state: "GameState",
    from_tid: str,
    to_tid: str,
    attacker_stacks_combined: list[dict],
    defender_stacks_target: list[dict],
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    margin: float | None = None,
    n_trials: int | None = None,
) -> bool:
    """
    True if staying on from_tid (worst-case P hold vs enemies that can reach it) is better by
    ``margin`` than winning as attacker into to_tid with attacker_stacks_combined vs
    defender_stacks_target. Only applies when to_tid holds part of the stack that threatens
    from_tid (same condition as garrison threat relief).
    """
    if not move_attacks_enemy_stack_that_threatens_origin(
        from_tid, to_tid, state, faction_id, fd, td, ud
    ):
        return False
    if not attacker_stacks_combined or not defender_stacks_target:
        return False
    by_f = enemy_units_reaching_by_faction(
        from_tid, state, faction_id, fd, td, ud
    )
    def_origin = _units_to_stacks(
        _coalition_land_defender_units(state, from_tid, faction_id, fd, ud), ud
    )
    m = (
        float(COMBAT_MOVE_HOLD_VS_COUNTERATTACK_MARGIN)
        if margin is None
        else float(margin)
    )
    p_hold = worst_case_defender_hold_probability(
        def_origin, from_tid, by_f, ud, td, n_trials=n_trials
    )
    if p_hold is None:
        return False
    tdef_to = td.get(to_tid)
    opts = _sim_options_for_territory(tdef_to)
    n = n_trials if n_trials is not None else COMBAT_SIM_TRIALS
    try:
        sim_att = run_simulation(
            attacker_stacks_combined,
            defender_stacks_target,
            to_tid,
            ud,
            td,
            n_trials=n,
            options=opts,
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return False
    p_attack = float(sim_att.p_attacker_win)
    return p_hold > p_attack + m


def reinforce_value_from_marginal_defense_sim(
    base_static: float,
    p_before: float | None,
    marginal: float | None,
    territory_id: str,
    td: dict[str, "TerritoryDefinition"],
    fd: dict[str, "FactionDefinition"],
) -> float:
    """
    Turn destination base priority × sim marginal ΔP(hold), with saturation by tile class and
    marginal epsilon (habits). Falls back to base_static alone if sim missing.
    """
    thr = defense_hold_saturation_threshold(territory_id, td, fd)
    eps = float(NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON)
    scale = float(NON_COMBAT_DEFENSE_MARGINAL_HOLD_SCORE_SCALE)

    if p_before is None or marginal is None:
        return base_static

    if p_before >= thr:
        return base_static * 0.02
    if marginal <= eps:
        return base_static * 0.02
    # Diminishing absolute headroom: less urge to reinforce when already near thr even if marginal>eps
    headroom = max(0.0, (thr - p_before) / max(thr, 1e-9))
    return base_static * marginal * scale * headroom


def marginal_hold_delta_add_land_unit(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    extra_unit_id: str,
    *,
    phantom_defender_stacks: list[dict] | None = None,
    by_faction_cache: dict[str, dict[str, list]] | None = None,
    n_trials: int | None = None,
    hold_prob_cache: dict[tuple, float | None] | None = None,
) -> float:
    """
    Worst-case ΔP(hold) if one land unit of extra_unit_id joins the garrison (purchase heuristic).
    ``phantom_defender_stacks`` merges hypothetical units already allocated earlier in the same
    purchase batch (iterative re-evaluation).
    Returns 0 if not simmable, naval unit, or sea territory.
    """
    tdef = td.get(territory_id)
    if not tdef or _is_sea_zone(tdef):
        return 0.0
    if _is_naval_unit(ud.get(extra_unit_id)):
        return 0.0
    if by_faction_cache is not None and territory_id not in by_faction_cache:
        by_faction_cache[territory_id] = enemy_units_reaching_by_faction(
            territory_id, state, faction_id, fd, td, ud
        )
    bf = (
        by_faction_cache[territory_id]
        if by_faction_cache is not None and territory_id in by_faction_cache
        else enemy_units_reaching_by_faction(
            territory_id, state, faction_id, fd, td, ud
        )
    )
    live = _units_to_stacks(
        _our_land_defender_units(state, territory_id, faction_id, ud), ud
    )
    def_before = merge_combat_move_attacker_stacks(
        live, phantom_defender_stacks or []
    )
    def_after = merge_combat_move_attacker_stacks(
        def_before, [{"unit_id": extra_unit_id, "count": 1}]
    )
    n = n_trials if n_trials is not None else PURCHASE_DEFENSE_SIM_TRIALS
    pb = worst_case_defender_hold_probability(
        def_before,
        territory_id,
        bf,
        ud,
        td,
        n_trials=n,
        result_cache=hold_prob_cache,
    )
    pa = worst_case_defender_hold_probability(
        def_after,
        territory_id,
        bf,
        ud,
        td,
        n_trials=n,
        result_cache=hold_prob_cache,
    )
    if pb is None or pa is None:
        return 0.0
    return max(0.0, float(pa) - float(pb))


def purchase_defense_interest_territories(
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    max_territories: int = PURCHASE_DEFENSE_INTEREST_MAX,
    n_trials: int | None = None,
) -> list[str]:
    """
    Our land territories under next-turn combat_move threat, ranked by expected loss
    (territory_loss_cost × P(attacker wins worst single-faction coalition)).
    Ally territories are excluded: purchases mobilize only onto our camps/ports/home.
    """
    n = n_trials if n_trials is not None else PURCHASE_DEFENSE_SIM_TRIALS
    rows: list[tuple[str, float]] = []
    for tid, terr in (state.territories or {}).items():
        if getattr(terr, "owner", None) != faction_id:
            continue
        tdef = td.get(tid)
        if not tdef or _is_sea_zone(tdef):
            continue
        if count_enemies_that_can_reach_territory_combat_move(
            tid, state, faction_id, fd, td, ud
        ) <= 0:
            continue
        p_hold = defender_hold_probability_sim(
            tid, state, faction_id, fd, td, ud, n_trials=n
        )
        if p_hold is None:
            continue
        vuln = max(0.0, min(1.0, 1.0 - float(p_hold)))
        loss = territory_loss_cost(tid, td, fd) * vuln
        if loss > 1e-9:
            rows.append((tid, loss))
    rows.sort(key=lambda x: -x[1])
    return [t for t, _ in rows[:max_territories]]


def defense_expected_loss_by_territory(
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    ud: dict[str, "UnitDefinition"],
    *,
    max_territories: int = MOBILIZATION_NEED_MAP_MAX_TERRITORIES,
    n_trials: int | None = None,
) -> dict[str, float]:
    """
    Map territory_id -> expected loss for mobilization proximity scoring.
    Includes allied faction land under threat (weighted by MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE);
    purchase interest list stays our territories only.
    """
    n = n_trials if n_trials is not None else MOBILIZATION_DEFENSE_SIM_TRIALS
    rows: list[tuple[str, float]] = []
    for tid, terr in (state.territories or {}).items():
        owner = getattr(terr, "owner", None)
        if not owner:
            continue
        if owner != faction_id and not _same_alliance_non_empty(owner, faction_id, fd):
            continue
        tdef = td.get(tid)
        if not tdef or _is_sea_zone(tdef):
            continue
        if count_enemies_that_can_reach_territory_combat_move(
            tid, state, faction_id, fd, td, ud
        ) <= 0:
            continue
        p_hold = defender_hold_probability_sim(
            tid, state, faction_id, fd, td, ud, n_trials=n
        )
        if p_hold is None:
            continue
        vuln = max(0.0, min(1.0, 1.0 - float(p_hold)))
        loss = territory_loss_cost(tid, td, fd) * vuln
        if owner != faction_id:
            loss *= MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE
        if loss > 1e-9:
            rows.append((tid, loss))
    rows.sort(key=lambda x: -x[1])
    out: dict[str, float] = {}
    for tid, loss in rows[:max_territories]:
        out[tid] = loss
    return out
