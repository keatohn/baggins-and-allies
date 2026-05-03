import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, getAuthToken, getResolvedApiBase, usesViteApiProxy } from '../services/api';
import type { AdminSetupBundle, AdminSetupListItem, AuthPlayer } from '../services/api';
import {
  CampsPanel,
  FactionsPanel,
  JsonTabEditor,
  ManifestPanel,
  PortsPanel,
  SpecialsPanel,
  StartingSetupPanel,
  TerritoriesPanel,
  UnitsPanel,
} from './admin/SetupEditorPanels';
import { isValidSetupId } from './admin/setupId';
import './Admin.css';

const TAB_KEYS = [
  'manifest',
  'units',
  'territories',
  'factions',
  'camps',
  'ports',
  'starting_setup',
  'specials',
] as const;

type TabKey = (typeof TAB_KEYS)[number];

const TAB_LABELS: Record<TabKey, string> = {
  manifest: 'Manifest',
  units: 'Units',
  territories: 'Territories',
  factions: 'Factions',
  camps: 'Camps',
  ports: 'Ports',
  starting_setup: 'Starting setup',
  specials: 'Specials',
};

const DELETE_SETUP_CONFIRM_PHRASE = 'DELETE SETUP';

/** Defaults for current production hotfix (spawn on existing game). */
const LIVE_HOTFIX_DEFAULT_GAME = '4f0c91c4-42fc-4b66-995e-16fd0e1b42cb';
const LIVE_HOTFIX_DEFAULT_TERRITORY = 'dol_amroth';
const LIVE_HOTFIX_DEFAULT_UNIT = 'nazgul';

type DictEntityMap = Record<string, Record<string, unknown>>;

const MASTER_BUNDLE_KEYS = [
  'manifest',
  'units',
  'territories',
  'factions',
  'camps',
  'ports',
  'starting_setup',
  'specials',
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
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmText, setDeleteConfirmText] = useState('');
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [liveHotfixGameId, setLiveHotfixGameId] = useState(LIVE_HOTFIX_DEFAULT_GAME);
  const [liveHotfixTerritoryId, setLiveHotfixTerritoryId] = useState(LIVE_HOTFIX_DEFAULT_TERRITORY);
  const [liveHotfixUnitId, setLiveHotfixUnitId] = useState(LIVE_HOTFIX_DEFAULT_UNIT);
  const [liveHotfixCount, setLiveHotfixCount] = useState(1);
  const [liveHotfixBusy, setLiveHotfixBusy] = useState(false);
  const [liveHotfixError, setLiveHotfixError] = useState<string | null>(null);
  const [liveHotfixOk, setLiveHotfixOk] = useState<string | null>(null);

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
    if (!selectedId || !bundle) return;
    setSaveError(null);
    setSaveOk(false);
    const body = {
      manifest: { ...(bundle.manifest as Record<string, unknown>), id: selectedId },
      units: bundle.units as DictEntityMap,
      territories: bundle.territories as DictEntityMap,
      factions: bundle.factions as DictEntityMap,
      camps: bundle.camps as DictEntityMap,
      ports: bundle.ports as DictEntityMap,
      starting_setup: bundle.starting_setup as Record<string, unknown>,
      specials: bundle.specials as Record<string, unknown>,
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

  const runLiveHotfixSpawn = async () => {
    const gid = liveHotfixGameId.trim();
    const tid = liveHotfixTerritoryId.trim();
    const uid = liveHotfixUnitId.trim();
    if (!gid || !tid || !uid) {
      setLiveHotfixError('Game id, territory, and unit are required.');
      return;
    }
    const n = Math.floor(Number(liveHotfixCount));
    if (!Number.isFinite(n) || n < 1 || n > 99) {
      setLiveHotfixError('Count must be between 1 and 99.');
      return;
    }
    setLiveHotfixBusy(true);
    setLiveHotfixError(null);
    setLiveHotfixOk(null);
    try {
      const r = await api.adminSpawnUnits(gid, { territory_id: tid, unit_id: uid, count: n });
      setLiveHotfixOk(
        `Spawned ${r.count}× ${r.unit_id} in ${r.territory_id} (${r.instance_ids.join(', ')})`,
      );
    } catch (e) {
      setLiveHotfixError(e instanceof Error ? e.message : 'Spawn failed');
    } finally {
      setLiveHotfixBusy(false);
    }
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
    if (!bundle) return null;
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
        case 'specials':
          return j(bundle.specials, (p) => setBundle((b) => (b ? { ...b, specials: p as typeof b.specials } : null)));
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
            onManifestChange={(m) =>
              setBundle((b) => (b ? { ...b, manifest: { ...m, id: selectedId } as typeof b.manifest } : null))
            }
          />
        );
      case 'units':
        return (
          <UnitsPanel
            units={(bundle.units as DictEntityMap) ?? {}}
            onChange={(next) => setBundle((b) => (b ? { ...b, units: next as typeof b.units } : null))}
          />
        );
      case 'territories':
        return (
          <TerritoriesPanel
            territories={(bundle.territories as DictEntityMap) ?? {}}
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
      case 'specials':
        return (
          <SpecialsPanel
            specials={bundle.specials as Record<string, unknown>}
            onChange={(sp) => setBundle((b) => (b ? { ...b, specials: sp as typeof b.specials } : null))}
          />
        );
      default:
        return null;
    }
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
      </div>

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

      <section className="admin-page__live-hotfix" aria-labelledby="admin-live-hotfix-title">
        <h2 id="admin-live-hotfix-title" className="admin-page__live-hotfix-title">
          Live game hotfix
        </h2>
        <p className="admin-form__micro">
          Spawn units directly into a running game&apos;s territory (persists immediately). Faction comes from the unit
          definition (e.g. nazgul → mordor).
        </p>
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-hotfix-game">
            Game id
          </label>
          <input
            id="admin-hotfix-game"
            className="admin-form__input"
            autoComplete="off"
            spellCheck={false}
            value={liveHotfixGameId}
            onChange={(e) => setLiveHotfixGameId(e.target.value)}
          />
        </div>
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-hotfix-territory">
            Territory id
          </label>
          <input
            id="admin-hotfix-territory"
            className="admin-form__input"
            autoComplete="off"
            spellCheck={false}
            value={liveHotfixTerritoryId}
            onChange={(e) => setLiveHotfixTerritoryId(e.target.value)}
          />
        </div>
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-hotfix-unit">
            Unit id
          </label>
          <input
            id="admin-hotfix-unit"
            className="admin-form__input"
            autoComplete="off"
            spellCheck={false}
            value={liveHotfixUnitId}
            onChange={(e) => setLiveHotfixUnitId(e.target.value)}
          />
        </div>
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-hotfix-count">
            Count
          </label>
          <input
            id="admin-hotfix-count"
            type="number"
            min={1}
            max={99}
            className="admin-form__input admin-form__input--narrow"
            value={liveHotfixCount}
            onChange={(e) => setLiveHotfixCount(Number(e.target.value))}
          />
        </div>
        <div className="admin-page__live-hotfix-actions">
          <button
            type="button"
            className="admin-page__btn admin-page__btn--primary"
            data-admin-major-sfx
            disabled={liveHotfixBusy}
            onClick={runLiveHotfixSpawn}
          >
            {liveHotfixBusy ? 'Spawning…' : 'Spawn units'}
          </button>
        </div>
        {liveHotfixError ? <div className="admin-page__error">{liveHotfixError}</div> : null}
        {liveHotfixOk ? <p className="admin-page__success">{liveHotfixOk}</p> : null}
      </section>

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

      {setups.length === 0 && !loadError && !loadingBundle ? (
        <p className="admin-page__empty">
          No setups in the database. Restart the API once so it can create the <code>setups</code> table and seed from{' '}
          <code>backend/data/setups</code> (only runs when the table is empty). Then use <strong>New setup</strong> to add
          one.
        </p>
      ) : null}

      {bundle ? (
        <>
          <div className="admin-page__tabs" role="tablist">
            {TAB_KEYS.map((k) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={activeTab === k}
                className={`admin-page__tab${activeTab === k ? ' admin-page__tab--active' : ''}`}
                onClick={() => setActiveTab(k)}
              >
                {TAB_LABELS[k]}
              </button>
            ))}
          </div>
          <div className="admin-page__panel">{renderTabBody()}</div>
        </>
      ) : null}

      <div className="admin-page__actions">
        <button
          type="button"
          className="admin-page__btn admin-page__btn--primary"
          disabled={!bundle || saving}
          onClick={handleSave}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button"
          className="admin-page__btn admin-page__btn--danger"
          disabled={!bundle || saving || deleting}
          onClick={openDeleteDialog}
        >
          Delete setup
        </button>
      </div>

      {saveError ? <div className="admin-page__error">{saveError}</div> : null}
      {saveOk ? <p className="admin-page__success">Saved. New games will use this data.</p> : null}

      <CreateSetupDialog open={createOpen} onClose={() => setCreateOpen(false)} setups={setups} onCreated={onCreatedSetup} />
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
    </div>
  );
}
