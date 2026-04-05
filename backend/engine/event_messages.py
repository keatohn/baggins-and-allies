"""
Human-readable message summaries for game events.
Used for the event log (one line per action / per battle), not fine-grained roll details.
"""

from typing import Any

from backend.engine.events import (
    CAMP_PLACED,
    COMBAT_ENDED,
    COMBAT_ROUND_RESOLVED,
    COMBAT_STARTED,
    INCOME_CALCULATED,
    INCOME_COLLECTED,
    PHASE_CHANGED,
    RESOURCES_CHANGED,
    TERRITORY_CAPTURED,
    TURN_ENDED,
    TURN_SKIPPED,
    TURN_STARTED,
    UNIT_DESTROYED,
    UNITS_MOBILIZED,
    UNITS_MOVED,
    UNITS_PURCHASED,
    UNITS_RETREATED,
    VICTORY,
)


def _territory_display(territory_id: str, territory_defs: dict) -> str:
    """Display name for a territory, fallback to id."""
    if not territory_defs:
        return territory_id.replace("_", " ")
    t = territory_defs.get(territory_id)
    if t and getattr(t, "display_name", None):
        return str(t.display_name)
    return territory_id.replace("_", " ")


def _faction_display(faction_id: str, faction_defs: dict) -> str:
    """Display name for a faction, fallback to id (title-case)."""
    if not faction_defs:
        return faction_id.replace("_", " ").title()
    f = faction_defs.get(faction_id)
    if f and getattr(f, "display_name", None):
        return str(f.display_name)
    return faction_id.replace("_", " ").title()


def _unit_display(unit_id: str, unit_defs: dict) -> str:
    """Display name for a unit type, fallback to id."""
    if not unit_defs:
        return unit_id.replace("_", " ")
    u = unit_defs.get(unit_id)
    if u and getattr(u, "display_name", None):
        return str(u.display_name)
    return unit_id.replace("_", " ")


def _format_unit_stack(count: int, unit_id: str, unit_defs: dict) -> str:
    if count <= 0:
        return ""
    name = _unit_display(unit_id, unit_defs)
    return f"{count} {name}" if count != 1 else name


def _casualty_summary(casualty_ids: list[str], unit_defs: dict) -> str:
    """Aggregate instance_ids into 'N Unit Name' counts."""
    if not casualty_ids or not unit_defs:
        return ""
    # instance_id often looks like faction_unitId_index; unit_id is in defs
    unit_id_from_instance = {}
    for iid in casualty_ids:
        # Best effort: unit_id may be after first underscore (e.g. isengard_urukhai_warrior_0)
        parts = iid.split("_")
        if len(parts) >= 2:
            # Common pattern: faction_unitType_index -> unit_id might be faction_unitType or just unitType
            candidate = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]
            if candidate in unit_defs:
                unit_id_from_instance[iid] = candidate
            else:
                unit_id_from_instance[iid] = parts[1] if len(parts) > 1 else "unknown"
        else:
            unit_id_from_instance[iid] = "unknown"
    counts: dict[str, int] = {}
    for iid in casualty_ids:
        uid = unit_id_from_instance.get(iid, "unknown")
        counts[uid] = counts.get(uid, 0) + 1
    parts = [_format_unit_stack(c, uid, unit_defs) for uid, c in sorted(counts.items()) if c > 0]
    return ", ".join(parts)


def build_message(
    event_type: str,
    payload: dict[str, Any],
    unit_defs: dict | None = None,
    territory_defs: dict | None = None,
    faction_defs: dict | None = None,
) -> str:
    """
    Return a one-line human-readable summary for the event, or "" if this event
    should not appear in the summary log (e.g. combat_round_resolved).
    """
    unit_defs = unit_defs or {}
    territory_defs = territory_defs or {}
    faction_defs = faction_defs or {}

    if event_type == COMBAT_ROUND_RESOLVED:
        return ""

    if event_type == UNITS_PURCHASED:
        faction = payload.get("faction", "")
        purchases = payload.get("purchases") or {}
        if not purchases:
            return f"{faction}: Purchased nothing"
        parts = [_format_unit_stack(c, uid, unit_defs) for uid, c in sorted(purchases.items()) if c > 0]
        return f"Purchased {', '.join(parts)}"

    if event_type == CAMP_PLACED:
        territory = _territory_display(payload.get("territory_id", ""), territory_defs)
        return f"Placed camp in {territory}"

    if event_type == UNITS_MOVED:
        from_t = _territory_display(payload.get("from_territory", ""), territory_defs)
        to_t = _territory_display(payload.get("to_territory", ""), territory_defs)
        unit_ids = payload.get("unit_ids") or []
        phase = payload.get("phase", "")
        # Group by unit_id for display (instance_ids like isengard_urukhai_0 -> urukhai)
        counts: dict[str, int] = {}
        for iid in unit_ids:
            parts = iid.split("_")
            uid = "_".join(parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else "unknown")
            if uid not in unit_defs and len(parts) >= 2:
                uid = parts[1]
            counts[uid] = counts.get(uid, 0) + 1
        stack_str = ", ".join(_format_unit_stack(c, uid, unit_defs) for uid, c in sorted(counts.items()) if c > 0)
        mt = payload.get("move_type") or ""
        if mt == "load":
            n_boats = int(payload.get("load_boat_count") or 1)
            ship_word = "ships" if n_boats > 1 else "ship"
            return f"Loaded {stack_str} from {from_t} onto {ship_word} in {to_t}"
        if mt == "sail":
            return f"Moved {stack_str} from {from_t} to {to_t}"
        if mt == "offload":
            base = f"Offloaded {stack_str} from {from_t} into {to_t}"
            if phase == "combat_move":
                return f"{base} for sea raid"
            return base
        if phase == "combat_move":
            return f"Moved {stack_str} to attack {to_t}"
        return f"Moved {stack_str} from {from_t} to {to_t}"

    if event_type == COMBAT_STARTED:
        # Attack declaration is already summarized in units_moved(phase=combat_move)
        return ""

    if event_type == COMBAT_ENDED:
        territory = _territory_display(payload.get("territory", ""), territory_defs)
        attacker_faction = _faction_display(payload.get("attacker_faction", ""), faction_defs)
        outcome_key = payload.get("outcome") or (
            "retreat" if payload.get("retreat_to") else
            "defeat" if payload.get("winner") == "defender" else
            "conquer"
        )
        att_cas = payload.get("attacker_casualty_ids") or []
        def_cas = payload.get("defender_casualty_ids") or []
        destroyed_str = _casualty_summary(def_cas, unit_defs)
        lost_str = _casualty_summary(att_cas, unit_defs)
        if outcome_key == "conquer":
            liberated_for = payload.get("liberated_for")
            if liberated_for:
                beneficiary = _faction_display(str(liberated_for), faction_defs)
                outcome = f"{attacker_faction} liberated {territory} for {beneficiary}"
            else:
                outcome = f"{attacker_faction} conquered {territory}"
        elif outcome_key == "victory":
            outcome = f"{attacker_faction} was victorious in {territory}"
        elif outcome_key == "retreat":
            to_t = _territory_display(payload.get("retreat_to", ""), territory_defs)
            outcome = f"{attacker_faction} retreated from {territory} into {to_t}"
        else:
            outcome = f"{attacker_faction} defeated in {territory}"
        parts = [outcome]
        if destroyed_str:
            parts.append(f"destroyed {destroyed_str}")
        if lost_str:
            parts.append(f"lost {lost_str}")
        return "; ".join(parts)

    if event_type == UNITS_RETREATED:
        # Retreat is summarized in combat_ended (one line per battle)
        return ""

    if event_type == UNITS_MOBILIZED:
        territory = _territory_display(payload.get("territory", ""), territory_defs)
        units = payload.get("units") or []
        counts: dict[str, int] = {}
        for u in units:
            uid = u.get("unit_id", "unknown") if isinstance(u, dict) else "unknown"
            counts[uid] = counts.get(uid, 0) + 1
        stack_str = ", ".join(_format_unit_stack(c, uid, unit_defs) for uid, c in sorted(counts.items()) if c > 0)
        return f"Mobilized {stack_str} to {territory}"

    if event_type == TURN_STARTED:
        turn = payload.get("turn_number", 0)
        faction = _faction_display(payload.get("faction", ""), faction_defs)
        return f"Turn {turn} — {faction}"

    if event_type == TURN_ENDED:
        turn = payload.get("turn_number", 0)
        faction = _faction_display(payload.get("faction", ""), faction_defs)
        return f"End of turn {turn} ({faction})"

    if event_type == TURN_SKIPPED:
        faction = _faction_display(payload.get("faction", ""), faction_defs)
        return f"{faction} skipped turn"

    if event_type == PHASE_CHANGED:
        new_phase = payload.get("new_phase", "")
        if new_phase == "turn_end":
            return ""  # Turn end is not a phase; turn_ended message covers it
        return f"Phase: {new_phase.replace('_', ' ')}"

    if event_type == INCOME_CALCULATED:
        income = payload.get("income") or {}
        power = int(income.get("power", 0) or 0)
        if power > 0:
            return f"Collected {power} power"
        if not income:
            return "Income calculated"
        parts = [f"{k}: {v}" for k, v in sorted(income.items()) if v]
        return f"Income: {', '.join(parts)}"

    if event_type == INCOME_COLLECTED:
        return "Income collected"

    if event_type == RESOURCES_CHANGED:
        # Usually not shown as a standalone log line; purchase/combat already summarize
        return ""

    if event_type == TERRITORY_CAPTURED:
        territory = _territory_display(payload.get("territory", ""), territory_defs)
        new_owner = payload.get("new_owner", "")
        return f"{territory} captured by {new_owner}"

    if event_type == UNIT_DESTROYED:
        # Shown per battle via combat_ended; skip individual unit_destroyed in summary
        return ""

    if event_type == VICTORY:
        winner = payload.get("winner", "")
        return f"{winner} alliance wins!"

    return ""
