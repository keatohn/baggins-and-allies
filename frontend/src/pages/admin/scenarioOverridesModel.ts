import type { AdminSetupBundle, ScenarioLibrary, SetupDefinition } from '../../services/api';
export type Dict = Record<string, unknown>;
export const object = (value: unknown): Dict => value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {};
export const deleted = (value: unknown) => JSON.stringify(value) === '{"$delete":true}';
const geometry = new Set(['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent']);
const mapId = (setup: SetupDefinition) => String(setup.manifest.map_asset || setup.manifest.id);
export function resolve(base: Dict, overrides: Dict): Dict {
  const out = structuredClone(base);
  for (const [key, value] of Object.entries(overrides)) {
    if (deleted(value)) delete out[key];
    else out[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? resolve(object(base[key]), object(value)) : value;
  }
  return out;
}

function reconcile(defaults: Dict, overrides: Dict, value: Dict): Dict {
  const previous = resolve(defaults, overrides);
  const next: Dict = {};
  for (const key of new Set([...Object.keys(defaults), ...Object.keys(value), ...Object.keys(overrides)])) {
    if (!Object.hasOwn(value, key)) {
      if (Object.hasOwn(defaults, key)) next[key] = { $delete: true };
    } else if (Object.hasOwn(overrides, key) && JSON.stringify(previous[key]) === JSON.stringify(value[key])) next[key] = overrides[key];
    else if (JSON.stringify(defaults[key]) !== JSON.stringify(value[key])) next[key] = value[key] && typeof value[key] === 'object' && !Array.isArray(value[key])
      ? reconcile(object(defaults[key]), object(overrides[key]), object(value[key])) : value[key];
  }
  return next;
}


export function resolveSetup(setup: SetupDefinition, library: ScenarioLibrary): AdminSetupBundle {
  const out: AdminSetupBundle = { id: String(setup.manifest.id), ...setup, setup };
  for (const section of ['units', 'factions', 'territories'] as const) {
    if (!setup[section] || Array.isArray(setup[section]) || typeof setup[section] !== 'object') throw new Error(`${section} must be an object of seed references and optional overrides.`);
    out[section] = Object.fromEntries(Object.entries(setup[section]).map(([id, entry]) => {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry) || Object.keys(entry).some(key => !['seed', 'overrides'].includes(key))) throw new Error(`${section}.${id}: use a seed reference and optional overrides.`);
      const base = section === 'territories' ? library.maps[mapId(setup)]?.[id] : library[section][entry.seed ?? ''];
      if (!base && (entry.seed || section === 'territories')) throw new Error(`Unknown ${section} seed or map region: ${id}`);
      const fields = section === 'territories' ? Object.fromEntries(Object.entries(entry.overrides ?? {}).filter(([key]) => !geometry.has(key))) : entry.overrides ?? {};
      const record = resolve(base ?? {}, fields);
      if ('id' in record && !Object.hasOwn(fields, 'id')) record.id = id;
      return [id, record];
    }));
  }
  return out;
}

/** Adapt legacy placement/map form controls into the same canonical JSON state. */
export function setupForRecords(bundle: AdminSetupBundle, library: ScenarioLibrary): SetupDefinition {
  const setup: SetupDefinition = { manifest: bundle.manifest, camps: bundle.camps, ports: bundle.ports,
    starting_setup: bundle.starting_setup, units: {}, factions: {}, territories: {} };
  for (const section of ['units', 'factions', 'territories'] as const) {
    setup[section] = Object.fromEntries(Object.entries(bundle[section]).map(([id, raw]) => {
      const entry = bundle.setup?.[section][id] ?? { ...(section === 'territories' || id in library[section] ? { seed: id } : {}) };
      const base = { ...(section === 'territories' ? library.maps[mapId(setup)]?.[id] : library[section][entry.seed ?? '']) };
      if ('id' in base) base.id = id;
      let overrides = reconcile(base, entry.overrides ?? {}, object(raw));
      if (section === 'territories') overrides = Object.fromEntries(Object.entries(overrides).filter(([key]) => !geometry.has(key)));
      return [id, { ...(entry.seed ? { seed: entry.seed } : {}), ...(Object.keys(overrides).length ? { overrides } : {}) }];
    }));
  }
  return setup;
}

export function withScenarioMap(bundle: AdminSetupBundle, library: ScenarioLibrary, map: string): AdminSetupBundle {
  if (!Object.hasOwn(library.maps, map)) throw new Error(`No territory data available for map ${map}. Save the map in Maps first.`);
  const setup = setupForRecords(bundle, library);
  setup.manifest = { ...setup.manifest, map_asset: map };
  const ids = new Set(Object.keys(library.maps[map]));
  setup.territories = Object.fromEntries([...ids].map(id => [id, setup.territories[id] ?? { seed: id }]));
  for (const field of ['territory_owners', 'starting_units']) setup.starting_setup = { ...setup.starting_setup,
    [field]: Object.fromEntries(Object.entries(object(setup.starting_setup[field])).filter(([id]) => ids.has(id))) };
  for (const section of ['camps', 'ports'] as const) setup[section] = Object.fromEntries(Object.entries(setup[section]).filter(([, value]) => ids.has(String(object(value).territory_id))));
  for (const [id, entry] of Object.entries(setup.factions)) {
    const capital = object(bundle.factions[id]).capital;
    if (typeof capital === 'string' && capital && !ids.has(capital)) setup.factions[id] = { ...entry, overrides: { ...entry.overrides, capital: null } };
  }
  return resolveSetup(setup, library);
}

export type ScenarioSection = 'units' | 'factions' | 'territories';
export type SelectionOption = { id: string; seed?: string; name: string };
export function scenarioSelectionOptions(setup: SetupDefinition, library: ScenarioLibrary, section: ScenarioSection): SelectionOption[] {
  const defaults = section === 'territories' ? library.maps[mapId(setup)] ?? {} : library[section];
  const options = Object.entries(setup[section]).map(([id, entry]) => ({ id, seed: entry.seed,
    name: String(resolve(defaults[entry.seed ?? id] ?? {}, entry.overrides ?? {}).display_name ?? id) }));
  for (const [id, seed] of Object.entries(defaults)) {
    if (!Object.hasOwn(setup[section], id) && !options.some(option => option.seed === id)) options.push({ id, seed: id, name: String(seed.display_name ?? id) });
  }
  return options.sort((a, b) => a.id.localeCompare(b.id));
}
export function withScenarioSelection(setup: SetupDefinition, section: ScenarioSection, ids: string[]): SetupDefinition {
  return { ...setup, [section]: Object.fromEntries([...new Set(ids)].map(id => [id, setup[section][id] ?? { seed: id }])) };
}
