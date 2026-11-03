import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AuthPlayer, GamePlayerPortrait, PlayerProfileCard } from '../../services/api';
import ModalShell from './ModalShell';
import ProfileModal from './ProfileModal';
import '../AccountMenu.css';

export interface SeatPortrait extends GamePlayerPortrait {
  playerId: string;
}

const FALLBACK_AVATAR = { image: 'hobbit.png', color: '#2d5a27' };

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

interface SeatPlayerModalProps {
  seat: SeatPortrait;
  onClose: () => void;
  onSeatChange: (next: SeatPortrait) => void;
}

/** Friend and self open the full profile card. Everyone else gets a small card with the friends-list actions. */
export default function SeatPlayerModal({ seat, onClose, onSeatChange }: SeatPlayerModalProps) {
  const fullProfile = seat.relation === 'friend' || seat.relation === 'self';
  const [profile, setProfile] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(fullProfile);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (seat.relation !== 'friend' && seat.relation !== 'self') {
      setProfile(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load = seat.relation === 'self'
      ? api.authMe()
      : api.getPlayerProfile(seat.playerId).then(profileToPlayer);
    load
      .then((next) => {
        if (!cancelled) setProfile(next);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load profile');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [seat.playerId, seat.relation]);

  if (fullProfile && profile && !loading) {
    return (
      <ProfileModal
        player={profile}
        readOnly={seat.relation !== 'self'}
        onPlayerChange={seat.relation === 'self' ? setProfile : undefined}
        onClose={onClose}
      />
    );
  }

  const face = seat.avatar ?? FALLBACK_AVATAR;

  const send = () => {
    setBusy(true);
    setError(null);
    void api
      .sendFriendRequest(seat.username)
      .then((res) => {
        onSeatChange({ ...seat, relation: 'pending_out', request_id: res.request?.id ?? seat.request_id });
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not send request'))
      .finally(() => setBusy(false));
  };

  const accept = () => {
    if (!seat.request_id) return;
    setBusy(true);
    setError(null);
    void api
      .acceptFriendRequest(seat.request_id)
      .then(() => onSeatChange({ ...seat, relation: 'friend' }))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not accept request'))
      .finally(() => setBusy(false));
  };

  const decline = () => {
    if (!seat.request_id) return;
    setBusy(true);
    setError(null);
    void api
      .declineFriendRequest(seat.request_id)
      .then(() => onSeatChange({ ...seat, relation: 'none', request_id: null }))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not decline request'))
      .finally(() => setBusy(false));
  };

  return (
    <ModalShell title={seat.username} onClose={onClose} className="account-modal--seat">
      <div className="seat-card">
        <span className="seat-card__avatar" style={{ backgroundColor: face.color }} aria-hidden>
          <img src={`/assets/units/${face.image}`} alt="" />
        </span>
        <p className="seat-card__name">{seat.username}</p>
        {loading && <p className="account-msg account-msg--subtle">Loading…</p>}
        {!loading && seat.relation === 'pending_out' && <p className="account-msg account-msg--subtle">Request sent</p>}
        {!loading && seat.relation === 'pending_in' && seat.request_id && (
          <div className="seat-card__actions">
            <button type="button" className="account-btn account-btn--small" disabled={busy} onClick={accept}>
              Accept
            </button>
            <button type="button" className="account-btn account-btn--small account-btn--ghost" disabled={busy} onClick={decline}>
              Decline
            </button>
          </div>
        )}
        {!loading && seat.relation === 'pending_in' && !seat.request_id && (
          <p className="account-msg account-msg--subtle">Wants to be friends</p>
        )}
        {!loading && seat.relation === 'none' && (
          <button type="button" className="account-btn account-btn--small" disabled={busy} onClick={send}>
            {busy ? 'Sending…' : 'Send Friend Request'}
          </button>
        )}
        {error && (
          <p className="account-msg account-msg--error" role="alert">
            {error}
          </p>
        )}
      </div>
    </ModalShell>
  );
}
