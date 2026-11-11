// Node 24's built-in type stripping runs the pure form model without a new framework.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolve, sourceForRecords, scenarioSelectionOptions, withScenarioSelection } from '../frontend/src/pages/admin/scenarioOverridesModel.ts';

test('the form resolves recursive overrides, list replacement, null and removal', () => {
  const defaults = { cost: { power: 3, other: 1 }, specials: ['heal'], home: ['shire'], optional: 2 };
  const overrides = { cost: { power: 5 }, specials: [], home: null, optional: { $delete: true } };
  assert.deepEqual(resolve(defaults, overrides), { cost: { power: 5, other: 1 }, specials: [], home: null });
  assert.deepEqual(defaults.specials, ['heal']);
});

test('raw JSON edits preserve explicit equal overrides and map inheritance', () => {
  const library = { units: { guard: { id: 'guard', attack: 5, cost: { power: 3 } } }, factions: {}, maps: { world: { shire: { id: 'shire', produces: { power: 2 } } } } };
  const source = { map: 'world', manifest: { id: 'alpha' }, units: { guard: { seed: 'guard', overrides: { attack: 5 } } }, factions: {} };
  const bundle = { authoring_source: source, units: { guard: { id: 'guard', attack: 5, cost: { power: 8 } } }, factions: {}, territories: { shire: { id: 'shire', produces: { power: 2 } } } };
  const edited = sourceForRecords(bundle, library);
  assert.deepEqual(edited.units.guard.overrides, { attack: 5, cost: { power: 8 } });
  assert.equal(edited.territory_ids, undefined);
  assert.deepEqual(source.units.guard.overrides, { attack: 5 });
  delete edited.units.guard.overrides.attack;
  assert.equal(resolve(library.units.guard, edited.units.guard.overrides).attack, 5);
  assert.equal(resolve({ ...library.units.guard, attack: 7 }, edited.units.guard.overrides).attack, 7);
});

test('seed aliases, custom records and deleted optional fields stay consistent', () => {
  const library = { units: { guard: { id: 'guard', attack: 5, home: ['shire'] } }, factions: {}, maps: { world: {} } };
  const source = { map: 'world', manifest: {}, units: { ally: { seed: 'guard', overrides: {} } }, factions: {} };
  const bundle = { authoring_source: source, units: { ally: { id: 'ally', attack: 5 }, custom: { id: 'custom', attack: 9 } }, factions: {}, territories: {} };
  const edited = sourceForRecords(bundle, library);
  assert.deepEqual(edited.units.ally.overrides, { home: { $delete: true } });
  assert.deepEqual(edited.units.custom, { overrides: { id: 'custom', attack: 9 } });
});

test('bulk seed selection preserves aliases, custom records and retained overrides', () => {
  const library = { units: { guard: { display_name: 'Guard' }, archer: { display_name: 'Archer' } }, factions: {}, maps: {} };
  const source = { map: 'world', manifest: {}, units: { ally: { seed: 'guard', overrides: { attack: 9 } }, custom: { overrides: { display_name: 'Custom' } } }, factions: {} };
  const options = scenarioSelectionOptions(source, library, 'units');
  assert.deepEqual(options.map(o => o.id), ['ally', 'archer', 'custom']);
  assert.equal(options.find(o => o.id === 'ally').seed, 'guard');
  const next = withScenarioSelection(source, 'units', ['ally', 'archer', 'custom']);
  assert.deepEqual(next.units.ally, source.units.ally);
  assert.deepEqual(next.units.custom, source.units.custom);
  assert.deepEqual(next.units.archer, { seed: 'archer', overrides: {} });
  assert.deepEqual(withScenarioSelection(next, 'units', []).units, {});
  assert.equal(Object.keys(source.units).length, 2);
});

test('bulk faction selection retains overrides and adds inherited seeds', () => {
  const source = { map: 'world', manifest: {}, units: {}, factions: { good: { seed: 'good', overrides: { color: '#123456' } } } };
  const next = withScenarioSelection(source, 'factions', ['good', 'evil']);
  assert.deepEqual(next.factions.good, source.factions.good);
  assert.deepEqual(next.factions.evil, { seed: 'evil', overrides: {} });
});

test('territory choices follow the selected map and remove only unchecked overrides', () => {
  const library = { units: {}, factions: {}, territories: { elsewhere: {} }, maps: { world: { shire: {}, bree: {} }, other: { elsewhere: {} } } };
  const source = { map: 'world', manifest: {}, units: {}, factions: {}, territory_overrides: { shire: { produces: { power: 7 } }, bree: { ownable: false } } };
  assert.deepEqual(scenarioSelectionOptions(source, library, 'territories').map(o => o.id), ['bree', 'shire']);
  const next = withScenarioSelection(source, 'territories', ['shire']);
  assert.deepEqual(next.territory_ids, ['shire']);
  assert.deepEqual(next.territory_overrides, { shire: { produces: { power: 7 } } });
  assert.deepEqual(withScenarioSelection(next, 'territories', []).territory_ids, []);
  assert.ok(source.territory_overrides.bree);
});
