// Node's built-in type stripping runs the pure form model without a new framework.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolve, resolveSetup, setupForRecords, scenarioSelectionOptions, withScenarioSelection, withScenarioMap } from '../frontend/src/pages/admin/scenarioOverridesModel.ts';
const library = {
  units: { guard: { id: 'guard', attack: 5, cost: { power: 3 }, specials: ['heal'] }, archer: { id: 'archer', attack: 2 } },
  factions: { good: { id: 'good', capital: 'shire' }, evil: { id: 'evil' } },
  territories: {},
  maps: { world: { shire: { id: 'shire', display_name: 'The Shire', adjacent: [] }, removed: { id: 'removed', adjacent: [] } },
    new: { shire: { id: 'shire', adjacent: ['bree'] }, bree: { id: 'bree', adjacent: ['shire'] } } },
};
const example = () => ({
  manifest: { id: 'alpha', map_asset: 'world' },
  units: { guard: { seed: 'guard', overrides: { attack: 5 } } }, factions: { good: { seed: 'good' } },
  territories: { shire: { seed: 'shire', overrides: { display_name: 'Home' } }, removed: { seed: 'removed' } },
  camps: { gone: { territory_id: 'removed' }, retained: { territory_id: 'shire' } }, ports: {},
  starting_setup: { turn_order: ['good'], territory_owners: { shire: 'good', removed: 'good' }, starting_units: { shire: [{ unit_id: 'guard', count: 1 }], removed: [] } },
});

test('recursive overrides retain list replacement, null and removal semantics', () => {
  const defaults = { cost: { power: 3, other: 1 }, specials: ['heal'], home: ['shire'], optional: 2 };
  assert.deepEqual(resolve(defaults, { cost: { power: 5 }, specials: [], home: null, optional: { $delete: true } }), { cost: { power: 5, other: 1 }, specials: [], home: null });
  assert.deepEqual(defaults.specials, ['heal']);
});

test('controls edit one compact setup and preserve pinned equal overrides', () => {
  const setup = example();
  const bundle = resolveSetup(setup, library);
  bundle.units.guard.cost.power = 8;
  const compact = setupForRecords(bundle, library);
  assert.deepEqual(compact.units.guard.overrides, { attack: 5, cost: { power: 8 } });
  assert.deepEqual(compact.territories.shire, { seed: 'shire', overrides: { display_name: 'Home' } });
  assert.equal(resolveSetup(compact, { ...library, units: { ...library.units, guard: { ...library.units.guard, attack: 9 } } }).units.guard.attack, 5);
});

test('seed selection preserves aliases, custom records and retained overrides', () => {
  const setup = example();
  setup.units = { ally: { seed: 'guard', overrides: { attack: 9 } }, custom: { overrides: { display_name: 'Custom' } } };
  assert.deepEqual(scenarioSelectionOptions(setup, library, 'units').map(o => o.id), ['ally', 'archer', 'custom']);
  const next = withScenarioSelection(setup, 'units', ['ally', 'archer', 'custom']);
  assert.deepEqual(next.units.ally, setup.units.ally);
  assert.deepEqual(next.units.custom, setup.units.custom);
  assert.deepEqual(next.units.archer, { seed: 'archer' });
  assert.deepEqual(withScenarioSelection(next, 'units', []).units, {});
  assert.equal(resolveSetup(next, library).units.ally.id, 'ally');
});

test('territory choices include unselected map seeds and preserve selected metadata', () => {
  const setup = example();
  setup.manifest.map_asset = 'new';
  setup.territories = { shire: setup.territories.shire };
  assert.deepEqual(scenarioSelectionOptions(setup, library, 'territories').map(o => o.id), ['bree', 'shire']);
  const next = withScenarioSelection(setup, 'territories', ['shire', 'bree']);
  assert.deepEqual(next.territories.bree, { seed: 'bree' });
  assert.deepEqual(next.territories.shire, setup.territories.shire);
});

test('map changes replace geography and retain only matching placements and metadata', () => {
  const setup = example();
  setup.factions.good.overrides = { capital: 'removed' };
  const bundle = resolveSetup(setup, library);
  const before = structuredClone(bundle);
  const next = withScenarioMap(bundle, library, 'new');
  assert.deepEqual(Object.keys(next.territories), ['shire', 'bree']);
  assert.deepEqual(next.territories.shire.adjacent, ['bree']);
  assert.equal(next.territories.shire.display_name, 'Home');
  assert.deepEqual(next.starting_setup.territory_owners, { shire: 'good' });
  assert.deepEqual(next.starting_setup.starting_units, { shire: [{ unit_id: 'guard', count: 1 }] });
  assert.equal(next.factions.good.capital, null);
  assert.deepEqual(next.camps, { retained: { territory_id: 'shire' } });
  assert.equal(next.setup.manifest.map_asset, 'new');
  assert.deepEqual(Object.keys(next.setup.territories), ['shire', 'bree']);
  assert.equal(next.setup.territories.shire.overrides.adjacent, undefined);
  assert.deepEqual(bundle, before);
  assert.throws(() => withScenarioMap(bundle, library, 'missing'), /No territory data/);
});

test('map graph changes are inherited without changing the compact setup', () => {
  const setup = example();
  const before = structuredClone(setup);
  const updated = structuredClone(library);
  updated.maps.world.shire.adjacent = ['removed'];
  assert.deepEqual(resolveSetup(setup, updated).territories.shire.adjacent, ['removed']);
  assert.deepEqual(setup, before);
});
