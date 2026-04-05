"""
Decide one action for the current phase. Dispatches to phase-specific policies.
Returns an Action (or end_phase / end_turn / skip_turn when appropriate).
"""

from backend.engine.actions import Action, continue_combat, end_phase

from backend.ai.context import AIContext
from backend.ai.strategic_context import build_strategic_turn_context
from backend.ai.purchase import decide_purchase
from backend.ai.combat import decide_combat, decide_initiate_combat
from backend.ai.mobilization import decide_mobilization
from backend.ai.combat_move import decide_combat_move
from backend.ai.non_combat_move import decide_non_combat_move


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
        if action is not None:
            return action
        return end_phase(ctx.faction_id)

    # Fallback: end phase to avoid getting stuck
    return end_phase(ctx.faction_id)
