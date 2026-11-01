import { useCallback, useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { FriendRequestItem, FriendUser } from '../../services/api';
import ModalShell from './ModalShell';

interface FriendsModalProps {
  onClose: () => void;
  /** Called after any request/friend mutation so the badge count refreshes. */
  onRequestsChanged: () => void;
}

export default function FriendsModal({ onClose, onRequestsChanged }: FriendsModalProps) {
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

  const loadLists = useCallback(async () => {
    try {
      const [reqs, friendRows] = await Promise.all([api.listFriendRequests(), api.listFriends()]);
      setIncoming(reqs.incoming ?? []);
      setOutgoing(reqs.outgoing ?? []);
      setFriends(friendRows.friends ?? []);
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

  return (
    <ModalShell title="Friends" onClose={onClose} className="account-modal--friends">
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
          Who have I played with?
        </button>
        {playedOpen && (
          <>
            {playedLoading && playedWith == null && <p className="account-subtle">Loading…</p>}
            {playedError && <p className="account-msg account-msg--error">{playedError}</p>}
            {playedWith && (
              <ul className="friends-list" aria-label="Players you have played with">
                {playedWith.length === 0 ? (
                  <li className="friends-list__empty">You have not played with anyone yet.</li>
                ) : (
                  playedWith.map((u) => (
                    <li key={u.id} className="friends-list__row">
                      <span className="friends-list__name">{u.username}</span>
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
                          {busyId === `send:${u.username}` ? 'Sending…' : 'Send Friend Request'}
                        </button>
                      )}
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
                  <span className="friends-list__name">{u.username}</span>
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

      <section className="account-section" aria-label="Friend requests">
        <h3 className="account-section__title">Requests</h3>
        {listLoading && <p className="account-subtle">Loading…</p>}
        {!listLoading && incoming.length === 0 && outgoing.length === 0 && (
          <p className="account-subtle">No pending requests.</p>
        )}
        {incoming.length > 0 && (
          <ul className="friends-list" aria-label="Incoming requests">
            {incoming.map((r) => (
              <li key={r.id} className="friends-list__row">
                <span className="friends-list__name">{r.username}</span>
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
                <span className="friends-list__name">{r.username}</span>
                <span className="friends-list__tag">Pending</span>
              </li>
            ))}
          </ul>
        )}
        {listError && <p className="account-msg account-msg--error">{listError}</p>}
        {actionError && <p className="account-msg account-msg--error">{actionError}</p>}
      </section>

      <section className="account-section account-section--last" aria-label="Your friends">
        <h3 className="account-section__title">Your friends</h3>
        {listLoading ? (
          <p className="account-subtle">Loading…</p>
        ) : friends.length === 0 ? (
          <p className="account-subtle">No friends yet.</p>
        ) : (
          <ul className="friends-list" aria-label="Friends list">
            {friends.map((f) => (
              <li key={f.id} className="friends-list__row">
                <span className="friends-list__name">{f.username}</span>
                <span className="friends-list__tag">Friend</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </ModalShell>
  );
}
