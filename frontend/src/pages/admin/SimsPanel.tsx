import { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { AdminAiLevel, AdminSetupListItem, AdminSimRun, AiFactionSetting } from '../../services/api';

type Props = {
  setups: AdminSetupListItem[];
  onError: (message: string) => void;
};

const WEIGHT_FIELDS: { key: keyof AiFactionSetting['weights']; label: string; max?: number }[] = [
  { key: 'units', label: 'Units' },
  { key: 'stronghold', label: 'Stronghold' },
  { key: 'capital', label: 'Capital' },
  { key: 'power_prod', label: 'Power' },
  { key: 'board_scale', label: 'Scale' },
  { key: 'purchase_attack', label: 'Purchase attack', max: 1 },
];

const DEFAULT_WEIGHTS: AiFactionSetting['weights'] = {
  units: 1,
  stronghold: 3,
  capital: 3,
  power_prod: 0.5,
  board_scale: 1,
  purchase_attack: 0.5,
};

const STYLE_FIELDS: { key: 'randomness' | 'aggression' | 'purchase_variety'; label: string }[] = [
  { key: 'randomness', label: 'Randomness' },
  { key: 'aggression', label: 'Aggression' },
  { key: 'purchase_variety', label: 'Purchase variety' },
];

const MAX_TURNS = 10;

type SimFaction = {
  id: string;
  name: string;
  alliance: string;
};

function blankSetting(difficulty: string, algorithm: string): AiFactionSetting {
  return {
    difficulty,
    algorithm,
    weights: { ...DEFAULT_WEIGHTS },
    randomness: 0,
    aggression: 0.5,
    purchase_variety: 0,
  };
}

function allianceLabel(alliance: string): string {
  if (alliance === 'good') return 'Good';
  if (alliance === 'evil') return 'Evil';
  if (!alliance || alliance === 'neutral') return 'Neutral';
  return alliance.charAt(0).toUpperCase() + alliance.slice(1);
}

function turnOrderIds(starting: Record<string, unknown> | undefined): string[] {
  const raw = starting?.turn_order;
  if (!Array.isArray(raw)) return [];
  return raw.filter((id): id is string => typeof id === 'string' && id.length > 0);
}

function factionsFromBundle(
  factions: Record<string, unknown> | undefined,
  turnOrder: string[],
): SimFaction[] {
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
  const rank = new Map(turnOrder.map((id, index) => [id, index]));
  return rows.sort((a, b) => {
    const ai = rank.has(a.id) ? rank.get(a.id)! : turnOrder.length;
    const bi = rank.has(b.id) ? rank.get(b.id)! : turnOrder.length;
    if (ai !== bi) return ai - bi;
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
  const [levels, setLevels] = useState<AdminAiLevel[]>([]);
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
        setLevels(ai.levels ?? []);
        const nextFactions = factionsFromBundle(
          bundle.factions,
          turnOrderIds(bundle.starting_setup),
        );
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
    patch: {
      difficulty?: string;
      algorithm?: string;
      weight?: { key: keyof AiFactionSetting['weights']; value: number };
    },
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
  const choices: AdminAiLevel[] = levels.length > 0 ? levels : [{
    id: 'normal',
    name: 'Normal',
    iterations: 500,
    seconds: 0.5,
    randomness: 0,
    aggression: 0.5,
    purchase_variety: 0,
    purchase_options: 48,
    combat_move_options: 24,
    non_combat_options: 24,
    mobilization_options: 24,
  }];

  return (
    <div className="admin-page__ai">
      <p className="admin-page__hint">
        Runs one computer-vs-computer game and keeps the report here. Factions are listed in turn
        order. Each one picks a difficulty. Randomness, aggression, and purchase variety come from
        that level. Steps, seconds, and the board weights apply only when the faction uses MCTS.
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
            const levelStyle = choices.find((level) => level.id === setting.difficulty);
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
                      <span className="admin-sim-faction__heuristic">
                        Heuristic ignores steps, seconds, and board weights.
                      </span>
                    )}
                  </label>
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
                  {STYLE_FIELDS.map((field) => (
                    <p key={field.key} className="admin-page__field">
                      <span className="admin-page__field-label">
                        {field.label}{' '}
                        <span className="admin-ai-style__value">
                          {Number(levelStyle?.[field.key] ?? 0).toFixed(2)}
                        </span>
                      </span>
                    </p>
                  ))}
                  {usesMcts ? WEIGHT_FIELDS.map((field) => (
                    <label key={field.key} className="admin-page__field">
                      <span className="admin-page__field-label">{field.label}</span>
                      <input
                        className="admin-form__input"
                        type="number"
                        min={0}
                        max={field.max}
                        step={0.1}
                        value={setting.weights[field.key]}
                        onChange={(e) => {
                          const value = Number(e.target.value);
                          if (Number.isFinite(value)) {
                            patchFaction(faction.id, { weight: { key: field.key, value } });
                          }
                        }}
                      />
                    </label>
                  )) : null}
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
