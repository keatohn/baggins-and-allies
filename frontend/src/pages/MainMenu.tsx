import { useEffect, useRef, useState } from 'react';
import { useMenuScroll } from '../components/MenuScrollContext';
import AccountMenu from '../components/AccountMenu';
import { api, getAuthToken } from '../services/api';
import type { AuthPlayer, GameListItem } from '../services/api';
import GameCardSummary from '../components/GameCardSummary';
import PlaqueFrame, { type PlaqueFrameVariant } from '../components/PlaqueFrame';
import './MainMenu.css';

export default function MainMenu() {
  const { navigateWithScroll, holdScroll, revealScroll } = useMenuScroll();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [recent, setRecent] = useState<GameListItem | null>(null);
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
    api
      .authMe()
      .then(async (me) => {
        setPlayer(me);
        try {
          const res = await api.recentGame();
          setRecent(res.game);
        } catch {
          setRecent(null);
        }
      })
      .catch(() => setPlayer(null))
      .finally(() => setLoading(false));
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
      {player?.is_admin ? (
        <button
          type="button"
          className="main-menu__corner-btn main-menu__admin"
          data-admin-major-sfx
          onClick={() => navigateWithScroll('/admin')}
        >
          Admin
        </button>
      ) : null}
      <div className="main-menu__hero">
        <h1 className="main-menu__title">Baggins & Allies</h1>
        {player ? (
          <p className="main-menu__quote">
            “The board is set. The pieces are moving.”{' '}
            <span className="main-menu__quote-attribution">—Gandalf</span>
          </p>
        ) : null}
      </div>
      {player ? (
        <div className={`main-menu__actions${recent ? '' : ' main-menu__actions--three'}`}>
          <MenuPlaque label="Create game" variant="ring" onClick={() => navigateWithScroll('/game/new')} />
          <MenuPlaque label="Join game" variant="hobbit" onClick={() => navigateWithScroll('/join')} />
          <MenuPlaque label="Load game" variant="elven" onClick={() => navigateWithScroll('/games')} />
          {recent ? (
            <button
              type="button"
              className="main-menu__panel main-menu__panel--recent"
              onClick={() => navigateWithScroll('/game/' + recent.id)}
              aria-label={`Recent game: ${recent.name}`}
            >
              <PlaqueFrame variant="dwarven" />
              <span className="main-menu__panel-body">
                <span className="main-menu__panel-kicker">Recent</span>
                <GameCardSummary game={recent} />
              </span>
            </button>
          ) : null}
        </div>
      ) : (
        <div className="main-menu__actions main-menu__actions--guest">
          <MenuPlaque label="Log in" variant="gondor" onClick={() => navigateWithScroll('/login')} />
          <MenuPlaque label="Create Account" variant="rohan" onClick={() => navigateWithScroll('/register')} />
        </div>
      )}
    </div>
  );
}

function MenuPlaque({
  label,
  variant,
  onClick,
}: {
  label: string;
  variant: PlaqueFrameVariant;
  onClick: () => void;
}) {
  return (
    <button type="button" className="main-menu__panel" onClick={onClick}>
      <PlaqueFrame variant={variant} />
      <span className="main-menu__panel-label">{label}</span>
    </button>
  );
}
