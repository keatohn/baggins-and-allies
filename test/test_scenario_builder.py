"""Canonical JSON migration, seed resolution, admin saves and map authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.api.auth import get_current_admin
from backend.api.database import Base, get_db
from backend.api.main import app
from backend.api.models import AppSetting, Setup
from backend.engine.definitions import DATA_DIR, SETUPS_DIR, load_static_definitions, load_setup
from backend.scenario_builder import (
    BUNDLE_KEYS, DELETE, MAP_TERRITORY_FIELDS, canonical_setup, compile_setup, delta,
    extract_library, load_library, merge, migrate_legacy_setups,
    parse_setup_json, read_setup_bundle, setup_from_bundle, write_setup,
)
from backend.legacy_setup_migration import extract_source
from backend.scenario_store import SETTING, compile_all, initialize, load_state, save_definition, update_library, with_map
from backend.setup_data import get_admin_setup_bundle
from backend.tools.scenario import main

def example(setup_id="alpha", attack=5):
    return {
        "manifest": {"id": setup_id, "display_name": setup_id, "map_asset": "world", "is_active": False},
        "units": {"guard": {"id": "guard", "display_name": "Guard", "archetype": "infantry", "faction": "good",
                            "attack": attack, "defense": 3, "movement": 1, "health": 1, "specials": [], "cost": {"power": 3}}},
        "territories": {"shire": {"id": "shire", "display_name": "The Shire", "terrain_type": "plains", "ownable": True,
                                   "adjacent": [], "produces": {"power": 2}}},
        "factions": {"good": {"id": "good", "display_name": "Good", "alliance": "good", "capital": "shire", "color": "#31572c"}},
        "camps": {}, "ports": {},
        "starting_setup": {"turn_order": ["good"], "territory_owners": {"shire": "good"},
                           "starting_units": {"shire": [{"unit_id": "guard", "count": 2}]}},
    }


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("SETUP_SOURCE", "db")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    for key, attack in (("alpha", 5), ("beta", 5), ("gamma", 6)):
        bundle = example(key, attack)
        session.add(Setup(id=key, specials_json="{}", **{f"{name}_json": json.dumps(bundle[name]) for name in BUNDLE_KEYS}))
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db):
    def override_db():
        yield db
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_admin] = lambda: object()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_recursive_overrides_replacement_null_and_deletion():
    base = {"cost": {"power": 3, "other": 2}, "specials": ["heal"], "home": ["shire"], "gone": 1}
    target = {"cost": {"power": 5}, "specials": [], "home": None, "new": False}
    assert merge(base, delta(base, target)) == target
    assert base["specials"] == ["heal"]


@pytest.mark.parametrize("text", ['manifest: {}', '{"a":1,"a":2}', '{"x": NaN}', '[]', '{'])
def test_json_parser_rejects_yaml_ambiguous_and_invalid_json(text):
    with pytest.raises(ValueError):
        parse_setup_json(text)


@pytest.mark.parametrize("folder", sorted(SETUPS_DIR.glob('*/setup.json')), ids=lambda p: p.parent.name)
def test_checked_in_setups_have_one_json_and_preserve_non_geometry_data(folder):
    setup = parse_setup_json(folder.read_text())
    assert set(setup) == set(BUNDLE_KEYS)
    assert sorted(p.name for p in folder.parent.glob('*.json')) == ['setup.json']
    assert not list(folder.parent.glob('*.yaml'))
    for section in ('units', 'factions', 'territories'):
        assert all(not (entry.keys() - {'seed', 'overrides'}) for entry in setup[section].values())
    bundle = read_setup_bundle(folder.parent)
    clean = {**bundle, 'territories': {tid: {k: v for k, v in record.items() if k not in MAP_TERRITORY_FIELDS | {"terrain_type"}} for tid, record in bundle['territories'].items()}}
    baseline = json.loads((Path(__file__).parent / 'fixtures/canonical_setup_baseline.json').read_text())
    assert hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest() == baseline[folder.parent.name]
    terrain = {tid: record['terrain_type'] for tid, record in bundle['territories'].items()}
    terrain_baseline = json.loads((Path(__file__).parent / 'fixtures/terrain_seed_migration_baseline.json').read_text())
    assert hashlib.sha256(json.dumps(terrain, sort_keys=True).encode()).hexdigest() == terrain_baseline[folder.parent.name]
    assert load_setup(folder.parent.name)['starting_setup'] == bundle['starting_setup']
    assert len(load_static_definitions(setup_id=folder.parent.name)[0]) == len(bundle['units'])


def test_extraction_and_compilation_are_lossless_for_seed_aliases_and_custom_records():
    bundle = example()
    library = extract_library({'alpha': bundle})
    setup = setup_from_bundle(bundle, library)
    setup['units']['ally'] = {'seed': 'guard', 'overrides': {'attack': 8, 'cost': {'power': 6}}}
    setup['units']['custom'] = {'overrides': {'id': 'custom', 'attack': 1}}
    resolved = compile_setup(setup, library)
    assert resolved['units']['ally']['id'] == 'ally'
    assert resolved['units']['ally']['cost'] == {'power': 6}
    assert resolved['units']['custom'] == {'id': 'custom', 'attack': 1}
    assert compile_setup(setup_from_bundle(resolved, library, setup), library) == resolved


def test_explicit_equal_overrides_stay_pinned():
    bundle = example()
    library = extract_library({'alpha': bundle})
    setup = setup_from_bundle(bundle, library)
    setup['units']['guard']['overrides'] = {'attack': 5}
    edited = deepcopy(bundle)
    edited['units']['guard']['cost']['power'] = 8
    setup = setup_from_bundle(edited, library, setup)
    assert setup['units']['guard']['overrides'] == {'attack': 5, 'cost': {'power': 8}}
    library['units']['guard']['attack'] = 9
    assert compile_setup(setup, library)['units']['guard']['attack'] == 5


def test_new_definitions_reject_extends_and_have_no_geometry_overrides():
    library = extract_library({'alpha': example()})
    setup = setup_from_bundle(example(), library)
    with pytest.raises(ValueError, match='Unknown setup fields'):
        canonical_setup({**setup, 'extends': 'parent'})
    setup['territories']['shire']['overrides'] = {'adjacent': ['missing'], 'produces': {'power': 8}}
    compact = canonical_setup(setup)
    assert compact['territories']['shire']['overrides'] == {'produces': {'power': 8}}
    assert compile_setup(compact, library)['territories']['shire']['adjacent'] == []


def test_live_legacy_migration_flattens_extends_and_preserves_overrides(db):
    bundles = {'alpha': example(), 'beta': example('beta')}
    library = extract_library(bundles)
    parent = extract_source(bundles['alpha'], library)
    parent['units']['guard']['overrides']['attack'] = 5
    state = {'library': library, 'sources': {'alpha': parent, 'beta': {'version': 1, 'extends': 'alpha', 'manifest': {'id': 'beta'}, 'units': {'guard': {'overrides': {'defense': 4}}}}}}
    db.add(AppSetting(key=SETTING, value_json=json.dumps(state)))
    db.commit()
    initialize(db)
    stored = json.loads(db.get(AppSetting, SETTING).value_json)
    assert 'sources' not in stored
    assert stored['setups']['alpha']['units']['guard']['overrides']['attack'] == 5
    assert stored['setups']['beta']['units']['guard']['overrides']['defense'] == 4
    assert 'extends' not in stored['setups']['beta']
    assert stored['setups']['alpha']['units']['guard']['overrides']['attack'] == 5
    initialize(db)
    assert json.loads(db.get(AppSetting, SETTING).value_json) == stored


def test_startup_uses_live_database_not_repository_seeds(db):
    initialize(db)
    state = load_state(db)
    assert state['library']['units']['guard']['attack'] == 5
    assert compile_all(state)['gamma']['units']['guard']['attack'] == 6
    assert set(state['setups']) == {'alpha', 'beta', 'gamma'}


def test_seed_updates_warn_save_and_leave_invalid_generated_snapshots_unchanged(db):
    initialize(db)
    state = load_state(db)
    state['setups']['alpha']['factions']['good']['overrides'] = {'capital': 'missing'}
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    library = deepcopy(load_state(db)['library'])
    library['units']['guard']['attack'] = 8
    preview = update_library(db, deepcopy(library))
    result = update_library(db, library, apply=True)
    assert result == preview
    assert result['skipped_setups'] == ['alpha']
    assert result['warnings']['alpha']
    assert json.loads(db.get(Setup, 'alpha').units_json)['guard']['attack'] == 5
    assert json.loads(db.get(Setup, 'beta').units_json)['guard']['attack'] == 8
    setup = deepcopy(load_state(db)['setups']['alpha'])
    setup['factions']['good'].pop('overrides')
    result = save_definition(db, 'alpha', setup)
    assert result['bundle']['units']['guard']['attack'] == 8


def test_seed_validation_messages_are_nonblocking(db):
    initialize(db)
    library = deepcopy(load_state(db)['library'])
    library['units']['guard']['faction'] = 'missing'
    result = update_library(db, library, apply=True)
    assert result['warnings']['seeds']
    assert load_state(db)['library']['units']['guard']['faction'] == 'missing'
    assert json.loads(db.get(Setup, 'alpha').units_json)['guard']['faction'] == 'good'
    # Editing geography must not accidentally apply invalid seed changes to games.
    from backend.map_overrides import save_map_overrides
    save_map_overrides(db, 'world', {'graph': {'adjacent': {'shire': []}}})
    assert json.loads(db.get(Setup, 'alpha').units_json)['guard']['faction'] == 'good'


def test_structurally_invalid_seed_update_is_atomic(db):
    initialize(db)
    before = db.get(AppSetting, SETTING).value_json
    library = deepcopy(load_state(db)['library'])
    library['units'] = []
    with pytest.raises(ValueError):
        update_library(db, library, apply=True)
    assert db.get(AppSetting, SETTING).value_json == before


def add_new_map(db):
    initialize(db)
    state = load_state(db)
    state['library']['maps']['new_world'] = {'bree': {**example()['territories']['shire'], 'id': 'bree', 'display_name': 'Bree'}}
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    return state


def test_map_change_updates_selection_and_prunes_starting_positions_before_validation(client, db):
    state = add_new_map(db)
    setup = deepcopy(state['setups']['alpha'])
    setup['manifest']['map_asset'] = 'new_world'
    response = client.put('/admin/setups/alpha', json=setup)
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result['setup']['territories']) == {'bree'}
    assert result['bundle']['starting_setup']['territory_owners'] == {}
    assert result['bundle']['starting_setup']['starting_units'] == {}
    assert result['bundle']['factions']['good']['capital'] is None
    assert 'shire' not in result['setup']['territories']
    assert client.get('/admin/setups/alpha').json()['setup'] == result['setup']


def test_map_change_keeps_matching_overrides_ownership_and_units(db):
    state = add_new_map(db)
    state['library']['maps']['new_world']['shire'] = deepcopy(state['library']['maps']['world']['shire'])
    state['library']['maps']['new_world']['shire']['adjacent'] = ['bree']
    state['library']['maps']['new_world']['bree']['adjacent'] = ['shire']
    setup = deepcopy(state['setups']['alpha'])
    setup['territories']['shire']['overrides'] = {'display_name': 'Home'}
    setup['camps'] = {'keep': {'id': 'keep', 'territory_id': 'shire'}, 'gone': {'id': 'gone', 'territory_id': 'removed'}}
    result = with_map(setup, state['library'], 'new_world')
    assert result['territories']['shire']['overrides'] == {'display_name': 'Home'}
    assert result['starting_setup'] == setup['starting_setup']
    assert result['camps'] == {'keep': setup['camps']['keep']}
    assert compile_setup(result, state['library'])['territories']['shire']['adjacent'] == ['bree']
    assert result['territories']['bree'] == {'seed': 'bree'}


def test_api_canonical_get_save_export_and_no_source_editor_endpoint(client, db):
    response = client.get('/admin/setups/alpha')
    assert response.status_code == 200
    setup = response.json()['setup']
    setup['units']['guard']['overrides'] = {'attack': 8}
    assert client.put('/admin/setups/alpha', json=setup).status_code == 200
    exported = client.get('/admin/scenarios/export').json()
    assert 'sources' not in exported
    assert exported['setups']['alpha']['units']['guard']['overrides']['attack'] == 8
    assert client.get('/admin/setups/alpha/source').status_code == 404
    assert client.post('/admin/setups/validate', json=setup).json()['valid']
    assert client.post('/admin/setups', json={'id': 'imported', 'setup': json.dumps(setup)}).status_code == 200
    assert client.get('/admin/setups/imported').json()['setup']['manifest']['is_active'] is False


def test_graph_edits_change_generated_snapshots_without_duplicating_graph_in_setup_json(db):
    from backend.map_overrides import save_map_overrides
    state = add_new_map(db)
    state['library']['maps']['world']['bree'] = deepcopy(state['library']['maps']['new_world']['bree'])
    for setup in state['setups'].values():
        setup['territories']['bree'] = {'seed': 'bree'}
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    save_map_overrides(db, 'world', {'graph': {'adjacent': {'shire': ['bree'], 'bree': ['shire']}}})
    for key in ('alpha', 'beta', 'gamma'):
        assert json.loads(db.get(Setup, key).territories_json)['shire']['adjacent'] == ['bree']
    setup = load_state(db)['setups']['alpha']
    assert setup['territories']['shire'] == {'seed': 'shire'}


def test_cli_json_export_round_trip(tmp_path, client, db):
    snapshot = client.get('/admin/scenarios/export').json()
    source = tmp_path / 'snapshot.json'
    source.write_text(json.dumps(snapshot))
    root = tmp_path / 'export'
    assert main(['unpack', str(source), '--output-root', str(root)]) == 0
    for key, setup in snapshot['setups'].items():
        folder = root / 'setups' / key
        assert [path.name for path in folder.iterdir()] == ['setup.json']
        assert read_setup_bundle(folder) == compile_setup(setup, snapshot['library'])
        assert main(['--data-root', str(root), 'build', str(folder / 'setup.json'), '--check']) == 0


def test_files_mode_saves_the_canonical_json_and_reads_it_immediately(tmp_path, monkeypatch):
    from backend.scenario_builder import write_library
    from backend.engine import definitions
    from backend import setup_data
    library = extract_library({'alpha': example()})
    write_library(tmp_path, library)
    (tmp_path / 'catalog.json').write_text((DATA_DIR / 'catalog.json').read_text())
    write_setup(tmp_path / 'setups/alpha', setup_from_bundle(example(), library))
    monkeypatch.setenv('SETUP_SOURCE', 'files')
    monkeypatch.setattr(definitions, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(definitions, 'SETUPS_DIR', tmp_path / 'setups')
    monkeypatch.setattr(setup_data, 'SETUPS_DIR', tmp_path / 'setups')
    engine = create_engine('sqlite://', poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        setup = load_state(session)['setups']['alpha']
        setup['units']['guard']['overrides'] = {'attack': 9}
        save_definition(session, 'alpha', setup)
    assert read_setup_bundle(tmp_path / 'setups/alpha')['units']['guard']['attack'] == 9
    assert sorted(p.name for p in (tmp_path / 'setups/alpha').iterdir()) == ['setup.json']


def test_empty_database_seeds_exact_canonical_definitions_and_shared_defaults(db, monkeypatch):
    from backend.setup_data import seed_setups_if_empty
    for row in db.query(Setup).all():
        db.delete(row)
    db.commit()
    assert seed_setups_if_empty(db) == 6
    state = load_state(db)
    for path in SETUPS_DIR.glob('*/setup.json'):
        assert state['setups'][path.parent.name] == canonical_setup(json.loads(path.read_text()))


@pytest.mark.parametrize('setup_id', ['motw_1.0', 'wotr_1.1', 'wotr_exp_1.1'])
def test_csv_import_updates_only_the_canonical_setup(tmp_path, monkeypatch, setup_id):
    import importlib.util
    from backend.scenario_builder import write_library
    spec = importlib.util.spec_from_file_location('csv_import', SETUPS_DIR / setup_id / 'csv_to_json.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    library = extract_library({'alpha': example()})
    write_library(tmp_path, library)
    folder = tmp_path / 'setups/alpha'
    write_setup(folder, setup_from_bundle(example(), library))
    monkeypatch.setattr(module, 'SETUP_DIR', folder)
    monkeypatch.setattr(module, '_require_units_csv', lambda: tmp_path / 'units.csv')
    units = deepcopy(example()['units']['guard'])
    units['attack'] = 9
    monkeypatch.setattr(module, 'load_units_csv', lambda _: [units])
    monkeypatch.setattr(module, '_first_existing_csv', lambda *_: None)
    module.main()
    assert read_setup_bundle(folder)['units']['guard']['attack'] == 9
    assert sorted(path.name for path in folder.iterdir()) == ['setup.json']


def test_canonical_files_do_not_introduce_new_validation_errors():
    from backend.setup_validation import validate_setup_payload
    baseline = json.loads((Path(__file__).parent / 'fixtures/scenario_validation_baseline.json').read_text())
    catalog = json.loads((DATA_DIR / 'catalog.json').read_text())
    for path in SETUPS_DIR.glob('*/setup.json'):
        errors = validate_setup_payload(read_setup_bundle(path.parent), catalog)
        assert not (set(errors) - set(baseline[path.parent.name]))


def test_copy_keeps_seed_aliases_and_explicit_equal_overrides(client, db):
    setup = client.get('/admin/setups/alpha').json()['setup']
    setup['units']['guard']['overrides'] = {'attack': 5}
    setup['units']['ally'] = {'seed': 'guard', 'overrides': {'attack': 7}}
    assert client.put('/admin/setups/alpha', json=setup).status_code == 200
    copied = client.post('/admin/setups', json={'id': 'copy', 'duplicate_from': 'alpha'})
    assert copied.status_code == 200, copied.text
    result = client.get('/admin/setups/copy').json()['setup']
    assert result['units']['guard']['overrides']['attack'] == 5
    assert result['units']['ally']['seed'] == 'guard'
    assert result['manifest']['is_active'] is False
    assert client.delete('/admin/setups/copy').status_code == 200
    assert client.get('/admin/setups/copy').status_code == 404


def test_territory_seed_create_update_and_duplicate_protection(client, db):
    record = {'id': 'bree', 'display_name': 'Bree', 'terrain_type': 'hills', 'produces': {'power': 3}, 'ownable': True}
    created = client.post('/admin/territory-seeds', json=record)
    assert created.status_code == 200, created.text
    assert created.json()['seed'] == record
    assert client.post('/admin/territory-seeds', json={**record, 'display_name': 'Overwrite'}).status_code == 400
    assert load_state(db)['library']['territories']['bree']['display_name'] == 'Bree'
    assert client.put('/admin/territory-seeds/bree', json={**record, 'id': 'renamed'}).status_code == 400
    assert client.post('/admin/territory-seeds', json={**record, 'id': 'invalid', 'terrain_type': 'nonexistent'}).status_code == 400
    updated = client.put('/admin/territory-seeds/bree', json={**record, 'display_name': 'Shared Bree', 'terrain_type': 'forest'})
    assert updated.status_code == 200, updated.text
    assert load_state(db)['library']['territories']['bree']['terrain_type'] == 'forest'


def test_shared_seed_terrain_updates_all_maps_and_respects_setup_overrides(client, db):
    from backend.setup_data import try_load_static_definitions
    setup = client.get('/admin/setups/beta').json()['setup']
    setup['territories']['shire']['overrides'] = {'terrain_type': 'forest'}
    assert client.put('/admin/setups/beta', json=setup).status_code == 200
    state = load_state(db)
    state['library']['maps']['other_map'] = deepcopy(state['library']['maps']['world'])
    state['setups']['gamma']['manifest']['map_asset'] = 'other_map'
    db.get(Setup, 'gamma').manifest_json = json.dumps(state['setups']['gamma']['manifest'])
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    seed = deepcopy(load_state(db)['library']['territories']['shire'])
    response = client.put('/admin/territory-seeds/shire', json={**seed, 'terrain_type': 'hills'})
    assert response.status_code == 200, response.text
    for key, terrain in [('alpha', 'hills'), ('beta', 'forest'), ('gamma', 'hills')]:
        assert client.get(f'/admin/setups/{key}').json()['territories']['shire']['terrain_type'] == terrain
        assert json.loads(db.get(Setup, key).territories_json)['shire']['terrain_type'] == terrain
        assert try_load_static_definitions(key, db)[1]['shire'].terrain_type == terrain
    assert load_state(db)['library']['maps']['world']['shire']['terrain_type'] == 'hills'


def test_legacy_map_terrain_migrates_once_and_keeps_existing_scenario_terrain(db):
    initialize(db)
    state = load_state(db)
    state['library']['territories']['shire'].pop('terrain_type')
    state['library']['maps']['other'] = {'shire': {**state['library']['maps']['world']['shire'], 'terrain_type': 'hills'}}
    state['setups']['beta']['manifest']['map_asset'] = 'other'
    row = db.get(Setup, 'beta')
    row.manifest_json = json.dumps(state['setups']['beta']['manifest'])
    territories = json.loads(row.territories_json)
    territories['shire']['terrain_type'] = 'hills'
    row.territories_json = json.dumps(territories)
    db.get(AppSetting, SETTING).value_json = json.dumps(state)
    db.commit()
    initialize(db)
    migrated = load_state(db)
    assert migrated['library']['territories']['shire']['terrain_type'] in {'plains', 'hills'}
    for key, terrain in [('alpha', 'plains'), ('beta', 'hills'), ('gamma', 'plains')]:
        assert compile_setup(migrated['setups'][key], migrated['library'])['territories']['shire']['terrain_type'] == terrain
    before = deepcopy(migrated)
    initialize(db)
    assert load_state(db) == before


def _seed_drawing(tid):
    return {'width': 1000, 'height': 800,
        'joints': {'a': {'x': 0, 'y': 0}, 'b': {'x': 1000, 'y': 0}, 'c': {'x': 1000, 'y': 800}, 'd': {'x': 0, 'y': 800}},
        'borders': {'ab': {'a': 'a', 'b': 'b'}, 'bc': {'a': 'b', 'b': 'c'}, 'cd': {'a': 'c', 'b': 'd'}, 'da': {'a': 'd', 'b': 'a'}},
        'labels': {tid: {'display_name': 'Ignored local name', 'x': 500, 'y': 400}}}


def test_saved_map_references_seed_ids_and_anchors_without_separate_names(client, db):
    response = client.put('/admin/maps/builder/seed_map', json={'display_name': 'Map', 'doc': _seed_drawing('shire')})
    assert response.status_code == 200, response.text
    assert response.json()['doc']['labels']['shire'] == {'x': 500, 'y': 400}
    loaded = client.get('/admin/maps/builder/seed_map').json()
    assert loaded['doc']['labels']['shire'] == {'x': 500, 'y': 400}
    assert load_state(db)['library']['maps']['seed_map']['shire']['display_name'] == 'The Shire'
    seed = load_state(db)['library']['territories']['shire']
    assert client.put('/admin/territory-seeds/shire', json={**seed, 'display_name': 'Renamed seed'}).status_code == 200
    assert load_state(db)['library']['maps']['seed_map']['shire']['display_name'] == 'Renamed seed'
    assert 'display_name' not in client.get('/admin/maps/builder/seed_map').json()['doc']['labels']['shire']


def test_map_builder_requires_known_seed_assignments_without_silently_creating_them(client, db):
    response = client.put('/admin/maps/builder/new_map', json={'doc': _seed_drawing('unknown_territory')})
    assert response.status_code == 400
    assert 'Assign existing territory seeds' in response.text
    assert 'unknown_territory' not in load_state(db)['library']['territories']


def test_terrain_seed_endpoints_require_admin(db):
    app.dependency_overrides[get_db] = lambda: db
    try:
        unauthenticated = TestClient(app)
        assert unauthenticated.post('/admin/territory-seeds', json={}).status_code in (401, 403)
        assert unauthenticated.put('/admin/territory-seeds/shire', json={}).status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_region_seed_reassignment_updates_setup_membership_and_removes_old_placements(client, db):
    assert client.put('/admin/maps/builder/world', json={'doc': _seed_drawing('shire')}).status_code == 200
    seed = {'id': 'bree', 'display_name': 'Bree', 'terrain_type': 'hills', 'produces': {'power': 2}, 'ownable': True}
    assert client.post('/admin/territory-seeds', json=seed).status_code == 200
    response = client.put('/admin/maps/builder/world', json={'doc': _seed_drawing('bree')})
    assert response.status_code == 200, response.text
    for key in ('alpha', 'beta', 'gamma'):
        state = client.get(f'/admin/setups/{key}').json()
        assert set(state['setup']['territories']) == {'bree'}
        assert state['starting_setup']['territory_owners'] == {}
        assert state['starting_setup']['starting_units'] == {}
        assert state['factions']['good']['capital'] is None
        assert set(json.loads(db.get(Setup, key).territories_json)) == {'bree'}
        assert json.loads(db.get(Setup, key).starting_setup_json)['starting_units'] == {}


def test_legacy_saved_drawing_without_a_setup_acquires_matching_seeds_once(db):
    from backend.api.models import MapAsset
    doc = _seed_drawing('legacy_region')
    doc['labels']['legacy_region']['display_name'] = 'Legacy Region Name'
    db.add(MapAsset(map_base='unused_legacy_map', display_name='Legacy map', builder_json=json.dumps(doc), svg_text='<svg/>'))
    db.commit()
    initialize(db)
    state = load_state(db)
    seed = state['library']['territories']['legacy_region']
    assert seed['display_name'] == 'Legacy Region Name'
    assert seed['terrain_type'] == 'plains'
    assert set(state['library']['maps']['unused_legacy_map']) == {'legacy_region'}
    assert not any(setup['manifest'].get('map_asset') == 'unused_legacy_map' for setup in state['setups'].values())
    assert load_state(db) == state
