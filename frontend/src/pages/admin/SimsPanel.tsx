import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AdminSetupListItem, AdminSimRun, AiLevelChoice } from '../../services/api';

type Props = {
  setups: AdminSetupListItem[];
  onError: (message: string) => void;
};

const MATCHUPS = [
  { id: 'mcts-vs-mcts', label: 'MCTS vs MCTS' },
  { id: 'mcts-vs-heuristic', label: 'MCTS vs heuristic' },
  { id: 'heuristic-vs-mcts', label: 'Heuristic vs MCTS' },
  { id: 'heuristic-vs-heuristic', label: 'Heuristic vs heuristic' },
];

export function SimsPanel({ setups, onError }: Props) {
  const [runs, setRuns] = useState<AdminSimRun[]>([]);
  const [setupId, setSetupId] = useState(setups[0]?.id ?? '');
  const [difficulty, setDifficulty] = useState('easy');
  const [matchup, setMatchup] = useState('mcts-vs-mcts');
  const [cycles, setCycles] = useState(1);
  const [running, setRunning] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [levels, setLevels] = useState<AiLevelChoice[]>([
    { id: 'easy', name: 'Easy' },
    { id: 'normal', name: 'Normal' },
    { id: 'hard', name: 'Hard' },
  ]);

  useEffect(() => {
    if (!setupId && setups[0]) setSetupId(setups[0].id);
  }, [setupId, setups]);

  useEffect(() => {
    api.adminListSims()
      .then((res) => setRuns(res.runs ?? []))
      .catch((e) => onError(e instanceof Error ? e.message : 'Could not load sims'));
    api.adminGetAi()
      .then((res) => {
        if (res.levels?.length) {
          setLevels(res.levels.map((level) => ({ id: level.id, name: level.name })));
          setDifficulty((current) => (
            res.levels.some((level) => level.id === current) ? current : res.levels[0].id
          ));
        }
      })
      .catch(() => {});
  }, []);

  const selected = runs.find((run) => run.id === selectedId) ?? runs[0] ?? null;

  return (
    <div className="admin-page__ai">
      <p className="admin-page__hint">
        Runs one short computer-vs-computer game for a scenario (at most two full turn cycles) and
        keeps the report here. Easy difficulty finishes fastest.
      </p>
      <div className="admin-page__toolbar admin-page__toolbar--wrap">
        <label className="admin-page__field">
          <span className="admin-page__field-label">Scenario</span>
          <select className="admin-page__select" value={setupId} onChange={(e) => setSetupId(e.target.value)}>
            {setups.map((setup) => (
              <option key={setup.id} value={setup.id}>
                {setup.display_name} ({setup.id})
              </option>
            ))}
          </select>
        </label>
        <label className="admin-page__field">
          <span className="admin-page__field-label">Difficulty</span>
          <select className="admin-page__select" value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
            {levels.map((level) => (
              <option key={level.id} value={level.id}>
                {level.name}
              </option>
            ))}
          </select>
        </label>
        <label className="admin-page__field">
          <span className="admin-page__field-label">Matchup</span>
          <select className="admin-page__select" value={matchup} onChange={(e) => setMatchup(e.target.value)}>
            {MATCHUPS.map((row) => (
              <option key={row.id} value={row.id}>
                {row.label}
              </option>
            ))}
          </select>
        </label>
        <label className="admin-page__field">
          <span className="admin-page__field-label">Cycles</span>
          <select
            className="admin-page__select"
            value={cycles}
            onChange={(e) => setCycles(Number(e.target.value))}
          >
            <option value={1}>1</option>
            <option value={2}>2</option>
          </select>
        </label>
        <button
          type="button"
          className="admin-page__btn admin-page__btn--primary"
          disabled={running || !setupId}
          onClick={() => {
            setRunning(true);
            api.adminRunSim({ setup_id: setupId, difficulty, matchup, max_cycles: cycles })
              .then((res) => {
                setRuns(res.runs ?? []);
                if (res.run?.id) setSelectedId(res.run.id);
                if (res.run?.error) onError(res.run.error);
              })
              .catch((e) => onError(e instanceof Error ? e.message : 'Sim failed'))
              .finally(() => setRunning(false));
          }}
        >
          {running ? 'Running…' : 'Run sim'}
        </button>
      </div>
      {runs.length === 0 ? <p className="admin-page__empty">No sims yet.</p> : null}
      {runs.length > 0 ? (
        <div className="admin-page__sim-layout">
          <ul className="admin-page__sim-list">
            {runs.map((run) => (
              <li key={run.id ?? run.created_at}>
                <button
                  type="button"
                  className={`page-menu-btn${selected?.id === run.id ? ' admin-page__nav-btn--active' : ''}`}
                  onClick={() => run.id && setSelectedId(run.id)}
                >
                  {(run.setup_id || 'sim')} · {run.matchup || 'matchup'} · {run.winner || run.leader || run.error || 'capped'}
                </button>
              </li>
            ))}
          </ul>
          {selected ? (
            <pre className="admin-page__sim-report">{selected.report || selected.error || 'No report'}</pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
