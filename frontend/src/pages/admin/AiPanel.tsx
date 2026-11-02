import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AdminAiSettings } from '../../services/api';

type Props = {
  onSaved: () => void;
  onError: (message: string) => void;
};

export function AiPanel({ onSaved, onError }: Props) {
  const [settings, setSettings] = useState<AdminAiSettings | null>(null);
  const [difficulty, setDifficulty] = useState('normal');
  const [algorithm, setAlgorithm] = useState('heuristic');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api.adminGetAi()
      .then((res) => {
        if (cancelled) return;
        setSettings(res);
        setDifficulty(res.difficulty);
        setAlgorithm(res.algorithm);
      })
      .catch((e) => {
        if (!cancelled) onError(e instanceof Error ? e.message : 'Could not load AI settings');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading || !settings) {
    return <p className="admin-page__empty">Loading AI settings…</p>;
  }

  return (
    <div className="admin-page__ai">
      <p className="admin-page__hint">
        These knobs are copied onto each new game and stored in that game&apos;s saved state, so later
        stats can tell how strong the computer was. Games already in progress keep the strength they
        were created with.
      </p>
      <label className="admin-page__field">
        <span className="admin-page__field-label">Difficulty</span>
        <select className="admin-page__select" value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
          {settings.difficulties.map((row) => (
            <option key={row.id} value={row.id}>
              {row.id} ({row.iterations} iterations / {row.seconds}s)
            </option>
          ))}
        </select>
      </label>
      <label className="admin-page__field">
        <span className="admin-page__field-label">Algorithm</span>
        <select className="admin-page__select" value={algorithm} onChange={(e) => setAlgorithm(e.target.value)}>
          {settings.algorithms.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="admin-page__btn admin-page__btn--primary"
        onClick={() => {
          api.adminPutAi({ difficulty, algorithm })
            .then((res) => {
              setSettings(res);
              setDifficulty(res.difficulty);
              setAlgorithm(res.algorithm);
              onSaved();
            })
            .catch((e) => onError(e instanceof Error ? e.message : 'Save failed'));
        }}
      >
        Save AI defaults
      </button>
    </div>
  );
}
