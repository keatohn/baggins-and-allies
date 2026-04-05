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

type DictEntityMap = Record<string, Record<string, unknown>>;

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
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setNewId('');
      setDuplicateFrom('');
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
    setBusy(true);
    setErr(null);
    try {
      await api.adminCreateSetup({
        id,
        duplicate_from: duplicateFrom.trim() || null,
      });
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
        className="admin-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-create-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="admin-create-title" className="admin-modal__title">
          New setup
        </h2>
        <p className="admin-form__micro">Setup id cannot be changed after creation. It must be unique.</p>
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
        <div className="admin-form__row">
          <label className="admin-form__label" htmlFor="admin-dup-from">
            Start from
          </label>
          <select
            id="admin-dup-from"
            className="admin-page__select"
            value={duplicateFrom}
            onChange={(e) => setDuplicateFrom(e.target.value)}
          >
            <option value="">Empty draft (inactive, empty maps)</option>
            {setups.map((s) => (
              <option key={s.id} value={s.id}>
                Copy of {s.display_name} ({s.id})
              </option>
            ))}
          </select>
        </div>
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
      </div>

      {saveError ? <div className="admin-page__error">{saveError}</div> : null}
      {saveOk ? <p className="admin-page__success">Saved. New games will use this data.</p> : null}

      <CreateSetupDialog open={createOpen} onClose={() => setCreateOpen(false)} setups={setups} onCreated={onCreatedSetup} />
    </div>
  );
}
