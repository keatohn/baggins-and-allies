import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import type { GameListItem, AuthPlayer, ForfeitOptionsResponse } from '../services/api';
import StrongholdAllianceBar from '../components/StrongholdAllianceBar';
import './GameList.css';

const DELETE_CONFIRM_PHRASE = 'DELETE GAME';
const FORFEIT_CONFIRM_PHRASE = 'FORFEIT GAME';

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

function truncateUsername(username: string, maxChars: number = 12): string {
  if (username.length <= maxChars) return username;
  return `${username.slice(0, maxChars)}...`;
}

export default function GameList() {
  const navigate = useNavigate();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [games, setGames] = useState<GameListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [gameToDelete, setGameToDelete] = useState<string | null>(null);
  const [gameToForfeit, setGameToForfeit] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState('');
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [forfeitError, setForfeitError] = useState<string | null>(null);
  const [forfeitOptions, setForfeitOptions] = useState<ForfeitOptionsResponse | null>(null);
  const [forfeitOptionsLoading, setForfeitOptionsLoading] = useState(false);
  const [forfeitAssignments, setForfeitAssignments] = useState<Record<string, string>>({});

  const loadGames = () => {
    api.listGames()
      .then((r) => setGames(r.games))
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadGames();
    api.authMe().then(setPlayer).catch(() => setPlayer(null));
  }, []);

  useEffect(() => {
    if (!gameToForfeit) {
      setForfeitOptions(null);
      setForfeitAssignments({});
      setForfeitOptionsLoading(false);
      setForfeitError(null);
      return;
    }
    let cancelled = false;
    setForfeitOptionsLoading(true);
    setForfeitError(null);
    api
      .getForfeitOptions(gameToForfeit)
      .then((o) => {
        if (cancelled) return;
        setForfeitOptions(o);
        const init: Record<string, string> = {};
        for (const f of o.factions) {
          init[f.faction_id] = 'computer';
        }
        setForfeitAssignments(init);
      })
      .catch((e) => {
        if (!cancelled) {
          setForfeitError(e instanceof Error ? e.message : 'Could not load forfeit options');
          setForfeitOptions(null);
        }
      })
      .finally(() => {
        if (!cancelled) setForfeitOptionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [gameToForfeit]);

  const handleDeleteClick = (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    setGameToDelete(id);
    setGameToForfeit(null);
    setConfirmText('');
    setDeleteError(null);
    setForfeitError(null);
  };

  const handleForfeitClick = (e: React.MouseEvent, id: string) => {
    e.preventDefault();
    e.stopPropagation();
    setGameToForfeit(id);
    setGameToDelete(null);
    setConfirmText('');
    setForfeitError(null);
    setDeleteError(null);
  };

  const handleDeleteCancel = () => {
    setGameToDelete(null);
    setConfirmText('');
    setDeleteError(null);
  };

  const handleForfeitCancel = () => {
    setGameToForfeit(null);
    setConfirmText('');
    setForfeitError(null);
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

  const handleForfeitConfirm = async () => {
    if (!gameToForfeit || confirmText !== FORFEIT_CONFIRM_PHRASE) return;
    if (!forfeitOptions?.factions.length) return;
    setForfeitError(null);
    try {
      await api.forfeitGame(gameToForfeit, { faction_assignments: forfeitAssignments });
      setGames((prev) => prev.filter((g) => g.id !== gameToForfeit));
      setGameToForfeit(null);
      setConfirmText('');
      setForfeitOptions(null);
      setForfeitAssignments({});
    } catch (e) {
      setForfeitError(e instanceof Error ? e.message : 'Failed to forfeit');
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
  const forfeitingGame = gameToForfeit ? games.find((g) => g.id === gameToForfeit) : null;
  const isHost = (g: GameListItem) => player != null && g.created_by != null && String(g.created_by) === String(player.id);

  return (
    <div className="game-list-page" data-page="load-game">
      <header className="game-list-page__header">
        <Link to="/" className="page-menu-btn">Menu</Link>
      </header>
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
                  {g.status === 'lobby' ? (
                    <div className="game-list__turn-info">
                      <span className="game-list__turn-meta game-list__turn-meta--lobby">
                        Lobby
                        {g.scenario?.display_name && (
                          <>
                            <span className="game-list__meta-sep">|</span>
                            <span>{g.scenario.display_name}</span>
                          </>
                        )}
                      </span>
                      <span className="game-list__lobby-stats">
                        {g.lobby_players ?? 0} {(g.lobby_players ?? 0) === 1 ? 'Player' : 'Players'} | {g.lobby_factions_claimed ?? 0}/{g.lobby_factions_total ?? 0} Factions
                      </span>
                      {g.created_at && (
                        <span className="game-list__created">Created {formatCreatedAt(g.created_at)}</span>
                      )}
                    </div>
                  ) : (
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
                            <span className="game-list__player-name" title={g.current_player_username}>
                              {' | '}
                              {truncateUsername(g.current_player_username)}
                            </span>
                          )}
                        </span>
                      )}
                      <span className="game-list__turn-meta">
                        {g.scenario?.display_name && <span>{g.scenario.display_name}</span>}
                        {g.scenario?.display_name && (g.turn_number != null || g.phase) && (
                          <span className="game-list__meta-sep">|</span>
                        )}
                        {g.turn_number != null && <span>Turn {g.turn_number}</span>}
                        {g.turn_number != null && g.phase && <span className="game-list__meta-sep">|</span>}
                        {g.phase && <span>{formatPhase(g.phase)}</span>}
                      </span>
                      {g.created_at && (
                        <span className="game-list__created">Created {formatCreatedAt(g.created_at)}</span>
                      )}
                    </div>
                  )}
                </div>
                {g.status !== 'lobby' && g.faction_stats && (
                  <div className="game-list__stronghold-bar-wrap">
                    <StrongholdAllianceBar factionStats={g.faction_stats} variant="gameList" />
                  </div>
                )}
                {((g.game_code != null && g.status !== 'lobby') || isHost(g)) && (
                  <div className="game-list__card-actions">
                    {g.game_code != null && g.status !== 'lobby' && (
                      <button
                        type="button"
                        className="game-list__forfeit-btn"
                        onClick={(e) => handleForfeitClick(e, g.id)}
                        title="Forfeit and leave game"
                        aria-label="Forfeit"
                      >
                        Forfeit
                      </button>
                    )}
                    {isHost(g) && (
                      <button
                        type="button"
                        className="game-list__delete-btn"
                        onClick={(e) => handleDeleteClick(e, g.id)}
                        title="Delete game"
                        aria-label="Delete game"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                )}
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

      {gameToForfeit && forfeitingGame && (
        <div className="game-list__modal-overlay" onClick={handleForfeitCancel}>
          <div className="game-list__modal" onClick={(e) => e.stopPropagation()}>
            <h2 className="game-list__modal-title">Forfeit this game?</h2>
            <p className="game-list__modal-text">
              You will be removed from <strong>{forfeitingGame.name}</strong>. Choose who will control each of your
              factions. Then type <strong>{FORFEIT_CONFIRM_PHRASE}</strong> to confirm.
            </p>
            {forfeitOptionsLoading && <p className="game-list__modal-text">Loading options…</p>}
            {!forfeitOptionsLoading && forfeitOptions && forfeitOptions.factions.length > 0 && (
              <ul className="game-list__forfeit-faction-list">
                {forfeitOptions.factions.map((f) => (
                  <li key={f.faction_id} className="game-list__forfeit-faction-row">
                    <label className="game-list__forfeit-faction-label" htmlFor={`forfeit-assign-${f.faction_id}`}>
                      {f.display_name}
                    </label>
                    <select
                      id={`forfeit-assign-${f.faction_id}`}
                      className="game-list__forfeit-select"
                      value={forfeitAssignments[f.faction_id] ?? 'computer'}
                      onChange={(e) =>
                        setForfeitAssignments((prev) => ({ ...prev, [f.faction_id]: e.target.value }))
                      }
                    >
                      {forfeitOptions.assignees.map((a) => (
                        <option key={`${f.faction_id}-${a.id}`} value={a.id}>
                          {a.label}
                        </option>
                      ))}
                    </select>
                  </li>
                ))}
              </ul>
            )}
            <input
              type="text"
              className="game-list__modal-input"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={FORFEIT_CONFIRM_PHRASE}
              autoFocus={!forfeitOptionsLoading}
              disabled={forfeitOptionsLoading || !forfeitOptions}
            />
            {forfeitError && <p className="game-list__error">{forfeitError}</p>}
            <div className="game-list__modal-actions">
              <button type="button" className="game-list__modal-btn" onClick={handleForfeitCancel}>
                Cancel
              </button>
              <button
                type="button"
                className="game-list__modal-btn game-list__modal-btn--danger"
                onClick={handleForfeitConfirm}
                disabled={
                  confirmText !== FORFEIT_CONFIRM_PHRASE ||
                  forfeitOptionsLoading ||
                  !forfeitOptions ||
                  forfeitOptions.factions.length === 0
                }
              >
                Forfeit
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
