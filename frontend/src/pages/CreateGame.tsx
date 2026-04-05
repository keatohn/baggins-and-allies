import React, { useState, useEffect } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api, type SetupInfo } from '../services/api';
import './CreateGame.css';

export default function CreateGame() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [isMultiplayer, setIsMultiplayer] = useState(false);
  const [setups, setSetups] = useState<SetupInfo[]>([]);
  const [selectedSetupId, setSelectedSetupId] = useState<string | null>(null);
  const [loadingSetups, setLoadingSetups] = useState(true);
  const [setupsError, setSetupsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Backend returns only is_active + context scenarios; frontend filter matches that contract
  const scenariosWithContext = setups.filter(
    (s) => s.context && typeof s.context === 'object' && Object.keys(s.context).length > 0
  );

  useEffect(() => {
    let cancelled = false;
    setSetupsError(null);
    api.getSetups().then(({ setups: list }) => {
      if (!cancelled) {
        setSetups(Array.isArray(list) ? list : []);
        const withContext = (Array.isArray(list) ? list : []).filter(
          (s) => s.context && typeof s.context === 'object' && Object.keys(s.context).length > 0
        );
        if (withContext.length > 0 && selectedSetupId === null) setSelectedSetupId(withContext[0].id);
      }
    }).catch((err) => {
      if (!cancelled) {
        setSetups([]);
        setSetupsError(err instanceof Error ? err.message : 'Could not load scenarios. Is the backend running?');
      }
    }).finally(() => {
      if (!cancelled) setLoadingSetups(false);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (scenariosWithContext.length > 0 && (selectedSetupId === null || !scenariosWithContext.some((s) => s.id === selectedSetupId)))
      setSelectedSetupId(scenariosWithContext[0].id);
  }, [scenariosWithContext, selectedSetupId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.createGame(
        name.trim() || 'My game',
        isMultiplayer,
        selectedSetupId ?? undefined,
      );
      const initialState = res.state != null
        ? { ...res.state, turn_order: res.turn_order ?? res.state.turn_order }
        : undefined;
      navigate(`/game/${res.game_id}`, {
        replace: true,
        state: initialState != null ? { initialState, gameId: res.game_id } : undefined,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create game');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="create-game-page">
      <h1 className="create-game-page__title">Create game</h1>
      <form className="create-game-form" onSubmit={handleSubmit}>
        {error && <p className="create-game-form__error">{error}</p>}
        <label className="create-game-form__label">
          Name
          <input
            type="text"
            className="create-game-form__input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My game"
          />
        </label>
        <div className="create-game-form__field">
          <span className="create-game-form__field-label">Scenario</span>
          {loadingSetups ? (
            <p className="create-game-form__hint">Loading scenarios…</p>
          ) : setupsError ? (
            <p className="create-game-form__error create-game-form__hint" role="alert">
              {setupsError}
            </p>
          ) : scenariosWithContext.length === 0 ? (
            <p className="create-game-form__hint">No scenarios available.</p>
          ) : (
            <>
              <div className="create-game-form__scenarios">
                {scenariosWithContext.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={`create-game-form__scenario ${selectedSetupId === s.id ? 'create-game-form__scenario--active' : ''}`}
                    onClick={() => setSelectedSetupId(s.id)}
                  >
                    <span className="create-game-form__scenario-name">{s.display_name}</span>
                    {s.context && (
                      <span className="create-game-form__scenario-context">
                        {[s.context.year, s.context.map].filter(Boolean).join(' · ')}
                        {s.context.faction_count != null && ` · ${s.context.faction_count} factions`}
                        {Array.isArray(s.context.factions) && s.context.factions.length > 0 && (
                          <span className="create-game-form__scenario-factions">
                            {s.context.factions.join(', ')}
                          </span>
                        )}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
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
        <button type="submit" className="create-game-form__submit primary" disabled={loading || loadingSetups}>
          {loading ? 'Creating…' : 'Create game'}
        </button>
      </form>
      <Link to="/" className="page-menu-btn create-game-page__menu-anchor">Menu</Link>
    </div>
  );
}
