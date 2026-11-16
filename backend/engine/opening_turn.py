"""Cheap purchase and placement placeholders for complete opening turns."""
from dataclasses import replace

from backend.ai.formulas import get_unit_power_cost, territory_expected_gain_components
from backend.engine.definitions import controlling_faction_id
from backend.engine.movement import get_reachable_territories_for_unit, _is_water_zone, _is_water_craft
from backend.engine.queries import participates_in_sea_hex_naval_combat
from backend.engine.special_rules import territory_current_power
from backend.engine.utils import get_unit_faction
from backend.engine.actions import end_phase, mobilize_units, purchase_units
from backend.engine.queries import get_purchasable_units, validate_action


def _largest_legal_count(make_action, maximum, state, ud, td, fd, camps, ports):
    """Purchase/placement legality is monotonic in count for a single unit type."""
    low, high = 0, maximum
    while low < high:
        count = (low + high + 1) // 2
        if validate_action(state, make_action(count), ud, td, fd, camps, ports).valid:
            low = count
        else:
            high = count - 1
    return low


def purchase_cheapest(state, ud, td, fd, camps, ports):
    """Buy the maximum legal quantity of the cheapest affordable, deployable type."""
    faction = state.current_faction
    choices = sorted(get_purchasable_units(state, faction, ud, fd),
                     key=lambda row: (row['cost'].get('power', 0), sum(row['cost'].values()), row['unit_id']))
    for row in choices:
        maximum = row['max_affordable']
        if maximum <= 0:
            continue
        make_action = lambda count: purchase_units(faction, {row['unit_id']: count})
        count = _largest_legal_count(make_action, maximum, state, ud, td, fd, camps, ports)
        if count:
            return make_action(count)
    return None


def _can_fight_at(unit_def, territory_def, unit=None):
    if unit_def is None:
        return False
    if _is_water_zone(territory_def):
        return participates_in_sea_hex_naval_combat(unit, unit_def)
    return not _is_water_craft(unit_def)


def mobilization_threat_power(state, ud, td, fd):
    """Opposing UP able to reach each destination on its next combat move.

    Calculate each enemy's legal reach once for all destinations. Restore full
    movement even if that faction already acted this round; ignore allies and
    factions that have no turn. Subfaction units act on their parent's turn.
    """
    faction = state.current_faction
    alliance = getattr(fd.get(faction), 'alliance', None)
    active = set(state.turn_order or fd)
    threats = {tid: 0 for tid in state.territories}
    for origin, territory in state.territories.items():
        for unit in territory.units:
            owner = get_unit_faction(unit, ud)
            controller = controlling_faction_id(fd, owner) if owner else None
            if controller not in active or controller == faction:
                continue
            if alliance and getattr(fd.get(owner), 'alliance', None) == alliance:
                continue
            power = get_unit_power_cost(ud.get(unit.unit_id))
            if power <= 0 or unit.base_movement <= 0:
                continue
            full = replace(unit, remaining_movement=unit.base_movement)
            reachable, _ = get_reachable_territories_for_unit(
                full, origin, state, ud, td, fd, 'combat_move',
                acting_faction_id=controller, slot_check_state=state)
            for dest in reachable:
                if dest in threats and _can_fight_at(ud.get(unit.unit_id), td.get(dest), unit):
                    threats[dest] += power
    return threats


def _defending_power(state, dest, ud, td, fd):
    alliance = getattr(fd.get(state.current_faction), 'alliance', None)
    power = 0
    for unit in state.territories[dest].units:
        owner = get_unit_faction(unit, ud)
        friendly = owner == state.current_faction or (alliance and getattr(fd.get(owner), 'alliance', None) == alliance)
        if friendly and _can_fight_at(ud.get(unit.unit_id), td.get(dest), unit):
            power += get_unit_power_cost(ud.get(unit.unit_id))
    # Mobilizations remain pending until phase end. Count them now so each
    # allocation reduces this destination's need before choosing the next one.
    for pending in state.pending_mobilizations or []:
        if pending.destination == dest:
            power += sum(get_unit_power_cost(ud.get(row['unit_id'])) * row['count'] for row in pending.units
                         if _can_fight_at(ud.get(row['unit_id']), td.get(dest)))
    return power


def _deployment_rank(threat, defending, value):
    # First cover weighted UP deficits; then spread surplus toward remaining
    # pressure. Static territory value only breaks ties when no threat remains.
    return (value * max(0, threat - defending),
            value * threat / (1 + defending),
            value / (1 + defending))


def place_purchases(state, ud, td, fd, camps, ports, *, threat_power=None):
    """Distribute legal purchases by reachable enemy UP and territory value.

    Re-score after each allocation, counting existing and queued allied defense.
    A safe capital gets no threat priority. Legality stays with the engine.
    """
    faction = state.current_faction
    threats = threat_power if threat_power is not None else mobilization_threat_power(state, ud, td, fd)
    candidates = []
    for stack in state.faction_purchased_units.get(faction, []):
        if stack.count <= 0:
            continue
        for dest in sorted(state.territories):
            make_action = lambda count: mobilize_units(faction, dest, [{'unit_id': stack.unit_id, 'count': count}])
            if not validate_action(state, make_action(1), ud, td, fd, camps, ports).valid:
                continue
            objective, _ = territory_expected_gain_components(dest, td, fd, camps, ports, ud)
            value = 1 + objective + 3 * territory_current_power(state, dest, td[dest])
            defending = _defending_power(state, dest, ud, td, fd)
            candidates.append((_deployment_rank(threats[dest], defending, value),
                               dest, stack.unit_id, stack.count, defending, value))
    if not candidates:
        return end_phase(faction)
    # Stable ties do not depend on the order of setup dictionaries.
    candidates.sort(key=lambda item: (item[1], item[2]))
    chosen = max(candidates, key=lambda item: item[0])
    rank, dest, uid, maximum, defending, value = chosen
    make_action = lambda count: mobilize_units(faction, dest, [{'unit_id': uid, 'count': count}])
    limit = _largest_legal_count(make_action, maximum, state, ud, td, fd, camps, ports)
    # Batch only while this remains the best destination. Multiple pressured
    # fronts are revisited as their current/queued defensive UP grows.
    other_rank = max((item[0] for item in candidates if item is not chosen), default=None)
    count = 1
    unit_power = get_unit_power_cost(ud.get(uid))
    while count < limit and (other_rank is None or _deployment_rank(threats[dest], defending + count * unit_power, value) > other_rank):
        count += 1
    return make_action(count)
