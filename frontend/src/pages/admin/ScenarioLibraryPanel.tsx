import { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { ScenarioImpact, ScenarioLibrary } from '../../services/api';
import { FactionsPanel, TerritoriesPanel, UnitsPanel } from './SetupEditorPanels';

const TABS = ['units', 'factions', 'territories'] as const;
const LABELS = { units: 'Units', factions: 'Factions', territories: 'Territories' };

/** All seed types share the admin modal, tabs, entity editors, and actions. */
export function ScenarioLibraryPanel({ onClose, onSaved }: { onClose: () => void; onSaved?: () => void }) {
  const [library, setLibrary] = useState<ScenarioLibrary | null>(null);
  const [original, setOriginal] = useState('');
  const [tab, setTab] = useState<(typeof TABS)[number]>('units');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [impact, setImpact] = useState<ScenarioImpact | null>(null);
  const [saved, setSaved] = useState(false);
  const [archetypes, setArchetypes] = useState<string[]>([]);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dirty = !!library && JSON.stringify(library) !== original;

  useEffect(() => {
    let active = true;
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    api.adminGetScenarioLibrary().then((value) => {
      if (active) { setLibrary(value); setOriginal(JSON.stringify(value)); }
    }).catch((e: Error) => { if (active) setError(e.message); });
    api.getCatalog().then((value) => { if (active) setArchetypes(value.archetypes); }).catch(() => {});
    return () => { active = false; previousFocus?.focus(); };
  }, []);

  const requestClose = () => {
    if (!busy && (!dirty || window.confirm('Discard unsaved seed changes?'))) onClose();
  };
  const change = (next: ScenarioLibrary) => {
    setLibrary(next); setImpact(null); setSaved(false); setError('');
  };
  const preview = async () => {
    if (!library) return;
    setBusy(true); setError('');
    try { setImpact(await api.adminPreviewScenarioLibrary(library)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!library) return;
    setBusy(true); setError('');
    try {
      const result = await api.adminSaveScenarioLibrary(library);
      const current = await api.adminGetScenarioLibrary();
      setLibrary(current); setOriginal(JSON.stringify(current));
      setImpact(result); setSaved(true); onSaved?.();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const exportState = async () => {
    setBusy(true); setError('');
    try {
      const state = await api.adminExportScenarios();
      const url = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url; link.download = 'scenario-authoring.json'; link.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={requestClose} onKeyDown={(e) => {
      if (e.key === 'Escape') { e.stopPropagation(); requestClose(); }
    }}>
      <div className="admin-modal admin-modal--seeds" role="dialog" aria-modal="true" aria-labelledby="admin-seeds-title" onClick={(e) => e.stopPropagation()}>
        <div className="balance-modal__header">
          <h2 id="admin-seeds-title" className="admin-modal__title">Seeds</h2>
          <button ref={closeRef} type="button" className="admin-page__btn" disabled={busy} onClick={requestClose}>Close</button>
        </div>
        <div className="admin-page__subtabs" role="tablist" aria-label="Seed types">
          {TABS.map((key) => <button key={key} type="button" role="tab" aria-selected={tab === key}
            className={`admin-page__tab${tab === key ? ' admin-page__tab--active' : ''}`} onClick={() => setTab(key)}>{LABELS[key]}</button>)}
        </div>
        <p className="admin-form__micro">{tab === 'territories'
          ? 'Territory IDs automatically match regions in every map. Terrain and connections stay in Maps; scenario overrides take precedence.'
          : 'Shared defaults for scenarios. Explicit scenario overrides take precedence.'}</p>
        {error && <p role="alert" className="admin-page__error">{error}</p>}
        {!library && !error && <p className="admin-form__micro">Loading seeds…</p>}
        {library && <fieldset disabled={busy} className="scenario-library__body" role="tabpanel" aria-label={`${LABELS[tab]} seeds`}>
          {tab === 'units' ? <UnitsPanel units={library.units} factionIds={Object.keys(library.factions)} archetypes={archetypes} setupId="" setups={[]} onChange={(units) => change({ ...library, units })} />
            : tab === 'factions' ? <FactionsPanel factions={library.factions} onChange={(factions) => change({ ...library, factions })} />
            : <TerritoriesPanel seedMode territories={library.territories} terrainTypes={[]} onChange={(territories) => change({ ...library, territories })} />}
        </fieldset>}
        {impact && <div className="scenario-library__review" role="status">
          <p className="admin-form__micro">{saved ? 'Saved. ' : ''}{impact.changed_setups.length ? `Affected scenarios: ${impact.changed_setups.join(', ')}` : 'No scenario definition changes.'}</p>
          <details><summary>Changes</summary><pre>{JSON.stringify(impact.changes, null, 2)}</pre></details>
        </div>}
        <div className="admin-modal__actions">
          <button type="button" className="admin-page__btn" disabled={busy || !library} onClick={preview}>Review</button>
          <button type="button" className="admin-page__btn admin-page__btn--primary" disabled={busy || !impact || saved} onClick={save}>Save and Rebuild</button>
          <button type="button" className="admin-page__btn" disabled={busy || !library} onClick={exportState}>Export</button>
        </div>
        <p className="admin-form__micro">All tabs share pending changes. Review shows affected scenarios without saving. Save and Rebuild validates and saves seed changes and affected scenario definitions together. Scenario overrides, visibility, and games in progress are preserved. Export downloads the saved authoring data.</p>
      </div>
    </div>
  );
}
