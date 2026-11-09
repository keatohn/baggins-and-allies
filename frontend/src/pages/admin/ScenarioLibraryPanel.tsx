import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { EntityRecords, ScenarioImpact, ScenarioLibrary } from '../../services/api';
import { FactionsPanel, UnitsPanel } from './SetupEditorPanels';

/** Shared editor used by Seeds and the Maps tab. Saves compiled scenarios atomically. */
export function ScenarioLibraryPanel({ mapBase, onSaved }: { mapBase?: string; onSaved?: () => void }) {
  const [library, setLibrary] = useState<ScenarioLibrary | null>(null);
  const [tab, setTab] = useState<'units' | 'factions'>('units');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [impact, setImpact] = useState<ScenarioImpact | null>(null);
  const [saved, setSaved] = useState(false);
  const [archetypes, setArchetypes] = useState<string[]>([]);
  const [terrainTypes, setTerrainTypes] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    api.adminGetScenarioLibrary().then((value) => { if (active) setLibrary(value); }).catch((e: Error) => { if (active) setError(e.message); });
    api.getCatalog().then((value) => { if (active) { setArchetypes(value.archetypes); setTerrainTypes(value.terrain_types); } }).catch(() => {});
    return () => { active = false; };
  }, []);
  const change = (next: ScenarioLibrary) => { setLibrary(next); setImpact(null); setSaved(false); setError(''); };
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
    try { setImpact(await api.adminSaveScenarioLibrary(library)); setSaved(true); onSaved?.(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const exportState = async () => {
    try {
      const state = await api.adminExportScenarios();
      const url = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a'); link.href = url; link.download = 'scenario-authoring.json'; link.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  return <div className="scenario-library">
    <p>{mapBase ? 'Map territory defaults. Scenario overrides take precedence. Terrain and connections are edited in Landscape and Graph.' : 'Shared unit and faction seeds. Editing a default updates scenarios that inherit it; explicit overrides stay in place.'}</p>
    {error && <p role="alert" className="admin-page__error">{error}</p>}
    {library && <fieldset disabled={busy} className="scenario-library__body">
      {mapBase ? <MapDefaults records={library.maps[mapBase] ?? {}} terrainTypes={terrainTypes} onChange={(records) => change({ ...library, maps: { ...library.maps, [mapBase]: records } })} /> : <>
        <div className="scenario-toolbar"><button type="button" aria-pressed={tab === 'units'} onClick={() => setTab('units')}>Unit seeds</button><button type="button" aria-pressed={tab === 'factions'} onClick={() => setTab('factions')}>Faction seeds</button></div>
        {tab === 'units' ? <UnitsPanel units={library.units} factionIds={Object.keys(library.factions)} archetypes={archetypes} setupId="" setups={[]} onChange={(units) => change({ ...library, units })} /> : <FactionsPanel factions={library.factions} onChange={(factions) => change({ ...library, factions })} />}
      </>}
      <div className="scenario-toolbar"><button type="button" onClick={preview}>Review changes</button><button type="button" disabled={!impact || saved} onClick={save}>Save defaults and rebuild</button><button type="button" onClick={exportState}>Export live authoring data</button></div>
    </fieldset>}
    {impact && <div role="status"><p>{saved ? 'Saved. ' : ''}{impact.changed_setups.length ? `Affected scenarios: ${impact.changed_setups.join(', ')}` : 'No compiled scenario changes.'}</p><details><summary>Compiled changes</summary><pre>{JSON.stringify(impact.changes, null, 2)}</pre></details></div>}
  </div>;
}

function MapDefaults({ records, terrainTypes, onChange }: { records: EntityRecords; terrainTypes: string[]; onChange: (v: EntityRecords) => void }) {
  const [selected, setSelected] = useState('');
  const [newId, setNewId] = useState('');
  const id = selected in records ? selected : Object.keys(records)[0] ?? '';
  const record = records[id];
  const patch = (value: Record<string, unknown>) => onChange({ ...records, [id]: { ...record, ...value } });
  const produces = record?.produces && typeof record.produces === 'object' ? record.produces as Record<string, unknown> : {};
  return <div>
    <label>Territory <select value={id} onChange={(e) => setSelected(e.target.value)}><option value="" disabled>Select a territory</option>{Object.keys(records).sort().map((key) => <option key={key}>{key}</option>)}</select></label>
    {record && <div className="scenario-map-fields">
      <label>Name <input value={String(record.display_name ?? '')} onChange={(e) => patch({ display_name: e.target.value })} /></label>
      <label>Image <input value={String(record.image ?? '')} onChange={(e) => patch({ image: e.target.value })} /></label>
      <label>Default power production <input type="number" min={0} value={Number(produces.power ?? 0)} onChange={(e) => patch({ produces: { ...produces, power: Math.max(0, Number(e.target.value)) } })} /></label>
      <label><input type="checkbox" checked={record.is_stronghold === true} onChange={(e) => patch({ is_stronghold: e.target.checked })} /> Stronghold</label>
      <label><input type="checkbox" checked={record.ownable !== false} onChange={(e) => patch({ ownable: e.target.checked })} /> Ownable</label>
    </div>}
    <div className="scenario-toolbar"><input placeholder="New territory id" aria-label="New map territory id" value={newId} onChange={(e) => setNewId(e.target.value)} /><button type="button" disabled={!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(newId) || newId in records} onClick={() => {
      onChange({ ...records, [newId]: { id: newId, display_name: newId, terrain_type: terrainTypes.find((value) => value !== 'sea') ?? 'plains', adjacent: [], ownable: true, is_stronghold: false, produces: { power: 0 } } }); setSelected(newId); setNewId('');
    }}>Add territory default</button></div>
  </div>;
}
