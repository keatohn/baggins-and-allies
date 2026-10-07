"""
Starting-setup balance.

Three separate readings, then one optional index:

- Stock: effective unit power. Raw unit power is discounted by how many turns
  a stack needs before it can affect an important attack or defense.
- Flow: economic power. Future production over a short horizon, discounted,
  with evolving-territory schedules applied turn by turn.
- Victory pressure: an alliance keeps a smaller share of its units and economy
  as more strongholds remain. One still needed keeps the full score. If one
  alliance already holds enough and another still needs more, the first has
  won at the start and takes the whole share. A target higher than the number
  of strongholds on the map cannot be met, so that alliance scores 0 while
  another alliance can still win.

    SPS = (EUP + EP) * closeness(strongholds_still_needed)
    already won at the start -> that alliance's share is 1 and the other scores 0
    EUP = attack_weight * EUP_attack + defense_weight * EUP_defense
    EP  = sum_{t=1..H} discount^t * PP_t

Territory count is reported and left out of SPS, because production already
carries the economic value of owning land.

Distance is shortest path on the setup graph. Land uses ground adjacency plus
ford links and cannot cross sea. Air uses ground plus aerial links and can
cross sea. Ships use the sea graph, and a ship still in port spends one step
putting to sea. Naval transport of land armies is not modeled.

This is a way to compare setups. It is not a win probability.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from backend.engine.special_rules import effective_territory_power


@dataclass(frozen=True)
class BalanceConfig:
    """Knobs for the first balance model. Echoed back on every report."""

    horizon_rounds: int = 3
    discount: float = 0.8
    attack_weight: float = 0.5
    defense_weight: float = 0.5
    high_production: int = 3
    # Fraction of unit-plus-economy score kept when 1, 2, 3, or 4 strongholds are still needed.
    victory_closeness: tuple[float, float, float, float] = (1.0, 0.70, 0.45, 0.28)
    victory_closeness_floor: float = 0.18
    availability_by_turns: tuple[float, float, float, float] = (1.0, 0.8, 0.6, 0.4)
    availability_floor: float = 0.25

    def closeness(self, needed: int) -> float:
        if needed <= 1:
            return self.victory_closeness[0]
        index = needed - 1
        if index < len(self.victory_closeness):
            return self.victory_closeness[index]
        return self.victory_closeness_floor

    def availability(self, turns: int | None) -> float:
        if turns is None or turns >= len(self.availability_by_turns):
            return self.availability_floor
        if turns < 0:
            return self.availability_by_turns[0]
        return self.availability_by_turns[turns]


@dataclass(frozen=True)
class _Territory:
    id: str
    display_name: str
    adjacent: tuple[str, ...]
    aerial_adjacent: tuple[str, ...]
    ford_adjacent: tuple[str, ...]
    power: int
    is_stronghold: bool
    terrain: str
    ownable: bool

    @property
    def is_sea(self) -> bool:
        return self.terrain == "sea"


@dataclass(frozen=True)
class _Faction:
    id: str
    display_name: str
    alliance: str
    capital: str
    parent: str = ""
    is_subfaction: bool = False


@dataclass(frozen=True)
class _UnitDef:
    id: str
    display_name: str
    faction: str
    power: int
    movement: int
    mobility: str  # "land" | "air" | "sea"


@dataclass
class _Stack:
    territory_id: str
    unit: _UnitDef
    count: int

    @property
    def power(self) -> int:
        return self.unit.power * self.count


@dataclass
class _Acc:
    territories: int = 0
    strongholds: int = 0
    units: int = 0
    unit_power: int = 0
    eup_attack: float = 0.0
    eup_defense: float = 0.0
    eup: float = 0.0
    immediate_attack_power: int = 0
    immediate_defense_power: int = 0
    unreachable_attack_power: int = 0
    power_production: int = 0
    economic_power: float = 0.0
    discounts: list[dict[str, Any]] = field(default_factory=list)


def compute_starting_strength(
    bundle: dict[str, Any] | None,
    *,
    config: BalanceConfig | None = None,
    rings: bool | None = None,
) -> dict[str, Any]:
    """Balance report for one setup bundle (the admin JSON shape).

    ``rings`` turns an optional Rings of Power rule off when False. A required rule is always on.
    """
    cfg = config or BalanceConfig()
    bundle = bundle if isinstance(bundle, dict) else {}
    manifest = _as_dict(bundle.get("manifest"))
    territories = _parse_territories(_as_dict(bundle.get("territories")))
    factions = _parse_factions(_as_dict(bundle.get("factions")))
    unit_defs = _parse_units(_as_dict(bundle.get("units")))
    starting = _as_dict(bundle.get("starting_setup"))
    owners = {
        tid: owner
        for tid, owner in _as_dict(starting.get("territory_owners")).items()
        if isinstance(tid, str) and isinstance(owner, str) and owner
    }
    rules = manifest.get("special_rules")
    from backend.engine.subfaction_rules import parse_subfaction_rules
    subfaction_rules = parse_subfaction_rules(manifest.get("subfaction_rules"))
    turn_order = [
        fid
        for fid in (starting.get("turn_order") or [])
        if isinstance(fid, str) and fid in factions
    ]
    capitals = {f.capital for f in factions.values() if f.capital and f.capital in territories}
    stacks = _parse_stacks(starting.get("starting_units"), unit_defs)

    faction_ids = _faction_order(factions, turn_order)
    alliances = _alliance_order(factions, faction_ids)
    land_n, air_n, sea_n = _neighbor_fns(territories)

    attack_maps: dict[str, dict[str, dict[str, int]]] = {}
    defense_maps: dict[str, dict[str, dict[str, int]]] = {}
    for alliance in alliances:
        offensive = _offensive_targets(alliance, territories, factions, owners, capitals, cfg)
        defensive = _defensive_targets(alliance, territories, factions, owners, capitals, land_n, cfg)
        attack_maps[alliance] = {
            "land": _distances(offensive, land_n),
            "air": _distances(offensive, air_n),
            "sea": _distances(
                _sea_targets(offensive, territories, sea_n)
                | _occupied_seas(stacks, territories, factions, alliance, enemy=True),
                sea_n,
            ),
        }
        defense_maps[alliance] = {
            "land": _distances(defensive, land_n),
            "air": _distances(defensive, air_n),
            "sea": _distances(
                _sea_targets(defensive, territories, sea_n)
                | _occupied_seas(stacks, territories, factions, alliance, enemy=True),
                sea_n,
            ),
        }

    rings_mode, ring_rows = _rings_rule(rules)
    rings_on = rings_mode == "always" or (rings_mode == "optional" and rings is not False)
    ring_power = (
        _ring_power(ring_rows, _as_dict(bundle.get("units")), stacks, owners, factions, subfaction_rules)
        if rings_on
        else {}
    )

    accs = {fid: _Acc() for fid in faction_ids}
    for fid in faction_ids:
        _add_economy(
            accs[fid], fid, territories, owners, factions, rules, cfg, subfaction_rules,
            ring_power=ring_power.get(fid, 0),
        )
    for stack in stacks:
        faction = _controlled_faction(stack.unit.faction, factions)
        if not faction or faction.alliance in ("", "neutral"):
            continue
        acc = accs.get(faction.id)
        if acc is None or faction.alliance not in attack_maps:
            continue
        _add_stack(
            acc,
            stack,
            faction.alliance,
            attack_maps[faction.alliance],
            defense_maps[faction.alliance],
            territories,
            factions,
            owners,
            capitals,
            sea_n,
            land_n,
            cfg,
        )

    neutral = _neutral_summary(territories, factions, owners, stacks)
    faction_rows = [_faction_row(factions[fid], accs[fid]) for fid in faction_ids]
    map_strongholds = sum(1 for terr in territories.values() if terr.is_stronghold)
    alliance_rows = _alliance_rows(alliances, faction_rows, manifest, cfg, map_strongholds)
    discounts = _largest_discounts(faction_rows)
    for row in faction_rows:
        for key in [k for k in row if k.startswith("_")]:
            row.pop(key)

    return {
        "parameters": _parameters(cfg),
        "factions": faction_rows,
        "alliances": alliance_rows,
        "neutral": neutral,
        "largest_discounts": discounts,
        "readings": _readings(alliance_rows, cfg.horizon_rounds),
        "rings_of_power": {"mode": rings_mode, "on": rings_on},
    }


def _rings_rule(rules: Any) -> tuple[str, list[dict[str, Any]]]:
    """("none" | "always" | "optional", ring rows) from the manifest special rules."""
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("type") == "rings_of_power":
            rows = [r for r in rule.get("rings") or [] if isinstance(r, dict)]
            return ("optional" if rule.get("is_optional") is True else "always"), rows
    return "none", []


def _ring_power(
    ring_rows: list[dict[str, Any]],
    raw_units: dict[str, Any],
    stacks: list[_Stack],
    owners: dict[str, str],
    factions: dict[str, _Faction],
    subfaction_rules: dict | None,
) -> dict[str, int]:
    """Starting ring income per playable faction, credited the way the engine credits it."""
    out: dict[str, int] = {}
    for ring in ring_rows:
        power = _nonneg_int(ring.get("power")) or 0
        tid = ring.get("territory_id")
        if power <= 0 or not isinstance(tid, str):
            continue
        holder = ""
        bearer = ring.get("bearer_hero_id")
        if isinstance(bearer, str) and bearer.strip():
            for stack in stacks:
                hero = _as_dict(raw_units.get(stack.unit.id)).get("hero_id")
                if stack.territory_id == tid and isinstance(hero, str) and hero.strip() == bearer.strip():
                    holder = stack.unit.faction
                    break
        holder = holder or owners.get(tid, "")
        faction = factions.get(holder)
        if faction is None:
            continue
        if faction.is_subfaction:
            parent = factions.get(faction.parent)
            pooled = ((subfaction_rules or {}).get(holder) or {}).get("economy") == "pool"
            if parent is None or not pooled or owners.get(parent.capital) != parent.id:
                continue
            faction = parent
        out[faction.id] = out.get(faction.id, 0) + power
    return out


def _parameters(cfg: BalanceConfig) -> dict[str, Any]:
    factor = sum(cfg.discount ** t for t in range(1, cfg.horizon_rounds + 1))
    ladder = [
        {"turns": str(i), "factor": factor_i}
        for i, factor_i in enumerate(cfg.availability_by_turns)
    ]
    ladder.append({"turns": f"{len(cfg.availability_by_turns)}+", "factor": cfg.availability_floor})
    closeness = _closeness_ladder(cfg)
    summary = (
        "Starting power score keeps a smaller share of effective unit power plus discounted production "
        "as more strongholds are still needed. "
        + ", ".join(f"{row['needed']} still needed keeps {_pct(row['kept'])}" for row in closeness)
        + ". "
        "If an alliance already holds enough strongholds and another still needs more, "
        "the first has won at the start and takes the whole share. "
        "A target higher than the number of strongholds on the map cannot be met, "
        "so that alliance scores 0 while another alliance can still win. "
        "Attack availability is how soon a unit can reach an enemy or unowned stronghold, "
        "capital, or territory producing "
        f"{cfg.high_production} or more. Defense availability is how soon it can stand on a "
        "threatened friendly stronghold, capital, or high-production territory. "
        f"The two are averaged ({_pct(cfg.attack_weight)} attack, {_pct(cfg.defense_weight)} defense). "
        "Availability is "
        + ", ".join(f"{row['turns']} turns {_pct(row['factor'])}" for row in ladder)
        + ". "
        f"Economic power discounts the next {cfg.horizon_rounds} rounds of production by {cfg.discount} "
        f"each round, so a flat economy counts as {factor:.2f} times current production. "
        "Evolving territories follow their step until they stop. Territory count is shown and left out of the score. "
        "The alliance percentage compares setups; it is a resource index, separate from win probability. "
        "Land paths use ground and ford links and do not board ships, so an army with no land route "
        "stays at the floor."
    )
    return {
        "horizon_rounds": cfg.horizon_rounds,
        "discount": cfg.discount,
        "attack_weight": cfg.attack_weight,
        "defense_weight": cfg.defense_weight,
        "high_production": cfg.high_production,
        "victory_closeness": closeness,
        "economic_coefficient": _q(factor),
        "availability": ladder,
        "summary": summary,
    }


def _add_economy(
    acc: _Acc,
    faction_id: str,
    territories: dict[str, _Territory],
    owners: dict[str, str],
    factions: dict[str, _Faction],
    rules: Any,
    cfg: BalanceConfig,
    subfaction_rules: dict | None = None,
    ring_power: int = 0,
) -> None:
    schedule: list[int] = []
    for turn in range(1, cfg.horizon_rounds + 1):
        produced = ring_power
        for tid, owner in owners.items():
            owner_faction = factions.get(owner)
            controlled = owner == faction_id or (
                owner_faction is not None
                and owner_faction.is_subfaction
                and owner_faction.parent == faction_id
            )
            if not controlled:
                continue
            terr = territories.get(tid)
            if not terr or terr.is_sea:
                continue
            if turn == 1:
                acc.territories += 1
                if terr.is_stronghold:
                    acc.strongholds += 1
            # Subfaction land is counted above. Pooled economy adds its power to the parent.
            if owner != faction_id:
                child_rule = (subfaction_rules or {}).get(owner) or {}
                if child_rule.get("economy") != "pool":
                    continue
            produced += effective_territory_power(terr.power, turn, rules, tid)
        schedule.append(produced)
    acc.power_production = schedule[0] if schedule else 0
    acc.economic_power = sum((cfg.discount ** (i + 1)) * pp for i, pp in enumerate(schedule))


def _add_stack(
    acc: _Acc,
    stack: _Stack,
    alliance: str,
    attack_maps: dict[str, dict[str, int]],
    defense_maps: dict[str, dict[str, int]],
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    capitals: set[str],
    sea_neighbors,
    land_neighbors,
    cfg: BalanceConfig,
) -> None:
    mobility = stack.unit.mobility
    attack_steps = _steps_from(stack.territory_id, attack_maps[mobility], territories, mobility, sea_neighbors, land_neighbors)
    defense_steps = _steps_from(stack.territory_id, defense_maps[mobility], territories, mobility, sea_neighbors, land_neighbors)
    # Already garrisoning a friendly capital, stronghold, or high-production territory.
    # That is useful even when the hex is not itself on the enemy border, and even when
    # the unit cannot move (siege engines).
    if _garrisons_important(stack.territory_id, alliance, territories, factions, owners, capitals, cfg):
        defense_steps = 0
    attack_turns = _turns(attack_steps, stack.unit.movement, attack=True)
    defense_turns = _turns(defense_steps, stack.unit.movement, attack=False)
    attack_factor = cfg.availability(attack_turns)
    defense_factor = cfg.availability(defense_turns)
    eup_attack = stack.power * attack_factor
    eup_defense = stack.power * defense_factor
    eup = cfg.attack_weight * eup_attack + cfg.defense_weight * eup_defense
    acc.units += stack.count
    acc.unit_power += stack.power
    acc.eup_attack += eup_attack
    acc.eup_defense += eup_defense
    acc.eup += eup
    if attack_turns is not None and attack_turns <= 0:
        acc.immediate_attack_power += stack.power
    if defense_turns is not None and defense_turns <= 0:
        acc.immediate_defense_power += stack.power
    if attack_steps is None:
        acc.unreachable_attack_power += stack.power
    lost = stack.power - eup
    if lost > 0.05 and stack.power > 0:
        terr = territories.get(stack.territory_id)
        acc.discounts.append(
            {
                "faction_id": stack.unit.faction,
                "territory_id": stack.territory_id,
                "territory_name": terr.display_name if terr else stack.territory_id,
                "unit_id": stack.unit.id,
                "unit_name": stack.unit.display_name,
                "count": stack.count,
                "unit_power": stack.power,
                "attack_turns": attack_turns,
                "defense_turns": defense_turns,
                "attack_turn_label": _turn_label(attack_turns, attack_steps, stack.unit.movement),
                "defense_turn_label": _turn_label(defense_turns, defense_steps, stack.unit.movement),
                "effective_unit_power": eup,
                "power_discounted": lost,
            }
        )


def _steps_from(
    territory_id: str,
    dist: dict[str, int],
    territories: dict[str, _Territory],
    mobility: str,
    sea_neighbors,
    land_neighbors,
) -> int | None:
    """Steps from a stack to the nearest target, including embark or unload."""
    terr = territories.get(territory_id)
    if mobility == "air":
        return dist.get(territory_id)
    if mobility == "sea":
        if terr and terr.is_sea:
            return dist.get(territory_id)
        best: int | None = None
        for sea_id in sea_neighbors(territory_id):
            sea_dist = dist.get(sea_id)
            if sea_dist is None:
                continue
            steps = sea_dist + 1
            if best is None or steps < best:
                best = steps
        return best
    if terr and not terr.is_sea:
        return dist.get(territory_id)
    best = None
    for land_id in land_neighbors(territory_id):
        land_dist = dist.get(land_id)
        if land_dist is None:
            continue
        steps = land_dist + 1
        if best is None or steps < best:
            best = steps
    return best


def _turn_label(turns: int | None, steps: int | None, movement: int) -> str:
    if movement <= 0 and steps not in (None, 0):
        return "immobile"
    if turns is None:
        return "no route"
    return str(turns)


def _turns(steps: int | None, movement: int, *, attack: bool) -> int | None:
    """
    Attack turns subtract one because the entering move is the attack itself.
    Defense turns are the moves required to be standing on the position.
    """
    if steps is None:
        return None
    if movement <= 0:
        return 0 if steps == 0 else None
    moves = math.ceil(steps / movement)
    if attack:
        return max(0, moves - 1)
    return moves


def _offensive_targets(
    alliance: str,
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    capitals: set[str],
    cfg: BalanceConfig,
) -> set[str]:
    targets: set[str] = set()
    for tid, terr in territories.items():
        if terr.is_sea or not _is_important(terr, capitals, cfg):
            continue
        owner_alliance = _owner_alliance(owners.get(tid), factions)
        if owner_alliance == alliance:
            continue
        targets.add(tid)
    return targets


def _defensive_targets(
    alliance: str,
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    capitals: set[str],
    land_neighbors,
    cfg: BalanceConfig,
) -> set[str]:
    important = {
        tid
        for tid, terr in territories.items()
        if not terr.is_sea
        and _is_important(terr, capitals, cfg)
        and _owner_alliance(owners.get(tid), factions) == alliance
    }
    threatened = {tid for tid in important if _borders_enemy(tid, alliance, territories, factions, owners, land_neighbors)}
    return threatened or important


def _garrisons_important(
    territory_id: str,
    alliance: str,
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    capitals: set[str],
    cfg: BalanceConfig,
) -> bool:
    terr = territories.get(territory_id)
    if not terr or terr.is_sea:
        return False
    if _owner_alliance(owners.get(territory_id), factions) != alliance:
        return False
    return _is_important(terr, capitals, cfg)


def _borders_enemy(
    territory_id: str,
    alliance: str,
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    land_neighbors,
) -> bool:
    for nid in land_neighbors(territory_id):
        other = _owner_alliance(owners.get(nid), factions)
        if other and other not in (alliance, "neutral"):
            return True
    return False


def _is_important(terr: _Territory, capitals: set[str], cfg: BalanceConfig) -> bool:
    return terr.is_stronghold or terr.id in capitals or terr.power >= cfg.high_production


def _sea_targets(land_targets: set[str], territories: dict[str, _Territory], sea_neighbors) -> set[str]:
    seas: set[str] = set()
    for tid in land_targets:
        for sea_id in sea_neighbors(tid):
            if sea_id in territories:
                seas.add(sea_id)
    return seas


def _occupied_seas(
    stacks: list[_Stack],
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    alliance: str,
    *,
    enemy: bool,
) -> set[str]:
    found: set[str] = set()
    for stack in stacks:
        if stack.unit.mobility != "sea":
            continue
        faction = _controlled_faction(stack.unit.faction, factions)
        if not faction:
            continue
        is_enemy = faction.alliance not in ("", "neutral", alliance)
        if enemy != is_enemy:
            continue
        terr = territories.get(stack.territory_id)
        if terr and terr.is_sea:
            found.add(stack.territory_id)
    return found


def _distances(targets: set[str], neighbors) -> dict[str, int]:
    dist: dict[str, int] = {}
    queue: deque[str] = deque()
    for tid in targets:
        if tid not in dist:
            dist[tid] = 0
            queue.append(tid)
    while queue:
        tid = queue.popleft()
        nxt = dist[tid] + 1
        for nid in neighbors(tid):
            if nid in dist:
                continue
            dist[nid] = nxt
            queue.append(nid)
    return dist


def _neighbor_fns(territories: dict[str, _Territory]):
    def unique(ids: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for tid in ids:
            if tid in seen or tid not in territories:
                continue
            seen.add(tid)
            out.append(tid)
        return out

    def land_neighbors(tid: str) -> list[str]:
        terr = territories.get(tid)
        if not terr:
            return []
        return [
            nid
            for nid in unique([*terr.adjacent, *terr.ford_adjacent])
            if not territories[nid].is_sea
        ]

    def air_neighbors(tid: str) -> list[str]:
        terr = territories.get(tid)
        if not terr:
            return []
        return unique([*terr.adjacent, *terr.aerial_adjacent])

    def sea_neighbors(tid: str) -> list[str]:
        terr = territories.get(tid)
        if not terr:
            return []
        return [nid for nid in unique(list(terr.adjacent)) if territories[nid].is_sea]

    return land_neighbors, air_neighbors, sea_neighbors


def _neutral_summary(
    territories: dict[str, _Territory],
    factions: dict[str, _Faction],
    owners: dict[str, str],
    stacks: list[_Stack],
) -> dict[str, int]:
    strongholds = 0
    land = 0
    for tid, terr in territories.items():
        if terr.is_sea:
            continue
        alliance = _owner_alliance(owners.get(tid), factions)
        if alliance in (None, "neutral"):
            land += 1
            if terr.is_stronghold:
                strongholds += 1
    units = 0
    unit_power = 0
    for stack in stacks:
        faction = _controlled_faction(stack.unit.faction, factions)
        if not faction or faction.alliance in ("", "neutral"):
            units += stack.count
            unit_power += stack.power
    return {
        "territories": land,
        "strongholds": strongholds,
        "units": units,
        "unit_power": unit_power,
    }


def _faction_row(faction: _Faction, acc: _Acc) -> dict[str, Any]:
    row = {
        "id": faction.id,
        "display_name": faction.display_name,
        "alliance": faction.alliance,
        "territories": acc.territories,
        "strongholds": acc.strongholds,
        "units": acc.units,
        "unit_power": acc.unit_power,
        "effective_unit_power_attack": _q(acc.eup_attack),
        "effective_unit_power_defense": _q(acc.eup_defense),
        "effective_unit_power": _q(acc.eup),
        "position_ratio": _ratio(acc.eup, acc.unit_power),
        "immediate_attack_power": acc.immediate_attack_power,
        "immediate_defense_power": acc.immediate_defense_power,
        "unreachable_attack_power": acc.unreachable_attack_power,
        "power_production": acc.power_production,
        "economic_power": _q(acc.economic_power),
        "starting_power_score": _q(acc.eup + acc.economic_power),
        "_discounts": acc.discounts,
        "_eup_raw": acc.eup,
        "_eup_attack_raw": acc.eup_attack,
        "_eup_defense_raw": acc.eup_defense,
        "_economic_raw": acc.economic_power,
    }
    return row


def _alliance_rows(
    alliances: list[str],
    faction_rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    cfg: BalanceConfig,
    map_strongholds: int,
) -> list[dict[str, Any]]:
    thresholds = _stronghold_thresholds(manifest)
    rows: list[dict[str, Any]] = []
    for alliance in alliances:
        members = [row for row in faction_rows if row["alliance"] == alliance]
        eup = sum(row["_eup_raw"] for row in members)
        economic = sum(row["_economic_raw"] for row in members)
        owned = sum(int(row["strongholds"]) for row in members)
        target = thresholds.get(alliance)
        needed = None if target is None else max(0, target - owned)
        resource = eup + economic
        rows.append(
            {
                "id": alliance,
                "display_name": _alliance_label(alliance),
                "territories": sum(int(row["territories"]) for row in members),
                "strongholds": owned,
                "stronghold_target": target,
                "strongholds_to_win": needed,
                "victory_kept": 1.0,
                "victory_adjustment": 0,
                "units": sum(int(row["units"]) for row in members),
                "unit_power": sum(int(row["unit_power"]) for row in members),
                "effective_unit_power_attack": _q(sum(row["_eup_attack_raw"] for row in members)),
                "effective_unit_power_defense": _q(sum(row["_eup_defense_raw"] for row in members)),
                "effective_unit_power": _q(eup),
                "position_ratio": _ratio(eup, sum(int(row["unit_power"]) for row in members)),
                "immediate_attack_power": sum(int(row["immediate_attack_power"]) for row in members),
                "immediate_defense_power": sum(int(row["immediate_defense_power"]) for row in members),
                "unreachable_attack_power": sum(int(row["unreachable_attack_power"]) for row in members),
                "power_production": sum(int(row["power_production"]) for row in members),
                "economic_power": _q(economic),
                "starting_power_score": _q(resource),
                "_score_raw": resource,
                "_resource_raw": resource,
            }
        )
    _apply_victory_outcomes(rows, map_strongholds, cfg)
    raw_scores = [float(row["_score_raw"]) for row in rows]
    total = sum(raw_scores)
    if total > 0 and rows:
        if len(rows) == 1:
            shares = [1.0]
        else:
            shares = [_q(row["_score_raw"] / total, "0.0001") for row in rows[:-1]]
            shares.append(_q(1 - sum(shares), "0.0001"))
        for row, share in zip(rows, shares):
            row["starting_power_share"] = share
    else:
        for row in rows:
            row["starting_power_share"] = None
    for row in rows:
        row.pop("_score_raw", None)
        row.pop("_resource_raw", None)
    return rows


def _wipe_score(row: dict[str, Any]) -> None:
    row["victory_kept"] = 0.0
    row["victory_adjustment"] = _q(-float(row["_resource_raw"]))
    row["starting_power_score"] = 0
    row["_score_raw"] = 0.0


def _grant_share_tokens(rows: list[dict[str, Any]], takers: list[dict[str, Any]]) -> None:
    """Give the remaining share to takers when every displayed score is already zero."""
    if not takers or sum(float(row["_score_raw"]) for row in rows) > 0:
        return
    share = _q(1 / len(takers), "0.0001")
    taker_ids = {id(row) for row in takers}
    for row in rows:
        row["_score_raw"] = share if id(row) in taker_ids else 0.0


def _apply_closeness(row: dict[str, Any], cfg: BalanceConfig) -> None:
    needed = row.get("strongholds_to_win")
    if not isinstance(needed, int) or needed <= 0:
        row["victory_kept"] = 1.0
        return
    factor = cfg.closeness(needed)
    resource = float(row["_resource_raw"])
    score = resource * factor
    row["victory_kept"] = factor
    row["_score_raw"] = score
    row["starting_power_score"] = _q(score)
    row["victory_adjustment"] = _q(score - resource)


def _apply_victory_outcomes(rows: list[dict[str, Any]], map_strongholds: int, cfg: BalanceConfig) -> None:
    """Turn an already-decided or unreachable stronghold target into the share."""
    for row in rows:
        row["strongholds_on_map"] = map_strongholds
        target = row.get("stronghold_target")
        needed = row.get("strongholds_to_win")
        if not isinstance(target, int) or not isinstance(needed, int):
            row["victory_possible"] = None
            row["_victory_status"] = "unset"
            continue
        if target > map_strongholds:
            row["victory_possible"] = False
            row["_victory_status"] = "impossible"
            continue
        row["victory_possible"] = True
        row["_victory_status"] = "won" if needed <= 0 else "open"

    won = [row for row in rows if row["_victory_status"] == "won"]
    impossible = [row for row in rows if row["_victory_status"] == "impossible"]
    open_rows = [row for row in rows if row["_victory_status"] == "open"]
    others = [row for row in rows if row["_victory_status"] != "won"]
    for row in open_rows:
        _apply_closeness(row, cfg)

    if won and others:
        for row in others:
            _wipe_score(row)
        _grant_share_tokens(rows, won)
    elif impossible and open_rows:
        for row in impossible:
            _wipe_score(row)
        _grant_share_tokens(rows, open_rows)
    elif impossible and not open_rows and not won:
        for row in impossible:
            resource = float(row["_resource_raw"])
            row["_score_raw"] = resource
            row["starting_power_score"] = _q(resource)
            row["victory_adjustment"] = 0

    for row in rows:
        row.pop("_victory_status", None)


def _largest_discounts(faction_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = {row["id"]: row["display_name"] for row in faction_rows}
    alliances = {row["id"]: row["alliance"] for row in faction_rows}
    found: list[dict[str, Any]] = []
    for row in faction_rows:
        for item in row.get("_discounts") or []:
            stamped = dict(item)
            stamped["faction_name"] = names.get(stamped["faction_id"], stamped["faction_id"])
            stamped["alliance"] = alliances.get(stamped["faction_id"], row["alliance"])
            stamped["effective_unit_power"] = _q(stamped["effective_unit_power"])
            stamped["power_discounted"] = _q(stamped["power_discounted"])
            found.append(stamped)
    found.sort(key=lambda item: (-item["power_discounted"], -item["unit_power"], item["territory_name"], item["unit_name"]))
    return found[:8]


def _readings(alliances: list[dict[str, Any]], horizon: int) -> list[str]:
    scored = [row for row in alliances if row.get("starting_power_share") is not None]
    lines: list[str] = []
    if len(scored) >= 2:
        ordered = sorted(scored, key=lambda row: row["starting_power_share"], reverse=True)
        parts = [f"{row['display_name']} {_pct(row['starting_power_share'])}" for row in ordered]
        gap = ordered[0]["starting_power_share"] - ordered[1]["starting_power_share"]
        body = "Modeled starting power is " + ", ".join(parts) + "."
        if gap < 0.01:
            lines.append(
                f"{ordered[0]['display_name']} and {ordered[1]['display_name']} are nearly even. {body}"
            )
        else:
            lines.append(body)
    elif len(scored) == 1:
        lines.append(f"{scored[0]['display_name']} holds all modeled starting power in this setup.")

    if len(alliances) >= 2:
        by_attack = sorted(alliances, key=lambda row: row["immediate_attack_power"], reverse=True)
        top, second = by_attack[0], by_attack[1]
        if top["immediate_attack_power"] > 0 and second["immediate_attack_power"] > 0:
            if top["immediate_attack_power"] >= second["immediate_attack_power"] * 1.1:
                lines.append(
                    f"{top['display_name']} has more power that can attack an important target immediately "
                    f"({top['immediate_attack_power']} compared with {second['immediate_attack_power']})."
                )
            else:
                lines.append(
                    f"Immediate attack power is close: {top['display_name']} {top['immediate_attack_power']}, "
                    f"{second['display_name']} {second['immediate_attack_power']}."
                )
        by_econ = sorted(alliances, key=lambda row: row["economic_power"], reverse=True)
        econ_top, econ_second = by_econ[0], by_econ[1]
        if econ_top["economic_power"] > econ_second["economic_power"] + 0.05:
            lines.append(
                f"{econ_top['display_name']} has the larger {horizon}-round economy "
                f"({_num(econ_top['economic_power'])} compared with {_num(econ_second['economic_power'])})."
            )
        else:
            lines.append(
                f"{horizon}-round economies are close: {econ_top['display_name']} {_num(econ_top['economic_power'])}, "
                f"{econ_second['display_name']} {_num(econ_second['economic_power'])}."
            )

    victory_bits: list[str] = []
    for row in alliances:
        needed = row.get("strongholds_to_win")
        if needed is None:
            continue
        if row.get("victory_possible") is False:
            victory_bits.append(
                f"{row['display_name']} must hold {row.get('stronghold_target')} strongholds to win, "
                f"and the map has {row.get('strongholds_on_map')}, so {row['display_name']} cannot win"
            )
        elif needed == 0:
            victory_bits.append(f"{row['display_name']} already holds enough strongholds to win")
        elif needed == 1:
            victory_bits.append(f"{row['display_name']} needs 1 more stronghold to win")
        else:
            victory_bits.append(f"{row['display_name']} needs {needed} more strongholds to win")
    if victory_bits:
        lines.append(". ".join(_sentence(bit) for bit in victory_bits) + ".")
    won = [row for row in alliances if row.get("victory_possible") is True and row.get("strongholds_to_win") == 0]
    impossible = [row for row in alliances if row.get("victory_possible") is False]
    open_rows = [
        row for row in alliances
        if row.get("victory_possible") is True and isinstance(row.get("strongholds_to_win"), int) and row["strongholds_to_win"] > 0
    ]
    if won and (impossible or open_rows):
        name = won[0]["display_name"]
        other = (impossible + open_rows)[0]["display_name"]
        lines.append(f"{name} wins at the start, so {name} takes the whole share and {other} scores 0.")
    elif impossible and open_rows:
        name = open_rows[0]["display_name"]
        lines.append(f"{name} takes the whole share.")
    elif impossible and not open_rows and not won:
        lines.append("No alliance can reach its stronghold target, so the share compares units and economy only.")
    elif len(open_rows) >= 2:
        lightest = min(open_rows, key=lambda row: row["strongholds_to_win"])
        heaviest = max(open_rows, key=lambda row: row["strongholds_to_win"])
        gap = heaviest["strongholds_to_win"] - lightest["strongholds_to_win"]
        if gap > 0:
            noun = "stronghold" if gap == 1 else "strongholds"
            lines.append(
                f"{lightest['display_name']} needs {gap} fewer {noun} than {heaviest['display_name']}, "
                f"so {lightest['display_name']} keeps {_pct(lightest['victory_kept'])} of its strength "
                f"and {heaviest['display_name']} keeps {_pct(heaviest['victory_kept'])}."
            )

    for row in alliances:
        up = row["unit_power"]
        stranded = row["unreachable_attack_power"]
        if up > 0 and stranded / up >= 0.15:
            lines.append(
                f"{row['display_name']} has {stranded} unit power with no route to an important target, "
                "so that portion stays at the availability floor. Land armies do not board ships in this model."
            )
    return lines


def _closeness_ladder(cfg: BalanceConfig) -> list[dict[str, Any]]:
    rows = [
        {"needed": str(index + 1), "kept": factor}
        for index, factor in enumerate(cfg.victory_closeness)
    ]
    rows.append({"needed": f"{len(cfg.victory_closeness) + 1}+", "kept": cfg.victory_closeness_floor})
    return rows


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _stronghold_thresholds(manifest: dict[str, Any]) -> dict[str, int]:
    victory = _as_dict(manifest.get("victory_criteria"))
    strongholds = _as_dict(victory.get("strongholds"))
    out: dict[str, int] = {}
    for key, raw in strongholds.items():
        if not isinstance(key, str):
            continue
        number = _nonneg_int(raw)
        if number is not None:
            out[key] = number
    return out


def _controlled_faction(faction_id: str, factions: dict[str, _Faction]) -> _Faction | None:
    """Playable faction row for a faction or subfaction id."""
    faction = factions.get(faction_id)
    if faction is None:
        return None
    if faction.is_subfaction and faction.parent:
        return factions.get(faction.parent) or faction
    return faction


def _faction_order(factions: dict[str, _Faction], turn_order: list[str]) -> list[str]:
    scored = [
        fid for fid, faction in factions.items()
        if faction.alliance not in ("", "neutral") and not faction.is_subfaction
    ]
    ordered = [fid for fid in turn_order if fid in scored]
    ordered.extend(fid for fid in scored if fid not in ordered)
    return ordered


def _alliance_order(factions: dict[str, _Faction], faction_ids: list[str]) -> list[str]:
    found: list[str] = []
    for fid in faction_ids:
        alliance = factions[fid].alliance
        if alliance not in found:
            found.append(alliance)
    preferred = [name for name in ("good", "evil") if name in found]
    preferred.extend(name for name in found if name not in preferred)
    return preferred


def _owner_alliance(owner: str | None, factions: dict[str, _Faction]) -> str | None:
    if not owner:
        return None
    faction = factions.get(owner)
    if not faction or faction.alliance in ("", "neutral"):
        return "neutral" if faction else None
    return faction.alliance


def _alliance_label(alliance: str) -> str:
    if alliance == "good":
        return "Good"
    if alliance == "evil":
        return "Evil"
    return alliance[:1].upper() + alliance[1:] if alliance else alliance


def _parse_territories(raw: dict[str, Any]) -> dict[str, _Territory]:
    out: dict[str, _Territory] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        tid = value.get("id") if isinstance(value.get("id"), str) and value.get("id") else key
        produces = value.get("produces")
        power = 0
        if isinstance(produces, dict):
            power = _nonneg_int(produces.get("power")) or 0
        elif isinstance(produces, (int, float)) and not isinstance(produces, bool):
            power = _nonneg_int(produces) or 0
        terrain = value.get("terrain_type")
        out[tid] = _Territory(
            id=tid,
            display_name=_text(value.get("display_name"), tid),
            adjacent=_str_list(value.get("adjacent")),
            aerial_adjacent=_str_list(value.get("aerial_adjacent")),
            ford_adjacent=_str_list(value.get("ford_adjacent")),
            power=power,
            is_stronghold=value.get("is_stronghold") is True,
            terrain=terrain.strip().lower() if isinstance(terrain, str) else "",
            ownable=value.get("ownable") is not False,
        )
    return out


def _parse_factions(raw: dict[str, Any]) -> dict[str, _Faction]:
    out: dict[str, _Faction] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        fid = value.get("id") if isinstance(value.get("id"), str) and value.get("id") else key
        alliance = value.get("alliance")
        capital = value.get("capital")
        alliance_s = alliance.strip().lower() if isinstance(alliance, str) else ""
        out[fid] = _Faction(
            id=fid,
            display_name=_text(value.get("display_name"), fid),
            alliance=alliance_s,
            capital=capital.strip() if isinstance(capital, str) else "",
        )
        subs = value.get("subfactions")
        if not isinstance(subs, list):
            continue
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            sid = sub.get("id")
            if not isinstance(sid, str) or not sid.strip():
                continue
            sid = sid.strip()
            out[sid] = _Faction(
                id=sid,
                display_name=_text(sub.get("display_name"), sid),
                alliance=alliance_s,
                capital="",
                parent=fid,
                is_subfaction=True,
            )
    return out


def _parse_units(raw: dict[str, Any]) -> dict[str, _UnitDef]:
    out: dict[str, _UnitDef] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        uid = value.get("id") if isinstance(value.get("id"), str) and value.get("id") else key
        faction = value.get("faction")
        if not isinstance(faction, str) or not faction:
            continue
        out[uid] = _UnitDef(
            id=uid,
            display_name=_text(value.get("display_name"), uid),
            faction=faction,
            power=_power_cost(value.get("cost")),
            movement=_movement(value.get("movement")),
            mobility=_mobility(value),
        )
    return out


def _parse_stacks(raw: Any, unit_defs: dict[str, _UnitDef]) -> list[_Stack]:
    if not isinstance(raw, dict):
        return []
    stacks: list[_Stack] = []
    for territory_id, entries in raw.items():
        if not isinstance(territory_id, str) or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            unit_id = entry.get("unit_id")
            if not isinstance(unit_id, str):
                continue
            unit = unit_defs.get(unit_id)
            if not unit:
                continue
            count = entry.get("count")
            n = 1 if count is None else (_nonneg_int(count) or 0)
            if n <= 0:
                continue
            stacks.append(_Stack(territory_id=territory_id, unit=unit, count=n))
    return stacks


def _mobility(unit: dict[str, Any]) -> str:
    archetype = unit.get("archetype") if isinstance(unit.get("archetype"), str) else ""
    tags = set(_str_list(unit.get("tags")))
    if archetype == "aerial" or "aerial" in tags:
        return "air"
    if archetype == "naval" or "naval" in tags:
        return "sea"
    return "land"


def _power_cost(cost: Any) -> int:
    if isinstance(cost, dict):
        return _nonneg_int(cost.get("power")) or 0
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return _nonneg_int(cost) or 0
    return 0


def _movement(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _nonneg_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or isinstance(raw, str):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw >= 0:
        return int(raw)
    return None


def _str_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _text(raw: Any, fallback: str) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _ratio(part: float, whole: float) -> float | None:
    if whole <= 0:
        return None
    return _q(part / whole, "0.001")


def _q(value: float, places: str = "0.01") -> float:
    return float(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _pct(share: float) -> str:
    return f"{_q(share * 100, '0.1'):.1f}%"


def _num(value: float) -> str:
    rounded = _q(value)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}"
