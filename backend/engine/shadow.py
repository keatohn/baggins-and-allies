"""Shadow of war: one-step sight from the viewer's alliance.

The stored game stays complete. Responses drop units, casualty order, and log
lines for territories outside that ring. Cavalry get no extra sight.
"""

from __future__ import annotations

from typing import Any, Callable

from backend.engine.state import GameState

_PUBLIC_EVENT_TYPES = frozenset({
    "units_purchased",
    "resources_changed",
    "victory",
    "phase_changed",
    "turn_started",
    "turn_ended",
    "turn_skipped",
    "income_calculated",
    "income_collected",
})

_TERRITORY_KEYS = ("territory", "territory_id", "from_territory", "to_territory")


def choose_viewer_alliance(controlled_alliances: set[str], current_alliance: str | None) -> str | None:
    """One alliance uses that ring. Both alliances follow the side whose turn it is."""
    alliances = {alliance for alliance in controlled_alliances if alliance and alliance != "neutral"}
    if not alliances:
        return None
    if len(alliances) == 1:
        return next(iter(alliances))
    if current_alliance and current_alliance != "neutral":
        return current_alliance
    return next(iter(alliances))


def alliance_of(faction_defs: dict, faction_id: str | None, fallback: Callable[[str], str] | None = None) -> str:
    if not faction_id:
        return "neutral"
    fdef = faction_defs.get(faction_id) if isinstance(faction_defs, dict) else None
    if fdef is None and fallback is not None:
        return fallback(faction_id)
    if fdef is None:
        return "neutral"
    parent_id = getattr(fdef, "parent", None)
    if parent_id and parent_id in faction_defs:
        parent = faction_defs.get(parent_id)
        parent_alliance = getattr(parent, "alliance", None) if parent is not None else None
        if parent_alliance:
            return str(parent_alliance)
    alliance = getattr(fdef, "alliance", None)
    return str(alliance) if alliance else "neutral"


def visible_territory_ids(
    state: GameState,
    territory_defs: dict,
    viewer_alliance: str,
    faction_defs: dict,
) -> set[str]:
    """Owned land of this alliance, plus one ground or ford step, including coastal sea."""
    owned: list[str] = []
    for tid, terr in state.territories.items():
        if alliance_of(faction_defs, terr.owner) == viewer_alliance:
            owned.append(tid)
    visible = set(owned)
    for tid in owned:
        tdef = territory_defs.get(tid)
        if tdef is None:
            continue
        neighbors = list(getattr(tdef, "adjacent", None) or [])
        neighbors.extend(getattr(tdef, "ford_adjacent", None) or [])
        for nid in neighbors:
            if nid:
                visible.add(str(nid))
    return visible


def event_territory_ids(event: dict[str, Any]) -> list[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    found: list[str] = []
    for key in _TERRITORY_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str) and raw:
            found.append(raw)
    return found


def filter_events_for_sight(events: list[dict[str, Any]], visible: set[str]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype in _PUBLIC_EVENT_TYPES:
            kept.append(event)
            continue
        places = event_territory_ids(event)
        if places and any(place not in visible for place in places):
            continue
        if etype == "territory_captured":
            payload = dict(event.get("payload") or {})
            payload.pop("capturing_units", None)
            message = payload.get("message")
            territory = payload.get("territory") or payload.get("territory_id") or "a territory"
            new_owner = payload.get("new_owner") or "an alliance"
            if isinstance(message, str) and message:
                payload["message"] = f"{new_owner} took {territory}."
            event = {**event, "payload": payload}
        kept.append(event)
    return kept


def apply_shadow_view(
    out: dict[str, Any],
    state: GameState,
    territory_defs: dict,
    faction_defs: dict,
    viewer_alliance: str,
) -> None:
    """Mutate a response state dict so this alliance cannot see past one territory."""
    visible = visible_territory_ids(state, territory_defs, viewer_alliance, faction_defs)
    territories = out.get("territories")
    shadowed: list[str] = []
    if isinstance(territories, dict):
        for tid, terr in territories.items():
            if tid in visible or not isinstance(terr, dict):
                continue
            shadowed.append(tid)
            terr["units"] = []
    out["shadowed_territories"] = shadowed
    rings = out.get("rings")
    if isinstance(rings, list):
        out["rings"] = [
            ring for ring in rings
            if isinstance(ring, dict) and ring.get("territory_id") in visible
        ]

    order = out.get("territory_defender_casualty_order")
    if isinstance(order, dict):
        out["territory_defender_casualty_order"] = {
            tid: value for tid, value in order.items() if tid in visible
        }

    moves = out.get("pending_moves")
    if isinstance(moves, list):
        out["pending_moves"] = [
            move for move in moves
            if isinstance(move, dict)
            and (
                move.get("from_territory") in visible
                or move.get("to_territory") in visible
            )
        ]

    ac = out.get("active_combat")
    if isinstance(ac, dict):
        battle_territory = ac.get("territory_id") or ac.get("territory")
        attacker = alliance_of(faction_defs, ac.get("attacker_faction"))
        owner = None
        if isinstance(battle_territory, str):
            terr_state = state.territories.get(battle_territory)
            owner = terr_state.owner if terr_state is not None else None
        defender = alliance_of(faction_defs, owner)
        in_battle = viewer_alliance in {attacker, defender} and viewer_alliance != "neutral"
        if battle_territory not in visible and not in_battle:
            out["active_combat"] = None
            out.pop("combat_stat_modifiers", None)
            out.pop("combat_specials", None)
            out.pop("combat_attacker_effective_attack_override", None)


def filter_charge_routes(actions: dict[str, Any], visible: set[str]) -> None:
    """Drop charge paths that cross a hidden territory. Those paths announce it is empty."""
    for unit in actions.get("moveable_units") or []:
        if not isinstance(unit, dict):
            continue
        routes = unit.get("charge_routes")
        if not isinstance(routes, dict):
            continue
        cleaned: dict[str, list] = {}
        for dest, paths in routes.items():
            if not isinstance(paths, list):
                continue
            kept = [
                path for path in paths
                if isinstance(path, list) and all(step in visible for step in path)
            ]
            if kept:
                cleaned[str(dest)] = kept
        unit["charge_routes"] = cleaned
