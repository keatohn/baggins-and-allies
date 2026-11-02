import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AdminAiLevel } from '../../services/api';

type Props = {
  onSaved: () => void;
  onError: (message: string) => void;
};

function uniqueLevelId(name: string, used: string[]): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24) || 'level';
  let id = base;
  let number = 2;
  const taken = new Set(used);
  while (taken.has(id)) {
    const suffix = `_${number++}`;
    id = `${base.slice(0, 32 - suffix.length)}${suffix}`;
  }
  return id;
}

export function AiPanel({ onSaved, onError }: Props) {
  const [algorithms, setAlgorithms] = useState<string[]>(['heuristic', 'mcts']);
  const [difficulty, setDifficulty] = useState('normal');
  const [algorithm, setAlgorithm] = useState('heuristic');
  const [levels, setLevels] = useState<AdminAiLevel[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.adminGetAi()
      .then((res) => {
        if (cancelled) return;
        setAlgorithms(res.algorithms?.length ? res.algorithms : ['heuristic', 'mcts']);
        setDifficulty(res.difficulty);
        setAlgorithm(res.algorithm);
        setLevels(res.levels ?? []);
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

  const updateLevel = (index: number, patch: Partial<AdminAiLevel>) => {
    setLevels((current) => current.map((level, i) => (i === index ? { ...level, ...patch } : level)));
  };

  const moveLevel = (index: number, direction: -1 | 1) => {
    setLevels((current) => {
      const next = index + direction;
      if (next < 0 || next >= current.length) return current;
      const copy = [...current];
      const [row] = copy.splice(index, 1);
      copy.splice(next, 0, row);
      return copy;
    });
  };

  const removeLevel = (index: number) => {
    setLevels((current) => {
      if (current.length <= 1) return current;
      const removed = current[index];
      const remaining = current.filter((_, i) => i !== index);
      if (removed && difficulty === removed.id) {
        setDifficulty(remaining[Math.max(0, index - 1)]?.id ?? remaining[0].id);
      }
      return remaining;
    });
  };

  const addLevel = () => {
    setLevels((current) => {
      if (current.length >= 8) return current;
      const names = new Set(current.map((level) => level.name.toLowerCase()));
      let name = 'New level';
      let number = 2;
      while (names.has(name.toLowerCase())) name = `New level ${number++}`;
      const last = current[current.length - 1];
      return [
        ...current,
        {
          id: uniqueLevelId(name, current.map((level) => level.id)),
          name,
          iterations: last?.iterations ?? 500,
          seconds: last?.seconds ?? 0.5,
        },
      ];
    });
  };

  if (loading) {
    return <p className="admin-page__empty">Loading AI settings…</p>;
  }

  return (
    <div className="admin-page__ai">
      <p className="admin-page__hint">
        Levels are listed from easiest to hardest. Search stops at the step count or the time cap,
        whichever comes first, and that same cap is used for every decision. New games copy the
        algorithm and the level marked for new games. Changing a level&apos;s steps or time changes
        every game that uses that level, including games already started. Renaming only changes the
        name players see.
      </p>
      <label className="admin-page__field">
        <span className="admin-page__field-label">Algorithm for new games</span>
        <select className="admin-page__select" value={algorithm} onChange={(e) => setAlgorithm(e.target.value)}>
          {algorithms.map((name) => (
            <option key={name} value={name}>
              {name === 'mcts' ? 'MCTS' : name === 'heuristic' ? 'Heuristic' : name}
            </option>
          ))}
        </select>
      </label>
      <p className="admin-page__hint">
        Single-player lobbies show these level names under a faction when that faction is the computer
        and new games are set to MCTS.
      </p>
      <div className="admin-ai-levels" role="list">
        {levels.map((level, index) => (
          <div key={level.id} className="admin-ai-levels__row" role="listitem">
            <label className="admin-ai-levels__default">
              <input
                type="radio"
                name="ai-default-level"
                checked={difficulty === level.id}
                onChange={() => setDifficulty(level.id)}
              />
              <span>New games</span>
            </label>
            <label className="admin-page__field">
              <span className="admin-page__field-label">Name</span>
              <input
                className="admin-form__input"
                value={level.name}
                maxLength={24}
                onChange={(e) => updateLevel(index, { name: e.target.value })}
              />
            </label>
            <label className="admin-page__field">
              <span className="admin-page__field-label">Steps</span>
              <input
                className="admin-form__input"
                type="number"
                min={1}
                max={20000}
                step={1}
                value={level.iterations}
                onChange={(e) => {
                  const value = Number(e.target.value);
                  if (Number.isFinite(value)) updateLevel(index, { iterations: Math.round(value) });
                }}
              />
            </label>
            <label className="admin-page__field">
              <span className="admin-page__field-label">Seconds</span>
              <input
                className="admin-form__input"
                type="number"
                min={0.05}
                max={30}
                step={0.05}
                value={level.seconds}
                onChange={(e) => {
                  const value = Number(e.target.value);
                  if (Number.isFinite(value)) updateLevel(index, { seconds: value });
                }}
              />
            </label>
            <div className="admin-ai-levels__actions">
              <button
                type="button"
                className="admin-page__btn"
                disabled={index === 0}
                onClick={() => moveLevel(index, -1)}
                aria-label={`Move ${level.name} toward easier`}
              >
                Up
              </button>
              <button
                type="button"
                className="admin-page__btn"
                disabled={index === levels.length - 1}
                onClick={() => moveLevel(index, 1)}
                aria-label={`Move ${level.name} toward harder`}
              >
                Down
              </button>
              <button
                type="button"
                className="admin-page__btn"
                disabled={levels.length <= 1}
                onClick={() => removeLevel(index)}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
      <div className="admin-ai-levels__footer">
        <button type="button" className="admin-page__btn" disabled={levels.length >= 8} onClick={addLevel}>
          Add level
        </button>
        <button
          type="button"
          className="admin-page__btn admin-page__btn--primary"
          disabled={saving || levels.length === 0}
          onClick={() => {
            setSaving(true);
            api.adminPutAi({ difficulty, algorithm, levels })
              .then((res) => {
                setDifficulty(res.difficulty);
                setAlgorithm(res.algorithm);
                setLevels(res.levels ?? []);
                onSaved();
              })
              .catch((e) => onError(e instanceof Error ? e.message : 'Save failed'))
              .finally(() => setSaving(false));
          }}
        >
          {saving ? 'Saving…' : 'Save AI settings'}
        </button>
      </div>
    </div>
  );
}
