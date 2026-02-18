import { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import type { GameListItem } from '../services/api';
import './GameList.css';

export default function GameList() {
  const navigate = useNavigate();
  const [games, setGames] = useState<GameListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listGames()
      .then((r) => setGames(r.games))
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="game-list-page">Loading…</div>;
  if (error) {
    return (
      <div className="game-list-page">
        <p className="game-list__error">{error}</p>
        <Link to="/">Back</Link>
      </div>
    );
  }

  return (
    <div className="game-list-page">
      <h1 className="game-list-page__title">Your games</h1>
      <Link to="/" className="game-list-page__back">Back to menu</Link>
      {games.length === 0 ? (
        <p className="game-list__empty">No games yet.</p>
      ) : (
        <ul className="game-list">
          {games.map((g) => (
            <li key={g.id} className="game-list__item">
              <button type="button" className="game-list__btn" onClick={() => navigate('/game/' + g.id)}>
                <span className="game-list__name">{g.name}</span>
                <span className="game-list__meta">{g.status}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
