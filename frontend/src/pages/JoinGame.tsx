import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import './JoinGame.css';

export default function JoinGame() {
  const navigate = useNavigate();
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const trimmed = code.trim().toUpperCase();
    if (trimmed.length !== 4) {
      setError('Enter a 4-character game code');
      return;
    }
    setLoading(true);
    try {
      const res = await api.joinGame(trimmed);
      navigate(`/game/${res.game_id}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to join');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="join-game-page">
      <h1 className="join-game-page__title">Join game</h1>
      <form className="join-game-form" onSubmit={handleSubmit}>
        {error && <p className="join-game-form__error">{error}</p>}
        <label className="join-game-form__label">
          Game code (4 characters)
          <input
            type="text"
            className="join-game-form__input"
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase().slice(0, 4))}
            placeholder="XXXX"
            maxLength={4}
            autoComplete="off"
          />
        </label>
        <button type="submit" className="join-game-form__submit primary" disabled={loading}>
          {loading ? 'Joining…' : 'Join game'}
        </button>
      </form>
      <p className="join-game-page__footer">
        <Link to="/">Back to menu</Link>
      </p>
    </div>
  );
}
