import { useEffect, useMemo, useState } from 'react';
import { api } from '../../services/api';
import type { AdminGameAssignments } from '../../services/api';

export default function GameAssignmentsEditor({ gameId, onClose, onSaved }: {
  gameId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [document, setDocument] = useState<AdminGameAssignments | null>(null);
  const [assignments, setAssignments] = useState<Record<string, string | null>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.adminGetGameAssignments(gameId).then((data) => {
      if (cancelled) return;
      setDocument(data);
      setAssignments(data.assignments);
    }).catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load assignments'); });
    return () => { cancelled = true; };
  }, [gameId]);
  const alliances = useMemo(() => [...new Set(document?.factions.map((faction) => faction.alliance) ?? [])], [document]);
  const playerAlliances = useMemo(() => {
    const result = new Map<string, Set<string>>();
    for (const faction of document?.factions ?? []) {
      const player = assignments[faction.id];
      if (!player || player === 'computer') continue;
      if (!result.has(player)) result.set(player, new Set());
      result.get(player)!.add(faction.alliance);
    }
    return result;
  }, [document, assignments]);
  const conflict = [...playerAlliances.values()].some((values) => values.size > 1);
  const save = async () => {
    if (busy || conflict || !document) return;
    setBusy(true);
    setError(null);
    try { await api.adminPutGameAssignments(gameId, assignments); onSaved(); }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not save assignments'); }
    finally { setBusy(false); }
  };
  return (
    <div className="admin-modal-overlay admin-graph__confirm" role="presentation" onClick={() => { if (!busy) onClose(); }}>
      <div className="admin-modal admin-assignments" role="dialog" aria-modal="true" aria-labelledby="admin-assignments-title" onClick={(event) => event.stopPropagation()}>
        <h2 id="admin-assignments-title" className="admin-modal__title">Assign — {document?.name ?? 'Loading…'}</h2>
        <p className="admin-form__micro">Assign each faction to a player or Computer. Players can have multiple factions within one alliance. Historical records only; live control, XP, and forfeited status stay unchanged.</p>
        {!document && !error ? <p role="status">Loading assignments…</p> : null}
        {alliances.map((alliance) => (
          <fieldset key={alliance} className="admin-assignments__alliance" disabled={busy}>
            <legend>{alliance === 'good' ? 'Good' : alliance === 'evil' ? 'Evil' : alliance}</legend>
            {document?.factions.filter((faction) => faction.alliance === alliance).map((faction) => (
              <label key={faction.id} className="admin-assignments__row">
                <span>{faction.name}</span>
                <select className="admin-page__select" value={assignments[faction.id] ?? ''} onChange={(event) => setAssignments((current) => ({ ...current, [faction.id]: event.target.value || null }))}>
                  <option value="">Unassigned</option>
                  <option value="computer">Computer</option>
                  {document.players.map((player) => <option key={player.id} value={player.id} disabled={[...(playerAlliances.get(player.id) ?? [])].some((other) => other !== alliance)}>{player.name}{player.forfeited ? ' (forfeited)' : ''}</option>)}
                </select>
              </label>
            ))}
          </fieldset>
        ))}
        {conflict ? <p className="admin-page__error" role="alert">A human player cannot be assigned to both alliances. Reassign their factions on one side before saving.</p> : null}
        {error ? <p className="admin-page__error" role="alert">{error}</p> : null}
        <div className="admin-modal__actions">
          <button type="button" className="admin-page__btn" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="button" className="admin-page__btn admin-page__btn--primary" disabled={busy || conflict || !document} onClick={save}>{busy ? 'Saving…' : 'Save assignments'}</button>
        </div>
      </div>
    </div>
  );
}
