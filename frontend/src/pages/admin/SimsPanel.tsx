import { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { AdminSetupListItem, AdminSimRun, AiFactionSetting, AiLevelChoice } from '../../services/api';

type Props = {
  setups: AdminSetupListItem[];
  onError: (message: string) => void;
};

const WEIGHT_FIELDS: { key: keyof AiFactionSetting['weights']; label: string; max?: number }[] = [
  { key: 'material', label: 'Material' },
  { key: 'territory', label: 'Territory' },
  { key: 'stronghold', label: 'Stronghold' },
  { key: 'power_prod', label: 'Power' },
  { key: 'board_scale', label: 'Scale' },
  { key: 'purchase_defense', label: 'Purchase defense', max: 1 },
];

const DEFAULT_WEIGHTS: AiFactionSetting['weights'] = {
  material: 1,
  territory: 1,
  stronghold: 3,
  power_prod: 0.5,
  board_scale: 1,
  purchase_defense: 0.5,
};

const ALLIANCE_ORDER = ['good', 'evil'];
const MAX_TURNS = 10;

type SimFaction = {
  id: string;
  name: string;
  alliance: string;
};

function blankSetting(difficulty: string, algorithm: string): AiFactionSetting {
  return { difficulty, algorithm, weights: { ...DEFAULT_WEIGHTS } };
}

function allianceLabel(alliance: string): string {
  if (alliance === 'good') return 'Good';
  if (alliance === 'evil') return 'Evil';
  if (!alliance || alliance === 'neutral') return 'Neutral';
  return alliance.charAt(0).toUpperCase() + alliance.slice(1);
}

function factionsFromBundle(factions: Record<string, unknown> | undefined): SimFaction[] {
  const rows: SimFaction[] = [];
  for (const [id, raw] of Object.entries(factions ?? {})) {
    const row = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
    const name = typeof row.display_name === 'string' && row.display_name.trim()
      ? row.display_name
      : typeof row.name === 'string' && row.name.trim()
        ? row.name
        : id;
    const alliance = typeof row.alliance === 'string' ? row.alliance.trim().toLowerCase() : '';
    if (!alliance || alliance === 'neutral') continue;
    rows.push({ id, name, alliance });
  }
  return rows.sort((a, b) => {
    const ai = ALLIANCE_ORDER.indexOf(a.alliance);
    const bi = ALLIANCE_ORDER.indexOf(b.alliance);
    const order = (ai === -1 ? ALLIANCE_ORDER.length : ai) - (bi === -1 ? ALLIANCE_ORDER.length : bi);
    if (order !== 0) return order;
    return a.name.localeCompare(b.name);
  });
}

function runLabel(run: AdminSimRun): string {
  const factions = run.factions ?? {};
  const ids = Object.keys(factions);
  let policy = run.matchup || 'sim';
  if (ids.length > 0) {
    const algorithms = new Set(ids.map((id) => factions[id]?.algorithm || 'mcts'));
    policy = algorithms.size === 1 ? [...algorithms][0] : 'mixed';
  }
  return `${run.setup_id || 'sim'} · ${policy} · ${run.winner || run.leader || run.error || 'capped'}`;
}

export function SimsPanel({ setups, onError }: Props) {
  const [runs, setRuns] = useState<AdminSimRun[]>([]);
  const [setupId, setSetupId] = useState(setups[0]?.id ?? '');
  const [factions, setFactions] = useState<SimFaction[]>([]);
  const [settings, setSettings] = useState<Record<string, AiFactionSetting>>({});
  const [turns, setTurns] = useState(3);
  const [running, setRunning] = useState(false);
  const [loadedSetupId, setLoadedSetupId] = useState<string | null>(null);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [levels, setLevels] = useState<AiLevelChoice[]>([
    { id: 'easy', name: 'Easy' },
    { id: 'normal', name: 'Normal' },
    { id: 'hard', name: 'Hard' },
  ]);
  const [defaultDifficulty, setDefaultDifficulty] = useState('normal');
  const [defaultAlgorithm, setDefaultAlgorithm] = useState('heuristic');

  const scenarioId = setupId || setups[0]?.id || '';
  const loadingSetup = Boolean(scenarioId) && loadedSetupId !== scenarioId;

  useEffect(() => {
    api.adminListSims()
      .then((res) => setRuns(res.runs ?? []))
      .catch((e) => onErrorRef.current(e instanceof Error ? e.message : 'Could not load sims'));
  }, []);

  useEffect(() => {
    if (!scenarioId) return;
    let cancelled = false;
    const requestedId = scenarioId;
    Promise.all([api.adminGetSetup(requestedId), api.adminGetAi()])
      .then(([bundle, ai]) => {
        if (cancelled) return;
        const difficulty = ai.difficulty || 'normal';
        const algorithm = ai.algorithm || 'heuristic';
        setDefaultDifficulty(difficulty);
        setDefaultAlgorithm(algorithm);
        if (ai.levels?.length) {
          setLevels(ai.levels.map((level) => ({ id: level.id, name: level.name })));
        }
        const nextFactions = factionsFromBundle(bundle.factions);
        setFactions(nextFactions);
        setSettings(Object.fromEntries(
          nextFactions.map((faction) => [faction.id, blankSetting(difficulty, algorithm)]),
        ));
        setLoadedSetupId(requestedId);
      })
      .catch((e) => {
        if (cancelled) return;
        onErrorRef.current(e instanceof Error ? e.message : 'Could not load scenario');
        setLoadedSetupId(requestedId);
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioId]);

  const patchFaction = (
    factionId: string,
    patch: { difficulty?: string; algorithm?: string; weight?: { key: keyof AiFactionSetting['weights']; value: number } },
  ) => {
    setSettings((current) => {
      const row = current[factionId] ?? blankSetting(defaultDifficulty, defaultAlgorithm);
      return {
        ...current,
        [factionId]: {
          ...row,
          ...(patch.difficulty != null ? { difficulty: patch.difficulty } : {}),
          ...(patch.algorithm != null ? { algorithm: patch.algorithm } : {}),
          ...(patch.weight ? { weights: { ...row.weights, [patch.weight.key]: patch.weight.value } } : {}),
        },
      };
    });
  };

  const selected = runs.find((run) => run.id === selectedId) ?? runs[0] ?? null;
  const choices = levels.length > 0 ? levels : [{ id: 'normal', name: 'Normal' }];

  return (
    <div className="admin-page__ai">
      <p className="admin-page__hint">
        Runs one computer-vs-computer game and keeps the report here. Each faction has its own
        algorithm, the same way a computer-only lobby does. Difficulty and weights apply only when
        that faction uses MCTS. Heuristic has no difficulty.
      </p>
      <div className="admin-page__toolbar admin-page__toolbar--wrap">
        <label className="admin-page__field">
          <span className="admin-page__field-label">Scenario</span>
          <select className="admin-page__select" value={scenarioId} onChange={(e) => setSetupId(e.target.value)}>
            {setups.map((setup) => (
              <option key={setup.id} value={setup.id}>
                {setup.display_name} ({setup.id})
              </option>
            ))}
          </select>
        </label>
        <label className="admin-page__field">
          <span className="admin-page__field-label">Turns</span>
          <select
            className="admin-page__select"
            value={turns}
            onChange={(e) => setTurns(Number(e.target.value))}
          >
            {Array.from({ length: MAX_TURNS }, (_, index) => index + 1).map((count) => (
              <option key={count} value={count}>
                {count}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="admin-page__btn admin-page__btn--primary"
          disabled={running || loadingSetup || !scenarioId || factions.length === 0}
          onClick={() => {
            setRunning(true);
            api.adminRunSim({ setup_id: scenarioId, max_cycles: turns, factions: settings })
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
      <p className="admin-page__hint">
        Each turn is one pass through every faction. The sim stops after the chosen number of turns, or when someone wins.
      </p>
      {loadingSetup ? <p className="admin-page__empty">Loading factions…</p> : null}
      {!loadingSetup && factions.length > 0 ? (
        <div className="admin-sim-factions">
          {factions.map((faction) => {
            const setting = settings[faction.id] ?? blankSetting(defaultDifficulty, defaultAlgorithm);
            const known = choices.some((level) => level.id === setting.difficulty);
            const usesMcts = (setting.algorithm || 'mcts') === 'mcts';
            return (
              <section key={faction.id} className="admin-sim-faction">
                <h3 className="admin-sim-faction__name">
                  {faction.name}
                  <span className="admin-sim-faction__alliance">{allianceLabel(faction.alliance)}</span>
                </h3>
                <div className="admin-sim-faction__grid">
                  <label className="admin-page__field">
                    <span className="admin-page__field-label">Algorithm</span>
                    <select
                      className="admin-page__select"
                      value={setting.algorithm || 'mcts'}
                      onChange={(e) => patchFaction(faction.id, { algorithm: e.target.value })}
                    >
                      <option value="mcts">MCTS</option>
                      <option value="heuristic">Heuristic</option>
                    </select>
                    {usesMcts ? null : (
                      <span className="admin-sim-faction__heuristic">Heuristic has no difficulty.</span>
                    )}
                  </label>
                  {usesMcts ? (
                    <label className="admin-page__field">
                      <span className="admin-page__field-label">Difficulty</span>
                      <select
                        className="admin-page__select"
                        value={setting.difficulty}
                        onChange={(e) => patchFaction(faction.id, { difficulty: e.target.value })}
                      >
                        {!known && <option value={setting.difficulty}>{setting.difficulty}</option>}
                        {choices.map((level) => (
                          <option key={level.id} value={level.id}>
                            {level.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  {WEIGHT_FIELDS.map((field) => (
                    <label key={field.key} className="admin-page__field">
                      <span className="admin-page__field-label">{field.label}</span>
                      <input
                        className="admin-form__input"
                        type="number"
                        min={0}
                        max={field.max}
                        step={0.1}
                        value={setting.weights[field.key]}
                        disabled={!usesMcts}
                        onChange={(e) => {
                          const value = Number(e.target.value);
                          if (Number.isFinite(value)) {
                            patchFaction(faction.id, { weight: { key: field.key, value } });
                          }
                        }}
                      />
                    </label>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      ) : null}
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
                  {runLabel(run)}
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
