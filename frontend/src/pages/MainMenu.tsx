import { useEffect, useState } from 'react';
import { useMenuScroll } from '../components/MenuScrollContext';
import { api, getAuthToken } from '../services/api';
import type { AuthPlayer } from '../services/api';
import './MainMenu.css';

export default function MainMenu() {
  const { navigateWithScroll } = useMenuScroll();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAuthToken()) {
      setLoading(false);
      return;
    }
    api.authMe().then(setPlayer).catch(() => setPlayer(null)).finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="main-menu main-menu--loading">Loading…</div>;

  return (
    <div className="main-menu">
      <h1 className="main-menu__title">Baggins & Allies</h1>
      {player ? (
        <>
          <p className="main-menu__quote">“The board is set. The pieces are moving.” <span className="main-menu__quote-attribution">—Gandalf</span></p>
          <div className="main-menu__actions">
          <button type="button" className="main-menu__btn primary" onClick={() => navigateWithScroll('/game/new')}>
            Create game
          </button>
          <button type="button" className="main-menu__btn" onClick={() => navigateWithScroll('/games')}>
            Load game
          </button>
          <button type="button" className="main-menu__btn" onClick={() => navigateWithScroll('/join')}>
            Join game
          </button>
          <button type="button" className="main-menu__btn" onClick={() => navigateWithScroll('/profile')}>
            Profile
          </button>
          {player.is_admin ? (
            <button
              type="button"
              className="main-menu__btn"
              data-admin-major-sfx
              onClick={() => navigateWithScroll('/admin')}
            >
              Admin
            </button>
          ) : null}
          <p className="main-menu__user">Signed in as {player.username}</p>
        </div>
        </>
      ) : (
        <div className="main-menu__actions">
          <button type="button" className="main-menu__btn primary" onClick={() => navigateWithScroll('/login')}>
            Log in
          </button>
          <button type="button" className="main-menu__btn" onClick={() => navigateWithScroll('/register')}>
            Create Account
          </button>
        </div>
      )}
    </div>
  );
}
