import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import type { GameListItem } from '../services/api';
import './GameList.css';

const DELETE_CONFIRM_PHRASE = 'DELETE GAME';

function formatPhase(phase: string): string {
  if (phase === 'non_combat_move') return 'Non-Combat Move';
  if (phase === 'mobilization') return 'Mobilization';
  return phase.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

function formatCreatedAt(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch {
    return iso;
  }
}

export default function GameList() {
  const navigate = useNavigate();
  const [games, setGames] = useState<GameListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [gameToDelete, setGameToDelete] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState('');
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const loadGames = () => {
    api.listGames()
      .then((r) => setGames(r.games))
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadGames();
  }, []);

  const handleDeleteClick = (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    setGameToDelete(id);
    setConfirmText('');
    setDeleteError(null);
  };

  const handleDeleteCancel = () => {
    setGameToDelete(null);
    setConfirmText('');
    setDeleteError(null);
  };

  const handleDeleteConfirm = async () => {
    if (!gameToDelete || confirmText !== DELETE_CONFIRM_PHRASE) return;
    setDeleteError(null);
    try {
      await api.deleteGame(gameToDelete);
      setGames((prev) => prev.filter((g) => g.id !== gameToDelete));
      setGameToDelete(null);
      setConfirmText('');
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Failed to delete game');
    }
  };

  if (loading) return <div className="game-list-page">Loading…</div>;
  if (error) {
    return (
      <div className="game-list-page">
        <p className="game-list__error">{error}</p>
        <Link to="/">Back</Link>
      </div>
    );
  }

  const deletingGame = gameToDelete ? games.find((g) => g.id === gameToDelete) : null;

  return (
    <div className="game-list-page" data-page="load-game">
      <h1 className="game-list-page__title">Your games</h1>
      <Link to="/" className="game-list-page__menu-btn">Menu</Link>
      {games.length === 0 ? (
        <p className="game-list__empty">No games yet.</p>
      ) : (
        <ul className="game-list">
          {games.map((g) => (
            <li key={g.id} className="game-list__item">
              <div
                className="game-list__card"
                role="button"
                tabIndex={0}
                onClick={() => navigate('/game/' + g.id)}
                onKeyDown={(e) => e.key === 'Enter' && navigate('/game/' + g.id)}
              >
                  <div className="game-list__card-main">
                  <h3 className="game-list__name">{g.name}</h3>
                  <div className="game-list__turn-info">
                    {(g.current_faction_icon != null || g.current_faction_display_name != null || (g.current_player_username != null && g.current_player_username !== '')) && (
                      <span className="game-list__faction-row">
                        {g.current_faction_icon && (
                          <img
                            src={g.current_faction_icon}
                            alt=""
                            className="game-list__faction-icon"
                          />
                        )}
                        <span className="game-list__faction-name">{g.current_faction_display_name ?? g.current_faction ?? '—'}</span>
                        {g.current_player_username != null && g.current_player_username !== '' && (
                          <span className="game-list__player-name"> | {g.current_player_username}</span>
                        )}
                      </span>
                    )}
                    <span className="game-list__turn-meta">
                      {g.turn_number != null && <span>Turn {g.turn_number}</span>}
                      {g.turn_number != null && g.phase && <span className="game-list__meta-sep">|</span>}
                      {g.phase && <span>{formatPhase(g.phase)}</span>}
                      {g.status === 'lobby' && !g.turn_number && <span>Lobby</span>}
                    </span>
                    {g.created_at && (
                      <span className="game-list__created">Created {formatCreatedAt(g.created_at)}</span>
                    )}
                  </div>
                </div>
                {(() => {
                  const fs = g.faction_stats;
                  const good = fs?.alliances?.['good']?.strongholds ?? 0;
                  const evil = fs?.alliances?.['evil']?.strongholds ?? 0;
                  const neutral = fs?.neutral_strongholds ?? 0;
                  const total = good + neutral + evil || 1;
                  return (
                    <div className="game-list__stronghold-bar-wrap">
                      <div className="game-list__stronghold-bar">
                        <div className="game-list__stronghold-bar-good" style={{ width: `${(good / total) * 100}%` }} />
                        <div className="game-list__stronghold-bar-neutral" style={{ width: `${(neutral / total) * 100}%` }} />
                        <div className="game-list__stronghold-bar-evil" style={{ width: `${(evil / total) * 100}%` }} />
                      </div>
                      <span className="game-list__stronghold-bar-label">Good {good} · Evil {evil}</span>
                    </div>
                  );
                })()}
                <button
                  type="button"
                  className="game-list__delete-btn"
                  onClick={(e) => handleDeleteClick(e, g.id)}
                  title="Delete game"
                  aria-label="Delete game"
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {gameToDelete && deletingGame && (
        <div className="game-list__modal-overlay" onClick={handleDeleteCancel}>
          <div className="game-list__modal" onClick={(e) => e.stopPropagation()}>
            <h2 className="game-list__modal-title">Delete game?</h2>
            <p className="game-list__modal-text">
              This will permanently delete <strong>{deletingGame.name}</strong>. Type <strong>{DELETE_CONFIRM_PHRASE}</strong> to confirm.
            </p>
            <input
              type="text"
              className="game-list__modal-input"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={DELETE_CONFIRM_PHRASE}
              autoFocus
            />
            {deleteError && <p className="game-list__error">{deleteError}</p>}
            <div className="game-list__modal-actions">
              <button type="button" className="game-list__modal-btn" onClick={handleDeleteCancel}>
                Cancel
              </button>
              <button
                type="button"
                className="game-list__modal-btn game-list__modal-btn--danger"
                onClick={handleDeleteConfirm}
                disabled={confirmText !== DELETE_CONFIRM_PHRASE}
              >
                Delete game
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
