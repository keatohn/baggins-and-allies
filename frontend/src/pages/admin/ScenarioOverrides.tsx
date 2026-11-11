import { useEffect, useRef, useState } from 'react';
import type { EntityRecords, ScenarioLibrary, SetupDefinition } from '../../services/api';
import { deleted, object, resolve, scenarioSelectionOptions, withScenarioSelection, type SelectionOption, type Dict } from './scenarioOverridesModel';

function ValueInput({ value, onChange, label }: { value: unknown; onChange: (v: unknown) => void; label: string }) {
  const [raw, setRaw] = useState(JSON.stringify(value ?? null));
  const [error, setError] = useState('');
  if (typeof value === 'number') return <input className="admin-form__input" aria-label={label} type="number" value={value} onChange={(e) => { if (e.target.value !== '') onChange(Number(e.target.value)); }} />;
  if (typeof value === 'boolean') return <input aria-label={label} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />;
  if (typeof value === 'string') return <input className="admin-form__input" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} />;
  return <div><textarea className="admin-form__textarea" aria-label={label} value={raw} rows={2} onChange={(e) => {
    setRaw(e.target.value);
    try { onChange(JSON.parse(e.target.value)); setError(''); e.target.setCustomValidity(''); } catch { setError('Enter valid JSON.'); e.target.setCustomValidity('Enter valid JSON.'); }
  }} />{error && <span className="admin-page__error" role="alert">{error}</span>}</div>;
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
      <div className="scenario-field__value">{override && <ValueInput key={`${label}:${deleted(overrides[key]) ? 'removed' : typeof current}`} label={label} value={current} onChange={(v) => onChange({ ...overrides, [key]: v })} />}</div>
      {override && <button type="button" className="admin-page__btn" title="Remove this optional field from the compiled record" onClick={() => onChange({ ...overrides, [key]: { $delete: true } })}>Remove field</button>}
    </div>;
  })}<div className="scenario-toolbar"><input className="admin-form__input" aria-label={`New ${prefix}field`} placeholder="Additional field" value={field} onChange={(e) => setField(e.target.value)} /><button type="button" className="admin-page__btn" disabled={!/^[a-z][a-z0-9_]*$/.test(field) || field === 'id' || field in resolved} onClick={() => { onChange({ ...overrides, [field]: null }); setField(''); }}>Add override field</button></div></>;
}

function SeedSelectionDialog({ title, options, selected, onClose, onApply }: {
  title: string; options: SelectionOption[]; selected: string[]; onClose: () => void; onApply: (ids: string[]) => void;
}) {
  const [checked, setChecked] = useState(() => new Set(selected));
  const [filter, setFilter] = useState('');
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => { previous?.focus(); };
  }, []);
  const shown = options.filter((option) => `${option.id} ${option.name}`.toLowerCase().includes(filter.trim().toLowerCase()));
  const toggle = (ids: string[], enabled: boolean) => setChecked((current) => {
    const next = new Set(current);
    ids.forEach((id) => { if (enabled) next.add(id); else next.delete(id); });
    return next;
  });
  return <div className="admin-modal-overlay" role="presentation" onClick={onClose} onKeyDown={(e) => {
    if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
    if (e.key === 'Tab') {
      const controls = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input')];
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
  }}>
    <div className="admin-modal scenario-selection" role="dialog" aria-modal="true" aria-labelledby="scenario-selection-title" onClick={(e) => e.stopPropagation()}>
      <div className="balance-modal__header">
        <h2 id="scenario-selection-title" className="admin-modal__title">{title}</h2>
        <button ref={closeRef} type="button" className="admin-page__btn" onClick={onClose}>Close</button>
      </div>
      <p className="admin-form__micro">Choose seeds for this setup. Existing overrides are kept for checked entries. Unchecking an entry removes it and its overrides when you update the list.</p>
      <input type="search" className="admin-form__input" aria-label="Filter seeds" placeholder="Filter seeds…" value={filter} onChange={(e) => setFilter(e.target.value)} />
      <div className="scenario-toolbar">
        <button type="button" className="admin-page__btn" disabled={!shown.length} onClick={() => toggle(shown.map((option) => option.id), true)}>Select all shown</button>
        <button type="button" className="admin-page__btn" disabled={!shown.length} onClick={() => toggle(shown.map((option) => option.id), false)}>Clear shown</button>
        <span className="admin-form__micro" role="status">{checked.size} selected</span>
      </div>
      <div className="scenario-selection__list">
        {shown.map((option) => <label key={option.id} className="scenario-selection__option">
          <input type="checkbox" checked={checked.has(option.id)} onChange={(e) => toggle([option.id], e.target.checked)} />
          <span>{option.name}<small>{option.id}{option.seed && option.seed !== option.id ? ` (seed: ${option.seed})` : !option.seed ? ' (custom)' : ''}</small></span>
        </label>)}
        {!shown.length && <p className="admin-form__micro">No matching seeds.</p>}
      </div>
      <div className="admin-modal__actions">
        <button type="button" className="admin-page__btn" onClick={onClose}>Cancel</button>
        <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={() => onApply(options.filter((option) => checked.has(option.id)).map((option) => option.id))}>Update List</button>
      </div>
    </div>
  </div>;
}

/** Defaults remain visible. Checked fields alone are stored as scenario overrides. */
export function ScenarioOverrides({ section, source, library, onChange }: {
  section: 'units' | 'factions' | 'territories'; source: SetupDefinition; library: ScenarioLibrary;
  onChange: (records: EntityRecords, source: SetupDefinition) => void;
}) {
  const [selected, setSelected] = useState('');
  const [newId, setNewId] = useState('');
  const [filter, setFilter] = useState('');
  const [updatingList, setUpdatingList] = useState(false);
  const singular = { units: 'Unit', territories: 'Territory', factions: 'Faction' }[section];
  const territory = section === 'territories';
  const defaults = territory ? library.maps[String(source.manifest.map_asset || source.manifest.id)] ?? {} : library[section];
  const ids = Object.keys(source[section]);
  const id = ids.includes(selected) ? selected : ids[0] ?? '';
  const entry = source[section][id];
  const seed = entry?.seed;
  const base = defaults[seed ?? ''] ?? {};
  const overrides = entry?.overrides ?? {};

  const emit = (next: SetupDefinition) => {
    const records: EntityRecords = {};
    for (const [key, item] of Object.entries(next[section])) {
      records[key] = resolve(defaults[item.seed ?? key] ?? {}, item.overrides ?? {});
      if ('id' in records[key] && !Object.hasOwn(item.overrides ?? {}, 'id')) records[key].id = key;
    }
    onChange(records, next);
  };
  const changeOverrides = (next: Dict) => emit({ ...source,
    [section]: { ...source[section], [id]: { ...entry, overrides: next } } });
  const options = scenarioSelectionOptions(source, library, section);
  const visibleIds = [...ids].sort().filter((key) => {
    const option = options.find((option) => option.id === key);
    return `${key} ${option?.name ?? ''}`.toLowerCase().includes(filter.trim().toLowerCase());
  });
  return <div className="scenario-overrides">
    <p className="admin-form__micro">Select an entry to modify its overrides. Unchecked fields inherit their shared default. Map terrain and connections are edited in Maps.</p>
    <div className="admin-entity">
      <div className="admin-entity__sidebar">
        <button type="button" className="admin-page__btn" onClick={() => setUpdatingList(true)}>Update {singular} List</button>
        <input type="search" className="admin-form__input" aria-label={`Filter selected ${section}`} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ul className="admin-entity__list" aria-label={`Selected ${section}`}>
          {visibleIds.map((key) => <li key={key}>
            <button type="button" className={`admin-entity__item${id === key ? ' admin-entity__item--active' : ''}`} aria-pressed={id === key} onClick={() => setSelected(key)}>{key}</button>
          </li>)}
        </ul>
        {!visibleIds.length && <p className="admin-form__micro">{ids.length ? 'No matching entries.' : `No ${section} selected.`}</p>}
        {!territory && <details>
          <summary className="admin-form__micro">Add custom {singular.toLowerCase()}</summary>
          <div className="scenario-custom-record">
            <input className="admin-form__input" aria-label="New record id" placeholder="New record id" value={newId} onChange={(e) => setNewId(e.target.value)} />
            <button type="button" className="admin-page__btn" disabled={!newId.trim() || Object.hasOwn(source[section], newId.trim())} onClick={() => {
              const key = newId.trim();
              emit({ ...source, [section]: { ...source[section], [key]: { overrides: { id: key, display_name: key } } } });
              setSelected(key); setNewId(''); setFilter('');
            }}>Add custom {singular.toLowerCase()}</button>
          </div>
        </details>}
      </div>
      <div className="admin-entity__main">
        {id ? <>
          <h3 className="admin-form__subtitle">{id}</h3>
          <p className="admin-form__micro">{seed ? `Seed: ${seed}` : 'Custom record'}</p>
          <Fields key={`${section}:${id}:${seed}`} defaults={territory ? Object.fromEntries(Object.entries(base).filter(([key]) => !['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))) : base}
            overrides={territory ? Object.fromEntries(Object.entries(overrides).filter(([key]) => !['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))) : overrides}
            onChange={(next) => changeOverrides(territory ? { ...Object.fromEntries(Object.entries(overrides).filter(([key]) => ['terrain_type', 'adjacent', 'aerial_adjacent', 'ford_adjacent'].includes(key))), ...next } : next)} />
        </> : <p className="admin-form__micro">Use Update {singular} List to choose seeds for this setup.</p>}
      </div>
    </div>
    {updatingList && <SeedSelectionDialog title={`Update ${singular} List`} options={options} selected={ids} onClose={() => setUpdatingList(false)} onApply={(nextIds) => {
      emit(withScenarioSelection(source, section, nextIds));
      setUpdatingList(false);
      if (!nextIds.includes(id)) setSelected(nextIds[0] ?? '');
    }} />}
  </div>;
}
