"""Isengard/Rohan excerpt of the reported live motw_2.0 scenario."""
import json
from pathlib import Path

from backend.ai.combat_move import _defender_stacks
from backend.ai.context import AIContext
from backend.ai.formulas import get_unit_power_cost
from backend.ai.mcts.combat_move import CombatMoveProblem, _bundle_move_action, apply_bundled_moves
from backend.ai.habits import COMBAT_MOVE_CONFIDENT_WIN_RATE
from backend.engine.actions import end_phase
from backend.engine.definitions import definitions_from_snapshot
from backend.engine.openings import clear_opening_cache, compute_opening_attacks
from backend.engine.opening_turn import purchase_cheapest
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.utils import initialize_game_state


def _case():
    bundle = json.loads((Path(__file__).parent / 'fixtures' / 'motw_2_opening.json').read_text())
    ud, td, fd, camps, ports = definitions_from_snapshot(bundle)
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud,
                                  starting_setup=bundle['starting_setup'], camp_defs=camps,
                                  prefire_penalty=bundle['manifest']['prefire_penalty'])
    state, _ = apply_action(state, purchase_cheapest(state, ud, td, fd, camps, ports), ud, td, fd, camps, ports)
    state, _ = apply_action(state, end_phase('isengard'), ud, td, fd, camps, ports)
    return bundle, state, ud, td, fd, camps, ports


def _stacks_for_moves(state, moves):
    counts = {}
    for origin, _dest, ids, _charge in moves:
        for unit in state.territories[origin].units:
            if unit.instance_id in ids:
                counts[unit.unit_id] = counts.get(unit.unit_id, 0) + 1
    return [{'unit_id': uid, 'count': count} for uid, count in counts.items()]


def test_fangorn_attack_does_not_consume_every_capital_unit_or_keep_adding_them():
    _bundle, state, ud, td, fd, camps, ports = _case()
    problem = CombatMoveProblem(AIContext(state, ud, td, fd, camps, ports, {'can_end_phase': True}))
    fangorn = [(score, moves) for score, moves in problem._enumerate_top_k(state) if moves[0][1] == 'fangorn']
    assert fangorn
    defenders = _defender_stacks(state, 'fangorn', 'isengard', fd, ud)
    confident = []
    for score, moves in fangorn:
        stacks = _stacks_for_moves(state, moves)
        chance, _net = problem._sim_battle(state, stacks, defenders, 'fangorn', sea_raid=False)
        if chance >= COMBAT_MOVE_CONFIDENT_WIN_RATE:
            power = sum(get_unit_power_cost(ud[row['unit_id']]) * row['count'] for row in stacks)
            confident.append((power, moves))
    assert confident
    assert max(power for power, _moves in confident) < 50  # Previously offered 67 UP for the same two Ents.
    _power, moves = min(confident)
    moved_ids = {iid for _origin, _dest, ids, _charge in moves for iid in ids}
    assert any(unit.instance_id not in moved_ids and ud[unit.unit_id].movement > 0
               for unit in state.territories['isengard'].units)
    queued, _ = apply_bundled_moves(state, _bundle_move_action('isengard', moves), ud, td, fd, camps, ports)
    projected = get_state_after_pending_moves(queued, 'combat_move', ud, td, fd)
    # An already-confident Fangorn attack must not keep collecting the remaining army.
    assert not [legs for _score, legs in problem._enumerate_top_k(queued) if legs[0][1] == 'fangorn']
    assert projected.territories['isengard'].units


def test_rohan_faces_completed_isengard_deployment():
    bundle, _state, ud, td, fd, camps, ports = _case()
    clear_opening_cache()
    result = compute_opening_attacks(ud, td, fd, camps, bundle['starting_setup'],
                                     manifest=bundle['manifest'], port_defs=ports,
                                     n_trials=100, mcts_iterations=20, mcts_seconds=0.4)
    first, second = result['factions'][:2]
    assert first['purchases'] == {'dunland_wildman': 5}
    at_capital = [row for row in first['deployments'] if row['destination'] == 'isengard']
    assert sum(stack['count'] for row in at_capital for stack in row['units']) == 4
    before_rohan = second['board']['units']['isengard']
    assert any(stack['unit_id'] == 'dunland_wildman' and stack['count'] >= 4 for stack in before_rohan)
    for attack in second['attacks']:
        if attack['to_territory'] == 'isengard':
            assert attack['empty'] is False
            assert attack['defender_stacks']
