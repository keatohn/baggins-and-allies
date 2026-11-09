import { useCallback, useEffect, useMemo, useState } from 'react';
import { useMatch, useSearchParams } from 'react-router-dom';
import { api, getAuthToken, AUTH_CHANGED_EVENT } from '../services/api';
import type { AuthPlayer, MyTurnGame } from '../services/api';
import ProfileModal from './account/ProfileModal';
import { tierRingProps } from './account/tierRing';
import AvatarFrame from './account/AvatarFrame';
import SettingsModal from './account/SettingsModal';
import FriendsModal from './account/FriendsModal';
import './AccountMenu.css';

export type AccountModalKind = 'profile' | 'friends' | 'settings';

interface AccountMenuProps {
  /**
   * `menu` (default): fixed top-right cluster on menu pages.
   * `header`: square icon buttons that match the in-game header chrome.
   */
  variant?: 'menu' | 'header';
}

const DEFAULT_AVATAR = { image: 'hobbit.png', color: '#2d5a27' };

function modalFromSearch(param: string | null): AccountModalKind | null {
  return param === 'profile' || param === 'friends' || param === 'settings' ? param : null;
}

/**
 * Profile / Friends / Settings buttons (in that order). Renders nothing when
 * signed out. The friends button carries a red badge with the pending-request count.
 */
export default function AccountMenu({ variant = 'menu' }: AccountMenuProps) {
  const [searchParams] = useSearchParams();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  // Signed out at mount → already "loaded" (no effect work, no sync setState).
  const [loaded, setLoaded] = useState(() => !getAuthToken());
  const [pendingCount, setPendingCount] = useState(0);
  const [turnGames, setTurnGames] = useState<MyTurnGame[]>([]);
  const [turnError, setTurnError] = useState(false);
  const currentGameId = useMatch('/game/:gameId')?.params.gameId;
  const otherTurnGames = useMemo(() => turnGames.filter((game) => game.id !== currentGameId), [turnGames, currentGameId]);
  // Old /profile route redirects here with ?modal=profile: open on first render.
  const [open, setOpen] = useState<AccountModalKind | null>(() =>
    modalFromSearch(searchParams.get('modal')),
  );

  const refreshPendingCount = useCallback(() => {
    const token = getAuthToken();
    if (!token) {
      setPendingCount(0);
      setTurnGames([]);
      setTurnError(false);
      return;
    }
    void api.getPendingFriendCount().then((r) => {
      if (getAuthToken() === token) setPendingCount(r.count);
    }).catch(() => {});
    void api.getMyTurnGames().then((r) => {
      if (getAuthToken() !== token) return;
      setTurnGames(r.games);
      setTurnError(false);
    }).catch(() => {
      if (getAuthToken() === token) setTurnError(true);
    });
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = () => {
      if (!getAuthToken()) return;
      api
        .authMe()
        .then((p) => {
          if (!cancelled) setPlayer(p);
        })
        .catch(() => {
          if (!cancelled) setPlayer(null);
        })
        .finally(() => {
          if (!cancelled) setLoaded(true);
        });
      refreshPendingCount();
    };

    if (getAuthToken()) load();

    // Logout/login while mounted (e.g. Log out inside the profile modal on this page).
    const onAuthChanged = () => {
      if (!getAuthToken()) {
        setPlayer(null);
        setLoaded(true);
        setPendingCount(0);
        setTurnGames([]);
        setTurnError(false);
        setOpen(null);
      } else {
        load();
      }
    };
    const onFocus = () => {
      if (getAuthToken()) refreshPendingCount();
    };
    window.addEventListener(AUTH_CHANGED_EVENT, onAuthChanged);
    window.addEventListener('focus', onFocus);
    const poll = window.setInterval(() => {
      if (getAuthToken()) refreshPendingCount();
    }, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
      window.removeEventListener(AUTH_CHANGED_EVENT, onAuthChanged);
      window.removeEventListener('focus', onFocus);
    };
  }, [refreshPendingCount]);

  // Strip ?modal=... from the URL without a navigation (modal already opened above).
  useEffect(() => {
    if (searchParams.get('modal') === null) return;
    const next = new URLSearchParams(searchParams);
    next.delete('modal');
    const query = next.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
    window.history.replaceState(window.history.state, '', url);
  }, [searchParams]);

  if (!loaded || !player) return null;

  const avatar = player.avatar ?? DEFAULT_AVATAR;
  const ring = tierRingProps(player.tier);
  const profileTitle = player.tier?.name ? `Profile · ${player.tier.name}` : 'Profile';
  const badgeCount = pendingCount + otherTurnGames.length;
  const friendsLabel = `Open friends (${pendingCount} pending requests, ${otherTurnGames.length} multiplayer games awaiting your turn)`;

  return (
    <>
      <div className={`account-menu account-menu--${variant}`}>
        <button
          type="button"
          className={`account-menu__btn account-menu__btn--avatar${ring.className}${open === 'profile' ? ' account-menu__btn--active' : ''}`}
          style={{ backgroundColor: avatar.color, ...ring.style }}
          onClick={() => setOpen(open === 'profile' ? null : 'profile')}
          title={profileTitle}
          aria-label={profileTitle}
          aria-pressed={open === 'profile'}
        >
          <img
            src={`/assets/units/${avatar.image}`}
            alt=""
            className="account-menu__avatar-img"
            onError={(e) => {
              e.currentTarget.style.visibility = 'hidden';
            }}
          />
          <AvatarFrame tier={player.tier} />
        </button>
        <button
          type="button"
          className={`account-menu__btn account-menu__btn--icon${open === 'friends' ? ' account-menu__btn--active' : ''}`}
          onClick={() => {
            if (open !== 'friends') refreshPendingCount();
            setOpen(open === 'friends' ? null : 'friends');
          }}
          title={friendsLabel}
          aria-label={friendsLabel}
          aria-pressed={open === 'friends'}
        >
          <svg className="account-menu__icon" viewBox="0 0 24 24" aria-hidden>
            <circle cx="9" cy="8.2" r="3.4" fill="currentColor" />
            <path
              d="M2.8 19.2c0-3.1 2.8-5.4 6.2-5.4s6.2 2.3 6.2 5.4"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            />
            <circle cx="17" cy="9" r="2.6" fill="currentColor" />
            <path
              d="M15.6 14.2c2.9-.3 5.6 1.5 5.6 4.4"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            />
          </svg>
          {badgeCount > 0 && (
            <span className="account-menu__badge" aria-hidden>
              {badgeCount > 99 ? '99+' : badgeCount}
            </span>
          )}
        </button>
        <button
          type="button"
          className={`account-menu__btn account-menu__btn--icon${open === 'settings' ? ' account-menu__btn--active' : ''}`}
          onClick={() => setOpen(open === 'settings' ? null : 'settings')}
          title="Settings"
          aria-label="Open settings"
          aria-pressed={open === 'settings'}
        >
          <svg className="account-menu__icon" viewBox="0 0 24 24" aria-hidden>
            <path
              d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            />
            <path
              d="M19.3 13.4a1.6 1.6 0 0 0 .3 1.7l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-1.7-.3 1.6 1.6 0 0 0-1 1.4v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.7.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0 .3-1.7 1.6 1.6 0 0 0-1.4-1h-.2a1.9 1.9 0 1 1 0-3.8h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.7l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.6 1.6 0 0 0 1.7.3h.1a1.6 1.6 0 0 0 1-1.4v-.2a1.9 1.9 0 1 1 3.8 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.7-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0-.3 1.7v.1a1.6 1.6 0 0 0 1.4 1h.2a1.9 1.9 0 1 1 0 3.8h-.1a1.6 1.6 0 0 0-1.4 1Z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>

      {open === 'profile' && (
        <ProfileModal player={player} onPlayerChange={setPlayer} onClose={() => setOpen(null)} />
      )}
      {open === 'settings' && (
        <SettingsModal player={player} onPlayerChange={setPlayer} onClose={() => setOpen(null)} />
      )}
      {open === 'friends' && (
        <FriendsModal
          myTurnGames={otherTurnGames}
          myTurnError={turnError}
          onRefreshTurns={refreshPendingCount}
          onClose={() => {
            setOpen(null);
            refreshPendingCount();
          }}
          onRequestsChanged={refreshPendingCount}
        />
      )}
    </>
  );
}
