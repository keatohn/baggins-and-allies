import { useState } from 'react';
import type { EntityRecords, ScenarioLibrary, ScenarioSource } from '../../services/api';
import { deleted, object, resolve, type Dict } from './scenarioOverridesModel';

function ValueInput({ value, onChange, label }: { value: unknown; onChange: (v: unknown) => void; label: string }) {
  const [raw, setRaw] = useState(JSON.stringify(value ?? null));
  const [error, setError] = useState('');
  if (typeof value === 'number') return <input aria-label={label} type="number" value={value} onChange={(e) => { if (e.target.value !== '') onChange(Number(e.target.value)); }} />;
  if (typeof value === 'boolean') return <input aria-label={label} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />;
  if (typeof value === 'string') return <input aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} />;
  return <div><textarea aria-label={label} value={raw} rows={2} onChange={(e) => {
    setRaw(e.target.value);
    try { onChange(JSON.parse(e.target.value)); setError(''); e.target.setCustomValidity(''); } catch { setError('Enter valid JSON.'); e.target.setCustomValidity('Enter valid JSON.'); }
  }} />{error && <span role="alert">{error}</span>}</div>;
}

function Fields({ defaults, overrides, onChange, prefix = '' }: { defaults: Dict; overrides: Dict; onChange: (next: Dict) => void; prefix?: string }) {
  const [field, setField] = useState('');
  const resolved = resolve(defaults, overrides);
  return <>{[...new Set([...Object.keys(defaults), ...Object.keys(overrides)])].filter((key) => key !== 'id').sort().map((key) => {
    const base = defaults[key];
    const override = Object.hasOwn(overrides, key);
    const current = resolved[key];
    const label = `${prefix}${key}`;
    const nested = base && typeof base === 'object' && !Array.isArray(base) && (!override || (overrides[key] && typeof overrides[key] === 'object' && !Array.isArray(overrides[key]) && !deleted(overrides[key])));
    if (nested) return <fieldset key={key} className="scenario-fields"><legend>{key.replaceAll('_', ' ')}</legend><Fields defaults={object(base)} overrides={object(overrides[key])} prefix={`${label}.`} onChange={(next) => {
      const result = { ...overrides };
      if (Object.keys(next).length) result[key] = next; else delete result[key];
      onChange(result);
    }} /></fieldset>;
    return <div className="scenario-field" key={key}>
      <label className="scenario-field__label"><input type="checkbox" aria-label={`Override ${label}`} checked={override} onChange={(e) => {
        const next = { ...overrides };
        if (e.target.checked) next[key] = current ?? ''; else delete next[key];
        onChange(next);
      }} /> {key.replaceAll('_', ' ')}</label>
      <span className={`scenario-field__default${override ? ' scenario-field__default--overridden' : ''}`} title="Inherited default">{base === undefined ? 'No default' : JSON.stringify(base)}</span>
      {override && <ValueInput key={`${label}:${deleted(overrides[key]) ? 'removed' : typeof current}`} label={label} value={current} onChange={(v) => onChange({ ...overrides, [key]: v })} />}
      {override && <button type="button" className="admin-page__tab" title="Remove this optional field from the compiled record" onClick={() => onChange({ ...overrides, [key]: { $delete: true } })}>Remove field</button>}
    </div>;
  })}<div className="scenario-toolbar"><input aria-label={`New ${prefix}field`} placeholder="Additional field" value={field} onChange={(e) => setField(e.target.value)} /><button type="button" disabled={!/^[a-z][a-z0-9_]*$/.test(field) || field === 'id' || field in resolved} onClick={() => { onChange({ ...overrides, [field]: null }); setField(''); }}>Add override field</button></div></>;
}

/** Defaults remain visible. Checked fields alone are stored as scenario overrides. */
export function ScenarioOverrides({ section, source, library, onChange }: {
  section: 'units' | 'factions' | 'territories'; source: ScenarioSource; library: ScenarioLibrary;
  onChange: (records: EntityRecords, source: ScenarioSource) => void;
}) {
  const [selected, setSelected] = useState('');
  const [newId, setNewId] = useState('');
  const [seedToAdd, setSeedToAdd] = useState('');
  const territory = section === 'territories';
  const defaults = territory ? library.maps[source.map] ?? {} : library[section];
  const ids = territory ? source.territory_ids ?? Object.keys(defaults) : Object.keys(source[section]);
  const id = ids.includes(selected) ? selected : ids[0] ?? '';
  const entry = territory ? undefined : source[section][id];
  const seed = territory ? id : entry?.seed;
  const base = defaults[seed ?? ''] ?? {};
  const overrides = territory ? source.territory_overrides?.[id] ?? {} : entry?.overrides ?? {};

  const emit = (next: ScenarioSource) => {
    const records: EntityRecords = {};
    if (territory) {
      for (const tid of next.territory_ids ?? Object.keys(defaults)) records[tid] = resolve(defaults[tid] ?? {}, next.territory_overrides?.[tid] ?? {});
    } else {
      for (const [key, item] of Object.entries(next[section])) {
        records[key] = resolve(defaults[item.seed ?? ''] ?? {}, item.overrides ?? {});
        if ('id' in records[key] && !Object.hasOwn(item.overrides ?? {}, 'id')) records[key].id = key;
      }
    }
    onChange(records, next);
  };
  const changeOverrides = (next: Dict) => emit(territory
    ? { ...source, territory_overrides: { ...source.territory_overrides, [id]: next } }
    : { ...source, [section]: { ...source[section], [id]: { ...entry, overrides: next } } });
  return <div className="scenario-overrides">
    <p>Select fields to override. Unchecked fields inherit their shared default. Map terrain and connections are edited in Maps.</p>
    <div className="scenario-toolbar">
      <label>Record <select aria-label={`${section} record`} value={id} onChange={(e) => setSelected(e.target.value)}><option value="" disabled>Select a record</option>{ids.map((key) => <option key={key}>{key}</option>)}</select></label>
      <button type="button" disabled={!id} onClick={() => {
        if (territory) emit({ ...source, territory_ids: ids.filter((key) => key !== id), territory_overrides: Object.fromEntries(Object.entries(source.territory_overrides ?? {}).filter(([key]) => key !== id)) });
        else emit({ ...source, [section]: Object.fromEntries(Object.entries(source[section]).filter(([key]) => key !== id)) });
      }}>Remove record</button>
    </div>
    {id && <>
      {!territory && <label>Seed <select value={seed ?? ''} onChange={(e) => emit({ ...source, [section]: { ...source[section], [id]: { ...entry, seed: e.target.value || undefined } } })}><option value="">Custom record</option>{Object.keys(defaults).sort().map((key) => <option key={key}>{key}</option>)}</select></label>}
      <Fields key={`${section}:${id}:${seed}`} defaults={territory ? Object.fromEntries(Object.entries(base).filter(([key]) => !['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))) : base}
        overrides={territory ? Object.fromEntries(Object.entries(overrides).filter(([key]) => !['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))) : overrides}
        onChange={(next) => changeOverrides(territory ? { ...Object.fromEntries(Object.entries(overrides).filter(([key]) => ['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))), ...next } : next)} />
    </>}
    <div className="scenario-toolbar">
      <select aria-label="Seed to add" value={seedToAdd} onChange={(e) => setSeedToAdd(e.target.value)}><option value="">{territory ? 'Choose territory' : 'Choose seed (or custom)'}</option>{Object.keys(defaults).sort().map((key) => <option key={key}>{key}</option>)}</select>
      {!territory && <input aria-label="New record id" placeholder="New record id (optional)" value={newId} onChange={(e) => setNewId(e.target.value)} />}
      <button type="button" disabled={!(newId.trim() || seedToAdd) || ids.includes(newId.trim() || seedToAdd) || (territory && !seedToAdd)} onClick={() => {
        const key = territory ? seedToAdd : newId.trim() || seedToAdd;
        if (territory) emit({ ...source, territory_ids: [...ids, key] });
        else emit({ ...source, [section]: { ...source[section], [key]: { ...(seedToAdd ? { seed: seedToAdd } : {}), overrides: seedToAdd ? {} : { id: key, display_name: key } } } });
        setSelected(key); setNewId(''); setSeedToAdd('');
      }}>Add {territory ? 'territory' : 'record'}</button>
    </div>
  </div>;
}
