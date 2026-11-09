import { useEffect, useState } from 'react';
import ScenarioTimeline from '../../components/ScenarioTimeline';
import { api } from '../../services/api';
import type { ScenarioValidation, SetupInfo } from '../../services/api';

/** JSON is valid YAML; authors can paste either format into this source editor. */
export function ScenarioSourceDialog({ setupId, onClose, onSaved }: { setupId: string; onClose: () => void; onSaved: () => void }) {
  const [text, setText] = useState('');
  const [validation, setValidation] = useState<ScenarioValidation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [original, setOriginal] = useState('');
  useEffect(() => {
    let active = true;
    api.adminGetScenarioSource(setupId).then((value) => { if (active) { const initial = JSON.stringify(value.source, null, 2); setText(initial); setOriginal(initial); } }).catch((e: Error) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [setupId]);
  const review = async () => {
    setBusy(true); setError('');
    try { setValidation(await api.adminValidateScenario(text)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true); setError('');
    try { await api.adminSaveScenarioSource(setupId, text); onSaved(); setSaved(true); setOriginal(text); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/yaml' }));
    const link = document.createElement('a'); link.href = url; link.download = `${setupId}.yaml`; link.click(); URL.revokeObjectURL(url);
  };
  const bundle = validation?.bundle;
  const owners = (bundle?.starting_setup.territory_owners ?? {}) as Record<string, string>;
  return <div className="admin-modal-overlay" role="presentation"><div className="admin-modal scenario-source" role="dialog" aria-modal="true" aria-label="Scenario source">
    <h2>Scenario source</h2><p>Edit or paste YAML / JSON, review the compiled scenario, then save. Set manifest.is_active to false for a draft, "admin" for admin preview, or true to publish.</p>
    <textarea aria-label="Declarative scenario source" spellCheck={false} rows={20} value={text} onChange={(e) => { setText(e.target.value); setValidation(null); setSaved(false); }} />
    <div className="scenario-toolbar"><button type="button" disabled={busy || !text} onClick={review}>Validate & preview</button><button type="button" disabled={busy || !validation?.valid || saved} onClick={save}>Save scenario</button><button type="button" onClick={download}>Download source</button><button type="button" disabled={busy} onClick={() => { if (text === original || window.confirm('Discard unsaved scenario source changes?')) onClose(); }}>Close</button></div>
    {error && <p role="alert">{error}</p>}{saved && <p role="status">Scenario saved.</p>}
    {validation && <div role="status">{validation.valid ? 'Validation passed.' : <ul>{validation.errors.map((value) => <li key={value}>{value}</li>)}</ul>}</div>}
    {bundle && <>
      <ScenarioTimeline scenarios={[{ ...bundle.manifest, id: String(bundle.manifest.id), display_name: String(bundle.manifest.display_name ?? bundle.manifest.id) } as SetupInfo]} selectedId={String(bundle.manifest.id)} onSelect={() => {}} onConfirm={() => {}} />
      <details open><summary>Starting positions</summary><table><thead><tr><th>Territory</th><th>Owner</th><th>Units</th></tr></thead><tbody>{Object.keys(bundle.territories).map((id) => <tr key={id}><td>{id}</td><td>{owners[id] ?? '—'}</td><td><code>{JSON.stringify((bundle.starting_setup.starting_units as Record<string, unknown> | undefined)?.[id] ?? {})}</code></td></tr>)}</tbody></table></details>
      <details><summary>Compiled master JSON</summary><pre>{JSON.stringify(bundle, null, 2)}</pre></details>
    </>}
  </div></div>;
}
