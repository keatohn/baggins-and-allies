"""
Decide one action for the current phase. Dispatches to phase-specific policies.
Returns an Action (or end_phase / end_turn / skip_turn when appropriate).
"""

import os

from backend.engine.actions import Action, continue_combat, end_phase, mobilize_units

from backend.ai.context import AIContext
from backend.ai.strategic_context import build_strategic_turn_context
from backend.ai.purchase import decide_purchase
from backend.ai.combat import decide_combat, decide_initiate_combat
from backend.ai.mobilization import decide_mobilization
from backend.ai.combat_move import decide_combat_move
from backend.ai.non_combat_move import decide_non_combat_move
from backend.engine.special_rules import territory_defs_with_current_power


def _mcts_enabled() -> bool:
    """True when AI_ALGORITHM=mcts routes purchase, combat, combat_move, non_combat_move, and mobilization through MCTS."""
    return (os.environ.get("AI_ALGORITHM") or "heuristic").strip().lower() == "mcts"


def _ensure_strategic(ctx: AIContext) -> None:
    """Build the shared strategic context once, only when a policy that reads it runs.

    Only the heuristic purchase/move/mobilization policies read ctx.strategic
    (combat and all MCTS Problems do not), so MCTS paths skip the ~2s build.
    """
    if ctx.strategic is None:
        ctx.strategic = build_strategic_turn_context(ctx)


def decide(ctx: AIContext) -> Action | None:
    """
    Return the next action for the current faction and phase, or None if the AI
    has no action (caller may then end_phase / skip_turn).
    All returned actions are intended to be validated by the engine before apply.
    """
    if not ctx.state or not ctx.faction_id:
        return None
    if ctx.state.winner:
        return None

    ctx.territory_defs = territory_defs_with_current_power(ctx.territory_defs, ctx.state)

    phase = ctx.phase

    if phase == "purchase":
        if _mcts_enabled():
            from backend.ai.mcts.purchase import decide_purchase_mcts

            action = decide_purchase_mcts(ctx)
            if action is not None:
                return action
            # Fall through to heuristic if MCTS returned nothing (safety net).
        _ensure_strategic(ctx)
        return decide_purchase(ctx)

    if phase == "combat":
        if _mcts_enabled():
            from backend.ai.mcts.combat import decide_combat_mcts

            action = decide_combat_mcts(ctx)
            if action is not None:
                return action
            # Fall through to heuristic if MCTS returned nothing (safety net).

        if ctx.state.active_combat:
            action = decide_combat(ctx)
            if action is not None:
                return action
            # end_phase is invalid while active_combat exists; advance the battle with empty dice (API fills for AI)
            return continue_combat(
                ctx.faction_id,
                dice_rolls={"attacker": [], "defender": []},
            )

        # No active combat: must initiate one of the declared battles if any
        combat_territories = (ctx.available_actions or {}).get("combat_territories") or []
        if combat_territories:
            action = decide_initiate_combat(ctx)
            if action is not None:
                return action
        return end_phase(ctx.faction_id)

    if phase == "combat_move":
        if _mcts_enabled():
            from backend.ai.mcts.combat_move import decide_combat_move_mcts

            action = decide_combat_move_mcts(ctx)
            if action is not None:
                return action
            # Fall through to heuristic if MCTS returned nothing (safety net).

        _ensure_strategic(ctx)
        action = decide_combat_move(ctx)
        if action is not None:
            return action
        # Only end phase when allowed (e.g. no loaded boats that must attack first)
        if ctx.available_actions.get("can_end_phase", True):
            return end_phase(ctx.faction_id)
        return None

    if phase == "non_combat_move":
        if _mcts_enabled():
            from backend.ai.mcts.non_combat_move import decide_non_combat_move_mcts

            action = decide_non_combat_move_mcts(ctx)
            if action is not None:
                return action
            # Fall through to heuristic if MCTS returned nothing (safety net).

        _ensure_strategic(ctx)
        action = decide_non_combat_move(ctx)
        if action is not None:
            return action
        if ctx.available_actions.get("can_end_phase", True):
            return end_phase(ctx.faction_id)
        return None

    if phase == "mobilization":
        if _mcts_enabled():
            from backend.ai.mcts.mobilization import decide_mobilization_mcts

            action = decide_mobilization_mcts(ctx)
            if action is not None:
                # Same anti-stuck guard as the heuristic path: if MCTS chose end_phase
                # while the engine forbids ending, fall back to a remaining placement.
                if action.type == "end_phase" and ctx.available_actions.get("can_end_phase") is False:
                    fallback = _fallback_mobilize(ctx)
                    if fallback is not None:
                        return fallback
                return action
            # Fall through to heuristic if MCTS returned nothing (safety net).

        _ensure_strategic(ctx)
        action = decide_mobilization(ctx)
        if action is not None and action.type == "end_phase" and ctx.available_actions.get("can_end_phase") is False:
            fallback = _fallback_mobilize(ctx)
            if fallback is not None:
                return fallback
        if action is not None:
            return action
        if ctx.available_actions.get("can_end_phase", True):
            return end_phase(ctx.faction_id)
        return None

    # Fallback: end phase to avoid getting stuck
    return end_phase(ctx.faction_id)


def _fallback_mobilize(ctx: AIContext) -> Action | None:
    """Place one remaining stack when the policy tried to end with units still deployable.

    Mirrors the engine's destination rules: camp destinations only have room when
    capacity still exceeds units already pending there, and home slots only when
    this unit type hasn't filled them yet. Every candidate is validated before
    being returned, so the fallback never emits an action the reducer rejects.
    """
    from backend.engine.queries import _purchase_kind, validate_action

    options = (ctx.available_actions or {}).get("mobilize_options") or {}
    specs = options.get("unit_destinations") or {}
    purchased = ctx.state.faction_purchased_units.get(ctx.faction_id) or []

    pending_dest: dict[str, int] = {}
    pending_unit: dict[tuple[str, str], int] = {}
    for pm in getattr(ctx.state, "pending_mobilizations", []) or []:
        dest = getattr(pm, "destination", "") or ""
        for item in getattr(pm, "units", None) or []:
            n = int(item.get("count", 0) or 0)
            uid = item.get("unit_id") or ""
            pending_dest[dest] = pending_dest.get(dest, 0) + n
            pending_unit[(dest, uid)] = pending_unit.get((dest, uid), 0) + n

    def try_place(tid: str, unit_id: str, count: int) -> Action | None:
        if count <= 0:
            return None
        raw = mobilize_units(
            ctx.faction_id, tid, [{"unit_id": unit_id, "count": count}]
        )
        v = validate_action(
            ctx.state, raw, ctx.unit_defs, ctx.territory_defs,
            ctx.faction_defs, ctx.camp_defs, ctx.port_defs,
        )
        return raw if v.valid else None

    def place_to(tid: str, unit_id: str, count: int, room: int | None = None) -> Action | None:
        # Best fit first (pending-aware room), then the full stack, then one.
        tries: list[int] = []
        if room is not None and room > 0:
            tries.append(min(count, room))
        tries.extend((count, 1))
        for n in dict.fromkeys(tries):
            action = try_place(tid, unit_id, n)
            if action is not None:
                return action
        return None

    # Remaining capacity per destination (power rows only; home slots are per-unit).
    room_map: dict[str, int] = {}
    capacity = options.get("capacity") or {}
    for bucket, key in (
        (capacity.get("territories"), "territory_id"),
        (capacity.get("sea_zones"), "sea_zone_id"),
        (capacity.get("river_zones"), "river_zone_id"),
    ):
        for row in bucket or []:
            tid = row.get(key)
            if tid:
                room_map[tid] = int(row.get("power", 0) or 0)

    for stack in purchased:
        count = int(getattr(stack, "count", 0) or 0)
        if count <= 0:
            continue
        unit_id = stack.unit_id
        spec = specs.get(unit_id)
        if spec:
            if spec.get("unlimited"):
                dests = (
                    list(spec.get("territories") or [])
                    + list(spec.get("sea_zones") or [])
                    + list(spec.get("river_zones") or [])
                )
                for tid in dests:
                    action = place_to(tid, unit_id, count)
                    if action is not None:
                        return action
            else:
                for tid, power in (spec.get("capacity") or {}).items():
                    room = int(power or 0) - pending_dest.get(tid, 0)
                    action = place_to(tid, unit_id, count, room=room)
                    if action is not None:
                        return action
                for tid, slots in (spec.get("home") or {}).items():
                    if pending_unit.get((tid, unit_id), 0) >= int(slots or 0):
                        continue
                    action = try_place(tid, unit_id, 1)
                    if action is not None:
                        return action
            continue
        kind = _purchase_kind(ctx.unit_defs.get(unit_id))
        if kind == "naval":
            dests = options.get("sea_zones") or []
        elif kind == "river":
            dests = options.get("river_zones") or []
        else:
            dests = options.get("territories") or []
        for tid in dests:
            room = room_map.get(tid)
            if room is not None:
                room -= pending_dest.get(tid, 0)
            action = place_to(tid, unit_id, count, room=room)
            if action is not None:
                return action
    return None
