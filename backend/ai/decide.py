"""
Decide one action for the current phase. Dispatches to phase-specific policies.
Returns an Action (or end_phase / end_turn / skip_turn when appropriate).
"""

from backend.engine.actions import Action, continue_combat, end_phase, mobilize_units

from backend.ai.context import AIContext
from backend.ai.strategic_context import build_strategic_turn_context
from backend.ai.purchase import decide_purchase
from backend.ai.combat import decide_combat, decide_initiate_combat
from backend.ai.mobilization import decide_mobilization
from backend.ai.combat_move import decide_combat_move
from backend.ai.non_combat_move import decide_non_combat_move
from backend.engine.special_rules import territory_defs_with_current_power


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
    ctx.strategic = build_strategic_turn_context(ctx)

    phase = ctx.phase

    if phase == "purchase":
        return decide_purchase(ctx)

    if phase == "combat" and ctx.state.active_combat:
        action = decide_combat(ctx)
        if action is not None:
            return action
        # end_phase is invalid while active_combat exists; advance the battle with empty dice (API fills for AI)
        return continue_combat(
            ctx.faction_id,
            dice_rolls={"attacker": [], "defender": []},
        )

    if phase == "combat":
        # No active combat: must initiate one of the declared battles if any
        combat_territories = (ctx.available_actions or {}).get("combat_territories") or []
        if combat_territories:
            action = decide_initiate_combat(ctx)
            if action is not None:
                return action
        return end_phase(ctx.faction_id)

    if phase == "combat_move":
        action = decide_combat_move(ctx)
        if action is not None:
            return action
        # Only end phase when allowed (e.g. no loaded boats that must attack first)
        if ctx.available_actions.get("can_end_phase", True):
            return end_phase(ctx.faction_id)
        return None

    if phase == "non_combat_move":
        action = decide_non_combat_move(ctx)
        if action is not None:
            return action
        if ctx.available_actions.get("can_end_phase", True):
            return end_phase(ctx.faction_id)
        return None

    if phase == "mobilization":
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
    """Place one remaining stack when the policy tried to end with units still deployable."""
    from backend.engine.queries import _purchase_kind

    options = (ctx.available_actions or {}).get("mobilize_options") or {}
    specs = options.get("unit_destinations") or {}
    purchased = ctx.state.faction_purchased_units.get(ctx.faction_id) or []
    for stack in purchased:
        count = int(getattr(stack, "count", 0) or 0)
        if count <= 0:
            continue
        spec = specs.get(stack.unit_id)
        if spec:
            if spec.get("unlimited"):
                dests = (
                    list(spec.get("territories") or [])
                    + list(spec.get("sea_zones") or [])
                    + list(spec.get("river_zones") or [])
                )
                if dests:
                    return mobilize_units(
                        ctx.faction_id, dests[0], [{"unit_id": stack.unit_id, "count": count}],
                    )
            for tid, power in (spec.get("capacity") or {}).items():
                room = int(power or 0)
                if room > 0:
                    return mobilize_units(
                        ctx.faction_id, tid, [{"unit_id": stack.unit_id, "count": min(count, room)}],
                    )
            home = spec.get("home") or {}
            if home:
                tid = next(iter(home))
                return mobilize_units(
                    ctx.faction_id, tid, [{"unit_id": stack.unit_id, "count": 1}],
                )
            continue
        kind = _purchase_kind(ctx.unit_defs.get(stack.unit_id))
        if kind == "naval":
            dests = options.get("sea_zones") or []
        elif kind == "river":
            dests = options.get("river_zones") or []
        else:
            dests = options.get("territories") or []
        if dests:
            return mobilize_units(
                ctx.faction_id, dests[0], [{"unit_id": stack.unit_id, "count": 1}],
            )
    return None
