import { useEffect, useMemo, useState } from 'react';
import type { AdminSetupBundle } from '../../services/api';

function linesToList(s: string): string[] {
  return s
    .split(/[\n,]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function listToLines(arr: unknown): string {
  return Array.isArray(arr) ? (arr as string[]).join('\n') : '';
}

function fieldRow(label: string, children: React.ReactNode) {
  return (
    <div className="admin-form__row">
      <label className="admin-form__label">{label}</label>
      <div className="admin-form__control">{children}</div>
    </div>
  );
}

function JsonObjectField({ value, onApply }: { value: unknown; onApply: (o: unknown) => void }) {
  const [t, setT] = useState('');
  useEffect(() => {
    setT(JSON.stringify(value ?? {}, null, 2));
  }, [JSON.stringify(value)]);
  return (
    <textarea
      className="admin-form__textarea admin-form__textarea--json"
      spellCheck={false}
      value={t}
      onChange={(e) => setT(e.target.value)}
      onBlur={() => {
        try {
          onApply(JSON.parse(t || '{}'));
        } catch {
          setT(JSON.stringify(value ?? {}, null, 2));
        }
      }}
    />
  );
}

function MultilineIdList({ value, onApply }: { value: unknown; onApply: (ids: string[]) => void }) {
  const [t, setT] = useState('');
  useEffect(() => {
    setT(listToLines(value));
  }, [JSON.stringify(value)]);
  return (
    <textarea
      className="admin-form__textarea"
      spellCheck={false}
      value={t}
      onChange={(e) => setT(e.target.value)}
      onBlur={() => onApply(linesToList(t))}
    />
  );
}

function MusicField({ value, onApply }: { value: unknown; onApply: (m: string | string[] | undefined) => void }) {
  const [t, setT] = useState('');
  useEffect(() => {
    if (value === undefined || value === null) setT('');
    else if (typeof value === 'string') setT(value);
    else setT(JSON.stringify(value, null, 2));
  }, [typeof value === 'string' ? value : JSON.stringify(value)]);
  return (
    <textarea
      className="admin-form__textarea admin-form__textarea--json"
      spellCheck={false}
      value={t}
      onChange={(e) => setT(e.target.value)}
      onBlur={() => {
        const raw = t.trim();
        if (!raw) {
          onApply(undefined);
          return;
        }
        if (raw.startsWith('[') || raw.startsWith('"')) {
          try {
            onApply(JSON.parse(raw) as string | string[]);
            return;
          } catch {
            /* treat as plain filename */
          }
        }
        onApply(raw);
      }}
    />
  );
}

export function ManifestPanel({
  setupId,
  manifest,
  onManifestChange,
}: {
  setupId: string;
  manifest: Record<string, unknown>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const [ctxDraft, setCtxDraft] = useState('');
  const [vcDraft, setVcDraft] = useState('');

  useEffect(() => {
    setCtxDraft(JSON.stringify(manifest.context ?? {}, null, 2));
    setVcDraft(JSON.stringify(manifest.victory_criteria ?? {}, null, 2));
  }, [setupId]);

  useEffect(() => {
    setCtxDraft(JSON.stringify(manifest.context ?? {}, null, 2));
  }, [manifest.context]);

  useEffect(() => {
    setVcDraft(JSON.stringify(manifest.victory_criteria ?? {}, null, 2));
  }, [manifest.victory_criteria]);

  const applyContext = () => {
    try {
      const o = JSON.parse(ctxDraft || '{}');
      if (typeof o !== 'object' || o === null) throw new Error('not an object');
      onManifestChange({ ...manifest, context: o });
    } catch {
      setCtxDraft(JSON.stringify(manifest.context ?? {}, null, 2));
    }
  };

  const applyVc = () => {
    try {
      const o = JSON.parse(vcDraft || '{}');
      if (typeof o !== 'object' || o === null) throw new Error('not an object');
      onManifestChange({ ...manifest, victory_criteria: o });
    } catch {
      setVcDraft(JSON.stringify(manifest.victory_criteria ?? {}, null, 2));
    }
  };

  return (
    <div className="admin-form">
      {fieldRow(
        'Setup id',
        <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={setupId} />,
      )}
      {fieldRow(
        'Display name',
        <input
          type="text"
          className="admin-form__input"
          value={String(manifest.display_name ?? '')}
          onChange={(e) => onManifestChange({ ...manifest, display_name: e.target.value })}
        />,
      )}
      {fieldRow(
        'Map asset',
        <input
          type="text"
          className="admin-form__input"
          placeholder="e.g. wotr_map_1.1"
          value={String(manifest.map_asset ?? '')}
          onChange={(e) => onManifestChange({ ...manifest, map_asset: e.target.value })}
        />,
      )}
      {fieldRow(
        'Active in create-game menu',
        <input
          type="checkbox"
          checked={manifest.is_active === true}
          onChange={(e) => onManifestChange({ ...manifest, is_active: e.target.checked })}
        />,
      )}
      {fieldRow(
        'Camp cost',
        <input
          type="number"
          className="admin-form__input admin-form__input--narrow"
          value={manifest.camp_cost != null ? String(manifest.camp_cost) : ''}
          onChange={(e) =>
            onManifestChange({
              ...manifest,
              camp_cost: e.target.value === '' ? undefined : Number(e.target.value),
            })
          }
        />,
      )}
      {fieldRow(
        'Stronghold repair cost',
        <input
          type="number"
          className="admin-form__input admin-form__input--narrow"
          value={manifest.stronghold_repair_cost != null ? String(manifest.stronghold_repair_cost) : ''}
          onChange={(e) =>
            onManifestChange({
              ...manifest,
              stronghold_repair_cost: e.target.value === '' ? undefined : Number(e.target.value),
            })
          }
        />,
      )}
      {fieldRow(
        'Prefire penalty (−1 stealth/archer prefire)',
        <input
          type="checkbox"
          checked={manifest.prefire_penalty !== false}
          onChange={(e) => onManifestChange({ ...manifest, prefire_penalty: e.target.checked })}
        />,
      )}
      {fieldRow(
        'Context (JSON)',
        <>
          <textarea className="admin-form__textarea admin-form__textarea--json" spellCheck={false} value={ctxDraft} onChange={(e) => setCtxDraft(e.target.value)} onBlur={applyContext} />
          <p className="admin-form__micro">Blur to apply. Required when Active is checked.</p>
        </>,
      )}
      {fieldRow(
        'Victory criteria (JSON)',
        <>
          <textarea className="admin-form__textarea admin-form__textarea--json" spellCheck={false} value={vcDraft} onChange={(e) => setVcDraft(e.target.value)} onBlur={applyVc} />
          <p className="admin-form__micro">Blur to apply.</p>
        </>,
      )}
    </div>
  );
}

function EntityDictPanel({
  title,
  data,
  onChange,
  renderEditor,
}: {
  title: string;
  data: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
  renderEditor: (id: string, obj: Record<string, unknown>, patch: (p: Record<string, unknown>) => void) => React.ReactNode;
}) {
  const keys = useMemo(() => Object.keys(data).sort(), [data]);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const filtered = useMemo(
    () => keys.filter((k) => k.toLowerCase().includes(filter.trim().toLowerCase())),
    [keys, filter],
  );

  useEffect(() => {
    if (selected && !keys.includes(selected)) setSelected(null);
    if (!selected && keys.length) setSelected(keys[0]);
  }, [keys, selected]);

  const patchSelected = (p: Record<string, unknown>) => {
    if (!selected) return;
    const cur = data[selected] ?? { id: selected };
    onChange({ ...data, [selected]: { ...cur, ...p } });
  };

  const addId = () => {
    const raw = window.prompt(`New ${title} id (key):`);
    if (!raw) return;
    const id = raw.trim();
    if (!id || data[id]) {
      window.alert(data[id] ? 'That id already exists' : 'Invalid id');
      return;
    }
    onChange({ ...data, [id]: { id } });
    setSelected(id);
  };

  const removeSelected = () => {
    if (!selected) return;
    if (!window.confirm(`Delete "${selected}"?`)) return;
    const next = { ...data };
    delete next[selected];
    onChange(next);
    setSelected(null);
  };

  const obj = selected ? data[selected] ?? { id: selected } : null;

  return (
    <div className="admin-entity">
      <div className="admin-entity__sidebar">
        <div className="admin-entity__toolbar">
          <input type="search" className="admin-form__input" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <button type="button" className="admin-page__btn" onClick={addId}>
            Add
          </button>
          <button type="button" className="admin-page__btn" onClick={removeSelected} disabled={!selected}>
            Delete
          </button>
        </div>
        <ul className="admin-entity__list">
          {filtered.map((k) => (
            <li key={k}>
              <button type="button" className={`admin-entity__item${selected === k ? ' admin-entity__item--active' : ''}`} onClick={() => setSelected(k)}>
                {k}
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="admin-entity__main">
        {obj && selected ? renderEditor(selected, obj, patchSelected) : <p className="admin-form__micro">Select or add an entry.</p>}
      </div>
    </div>
  );
}

export function UnitsPanel({
  units,
  onChange,
}: {
  units: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  return (
    <EntityDictPanel
      title="unit"
      data={units}
      onChange={onChange}
      renderEditor={(id, u, patch) => (
        <div className="admin-form">
          {fieldRow('Unit id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Display name',
            <input type="text" className="admin-form__input" value={String(u.display_name ?? '')} onChange={(e) => patch({ display_name: e.target.value })} />,
          )}
          {fieldRow(
            'Faction',
            <input type="text" className="admin-form__input" value={String(u.faction ?? '')} onChange={(e) => patch({ faction: e.target.value })} />,
          )}
          {fieldRow(
            'Archetype',
            <input type="text" className="admin-form__input" value={String(u.archetype ?? '')} onChange={(e) => patch({ archetype: e.target.value })} />,
          )}
          {fieldRow(
            'Tags (comma-separated)',
            <input
              type="text"
              className="admin-form__input"
              value={Array.isArray(u.tags) ? (u.tags as string[]).join(', ') : ''}
              onChange={(e) =>
                patch({
                  tags: e.target.value
                    .split(',')
                    .map((t) => t.trim())
                    .filter(Boolean),
                })
              }
            />,
          )}
          {fieldRow(
            'Attack',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.attack != null ? String(u.attack) : ''} onChange={(e) => patch({ attack: Number(e.target.value) })} />,
          )}
          {fieldRow(
            'Defense',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.defense != null ? String(u.defense) : ''} onChange={(e) => patch({ defense: Number(e.target.value) })} />,
          )}
          {fieldRow(
            'Movement',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.movement != null ? String(u.movement) : ''} onChange={(e) => patch({ movement: Number(e.target.value) })} />,
          )}
          {fieldRow(
            'Health',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.health != null ? String(u.health) : ''} onChange={(e) => patch({ health: Number(e.target.value) })} />,
          )}
          {fieldRow(
            'Dice',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.dice != null ? String(u.dice) : '1'} onChange={(e) => patch({ dice: Number(e.target.value) || 1 })} />,
          )}
          {fieldRow(
            'Cost (JSON)',
            <JsonObjectField value={u.cost ?? { power: 0 }} onApply={(o) => patch({ cost: o })} />,
          )}
          {fieldRow(
            'Purchasable',
            <input type="checkbox" checked={u.purchasable !== false} onChange={(e) => patch({ purchasable: e.target.checked })} />,
          )}
          {fieldRow(
            'Unique',
            <input type="checkbox" checked={u.unique === true} onChange={(e) => patch({ unique: e.target.checked })} />,
          )}
          {fieldRow(
            'Icon filename',
            <input type="text" className="admin-form__input" value={String(u.icon ?? '')} onChange={(e) => patch({ icon: e.target.value || undefined })} />,
          )}
          {fieldRow(
            'Transport capacity',
            <input type="number" className="admin-form__input admin-form__input--narrow" value={u.transport_capacity != null ? String(u.transport_capacity) : '0'} onChange={(e) => patch({ transport_capacity: Number(e.target.value) || 0 })} />,
          )}
          {fieldRow(
            'Downgrade to unit id',
            <input type="text" className="admin-form__input" value={String(u.downgrade_to ?? '')} onChange={(e) => patch({ downgrade_to: e.target.value || undefined })} />,
          )}
          {fieldRow(
            'Specials (comma-separated)',
            <input
              type="text"
              className="admin-form__input"
              value={Array.isArray(u.specials) ? (u.specials as string[]).join(', ') : ''}
              onChange={(e) =>
                patch({
                  specials: e.target.value
                    .split(',')
                    .map((t) => t.trim())
                    .filter(Boolean),
                })
              }
            />,
          )}
          {fieldRow(
            'Home territory ids (comma-separated)',
            <input
              type="text"
              className="admin-form__input"
              value={
                Array.isArray(u.home_territory_ids)
                  ? (u.home_territory_ids as string[]).join(', ')
                  : typeof u.home_territory_id === 'string'
                    ? u.home_territory_id
                    : ''
              }
              onChange={(e) => {
                const parts = e.target.value
                  .split(',')
                  .map((t) => t.trim())
                  .filter(Boolean);
                patch({ home_territory_ids: parts.length ? parts : undefined, home_territory_id: undefined });
              }}
            />,
          )}
        </div>
      )}
    />
  );
}

export function TerritoriesPanel({
  territories,
  onChange,
}: {
  territories: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  return (
    <EntityDictPanel
      title="territory"
      data={territories}
      onChange={onChange}
      renderEditor={(id, t, patch) => (
        <div className="admin-form">
          {fieldRow('Territory id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Display name',
            <input type="text" className="admin-form__input" value={String(t.display_name ?? '')} onChange={(e) => patch({ display_name: e.target.value })} />,
          )}
          {fieldRow(
            'Terrain type',
            <input type="text" className="admin-form__input" value={String(t.terrain_type ?? '')} onChange={(e) => patch({ terrain_type: e.target.value })} />,
          )}
          {fieldRow(
            'Adjacent (one id per line or comma-separated)',
            <MultilineIdList value={t.adjacent} onApply={(ids) => patch({ adjacent: ids })} />,
          )}
          {fieldRow(
            'Aerial adjacent',
            <MultilineIdList value={t.aerial_adjacent} onApply={(ids) => patch({ aerial_adjacent: ids })} />,
          )}
          {fieldRow(
            'Ford adjacent',
            <MultilineIdList value={t.ford_adjacent} onApply={(ids) => patch({ ford_adjacent: ids })} />,
          )}
          {fieldRow(
            'Produces (JSON)',
            <JsonObjectField value={t.produces ?? {}} onApply={(o) => patch({ produces: o })} />,
          )}
          {fieldRow(
            'Stronghold',
            <input type="checkbox" checked={t.is_stronghold === true} onChange={(e) => patch({ is_stronghold: e.target.checked })} />,
          )}
          {fieldRow(
            'Stronghold base health',
            <input
              type="number"
              className="admin-form__input admin-form__input--narrow"
              value={t.stronghold_base_health != null ? String(t.stronghold_base_health) : t.stronghold_health != null ? String(t.stronghold_health) : '0'}
              onChange={(e) => patch({ stronghold_base_health: Number(e.target.value) || 0, stronghold_health: undefined })}
            />,
          )}
          {fieldRow(
            'Ownable',
            <input type="checkbox" checked={t.ownable !== false} onChange={(e) => patch({ ownable: e.target.checked })} />,
          )}
        </div>
      )}
    />
  );
}

export function FactionsPanel({
  factions,
  onChange,
}: {
  factions: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  return (
    <EntityDictPanel
      title="faction"
      data={factions}
      onChange={onChange}
      renderEditor={(id, f, patch) => (
        <div className="admin-form">
          {fieldRow('Faction id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Display name',
            <input type="text" className="admin-form__input" value={String(f.display_name ?? '')} onChange={(e) => patch({ display_name: e.target.value })} />,
          )}
          {fieldRow(
            'Alliance',
            <input type="text" className="admin-form__input" placeholder="good | evil" value={String(f.alliance ?? '')} onChange={(e) => patch({ alliance: e.target.value })} />,
          )}
          {fieldRow(
            'Capital territory id',
            <input type="text" className="admin-form__input" value={String(f.capital ?? '')} onChange={(e) => patch({ capital: e.target.value })} />,
          )}
          {fieldRow(
            'Color',
            <input type="text" className="admin-form__input" value={String(f.color ?? '')} onChange={(e) => patch({ color: e.target.value })} />,
          )}
          {fieldRow(
            'Icon filename',
            <input type="text" className="admin-form__input" value={String(f.icon ?? '')} onChange={(e) => patch({ icon: e.target.value || undefined })} />,
          )}
          {fieldRow(
            'Music (filename, JSON string, or JSON array)',
            <MusicField value={f.music} onApply={(m) => patch({ music: m })} />,
          )}
        </div>
      )}
    />
  );
}

export function CampsPanel({
  camps,
  onChange,
}: {
  camps: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  return (
    <EntityDictPanel
      title="camp"
      data={camps}
      onChange={onChange}
      renderEditor={(id, c, patch) => (
        <div className="admin-form">
          {fieldRow('Camp id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Territory id',
            <input type="text" className="admin-form__input" value={String(c.territory_id ?? '')} onChange={(e) => patch({ territory_id: e.target.value })} />,
          )}
        </div>
      )}
    />
  );
}

export function PortsPanel({
  ports,
  onChange,
}: {
  ports: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  return (
    <EntityDictPanel
      title="port"
      data={ports}
      onChange={onChange}
      renderEditor={(id, p, patch) => (
        <div className="admin-form">
          {fieldRow('Port id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Territory id',
            <input type="text" className="admin-form__input" value={String(p.territory_id ?? '')} onChange={(e) => patch({ territory_id: e.target.value })} />,
          )}
        </div>
      )}
    />
  );
}

export function StartingSetupPanel({
  bundle,
  onChange,
}: {
  bundle: AdminSetupBundle;
  onChange: (ss: Record<string, unknown>) => void;
}) {
  const ss = bundle.starting_setup as Record<string, unknown>;
  const turnOrder = Array.isArray(ss.turn_order) ? ([...ss.turn_order] as string[]) : [];
  const owners = (ss.territory_owners && typeof ss.territory_owners === 'object' ? ss.territory_owners : {}) as Record<string, string>;
  const startingUnits = (ss.starting_units && typeof ss.starting_units === 'object' ? ss.starting_units : {}) as Record<string, { unit_id: string; count: number }[]>;

  const territoryIds = useMemo(
    () => [...new Set([...Object.keys(bundle.territories), ...Object.keys(owners), ...Object.keys(startingUnits)])].sort(),
    [bundle.territories, owners, startingUnits],
  );
  const unitIds = useMemo(() => Object.keys(bundle.units).sort(), [bundle.units]);

  const [selTer, setSelTer] = useState<string>(() => territoryIds[0] ?? '');

  useEffect(() => {
    if (selTer && !territoryIds.includes(selTer) && territoryIds.length) setSelTer(territoryIds[0]);
  }, [territoryIds, selTer]);

  const setTurnOrder = (list: string[]) => onChange({ ...ss, turn_order: list });
  const setOwners = (o: Record<string, string>) => onChange({ ...ss, territory_owners: o });
  const setStartingUnits = (su: Record<string, { unit_id: string; count: number }[]>) => onChange({ ...ss, starting_units: su });

  const stacks = selTer ? startingUnits[selTer] ?? [] : [];

  const updateStack = (i: number, patch: Partial<{ unit_id: string; count: number }>) => {
    const next = { ...startingUnits };
    const row = [...(next[selTer] ?? [])];
    const prev = row[i];
    const base = {
      unit_id: typeof prev?.unit_id === 'string' && prev.unit_id ? prev.unit_id : unitIds[0] ?? '',
      count: typeof prev?.count === 'number' && Number.isFinite(prev.count) ? Math.max(1, prev.count) : 1,
    };
    row[i] = { ...base, ...patch };
    if (typeof row[i].count === 'number') row[i].count = Math.max(1, row[i].count);
    next[selTer] = row;
    setStartingUnits(next);
  };

  const addStack = () => {
    const next = { ...startingUnits };
    const row = [...(next[selTer] ?? [])];
    row.push({ unit_id: unitIds[0] ?? '', count: 1 });
    next[selTer] = row;
    setStartingUnits(next);
  };

  const removeStack = (i: number) => {
    const next = { ...startingUnits };
    const row = [...(next[selTer] ?? [])];
    row.splice(i, 1);
    if (row.length) next[selTer] = row;
    else delete next[selTer];
    setStartingUnits(next);
  };

  return (
    <div className="admin-form">
      <h3 className="admin-form__subtitle">Turn order</h3>
      <p className="admin-form__micro">One faction id per row (drag order = turn order).</p>
      {turnOrder.map((fid, i) => (
        <div key={`${fid}-${i}`} className="admin-form__inline">
          <input type="text" className="admin-form__input" value={fid} onChange={(e) => {
            const next = [...turnOrder];
            next[i] = e.target.value;
            setTurnOrder(next);
          }} />
          <button type="button" className="admin-page__btn" onClick={() => {
            if (i === 0) return;
            const next = [...turnOrder];
            [next[i - 1], next[i]] = [next[i], next[i - 1]];
            setTurnOrder(next);
          }}>
            Up
          </button>
          <button type="button" className="admin-page__btn" onClick={() => {
            if (i >= turnOrder.length - 1) return;
            const next = [...turnOrder];
            [next[i], next[i + 1]] = [next[i + 1], next[i]];
            setTurnOrder(next);
          }}>
            Down
          </button>
          <button type="button" className="admin-page__btn" onClick={() => setTurnOrder(turnOrder.filter((_, j) => j !== i))}>
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="admin-page__btn" onClick={() => setTurnOrder([...turnOrder, ''])}>
        Add faction to turn order
      </button>

      <h3 className="admin-form__subtitle">Territory owners</h3>
      {territoryIds.map((tid) => (
        <div key={tid} className="admin-form__row">
          <label className="admin-form__label">{tid}</label>
          <input
            type="text"
            className="admin-form__input"
            placeholder="faction id"
            value={owners[tid] ?? ''}
            onChange={(e) => {
              const next = { ...owners };
              if (e.target.value) next[tid] = e.target.value;
              else delete next[tid];
              setOwners(next);
            }}
          />
        </div>
      ))}

      <h3 className="admin-form__subtitle">Starting units by territory</h3>
      <div className="admin-form__row">
        <label className="admin-form__label">Territory</label>
        <select className="admin-page__select" value={selTer} onChange={(e) => setSelTer(e.target.value)}>
          {territoryIds.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      {stacks.map((st, i) => (
        <div key={i} className="admin-form__inline">
          <select className="admin-page__select" value={st.unit_id} onChange={(e) => updateStack(i, { unit_id: e.target.value })}>
            {unitIds.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
          <input
            type="number"
            min={1}
            className="admin-form__input admin-form__input--narrow"
            value={st.count}
            onChange={(e) => updateStack(i, { count: Math.max(1, Number(e.target.value) || 1) })}
          />
          <button type="button" className="admin-page__btn" onClick={() => removeStack(i)}>
            Remove stack
          </button>
        </div>
      ))}
      <button type="button" className="admin-page__btn" onClick={addStack} disabled={!selTer}>
        Add stack in {selTer || '…'}
      </button>
    </div>
  );
}

function SpecialOrderField({ order, onApply }: { order: string[]; onApply: (ids: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const sig = order.join('|');
  useEffect(() => {
    setDraft(order.join(', '));
  }, [sig]);
  return (
    <>
      <input
        type="text"
        className="admin-form__input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() =>
          onApply(
            draft
              .split(',')
              .map((x) => x.trim())
              .filter(Boolean),
          )
        }
      />
      <p className="admin-form__micro">Comma-separated ids. Blur to apply.</p>
    </>
  );
}

export function SpecialsPanel({
  specials,
  onChange,
}: {
  specials: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const order = Array.isArray(specials.order) ? ([...specials.order] as string[]) : [];
  const defs = { ...specials };
  delete (defs as { order?: unknown }).order;
  const keys = Object.keys(defs).sort();

  const patchDef = (id: string, patch: Record<string, unknown>) => {
    onChange({ ...specials, [id]: { ...(typeof defs[id] === 'object' ? (defs[id] as object) : {}), ...patch } });
  };

  const addSpecial = () => {
    const raw = window.prompt('New special id:');
    if (!raw) return;
    const id = raw.trim();
    if (!id || id === 'order' || specials[id] != null) {
      window.alert('Invalid or duplicate id');
      return;
    }
    onChange({ ...specials, [id]: { name: id, description: '', display_code: '' } });
  };

  const removeSpecial = (id: string) => {
    if (!window.confirm(`Remove special "${id}"?`)) return;
    const next = { ...specials };
    delete next[id];
    next.order = (Array.isArray(next.order) ? next.order : []).filter((x) => x !== id);
    onChange(next);
  };

  return (
    <div className="admin-form">
      {fieldRow(
        'Display order',
        <SpecialOrderField order={order} onApply={(ids) => onChange({ ...specials, order: ids })} />,
      )}
      <button type="button" className="admin-page__btn" onClick={addSpecial}>
        Add special
      </button>
      {keys.map((id) => {
        const row = (typeof defs[id] === 'object' && defs[id] !== null ? defs[id] : {}) as Record<string, unknown>;
        return (
          <div key={id} className="admin-special-card">
            <div className="admin-special-card__head">
              <strong>{id}</strong>
              <button type="button" className="admin-page__btn" onClick={() => removeSpecial(id)}>
                Remove
              </button>
            </div>
            {fieldRow(
              'Name',
              <input type="text" className="admin-form__input" value={String(row.name ?? '')} onChange={(e) => patchDef(id, { name: e.target.value })} />,
            )}
            {fieldRow(
              'Description',
              <textarea className="admin-form__textarea" value={String(row.description ?? '')} onChange={(e) => patchDef(id, { description: e.target.value })} />,
            )}
            {fieldRow(
              'Display code',
              <input type="text" className="admin-form__input" value={String(row.display_code ?? '')} onChange={(e) => patchDef(id, { display_code: e.target.value })} />,
            )}
          </div>
        );
      })}
    </div>
  );
}

export function JsonTabEditor({ value, onChange }: { value: unknown; onChange: (parsed: unknown) => void }) {
  const [text, setText] = useState('');
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setText(JSON.stringify(value ?? {}, null, 2));
    setErr(null);
  }, [value]);

  const apply = () => {
    try {
      const p = JSON.parse(text);
      onChange(p);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Invalid JSON');
    }
  };

  return (
    <div>
      <textarea className="admin-page__editor" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="admin-page__actions">
        <button type="button" className="admin-page__btn" onClick={apply}>
          Apply JSON
        </button>
      </div>
      {err ? <p className="admin-page__error">{err}</p> : null}
    </div>
  );
}
