from copy import deepcopy

from backend.balance import opening_projection as projection
from test.test_starting_strength import _line_map, _bundle, _unit


def test_projection_scores_final_board_and_preserves_setup(monkeypatch):
    territories, owners = _line_map()
    bundle = _bundle(territories=territories, owners=owners,
                     units={"soldier": _unit("soldier", "goodland", 3)},
                     starting_units={"border": [{"unit_id": "soldier", "count": 2}]})
    original = deepcopy(bundle)
    calls = []
    monkeypatch.setattr(projection, 'definitions_from_snapshot', lambda snapshot: ({}, {}, {}, {}, {}))

    def simulate(*args, **kwargs):
        calls.append(kwargs['seed'])
        owners = deepcopy(bundle['starting_setup']['territory_owners'])
        owners['dark'] = 'goodland'
        return {'final_board': {'owners': owners, 'units': {}}}

    monkeypatch.setattr(projection, 'compute_opening_attacks', simulate)
    result = projection.compute_opening_projection(bundle)
    assert calls == list(projection.SEEDS)
    assert result['runs'] == 3
    good = next(row for row in result['alliances'] if row['id'] == 'good')
    assert good['metrics']['strongholds']['change'] == 1
    assert good['metrics']['unit_power']['mean'] == 0
    assert good['metrics']['power_production']['change'] == 5
    assert bundle == original


def test_projection_samples_spread_and_matches_optional_rings(monkeypatch):
    territories, owners = _line_map()
    bundle = _bundle(territories=territories, owners=owners,
                     units={'soldier': _unit('soldier', 'goodland', 3)},
                     starting_units={'border': [{'unit_id': 'soldier', 'count': 2}]},
                     rules=[{'type': 'rings_of_power', 'is_optional': True, 'rings': []}])
    monkeypatch.setattr(projection, 'definitions_from_snapshot', lambda snapshot: ({}, {}, {}, {}, {}))
    rules_seen = []

    def simulate(*args, **kwargs):
        rules_seen.append(deepcopy(kwargs['manifest']['special_rules']))
        owners_after = deepcopy(owners)
        if kwargs['seed'] == projection.SEEDS[0]:
            owners_after['dark'] = 'goodland'
        return {'final_board': {'owners': owners_after, 'units': {}}}

    monkeypatch.setattr(projection, 'compute_opening_attacks', simulate)
    result = projection.compute_opening_projection(bundle, rings=True)
    good = next(row for row in result['alliances'] if row['id'] == 'good')
    assert good['metrics']['strongholds']['min'] == 0
    assert good['metrics']['strongholds']['max'] == 1
    assert good['metrics']['strongholds']['mean'] == 0.333
    assert all(rows[0]['is_optional'] is False for rows in rules_seen)
    rules_seen.clear()
    projection.compute_opening_projection(bundle, rings=False)
    assert rules_seen == [[], [], []]
    assert bundle['manifest']['special_rules'][0]['is_optional'] is True
