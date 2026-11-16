"""Opening turns must deploy real defenses before the next faction attacks."""
from copy import deepcopy

import pytest

from backend.engine.actions import end_phase, move_units
from backend.engine.definitions import CampDefinition, FactionDefinition, TerritoryDefinition, UnitDefinition
from backend.engine.opening_turn import purchase_cheapest, place_purchases
from backend.engine.openings import compute_opening_attacks, clear_opening_cache
from backend.engine.reducer import apply_action
from backend.engine.utils import initialize_game_state


def _setup():
    td = {
        'home': TerritoryDefinition('home', 'Home', 'land', ['loot', 'enemy', 'rear'], {'power': 6}),
        'loot': TerritoryDefinition('loot', 'Loot', 'land', ['home'], {}),
        'rear': TerritoryDefinition('rear', 'Rear', 'land', ['home'], {}),
        'enemy': TerritoryDefinition('enemy', 'Enemy', 'land', ['home'], {'power': 1}),
    }
    fd = {'a': FactionDefinition('a', 'A', 'good', 'home', '#fff'),
          'b': FactionDefinition('b', 'B', 'evil', 'enemy', '#000')}
    ud = {'guard': UnitDefinition('guard', 'Guard', 'a', 'infantry', [], 1, 6, 1, 1, {'power': 1}),
          'raider': UnitDefinition('raider', 'Raider', 'b', 'infantry', [], 1, 1, 1, 1, {'power': 1})}
    camps = {'home_camp': CampDefinition('home_camp', 'home')}
    starting = {'turn_order': ['a', 'b'], 'territory_owners': {'home': 'a', 'rear': 'a', 'enemy': 'b'},
                'starting_units': {'home': [{'unit_id': 'guard', 'count': 2}],
                                   'enemy': [{'unit_id': 'raider', 'count': 1}]}}
    return ud, td, fd, camps, starting


def test_purchase_placeholder_obeys_budget_capacity_and_cheapest_type():
    ud, td, fd, camps, starting = _setup()
    ud['guard'].cost = {'power': 2}
    ud['expensive'] = UnitDefinition('expensive', 'Expensive', 'a', 'infantry', [], 3, 3, 1, 1, {'power': 4})
    # Cheaper but no legal naval placement. It must not prevent buying land units.
    ud['ship'] = UnitDefinition('ship', 'Ship', 'a', 'naval', ['naval'], 3, 3, 1, 1, {'power': 1})
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud, starting_setup=starting, camp_defs=camps)
    action = purchase_cheapest(state, ud, td, fd, camps, {})
    assert action.payload == {'purchases': {'guard': 3}}
    bought, _ = apply_action(state, action, ud, td, fd, camps, {})
    assert bought.faction_resources['a']['power'] == 0
    assert state.faction_resources['a']['power'] == 6
    state.faction_resources['a']['power'] = 100
    assert purchase_cheapest(state, ud, td, fd, camps, {}).payload == {'purchases': {'guard': 6}}
    state.territories['home'].owner = 'b'
    assert purchase_cheapest(state, ud, td, fd, camps, {}) is None


def test_later_faction_faces_mobilized_capital_defenders(monkeypatch):
    from backend.engine import openings
    ud, td, fd, camps, starting = _setup()
    original = deepcopy(starting)
    clear_opening_cache()

    def combat_move(ctx, rng):
        source, dest = ('home', 'loot') if ctx.faction_id == 'a' else ('enemy', 'home')
        ids = [u.instance_id for u in ctx.state.territories[source].units if u.remaining_movement > 0]
        return move_units(ctx.faction_id, source, dest, ids) if ids else end_phase(ctx.faction_id)

    monkeypatch.setattr(openings, 'decide_combat_move_mcts', combat_move)
    monkeypatch.setattr(openings, 'decide_non_combat_move_mcts', lambda ctx, rng: end_phase(ctx.faction_id))
    result = compute_opening_attacks(ud, td, fd, camps, starting, n_trials=100, mcts_iterations=1)
    second = result['factions'][1]
    assert second['board']['units']['home'] == [{'unit_id': 'guard', 'count': 6}]
    attack = second['attacks'][0]
    assert attack['to_territory'] == 'home'
    assert attack['empty'] is False
    assert attack['defender_stacks'] == [{'unit_id': 'guard', 'count': 6}]
    assert attack['p_conquer'] == 0
    assert result['final_board']['owners']['home'] == 'a'
    assert starting == original


def test_noncombat_reinforcement_arrives_before_next_faction(monkeypatch):
    from backend.engine import openings
    ud, td, fd, camps, starting = _setup()
    starting['starting_units']['rear'] = [{'unit_id': 'guard', 'count': 3}]
    clear_opening_cache()
    monkeypatch.setattr(openings, 'decide_combat_move_mcts', lambda ctx, rng: end_phase(ctx.faction_id))

    def reinforce(ctx, rng):
        pending = {iid for pm in ctx.state.pending_moves for iid in pm.unit_instance_ids}
        ids = [u.instance_id for u in ctx.state.territories['rear'].units if u.remaining_movement > 0 and u.instance_id not in pending]
        if ctx.faction_id == 'a' and ids:
            return move_units('a', 'rear', 'home', ids)
        return end_phase(ctx.faction_id)

    monkeypatch.setattr(openings, 'decide_non_combat_move_mcts', reinforce)
    result = compute_opening_attacks(ud, td, fd, camps, starting, n_trials=1, mcts_iterations=1)
    assert result['factions'][1]['board']['units']['home'] == [{'unit_id': 'guard', 'count': 11}]
    assert 'rear' not in result['factions'][1]['board']['units']


def test_garrison_excludes_units_already_committed_to_another_attack():
    from backend.ai.garrison import prune_move_unit_ids_for_garrison_floor
    ud, td, fd, camps, starting = _setup()
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud, starting_setup=starting, camp_defs=camps)
    state.phase = 'combat_move'
    first, second = [u.instance_id for u in state.territories['home'].units]
    state, _ = apply_action(state, move_units('a', 'home', 'loot', [first]), ud, td, fd, camps)
    # The first unit is still physically on home until the queue replays.
    assert len(state.territories['home'].units) == 2
    allowed = prune_move_unit_ids_for_garrison_floor(state, 'home', [second], 'a', ud, td, fd,
                                                     threat_from_count=1, threat_relief_attack=True)
    assert allowed == []


@pytest.mark.parametrize("capital", [True, False])
def test_objective_defense_uses_hold_odds_instead_of_one_token_guard(capital):
    from backend.ai.garrison import prune_move_unit_ids_for_garrison_floor
    ud, td, fd, camps, starting = _setup()
    if not capital:
        fd['a'].capital = 'rear'
        td['home'].is_stronghold = True
    ud['raider'].attack = 6
    starting['starting_units']['home'][0]['count'] = 5
    starting['starting_units']['enemy'][0]['count'] = 3
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud, starting_setup=starting, camp_defs=camps)
    state.phase = 'combat_move'
    ids = [u.instance_id for u in state.territories['home'].units]
    # A naive count floor of one would permit four departures. Facing three
    # guaranteed-hit attackers requires more than that token defender.
    allowed = prune_move_unit_ids_for_garrison_floor(state, 'home', ids, 'a', ud, td, fd,
                                                     threat_from_count=1, effective_reach_count=1)
    assert len(allowed) < 4
    # Counterattacking the force threatening the origin retains the exemption.
    counterattack = prune_move_unit_ids_for_garrison_floor(state, 'home', ids, 'a', ud, td, fd,
                                                           threat_from_count=1, threat_relief_attack=True)
    assert len(counterattack) == 4


def test_opening_ports_are_used_for_purchase_placement_and_cache_identity(monkeypatch):
    from backend.engine import openings
    from backend.engine.definitions import PortDefinition
    ud, td, fd, camps, starting = _setup()
    td['home'].adjacent.append('bay')
    td['bay'] = TerritoryDefinition('bay', 'Bay', 'sea', ['home'], {})
    ud['ship'] = UnitDefinition('ship', 'Ship', 'a', 'naval', ['naval'], 1, 1, 1, 1, {'power': 1})
    ports = {'port': PortDefinition('port', 'home')}
    clear_opening_cache()
    monkeypatch.setattr(openings, 'decide_combat_move_mcts', lambda ctx, rng: end_phase(ctx.faction_id))
    monkeypatch.setattr(openings, 'decide_non_combat_move_mcts', lambda ctx, rng: end_phase(ctx.faction_id))
    without = compute_opening_attacks(ud, td, fd, {}, starting, n_trials=1, mcts_iterations=1)
    assert 'bay' not in without['factions'][1]['board']['units']
    with_ports = compute_opening_attacks(ud, td, fd, {}, starting, port_defs=ports, n_trials=1, mcts_iterations=1)
    assert with_ports['factions'][1]['board']['units']['bay'] == [{'unit_id': 'ship', 'count': 6}]


def _fronts():
    from backend.engine.actions import purchase_units
    ud, td, fd, camps, starting = _setup()
    td['home'].adjacent = ['rear']
    td['rear'].adjacent = ['home']
    td['front_a'] = TerritoryDefinition('front_a', 'Front A', 'land', ['enemy'], {'power': 6})
    td['front_b'] = TerritoryDefinition('front_b', 'Front B', 'land', ['enemy_b'], {'power': 6})
    td['enemy'].adjacent = ['front_a']
    td['enemy_b'] = TerritoryDefinition('enemy_b', 'Enemy B', 'land', ['front_b'], {})
    camps.update({name: CampDefinition(name, name) for name in ('front_a', 'front_b')})
    starting['territory_owners'].update({'front_a': 'a', 'front_b': 'a', 'enemy_b': 'b'})
    ud['raider'].cost = {'power': 4}
    starting['starting_units']['enemy'][0]['count'] = 2
    starting['starting_units']['enemy_b'] = [{'unit_id': 'raider', 'count': 1}]
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud, starting_setup=starting, camp_defs=camps)
    state, _ = apply_action(state, purchase_units('a', {'guard': 10}), ud, td, fd, camps)
    state.phase = 'mobilization'
    return state, ud, td, fd, camps


def test_reachable_enemy_up_distributes_deployments_without_safe_capital_priority():
    from backend.engine.opening_turn import mobilization_threat_power
    state, ud, td, fd, camps = _fronts()
    # Units that acted earlier still threaten on their next turn.
    for territory in state.territories.values():
        for unit in territory.units:
            unit.remaining_movement = 0
    original = deepcopy(state.to_dict())
    threats = mobilization_threat_power(state, ud, td, fd)
    assert threats['home'] == 0
    assert threats['front_a'] == 8
    assert threats['front_b'] == 4
    assert state.to_dict() == original
    first = place_purchases(state, ud, td, fd, camps, {}, threat_power=threats)
    assert first.payload['destination'] == 'front_a'
    assert first.payload['units'] == [{'unit_id': 'guard', 'count': 4}]
    for _ in range(12):
        action = place_purchases(state, ud, td, fd, camps, {}, threat_power=threats)
        if action.type == 'end_phase':
            break
        state, _ = apply_action(state, action, ud, td, fd, camps)
    allocated = {}
    for pending in state.pending_mobilizations:
        allocated[pending.destination] = allocated.get(pending.destination, 0) + sum(row['count'] for row in pending.units)
    assert allocated == {'front_a': 6, 'front_b': 4}


def test_existing_and_allied_defense_reduce_mobilization_need():
    from backend.engine.opening_turn import mobilization_threat_power
    from backend.engine.state import Unit
    state, ud, td, fd, camps = _fronts()
    fd['ally'] = FactionDefinition('ally', 'Ally', 'good', '', '#aaa')
    ud['ally_guard'] = UnitDefinition('ally_guard', 'Ally Guard', 'ally', 'infantry', [], 1, 6, 1, 1, {'power': 8})
    state.territories['front_a'].units = [Unit('ally_guard_1', 'ally_guard', 1, 1, 1, 1)]
    threats = mobilization_threat_power(state, ud, td, fd)
    action = place_purchases(state, ud, td, fd, camps, {}, threat_power=threats)
    assert action.payload['destination'] == 'front_b'
    assert threats['front_b'] == 4  # Allied UP must not inflate enemy pressure.


def test_stronghold_value_changes_priority_at_equal_enemy_up():
    state, ud, td, fd, camps = _fronts()
    td['front_b'].is_stronghold = True
    threats = {'home': 0, 'rear': 0, 'loot': 0, 'enemy': 0, 'enemy_b': 0, 'front_a': 4, 'front_b': 4}
    action = place_purchases(state, ud, td, fd, camps, {}, threat_power=threats)
    assert action.payload['destination'] == 'front_b'


def test_enemy_reach_ignores_allies_inactive_factions_and_blocked_routes():
    from backend.engine.opening_turn import mobilization_threat_power
    state, ud, td, fd, camps = _fronts()
    fd['b'].alliance = 'good'
    assert not any(mobilization_threat_power(state, ud, td, fd).values())
    fd['b'].alliance = 'evil'
    state.turn_order = ['a']
    assert not any(mobilization_threat_power(state, ud, td, fd).values())
    state.turn_order = ['a', 'b']
    td['enemy'].adjacent = []
    assert mobilization_threat_power(state, ud, td, fd)['front_a'] == 0
