"""
Forecast combat_move outcomes by reusing the reducer's pending-move apply (ordering, charges, captures).
Used to compare baseline (pending only) vs pending + candidate for empty exposed hex scoring.
"""

from __future__ import annotations

from backend.engine.reducer import get_state_after_combat_moves_scenario
from backend.engine.state import GameState, PendingMove
from backend.engine.utils import get_unit_faction, is_land_unit

from backend.ai.geography import exposed_empty_conquest_reinforce_need


def make_combat_pending_move(
    from_territory: str,
    to_territory: str,
    unit_instance_ids: list[str],
    *,
    charge_through: list[str] | None = None,
) -> PendingMove:
    return PendingMove(
        from_territory=str(from_territory or ""),
        to_territory=str(to_territory or ""),
        unit_instance_ids=list(unit_instance_ids),
        phase="combat_move",
        charge_through=list(charge_through or []),
    )


def our_land_unit_count_on_territory(
    state: GameState,
    territory_id: str,
    faction_id: str,
    unit_defs: dict,
) -> int:
    terr = state.territories.get(territory_id)
    if not terr or getattr(terr, "owner", None) != faction_id:
        return 0
    n = 0
    for u in getattr(terr, "units", []) or []:
        if get_unit_faction(u, unit_defs) != faction_id:
            continue
        ud = unit_defs.get(u.unit_id)
        if ud and is_land_unit(ud):
            n += 1
    return n


def empty_exposed_holes_map(
    forecast_state: GameState,
    faction_id: str,
    fd: dict,
    td: dict,
    unit_defs: dict,
) -> dict[str, float]:
    """
    Territories we own with zero of our land units but positive exposed_empty_conquest_reinforce_need.
    """
    out: dict[str, float] = {}
    for tid in forecast_state.territories or {}:
        if our_land_unit_count_on_territory(forecast_state, tid, faction_id, unit_defs) > 0:
            continue
        terr = forecast_state.territories.get(tid)
        if getattr(terr, "owner", None) != faction_id:
            continue
        need = exposed_empty_conquest_reinforce_need(
            tid, forecast_state, faction_id, fd, td, unit_defs
        )
        if need > 0:
            out[tid] = need
    return out


def forecast_state_with_extra_combat_moves(
    state: GameState,
    unit_defs: dict,
    territory_defs: dict,
    faction_defs: dict,
    extra_moves: list[PendingMove],
) -> GameState:
    return get_state_after_combat_moves_scenario(
        state, unit_defs, territory_defs, faction_defs, extra_moves
    )


def new_empty_exposed_holes_vs_baseline(
    baseline_holes: dict[str, float],
    forecast_state: GameState,
    faction_id: str,
    fd: dict,
    td: dict,
    unit_defs: dict,
) -> dict[str, float]:
    """Exposed empty holes after forecast that were not exposed empty in the baseline map."""
    full = empty_exposed_holes_map(forecast_state, faction_id, fd, td, unit_defs)
    return {tid: v for tid, v in full.items() if tid not in baseline_holes}
