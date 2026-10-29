import { useEffect, useMemo, useState } from 'react';
import { api } from '../../services/api';
import type { GameListItem } from '../../services/api';

type FilterKey = 'all' | 'lobby' | 'active' | 'finished';

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
  const names = (game.players ?? [])
    .map((p) => p.username?.trim())
    .filter((n): n is string => Boolean(n));
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

export function GamesReportModal({ onClose }: { onClose: () => void }) {
  const [games, setGames] = useState<GameListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>('all');

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

  const shown = useMemo(() => {
    const all = games ?? [];
    if (filter === 'all') return all;
    return all.filter((g) => g.status === filter);
  }, [games, filter]);

  const counts = useMemo(() => {
    const all = games ?? [];
    return {
      all: all.length,
      lobby: all.filter((g) => g.status === 'lobby').length,
      active: all.filter((g) => g.status === 'active').length,
      finished: all.filter((g) => g.status === 'finished').length,
    };
  }, [games]);

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

        {error ? <p className="admin-page__error">{error}</p> : null}

        {games != null && !error ? (
          shown.length === 0 ? (
            <p className="admin-form__micro">No games match this filter.</p>
          ) : (
            <div className="games-report__table-scroll">
              <table className="games-report__table">
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
                  </tr>
                </thead>
                <tbody>
                  {shown.map((g) => (
                    <tr key={g.id}>
                      <td className="games-report__name">{g.name}</td>
                      <td className="games-report__players">{playerNames(g)}</td>
                      <td>{g.scenario?.display_name ?? '—'}</td>
                      <td>{turnPhase(g)}</td>
                      <td>
                        <span className={`games-report__status games-report__status--${g.status}`}>
                          {statusLabel(g.status)}
                        </span>
                      </td>
                      <td>{winnerLabel(g)}</td>
                      <td>{formatDate(g.created_at)}</td>
                      <td>{formatDate(g.completed_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
