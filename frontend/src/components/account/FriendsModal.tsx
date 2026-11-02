import { useCallback, useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AuthPlayer, FriendRequestItem, FriendUser, GameInvitation, PlayerAvatarSettings, PlayerProfileCard } from '../../services/api';
import { useMenuScroll } from '../MenuScrollContext';
import ModalShell from './ModalShell';
import ProfileModal from './ProfileModal';

const FALLBACK_AVATAR: PlayerAvatarSettings = { image: 'hobbit.png', color: '#2d5a27' };

function profileToPlayer(card: PlayerProfileCard): AuthPlayer {
  return {
    id: card.id,
    email: '',
    username: card.username,
    avatar: card.avatar,
    xp: card.xp,
    tier: card.tier,
    next_tier: card.next_tier ?? null,
    tier_floor_xp: card.tier_floor_xp,
    next_tier_xp: card.next_tier_xp,
  };
}

function PersonIdentity({
  username,
  avatar,
  onOpen,
}: {
  username: string;
  avatar?: PlayerAvatarSettings;
  onOpen?: () => void;
}) {
  const face = avatar ?? FALLBACK_AVATAR;
  const picture = (
    <span className="friends-list__avatar" style={{ backgroundColor: face.color }} aria-hidden>
      <img src={`/assets/units/${face.image}`} alt="" />
    </span>
  );
  const name = <span className="friends-list__name">{username}</span>;
  if (!onOpen) {
    return (
      <span className="friends-list__person">
        {picture}
        {name}
      </span>
    );
  }
  return (
    <button type="button" className="friends-list__person" onClick={onOpen} aria-label={`View ${username}'s profile`}>
      {picture}
      {name}
    </button>
  );
}

interface FriendsModalProps {
  onClose: () => void;
  /** Called after any request/friend mutation so the badge count refreshes. */
  onRequestsChanged: () => void;
}

export default function FriendsModal({ onClose, onRequestsChanged }: FriendsModalProps) {
  const { navigateWithScroll } = useMenuScroll();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<FriendUser[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [incoming, setIncoming] = useState<FriendRequestItem[]>([]);
  const [outgoing, setOutgoing] = useState<FriendRequestItem[]>([]);
  const [friends, setFriends] = useState<FriendUser[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [playedOpen, setPlayedOpen] = useState(false);
  const [playedWith, setPlayedWith] = useState<FriendUser[] | null>(null);
  const [playedLoading, setPlayedLoading] = useState(false);
  const [playedError, setPlayedError] = useState<string | null>(null);
  const [viewing, setViewing] = useState<AuthPlayer | null>(null);
  const [viewError, setViewError] = useState<string | null>(null);
  const [invitations, setInvitations] = useState<GameInvitation[]>([]);

  const loadLists = useCallback(async () => {
    try {
      const [reqs, friendRows, inviteRows] = await Promise.all([
        api.listFriendRequests(),
        api.listFriends(),
        api.listGameInvites(),
      ]);
      setIncoming(reqs.incoming ?? []);
      setOutgoing(reqs.outgoing ?? []);
      setFriends(friendRows.friends ?? []);
      setInvitations(inviteRows.invitations ?? []);
      setListError(null);
    } catch (err) {
      setListError(err instanceof Error ? err.message : 'Could not load friends');
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLists();
  }, [loadLists]);

  const loadPlayedWith = useCallback(async () => {
    setPlayedLoading(true);
    try {
      const r = await api.listPlayedWith();
      setPlayedWith(r.users ?? []);
      setPlayedError(null);
    } catch (err) {
      setPlayedError(err instanceof Error ? err.message : 'Could not load players');
    } finally {
      setPlayedLoading(false);
    }
  }, []);

  const togglePlayedWith = () => {
    setPlayedOpen((open) => {
      const next = !open;
      if (next) void loadPlayedWith();
      return next;
    });
  };

  const runAction = async (action: () => Promise<unknown>) => {
    setActionError(null);
    try {
      await action();
      await loadLists();
      if (playedOpen) await loadPlayedWith();
      onRequestsChanged();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Action failed';
      const isGeneric = /^(Failed to fetch|Load failed|NetworkError|Network request failed)$/i.test(msg.trim());
      setActionError(isGeneric ? 'Could not update. Check your connection and try again.' : msg);
    }
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    setSearchError(null);
    if (!q) {
      setResults(null);
      return;
    }
    setSearching(true);
    try {
      const r = await api.searchFriends(q);
      setResults(r.users ?? []);
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : 'Search failed');
      setResults(null);
    } finally {
      setSearching(false);
    }
  };

  const sendRequest = (username: string) => {
    setBusyId(`send:${username}`);
    void runAction(() => api.sendFriendRequest(username)).finally(() => setBusyId(null));
  };

  const acceptRequest = (id: string) => {
    setBusyId(id);
    void runAction(() => api.acceptFriendRequest(id)).finally(() => setBusyId(null));
  };

  const declineRequest = (id: string) => {
    setBusyId(id);
    void runAction(() => api.declineFriendRequest(id)).finally(() => setBusyId(null));
  };

  const listedBelow = (user: FriendUser) =>
    user.relation === 'friend' || user.relation === 'pending_out' || user.relation === 'pending_in';
  const othersPlayedWith = (playedWith ?? []).filter((user) => !listedBelow(user));

  const openFriend = (user: FriendUser) => {
    setViewError(null);
    void api
      .getPlayerProfile(user.id)
      .then((card) => setViewing(profileToPlayer(card)))
      .catch((err) => setViewError(err instanceof Error ? err.message : 'Could not open profile'));
  };

  const acceptInvite = (invite: GameInvitation) => {
    setActionError(null);
    setBusyId(invite.id);
    void api
      .acceptGameInvite(invite.id)
      .then((res) => {
        onRequestsChanged();
        onClose();
        navigateWithScroll(`/game/${res.game_id}`, undefined, { force: true });
      })
      .catch((err) => setActionError(err instanceof Error ? err.message : 'Could not accept invitation'))
      .finally(() => setBusyId(null));
  };

  const declineInvite = (invite: GameInvitation) => {
    setBusyId(invite.id);
    void runAction(() => api.declineGameInvite(invite.id)).finally(() => setBusyId(null));
  };

  return (
    <>
    <ModalShell
      title="Friends"
      onClose={onClose}
      className="account-modal--friends"
      closeOnEscape={viewing == null}
    >
      {invitations.length > 0 && (
        <section className="account-section" aria-label="Invitations">
          <h3 className="account-section__title">Invitations</h3>
          {actionError && <p className="account-msg account-msg--error">{actionError}</p>}
          <ul className="friends-list" aria-label="Game invitations">
            {invitations.map((invite) => (
              <li key={invite.id} className="friends-list__row friends-list__row--invite">
                <span className="friends-list__invite-copy">
                  <span className="friends-list__name">{invite.inviter_username}</span>
                  <span className="friends-list__tag">invited you to {invite.game_name}</span>
                </span>
                <span className="friends-list__actions">
                  <button
                    type="button"
                    className="account-btn account-btn--small"
                    onClick={() => acceptInvite(invite)}
                    disabled={busyId === invite.id}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    className="account-btn account-btn--small account-btn--ghost"
                    onClick={() => declineInvite(invite)}
                    disabled={busyId === invite.id}
                  >
                    Decline
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="account-section" aria-label="Find friends">
        <h3 className="account-section__title">Find a friend</h3>
        <form className="friends-search" onSubmit={handleSearch}>
          <input
            type="text"
            className="account-input friends-search__input"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value.slice(0, 32));
              setSearchError(null);
            }}
            placeholder="Username"
            aria-label="Username to search"
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit" className="account-btn" disabled={searching || !query.trim()}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>
        {searchError && <p className="account-msg account-msg--error">{searchError}</p>}
        <button
          type="button"
          className="friends-played-with__toggle"
          aria-expanded={playedOpen}
          onClick={togglePlayedWith}
        >
          Who else have I played with?
        </button>
        {playedOpen && (
          <>
            {playedLoading && playedWith == null && <p className="account-subtle">Loading…</p>}
            {playedError && <p className="account-msg account-msg--error">{playedError}</p>}
            {playedWith && (
              <ul className="friends-list" aria-label="Other players you have played with">
                {playedWith.length === 0 ? (
                  <li className="friends-list__empty">You have not played with anyone yet.</li>
                ) : othersPlayedWith.length === 0 ? (
                  <li className="friends-list__empty">Everyone you have played with is already listed below.</li>
                ) : (
                  othersPlayedWith.map((u) => (
                    <li key={u.id} className="friends-list__row">
                      <PersonIdentity username={u.username} avatar={u.avatar} />
                      <button
                        type="button"
                        className="account-btn account-btn--small"
                        onClick={() => sendRequest(u.username)}
                        disabled={busyId === `send:${u.username}`}
                      >
                        {busyId === `send:${u.username}` ? 'Sending…' : 'Send Friend Request'}
                      </button>
                    </li>
                  ))
                )}
              </ul>
            )}
          </>
        )}
        {results && (
          <ul className="friends-list" aria-label="Search results">
            {results.length === 0 ? (
              <li className="friends-list__empty">No users found.</li>
            ) : (
              results.map((u) => (
                <li key={u.id} className="friends-list__row">
                  <PersonIdentity
                    username={u.username}
                    avatar={u.avatar}
                    onOpen={u.relation === 'friend' ? () => openFriend(u) : undefined}
                  />
                  {u.relation === 'friend' ? (
                    <span className="friends-list__tag">Friends</span>
                  ) : u.relation === 'pending_out' ? (
                    <span className="friends-list__tag">Request sent</span>
                  ) : u.relation === 'pending_in' ? (
                    <span className="friends-list__tag">Wants to be friends</span>
                  ) : (
                    <button
                      type="button"
                      className="account-btn account-btn--small"
                      onClick={() => sendRequest(u.username)}
                      disabled={busyId === `send:${u.username}`}
                    >
                      {busyId === `send:${u.username}` ? 'Sending…' : 'Add'}
                    </button>
                  )}
                </li>
              ))
            )}
          </ul>
        )}
      </section>

      <section className="account-section" aria-label="Friends">
        <h3 className="account-section__title">Friends</h3>
        {listLoading ? (
          <p className="account-subtle">Loading…</p>
        ) : friends.length === 0 ? (
          <p className="account-subtle">No friends yet.</p>
        ) : (
          <ul className="friends-list" aria-label="Friends list">
            {friends.map((f) => (
              <li key={f.id} className="friends-list__row">
                <PersonIdentity username={f.username} avatar={f.avatar} onOpen={() => openFriend(f)} />
                <span className="friends-list__tag">Friend</span>
              </li>
            ))}
          </ul>
        )}
        {viewError && <p className="account-msg account-msg--error">{viewError}</p>}
      </section>

      <section className="account-section account-section--last" aria-label="Friend requests">
        <h3 className="account-section__title">Requests</h3>
        {listLoading && <p className="account-subtle">Loading…</p>}
        {!listLoading && incoming.length === 0 && outgoing.length === 0 && (
          <p className="account-subtle">No pending requests.</p>
        )}
        {incoming.length > 0 && (
          <ul className="friends-list" aria-label="Incoming requests">
            {incoming.map((r) => (
              <li key={r.id} className="friends-list__row">
                <PersonIdentity username={r.username} avatar={r.avatar} />
                <span className="friends-list__actions">
                  <button
                    type="button"
                    className="account-btn account-btn--small"
                    onClick={() => acceptRequest(r.id)}
                    disabled={busyId === r.id}
                  >
                    Accept
                  </button>
                  <button
                    type="button"
                    className="account-btn account-btn--small account-btn--ghost"
                    onClick={() => declineRequest(r.id)}
                    disabled={busyId === r.id}
                  >
                    Decline
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}
        {outgoing.length > 0 && (
          <ul className="friends-list" aria-label="Outgoing requests">
            {outgoing.map((r) => (
              <li key={r.id} className="friends-list__row">
                <PersonIdentity username={r.username} avatar={r.avatar} />
                <span className="friends-list__tag">Pending</span>
              </li>
            ))}
          </ul>
        )}
        {listError && <p className="account-msg account-msg--error">{listError}</p>}
        {actionError && <p className="account-msg account-msg--error">{actionError}</p>}
      </section>
    </ModalShell>
    {viewing && (
      <ProfileModal player={viewing} readOnly onClose={() => setViewing(null)} />
    )}
    </>
  );
}
