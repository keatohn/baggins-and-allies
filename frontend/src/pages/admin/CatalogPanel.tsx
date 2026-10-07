import { useState } from 'react';
import type { Catalog, CatalogEntry, CatalogSpecial } from '../../services/api';

const ID_RE = /^[a-z][a-z0-9_]{0,47}$/;

function cleanId(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

function SpecialsSection({ specials, onChange }: { specials: CatalogSpecial[]; onChange: (next: CatalogSpecial[]) => void }) {
  const [newId, setNewId] = useState('');
  const id = cleanId(newId);
  const idOk = ID_RE.test(id) && !specials.some((s) => s.id === id);

  const update = (i: number, patch: Partial<CatalogSpecial>) => {
    onChange(specials.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  };
  const move = (i: number, by: number) => {
    const j = i + by;
    if (j < 0 || j >= specials.length) return;
    const next = [...specials];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <section className="admin-catalog-section">
      <h3>Specials</h3>
      <p className="admin-form__micro">
        Units list special ids. The order here is the order in the Specials modal and combat sim.
      </p>
      {specials.map((s, i) => (
        <div key={s.id} className="admin-special-card">
          <div className="admin-special-card__head">
            <strong>{s.id}</strong>
            <span className="admin-catalog-actions">
              <button type="button" className="admin-page__btn" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${s.id} up`}>
                ↑
              </button>
              <button type="button" className="admin-page__btn" disabled={i === specials.length - 1} onClick={() => move(i, 1)} aria-label={`Move ${s.id} down`}>
                ↓
              </button>
              <button type="button" className="admin-page__btn admin-page__btn--danger" onClick={() => onChange(specials.filter((_, j) => j !== i))}>
                Remove
              </button>
            </span>
          </div>
          <div className="admin-catalog-fields">
            <label className="admin-form__label">
              Name
              <input className="admin-form__input" value={s.name} onChange={(e) => update(i, { name: e.target.value })} />
            </label>
            <label className="admin-form__label admin-catalog-code">
              Code
              <input className="admin-form__input" value={s.display_code} maxLength={8} onChange={(e) => update(i, { display_code: e.target.value })} />
            </label>
          </div>
          <label className="admin-form__label">
            Description
            <textarea className="admin-form__textarea" value={s.description} onChange={(e) => update(i, { description: e.target.value })} />
          </label>
        </div>
      ))}
      <div className="admin-form__inline admin-catalog-add">
        <input
          className="admin-form__input"
          placeholder="new_special_id"
          value={newId}
          onChange={(e) => setNewId(e.target.value)}
        />
        <button
          type="button"
          className="admin-page__btn"
          disabled={!idOk}
          onClick={() => {
            onChange([...specials, { id, name: id, display_code: '', description: '' }]);
            setNewId('');
          }}
        >
          Add special
        </button>
      </div>
    </section>
  );
}

function FixedEntriesSection({
  title,
  note,
  entries,
  onChange,
}: {
  title: string;
  note: string;
  entries: CatalogEntry[];
  onChange: (next: CatalogEntry[]) => void;
}) {
  const update = (i: number, patch: Partial<CatalogEntry>) => {
    onChange(entries.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  };
  return (
    <section className="admin-catalog-section">
      <h3>{title}</h3>
      <p className="admin-form__micro">{note}</p>
      {entries.map((entry, i) => (
        <div key={entry.id} className="admin-special-card">
          <div className="admin-special-card__head">
            <strong>{entry.id}</strong>
          </div>
          <label className="admin-form__label">
            Name
            <input className="admin-form__input" value={entry.name} onChange={(e) => update(i, { name: e.target.value })} />
          </label>
          <label className="admin-form__label">
            Description
            <textarea className="admin-form__textarea" value={entry.description} onChange={(e) => update(i, { description: e.target.value })} />
          </label>
        </div>
      ))}
    </section>
  );
}

function IdListSection({
  title,
  note,
  values,
  locked,
  onChange,
}: {
  title: string;
  note: string;
  values: string[];
  locked?: readonly string[];
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState('');
  const id = cleanId(draft);
  const ok = ID_RE.test(id) && !values.includes(id);
  return (
    <section className="admin-catalog-section">
      <h3>{title}</h3>
      <p className="admin-form__micro">{note}</p>
      <ul className="admin-catalog-chips">
        {values.map((v) => (
          <li key={v} className="admin-catalog-chip">
            {v}
            {locked?.includes(v) ? null : (
              <button type="button" aria-label={`Remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>
                ×
              </button>
            )}
          </li>
        ))}
      </ul>
      <form
        className="admin-form__inline admin-catalog-add"
        onSubmit={(e) => {
          e.preventDefault();
          if (!ok) return;
          onChange([...values, id]);
          setDraft('');
        }}
      >
        <input className="admin-form__input" placeholder="new_id" value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button type="submit" className="admin-page__btn" disabled={!ok}>
          Add
        </button>
      </form>
    </section>
  );
}

export function CatalogPanel({ catalog, onChange }: { catalog: Catalog; onChange: (next: Catalog) => void }) {
  return (
    <div className="admin-form">
      <p className="admin-form__micro">
        Shared by every setup. Text changes show up everywhere right away, including games already in progress.
        Saving fails if you remove something a setup still uses.
      </p>
      <SpecialsSection specials={catalog.specials} onChange={(specials) => onChange({ ...catalog, specials })} />
      <FixedEntriesSection
        title="Special rules"
        note="A setup's manifest turns these on and holds their data. A setup can override the name or description."
        entries={catalog.special_rules}
        onChange={(special_rules) => onChange({ ...catalog, special_rules })}
      />
      <FixedEntriesSection
        title="Game options"
        note="Chosen when a game is created, for any setup."
        entries={catalog.game_options}
        onChange={(game_options) => onChange({ ...catalog, game_options })}
      />
      <IdListSection
        title="Terrain types"
        note="Territory terrain types a setup can use."
        values={catalog.terrain_types}
        locked={['sea']}
        onChange={(terrain_types) => onChange({ ...catalog, terrain_types })}
      />
      <IdListSection
        title="Archetypes"
        note="Unit archetypes a setup can use."
        values={catalog.archetypes}
        onChange={(archetypes) => onChange({ ...catalog, archetypes })}
      />
    </div>
  );
}
