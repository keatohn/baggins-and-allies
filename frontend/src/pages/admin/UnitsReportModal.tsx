import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { AdminSetupBundle, AdminSetupListItem, CatalogSpecial } from '../../services/api';
import { previewStatsFromBundle } from './previewStats';

type Props = {
  setups: AdminSetupListItem[];
  specials: CatalogSpecial[];
  onClose: () => void;
  /** Called after stat edits land so an open setup editor keeps the new values. */
  onSaved?: (setupId: string, units: Record<string, unknown>) => void;
};

type StatKey = 'cost' | 'attack' | 'defense' | 'dice' | 'movement' | 'health';

const STATS: { key: StatKey; label: string; title: string }[] = [
  { key: 'cost', label: 'P', title: 'Power cost' },
  { key: 'attack', label: 'A', title: 'Attack' },
  { key: 'defense', label: 'D', title: 'Defense' },
  { key: 'dice', label: 'R', title: 'Dice rolls' },
  { key: 'movement', label: 'M', title: 'Moves' },
  { key: 'health', label: 'HP', title: 'Hit points' },
];

type Range = { min: string; max: string };

const EMPTY_RANGES: Record<StatKey, Range> = {
  cost: { min: '', max: '' },
  attack: { min: '', max: '' },
  defense: { min: '', max: '' },
  dice: { min: '', max: '' },
  movement: { min: '', max: '' },
  health: { min: '', max: '' },
};

type UnitSource = {
  icon: string;
  setupId: string;
  scenario: string;
  unitId: string;
  cost: number;
  purchasable: boolean;
  attack: number;
  defense: number;
  dice: number;
  movement: number;
  health: number;
  specials: string;
  specialKeys: string[];
};

type UnitRow = UnitSource & {
  iconSpan: number;
  scenarioSpan: number;
  scenarioBand: boolean;
  iconStart: boolean;
  iconEnd: boolean;
  scenarioEnd: boolean;
  /** The scenario cell closes an icon group, so its rule matches the thicker icon line. */
  scenarioClosesIcon: boolean;
};

/** The unique most common value. A tie for that count is not an outright mode. */
function outrightMode(values: number[]): number | null {
  const counts = new Map<number, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  let bestValue = 0;
  let bestCount = 0;
  let tied = false;
  for (const [value, count] of counts) {
    if (count > bestCount) {
      bestValue = value;
      bestCount = count;
      tied = false;
    } else if (count === bestCount) {
      tied = true;
    }
  }
  if (tied || bestCount < 1) return null;
  return bestValue;
}

function isListedScenario(setup: AdminSetupListItem): boolean {
  return setup.is_active === true || setup.is_active === 'admin';
}

function specialKey(label: string): string {
  if (label === 'Home' || label.startsWith('Home (')) return 'Home';
  return label;
}

/** Filename of a unit image, including the extension (`galadhrim_sentinel.png`). */
function iconFileName(icon: string): string {
  return (icon.split('/').pop() ?? icon).toLowerCase();
}

function bound(raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function inRange(value: number | null, range: Range): boolean {
  const lo = bound(range.min);
  const hi = bound(range.max);
  if (lo == null && hi == null) return true;
  if (value == null) return false;
  if (lo != null && value < lo) return false;
  if (hi != null && value > hi) return false;
  return true;
}

function withSpans(rows: UnitSource[]): UnitRow[] {
  let scenarioBand = false;
  return rows.map((row, index) => {
    const iconStart = index === 0 || rows[index - 1].icon !== row.icon;
    let iconSpan = 0;
    if (iconStart) {
      iconSpan = 1;
      while (index + iconSpan < rows.length && rows[index + iconSpan].icon === row.icon) iconSpan += 1;
    }
    const scenarioStart = iconStart || rows[index - 1].setupId !== row.setupId;
    let scenarioSpan = 0;
    if (scenarioStart) {
      scenarioBand = !scenarioBand;
      scenarioSpan = 1;
      while (
        index + scenarioSpan < rows.length
        && rows[index + scenarioSpan].icon === row.icon
        && rows[index + scenarioSpan].setupId === row.setupId
      ) {
        scenarioSpan += 1;
      }
    }
    const iconEnd = index + 1 === rows.length || rows[index + 1].icon !== row.icon;
    const scenarioEnd = iconEnd || rows[index + 1].setupId !== row.setupId;
    const scenarioClosesIcon = scenarioStart && (index + scenarioSpan === rows.length || rows[index + scenarioSpan].icon !== row.icon);
    return { ...row, iconSpan, scenarioSpan, scenarioBand, iconStart, iconEnd, scenarioEnd, scenarioClosesIcon };
  });
}

type LoadedSetup = { setup: AdminSetupListItem; bundle: AdminSetupBundle };

function flattenUnits(loaded: LoadedSetup[], specials: CatalogSpecial[]): UnitSource[] {
  const flat: UnitSource[] = [];
  for (const { setup, bundle } of loaded) {
    const preview = previewStatsFromBundle(bundle, { specials });
    if (!preview) continue;
    const scenario = setup.display_name?.trim() || setup.id;
    for (const units of Object.values(preview.unitsByFaction)) {
      for (const unit of units) {
        const keys = [...new Set(unit.specials.map(specialKey))];
        flat.push({
          icon: unit.icon,
          setupId: setup.id,
          scenario,
          unitId: unit.id,
          cost: unit.cost,
          purchasable: unit.purchasable,
          attack: unit.attack,
          defense: unit.defense,
          dice: unit.dice,
          movement: unit.movement,
          health: unit.health,
          specials: unit.specials.length ? unit.specials.join(', ') : '',
          specialKeys: keys,
        });
      }
    }
  }
  flat.sort((a, b) => (
    a.icon.localeCompare(b.icon)
    || a.scenario.localeCompare(b.scenario)
    || a.setupId.localeCompare(b.setupId)
    || a.unitId.localeCompare(b.unitId)
  ));
  return flat;
}

/** Pending edits are tracked per unit within a scenario. */
function pendingKey(row: UnitSource): string {
  return `${row.setupId}|${row.unitId}`;
}

function statMin(key: StatKey): number {
  return key === 'dice' ? 1 : 0;
}

/** Write one stat back into a unit document; cost may be a number or {power: n}. */
function applyStat(unit: Record<string, unknown>, key: StatKey, value: number): void {
  if (key === 'cost') {
    const cost = unit.cost;
    unit.cost = cost !== null && typeof cost === 'object' && !Array.isArray(cost)
      ? { ...(cost as Record<string, unknown>), power: value }
      : value;
  } else {
    unit[key] = value;
  }
}

export function UnitsReportModal({ setups, specials, onClose, onSaved }: Props) {
  const listed = useMemo(() => setups.filter(isListedScenario), [setups]);
  const [sources, setSources] = useState<UnitSource[]>([]);
  const [loading, setLoading] = useState(listed.length > 0);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [ranges, setRanges] = useState(EMPTY_RANGES);
  const [selectedSpecials, setSelectedSpecials] = useState<string[]>([]);
  const [specialsOpen, setSpecialsOpen] = useState(false);
  const specialsMenuRef = useRef<HTMLDivElement>(null);
  const bundlesRef = useRef<Record<string, AdminSetupBundle>>({});
  const [pending, setPending] = useState<Map<string, Partial<Record<StatKey, number>>>>(new Map());
  const [editing, setEditing] = useState<{ key: string; stat: StatKey } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (!specialsOpen) return;
    const onDown = (event: MouseEvent) => {
      if (specialsMenuRef.current && !specialsMenuRef.current.contains(event.target as Node)) {
        setSpecialsOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [specialsOpen]);

  useEffect(() => {
    if (!editing) return;
    const onDown = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest('[data-unit-stat-edit]')) return;
      setEditing(null);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [editing]);

  useEffect(() => {
    if (listed.length === 0) return;
    let cancelled = false;
    Promise.all(listed.map((setup) => api.adminGetSetup(setup.id).then((bundle) => ({ setup, bundle }))))
      .then((loaded) => {
        if (cancelled) return;
        const byId: Record<string, AdminSetupBundle> = {};
        for (const { setup, bundle } of loaded) byId[setup.id] = bundle;
        bundlesRef.current = byId;
        setSources(flattenUnits(loaded, specials));
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load units');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [listed, specials]);

  const specialOptions = useMemo(() => {
    const names = new Set<string>();
    for (const row of sources) {
      for (const key of row.specialKeys) names.add(key);
    }
    return [...names].sort((a, b) => a.localeCompare(b));
  }, [sources]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return sources.filter((row) => {
      if (
        needle
        && !row.unitId.toLowerCase().includes(needle)
        && !iconFileName(row.icon).includes(needle)
        && !row.scenario.toLowerCase().includes(needle)
      ) {
        return false;
      }
      if (selectedSpecials.some((name) => !row.specialKeys.includes(name))) return false;
      return STATS.every((stat) => inRange(
        stat.key === 'cost' && !row.purchasable ? null : row[stat.key],
        ranges[stat.key],
      ));
    });
  }, [sources, query, ranges, selectedSpecials]);

  const rows = useMemo(() => withSpans(filtered), [filtered]);
  const modes = useMemo(() => {
    const byIcon = new Map<string, UnitSource[]>();
    for (const row of sources) {
      const list = byIcon.get(row.icon);
      if (list) list.push(row);
      else byIcon.set(row.icon, [row]);
    }
    const out = new Map<string, Record<StatKey, number | null>>();
    for (const [icon, list] of byIcon) {
      const record = {} as Record<StatKey, number | null>;
      for (const stat of STATS) {
        const values = list
          .filter((row) => stat.key !== 'cost' || row.purchasable)
          .map((row) => row[stat.key]);
        record[stat.key] = outrightMode(values);
      }
      out.set(icon, record);
    }
    return out;
  }, [sources]);
  const statOffMode = (row: UnitRow, key: StatKey, value: number) => {
    if (key === 'cost' && !row.purchasable) return false;
    const mode = modes.get(row.icon)?.[key];
    return mode == null || value !== mode;
  };
  const filtersActive = query.trim() !== ''
    || selectedSpecials.length > 0
    || STATS.some((stat) => ranges[stat.key].min.trim() !== '' || ranges[stat.key].max.trim() !== '');

  const setRange = (key: StatKey, edge: keyof Range, value: string) => {
    setRanges((current) => ({ ...current, [key]: { ...current[key], [edge]: value } }));
  };

  const toggleSpecial = (name: string) => {
    setSelectedSpecials((current) => (
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name]
    ));
  };

  const clearFilters = () => {
    setQuery('');
    setRanges(EMPTY_RANGES);
    setSelectedSpecials([]);
    setSpecialsOpen(false);
  };

  const pendingCount = useMemo(() => {
    let count = 0;
    for (const patch of pending.values()) count += Object.keys(patch).length;
    return count;
  }, [pending]);

  const rebuildSources = () => {
    const loaded: LoadedSetup[] = [];
    for (const setup of listed) {
      const bundle = bundlesRef.current[setup.id];
      if (bundle) loaded.push({ setup, bundle });
    }
    setSources(flattenUnits(loaded, specials));
  };

  const stepStat = (row: UnitSource, stat: StatKey, delta: number) => {
    if (saving) return;
    const key = pendingKey(row);
    const current = pending.get(key)?.[stat] ?? row[stat];
    const next = Math.max(statMin(stat), current + delta);
    if (next === current) return;
    setPending((prev) => {
      const updated = new Map(prev);
      const patch = { ...(updated.get(key) ?? {}) };
      if (next === row[stat]) delete patch[stat];
      else patch[stat] = next;
      if (Object.keys(patch).length === 0) updated.delete(key);
      else updated.set(key, patch);
      return updated;
    });
  };

  const cancelChanges = () => {
    setPending(new Map());
    setEditing(null);
    setSaveError(null);
  };

  const saveChanges = async () => {
    if (pending.size === 0 || saving) return;
    setSaving(true);
    setSaveError(null);
    const bySetup = new Map<string, { unitId: string; stat: StatKey; value: number }[]>();
    for (const [key, patch] of pending) {
      const sep = key.indexOf('|');
      const setupId = key.slice(0, sep);
      const unitId = key.slice(sep + 1);
      const edits = bySetup.get(setupId) ?? [];
      for (const stat of STATS) {
        const value = patch[stat.key];
        if (value !== undefined) edits.push({ unitId, stat: stat.key, value });
      }
      bySetup.set(setupId, edits);
    }
    const failures: string[] = [];
    for (const [setupId, edits] of bySetup) {
      const bundle = bundlesRef.current[setupId];
      const setup = listed.find((s) => s.id === setupId);
      const label = setup?.display_name?.trim() || setupId;
      if (!bundle) {
        failures.push(`${label}: scenario could not be loaded`);
        continue;
      }
      const units = JSON.parse(JSON.stringify(bundle.units)) as Record<string, unknown>;
      const patched: Record<string, unknown> = {};
      for (const { unitId, stat, value } of edits) {
        const raw = units[unitId];
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
        const next = { ...(raw as Record<string, unknown>) };
        applyStat(next, stat, value);
        units[unitId] = next;
        patched[unitId] = next;
      }
      try {
        await api.adminPutSetup(setupId, {
          manifest: { ...(bundle.manifest as Record<string, unknown>), id: setupId },
          units,
          territories: bundle.territories,
          factions: bundle.factions,
          camps: bundle.camps,
          ports: bundle.ports,
          starting_setup: bundle.starting_setup,
        });
        bundlesRef.current = { ...bundlesRef.current, [setupId]: { ...bundle, units } };
        setPending((prev) => {
          const updated = new Map(prev);
          for (const key of [...updated.keys()]) {
            if (key.startsWith(`${setupId}|`)) updated.delete(key);
          }
          return updated;
        });
        onSaved?.(setupId, patched);
      } catch (e) {
        failures.push(`${label}: ${e instanceof Error ? e.message : 'Save failed'}`);
      }
    }
    rebuildSources();
    setSaveError(failures.length > 0 ? failures.join(' · ') : null);
    setSaving(false);
  };

  const requestClose = () => {
    if (saving) return;
    if (pending.size > 0) {
      const discard = window.confirm(
        `Discard ${pendingCount} unsaved stat change${pendingCount === 1 ? '' : 's'}?`,
      );
      if (!discard) return;
    }
    onClose();
  };

  const renderStatCell = (row: UnitRow, stat: { key: StatKey; label: string; title: string }) => {
    const key = pendingKey(row);
    const patch = pending.get(key);
    const value = patch?.[stat.key] ?? row[stat.key];
    const dirty = patch?.[stat.key] !== undefined;
    const isEditing = editing?.key === key && editing.stat === stat.key;
    const editable = stat.key !== 'cost' || row.purchasable;
    const off = statOffMode(row, stat.key, value);
    const classNames = [
      'stats-col-num',
      off ? ' units-across__off' : '',
      dirty ? ' units-across__pending' : '',
      isEditing ? ' units-across__editing' : '',
    ].filter(Boolean).join(' ');
    return (
      <td
        key={stat.key}
        className={classNames}
        data-unit-stat-edit={isEditing ? '' : undefined}
        title={editable ? `${stat.title}${dirty ? ` (${row[stat.key]} → ${value}, unsaved)` : ''}` : undefined}
        onClick={editable
          ? () => setEditing((cur) => (cur && cur.key === key && cur.stat === stat.key ? null : { key, stat: stat.key }))
          : undefined}
      >
        {isEditing ? (
          <span className="units-across__stepper">
            <span className="units-across__step-value">{value}</span>
            <span className="units-across__step-arrows">
              <button
                type="button"
                className="units-across__step-btn"
                disabled={saving}
                aria-label={`Increase ${stat.title} for ${row.unitId}`}
                onClick={(e) => {
                  e.stopPropagation();
                  stepStat(row, stat.key, 1);
                }}
              >
                ▲
              </button>
              <button
                type="button"
                className="units-across__step-btn"
                disabled={saving}
                aria-label={`Decrease ${stat.title} for ${row.unitId}`}
                onClick={(e) => {
                  e.stopPropagation();
                  stepStat(row, stat.key, -1);
                }}
              >
                ▼
              </button>
            </span>
          </span>
        ) : editable ? (
          value
        ) : (
          '–'
        )}
      </td>
    );
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={requestClose}>
      <div
        className="admin-modal admin-modal--units"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-units-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="games-report__header">
          <div>
            <h2 id="admin-units-title" className="admin-modal__title">
              Units
            </h2>
            <p className="admin-form__micro">
              {loading
                ? 'Loading units…'
                : `${rows.length} of ${sources.length} units from scenarios set to Yes or Admin only.`}
            </p>
          </div>
          <div className="units-across__header-actions">
            {pendingCount > 0 ? (
              <>
                <span className="admin-form__micro">
                  {pendingCount} unsaved {pendingCount === 1 ? 'change' : 'changes'}
                </span>
                <button type="button" className="admin-page__btn" disabled={saving} onClick={cancelChanges}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--primary"
                  disabled={saving}
                  onClick={saveChanges}
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
              </>
            ) : null}
            <button type="button" className="admin-page__btn" disabled={saving} onClick={requestClose}>
              Close
            </button>
          </div>
        </div>
        <div className="units-across__filters">
          <label className="units-across__search">
            <span className="admin-page__field-label">Search</span>
            <input
              className="admin-form__input"
              value={query}
              placeholder="Unit id, icon, or scenario"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <div className={`games-report__dropdown${specialsOpen ? ' games-report__dropdown--open' : ''}`} ref={specialsMenuRef}>
            <button
              type="button"
              className={`games-report__filter${selectedSpecials.length > 0 ? ' games-report__filter--active' : ''}`}
              aria-haspopup="listbox"
              aria-expanded={specialsOpen}
              onClick={() => setSpecialsOpen((open) => !open)}
            >
              Specials ▾
            </button>
            {specialsOpen ? (
              <div className="games-report__dropdown-menu" role="listbox" aria-multiselectable="true">
                {specialOptions.length === 0 ? (
                  <p className="admin-form__micro games-report__dropdown-empty">No specials found.</p>
                ) : (
                  specialOptions.map((name) => (
                    <label key={name} className="games-report__dropdown-item" role="option" aria-selected={selectedSpecials.includes(name)}>
                      <input
                        type="checkbox"
                        checked={selectedSpecials.includes(name)}
                        onChange={() => toggleSpecial(name)}
                      />
                      <span>{name}</span>
                    </label>
                  ))
                )}
              </div>
            ) : null}
          </div>
          {filtersActive ? (
            <button type="button" className="admin-page__btn" onClick={clearFilters}>
              Clear
            </button>
          ) : null}
        </div>
        <div className="units-across__ranges" aria-label="Stat ranges">
          {STATS.map((stat) => (
            <fieldset key={stat.key} className="units-across__range">
              <legend title={stat.title}>{stat.label}</legend>
              <input
                className="admin-form__input"
                inputMode="decimal"
                aria-label={`${stat.title} minimum`}
                placeholder="min"
                value={ranges[stat.key].min}
                onChange={(e) => setRange(stat.key, 'min', e.target.value)}
              />
              <input
                className="admin-form__input"
                inputMode="decimal"
                aria-label={`${stat.title} maximum`}
                placeholder="max"
                value={ranges[stat.key].max}
                onChange={(e) => setRange(stat.key, 'max', e.target.value)}
              />
            </fieldset>
          ))}
        </div>
        <p className="admin-form__micro units-across__filter-note">
          A unit is kept when each filled minimum and maximum includes its stat, and it has every selected special.
        </p>
        {error ? <p className="admin-page__error games-report__error">{error}</p> : null}
        {saveError ? <p className="admin-page__error games-report__error">{saveError}</p> : null}
        <div className="games-report__table-scroll unit-stats-modal units-across">
          {loading ? <p className="admin-form__micro">Loading units…</p> : null}
          {!loading && sources.length === 0 ? <p className="admin-form__micro">No units in active scenarios.</p> : null}
          {!loading && sources.length > 0 && rows.length === 0 ? (
            <p className="admin-form__micro">No units match.</p>
          ) : null}
          {!loading && rows.length > 0 ? (
            <table className="header-stats-table header-stats-table--units units-across__table">
              <colgroup>
                <col className="units-across__col-icon" />
                <col className="units-across__col-scenario" />
                <col className="units-across__col-id" />
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <col key={i} className="stats-units-col-num" />
                ))}
                <col className="stats-units-col-specials" />
              </colgroup>
              <thead>
                <tr>
                  <th className="units-across__icon">Icon</th>
                  <th className="units-across__scenario">Scenario</th>
                  <th className="units-across__id">Unit id</th>
                  <th className="stats-col-num">P</th>
                  <th className="stats-col-num">A</th>
                  <th className="stats-col-num">D</th>
                  <th className="stats-col-num">R</th>
                  <th className="stats-col-num">M</th>
                  <th className="stats-col-num">HP</th>
                  <th className="stats-col-num stats-col-specials">SP</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.icon}|${row.setupId}|${row.unitId}`}
                    className={`unit-stats-unit-row${row.scenarioBand ? ' units-across__band' : ''}${row.iconStart ? ' units-across__icon-start' : ''}${row.iconEnd ? ' units-across__icon-end' : ''}${row.scenarioEnd ? ' units-across__scenario-end' : ''}`}
                  >
                    {row.iconSpan > 0 ? (
                      <td className="units-across__icon" rowSpan={row.iconSpan}>
                        <img src={row.icon} alt="" className="unit-stats-unit-icon" aria-hidden />
                      </td>
                    ) : null}
                    {row.scenarioSpan > 0 ? (
                      <td
                        className={`units-across__scenario${row.scenarioClosesIcon ? ' units-across__edge-icon' : ' units-across__edge-scenario'}`}
                        rowSpan={row.scenarioSpan}
                      >
                        {row.scenario}
                      </td>
                    ) : null}
                    <td className="units-across__id">{row.unitId}</td>
                    {STATS.map((stat) => renderStatCell(row, stat))}
                    <td className="stats-col-num stats-col-specials">{row.specials}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
        <p className="unit-stats-modal-key">
          P = Power cost | A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials. A red number is not that icon’s usual value. Click a stat to adjust it, then Save or Cancel.
        </p>
      </div>
    </div>
  );
}
