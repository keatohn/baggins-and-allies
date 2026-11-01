import { useEffect, useRef, useState } from 'react';
import { useMenuScroll } from '../components/MenuScrollContext';
import AccountMenu from '../components/AccountMenu';
import { api, getAuthToken } from '../services/api';
import type { AuthPlayer } from '../services/api';
import './MainMenu.css';

export default function MainMenu() {
  const { navigateWithScroll, holdScroll, revealScroll } = useMenuScroll();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(true);
  const heldForLoadRef = useRef(false);

  useEffect(() => {
    if (!getAuthToken()) {
      setLoading(false);
      return;
    }
    // Hold the scroll closed while authMe loads so "Loading…" isn't revealed.
    heldForLoadRef.current = true;
    holdScroll();
    api.authMe().then(setPlayer).catch(() => setPlayer(null)).finally(() => setLoading(false));
  }, [holdScroll]);

  useEffect(() => {
    if (!loading && heldForLoadRef.current) {
      heldForLoadRef.current = false;
      revealScroll();
    }
  }, [loading, revealScroll]);

  if (loading) {
    return (
      <>
        <AccountMenu />
        <div className="main-menu main-menu--loading">Loading…</div>
      </>
    );
  }

  return (
    <div className="main-menu">
      <AccountMenu />
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
