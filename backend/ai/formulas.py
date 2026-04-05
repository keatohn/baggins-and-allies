"""
AI decision formulas: balance variables (gain vs cost, defense vs offense).
See docs/AI_PLAYER_DESIGN.md "Formulas to define (your input needed)".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.engine.utils import has_unit_special

from backend.ai.habits import (
    DEFENSE_ARCHER_MULTIPLIER,
    ANTICAV_VS_CAVALRY_MULTIPLIER,
    TERROR_ATTACK_BONUS,
    AERIAL_VALUE_BONUS,
    MOVEMENT_MIN_FOR_ATTACK,
    EXPECTED_STRONGHOLD_VALUE,
    EXPECTED_CAPITAL_VALUE,
    EXPECTED_CAMP_VALUE,
    EXPECTED_PORT_VALUE,
    EXPECTED_HOME_VALUE,
    EXPECTED_POWER_MULTIPLIER,
)

if TYPE_CHECKING:
    from backend.engine.definitions import (
        UnitDefinition,
        TerritoryDefinition,
        FactionDefinition,
    )
    from backend.engine.state import GameState


# ----- Battle gain (from doc: A=15, B=10, C=3) -----
BATTLE_GAIN_STRONGHOLD = 15
BATTLE_GAIN_CAPITAL = 10
BATTLE_GAIN_POWER_PER = 3


def get_unit_power_cost(unit_def: UnitDefinition | None) -> int:
    """Power cost of one unit. Returns 0 if no cost or not a dict."""
    if not unit_def:
        return 0
    cost = getattr(unit_def, "cost", None)
    if isinstance(cost, dict):
        return int(cost.get("power", 0) or 0)
    return 0


def territory_reinforce_base_score(territory_id: str, td, fd) -> float:
    """Static part of reinforce priority: stronghold, capital, power production (see non_combat _reinforce_value)."""
    tdef = td.get(territory_id)
    if not tdef:
        return 0.0
    base = 1.0
    if getattr(tdef, "is_stronghold", False):
        base += 3.0
    for f in (fd or {}).values():
        if getattr(f, "capital", None) == territory_id:
            base += 2.0
            break
    power = 0
    if getattr(tdef, "produces", None) and isinstance(tdef.produces, dict):
        power = tdef.produces.get("power", 0) or 0
    base += float(power) * 0.5
    return base


def battle_gain_if_win(
    territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> float:
    """
    Value of winning a battle at this territory (we gain it).
    Formula: stronghold A + capital B + power_production * C.
    """
    tdef = territory_defs.get(territory_id)
    if not tdef:
        return 0.0
    gain = 0.0
    if getattr(tdef, "is_stronghold", False):
        gain += BATTLE_GAIN_STRONGHOLD
    for fd in (faction_defs or {}).values():
        if getattr(fd, "capital", None) == territory_id:
            gain += BATTLE_GAIN_CAPITAL
            break
    power = 0
    if getattr(tdef, "produces", None) and isinstance(tdef.produces, dict):
        power = int(tdef.produces.get("power", 0) or 0)
    gain += power * BATTLE_GAIN_POWER_PER
    return gain


def battle_expected_cost(
    attacker_casualty_cost_mean: float,
    defender_casualty_cost_mean: float,
) -> float:
    """
    Net expected cost to us (attacker): our casualty cost minus their casualty cost.
    Positive = we pay more than we inflict; negative = we inflict more than we pay.
    """
    return attacker_casualty_cost_mean - defender_casualty_cost_mean


def attack_expected_loss(attacker_casualty_cost_mean: float) -> float:
    """
    Expected unit power loss when attacking (our casualties).
    """
    return attacker_casualty_cost_mean


def battle_net_value(
    win_rate: float,
    gain_if_win: float,
    expected_cost: float,
) -> float:
    """Net value of attacking: (win_rate * gain) - expected_cost. Attack when > 0."""
    return (win_rate * gain_if_win) - expected_cost


def battle_net_value_expected(
    win_rate: float,
    gain_if_win: float,
    expected_loss: float,
) -> float:
    """Expected value: gain * win_rate - expected_loss * (1 - win_rate)."""
    return win_rate * gain_if_win - expected_loss * (1.0 - win_rate)


def territory_expected_gain_components(
    territory_id: str,
    territory_defs: dict,
    faction_defs: dict,
    camp_defs: dict | None = None,
    port_defs: dict | None = None,
    unit_defs: dict | None = None,
) -> tuple[float, float]:
    """
    (territory_objective_bonus, power_production).
    territory_objective_bonus includes stronghold/capital plus camp/port/home markers.
    """
    tdef = territory_defs.get(territory_id)
    if not tdef:
        return 0.0, 0.0
    objective_bonus = float(getattr(tdef, "is_stronghold", False)) * EXPECTED_STRONGHOLD_VALUE
    is_capital = 0.0
    for fd in (faction_defs or {}).values():
        if getattr(fd, "capital", None) == territory_id:
            is_capital = 1.0
            break
    objective_bonus += is_capital * EXPECTED_CAPITAL_VALUE
    # Camp / port / home are optional inputs because some callers may not pass setup defs.
    if camp_defs:
        has_camp = any(getattr(c, "territory_id", None) == territory_id for c in camp_defs.values())
        if has_camp:
            objective_bonus += EXPECTED_CAMP_VALUE
    if port_defs:
        has_port = any(getattr(p, "territory_id", None) == territory_id for p in port_defs.values())
        if has_port:
            objective_bonus += EXPECTED_PORT_VALUE
    if unit_defs:
        has_home = any(
            territory_id in (getattr(u, "home_territory_ids", None) or [])
            for u in unit_defs.values()
        )
        if has_home:
            objective_bonus += EXPECTED_HOME_VALUE
    power = 0.0
    if getattr(tdef, "produces", None) and isinstance(tdef.produces, dict):
        power = float(tdef.produces.get("power", 0) or 0)
    return (objective_bonus, power)


def expected_net_gain(
    win_rate: float,
    territory_id: str,
    territory_defs: dict,
    faction_defs: dict,
    defender_casualty_cost_mean: float,
    attacker_casualty_cost_mean: float,
    camp_defs: dict | None = None,
    port_defs: dict | None = None,
    unit_defs: dict | None = None,
) -> float:
    """
    Expected net gain for attacking (positive = good, negative = bad).
    expected_objective_gain = win_rate * objective_bonus
    expected_power_gain = win_rate * power_production * 3
    expected_unit_gain = def_cas_mean - att_cas_mean  (not multiplied by win_rate)
    expected_net_gain = expected_objective_gain + exp_power_gain + exp_unit_gain
    """
    objective_bonus, power_production = territory_expected_gain_components(
        territory_id, territory_defs, faction_defs, camp_defs, port_defs, unit_defs
    )
    expected_objective_gain = win_rate * objective_bonus
    expected_power_gain = win_rate * power_production * EXPECTED_POWER_MULTIPLIER
    expected_unit_gain = defender_casualty_cost_mean - attacker_casualty_cost_mean
    return expected_objective_gain + expected_power_gain + expected_unit_gain


# ----- Unit value per power (from doc) -----


def _has_anti_cavalry(unit_def: UnitDefinition | None) -> bool:
    if not unit_def:
        return False
    tags = getattr(unit_def, "tags", []) or []
    if "anti_cavalry" in tags:
        return True
    specials = getattr(unit_def, "specials", []) or []
    return "anti_cavalry" in (specials if isinstance(specials, list) else [])


def _has_terror(unit_def: UnitDefinition | None) -> bool:
    if not unit_def:
        return False
    tags = getattr(unit_def, "tags", []) or []
    if "terror" in tags:
        return True
    specials = getattr(unit_def, "specials", []) or []
    return "terror" in (specials if isinstance(specials, list) else [])


def _is_aerial(unit_def: UnitDefinition | None) -> bool:
    if not unit_def:
        return False
    if getattr(unit_def, "archetype", "") == "aerial":
        return True
    tags = getattr(unit_def, "tags", None) or []
    return isinstance(tags, list) and "aerial" in tags


def _is_siegework(unit_def: UnitDefinition | None) -> bool:
    """True if unit is siegework archetype (rolls in siegeworks round vs strongholds)."""
    return bool(unit_def and getattr(unit_def, "archetype", "") == "siegework")


def defense_value_per_power(
    unit_def: UnitDefinition | None,
    power_cost: int,
    turns_to_reach: int = 0,
    enemy_has_cavalry: bool = False,
    archer_multiplier: float = DEFENSE_ARCHER_MULTIPLIER,
    anticav_multiplier: float = ANTICAV_VS_CAVALRY_MULTIPLIER,
) -> float:
    """
    defense_value = ((defense * rolls * health) + (is_archer * (defense-1)/2 * archer_multiplier) - turns_to_reach) / power_cost.
    If enemy_has_cavalry and unit has anti_cavalry, result is multiplied by anticav_multiplier.
    """
    if not unit_def or power_cost <= 0:
        return 0.0
    defense = int(getattr(unit_def, "defense", 0) or 0)
    health = int(getattr(unit_def, "health", 0) or 0)
    dice = int(getattr(unit_def, "dice", 1) or 1)
    base = (defense * dice * health)
    if has_unit_special(unit_def, "archer"):
        base += max(0, (defense - 1)) / 2.0 * archer_multiplier
    if _is_aerial(unit_def):
        base += AERIAL_VALUE_BONUS
    base -= turns_to_reach
    val = max(0.0, base) / power_cost
    if enemy_has_cavalry and _has_anti_cavalry(unit_def):
        val *= anticav_multiplier
    return val


def attack_value_per_power(
    unit_def: UnitDefinition | None,
    power_cost: int,
    turns_to_reach: int = 0,
    enemy_has_cavalry: bool = False,
    anticav_multiplier: float = ANTICAV_VS_CAVALRY_MULTIPLIER,
    attack_needs_siege: bool = False,
) -> float:
    """
    attack_value = ((attack + (movement-1) * rolls * health) + transport_capacity + len(specials)
        + (attack_needs_siege * is_siegework * attack * (rolls-1)) - turns_to_reach) / power_cost.
    If enemy_has_cavalry and unit has anti_cavalry, result is multiplied by anticav_multiplier.
    attack_needs_siege: true when evaluating for a potential attack on a stronghold reachable from mobilization in 1-2 turns.
    """
    if not unit_def or power_cost <= 0:
        return 0.0
    attack = int(getattr(unit_def, "attack", 0) or 0)
    movement = int(getattr(unit_def, "movement", 0) or 0)
    health = int(getattr(unit_def, "health", 0) or 0)
    dice = int(getattr(unit_def, "dice", 1) or 1)
    transport = int(getattr(unit_def, "transport_capacity", 0) or 0)
    specials = getattr(unit_def, "specials", []) or []
    if not isinstance(specials, list):
        specials = []
    mov_term = max(MOVEMENT_MIN_FOR_ATTACK, (movement - 1)) * dice * health
    terror_bonus = TERROR_ATTACK_BONUS if _has_terror(unit_def) else 0.0
    base = attack + mov_term + transport + terror_bonus + len(specials) - turns_to_reach
    if _is_aerial(unit_def):
        base += AERIAL_VALUE_BONUS
    # Siegework bonus when we may attack a stronghold soon and unit can get there from mobilization in 1-2 turns
    if attack_needs_siege and _is_siegework(unit_def):
        base += attack * max(0, dice - 1)
    val = max(0.0, base) / power_cost
    if enemy_has_cavalry and _has_anti_cavalry(unit_def):
        val *= anticav_multiplier
    return val


def territory_loss_cost(
    territory_id: str,
    territory_defs: dict[str, TerritoryDefinition],
    faction_defs: dict[str, FactionDefinition],
) -> float:
    """Cost to us of losing this territory (same as battle_gain_if_win for that territory)."""
    return battle_gain_if_win(territory_id, territory_defs, faction_defs)


# ----- Purchase: cost-range incentive (from doc) -----


def purchase_cost_bounds(
    unit_costs: list[int],
) -> tuple[float, float]:
    """
    Lower = mean of 2nd and 3rd cheapest unit cost.
    Upper = median of all unit costs.
    unit_costs should be sorted ascending (only purchasable units for this faction).
    """
    if not unit_costs:
        return (0.0, 999.0)
    sorted_costs = sorted(c for c in unit_costs if c > 0)
    if not sorted_costs:
        return (0.0, 999.0)
    n = len(sorted_costs)
    if n == 1:
        lower = upper = float(sorted_costs[0])
        return (lower, upper)
    # Lower: mean of 2nd and 3rd cheapest
    if n >= 3:
        lower = (sorted_costs[1] + sorted_costs[2]) / 2.0
    else:
        lower = (sorted_costs[0] + sorted_costs[1]) / 2.0
    # Upper: median
    mid = n // 2
    if n % 2:
        upper = float(sorted_costs[mid])
    else:
        upper = (sorted_costs[mid - 1] + sorted_costs[mid]) / 2.0
    return (lower, upper)


def cost_range_bonus(
    unit_cost: int,
    lower_bound: float,
    upper_bound: float,
) -> float:
    """
    Bonus for unit cost being within [lower, upper]. Outside = small penalty.
    Keeps some balance in the ranks (not only buying the single most cost-effective).
    """
    if lower_bound >= upper_bound:
        return 0.0
    if unit_cost <= 0:
        return 0.0
    if lower_bound <= unit_cost <= upper_bound:
        # In range: small positive bonus (e.g. 1.0)
        return 1.0
    if unit_cost < lower_bound:
        # Below: slight penalty (prefer not to go too cheap-only)
        return -0.3
    # Above: slight penalty (prefer not to go too expensive-only)
    return -0.3


# ----- Risk (slight risk-aversion; from doc) -----


def risk_adjustment(
    win_rate: float,
    variance_proxy: float = 0.0,
    risk_aversion: float = 0.1,
) -> float:
    """
    Slight risk-aversion: subtract something proportional to variance.
    variance_proxy could be (e.g.) variance of win rate or casualty cost across trials.
    If we don't have variance, pass 0 and no adjustment.
    """
    if variance_proxy <= 0:
        return 0.0
    return -risk_aversion * variance_proxy
