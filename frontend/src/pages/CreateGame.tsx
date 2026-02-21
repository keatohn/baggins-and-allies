import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import './CreateGame.css';

export default function CreateGame() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [isMultiplayer, setIsMultiplayer] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.createGame(name.trim() || 'My game', isMultiplayer);
      navigate(`/game/${res.game_id}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create game');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="create-game-page">
      <h1 className="create-game-page__title">Create new game</h1>
      <form className="create-game-form" onSubmit={handleSubmit}>
        {error && <p className="create-game-form__error">{error}</p>}
        <label className="create-game-form__label">
          Game name
          <input
            type="text"
            className="create-game-form__input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My game"
          />
        </label>
        <div className="create-game-form__field">
          <span className="create-game-form__field-label">Mode</span>
          <div className="create-game-form__picker" role="group" aria-label="Single or multiplayer">
            <button
              type="button"
              className={`create-game-form__picker-option ${!isMultiplayer ? 'create-game-form__picker-option--active' : ''}`}
              onClick={() => setIsMultiplayer(false)}
            >
              Single Player
            </button>
            <button
              type="button"
              className={`create-game-form__picker-option ${isMultiplayer ? 'create-game-form__picker-option--active' : ''}`}
              onClick={() => setIsMultiplayer(true)}
            >
              Multiplayer
            </button>
          </div>
        </div>
        <button type="submit" className="create-game-form__submit primary" disabled={loading}>
          {loading ? 'Creating…' : 'Create game'}
        </button>
      </form>
      <Link to="/" className="create-game-page__menu-btn">Menu</Link>
    </div>
  );
}
