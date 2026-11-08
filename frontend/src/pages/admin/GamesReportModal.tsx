import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { AdminGameEditor, GameListItem } from '../../services/api';
import { MapViewPane } from './MapViewPane';

type FilterKey = 'all' | 'lobby' | 'active' | 'finished';

const END_CONFIRM_PHRASE = 'END GAME';
const TEST_CONFIRM_PHRASE = 'TEST MODE';
const EDIT_CONFIRM_PHRASE = 'EDIT GAME';

type EndWinner = 'good' | 'evil' | 'none';

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'lobby', label: 'Lobby' },
  { key: 'active', label: 'In progress' },
  { key: 'finished', label: 'Completed' },
];

function statusLabel(status: string): string {
  if (status === 'lobby') return 'Lobby';
  if (status === 'active') return 'In progress';
  if (status === 'finished') return 'Completed';
  return status;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch {
    return iso;
  }
}

function playerNames(game: GameListItem): string {
  const names: string[] = [];
  for (const p of game.players ?? []) {
    const n = p.username?.trim();
    if (!n) continue;
    const initials = [...new Set((p.alliances ?? []).filter(Boolean).map((alliance) => alliance[0].toUpperCase()))];
    const name = initials.length ? `${n} (${initials.join('/')})` : n;
    const label = p.forfeited ? `${name} (forfeited)` : name;
    if (!names.includes(label)) names.push(label);
  }
  if (names.length > 0) return names.join(', ');
  if (game.current_player_username) return game.current_player_username;
  return '—';
}

function turnPhase(game: GameListItem): string {
  if (game.status === 'lobby') return '—';
  const parts: string[] = [];
  if (game.turn_number != null) parts.push(`Turn ${game.turn_number}`);
  if (game.phase) parts.push(game.phase);
  return parts.length > 0 ? parts.join(' | ') : '—';
}

function winnerLabel(game: GameListItem): string {
  if (!game.winner) return '—';
  const w = game.winner.toLowerCase();
  if (w === 'good') return 'Good';
  if (w === 'evil') return 'Evil';
  return game.winner;
}

function scenarioKey(game: GameListItem): string {
  return game.scenario?.id ?? game.scenario?.display_name ?? '__none__';
}

function scenarioOptionLabel(game: GameListItem): string {
  const name = game.scenario?.display_name;
  const id = game.scenario?.id;
  if (!name && !id) return 'Unknown scenario';
  if (name && id && name !== id) return `${name} (${id})`;
  return name ?? id ?? 'Unknown scenario';
}

function allianceLabel(key: string): string {
  if (key === 'good') return 'Good';
  if (key === 'evil') return 'Evil';
  if (key === 'none' || key === 'neither') return 'Neither';
  return key;
}

/** Winner-pill keys for a game: alliances from faction_stats, fallback good/evil. */
function winnerPillKeys(game: GameListItem): string[] {
  const keys = Object.keys(game.faction_stats?.alliances ?? {});
  const filtered = keys.filter((k) => k === 'good' || k === 'evil');
  return filtered.length > 0 ? filtered.sort() : ['good', 'evil'];
}

export function GamesReportModal({ onClose }: { onClose: () => void }) {
  const [games, setGames] = useState<GameListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>('all');
  const [selectedPlayers, setSelectedPlayers] = useState<Set<string>>(new Set());
  const [selectedScenario, setSelectedScenario] = useState<string>('');
  const [playersOpen, setPlayersOpen] = useState(false);
  const playersMenuRef = useRef<HTMLDivElement | null>(null);
  const [endTarget, setEndTarget] = useState<GameListItem | null>(null);
  const [endWinner, setEndWinner] = useState<EndWinner | null>(null);
  const [endConfirm, setEndConfirm] = useState('');
  const [endBusy, setEndBusy] = useState(false);
  const [endError, setEndError] = useState<string | null>(null);
  const [testTarget, setTestTarget] = useState<GameListItem | null>(null);
  const [testConfirm, setTestConfirm] = useState('');
  const [testBusy, setTestBusy] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [editDoc, setEditDoc] = useState<AdminGameEditor | null>(null);
  const [editLoading, setEditLoading] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const editRequest = useRef(0);

  const refreshGames = () => {
    api
      .adminListGames()
      .then((r) => setGames(r.games ?? []))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load games'));
  };

  const closeEdit = () => {
    editRequest.current += 1;
    setEditDoc(null);
    setEditError(null);
    setEditLoading(false);
  };

  const openEdit = (game: GameListItem) => {
    const token = ++editRequest.current;
    setEndTarget(null);
    setTestTarget(null);
    setEditDoc(null);
    setEditError(null);
    setEditLoading(true);
    api
      .adminGetGameEditor(game.id)
      .then((doc) => {
        if (editRequest.current === token) setEditDoc(doc);
      })
      .catch((e: unknown) => {
        if (editRequest.current === token) {
          setEditError(e instanceof Error ? e.message : 'Could not load this game');
        }
      })
      .finally(() => {
        if (editRequest.current === token) setEditLoading(false);
      });
  };

  const openEndGame = (game: GameListItem) => {
    setEditDoc(null);
    setTestTarget(null);
    setEndTarget(game);
    setEndWinner(null);
    setEndConfirm('');
    setEndError(null);
  };

  const openTestMode = (game: GameListItem) => {
    setEndTarget(null);
    setTestTarget(game);
    setTestConfirm('');
    setTestError(null);
  };

  const closeTestMode = () => {
    if (testBusy) return;
    setTestTarget(null);
    setTestConfirm('');
    setTestError(null);
  };

  const submitTestMode = async () => {
    if (!testTarget || testConfirm !== TEST_CONFIRM_PHRASE || testBusy) return;
    setTestBusy(true);
    setTestError(null);
    try {
      await api.adminMarkTestMode(testTarget.id);
      setTestTarget(null);
      setTestConfirm('');
      refreshGames();
    } catch (e: unknown) {
      setTestError(e instanceof Error ? e.message : 'Could not switch game to Test Mode');
    } finally {
      setTestBusy(false);
    }
  };

  const closeEndGame = () => {
    if (endBusy) return;
    setEndTarget(null);
    setEndWinner(null);
    setEndConfirm('');
    setEndError(null);
  };

  const submitEndGame = async () => {
    if (!endTarget || !endWinner || endConfirm !== END_CONFIRM_PHRASE || endBusy) return;
    setEndBusy(true);
    setEndError(null);
    try {
      await api.adminEndGame(endTarget.id, endWinner);
      setEndTarget(null);
      setEndWinner(null);
      setEndConfirm('');
      refreshGames();
    } catch (e: unknown) {
      setEndError(e instanceof Error ? e.message : 'Could not end game');
    } finally {
      setEndBusy(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    api
      .adminListGames()
      .then((r) => {
        if (!cancelled) setGames(r.games ?? []);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load games');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!playersOpen) return;
    const onDown = (e: MouseEvent) => {
      if (playersMenuRef.current && !playersMenuRef.current.contains(e.target as Node)) {
        setPlayersOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [playersOpen]);

  const allPlayers = useMemo(() => {
    const byId = new Map<string, string>();
    for (const g of games ?? []) {
      for (const p of g.players ?? []) {
        const name = p.username?.trim();
        if (p.player_id && name && !byId.has(p.player_id)) byId.set(p.player_id, name);
      }
    }
    return [...byId.entries()]
      .map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [games]);

  const scenarioOptions = useMemo(() => {
    const byKey = new Map<string, { key: string; label: string }>();
    for (const g of games ?? []) {
      const key = scenarioKey(g);
      if (!byKey.has(key)) byKey.set(key, { key, label: scenarioOptionLabel(g) });
    }
    return [...byKey.values()].sort((a, b) => {
      if (a.key === '__none__') return 1;
      if (b.key === '__none__') return -1;
      return a.label.localeCompare(b.label);
    });
  }, [games]);

  const shown = useMemo(() => {
    let list = games ?? [];
    if (filter !== 'all') list = list.filter((g) => g.status === filter);
    if (selectedScenario) {
      list = list.filter((g) => scenarioKey(g) === selectedScenario);
    }
    if (selectedPlayers.size > 0) {
      // AND logic: every selected player must be in the game.
      list = list.filter((g) => {
        const ids = new Set((g.players ?? []).map((p) => p.player_id));
        for (const pid of selectedPlayers) {
          if (!ids.has(pid)) return false;
        }
        return true;
      });
    }
    return list;
  }, [games, filter, selectedPlayers, selectedScenario]);

  const counts = useMemo(() => {
    const all = games ?? [];
    return {
      all: all.length,
      lobby: all.filter((g) => g.status === 'lobby').length,
      active: all.filter((g) => g.status === 'active').length,
      finished: all.filter((g) => g.status === 'finished').length,
    };
  }, [games]);

  const togglePlayer = (pid: string) => {
    setSelectedPlayers((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  };

  const playersButtonLabel = useMemo(() => {
    if (selectedPlayers.size === 0) return 'All players';
    if (selectedPlayers.size === 1) {
      const pid = [...selectedPlayers][0];
      return allPlayers.find((p) => p.id === pid)?.name ?? '1 player';
    }
    return `${selectedPlayers.size} players`;
  }, [selectedPlayers, allPlayers]);

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="admin-modal admin-modal--games"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-games-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="games-report__header">
          <div>
            <h2 id="admin-games-title" className="admin-modal__title">
              Games
            </h2>
            <p className="admin-form__micro">
              {games == null ? 'Loading…' : `${counts.all} game${counts.all === 1 ? '' : 's'} total`}
            </p>
          </div>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="games-report__filters" role="group" aria-label="Filter games">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`games-report__filter${filter === f.key ? ' games-report__filter--active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label} ({counts[f.key]})
            </button>
          ))}
        </div>

        <div className="games-report__filters" role="group" aria-label="Filter by player or scenario">
          <div className="games-report__dropdown" ref={playersMenuRef}>
            <button
              type="button"
              className={`games-report__filter${selectedPlayers.size > 0 ? ' games-report__filter--active' : ''}`}
              aria-haspopup="listbox"
              aria-expanded={playersOpen}
              onClick={() => setPlayersOpen((o) => !o)}
            >
              {playersButtonLabel} ▾
            </button>
            {playersOpen ? (
              <div className="games-report__dropdown-menu" role="listbox" aria-multiselectable="true">
                {allPlayers.length === 0 ? (
                  <p className="admin-form__micro games-report__dropdown-empty">No players found.</p>
                ) : (
                  allPlayers.map((p) => (
                    <label key={p.id} className="games-report__dropdown-item" role="option" aria-selected={selectedPlayers.has(p.id)}>
                      <input
                        type="checkbox"
                        checked={selectedPlayers.has(p.id)}
                        onChange={() => togglePlayer(p.id)}
                      />
                      <span>{p.name}</span>
                    </label>
                  ))
                )}
              </div>
            ) : null}
          </div>
          <select
            className="games-report__filter games-report__select"
            value={selectedScenario}
            onChange={(e) => setSelectedScenario(e.target.value)}
            aria-label="Filter by scenario"
          >
            <option value="">All scenarios</option>
            {scenarioOptions.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        {error ? <p className="admin-page__error games-report__error">{error}</p> : null}

        <div className="games-report__table-scroll">
          {games != null && !error ? (
            shown.length === 0 ? (
              <p className="admin-form__micro">No games match this filter.</p>
            ) : (
              <table className="games-report__table">
                <colgroup>
                  <col className="games-report__col-name" />
                  <col className="games-report__col-players" />
                  <col className="games-report__col-scenario" />
                  <col className="games-report__col-turn" />
                  <col className="games-report__col-status" />
                  <col className="games-report__col-winner" />
                  <col className="games-report__col-created" />
                  <col className="games-report__col-completed" />
                  <col className="games-report__col-actions" />
                </colgroup>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Players</th>
                    <th>Scenario</th>
                    <th>Turn / Phase</th>
                    <th>Status</th>
                    <th>Winner</th>
                    <th>Created</th>
                    <th>Completed</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((g) => (
                    <tr key={g.id}>
                      <td className="games-report__name">{g.name}</td>
                      <td className="games-report__players">
                        <span className="games-report__players-text">{playerNames(g)}</span>
                      </td>
                      <td className="games-report__scenario">{scenarioOptionLabel(g)}</td>
                      <td className="games-report__turn">{turnPhase(g)}</td>
                      <td>
                        <span className={`games-report__status games-report__status--${g.status}`}>
                          {statusLabel(g.status)}
                        </span>
                      </td>
                      <td>{winnerLabel(g)}</td>
                      <td>{formatDate(g.created_at)}</td>
                      <td>{formatDate(g.completed_at)}</td>
                      <td className="games-report__actions">
                        <span className="games-report__action-row">
                          <button
                            type="button"
                            className="games-report__end-btn"
                            onClick={() => openEdit(g)}
                          >
                            Edit
                          </button>
                          {g.status === 'active' ? (
                            <button
                              type="button"
                              className="games-report__end-btn"
                              onClick={() => openEndGame(g)}
                            >
                              End
                            </button>
                          ) : null}
                          {g.test_mode ? (
                            <span className="games-report__test-tag">Test</span>
                          ) : (
                            <button
                              type="button"
                              className="games-report__end-btn"
                              onClick={() => openTestMode(g)}
                            >
                              Test
                            </button>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}
        </div>
        {endTarget ? (
          <div
            className="admin-modal-overlay games-report__end-overlay"
            role="presentation"
            onClick={closeEndGame}
          >
            <div
              className="admin-modal games-report__end-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-end-game-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-end-game-title" className="admin-modal__title">
                End game?
              </h2>
              <p className="admin-form__micro games-report__end-text">
                This will permanently end <strong>{endTarget.name}</strong>. Choose the winner, or Neither to
                void the game (no stats recorded). Type <strong>{END_CONFIRM_PHRASE}</strong> to confirm.
              </p>
              <div className="games-report__end-pills" role="group" aria-label="Winner">
                {[...winnerPillKeys(endTarget), 'none'].map((key) => {
                  const value = key as EndWinner;
                  const selected = endWinner === value;
                  return (
                    <button
                      key={key}
                      type="button"
                      className={`games-report__end-pill${selected ? ' games-report__end-pill--active' : ''}`}
                      aria-pressed={selected}
                      onClick={() => setEndWinner(value)}
                    >
                      {allianceLabel(key)}
                    </button>
                  );
                })}
              </div>
              <input
                type="text"
                className="games-report__end-input"
                value={endConfirm}
                onChange={(e) => setEndConfirm(e.target.value)}
                placeholder={END_CONFIRM_PHRASE}
                autoFocus
              />
              {endError ? <p className="admin-page__error games-report__end-error">{endError}</p> : null}
              <div className="games-report__end-actions">
                <button type="button" className="admin-page__btn" onClick={closeEndGame} disabled={endBusy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn games-report__end-confirm"
                  onClick={submitEndGame}
                  disabled={!endWinner || endConfirm !== END_CONFIRM_PHRASE || endBusy}
                >
                  {endBusy ? 'Ending…' : 'End game'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
        {testTarget ? (
          <div
            className="admin-modal-overlay games-report__end-overlay"
            role="presentation"
            onClick={closeTestMode}
          >
            <div
              className="admin-modal games-report__end-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-test-mode-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-test-mode-title" className="admin-modal__title">
                Switch to Test Mode?
              </h2>
              <p className="admin-form__micro games-report__end-text">
                <strong>{testTarget.name}</strong> will no longer count toward XP or player profile stats.
                Anything this game already added will be taken back out. Type <strong>{TEST_CONFIRM_PHRASE}</strong> to confirm.
              </p>
              <input
                type="text"
                className="games-report__end-input"
                value={testConfirm}
                onChange={(e) => setTestConfirm(e.target.value)}
                placeholder={TEST_CONFIRM_PHRASE}
                autoFocus
              />
              {testError ? <p className="admin-page__error games-report__end-error">{testError}</p> : null}
              <div className="games-report__end-actions">
                <button type="button" className="admin-page__btn" onClick={closeTestMode} disabled={testBusy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn games-report__end-confirm"
                  onClick={submitTestMode}
                  disabled={testConfirm !== TEST_CONFIRM_PHRASE || testBusy}
                >
                  {testBusy ? 'Switching…' : 'Switch to Test Mode'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
        {editDoc ? (
          <MapViewPane
            mode="game"
            title={`Edit ${editDoc.game.name}`}
            contextLine={`${editDoc.turn_number ? `Turn ${editDoc.turn_number}` : 'Not started'} · ${editDoc.phase || '—'} · ${
              (typeof editDoc.factions[editDoc.current_faction]?.display_name === 'string'
                ? editDoc.factions[editDoc.current_faction].display_name
                : editDoc.current_faction) || '—'
            }`}
            saveConfirmPhrase={EDIT_CONFIRM_PHRASE}
            startEditing
            mapAsset={editDoc.map_asset || undefined}
            manifest={editDoc.manifest}
            territories={editDoc.territories}
            startingSetup={editDoc.starting_setup}
            camps={editDoc.camps}
            ports={editDoc.ports}
            factions={editDoc.factions}
            units={editDoc.units}
            ringsView={editDoc.rings}
            turnNumber={editDoc.turn_number}
            phase={editDoc.phase}
            currentFaction={editDoc.current_faction}
            onClose={closeEdit}
            onSaveUnits={async (units) => {
              await api.adminPutGameUnits(editDoc.game.id, EDIT_CONFIRM_PHRASE, units);
              setEditDoc((doc) => (doc ? { ...doc, units } : doc));
            }}
            onSave={async (draft) => {
              await api.adminPutGameEditor(editDoc.game.id, {
                confirm: EDIT_CONFIRM_PHRASE,
                territories: draft.territories,
                starting_setup: draft.starting_setup,
                camps: draft.camps,
                ports: draft.ports,
              });
            }}
          />
        ) : null}
        {editLoading || (editError && !editDoc) ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation" onClick={closeEdit}>
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-edit-game-loading"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-edit-game-loading" className="admin-modal__title">
                Edit game
              </h2>
              {editError ? <p className="admin-page__error">{editError}</p> : <p className="admin-form__micro">Loading the board…</p>}
              <div className="admin-modal__actions">
                <button type="button" className="admin-page__btn" onClick={closeEdit}>
                  Close
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
