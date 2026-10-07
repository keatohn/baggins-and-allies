import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, getAuthToken, getResolvedApiBase, usesViteApiProxy } from '../services/api';
import type { AdminSetupBundle, AdminSetupListItem, AuthPlayer, Catalog, UnitFormulas } from '../services/api';
import {
  CampsPanel,
  FactionsPanel,
  JsonTabEditor,
  ManifestPanel,
  PortsPanel,
  StartingSetupPanel,
  TerritoriesPanel,
  UnitsPanel,
} from './admin/SetupEditorPanels';
import { AudioPanel } from './admin/AudioPanel';
import { CatalogPanel } from './admin/CatalogPanel';
import { FormulasPanel } from './admin/FormulasPanel';
import { EMPTY_FORMULAS, asFormulas, predictCost, unitFeatures, unitPowerCost } from './admin/unitFormulas';
import { SignalsPanel } from './admin/SignalsPanel';
import type { SignalPreset } from '../territorySignals';
import { isValidSetupId } from './admin/setupId';
import { BalanceModal } from './admin/BalanceModal';
import { previewStatsFromBundle } from './admin/previewStats';
import { completeTerritoryAsymmetries } from './admin/territoryGraph';
import { MapViewPane, type MapSetupDraft } from './admin/MapViewPane';
import { GameStatsModal, UnitStatsModal, type UnitStatsExtraColumn } from '../components/StatsModals';
import './Admin.css';

const TAB_KEYS = [
  'manifest',
  'units',
  'territories',
  'factions',
  'camps',
  'ports',
  'starting_setup',
] as const;

type TabKey = (typeof TAB_KEYS)[number];

const EMPTY_CATALOG: Catalog = { specials: [], special_rules: [], game_options: [], terrain_types: [], archetypes: [] };

/** Raw JSON edits may drop keys; the panels need every list present. */
function asCatalog(raw: unknown): Catalog {
  const o = raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const list = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
  return {
    specials: list(o.specials),
    special_rules: list(o.special_rules),
    game_options: list(o.game_options),
    terrain_types: list(o.terrain_types),
    archetypes: list(o.archetypes),
  };
}

function printedTerritoryPower(territories: AdminSetupBundle['territories'] | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  if (!territories) return out;
  for (const [id, raw] of Object.entries(territories)) {
    const produces = raw && typeof raw === 'object' ? (raw as { produces?: { power?: unknown } }).produces : undefined;
    const n = Number(produces?.power);
    out[id] = Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0;
  }
  return out;
}

function factionAndSubfactionIds(factions: AdminSetupBundle['factions'] | undefined): string[] {
  const ids: string[] = [];
  const raw = factions && typeof factions === 'object' ? factions : {};
  for (const [fid, faction] of Object.entries(raw)) {
    ids.push(fid);
    const subs = (faction as { subfactions?: unknown }).subfactions;
    if (!Array.isArray(subs)) continue;
    for (const sub of subs) {
      if (!sub || typeof sub !== 'object') continue;
      const sid = (sub as { id?: unknown }).id;
      if (typeof sid === 'string' && sid.trim()) ids.push(sid.trim());
    }
  }
  return ids;
}

const TAB_LABELS: Record<TabKey, string> = {
  manifest: 'Manifest',
  units: 'Units',
  territories: 'Territories',
  factions: 'Factions',
  camps: 'Camps',
  ports: 'Ports',
  starting_setup: 'Starting setup',
};

const DELETE_SETUP_CONFIRM_PHRASE = 'DELETE SETUP';

type DictEntityMap = Record<string, Record<string, unknown>>;

const MASTER_BUNDLE_KEYS = [
  'manifest',
  'units',
  'territories',
  'factions',
  'camps',
  'ports',
  'starting_setup',
] as const;

/** Leading UTF-8 BOM from some editors breaks `JSON.parse` in the browser. */
function normalizeImportedMasterJsonText(raw: string): string {
  return raw.replace(/^\uFEFF/, '').trim();
}

function CreateSetupDialog({
  open,
  onClose,
  setups,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  setups: AdminSetupListItem[];
  onCreated: (id: string) => void;
}) {
  const [newId, setNewId] = useState('');
  const [duplicateFrom, setDuplicateFrom] = useState('');
  const [createMode, setCreateMode] = useState<'empty' | 'copy' | 'import'>('empty');
  const [masterJsonText, setMasterJsonText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setNewId('');
      setDuplicateFrom('');
      setCreateMode('empty');
      setMasterJsonText('');
      setErr(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    const id = newId.trim();
    if (!isValidSetupId(id)) {
      setErr('Use a unique id: start with a letter or digit; only letters, digits, underscore, hyphen, dot; max 127 chars.');
      return;
    }
    if (createMode === 'copy' && !duplicateFrom.trim()) {
      setErr('Choose which setup to copy.');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      if (createMode === 'import') {
        let parsed: unknown;
        const jsonText = normalizeImportedMasterJsonText(masterJsonText) || '{}';
        try {
          parsed = JSON.parse(jsonText);
        } catch (e) {
          const detail = e instanceof Error ? e.message : String(e);
          setErr(`Master JSON is not valid JSON: ${detail}`);
          setBusy(false);
          return;
        }
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          setErr('Master JSON must be a single object.');
          setBusy(false);
          return;
        }
        const o = parsed as Record<string, unknown>;
        const missing = MASTER_BUNDLE_KEYS.filter((k) => !(k in o));
        if (missing.length) {
          setErr(`Master JSON is missing keys: ${missing.join(', ')}`);
          setBusy(false);
          return;
        }
        await api.adminCreateSetup({
          id,
          duplicate_from: null,
          bundle_json: jsonText,
        });
      } else if (createMode === 'copy') {
        await api.adminCreateSetup({
          id,
          duplicate_from: duplicateFrom.trim(),
        });
      } else {
        await api.adminCreateSetup({ id, duplicate_from: null });
      }
      onCreated(id);
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Create failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className={`admin-modal${createMode === 'import' ? ' admin-modal--wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-create-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="admin-create-title" className="admin-modal__title">
          New setup
        </h2>
        <p className="admin-form__micro">Setup id cannot be changed after creation. It must be unique.</p>
        <div className="admin-form__row admin-form__row--radio-row">
          <span className="admin-form__label">Source</span>
          <div className="admin-form__radio-group admin-form__radio-group--create-setup">
            <label className="admin-form__radio-label">
              <input
                type="radio"
                name="admin-create-mode"
                checked={createMode === 'empty'}
                onChange={() => setCreateMode('empty')}
              />
              Empty
            </label>
            <label
              className={`admin-form__radio-label${setups.length === 0 ? ' admin-form__radio-label--disabled' : ''}`}
              title={setups.length === 0 ? 'No setups to copy yet' : undefined}
            >
              <input
                type="radio"
                name="admin-create-mode"
                checked={createMode === 'copy'}
                disabled={setups.length === 0}
                onChange={() => setCreateMode('copy')}
              />
              Copy
            </label>
            <label className="admin-form__radio-label">
              <input
                type="radio"
                name="admin-create-mode"
                checked={createMode === 'import'}
                onChange={() => setCreateMode('import')}
              />
              Import JSON
            </label>
          </div>
        </div>
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-new-id">
            Setup id
          </label>
          <input
            id="admin-new-id"
            className="admin-form__input"
            autoComplete="off"
            value={newId}
            onChange={(e) => setNewId(e.target.value)}
            placeholder="e.g. my_scenario_1"
          />
        </div>
        {createMode === 'copy' ? (
          <div className="admin-form__row">
            <label className="admin-form__label" htmlFor="admin-dup-from">
              Copy from
            </label>
            <select
              id="admin-dup-from"
              className="admin-page__select admin-page__select--full"
              value={duplicateFrom}
              onChange={(e) => setDuplicateFrom(e.target.value)}
            >
              <option value="">Select a setup…</option>
              {setups.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.display_name} ({s.id})
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {createMode === 'import' ? (
          <div className="admin-form__row admin-form__row--stack">
            <label className="admin-form__label" htmlFor="admin-master-json">
              Bundle JSON
            </label>
            <textarea
              id="admin-master-json"
              className="admin-form__textarea admin-form__textarea--json admin-form__textarea--master-bundle"
              spellCheck={false}
              placeholder={`{\n  "manifest": { ... },\n  "units": { ... },\n  ...\n}`}
              value={masterJsonText}
              onChange={(e) => setMasterJsonText(e.target.value)}
            />
            <p className="admin-form__micro">
              Keys: {MASTER_BUNDLE_KEYS.join(', ')}
            </p>
          </div>
        ) : null}
        {err ? <div className="admin-page__error">{err}</div> : null}
        <div className="admin-modal__actions">
          <button type="button" className="admin-page__btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={submit} disabled={busy}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Admin() {
  const navigate = useNavigate();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(true);
  const [setups, setSetups] = useState<AdminSetupListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [activeTab, setActiveTab] = useState<TabKey>('manifest');
  const [bundle, setBundle] = useState<AdminSetupBundle | null>(null);
  const [jsonTab, setJsonTab] = useState<Partial<Record<TabKey, boolean>>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveOk, setSaveOk] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadingBundle, setLoadingBundle] = useState(false);
  const [mapViewOpen, setMapViewOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [resolveAsymmetriesOpen, setResolveAsymmetriesOpen] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState('');
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [audioGains, setAudioGains] = useState<Record<string, number>>({});
  const [menuMusic, setMenuMusic] = useState<string[]>([]);
  const [audioOpen, setAudioOpen] = useState(false);
  const [audioJsonMode, setAudioJsonMode] = useState(false);
  const [signalsOpen, setSignalsOpen] = useState(false);
  const [signalsJsonMode, setSignalsJsonMode] = useState(false);
  const [signalPresets, setSignalPresets] = useState<SignalPreset[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogJsonMode, setCatalogJsonMode] = useState(false);
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);
  const [formulasOpen, setFormulasOpen] = useState(false);
  const [formulasJsonMode, setFormulasJsonMode] = useState(false);
  const [formulas, setFormulas] = useState<UnitFormulas>(EMPTY_FORMULAS);
  const globalOpen = audioOpen || signalsOpen || catalogOpen || formulasOpen;
  const [unitStatsOpen, setUnitStatsOpen] = useState(false);
  const [gameStatsOpen, setGameStatsOpen] = useState(false);
  const [balanceOpen, setBalanceOpen] = useState(false);
  const audioJson = useMemo(() => ({ gains: audioGains, menu_music: menuMusic }), [audioGains, menuMusic]);
  const signalsJson = useMemo(() => ({ presets: signalPresets }), [signalPresets]);
  const [statsWithRings, setStatsWithRings] = useState(true);
  const statsPreview = useMemo(
    () => previewStatsFromBundle(bundle, { rings: statsWithRings, specials: catalog.specials }),
    [bundle, statsWithRings, catalog.specials],
  );
  const formulaColumns = useMemo((): UnitStatsExtraColumn[] => {
    const units = (bundle?.units ?? {}) as Record<string, Record<string, unknown>>;
    const shown = [
      { key: 'regression', formula: formulas.regression },
      { key: 'custom', formula: formulas.custom },
    ].filter(({ formula }) => formula.show);
    if (!shown.length) return [];
    const columns: UnitStatsExtraColumn[] = shown.map(({ key, formula }) => ({
      key,
      label: formula.label.trim() || (key === 'regression' ? 'Fit' : 'Custom'),
      tone: 'formula',
      values: {},
    }));
    const diff: UnitStatsExtraColumn = { key: 'diff', label: formulas.diff_label.trim() || 'Δ', tone: 'diff', values: {} };
    for (const [id, unit] of Object.entries(units)) {
      const feats = unitFeatures(unit);
      const cost = unitPowerCost(unit);
      const priced = unit.purchasable !== false && cost > 0;
      let total = 0;
      shown.forEach(({ formula }, i) => {
        const predicted = predictCost(formula, feats);
        columns[i].values[id] = predicted;
        total += predicted - cost;
      });
      if (priced) diff.values[id] = total;
    }
    return [...columns, diff];
  }, [bundle?.units, formulas]);
  const formulaKey = formulaColumns.length
    ? `${formulaColumns
        .filter((c) => c.tone === 'formula')
        .map((c) => c.label)
        .join(', ')} = predicted cost | ${formulaColumns[formulaColumns.length - 1].label} = sum of (predicted − P): green = underpriced, red = overpriced`
    : undefined;
  const ringsToggle = statsPreview?.ringsMode === 'optional' ? (
    <label>
      <input type="checkbox" checked={statsWithRings} onChange={(e) => setStatsWithRings(e.target.checked)} />
      Rings of Power
    </label>
  ) : undefined;
  const asymmetryError = Boolean(saveError?.includes('territory graph asymmetry'));

  const useJson = jsonTab[activeTab] === true;

  useEffect(() => {
    if (!getAuthToken()) {
      navigate('/', { replace: true });
      return;
    }
    api
      .authMe()
      .then((p) => {
        setPlayer(p);
        if (!p.is_admin) navigate('/', { replace: true });
      })
      .catch(() => navigate('/', { replace: true }))
      .finally(() => setLoading(false));
  }, [navigate]);

  const refreshList = useCallback(() => {
    setLoadError(null);
    return api
      .adminListSetups()
      .then((r) => {
        setSetups(r.setups);
        setSelectedId((prev) => prev || (r.setups[0]?.id ?? ''));
      })
      .catch((e: Error) => setLoadError(e.message));
  }, []);

  useEffect(() => {
    if (!player?.is_admin) return;
    refreshList();
  }, [player?.is_admin, refreshList]);

  useEffect(() => {
    if (!player?.is_admin) return;
    api
      .getAudioGains()
      .then((r) => {
        setAudioGains(r.gains ?? {});
        setMenuMusic(r.menu_music ?? []);
      })
      .catch(() => setAudioGains({}));
    api
      .getSignals()
      .then((r) => setSignalPresets(r.presets ?? []))
      .catch(() => setSignalPresets([]));
    api
      .getCatalog()
      .then((c) => setCatalog(asCatalog(c)))
      .catch(() => setCatalog(EMPTY_CATALOG));
    api
      .adminGetFormulas()
      .then((f) => setFormulas(asFormulas(f)))
      .catch(() => setFormulas(EMPTY_FORMULAS));
  }, [player?.is_admin]);

  const loadBundle = useCallback((id: string) => {
    if (!id) return;
    setLoadingBundle(true);
    setSaveError(null);
    setSaveOk(false);
    setLoadError(null);
    api
      .adminGetSetup(id)
      .then((b) => {
        setBundle({
          ...b,
          manifest: { ...b.manifest, id: b.id },
        } as AdminSetupBundle);
      })
      .catch((e: Error) => {
        setBundle(null);
        setLoadError(e.message);
      })
      .finally(() => setLoadingBundle(false));
  }, []);

  useEffect(() => {
    if (!player?.is_admin || !selectedId) return;
    loadBundle(selectedId);
  }, [player?.is_admin, selectedId, loadBundle]);

  const handleSave = async () => {
    setSaveError(null);
    setSaveOk(false);
    if (audioOpen) {
      setSaving(true);
      try {
        const res = await api.adminPutAudio({ gains: audioGains, menu_music: menuMusic });
        setAudioGains(res.gains ?? {});
        setMenuMusic(res.menu_music ?? []);
        setSaveOk(true);
      } catch (e) {
        setSaveError(e instanceof Error ? e.message : 'Save failed');
      } finally {
        setSaving(false);
      }
      return;
    }
    if (signalsOpen) {
      setSaving(true);
      try {
        const res = await api.adminPutSignals(signalPresets);
        setSignalPresets(res.presets ?? []);
        setSaveOk(true);
      } catch (e) {
        setSaveError(e instanceof Error ? e.message : 'Save failed');
      } finally {
        setSaving(false);
      }
      return;
    }
    if (catalogOpen) {
      setSaving(true);
      try {
        const res = await api.adminPutCatalog(catalog);
        setCatalog(asCatalog(res.catalog));
        setSaveOk(true);
      } catch (e) {
        setSaveError(e instanceof Error ? e.message : 'Save failed');
      } finally {
        setSaving(false);
      }
      return;
    }
    if (formulasOpen) {
      setSaving(true);
      try {
        const res = await api.adminPutFormulas(formulas);
        setFormulas(asFormulas(res.formulas));
        setSaveOk(true);
      } catch (e) {
        setSaveError(e instanceof Error ? e.message : 'Save failed');
      } finally {
        setSaving(false);
      }
      return;
    }
    if (!selectedId || !bundle) return;
    const body = {
      manifest: { ...(bundle.manifest as Record<string, unknown>), id: selectedId },
      units: bundle.units as DictEntityMap,
      territories: bundle.territories as DictEntityMap,
      factions: bundle.factions as DictEntityMap,
      camps: bundle.camps as DictEntityMap,
      ports: bundle.ports as DictEntityMap,
      starting_setup: bundle.starting_setup as Record<string, unknown>,
    };
    setSaving(true);
    try {
      await api.adminPutSetup(selectedId, body);
      setSaveOk(true);
      await refreshList();
      loadBundle(selectedId);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const saveSetupFromMap = async ({ territories, starting_setup, camps, ports }: MapSetupDraft) => {
    if (!selectedId || !bundle) throw new Error('No setup loaded');
    const body = {
      manifest: { ...(bundle.manifest as Record<string, unknown>), id: selectedId },
      units: bundle.units as DictEntityMap,
      territories,
      factions: bundle.factions as DictEntityMap,
      camps,
      ports,
      starting_setup,
    };
    try {
      await api.adminPutSetup(selectedId, body);
      setBundle({
        ...bundle,
        territories,
        camps,
        ports,
        starting_setup: starting_setup as typeof bundle.starting_setup,
      });
      setSaveError(null);
      setSaveOk(true);
      await refreshList();
      loadBundle(selectedId);
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Save failed';
      setSaveError(message);
      throw new Error(message);
    }
  };

  const confirmResolveAsymmetries = () => {
    if (!bundle?.territories || typeof bundle.territories !== 'object' || Array.isArray(bundle.territories)) return;
    const territories = completeTerritoryAsymmetries(bundle.territories as Record<string, Record<string, unknown>>);
    setBundle({ ...bundle, territories });
    const remaining = (saveError ?? '')
      .split('; ')
      .filter((part) => !part.includes('territory graph asymmetry'))
      .join('; ')
      .trim();
    setSaveError(remaining || null);
    setSaveOk(false);
    setResolveAsymmetriesOpen(false);
  };

  const onCreatedSetup = (id: string) => {
    refreshList().then(() => {
      setSelectedId(id);
    });
  };

  const openDeleteDialog = () => {
    if (!selectedId) return;
    setDeleteOpen(true);
    setDeleteConfirmText('');
    setDeleteError(null);
  };

  const closeDeleteDialog = () => {
    setDeleteOpen(false);
    setDeleteConfirmText('');
    setDeleteError(null);
    setDeleting(false);
  };

  const confirmDeleteSetup = async () => {
    if (!selectedId || deleteConfirmText !== DELETE_SETUP_CONFIRM_PHRASE || deleting) return;
    setDeleteError(null);
    setDeleting(true);
    try {
      const deletingId = selectedId;
      await api.adminDeleteSetup(deletingId);
      const list = await api.adminListSetups();
      setSetups(list.setups);
      const remaining = list.setups;
      const nextSelected = remaining.find((s) => s.id !== deletingId)?.id ?? remaining[0]?.id ?? '';
      setSelectedId(nextSelected);
      if (!nextSelected) setBundle(null);
      closeDeleteDialog();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Delete failed');
      setDeleting(false);
    }
  };

  const renderTabBody = () => {
    if (formulasOpen) {
      if (formulasJsonMode) {
        return <JsonTabEditor value={formulas} onChange={(p) => setFormulas(asFormulas(p))} />;
      }
      return <FormulasPanel formulas={formulas} onChange={setFormulas} setups={setups} specials={catalog.specials} />;
    }
    if (catalogOpen) {
      if (catalogJsonMode) {
        return <JsonTabEditor value={catalog} onChange={(p) => setCatalog(asCatalog(p))} />;
      }
      return <CatalogPanel catalog={catalog} onChange={setCatalog} />;
    }
    if (signalsOpen) {
      if (signalsJsonMode) {
        return (
          <JsonTabEditor
            value={signalsJson}
            onChange={(p) => {
              const obj = p && typeof p === 'object' && !Array.isArray(p) ? (p as Record<string, unknown>) : {};
              const list = Array.isArray(obj.presets) ? obj.presets : [];
              const next: SignalPreset[] = [];
              for (const item of list) {
                if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
                const row = item as Record<string, unknown>;
                const applies = row.applies_to;
                if (
                  typeof row.id !== 'string' ||
                  typeof row.label !== 'string' ||
                  typeof row.icon !== 'string' ||
                  (applies !== 'enemy' && applies !== 'allied' && applies !== 'neutral' && applies !== 'any')
                ) {
                  continue;
                }
                const color = typeof row.color === 'string' && /^#[0-9a-fA-F]{6}$/.test(row.color)
                  ? row.color
                  : '#6b5b4b';
                next.push({ id: row.id, label: row.label, icon: row.icon, applies_to: applies, color });
              }
              setSignalPresets(next);
            }}
          />
        );
      }
      return <SignalsPanel presets={signalPresets} onChange={setSignalPresets} />;
    }
    if (audioOpen) {
      if (audioJsonMode) {
        return (
          <JsonTabEditor
            value={audioJson}
            onChange={(p) => {
              const obj = p && typeof p === 'object' && !Array.isArray(p) ? (p as Record<string, unknown>) : {};
              const inner = obj.gains && typeof obj.gains === 'object' && !Array.isArray(obj.gains) ? obj.gains : obj;
              const next: Record<string, number> = {};
              for (const [k, v] of Object.entries(inner as Record<string, unknown>)) {
                const n = typeof v === 'number' ? v : Number(v);
                if (Number.isFinite(n)) next[k] = n;
              }
              setAudioGains(next);
              if (Array.isArray(obj.menu_music)) {
                setMenuMusic(obj.menu_music.filter((x): x is string => typeof x === 'string'));
              }
            }}
          />
        );
      }
      return (
        <AudioPanel
          gains={audioGains}
          onGainsChange={setAudioGains}
          menuMusic={menuMusic}
          onMenuMusicChange={setMenuMusic}
        />
      );
    }
    if (!bundle) {
      return <p className="admin-page__empty">Select a setup to edit.</p>;
    }
    if (useJson) {
      const j = (v: unknown, fn: (p: unknown) => void) => <JsonTabEditor value={v} onChange={fn} />;
      switch (activeTab) {
        case 'manifest':
          return j(bundle.manifest, (p) => setBundle((b) => (b ? { ...b, manifest: p as typeof b.manifest } : null)));
        case 'units':
          return j(bundle.units, (p) => setBundle((b) => (b ? { ...b, units: p as typeof b.units } : null)));
        case 'territories':
          return j(bundle.territories, (p) => setBundle((b) => (b ? { ...b, territories: p as typeof b.territories } : null)));
        case 'factions':
          return j(bundle.factions, (p) => setBundle((b) => (b ? { ...b, factions: p as typeof b.factions } : null)));
        case 'camps':
          return j(bundle.camps, (p) => setBundle((b) => (b ? { ...b, camps: p as typeof b.camps } : null)));
        case 'ports':
          return j(bundle.ports, (p) => setBundle((b) => (b ? { ...b, ports: p as typeof b.ports } : null)));
        case 'starting_setup':
          return j(bundle.starting_setup, (p) => setBundle((b) => (b ? { ...b, starting_setup: p as typeof b.starting_setup } : null)));
        default:
          return null;
      }
    }
    switch (activeTab) {
      case 'manifest':
        return (
          <ManifestPanel
            setupId={selectedId}
            manifest={bundle.manifest as Record<string, unknown>}
            territoryIds={Object.keys((bundle.territories as Record<string, unknown>) ?? {}).sort()}
            territoryPower={printedTerritoryPower(bundle.territories)}
            onManifestChange={(m) =>
              setBundle((b) => (b ? { ...b, manifest: { ...m, id: selectedId } as typeof b.manifest } : null))
            }
          />
        );
      case 'units':
        return (
          <UnitsPanel
            units={(bundle.units as DictEntityMap) ?? {}}
            factionIds={factionAndSubfactionIds(bundle.factions)}
            archetypes={catalog.archetypes}
            setupId={selectedId}
            setups={setups}
            onChange={(next) => setBundle((b) => (b ? { ...b, units: next as typeof b.units } : null))}
          />
        );
      case 'territories':
        return (
          <TerritoriesPanel
            territories={(bundle.territories as DictEntityMap) ?? {}}
            terrainTypes={catalog.terrain_types}
            onChange={(next) => setBundle((b) => (b ? { ...b, territories: next as typeof b.territories } : null))}
          />
        );
      case 'factions':
        return (
          <FactionsPanel
            factions={(bundle.factions as DictEntityMap) ?? {}}
            onChange={(next) => setBundle((b) => (b ? { ...b, factions: next as typeof b.factions } : null))}
          />
        );
      case 'camps':
        return (
          <CampsPanel
            camps={(bundle.camps as DictEntityMap) ?? {}}
            onChange={(next) => setBundle((b) => (b ? { ...b, camps: next as typeof b.camps } : null))}
          />
        );
      case 'ports':
        return (
          <PortsPanel
            ports={(bundle.ports as DictEntityMap) ?? {}}
            onChange={(next) => setBundle((b) => (b ? { ...b, ports: next as typeof b.ports } : null))}
          />
        );
      case 'starting_setup':
        return (
          <StartingSetupPanel
            bundle={bundle}
            onChange={(ss) => setBundle((b) => (b ? { ...b, starting_setup: ss as typeof b.starting_setup } : null))}
          />
        );
      default:
        return null;
    }
  };

  const openSection = (section: 'setups' | 'catalog' | 'formulas' | 'audio' | 'signals') => {
    setCatalogOpen(section === 'catalog');
    setFormulasOpen(section === 'formulas');
    setAudioOpen(section === 'audio');
    setSignalsOpen(section === 'signals');
    setSaveOk(false);
    setSaveError(null);
  };

  if (loading) {
    return <div className="admin-page admin-page--loading">Loading…</div>;
  }

  if (!player?.is_admin) {
    return null;
  }

  return (
    <div className="admin-page">
      <div className="admin-page__nav">
        <Link to="/" className="page-menu-btn">
          Menu
        </Link>
        <div className="admin-page__nav-tools">
          <button
            type="button"
            className={`page-menu-btn${!globalOpen ? ' admin-page__nav-btn--active' : ''}`}
            onClick={() => openSection('setups')}
          >
            Setups
          </button>
          <button
            type="button"
            className={`page-menu-btn${catalogOpen ? ' admin-page__nav-btn--active' : ''}`}
            onClick={() => openSection('catalog')}
          >
            Catalog
          </button>
          <button
            type="button"
            className={`page-menu-btn${formulasOpen ? ' admin-page__nav-btn--active' : ''}`}
            onClick={() => openSection('formulas')}
          >
            Formulas
          </button>
          <button
            type="button"
            className={`page-menu-btn${audioOpen ? ' admin-page__nav-btn--active' : ''}`}
            onClick={() => openSection('audio')}
          >
            Audio
          </button>
          <button
            type="button"
            className={`page-menu-btn${signalsOpen ? ' admin-page__nav-btn--active' : ''}`}
            onClick={() => openSection('signals')}
          >
            Signals
          </button>
        </div>
      </div>

      {formulasOpen ? (
        <div className="admin-page__toolbar admin-page__toolbar--wrap">
          <label className="admin-page__checkbox-label">
            <input
              type="checkbox"
              checked={formulasJsonMode}
              onChange={() => setFormulasJsonMode((v) => !v)}
            />
            Raw JSON
          </label>
        </div>
      ) : catalogOpen ? (
        <div className="admin-page__toolbar admin-page__toolbar--wrap">
          <label className="admin-page__checkbox-label">
            <input
              type="checkbox"
              checked={catalogJsonMode}
              onChange={() => setCatalogJsonMode((v) => !v)}
            />
            Raw JSON
          </label>
        </div>
      ) : signalsOpen ? (
        <div className="admin-page__toolbar admin-page__toolbar--wrap">
          <label className="admin-page__checkbox-label">
            <input
              type="checkbox"
              checked={signalsJsonMode}
              onChange={() => setSignalsJsonMode((v) => !v)}
            />
            Raw JSON
          </label>
        </div>
      ) : audioOpen ? (
        <div className="admin-page__toolbar admin-page__toolbar--wrap">
          <label className="admin-page__checkbox-label">
            <input
              type="checkbox"
              checked={audioJsonMode}
              onChange={() => setAudioJsonMode((v) => !v)}
            />
            Raw JSON
          </label>
        </div>
      ) : (
      <div className="admin-page__toolbar admin-page__toolbar--wrap">
        <label className="admin-page__field">
          <span className="admin-page__field-label">Setup</span>
          <select
            className="admin-page__select"
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            disabled={loadingBundle || setups.length === 0}
          >
            {setups.length === 0 ? (
              <option value="">No setups</option>
            ) : (
              setups.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.display_name} ({s.id})
                </option>
              ))
            )}
          </select>
        </label>
        <button type="button" className="admin-page__btn" onClick={() => setCreateOpen(true)}>
          New setup
        </button>
        <button type="button" className="admin-page__btn" disabled={!bundle} onClick={() => setMapViewOpen(true)}>
          Map View
        </button>
        <label className="admin-page__checkbox-label">
          <input
            type="checkbox"
            checked={useJson}
            onChange={() => setJsonTab((t) => ({ ...t, [activeTab]: !t[activeTab] }))}
          />
          Raw JSON
        </label>
        {loadingBundle ? <span className="admin-page__loading-inline">Loading…</span> : null}
      </div>
      )}

      {loadError ? (
        <div className="admin-page__error">
          {loadError}
          {loadError === 'Not Found' ? (
            <span className="admin-page__error-hint">
              {usesViteApiProxy() ? (
                <>
                  {' '}
                  You are on the Vite dev server: the UI requests <code>/api/admin/setups</code>, which is proxied to{' '}
                  <code>http://localhost:8000/admin/setups</code> (see <code>frontend/vite.config.ts</code>). A 404 here
                  usually means the FastAPI app on port 8000 does not expose that route yet—restart it from the repo root
                  with <code>uvicorn backend.api.main:app --reload --port 8000</code>, then open{' '}
                  <code>http://localhost:8000/docs</code> and confirm <strong>GET /admin/setups</strong> appears. If the
                  backend is on another port, change the proxy <code>target</code> in Vite config.
                </>
              ) : (
                <>
                  {' '}
                  Current API base is <code>{getResolvedApiBase()}</code> (from <code>VITE_API_URL</code> when set). The
                  FastAPI app serves <code>/admin/setups</code> at the server root—avoid an extra <code>/api</code> segment
                  in that URL unless your host adds it via a reverse proxy. For typical local dev, unset{' '}
                  <code>VITE_API_URL</code> and use <code>npm run dev</code> so requests use the Vite <code>/api</code>{' '}
                  proxy.
                </>
              )}
            </span>
          ) : null}
        </div>
      ) : null}

      {setups.length === 0 && !loadError && !loadingBundle && !globalOpen ? (
        <p className="admin-page__empty">
          No setups in the database. Restart the API once so it can create the <code>setups</code> table and seed from{' '}
          <code>backend/data/setups</code> (only runs when the table is empty). Then use <strong>New setup</strong> to add
          one.
        </p>
      ) : null}

      {globalOpen ? (
        <div className="admin-page__panel">{renderTabBody()}</div>
      ) : (
      <>
        <div className="admin-page__tabs-row">
          <div className="admin-page__tabs" role="tablist">
            {TAB_KEYS.map((k) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={activeTab === k}
                className={`admin-page__tab${activeTab === k ? ' admin-page__tab--active' : ''}`}
                onClick={() => {
                  setActiveTab(k);
                  setSaveOk(false);
                  setSaveError(null);
                }}
              >
                {TAB_LABELS[k]}
              </button>
            ))}
          </div>
          <div className="admin-page__tab-previews">
            <button
              type="button"
              className="admin-page__tab"
              disabled={!statsPreview}
              onClick={() => setUnitStatsOpen(true)}
            >
              Unit stats
            </button>
            <button
              type="button"
              className="admin-page__tab"
              disabled={!statsPreview}
              onClick={() => setGameStatsOpen(true)}
            >
              Game stats
            </button>
            <button
              type="button"
              className="admin-page__tab"
              disabled={!bundle || !statsPreview}
              onClick={() => setBalanceOpen(true)}
            >
              Balance
            </button>
          </div>
        </div>
        <div className="admin-page__panel">{renderTabBody()}</div>
      </>
      )}

      <div className="admin-page__actions">
        <button
          type="button"
          className="admin-page__btn admin-page__btn--primary"
          disabled={(globalOpen ? false : !bundle) || saving}
          onClick={handleSave}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button"
          className="admin-page__btn admin-page__btn--danger"
          disabled={!bundle || saving || deleting || globalOpen}
          onClick={openDeleteDialog}
        >
          Delete setup
        </button>
      </div>

      {saveError ? <div className="admin-page__error">{saveError}</div> : null}
      {asymmetryError ? (
        <button
          type="button"
          className="admin-page__btn admin-page__error-action"
          disabled={!bundle || saving}
          onClick={() => setResolveAsymmetriesOpen(true)}
        >
          Resolve Asymmetries
        </button>
      ) : null}
      {saveOk ? (
        <p className="admin-page__success">
          {audioOpen
            ? 'Saved audio mix.'
            : signalsOpen
              ? 'Saved signals.'
              : catalogOpen
                ? 'Saved catalog.'
                : formulasOpen
                  ? 'Saved formulas.'
                : 'Saved. New games will use this data.'}
        </p>
      ) : null}

      <CreateSetupDialog open={createOpen} onClose={() => setCreateOpen(false)} setups={setups} onCreated={onCreatedSetup} />
      {mapViewOpen && bundle && !globalOpen ? (
        <MapViewPane
          mapAsset={typeof bundle.manifest?.map_asset === 'string' ? bundle.manifest.map_asset : undefined}
          manifest={(bundle.manifest as Record<string, unknown>) ?? {}}
          territories={(bundle.territories as DictEntityMap) ?? {}}
          startingSetup={(bundle.starting_setup as Record<string, unknown>) ?? {}}
          camps={(bundle.camps as DictEntityMap) ?? {}}
          ports={(bundle.ports as DictEntityMap) ?? {}}
          factions={(bundle.factions as DictEntityMap) ?? {}}
          units={(bundle.units as DictEntityMap) ?? {}}
          onClose={() => setMapViewOpen(false)}
          onSave={saveSetupFromMap}
        />
      ) : null}
      {resolveAsymmetriesOpen && (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setResolveAsymmetriesOpen(false)}>
          <div
            className="admin-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-resolve-asymmetries-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="admin-resolve-asymmetries-title" className="admin-modal__title">
              Resolve asymmetries?
            </h2>
            <p className="admin-form__micro">
              Are you sure? This will assume that adjacencies are missing and update the territory graph to complete all graph asymmetries.
            </p>
            <div className="admin-modal__actions">
              <button type="button" className="admin-page__btn" onClick={() => setResolveAsymmetriesOpen(false)}>
                Cancel
              </button>
              <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={confirmResolveAsymmetries}>
                Resolve asymmetries
              </button>
            </div>
          </div>
        </div>
      )}
      {deleteOpen && (
        <div className="admin-modal-overlay" role="presentation" onClick={closeDeleteDialog}>
          <div
            className="admin-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-delete-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="admin-delete-title" className="admin-modal__title">
              Delete setup?
            </h2>
            <p className="admin-form__micro">
              This will permanently delete <strong>{selectedId}</strong>. Type <strong>{DELETE_SETUP_CONFIRM_PHRASE}</strong> to confirm.
            </p>
            <div className="admin-form__row">
              <label className="admin-form__label" htmlFor="admin-delete-setup-confirm">
                Confirmation
              </label>
              <input
                id="admin-delete-setup-confirm"
                className="admin-form__input"
                value={deleteConfirmText}
                onChange={(e) => setDeleteConfirmText(e.target.value)}
                placeholder={DELETE_SETUP_CONFIRM_PHRASE}
                autoFocus
              />
            </div>
            {deleteError ? <div className="admin-page__error">{deleteError}</div> : null}
            <div className="admin-modal__actions">
              <button type="button" className="admin-page__btn" onClick={closeDeleteDialog} disabled={deleting}>
                Cancel
              </button>
              <button
                type="button"
                className="admin-page__btn admin-page__btn--danger"
                onClick={confirmDeleteSetup}
                disabled={deleteConfirmText !== DELETE_SETUP_CONFIRM_PHRASE || deleting}
              >
                {deleting ? 'Deleting…' : 'Delete setup'}
              </button>
            </div>
          </div>
        </div>
      )}
      {unitStatsOpen && statsPreview && (
        <UnitStatsModal
          unitsByFaction={statsPreview.unitsByFaction}
          factionData={statsPreview.factionData}
          turnOrder={statsPreview.turnOrder}
          extraColumns={formulaColumns}
          extraKey={formulaKey}
          onClose={() => setUnitStatsOpen(false)}
        />
      )}
      {gameStatsOpen && statsPreview && (
        <GameStatsModal
          factionStats={statsPreview.factionStats}
          factionData={statsPreview.factionData}
          turnOrder={statsPreview.turnOrder}
          ringsByFaction={statsPreview.ringsByFaction}
          toolbar={ringsToggle}
          onClose={() => setGameStatsOpen(false)}
        />
      )}
      {balanceOpen && bundle && statsPreview && (
        <BalanceModal
          bundle={bundle}
          factionData={statsPreview.factionData}
          withRings={statsWithRings}
          ringsToggle={ringsToggle}
          onClose={() => setBalanceOpen(false)}
        />
      )}
    </div>
  );
}
