"""Rings of Power.

The catalog is a special rule on the scenario manifest. A game turns it on at
creation. Nothing here looks up a scenario id. A bearer is any unit with a hero id.
"""

from __future__ import annotations

import random
from typing import Any

from backend.engine import DICE_SIDES
from backend.engine.state import GameState, PendingMove, Ring, Unit


def _is_hero(unit_def: Any) -> bool:
    raw = getattr(unit_def, "hero_id", None)
    return isinstance(raw, str) and bool(raw.strip())


def rings_rule(special_rules: Any) -> dict[str, Any] | None:
    if not isinstance(special_rules, list):
        return None
    for rule in special_rules:
        if isinstance(rule, dict) and str(rule.get("type") or "").strip() == "rings_of_power":
            return rule
    return None


def mode_declared(special_rules: Any) -> bool:
    return rings_rule(special_rules) is not None


def ring_specs(special_rules: Any) -> list[dict[str, Any]]:
    """Normalized catalog rows. Skips malformed entries."""
    rule = rings_rule(special_rules)
    if rule is None:
        return []
    rows = rule.get("rings")
    if not isinstance(rows, list):
        return []
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        parsed = _parse_ring_row(row, seen)
        if parsed is not None:
            specs.append(parsed)
    return specs


def _opt_text(row: dict[str, Any], key: str) -> str | None:
    raw = row.get(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _opt_boost(row: dict[str, Any], key: str) -> int:
    raw = row.get(key, 0)
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _parse_ring_row(row: Any, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    rid = row.get("id")
    territory_id = row.get("territory_id")
    if not isinstance(rid, str) or not rid.strip():
        return None
    if not isinstance(territory_id, str) or not territory_id.strip():
        return None
    rid = rid.strip()
    territory_id = territory_id.strip()
    if rid in seen:
        return None
    power = row.get("power")
    if isinstance(power, bool) or not isinstance(power, int) or power < 0:
        return None
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        name = rid
    else:
        name = name.strip()
    seen.add(rid)
    return {
        "id": rid,
        "name": name,
        "territory_id": territory_id,
        "power": power,
        "bearer_hero_id": _opt_text(row, "bearer_hero_id"),
        "returns_to": _opt_text(row, "returns_to"),
        "attack_boost": _opt_boost(row, "attack_boost"),
        "defense_boost": _opt_boost(row, "defense_boost"),
        "rolls_boost": _opt_boost(row, "rolls_boost"),
        "hp_boost": _opt_boost(row, "hp_boost"),
        "moves_boost": _opt_boost(row, "moves_boost"),
    }


def spawn_rings(special_rules: Any) -> list[Ring]:
    return [
        Ring(
            id=spec["id"],
            name=spec["name"],
            power=spec["power"],
            territory_id=spec["territory_id"],
            bearer_hero_id=spec.get("bearer_hero_id"),
            returns_to=spec.get("returns_to"),
            attack_boost=int(spec.get("attack_boost") or 0),
            defense_boost=int(spec.get("defense_boost") or 0),
            rolls_boost=int(spec.get("rolls_boost") or 0),
            hp_boost=int(spec.get("hp_boost") or 0),
            moves_boost=int(spec.get("moves_boost") or 0),
        )
        for spec in ring_specs(special_rules)
    ]


def _hero_id_of(unit_def: Any) -> str | None:
    raw = getattr(unit_def, "hero_id", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _hero_power(unit_def: Any) -> int:
    cost = getattr(unit_def, "cost", None)
    if isinstance(cost, dict):
        try:
            return int(cost.get("power") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(cost, int) and not isinstance(cost, bool):
        return cost
    return 0


def _heroes_in(units: list[Unit], unit_defs: dict) -> list[Unit]:
    return [unit for unit in units if _hero_id_of(unit_defs.get(unit.unit_id))]


def unit_bearing_ring(
    state: GameState,
    ring: Ring,
    unit_defs: dict,
    attacker_ids: set[str] | None = None,
) -> Unit | None:
    """The hero this ring sits on. A required bearer wins over a stronger bystander.

    In a battle, attackers have not won the territory's rings yet. An attacker holds
    only the ring he carried in himself.
    """
    terr = state.territories.get(ring.territory_id)
    if terr is None:
        return None
    units = list(terr.units)
    if attacker_ids:
        carrier = ring.carried_in_by
        if carrier and carrier in attacker_ids:
            for unit in units:
                if unit.instance_id == carrier and _hero_id_of(unit_defs.get(unit.unit_id)):
                    return unit
            return None
        units = [unit for unit in units if unit.instance_id not in attacker_ids]
    heroes = _heroes_in(units, unit_defs)
    required = (ring.bearer_hero_id or "").strip()
    if required:
        for unit in heroes:
            if _hero_id_of(unit_defs.get(unit.unit_id)) == required:
                return unit
        return None
    if not heroes:
        return None
    return max(heroes, key=lambda unit: (_hero_power(unit_defs.get(unit.unit_id)), unit.instance_id))


def power_for_faction(state: GameState, faction_id: str, unit_defs: dict | None = None) -> int:
    if not getattr(state, "rings_of_power", False):
        return 0
    total = 0
    for ring in getattr(state, "rings", None) or []:
        required = (ring.bearer_hero_id or "").strip()
        if required and unit_defs is not None:
            bearer = unit_bearing_ring(state, ring, unit_defs)
            if bearer is not None:
                faction = getattr(unit_defs.get(bearer.unit_id), "faction", None)
                if faction == faction_id:
                    total += int(ring.power)
                continue
        terr = state.territories.get(ring.territory_id)
        if terr is not None and terr.owner == faction_id:
            total += int(ring.power)
    return total


def on_bearer_destroyed(state: GameState, unit: Unit, territory_id: str, unit_defs: dict) -> None:
    """A required bearer who dies in the ring's territory sends it home. Other rings stay."""
    if not getattr(state, "rings_of_power", False):
        return
    hero_id = _hero_id_of(unit_defs.get(unit.unit_id))
    if not hero_id:
        return
    for ring in getattr(state, "rings", None) or []:
        if ring.bearer_instance_id == unit.instance_id:
            ring.bearer_instance_id = None
        if ring.territory_id != territory_id:
            continue
        if (ring.bearer_hero_id or "").strip() != hero_id:
            continue
        home = (ring.returns_to or "").strip()
        if not home:
            continue
        ring.territory_id = home
        ring.bearer_instance_id = None
        ring.carried_in_by = None


def combat_boosts(
    state: GameState,
    units: list[Unit],
    unit_defs: dict,
    territory_id: str,
    attacker_ids: set[str] | None = None,
) -> dict[str, tuple[int, int, int, int]]:
    """instance_id -> (attack, defense, extra dice, extra hp) while the ring is on that hero."""
    if not getattr(state, "rings_of_power", False):
        return {}
    out: dict[str, tuple[int, int, int, int]] = {}
    present = {unit.instance_id: unit for unit in units}
    for ring in getattr(state, "rings", None) or []:
        if ring.territory_id != territory_id:
            continue
        bearer = unit_bearing_ring(state, ring, unit_defs, attacker_ids)
        if bearer is None or bearer.instance_id not in present:
            continue
        atk, dfn, dice, hp = out.get(bearer.instance_id, (0, 0, 0, 0))
        out[bearer.instance_id] = (
            atk + int(ring.attack_boost or 0),
            dfn + int(ring.defense_boost or 0),
            dice + int(ring.rolls_boost or 0),
            hp + int(ring.hp_boost or 0),
        )
    return out


def merge_ring_stat_mods(
    mods: dict[str, int],
    boosts: dict[str, tuple[int, int, int, int]],
    *,
    attacking: bool,
) -> dict[str, int]:
    merged = dict(mods)
    slot = 0 if attacking else 1
    for instance_id, parts in boosts.items():
        extra = parts[slot]
        if extra:
            merged[instance_id] = merged.get(instance_id, 0) + extra
    return merged


def merge_ring_dice(
    override: dict[str, int] | None,
    units: list[Unit],
    unit_defs: dict,
    boosts: dict[str, tuple[int, int, int, int]],
) -> dict[str, int] | None:
    extras = {iid: parts[2] for iid, parts in boosts.items() if parts[2]}
    if not extras and not override:
        return override
    merged = dict(override or {})
    for unit in units:
        extra = extras.get(unit.instance_id, 0)
        if not extra:
            continue
        base = merged.get(unit.instance_id)
        if base is None:
            base = int(getattr(unit_defs.get(unit.unit_id), "dice", 1) or 1)
        merged[unit.instance_id] = base + extra
    return merged or None


def sync_ring_movement(state: GameState, unit_defs: dict) -> None:
    """Fold moves_boost into the hero the ring is on. Idempotent: leaving drops the unused extra."""
    if not getattr(state, "rings_of_power", False):
        return
    wanted: dict[str, int] = {}
    for ring in getattr(state, "rings", None) or []:
        extra = int(getattr(ring, "moves_boost", 0) or 0)
        if extra <= 0:
            continue
        bearer = unit_bearing_ring(state, ring, unit_defs)
        if bearer is None:
            continue
        wanted[bearer.instance_id] = wanted.get(bearer.instance_id, 0) + extra
    for territory in state.territories.values():
        for unit in territory.units:
            ud = unit_defs.get(unit.unit_id)
            if ud is None:
                continue
            try:
                natural = int(getattr(ud, "movement", 0) or 0)
            except (TypeError, ValueError):
                continue
            want = wanted.get(unit.instance_id, 0)
            baked = int(unit.base_movement) - natural
            delta = want - baked
            if delta == 0:
                continue
            unit.base_movement = natural + want
            unit.remaining_movement = max(0, int(unit.remaining_movement) + delta)


def hp_shield_from_boosts(boosts: dict[str, tuple[int, int, int, int]]) -> dict[str, int]:
    return {iid: parts[3] for iid, parts in boosts.items() if parts[3] > 0}


def expand_rolls_for_bonus(
    rolls: list[int],
    units: list[Unit],
    unit_defs: dict,
    dice_bonus: dict[str, int],
) -> list[int]:
    """Insert a ring's extra dice just after that unit's own rolls."""
    if not any(dice_bonus.values()):
        return list(rolls)
    src = list(rolls)
    idx = 0
    out: list[int] = []
    for unit in units:
        base = int(getattr(unit_defs.get(unit.unit_id), "dice", 1) or 1)
        for _ in range(max(0, base)):
            if idx < len(src):
                out.append(int(src[idx]))
                idx += 1
            else:
                out.append(random.randint(1, DICE_SIDES))
        for _ in range(max(0, dice_bonus.get(unit.instance_id, 0))):
            out.append(random.randint(1, DICE_SIDES))
    out.extend(int(n) for n in src[idx:])
    return out


def _ring_by_id(state: GameState, ring_id: str) -> Ring | None:
    for ring in getattr(state, "rings", None) or []:
        if ring.id == ring_id:
            return ring
    return None


def validate_ring_carry(
    state: GameState,
    unit_defs: dict,
    units_to_move: list,
    from_id: str,
    charge_through: list[str] | None,
    ring_id: str | None,
) -> str | None:
    """Error text when this move cannot carry the ring. None when the carry is legal or absent."""
    if not ring_id:
        return None
    if not getattr(state, "rings_of_power", False):
        return "Rings of Power is off"
    ring = _ring_by_id(state, ring_id)
    if ring is None:
        return "Unknown ring"
    heroes = [unit for unit in units_to_move if _is_hero(unit_defs.get(unit.unit_id))]
    if len(heroes) != 1:
        return "A ring moves only with a single hero"
    hero = heroes[0]
    for pending in state.pending_moves:
        pending_ring = getattr(pending, "ring_id", None)
        if pending_ring and hero.instance_id in (pending.unit_instance_ids or []):
            return "That hero is already carrying a ring"
        if pending_ring == ring_id:
            return "That ring is already being carried"
    if ring.bearer_instance_id and ring.bearer_instance_id != hero.instance_id:
        return "Another hero is carrying that ring"
    if ring.territory_id != from_id:
        return "A ring leaves only with a hero moving out of its territory"
    return None


def claim_ring(state: GameState, ring_id: str, hero_instance_id: str) -> None:
    ring = _ring_by_id(state, ring_id)
    if ring is not None:
        ring.bearer_instance_id = hero_instance_id


def release_ring(state: GameState, ring_id: str | None) -> None:
    if not ring_id:
        return
    ring = _ring_by_id(state, ring_id)
    if ring is not None:
        ring.bearer_instance_id = None


def apply_carried_ring(state: GameState, move: PendingMove, to_id: str) -> None:
    ring_id = getattr(move, "ring_id", None)
    if not ring_id:
        return
    ring = _ring_by_id(state, ring_id)
    if ring is None:
        return
    carrier = ring.bearer_instance_id
    if carrier not in (move.unit_instance_ids or []):
        carrier = None
    ring.territory_id = to_id
    ring.bearer_instance_id = None
    ring.carried_in_by = carrier


def clear_ring_carriers(state: GameState) -> None:
    """A carry only decides battles in the turn it happened."""
    for ring in getattr(state, "rings", None) or []:
        ring.carried_in_by = None


def hero_instance_for_carry(unit_defs: dict, units_to_move: list) -> str | None:
    heroes = [unit for unit in units_to_move if _is_hero(unit_defs.get(unit.unit_id))]
    if len(heroes) != 1:
        return None
    return heroes[0].instance_id
