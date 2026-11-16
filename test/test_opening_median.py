"""Openings show a typical combat outcome rather than one unlucky dice sample."""
import random

from backend.engine import openings
from backend.engine.actions import end_phase
from backend.engine.definitions import FactionDefinition, TerritoryDefinition, UnitDefinition
from backend.engine.utils import initialize_game_state


def _battle():
    ud = {'a': UnitDefinition('a', 'A', 'a', 'infantry', [], 3, 1, 1, 1, {'power': 3}),
          'b': UnitDefinition('b', 'B', 'b', 'infantry', [], 1, 3, 1, 1, {'power': 3})}
    td = {'home': TerritoryDefinition('home', 'Home', 'land', ['target'], {}),
          'target': TerritoryDefinition('target', 'Target', 'land', ['home'], {'power': 1})}
    fd = {'a': FactionDefinition('a', 'A', 'good', 'home', '#fff'),
          'b': FactionDefinition('b', 'B', 'evil', 'target', '#000')}
    setup = {'turn_order': ['a', 'b'], 'territory_owners': {'home': 'a', 'target': 'b'},
             'starting_units': {'target': [{'unit_id': 'a', 'count': 3}, {'unit_id': 'b', 'count': 2}]}}
    state = initialize_game_state(faction_defs=fd, territory_defs=td, unit_defs=ud, starting_setup=setup)
    state.phase = 'combat'
    return state, ud, td, fd


def test_eighty_percent_attack_projects_a_win_even_when_first_engine_trial_loses(monkeypatch):
    state, ud, td, fd = _battle()
    attackers = [u for u in state.territories['target'].units if u.unit_id == 'a']
    defenders = [u for u in state.territories['target'].units if u.unit_id == 'b']
    median = {}
    p_win, _ = openings._sim_chance(state, 'target', 'target', attackers, defenders, ud, td,
                                    sea_raid=False, n_trials=1000, seed=8, median_outcome=median)
    assert 0.75 < p_win < 0.85
    assert median['winner'] == 'attacker' and median['conquered']
    first_rng = []
    results = []
    def unlucky_first(sampler):
        def wrapped(*args, **kwargs):
            rng = kwargs['rng']
            if not first_rng:
                first_rng.append(rng)
            dice = sampler(*args, **kwargs)
            if rng is first_rng[0]:
                return {'attacker': [6] * len(dice['attacker']), 'defender': [1] * len(dice['defender'])}
            return dice
        return wrapped
    monkeypatch.setattr(openings, 'sample_dice_for_initiate', unlucky_first(openings.sample_dice_for_initiate))
    monkeypatch.setattr(openings, 'sample_dice_for_continue', unlucky_first(openings.sample_dice_for_continue))
    apply = openings.apply_action
    def record(*args, **kwargs):
        result, events = apply(*args, **kwargs)
        results.extend(event.payload['winner'] for event in events if event.type == 'combat_ended')
        return result, events
    monkeypatch.setattr(openings, 'apply_action', record)
    result = openings._resolve_combat(state, 'a', ud, td, fd, {}, {}, random.Random(8), n_trials=1000)
    assert results[0] == 'defender'
    assert result.territories['target'].owner == 'a'
    remaining = sum(unit.unit_id == 'a' for unit in result.territories['target'].units)
    assert remaining > 0
    assert 3 - remaining == median['attacker_casualties'].get('a', 0)
    assert state.territories['target'].owner == 'b'  # Original state remains unchanged.


def test_openings_use_highest_ordered_custom_level_and_its_branch_caps(monkeypatch):
    state, ud, td, fd = _battle()
    setup = {'turn_order': ['a', 'b'], 'territory_owners': {'home': 'a', 'target': 'b'}, 'starting_units': {}}
    levels = [{'id': 'normal', 'name': 'Normal', 'iterations': 1, 'seconds': 0.05},
              {'id': 'master', 'name': 'Master', 'iterations': 9, 'seconds': 0.08,
               'combat_move_options': 31, 'non_combat_options': 7, 'aggression': 0.8, 'randomness': 0.1, 'purchase_variety': 0.2}]
    from backend.ai.mcts.budget import get_phase_budget, branch_limit
    seen = []
    def choose(ctx, rng):
        from backend.ai.style import aggression, randomness, purchase_variety
        assert aggression() == 0.8 and randomness() == 0.1 and purchase_variety() == 0.2
        seen.append((ctx.state.ai_difficulty, get_phase_budget(ctx.phase, ctx.state.ai_difficulty),
                     branch_limit(ctx.phase, ctx.state.ai_difficulty)))
        return end_phase(ctx.faction_id)
    monkeypatch.setattr(openings, 'decide_combat_move_mcts', choose)
    monkeypatch.setattr(openings, 'decide_non_combat_move_mcts', choose)
    openings.clear_opening_cache()
    result = openings.compute_opening_attacks(ud, td, fd, {}, setup, ai_levels=levels, n_trials=1)
    assert result['difficulty'] == 'master'
    assert result['mcts_iterations'] == 9
    assert result['mcts_seconds'] == 0.08
    assert all(level == 'master' and budget.max_iterations == 9 and budget.max_time_seconds == 0.08 for level, budget, cap in seen)
    assert {cap for _level, _budget, cap in seen} == {31, 7}


def test_likely_defeat_is_not_turned_into_an_automatic_capture():
    state, ud, td, fd = _battle()
    keep = next(u.instance_id for u in state.territories['target'].units if u.unit_id == 'a')
    state.territories['target'].units = [u for u in state.territories['target'].units if u.unit_id == 'b' or u.instance_id == keep]
    result = openings._resolve_combat(state, 'a', ud, td, fd, {}, {}, random.Random(8), n_trials=1000)
    assert result.territories['target'].owner == 'b'
    assert any(unit.unit_id == 'b' for unit in result.territories['target'].units)
