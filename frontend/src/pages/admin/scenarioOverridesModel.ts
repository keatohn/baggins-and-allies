import type { AdminSetupBundle, ScenarioLibrary, ScenarioSource } from '../../services/api';

export type Dict = Record<string, unknown>;
export const object = (value: unknown): Dict => value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {};
export const deleted = (value: unknown) => JSON.stringify(value) === '{"$delete":true}';

export function resolve(base: Dict, overrides: Dict): Dict {
  const out = { ...base };
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

/** Keep raw JSON / map / existing structured form edits consistent with source metadata. */
export function sourceForRecords(bundle: AdminSetupBundle, library: ScenarioLibrary): ScenarioSource | undefined {
  if (!bundle.authoring_source) return undefined;
  const source = { ...bundle.authoring_source };
  for (const section of ['units', 'factions'] as const) {
    source[section] = Object.fromEntries(Object.entries(bundle[section]).map(([id, raw]) => {
      const entry = source[section][id] ?? { ...(id in library[section] ? { seed: id } : {}), overrides: {} };
      const base = { ...library[section][entry.seed ?? ''] };
      if ('id' in base && entry.seed !== id) base.id = id;
      return [id, { ...entry, overrides: reconcile(base, entry.overrides ?? {}, object(raw)) }];
    }));
  }
  const territoryIds = Object.keys(bundle.territories);
  const mapIds = Object.keys(library.maps[source.map] ?? {});
  if (source.territory_ids || territoryIds.length !== mapIds.length || territoryIds.some((id) => !mapIds.includes(id))) source.territory_ids = territoryIds;
  source.territory_overrides = Object.fromEntries(Object.entries(bundle.territories).map(([id, raw]) => [id,
    reconcile(library.maps[source.map]?.[id] ?? {}, source.territory_overrides?.[id] ?? {}, object(raw)),
  ]));
  return source;
}

