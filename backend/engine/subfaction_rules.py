"""Manifest subfaction_rules: economy, recruitment, purchase, mobilization, capture, and movement.

Missing keys use the defaults. A lost parent capital stops pooled production,
recruitment grants, and placing subfaction units.
"""

from __future__ import annotations

from typing import Any

from backend.engine.definitions import FactionDefinition, controlling_faction_id
from backend.engine.special_rules import territory_current_power
from backend.engine.state import GameState, UnitStack
from backend.engine.utils import (
    can_conquer_territory_as_attacker,
    effective_original_owner,
    faction_owns_capital,
    has_unit_special,
)

ECONOMIES = ("none", "pool")
MOBILIZATIONS = ("any_home", "camps")
CAPTURES = ("liberate_else_parent", "unit_faction")
MOVEMENTS = ("with_parent", "home_only")
RULE_KEYS = (
    "economy",
    "recruitment",
    "purchasable_by_parent",
    "mobilization",
    "capture",
    "movement",
)


def default_rule() -> dict[str, Any]:
    return {
        "economy": "none",
        "recruitment": None,
        "purchasable_by_parent": False,
        "mobilization": "camps",
        "capture": "liberate_else_parent",
        "movement": "with_parent",
    }


def parse_subfaction_rules(raw: Any) -> dict[str, dict[str, Any]]:
    """Keep well-formed rule objects. Bad entries are dropped so old saves still load."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sid, rule in raw.items():
        if not isinstance(sid, str) or not sid or not isinstance(rule, dict):
            continue
        parsed = _coerce_rule(rule)
        if parsed is not None:
            out[sid] = parsed
    return out


def subfaction_rule_errors(sid: str, rule: Any, units: dict[str, Any]) -> list[str]:
    """Setup-editor errors for one manifest.subfaction_rules entry."""
    prefix = f'manifest.subfaction_rules["{sid}"]'
    if not isinstance(rule, dict):
        return [f"{prefix} must be an object"]
    errors: list[str] = []
    unknown = [k for k in rule.keys() if k not in RULE_KEYS]
    for key in unknown:
        errors.append(f"{prefix} has unknown key \"{key}\"")
    economy = rule.get("economy", "none")
    if economy not in ECONOMIES:
        errors.append(f'{prefix}.economy must be "none" or "pool"')
    mobilization = rule.get("mobilization", "camps")
    if mobilization not in MOBILIZATIONS:
        errors.append(f'{prefix}.mobilization must be "any_home" or "camps"')
    capture = rule.get("capture", "liberate_else_parent")
    if capture not in CAPTURES:
        errors.append(f'{prefix}.capture must be "liberate_else_parent" or "unit_faction"')
    movement = rule.get("movement", "with_parent")
    if movement not in MOVEMENTS:
        errors.append(f'{prefix}.movement must be "with_parent" or "home_only"')
    if "purchasable_by_parent" in rule and not isinstance(rule.get("purchasable_by_parent"), bool):
        errors.append(f"{prefix}.purchasable_by_parent must be true or false")
    if "recruitment" in rule and rule.get("recruitment") is not None:
        errors.extend(_recruitment_errors(prefix, rule.get("recruitment"), sid, units))
    return errors


def child_ids(faction_defs: dict[str, FactionDefinition] | None, parent_id: str) -> list[str]:
    if not faction_defs or not parent_id:
        return []
    parent = faction_defs.get(parent_id) if parent_id in faction_defs else None
    if parent is None:
        return []
    return [sub.id for sub in getattr(parent, "subfactions", ()) or ()]


def rule_for(state: GameState, subfaction_id: str) -> dict[str, Any]:
    stored = getattr(state, "subfaction_rules", None) or {}
    raw = stored.get(subfaction_id) if isinstance(stored, dict) else None
    if not isinstance(raw, dict):
        return default_rule()
    parsed = _coerce_rule(raw)
    return parsed if parsed is not None else default_rule()


def unit_subfaction_id(
    unit_def: Any,
    faction_defs: dict[str, FactionDefinition] | None,
    parent_id: str,
) -> str | None:
    """Subfaction id when this unit belongs to a child of parent_id."""
    fid = getattr(unit_def, "faction", None)
    if not isinstance(fid, str) or not fid or fid == parent_id:
        return None
    if controlling_faction_id(faction_defs, fid) != parent_id:
        return None
    view = faction_defs.get(fid) if faction_defs else None
    if view is None or getattr(view, "parent", None) != parent_id:
        return None
    return fid


def capture_owner_for_units(
    state: GameState,
    units: list[Any],
    acting_faction: str | None,
    unit_defs: dict[str, Any],
    faction_defs: dict[str, FactionDefinition] | None,
) -> str:
    """Faction credited for land these units take.

    liberate_else_parent credits the acting parent. unit_faction credits the
    subfaction when every conquering unit belongs to that one subfaction.
    A parent unit, or two different subfactions, keeps the capture with the parent.
    Aerial and siegework units do not count.
    """
    parent = controlling_faction_id(faction_defs, acting_faction) or (acting_faction or "")
    subs: set[str] = set()
    saw_other = False
    for unit in units or []:
        uid = getattr(unit, "unit_id", None)
        if uid is None and isinstance(unit, dict):
            uid = unit.get("unit_id")
        unit_def = unit_defs.get(uid) if uid else None
        if not can_conquer_territory_as_attacker(unit_def):
            continue
        sid = unit_subfaction_id(unit_def, faction_defs, parent)
        if sid:
            subs.add(sid)
        else:
            saw_other = True
    if not saw_other and len(subs) == 1:
        sid = next(iter(subs))
        if rule_for(state, sid).get("capture") == "unit_faction":
            return sid
    return parent


def home_only_subfaction_id(
    state: GameState,
    unit_def: Any,
    faction_defs: dict[str, FactionDefinition] | None,
) -> str | None:
    """Subfaction id when this unit may move only onto its original territories."""
    fid = getattr(unit_def, "faction", None)
    if not isinstance(fid, str) or not fid or not faction_defs:
        return None
    view = faction_defs.get(fid)
    if view is None or not getattr(view, "parent", None):
        return None
    if rule_for(state, fid).get("movement") != "home_only":
        return None
    return fid


def restrict_home_only_destinations(
    state: GameState,
    unit_def: Any,
    reachable: dict[str, int],
    faction_defs: dict[str, FactionDefinition] | None,
) -> dict[str, int]:
    """Drop destinations that are not this unit's original territories."""
    sid = home_only_subfaction_id(state, unit_def, faction_defs)
    if not sid:
        return reachable
    kept: dict[str, int] = {}
    for territory_id, distance in reachable.items():
        territory = state.territories.get(territory_id)
        if territory is None:
            continue
        if effective_original_owner(territory_id, territory, state) == sid:
            kept[territory_id] = distance
    return kept


def parent_may_purchase_unit(
    state: GameState,
    parent_id: str,
    unit_def: Any,
    faction_defs: dict[str, FactionDefinition] | None,
) -> bool:
    if getattr(unit_def, "faction", None) == parent_id:
        return True
    sid = unit_subfaction_id(unit_def, faction_defs, parent_id)
    if not sid:
        return False
    return bool(rule_for(state, sid)["purchasable_by_parent"])


def snapshot_subfaction_territories(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
) -> None:
    """Territories each child owns as the parent's turn starts. Grants use this list."""
    snap = getattr(state, "subfaction_territories_at_turn_start", None)
    if not isinstance(snap, dict):
        snap = {}
        state.subfaction_territories_at_turn_start = snap
    for child in child_ids(faction_defs, parent_id):
        snap[child] = [
            tid for tid, ts in state.territories.items() if getattr(ts, "owner", None) == child
        ]
    state.subfaction_grants_applied = False


def apply_subfaction_grants(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
    unit_defs: dict[str, Any],
) -> None:
    """Add this turn's free units to the mobilization pool. Once per turn."""
    if getattr(state, "subfaction_grants_applied", False):
        return
    state.subfaction_grants_applied = True
    if not faction_owns_capital(state, parent_id, faction_defs or {}):
        return
    for child in child_ids(faction_defs, parent_id):
        recruitment = rule_for(state, child).get("recruitment")
        if not recruitment:
            continue
        unit_id = recruitment["unit_id"]
        unit_def = unit_defs.get(unit_id)
        if unit_def is None or getattr(unit_def, "faction", None) != child:
            continue
        per = int(recruitment["count"])
        if per <= 0:
            continue
        if recruitment["mode"] == "fixed":
            count = per
        else:
            owned = (getattr(state, "subfaction_territories_at_turn_start", None) or {}).get(child) or []
            count = len(owned) // per
        if count <= 0:
            continue
        _add_purchased(state, parent_id, unit_id, count)


def pool_territory_ids(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
) -> list[str]:
    """Subfaction territories whose production folds into the parent. Empty if the capital has fallen."""
    if not faction_owns_capital(state, parent_id, faction_defs or {}):
        return []
    ids: list[str] = []
    for child in child_ids(faction_defs, parent_id):
        if rule_for(state, child)["economy"] != "pool":
            continue
        for tid, ts in state.territories.items():
            if getattr(ts, "owner", None) == child:
                ids.append(tid)
    return ids


def purchase_capacity_error(
    state: GameState,
    faction_id: str,
    unit_defs: dict[str, Any],
    faction_defs: dict[str, FactionDefinition] | None,
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
    port_defs: dict[str, Any] | None,
    purchases: dict[str, int],
    land_cap: int,
    sea_cap: int,
    river_cap: int,
) -> str | None:
    """
    Parent units stay on the parent caps. any_home subfaction units are not capped.
    camps-mode subfaction units share the parent caps and may also use the child's camps and ports.
    """
    already = state.faction_purchased_units.get(faction_id, []) or []
    parent = {"land": 0, "naval": 0, "river": 0}
    camps = {"land": 0, "naval": 0, "river": 0}

    def add(unit_id: str, count: int) -> None:
        if count <= 0:
            return
        unit_def = unit_defs.get(unit_id)
        kind = _kind(unit_def)
        sid = unit_subfaction_id(unit_def, faction_defs, faction_id) if unit_def else None
        if sid and rule_for(state, sid)["mobilization"] == "any_home":
            return
        bucket = camps if sid else parent
        bucket[kind] += count

    for stack in already:
        add(getattr(stack, "unit_id", ""), int(getattr(stack, "count", 0) or 0))
    for unit_id, count in purchases.items():
        add(unit_id, int(count or 0))

    if parent["land"] > land_cap:
        return (
            f"Cannot purchase that many land units: land mobilization capacity is {land_cap} "
            f"(already purchased: {parent['land']} land, this purchase would exceed it)"
        )
    extra_land = _camps_land_capacity(state, faction_id, faction_defs, territory_defs, camp_defs)
    if parent["land"] + camps["land"] > land_cap + extra_land:
        return (
            f"Cannot purchase that many land units: land mobilization capacity is {land_cap + extra_land} "
            f"(parent camps {land_cap}, subfaction camps {extra_land})"
        )
    if parent["naval"] > sea_cap:
        return (
            f"Cannot purchase that many naval units: sea mobilization capacity is {sea_cap} "
            f"(already purchased: {parent['naval']} naval, this purchase would exceed it)"
        )
    extra_sea = _camps_port_capacity(state, faction_id, faction_defs, territory_defs, port_defs)
    if parent["naval"] + camps["naval"] > sea_cap + extra_sea:
        return (
            f"Cannot purchase that many naval units: sea mobilization capacity is {sea_cap + extra_sea}"
        )
    if parent["river"] > river_cap:
        return (
            f"Cannot purchase that many river units: river mobilization capacity is {river_cap} "
            f"(already purchased: {parent['river']} river, this purchase would exceed it)"
        )
    extra_river = _camps_river_capacity(state, faction_id, faction_defs, territory_defs)
    if parent["river"] + camps["river"] > river_cap + extra_river:
        return (
            f"Cannot purchase that many river units: river mobilization capacity is {river_cap + extra_river}"
        )
    return None


def subfaction_mobilization_error(
    state: GameState,
    faction_id: str,
    destination: str,
    units: list[dict[str, Any]],
    unit_defs: dict[str, Any],
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
    port_defs: dict[str, Any] | None,
    faction_defs: dict[str, FactionDefinition] | None,
) -> str | None:
    """
    None: this batch is parent units, so the normal camp and port rules apply.
    "": subfaction destination is legal.
    other: error message.
    """
    subs: set[str] = set()
    parents = False
    for item in units:
        unit_def = unit_defs.get(item.get("unit_id"))
        sid = unit_subfaction_id(unit_def, faction_defs, faction_id) if unit_def else None
        if sid:
            subs.add(sid)
        elif unit_def is not None and getattr(unit_def, "faction", None) == faction_id:
            parents = True
        else:
            return f"Unit {item.get('unit_id')} cannot be mobilized by {faction_id}"
    if not subs:
        return None
    if parents or len(subs) != 1:
        return "Mobilize parent units and subfaction units separately"
    sid = next(iter(subs))
    if not faction_owns_capital(state, faction_id, faction_defs or {}):
        return f"Cannot mobilize units: {faction_id}'s capital has been captured"
    mode = rule_for(state, sid)["mobilization"]
    dest = state.territories.get(destination)
    dest_def = territory_defs.get(destination)
    if not dest or not dest_def:
        return f"Territory {destination} does not exist"
    count = sum(int(item.get("count", 0) or 0) for item in units)
    kind = _kind(unit_defs.get(units[0].get("unit_id")))
    if mode == "any_home":
        return _any_home_error(state, sid, destination, dest, dest_def, kind, territory_defs)
    return _camps_error(
        state, faction_id, sid, destination, dest, dest_def, units, unit_defs, kind, count,
        territory_defs, camp_defs, port_defs,
    )


def unit_destination_spec(
    state: GameState,
    parent_id: str,
    unit_def: Any,
    faction_defs: dict[str, FactionDefinition] | None,
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
    port_defs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Placement options for one subfaction unit type. None for a parent unit."""
    sid = unit_subfaction_id(unit_def, faction_defs, parent_id)
    if not sid or not faction_owns_capital(state, parent_id, faction_defs or {}):
        return None
    mode = rule_for(state, sid)["mobilization"]
    if mode == "any_home":
        return {
            "territories": _any_home_land(state, sid, territory_defs),
            "sea_zones": _adjacent_water(state, sid, territory_defs, "sea"),
            "river_zones": _adjacent_water(state, sid, territory_defs, "river"),
            "unlimited": True,
        }
    territories, capacity, home = _camps_land_destinations(
        state, parent_id, sid, unit_def, territory_defs, camp_defs,
    )
    seas, sea_cap = _camps_sea_destinations(
        state, parent_id, sid, territory_defs, port_defs,
    )
    rivers, river_cap = _camps_river_destinations(
        state, parent_id, sid, territory_defs,
    )
    capacity.update(sea_cap)
    capacity.update(river_cap)
    spec: dict[str, Any] = {
        "territories": territories,
        "sea_zones": seas,
        "river_zones": rivers,
        "unlimited": False,
        "capacity": capacity,
    }
    if home:
        spec["home"] = home
    return spec


def _coerce_rule(rule: dict[str, Any]) -> dict[str, Any] | None:
    out = default_rule()
    economy = rule.get("economy", "none")
    if economy not in ECONOMIES:
        return None
    out["economy"] = economy
    mobilization = rule.get("mobilization", "camps")
    if mobilization not in MOBILIZATIONS:
        return None
    out["mobilization"] = mobilization
    capture = rule.get("capture", "liberate_else_parent")
    if capture not in CAPTURES:
        return None
    out["capture"] = capture
    movement = rule.get("movement", "with_parent")
    if movement not in MOVEMENTS:
        return None
    out["movement"] = movement
    flag = rule.get("purchasable_by_parent", False)
    if not isinstance(flag, bool):
        return None
    out["purchasable_by_parent"] = flag
    if "recruitment" not in rule or rule.get("recruitment") is None:
        out["recruitment"] = None
        return out
    recruitment = rule.get("recruitment")
    if not isinstance(recruitment, dict):
        return None
    mode = recruitment.get("mode")
    unit_id = recruitment.get("unit_id")
    count = recruitment.get("count")
    if mode not in ("fixed", "per_territory"):
        return None
    if not isinstance(unit_id, str) or not unit_id:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    out["recruitment"] = {"mode": mode, "unit_id": unit_id, "count": count}
    return out


def _recruitment_errors(prefix: str, recruitment: Any, sid: str, units: dict[str, Any]) -> list[str]:
    label = f"{prefix}.recruitment"
    if not isinstance(recruitment, dict):
        return [f"{label} must be an object or null"]
    errors: list[str] = []
    mode = recruitment.get("mode")
    if mode not in ("fixed", "per_territory"):
        errors.append(f'{label}.mode must be "fixed" or "per_territory"')
    unit_id = recruitment.get("unit_id")
    if not isinstance(unit_id, str) or unit_id not in units:
        errors.append(f"{label}.unit_id must be a known unit")
    else:
        fac = (units.get(unit_id) or {}).get("faction")
        if fac != sid:
            errors.append(f'{label}.unit_id must belong to "{sid}"')
    count = recruitment.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        errors.append(f"{label}.count must be an integer >= 1")
    return errors


def _add_purchased(state: GameState, faction_id: str, unit_id: str, count: int) -> None:
    pool = state.faction_purchased_units.setdefault(faction_id, [])
    for stack in pool:
        if stack.unit_id == unit_id:
            stack.count += count
            return
    pool.append(UnitStack(unit_id=unit_id, count=count))


def _kind(unit_def: Any) -> str:
    if unit_def is None:
        return "land"
    archetype = getattr(unit_def, "archetype", "") or ""
    tags = getattr(unit_def, "tags", []) or []
    if archetype == "naval" or "naval" in tags:
        return "naval"
    if archetype == "river" or "river" in tags:
        return "river"
    return "land"


def _terrain(tdef: Any) -> str:
    return (getattr(tdef, "terrain_type", "") or "").lower()


def _has_camp(state: GameState, territory_id: str, camp_defs: dict[str, Any] | None) -> bool:
    dynamic = getattr(state, "dynamic_camps", {}) or {}
    for camp_id in getattr(state, "camps_standing", []) or []:
        if dynamic.get(camp_id) == territory_id:
            return True
        camp = (camp_defs or {}).get(camp_id)
        if camp is not None and getattr(camp, "territory_id", None) == territory_id:
            return True
    return False


def _has_port(territory_id: str, port_defs: dict[str, Any] | None) -> bool:
    for port in (port_defs or {}).values():
        if getattr(port, "territory_id", None) == territory_id:
            return True
    return False


def _owned_at_start(state: GameState, owner_id: str, parent_id: str) -> set[str]:
    if owner_id == parent_id:
        raw = (getattr(state, "faction_territories_at_turn_start", None) or {}).get(parent_id) or []
    else:
        raw = (getattr(state, "subfaction_territories_at_turn_start", None) or {}).get(owner_id) or []
    return set(raw)


def _pending_count(state: GameState, destination: str) -> int:
    total = 0
    for pm in getattr(state, "pending_mobilizations", []) or []:
        if getattr(pm, "destination", None) != destination:
            continue
        total += sum(int(u.get("count", 0) or 0) for u in (getattr(pm, "units", None) or []))
    return total


def _any_home_land(state: GameState, sid: str, territory_defs: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for tid, ts in state.territories.items():
        if getattr(ts, "owner", None) != sid:
            continue
        if _terrain(territory_defs.get(tid)) in ("sea", "river"):
            continue
        out.append(tid)
    return out


def _adjacent_water(
    state: GameState,
    sid: str,
    territory_defs: dict[str, Any],
    kind: str,
) -> list[str]:
    """Sea or river zones that touch land this subfaction currently owns. A port is not required."""
    out: list[str] = []
    for tid, tdef in territory_defs.items():
        if _terrain(tdef) != kind:
            continue
        for adj in getattr(tdef, "adjacent", []) or []:
            ts = state.territories.get(adj)
            adj_def = territory_defs.get(adj)
            if ts and getattr(ts, "owner", None) == sid and _terrain(adj_def) not in ("sea", "river"):
                out.append(tid)
                break
    return out


def _any_home_error(
    state: GameState,
    sid: str,
    destination: str,
    dest: Any,
    dest_def: Any,
    kind: str,
    territory_defs: dict[str, Any],
) -> str:
    if kind == "land":
        if _terrain(dest_def) in ("sea", "river") or getattr(dest, "owner", None) != sid:
            return f"Subfaction units can only mobilize on land {sid} owns; {destination} is not valid"
        return ""
    wanted = "sea" if kind == "naval" else "river"
    if destination not in _adjacent_water(state, sid, territory_defs, wanted):
        label = "sea zone" if kind == "naval" else "river zone"
        return (
            f"Subfaction units can only mobilize to a {label} adjacent to land {sid} owns; "
            f"{destination} is not valid"
        )
    return ""


def _camps_error(
    state: GameState,
    parent_id: str,
    sid: str,
    destination: str,
    dest: Any,
    dest_def: Any,
    units: list[dict[str, Any]],
    unit_defs: dict[str, Any],
    kind: str,
    count: int,
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
    port_defs: dict[str, Any] | None,
) -> str:
    owners = {parent_id, sid}
    if kind == "naval":
        ports = _ports_touching(state, destination, owners, territory_defs, port_defs)
        if not ports:
            return (
                f"Naval units can only mobilize to a sea zone adjacent to a port you own; "
                f"{destination} is not valid"
            )
        for port_id, power in ports:
            pending = _pending_port_pool(state, port_id, territory_defs)
            if pending + count > power:
                return (
                    f"Cannot mobilize {count} naval to {destination}: "
                    f"port {port_id} shared pool would exceed capacity ({pending + count} > {power})"
                )
        return ""
    if kind == "river":
        banks = _river_banks(state, parent_id, sid, destination, territory_defs)
        if not banks:
            return (
                f"River units can only mobilize to a river zone that borders a territory you owned "
                f"at the start of your turn; {destination} is not valid"
            )
        power = sum(power for _bid, power in banks)
        if _pending_count(state, destination) + count > power:
            return f"Cannot mobilize {count} river units to {destination}: capacity is {power}"
        return ""
    owner = getattr(dest, "owner", None)
    if owner not in owners:
        return f"{destination} is not owned by {parent_id} or {sid}"
    if _terrain(dest_def) in ("sea", "river"):
        return f"Land units cannot mobilize to {destination}"
    has_camp = _has_camp(state, destination, camp_defs)
    owned_at_start = destination in _owned_at_start(state, owner, parent_id)
    home_ids = [
        item.get("unit_id")
        for item in units
        if _is_home_destination(unit_defs.get(item.get("unit_id")), destination)
    ]
    if has_camp and owned_at_start:
        power = territory_current_power(state, destination, dest_def)
        if _pending_count(state, destination) + count > power:
            return (
                f"Cannot mobilize {count} more to {destination}: capacity is {power}"
            )
        return ""
    if len(home_ids) == len(units) and len({item.get("unit_id") for item in units}) == 1:
        unit_id = units[0].get("unit_id")
        already = 0
        for pm in getattr(state, "pending_mobilizations", []) or []:
            if getattr(pm, "destination", None) != destination:
                continue
            for u in getattr(pm, "units", None) or []:
                if u.get("unit_id") == unit_id:
                    already += int(u.get("count", 0) or 0)
        if already + count > 1:
            return f"At most 1 {unit_id} can be mobilized to home territory {destination} per phase"
        return ""
    return (
        f"Land units can only mobilize to a standing camp or a home territory for that unit type; "
        f"{destination} is not valid"
    )


def _is_home_destination(unit_def: Any, territory_id: str) -> bool:
    if unit_def is None or not has_unit_special(unit_def, "home"):
        return False
    return territory_id in (getattr(unit_def, "home_territory_ids", None) or [])


def _ports_touching(
    state: GameState,
    sea_id: str,
    owners: set[str],
    territory_defs: dict[str, Any],
    port_defs: dict[str, Any] | None,
) -> list[tuple[str, int]]:
    sea_def = territory_defs.get(sea_id)
    if _terrain(sea_def) != "sea":
        return []
    found: list[tuple[str, int]] = []
    for adj in getattr(sea_def, "adjacent", []) or []:
        ts = state.territories.get(adj)
        if not ts or getattr(ts, "owner", None) not in owners:
            continue
        if not _has_port(adj, port_defs):
            continue
        power = territory_current_power(state, adj, territory_defs.get(adj))
        found.append((adj, power))
    return found


def _pending_port_pool(state: GameState, port_id: str, territory_defs: dict[str, Any]) -> int:
    dests = {port_id}
    port_def = territory_defs.get(port_id)
    for adj in getattr(port_def, "adjacent", []) or []:
        if _terrain(territory_defs.get(adj)) == "sea":
            dests.add(adj)
    total = 0
    for dest in dests:
        total += _pending_count(state, dest)
    return total


def _river_banks(
    state: GameState,
    parent_id: str,
    sid: str,
    zone_id: str,
    territory_defs: dict[str, Any],
) -> list[tuple[str, int]]:
    zdef = territory_defs.get(zone_id)
    if _terrain(zdef) != "river":
        return []
    banks: list[tuple[str, int]] = []
    for adj in getattr(zdef, "adjacent", []) or []:
        ts = state.territories.get(adj)
        if not ts:
            continue
        owner = getattr(ts, "owner", None)
        if owner not in (parent_id, sid):
            continue
        if adj not in _owned_at_start(state, owner, parent_id):
            continue
        if _terrain(territory_defs.get(adj)) in ("sea", "river"):
            continue
        banks.append((adj, territory_current_power(state, adj, territory_defs.get(adj))))
    return banks


def _camps_land_capacity(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
) -> int:
    total = 0
    for child in child_ids(faction_defs, parent_id):
        if rule_for(state, child)["mobilization"] != "camps":
            continue
        for tid in _owned_at_start(state, child, parent_id):
            ts = state.territories.get(tid)
            if not ts or getattr(ts, "owner", None) != child:
                continue
            if not _has_camp(state, tid, camp_defs):
                continue
            total += territory_current_power(state, tid, territory_defs.get(tid))
    return total


def _camps_port_capacity(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
    territory_defs: dict[str, Any],
    port_defs: dict[str, Any] | None,
) -> int:
    total = 0
    for child in child_ids(faction_defs, parent_id):
        if rule_for(state, child)["mobilization"] != "camps":
            continue
        for tid, ts in state.territories.items():
            if getattr(ts, "owner", None) != child or not _has_port(tid, port_defs):
                continue
            total += territory_current_power(state, tid, territory_defs.get(tid))
    return total


def _camps_river_capacity(
    state: GameState,
    parent_id: str,
    faction_defs: dict[str, FactionDefinition] | None,
    territory_defs: dict[str, Any],
) -> int:
    total = 0
    for child in child_ids(faction_defs, parent_id):
        if rule_for(state, child)["mobilization"] != "camps":
            continue
        for tid in _owned_at_start(state, child, parent_id):
            ts = state.territories.get(tid)
            tdef = territory_defs.get(tid)
            if not ts or getattr(ts, "owner", None) != child or tdef is None:
                continue
            if _terrain(tdef) in ("sea", "river"):
                continue
            if any(_terrain(territory_defs.get(adj)) == "river" for adj in (tdef.adjacent or [])):
                total += territory_current_power(state, tid, tdef)
    return total


def _camps_land_destinations(
    state: GameState,
    parent_id: str,
    sid: str,
    unit_def: Any,
    territory_defs: dict[str, Any],
    camp_defs: dict[str, Any] | None,
) -> tuple[list[str], dict[str, int], dict[str, int]]:
    territories: list[str] = []
    capacity: dict[str, int] = {}
    home: dict[str, int] = {}
    for owner in (parent_id, sid):
        for tid, ts in state.territories.items():
            if getattr(ts, "owner", None) != owner:
                continue
            tdef = territory_defs.get(tid)
            if tdef is None or _terrain(tdef) in ("sea", "river"):
                continue
            if _has_camp(state, tid, camp_defs) and tid in _owned_at_start(state, owner, parent_id):
                if tid not in territories:
                    territories.append(tid)
                capacity[tid] = territory_current_power(state, tid, tdef)
            elif _is_home_destination(unit_def, tid):
                if tid not in territories:
                    territories.append(tid)
                home[tid] = 1
    return territories, capacity, home


def _camps_sea_destinations(
    state: GameState,
    parent_id: str,
    sid: str,
    territory_defs: dict[str, Any],
    port_defs: dict[str, Any] | None,
) -> tuple[list[str], dict[str, int]]:
    zones: list[str] = []
    capacity: dict[str, int] = {}
    owners = {parent_id, sid}
    for tid, tdef in territory_defs.items():
        if _terrain(tdef) != "sea":
            continue
        ports = _ports_touching(state, tid, owners, territory_defs, port_defs)
        if not ports:
            continue
        zones.append(tid)
        capacity[tid] = min(power for _pid, power in ports)
    return zones, capacity


def _camps_river_destinations(
    state: GameState,
    parent_id: str,
    sid: str,
    territory_defs: dict[str, Any],
) -> tuple[list[str], dict[str, int]]:
    zones: list[str] = []
    capacity: dict[str, int] = {}
    for tid, tdef in territory_defs.items():
        if _terrain(tdef) != "river":
            continue
        banks = _river_banks(state, parent_id, sid, tid, territory_defs)
        if not banks:
            continue
        zones.append(tid)
        capacity[tid] = sum(power for _bid, power in banks)
    return zones, capacity
