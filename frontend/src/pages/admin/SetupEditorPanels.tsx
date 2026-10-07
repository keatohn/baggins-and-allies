import { useEffect, useMemo, useRef, useState } from 'react';
import MUSIC_M4A from 'virtual:music-m4a';
import UNIT_ICON_PNG from 'virtual:unit-icon-png';
import FACTION_ICON_PNG from 'virtual:faction-icon-png';
import TERRITORY_IMAGE_PNG from 'virtual:territory-image-png';
import SCENARIO_IMAGE from 'virtual:scenario-image';
import { api, type AdminSetupBundle, type AdminSetupListItem } from '../../services/api';

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

function parseCommaList(raw: string): string[] {
  return raw
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
}

function commaListText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.filter((x): x is string => typeof x === 'string' && x.trim() !== '').join(', ');
  }
  if (typeof value === 'string' && value.trim()) return value.trim();
  return '';
}

/** Keeps the raw text while typing so a trailing comma or space is not stripped on each keystroke. */
function CommaListField({ value, onApply }: { value: unknown; onApply: (ids: string[]) => void }) {
  const committed = commaListText(value);
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <input
      type="text"
      className="admin-form__input"
      spellCheck={false}
      value={draft ?? committed}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        onApply(parseCommaList(raw));
      }}
      onBlur={() => setDraft(null)}
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

function musicValueToList(value: unknown): string[] {
  if (value == null) return [];
  if (typeof value === 'string') {
    const s = value.trim();
    return s ? [s] : [];
  }
  if (Array.isArray(value)) {
    return value.filter((x): x is string => typeof x === 'string' && Boolean(x.trim())).map((x) => x.trim());
  }
  return [];
}

function musicChipLabel(filename: string): string {
  return filename.replace(/\.(m4a|mp3|ogg|wav)$/i, '');
}

export function MusicField({
  value,
  files,
  emptyLabel,
  addAriaLabel,
  onApply,
}: {
  value: unknown;
  files: string[];
  emptyLabel: string;
  addAriaLabel: string;
  onApply: (m: string | string[] | undefined) => void;
}) {
  const selected = musicValueToList(value);
  const unselected = files.filter((f) => !selected.includes(f));

  const commit = (next: string[]) => {
    onApply(next.length > 0 ? next : undefined);
  };

  return (
    <div className="admin-music-picker">
      {selected.length === 0 ? (
        <span className="admin-music-picker__empty">{emptyLabel}</span>
      ) : (
        selected.map((filename, idx) => (
          <span key={`${filename}-${idx}`} className="admin-music-chip">
            <span className="admin-music-chip__name" title={filename}>
              {musicChipLabel(filename)}
            </span>
            <span className="admin-music-chip__actions">
              <button
                type="button"
                className="admin-music-chip__btn"
                disabled={idx === 0}
                aria-label={`Move ${filename} earlier`}
                onClick={() => {
                  if (idx === 0) return;
                  const next = selected.slice();
                  [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
                  commit(next);
                }}
              >
                ‹
              </button>
              <button
                type="button"
                className="admin-music-chip__btn"
                disabled={idx === selected.length - 1}
                aria-label={`Move ${filename} later`}
                onClick={() => {
                  if (idx >= selected.length - 1) return;
                  const next = selected.slice();
                  [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
                  commit(next);
                }}
              >
                ›
              </button>
              <button
                type="button"
                className="admin-music-chip__btn admin-music-chip__btn--remove"
                aria-label={`Remove ${filename}`}
                onClick={() => commit(selected.filter((_, i) => i !== idx))}
              >
                ×
              </button>
            </span>
          </span>
        ))
      )}
      <select
        className="admin-form__input admin-music-picker__add"
        aria-label={addAriaLabel}
        value=""
        disabled={unselected.length === 0}
        onChange={(e) => {
          const filename = e.target.value;
          if (!filename || selected.includes(filename)) return;
          commit([...selected, filename]);
        }}
      >
        <option value="">{unselected.length === 0 ? 'All tracks added' : 'Add track…'}</option>
        {unselected.map((f) => (
          <option key={f} value={f}>
            {musicChipLabel(f)}
          </option>
        ))}
      </select>
    </div>
  );
}

function HeroIdField({
  value,
  knownHeroIds,
  onApply,
}: {
  value: unknown;
  knownHeroIds: string[];
  onApply: (id: string | undefined) => void;
}) {
  const current = typeof value === 'string' ? value.trim() : '';
  const options = [...knownHeroIds];
  if (current && !options.includes(current)) options.unshift(current);
  return (
    <div className="admin-icon-picker">
      <input
        className="admin-form__input admin-icon-picker__select"
        list="admin-hero-id-options"
        placeholder="None (not a hero)"
        value={current}
        onChange={(e) => onApply(e.target.value.trim() || undefined)}
        aria-label="Hero id"
      />
      <datalist id="admin-hero-id-options">
        {options.map((id) => (
          <option key={id} value={id} />
        ))}
      </datalist>
    </div>
  );
}

function iconStem(filename: string): string {
  return filename.replace(/\.(png|jpe?g|webp|gif)$/i, '');
}

function AssetPngField({
  value,
  files,
  dir,
  noneLabel,
  ariaLabel,
  onApply,
}: {
  value: unknown;
  files: string[];
  dir: string;
  noneLabel: string;
  ariaLabel: string;
  onApply: (filename: string | undefined) => void;
}) {
  const current = typeof value === 'string' ? value.trim() : '';
  const options = current && !files.includes(current) ? [current, ...files] : files;
  return (
    <div className="admin-icon-picker">
      {current ? (
        <img
          key={current}
          src={`/assets/${dir}/${current}`}
          alt=""
          className="admin-icon-picker__preview"
          onError={(e) => {
            (e.target as HTMLImageElement).style.visibility = 'hidden';
          }}
        />
      ) : (
        <span className="admin-icon-picker__preview admin-icon-picker__preview--empty" aria-hidden />
      )}
      <select
        className="admin-form__input admin-icon-picker__select"
        aria-label={ariaLabel}
        value={current}
        onChange={(e) => onApply(e.target.value || undefined)}
      >
        <option value="">{noneLabel}</option>
        {options.map((f) => (
          <option key={f} value={f}>
            {iconStem(f)}
          </option>
        ))}
      </select>
    </div>
  );
}

function FactionIconField({ value, onApply }: { value: unknown; onApply: (icon: string | undefined) => void }) {
  return (
    <AssetPngField
      value={value}
      files={FACTION_ICON_PNG}
      dir="factions"
      noneLabel="None (uses faction id)"
      ariaLabel="Faction icon"
      onApply={onApply}
    />
  );
}

function UnitIconField({ value, onApply }: { value: unknown; onApply: (icon: string | undefined) => void }) {
  return (
    <AssetPngField
      value={value}
      files={UNIT_ICON_PNG}
      dir="units"
      noneLabel="None (uses unit id)"
      ariaLabel="Unit icon"
      onApply={onApply}
    />
  );
}

function TerritoryImageField({ value, onApply }: { value: unknown; onApply: (image: string | undefined) => void }) {
  return (
    <AssetPngField
      value={value}
      files={TERRITORY_IMAGE_PNG}
      dir="territories"
      noneLabel="None"
      ariaLabel="Territory image"
      onApply={onApply}
    />
  );
}

type EvolvingTerritoryRow = { territory_id: string; step: number; stop_at: number };

function evolvingRowsFromManifest(manifest: Record<string, unknown>): EvolvingTerritoryRow[] {
  const rules = manifest.special_rules;
  if (!Array.isArray(rules)) return [];
  const rows: EvolvingTerritoryRow[] = [];
  for (const rule of rules) {
    if (!rule || typeof rule !== 'object') continue;
    const typ = (rule as { type?: string }).type;
    if (typ !== 'evolving_territory' && typ !== 'fading_territory') continue;
    const territories = (rule as { territories?: unknown }).territories;
    if (!Array.isArray(territories)) continue;
    for (const row of territories) {
      if (!row || typeof row !== 'object') continue;
      const rec = row as Record<string, unknown>;
      const stop = Number(rec.stop_at != null ? rec.stop_at : rec.floor);
      const rawStep = typ === 'fading_territory' ? -Number(rec.fade_per_turn) : Number(rec.step);
      rows.push({
        territory_id: typeof rec.territory_id === 'string' ? rec.territory_id : '',
        step: Number.isFinite(rawStep) ? Math.trunc(rawStep) : 0,
        stop_at: Number.isFinite(stop) ? Math.max(0, Math.trunc(stop)) : 0,
      });
    }
  }
  return rows;
}

function specialRuleMeta(manifest: Record<string, unknown>, type: string): { name: string; is_optional: boolean } {
  const rules = Array.isArray(manifest.special_rules) ? manifest.special_rules : [];
  for (const rule of rules) {
    if (!rule || typeof rule !== 'object' || (rule as { type?: string }).type !== type) continue;
    const rec = rule as { name?: unknown; is_optional?: unknown };
    return {
      name: typeof rec.name === 'string' ? rec.name : '',
      is_optional: rec.is_optional === true,
    };
  }
  return { name: '', is_optional: false };
}

function withRuleMeta(
  rule: Record<string, unknown>,
  meta: { name: string; is_optional: boolean },
): Record<string, unknown> {
  const name = meta.name.trim();
  if (name) rule.name = name;
  rule.is_optional = meta.is_optional;
  return rule;
}

function RuleOptionFields({
  meta,
  onChange,
}: {
  meta: { name: string; is_optional: boolean };
  onChange: (next: { name: string; is_optional: boolean }) => void;
}) {
  return (
    <>
      {fieldRow(
        'Rule name',
        <input
          type="text"
          className="admin-form__input"
          placeholder="Shown as Special Mode"
          value={meta.name}
          onChange={(e) => onChange({ ...meta, name: e.target.value })}
        />,
      )}
      {fieldRow(
        'Optional',
        <input
          type="checkbox"
          checked={meta.is_optional}
          onChange={(e) => onChange({ ...meta, is_optional: e.target.checked })}
        />,
      )}
      <p className="admin-form__micro">
        Optional rules show on the scenario card under the factions, in italics, and create game asks whether they are on. They start on. A name is required when this is checked.
      </p>
    </>
  );
}

function manifestWithEvolvingRows(
  manifest: Record<string, unknown>,
  rows: EvolvingTerritoryRow[],
  meta: { name: string; is_optional: boolean },
): Record<string, unknown> {
  const rules = Array.isArray(manifest.special_rules) ? manifest.special_rules : [];
  const others = rules.filter((rule) => {
    if (!rule || typeof rule !== 'object') return true;
    const typ = (rule as { type?: string }).type;
    return typ !== 'evolving_territory' && typ !== 'fading_territory';
  });
  const nextRules = rows.length
    ? [...others, withRuleMeta({ type: 'evolving_territory', territories: rows }, meta)]
    : others;
  const next = { ...manifest };
  if (nextRules.length) next.special_rules = nextRules;
  else delete next.special_rules;
  return next;
}

function boundWarning(row: EvolvingTerritoryRow, printed: number | undefined): string | null {
  if (!row.territory_id || printed == null || row.step === 0) return null;
  if (row.step > 0 && row.stop_at <= printed) {
    return `Stop at must be above printed power ${printed}.`;
  }
  if (row.step < 0 && row.stop_at >= printed) {
    return `Stop at must be below printed power ${printed}.`;
  }
  return null;
}

function EvolvingTerritoryFields({
  manifest,
  territoryIds,
  territoryPower,
  onManifestChange,
}: {
  manifest: Record<string, unknown>;
  territoryIds: string[];
  territoryPower: Record<string, number>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const rows = evolvingRowsFromManifest(manifest);
  const meta = specialRuleMeta(manifest, 'evolving_territory');
  const used = new Set(rows.map((r) => r.territory_id).filter(Boolean));

  const write = (nextRows: EvolvingTerritoryRow[], nextMeta = meta) => {
    onManifestChange(manifestWithEvolvingRows(manifest, nextRows, nextMeta));
  };

  return (
    <>
      {rows.length > 0 && (
        <RuleOptionFields meta={meta} onChange={(next) => write(rows, next)} />
      )}
      {rows.length > 0 && (
        <div className="admin-form__rule-list">
          {rows.map((row, index) => {
            const options = territoryIds.filter((id) => id === row.territory_id || !used.has(id));
            const warning = boundWarning(row, row.territory_id ? territoryPower[row.territory_id] : undefined);
            return (
              <div key={`${row.territory_id || 'new'}-${index}`} className="admin-form__rule-row">
                <select
                  className="admin-form__input"
                  aria-label="Evolving territory"
                  value={row.territory_id}
                  onChange={(e) => {
                    const next = rows.slice();
                    next[index] = { ...row, territory_id: e.target.value };
                    write(next);
                  }}
                >
                  <option value="">Territory</option>
                  {options.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
                <label className="admin-form__inline-label">
                  Step / turn
                  <input
                    type="number"
                    className="admin-form__input admin-form__input--narrow"
                    aria-label="Step per turn"
                    value={String(row.step)}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      const next = rows.slice();
                      next[index] = { ...row, step: Number.isFinite(n) ? Math.trunc(n) : 0 };
                      write(next);
                    }}
                  />
                </label>
                <label className="admin-form__inline-label">
                  Stop at
                  <input
                    type="number"
                    min={0}
                    className="admin-form__input admin-form__input--narrow"
                    aria-label="Stop at"
                    value={String(row.stop_at)}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      const next = rows.slice();
                      next[index] = { ...row, stop_at: Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0 };
                      write(next);
                    }}
                  />
                </label>
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={() => write(rows.filter((_, i) => i !== index))}
                >
                  Remove
                </button>
                {warning && <p className="admin-form__micro">{warning}</p>}
              </div>
            );
          })}
        </div>
      )}
      <button
        type="button"
        className="admin-page__btn"
        disabled={territoryIds.length === 0 || used.size >= territoryIds.length}
        onClick={() => {
          const territory_id = territoryIds.find((id) => !used.has(id)) ?? '';
          write([...rows, { territory_id, step: -1, stop_at: 0 }]);
        }}
      >
        Add evolving territory
      </button>
      <p className="admin-form__micro">
        Each new turn, power changes by the step until it reaches Stop at. A negative step must stop below the printed power. A positive step must stop above it. Turn 1 still uses the printed power.
      </p>
    </>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? { ...(value as Record<string, unknown>) } : {};
}

function intField(value: unknown, fallback = 0): number {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function ContextFields({
  manifest,
  onManifestChange,
}: {
  manifest: Record<string, unknown>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const context = asRecord(manifest.context);
  const factions = Array.isArray(context.factions)
    ? context.factions.filter((name): name is string => typeof name === 'string')
    : [];

  const write = (patch: Record<string, unknown>) => {
    onManifestChange({ ...manifest, context: { ...context, ...patch } });
  };

  return (
    <>
      {fieldRow(
        'Year',
        <input
          type="text"
          className="admin-form__input"
          value={typeof context.year === 'string' ? context.year : ''}
          onChange={(e) => write({ year: e.target.value })}
        />,
      )}
      {fieldRow(
        'Map',
        <input
          type="text"
          className="admin-form__input"
          value={typeof context.map === 'string' ? context.map : ''}
          onChange={(e) => write({ map: e.target.value })}
        />,
      )}
      {fieldRow(
        'Faction count',
        <input
          type="number"
          min={0}
          className="admin-form__input admin-form__input--narrow"
          value={context.faction_count != null ? String(intField(context.faction_count)) : ''}
          onChange={(e) => {
            if (e.target.value === '') {
              const next = { ...context };
              delete next.faction_count;
              onManifestChange({ ...manifest, context: next });
              return;
            }
            write({ faction_count: Math.max(0, intField(e.target.value)) });
          }}
        />,
      )}
      {fieldRow(
        'Factions',
        <>
          {factions.length > 0 && (
            <div className="admin-form__rule-list">
              {factions.map((name, index) => (
                <div key={index} className="admin-form__rule-row">
                  <input
                    type="text"
                    className="admin-form__input"
                    aria-label="Faction name"
                    value={name}
                    onChange={(e) => {
                      const next = factions.slice();
                      next[index] = e.target.value;
                      write({ factions: next });
                    }}
                  />
                  <button
                    type="button"
                    className="admin-page__btn"
                    onClick={() => write({ factions: factions.filter((_, i) => i !== index) })}
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
          <button type="button" className="admin-page__btn" onClick={() => write({ factions: [...factions, ''] })}>
            Add faction
          </button>
          <p className="admin-form__micro">Names shown for this scenario on the create-game timeline.</p>
        </>,
      )}
    </>
  );
}

function VictoryFields({
  manifest,
  onManifestChange,
}: {
  manifest: Record<string, unknown>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const criteria = asRecord(manifest.victory_criteria);
  const strongholds = asRecord(criteria.strongholds);

  const writeStronghold = (side: 'good' | 'evil', raw: string) => {
    const nextStrongholds = { ...strongholds };
    if (raw === '') delete nextStrongholds[side];
    else nextStrongholds[side] = Math.max(0, intField(raw));
    onManifestChange({
      ...manifest,
      victory_criteria: { ...criteria, strongholds: nextStrongholds },
    });
  };

  return (
    <>
      {fieldRow(
        'Good strongholds',
        <input
          type="number"
          min={0}
          className="admin-form__input admin-form__input--narrow"
          value={strongholds.good != null ? String(intField(strongholds.good)) : ''}
          onChange={(e) => writeStronghold('good', e.target.value)}
        />,
      )}
      {fieldRow(
        'Evil strongholds',
        <input
          type="number"
          min={0}
          className="admin-form__input admin-form__input--narrow"
          value={strongholds.evil != null ? String(intField(strongholds.evil)) : ''}
          onChange={(e) => writeStronghold('evil', e.target.value)}
        />,
      )}
      <p className="admin-form__micro">Strongholds an alliance must hold at the end of a full turn cycle to win.</p>
    </>
  );
}

type SubfactionRuleRow = {
  id: string;
  economy: string;
  mobilization: string;
  capture: string;
  movement: string;
  purchasable_by_parent: boolean;
  recruitMode: '' | 'fixed' | 'per_territory';
  recruitUnitId: string;
  recruitCount: number;
};

function subfactionRowsFromManifest(manifest: Record<string, unknown>): SubfactionRuleRow[] {
  const raw = asRecord(manifest.subfaction_rules);
  return Object.entries(raw).map(([id, rule]) => {
    const rec = asRecord(rule);
    const recruitment = asRecord(rec.recruitment);
    const mode = recruitment.mode === 'fixed' || recruitment.mode === 'per_territory' ? recruitment.mode : '';
    return {
      id,
      economy: rec.economy === 'pool' ? 'pool' : 'none',
      mobilization: rec.mobilization === 'any_home' ? 'any_home' : 'camps',
      capture: rec.capture === 'unit_faction' ? 'unit_faction' : 'liberate_else_parent',
      movement: rec.movement === 'home_only' ? 'home_only' : 'with_parent',
      purchasable_by_parent: rec.purchasable_by_parent === true,
      recruitMode: mode,
      recruitUnitId: typeof recruitment.unit_id === 'string' ? recruitment.unit_id : '',
      recruitCount: Math.max(1, intField(recruitment.count, 1)),
    };
  });
}

function manifestWithSubfactionRows(
  manifest: Record<string, unknown>,
  rows: SubfactionRuleRow[],
): Record<string, unknown> {
  const next = { ...manifest };
  const rules: Record<string, unknown> = {};
  for (const row of rows) {
    const id = row.id.trim();
    if (!id) continue;
    const rule: Record<string, unknown> = {
      economy: row.economy === 'pool' ? 'pool' : 'none',
      mobilization: row.mobilization === 'any_home' ? 'any_home' : 'camps',
      capture: row.capture === 'unit_faction' ? 'unit_faction' : 'liberate_else_parent',
      movement: row.movement === 'home_only' ? 'home_only' : 'with_parent',
      purchasable_by_parent: row.purchasable_by_parent,
    };
    if (row.recruitMode) {
      rule.recruitment = {
        mode: row.recruitMode,
        unit_id: row.recruitUnitId.trim(),
        count: Math.max(1, row.recruitCount),
      };
    }
    rules[id] = rule;
  }
  if (Object.keys(rules).length) next.subfaction_rules = rules;
  else delete next.subfaction_rules;
  return next;
}

function SubfactionRulesFields({
  manifest,
  onManifestChange,
}: {
  manifest: Record<string, unknown>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const [rows, setRows] = useState<SubfactionRuleRow[]>(() => subfactionRowsFromManifest(manifest));

  const write = (nextRows: SubfactionRuleRow[]) => {
    setRows(nextRows);
    onManifestChange(manifestWithSubfactionRows(manifest, nextRows));
  };

  const patch = (index: number, partial: Partial<SubfactionRuleRow>) => {
    write(rows.map((row, i) => (i === index ? { ...row, ...partial } : row)));
  };

  return (
    <>
      {rows.length > 0 && (
        <div className="admin-form__rule-list">
          {rows.map((row, index) => (
            <div key={index} className="admin-form__card">
              {fieldRow(
                'Subfaction id',
                <input
                  type="text"
                  className="admin-form__input"
                  value={row.id}
                  onChange={(e) => patch(index, { id: e.target.value.trim() })}
                />,
              )}
              {fieldRow(
                'Economy',
                <select className="admin-form__input" value={row.economy} onChange={(e) => patch(index, { economy: e.target.value })}>
                  <option value="none">None</option>
                  <option value="pool">Pool with parent</option>
                </select>,
              )}
              {fieldRow(
                'Mobilization',
                <select className="admin-form__input" value={row.mobilization} onChange={(e) => patch(index, { mobilization: e.target.value })}>
                  <option value="camps">Camps and ports</option>
                  <option value="any_home">Subfaction land only</option>
                </select>,
              )}
              {fieldRow(
                'Capture',
                <select className="admin-form__input" value={row.capture} onChange={(e) => patch(index, { capture: e.target.value })}>
                  <option value="liberate_else_parent">Liberate, else parent</option>
                  <option value="unit_faction">Subfaction keeps its own captures</option>
                </select>,
              )}
              {fieldRow(
                'Movement',
                <select className="admin-form__input" value={row.movement} onChange={(e) => patch(index, { movement: e.target.value })}>
                  <option value="with_parent">With parent</option>
                  <option value="home_only">Home territories only</option>
                </select>,
              )}
              {fieldRow(
                'Parent can purchase',
                <input
                  type="checkbox"
                  checked={row.purchasable_by_parent}
                  onChange={(e) => patch(index, { purchasable_by_parent: e.target.checked })}
                />,
              )}
              {fieldRow(
                'Recruitment',
                <select
                  className="admin-form__input"
                  value={row.recruitMode}
                  onChange={(e) => {
                    const mode = e.target.value;
                    patch(index, { recruitMode: mode === 'fixed' || mode === 'per_territory' ? mode : '' });
                  }}
                >
                  <option value="">None</option>
                  <option value="fixed">Fixed</option>
                  <option value="per_territory">Per territory</option>
                </select>,
              )}
              {row.recruitMode && (
                <>
                  {fieldRow(
                    'Recruit unit id',
                    <input
                      type="text"
                      className="admin-form__input"
                      value={row.recruitUnitId}
                      onChange={(e) => patch(index, { recruitUnitId: e.target.value })}
                    />,
                  )}
                  {fieldRow(
                    'Recruit count',
                    <input
                      type="number"
                      min={1}
                      className="admin-form__input admin-form__input--narrow"
                      value={String(row.recruitCount)}
                      onChange={(e) => patch(index, { recruitCount: Math.max(1, intField(e.target.value, 1)) })}
                    />,
                  )}
                </>
              )}
              <button type="button" className="admin-page__btn" onClick={() => write(rows.filter((_, i) => i !== index))}>
                Remove subfaction
              </button>
            </div>
          ))}
        </div>
      )}
      <button
        type="button"
        className="admin-page__btn"
        onClick={() => write([...rows, {
          id: '',
          economy: 'none',
          mobilization: 'camps',
          capture: 'liberate_else_parent',
          movement: 'with_parent',
          purchasable_by_parent: false,
          recruitMode: '',
          recruitUnitId: '',
          recruitCount: 1,
        }])}
      >
        Add subfaction
      </button>
      <p className="admin-form__micro">
        The id must match a subfaction on its parent faction. A lost parent capital stops pooled production, recruitment, and subfaction mobilization.
      </p>
    </>
  );
}

type RingRow = {
  id: string;
  name: string;
  territory_id: string;
  power: number;
  bearer_hero_id: string;
  returns_to: string;
  attack_boost: string;
  defense_boost: string;
  rolls_boost: string;
  hp_boost: string;
  moves_boost: string;
};

function ringsFromManifest(manifest: Record<string, unknown>): RingRow[] {
  const rules = Array.isArray(manifest.special_rules) ? manifest.special_rules : [];
  const rows: RingRow[] = [];
  for (const rule of rules) {
    if (!rule || typeof rule !== 'object' || (rule as { type?: string }).type !== 'rings_of_power') continue;
    const rings = (rule as { rings?: unknown }).rings;
    if (!Array.isArray(rings)) continue;
    for (const ring of rings) {
      if (!ring || typeof ring !== 'object') continue;
      const rec = ring as Record<string, unknown>;
      const boost = (key: string) => (rec[key] == null || rec[key] === '' ? '' : String(intField(rec[key])));
      rows.push({
        id: typeof rec.id === 'string' ? rec.id : '',
        name: typeof rec.name === 'string' ? rec.name : '',
        territory_id: typeof rec.territory_id === 'string' ? rec.territory_id : '',
        power: Math.max(0, intField(rec.power)),
        bearer_hero_id: typeof rec.bearer_hero_id === 'string' ? rec.bearer_hero_id : '',
        returns_to: typeof rec.returns_to === 'string' ? rec.returns_to : '',
        attack_boost: boost('attack_boost'),
        defense_boost: boost('defense_boost'),
        rolls_boost: boost('rolls_boost'),
        hp_boost: boost('hp_boost'),
        moves_boost: boost('moves_boost'),
      });
    }
  }
  return rows;
}

function manifestWithRings(
  manifest: Record<string, unknown>,
  rows: RingRow[],
  meta: { name: string; is_optional: boolean },
): Record<string, unknown> {
  const rules = Array.isArray(manifest.special_rules) ? manifest.special_rules : [];
  const others = rules.filter(
    (rule) => !rule || typeof rule !== 'object' || (rule as { type?: string }).type !== 'rings_of_power',
  );
  const rings = rows.map((row) => {
    const out: Record<string, unknown> = {
      id: row.id.trim(),
      name: row.name,
      territory_id: row.territory_id,
      power: Math.max(0, row.power),
    };
    const bearer = row.bearer_hero_id.trim();
    const home = row.returns_to.trim();
    if (bearer) out.bearer_hero_id = bearer;
    if (home) out.returns_to = home;
    for (const key of ['attack_boost', 'defense_boost', 'rolls_boost', 'hp_boost', 'moves_boost'] as const) {
      if (row[key] === '') continue;
      out[key] = Math.max(0, intField(row[key]));
    }
    return out;
  });
  const nextRules = rings.length
    ? [...others, withRuleMeta({ type: 'rings_of_power', rings }, meta)]
    : others;
  const next = { ...manifest };
  if (nextRules.length) next.special_rules = nextRules;
  else delete next.special_rules;
  return next;
}

function RingFields({
  manifest,
  territoryIds,
  onManifestChange,
}: {
  manifest: Record<string, unknown>;
  territoryIds: string[];
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
  const rows = ringsFromManifest(manifest);
  const meta = specialRuleMeta(manifest, 'rings_of_power');
  const write = (nextRows: RingRow[], nextMeta = meta) => onManifestChange(manifestWithRings(manifest, nextRows, nextMeta));
  const patch = (index: number, partial: Partial<RingRow>) => {
    write(rows.map((row, i) => (i === index ? { ...row, ...partial } : row)));
  };
  const boost = (index: number, key: keyof RingRow, raw: string) => {
    if (raw === '') {
      patch(index, { [key]: '' });
      return;
    }
    patch(index, { [key]: String(Math.max(0, intField(raw))) });
  };

  return (
    <>
      {rows.length > 0 && (
        <RuleOptionFields meta={meta} onChange={(next) => write(rows, next)} />
      )}
      {rows.length > 0 && (
        <div className="admin-form__rule-list">
          {rows.map((row, index) => (
            <div key={index} className="admin-form__card">
              {fieldRow(
                'Ring id',
                <input type="text" className="admin-form__input" value={row.id} onChange={(e) => patch(index, { id: e.target.value })} />,
              )}
              {fieldRow(
                'Name',
                <input type="text" className="admin-form__input" value={row.name} onChange={(e) => patch(index, { name: e.target.value })} />,
              )}
              {fieldRow(
                'Territory',
                <select className="admin-form__input" value={row.territory_id} onChange={(e) => patch(index, { territory_id: e.target.value })}>
                  <option value="">Territory</option>
                  {territoryIds.map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>,
              )}
              {fieldRow(
                'Power',
                <input
                  type="number"
                  min={0}
                  className="admin-form__input admin-form__input--narrow"
                  value={String(row.power)}
                  onChange={(e) => patch(index, { power: Math.max(0, intField(e.target.value)) })}
                />,
              )}
              {fieldRow(
                'Bearer hero id',
                <input
                  type="text"
                  className="admin-form__input"
                  placeholder="Optional"
                  value={row.bearer_hero_id}
                  onChange={(e) => patch(index, { bearer_hero_id: e.target.value })}
                />,
              )}
              {fieldRow(
                'Returns to',
                <select className="admin-form__input" value={row.returns_to} onChange={(e) => patch(index, { returns_to: e.target.value })}>
                  <option value="">None</option>
                  {territoryIds.map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>,
              )}
              <div className="admin-form__rule-row">
                {([
                  ['attack_boost', 'Attack'],
                  ['defense_boost', 'Defense'],
                  ['rolls_boost', 'Rolls'],
                  ['hp_boost', 'HP'],
                  ['moves_boost', 'Moves'],
                ] as const).map(([key, label]) => (
                  <label key={key} className="admin-form__inline-label">
                    {label}
                    <input
                      type="number"
                      min={0}
                      className="admin-form__input admin-form__input--narrow"
                      aria-label={`${label} boost`}
                      value={row[key]}
                      placeholder="0"
                      onChange={(e) => boost(index, key, e.target.value)}
                    />
                  </label>
                ))}
              </div>
              <button type="button" className="admin-page__btn" onClick={() => write(rows.filter((_, i) => i !== index))}>
                Remove ring
              </button>
            </div>
          ))}
        </div>
      )}
      <button
        type="button"
        className="admin-page__btn"
        onClick={() => write([...rows, {
          id: '',
          name: '',
          territory_id: territoryIds[0] ?? '',
          power: 0,
          bearer_hero_id: '',
          returns_to: '',
          attack_boost: '',
          defense_boost: '',
          rolls_boost: '',
          hp_boost: '',
          moves_boost: '',
        }])}
      >
        Add ring
      </button>
      <p className="admin-form__micro">
        A bearer hero id sends that ring home when that hero is destroyed in its territory, and its power follows that hero&apos;s faction while they stand together. Boosts are optional.
      </p>
    </>
  );
}

export function ManifestPanel({
  setupId,
  manifest,
  territoryIds,
  territoryPower,
  onManifestChange,
}: {
  setupId: string;
  manifest: Record<string, unknown>;
  territoryIds: string[];
  territoryPower: Record<string, number>;
  onManifestChange: (next: Record<string, unknown>) => void;
}) {
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
        'Timeline image',
        <>
          <AssetPngField
            value={manifest.timeline_image}
            files={SCENARIO_IMAGE}
            dir="scenarios"
            noneLabel="None"
            ariaLabel="Timeline image"
            onApply={(image) => {
              if (!image) {
                const next = { ...manifest };
                delete next.timeline_image;
                onManifestChange(next);
                return;
              }
              onManifestChange({ ...manifest, timeline_image: image });
            }}
          />
          <p className="admin-form__micro">
            Image file in public/assets/scenarios. Scenarios that share this image and the same year appear as one point on the create-game timeline, with each scenario listed underneath. Scenarios with no image still share a point when their year matches.
          </p>
        </>,
      )}
      {fieldRow(
        'Lobby music',
        <MusicField
          value={manifest.lobby_music}
          files={MUSIC_M4A}
          emptyLabel="None"
          addAriaLabel="Add lobby music track"
          onApply={(m) => onManifestChange({ ...manifest, lobby_music: m ?? [] })}
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
        'Create-game menu order',
        <>
          <input
            type="number"
            className="admin-form__input admin-form__input--narrow"
            placeholder="default 1000"
            value={manifest.menu_order != null && manifest.menu_order !== '' ? String(manifest.menu_order) : ''}
            onChange={(e) => {
              const v = e.target.value.trim();
              if (v === '') {
                const next = { ...manifest };
                delete next.menu_order;
                onManifestChange(next);
                return;
              }
              const n = Number(v);
              onManifestChange({
                ...manifest,
                menu_order: Number.isFinite(n) ? Math.trunc(n) : manifest.menu_order,
              });
            }}
          />
          <p className="admin-form__micro">Lower numbers appear first in the scenario list. Omit for default (1000).</p>
        </>,
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
        'Starting message',
        <>
          <textarea
            className="admin-form__textarea"
            rows={4}
            placeholder="Optional. Shown once in the middle of the map when the match starts."
            value={typeof manifest.starting_message === 'string' ? manifest.starting_message : ''}
            onChange={(e) => {
              const value = e.target.value;
              if (!value) {
                const next = { ...manifest };
                delete next.starting_message;
                onManifestChange(next);
                return;
              }
              onManifestChange({ ...manifest, starting_message: value });
            }}
          />
          <p className="admin-form__micro">
            Each player sees this when the lobby becomes a match, and only then. Closing it hides it for that player.
          </p>
        </>,
      )}
      <h3 className="admin-form__subtitle">Context</h3>
      <ContextFields manifest={manifest} onManifestChange={onManifestChange} />
      <h3 className="admin-form__subtitle">Victory criteria</h3>
      <VictoryFields manifest={manifest} onManifestChange={onManifestChange} />
      <h3 className="admin-form__subtitle">Subfactions</h3>
      <SubfactionRulesFields key={setupId} manifest={manifest} onManifestChange={onManifestChange} />
      <h3 className="admin-form__subtitle">Special rules</h3>
      {fieldRow(
        'Rings of Power',
        <RingFields manifest={manifest} territoryIds={territoryIds} onManifestChange={onManifestChange} />,
      )}
      {fieldRow(
        'Evolving territories',
        <EvolvingTerritoryFields
          manifest={manifest}
          territoryIds={territoryIds}
          territoryPower={territoryPower}
          onManifestChange={onManifestChange}
        />,
      )}
    </div>
  );
}

function EntityDictPanel({
  title,
  data,
  onChange,
  renderEditor,
  onAdd,
}: {
  title: string;
  data: Record<string, Record<string, unknown>>;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
  renderEditor: (id: string, obj: Record<string, unknown>, patch: (p: Record<string, unknown>) => void) => React.ReactNode;
  onAdd?: (select: (id: string) => void) => void;
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
    if (onAdd) {
      onAdd(setSelected);
      return;
    }
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

function unitMap(raw: unknown): Record<string, Record<string, unknown>> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out: Record<string, Record<string, unknown>> = {};
  for (const [id, value] of Object.entries(raw)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) out[id] = value as Record<string, unknown>;
  }
  return out;
}

function AddUnitDialog({
  open,
  units,
  setupId,
  setups,
  onClose,
  onCreate,
}: {
  open: boolean;
  units: Record<string, Record<string, unknown>>;
  setupId: string;
  setups: AdminSetupListItem[];
  onClose: () => void;
  onCreate: (id: string, def: Record<string, unknown>) => void;
}) {
  const [mode, setMode] = useState<'empty' | 'copy'>('empty');
  const [newId, setNewId] = useState('');
  const [sourceSetup, setSourceSetup] = useState('');
  const [sourceUnitId, setSourceUnitId] = useState('');
  const [sourceUnits, setSourceUnits] = useState<Record<string, Record<string, unknown>>>({});
  const [loadingUnits, setLoadingUnits] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setMode('empty');
    setNewId('');
    setSourceSetup('');
    setSourceUnitId('');
    setSourceUnits({});
    setLoadingUnits(false);
    setErr(null);
  }, [open]);

  useEffect(() => {
    if (!open || mode !== 'copy') return;
    if (!sourceSetup) {
      setSourceUnits({});
      setLoadingUnits(false);
      return;
    }
    if (sourceSetup === setupId) {
      setSourceUnits(units);
      setLoadingUnits(false);
      return;
    }
    let cancelled = false;
    setLoadingUnits(true);
    setErr(null);
    api
      .adminGetSetup(sourceSetup)
      .then((bundle) => {
        if (cancelled) return;
        setSourceUnits(unitMap(bundle.units));
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setSourceUnits({});
        setErr(e instanceof Error ? e.message : 'Could not load that setup');
      })
      .finally(() => {
        if (!cancelled) setLoadingUnits(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, mode, sourceSetup, setupId, units]);

  if (!open) return null;

  const sourceUnitIds = Object.keys(sourceUnits).sort((a, b) => a.localeCompare(b));

  const submit = () => {
    const id = newId.trim();
    if (!id) {
      setErr('Enter a unit id.');
      return;
    }
    if (units[id]) {
      setErr('That id already exists.');
      return;
    }
    if (mode === 'copy') {
      const src = sourceUnits[sourceUnitId];
      if (!sourceSetup || !src) {
        setErr('Choose a setup and a unit to copy.');
        return;
      }
      const copy = JSON.parse(JSON.stringify(src)) as Record<string, unknown>;
      copy.id = id;
      onCreate(id, copy);
      return;
    }
    onCreate(id, { id, cost: { power: 0 } });
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="admin-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-add-unit-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="admin-add-unit-title" className="admin-modal__title">
          Add unit
        </h2>
        <div className="admin-form__row admin-form__row--radio-row">
          <span className="admin-form__label">Source</span>
          <div className="admin-form__radio-group admin-form__radio-group--create-setup">
            <label className="admin-form__radio-label">
              <input type="radio" name="admin-add-unit-mode" checked={mode === 'empty'} onChange={() => setMode('empty')} />
              Empty
            </label>
            <label
              className={`admin-form__radio-label${setups.length === 0 ? ' admin-form__radio-label--disabled' : ''}`}
              title={setups.length === 0 ? 'No setups to copy yet' : undefined}
            >
              <input
                type="radio"
                name="admin-add-unit-mode"
                checked={mode === 'copy'}
                disabled={setups.length === 0}
                onChange={() => setMode('copy')}
              />
              Copy
            </label>
          </div>
        </div>
        {mode === 'copy' ? (
          <>
            <div className="admin-form__row">
              <label className="admin-form__label" htmlFor="admin-copy-unit-setup">
                Copy from
              </label>
              <select
                id="admin-copy-unit-setup"
                className="admin-page__select admin-page__select--full"
                value={sourceSetup}
                onChange={(e) => {
                  setSourceSetup(e.target.value);
                  setSourceUnitId('');
                  setNewId('');
                }}
              >
                <option value="">Select a setup…</option>
                {setups.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.display_name} ({s.id}){s.id === setupId ? ' — this setup' : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="admin-form__row">
              <label className="admin-form__label" htmlFor="admin-copy-unit-id">
                Unit
              </label>
              <select
                id="admin-copy-unit-id"
                className="admin-page__select admin-page__select--full"
                value={sourceUnitId}
                disabled={!sourceSetup || loadingUnits}
                onChange={(e) => {
                  const id = e.target.value;
                  setSourceUnitId(id);
                  setNewId(id);
                }}
              >
                <option value="">{loadingUnits ? 'Loading units…' : 'Select a unit…'}</option>
                {sourceUnitIds.map((id) => {
                  const name = sourceUnits[id]?.display_name;
                  const label = typeof name === 'string' && name.trim() && name !== id ? `${name} (${id})` : id;
                  return (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  );
                })}
              </select>
            </div>
          </>
        ) : null}
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-new-unit-id">
            Unit id
          </label>
          <input
            id="admin-new-unit-id"
            className="admin-form__input"
            autoComplete="off"
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            placeholder="e.g. gondor_infantry"
          />
        </div>
        {err ? <div className="admin-page__error">{err}</div> : null}
        <div className="admin-modal__actions">
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={submit}>
            Add
          </button>
        </div>
      </div>
    </div>
  );
}

export function UnitsPanel({
  units,
  factionIds,
  setupId,
  setups,
  onChange,
}: {
  units: Record<string, Record<string, unknown>>;
  factionIds: string[];
  setupId: string;
  setups: AdminSetupListItem[];
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}) {
  const knownHeroIds = useMemo(() => {
    const ids = new Set<string>();
    for (const u of Object.values(units)) {
      const hid = typeof u.hero_id === 'string' ? u.hero_id.trim() : '';
      if (hid) ids.add(hid);
    }
    return [...ids].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  }, [units]);

  const [addOpen, setAddOpen] = useState(false);
  const selectRef = useRef<(id: string) => void>(() => {});

  return (
    <>
    <EntityDictPanel
      title="unit"
      data={units}
      onChange={onChange}
      onAdd={(select) => {
        selectRef.current = select;
        setAddOpen(true);
      }}
      renderEditor={(id, u, patch) => (
        <div className="admin-form">
          {fieldRow('Unit id', <input type="text" className="admin-form__input admin-form__input--readonly" readOnly disabled value={id} />)}
          {fieldRow(
            'Display name',
            <input type="text" className="admin-form__input" value={String(u.display_name ?? '')} onChange={(e) => patch({ display_name: e.target.value })} />,
          )}
          {fieldRow(
            'Faction',
            <select
              className="admin-form__input"
              value={String(u.faction ?? '')}
              onChange={(e) => patch({ faction: e.target.value })}
            >
              <option value="">Select faction</option>
              {factionIds.map((fid) => (
                <option key={fid} value={fid}>{fid}</option>
              ))}
              {typeof u.faction === 'string' && u.faction && !factionIds.includes(u.faction) ? (
                <option value={u.faction}>{u.faction}</option>
              ) : null}
            </select>,
          )}
          {fieldRow(
            'Hero id',
            <HeroIdField
              value={u.hero_id}
              knownHeroIds={knownHeroIds}
              onApply={(hero_id) => patch({ hero_id })}
            />,
          )}
          {fieldRow(
            'Archetype',
            <input type="text" className="admin-form__input" value={String(u.archetype ?? '')} onChange={(e) => patch({ archetype: e.target.value })} />,
          )}
          {fieldRow(
            'Tags (comma-separated)',
            <CommaListField key={`${id}:tags`} value={u.tags} onApply={(tags) => patch({ tags })} />,
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
            <input
              type="number"
              min={1}
              className="admin-form__input admin-form__input--narrow"
              value={u.dice != null ? String(u.dice) : ''}
              onChange={(e) => {
                const raw = e.target.value;
                patch({ dice: raw === '' ? undefined : Number(raw) });
              }}
              onBlur={() => {
                const current = Number(u.dice);
                if (!Number.isFinite(current) || current < 1) patch({ dice: 1 });
              }}
            />,
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
            'Icon',
            <UnitIconField value={u.icon} onApply={(icon) => patch({ icon })} />,
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
            <CommaListField key={`${id}:specials`} value={u.specials} onApply={(specials) => patch({ specials })} />,
          )}
          {fieldRow(
            'Home territory ids (comma-separated)',
            <CommaListField
              key={`${id}:home`}
              value={
                Array.isArray(u.home_territory_ids)
                  ? u.home_territory_ids
                  : typeof u.home_territory_id === 'string'
                    ? u.home_territory_id
                    : []
              }
              onApply={(ids) =>
                patch({ home_territory_ids: ids.length ? ids : undefined, home_territory_id: undefined })
              }
            />,
          )}
        </div>
      )}
    />
    <AddUnitDialog
      open={addOpen}
      units={units}
      setupId={setupId}
      setups={setups}
      onClose={() => setAddOpen(false)}
      onCreate={(id, def) => {
        onChange({ ...units, [id]: def });
        selectRef.current(id);
        setAddOpen(false);
      }}
    />
    </>
  );
}

function powerFromProduces(produces: unknown): number {
  if (!produces || typeof produces !== 'object' || Array.isArray(produces)) return 0;
  const raw = (produces as Record<string, unknown>).power;
  const n = typeof raw === 'number' ? raw : Number(raw);
  return Number.isFinite(n) ? n : 0;
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
            'Image',
            <TerritoryImageField value={t.image} onApply={(image) => patch({ image })} />,
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
            'Power production',
            <input
              type="number"
              min={0}
              step={1}
              className="admin-form__input admin-form__input--narrow"
              value={String(powerFromProduces(t.produces))}
              onChange={(e) => {
                const n = Number(e.target.value);
                const power = Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0;
                const prev =
                  t.produces && typeof t.produces === 'object' && !Array.isArray(t.produces)
                    ? (t.produces as Record<string, unknown>)
                    : {};
                patch({ produces: { ...prev, power } });
              }}
            />,
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

function parseHexColor(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const s = value.trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return `#${s.slice(1).toLowerCase()}`;
  return null;
}

function FactionColorField({ value, onApply }: { value: unknown; onApply: (c: string) => void }) {
  const raw = typeof value === 'string' ? value : '';
  const hex = parseHexColor(value);
  return (
    <div className="admin-color-picker">
      <input
        type="color"
        className="admin-color-picker__swatch"
        value={hex ?? '#888888'}
        onChange={(e) => onApply(e.target.value.toLowerCase())}
        aria-label="Faction color palette"
      />
      <input
        type="text"
        className="admin-form__input admin-color-picker__hex"
        spellCheck={false}
        value={raw}
        onChange={(e) => onApply(e.target.value)}
      />
    </div>
  );
}

function SubfactionsField({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (next: Record<string, unknown>[]) => void;
}) {
  const rows: Record<string, unknown>[] = Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object' && !Array.isArray(row))
    : [];

  const patchRow = (index: number, patch: Record<string, unknown>) => {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  return (
    <div className="admin-subfactions">
      <h3 className="admin-form__subtitle">Subfactions</h3>
      <p className="admin-form__micro">
        Controlled on this faction&apos;s turn. They do not appear in the turn order. Leave the icon empty to use this faction&apos;s icon.
      </p>
      {rows.map((row, i) => (
        <div key={i} className="admin-subfaction">
          {fieldRow(
            'Subfaction id',
            <input
              type="text"
              className="admin-form__input"
              value={String(row.id ?? '')}
              onChange={(e) => patchRow(i, { id: e.target.value.trim() })}
            />,
          )}
          {fieldRow(
            'Display name',
            <input
              type="text"
              className="admin-form__input"
              value={String(row.display_name ?? '')}
              onChange={(e) => patchRow(i, { display_name: e.target.value })}
            />,
          )}
          {fieldRow(
            'Color',
            <FactionColorField value={row.color} onApply={(color) => patchRow(i, { color })} />,
          )}
          {fieldRow(
            'Icon filename',
            <AssetPngField
              value={row.icon}
              files={FACTION_ICON_PNG}
              dir="factions"
              noneLabel="None (uses faction icon)"
              ariaLabel="Subfaction icon"
              onApply={(icon) => patchRow(i, { icon })}
            />,
          )}
          <button type="button" className="admin-page__btn" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            Remove subfaction
          </button>
        </div>
      ))}
      <button
        type="button"
        className="admin-page__btn"
        onClick={() => onChange([...rows, { id: '', display_name: '', color: '#888888' }])}
      >
        Add subfaction
      </button>
    </div>
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
            <FactionColorField value={f.color} onApply={(color) => patch({ color })} />,
          )}
          {fieldRow(
            'Icon',
            <FactionIconField value={f.icon} onApply={(icon) => patch({ icon })} />,
          )}
          {fieldRow(
            'Music',
            <MusicField
              value={f.music}
              files={MUSIC_M4A}
              emptyLabel="None (uses faction id)"
              addAriaLabel="Add turn music track"
              onApply={(m) => patch({ music: m })}
            />,
          )}
          <SubfactionsField
            value={f.subfactions}
            onChange={(subfactions) => patch({ subfactions })}
          />
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
      <p className="admin-form__micro">Faction id or subfaction id. A subfaction colors the territory with its own color.</p>
      {territoryIds.map((tid) => (
        <div key={tid} className="admin-form__row">
          <label className="admin-form__label">{tid}</label>
          <input
            type="text"
            className="admin-form__input"
            placeholder="faction or subfaction id"
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
            value={st.count <= 0 ? '' : st.count}
            onChange={(e) => {
              const raw = e.target.value;
              updateStack(i, { count: raw === '' ? 0 : Number(raw) });
            }}
            onBlur={() => {
              if (!Number.isFinite(st.count) || st.count < 1) updateStack(i, { count: 1 });
            }}
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
