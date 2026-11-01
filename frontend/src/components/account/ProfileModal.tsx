import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setAuthToken } from '../../services/api';
import type { AuthPlayer } from '../../services/api';
import ModalShell from './ModalShell';

/** Basic background-color palette for the avatar circle (default green first). */
const COLOR_PALETTE = [
  '#2d5a27',
  '#1a3a63',
  '#6b2d2d',
  '#5b3a8a',
  '#8a6b1f',
  '#2f6b6b',
  '#b3541e',
  '#1f1f1f',
  '#5c5346',
  '#7a8a5a',
];

const DEFAULT_AVATAR = { image: 'hobbit.png', color: '#2d5a27' };

interface ProfileModalProps {
  player: AuthPlayer;
  onPlayerChange: (player: AuthPlayer) => void;
  onClose: () => void;
}

export default function ProfileModal({ player, onPlayerChange, onClose }: ProfileModalProps) {
  const navigate = useNavigate();
  const [icons, setIcons] = useState<string[]>([]);
  const [iconsLoading, setIconsLoading] = useState(true);
  const [avatarImage, setAvatarImage] = useState(player.avatar?.image ?? DEFAULT_AVATAR.image);
  const [avatarColor, setAvatarColor] = useState(player.avatar?.color ?? DEFAULT_AVATAR.color);
  const [avatarSaving, setAvatarSaving] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [avatarSaved, setAvatarSaved] = useState(false);

  const [usernameEditing, setUsernameEditing] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState(player.username);
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [usernameSuccess, setUsernameSuccess] = useState(false);
  const [savingUsername, setSavingUsername] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getAvatarIcons()
      .then((r) => {
        if (!cancelled) setIcons(Array.isArray(r.icons) ? r.icons : []);
      })
      .catch(() => {
        if (!cancelled) setIcons([]);
      })
      .finally(() => {
        if (!cancelled) setIconsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const currentImage = player.avatar?.image ?? DEFAULT_AVATAR.image;
  const currentColor = player.avatar?.color ?? DEFAULT_AVATAR.color;
  const avatarDirty = avatarImage !== currentImage || avatarColor !== currentColor;

  const iconOptions = (() => {
    const list = [...icons];
    if (avatarImage && !list.includes(avatarImage)) list.unshift(avatarImage);
    return list;
  })();

  const handleAvatarSave = async () => {
    setAvatarError(null);
    setAvatarSaved(false);
    setAvatarSaving(true);
    try {
      const updated = await api.updateMyProfile({ avatar: { image: avatarImage, color: avatarColor } });
      onPlayerChange(updated);
      setAvatarSaved(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Could not save avatar';
      const isGeneric = /^(Failed to fetch|Load failed|NetworkError|Network request failed)$/i.test(msg.trim());
      setAvatarError(isGeneric ? 'Could not save avatar. Check your connection and try again.' : msg);
    } finally {
      setAvatarSaving(false);
    }
  };

  const startUsernameEdit = () => {
    setUsernameSuccess(false);
    setUsernameError(null);
    setUsernameDraft(player.username);
    setUsernameEditing(true);
  };

  const cancelUsernameEdit = () => {
    setUsernameDraft(player.username);
    setUsernameError(null);
    setUsernameEditing(false);
  };

  const handleUsernameSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setUsernameError(null);
    setUsernameSuccess(false);
    const trimmed = usernameDraft.trim();
    if (!/^[a-zA-Z0-9_]{2,32}$/.test(trimmed)) {
      setUsernameError('Username must be 2–32 characters, letters numbers and underscore only');
      return;
    }
    if (trimmed === player.username) {
      setUsernameEditing(false);
      return;
    }
    setSavingUsername(true);
    try {
      const updated = await api.updateMyProfile({ username: trimmed });
      onPlayerChange(updated);
      setUsernameDraft(updated.username);
      setUsernameSuccess(true);
      setUsernameEditing(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Could not update username';
      const isGeneric = /^(Failed to fetch|Load failed|NetworkError|Network request failed)$/i.test(msg.trim());
      setUsernameError(isGeneric ? 'Could not update username. Check your connection and try again.' : msg);
    } finally {
      setSavingUsername(false);
    }
  };

  const handleLogout = () => {
    setAuthToken(null);
    navigate('/', { replace: true });
  };

  const usernameDirty = usernameDraft.trim() !== player.username;
  const usernameInvalid = !/^[a-zA-Z0-9_]{2,32}$/.test(usernameDraft.trim());

  return (
    <ModalShell title="Profile" onClose={onClose} className="account-modal--profile">
      <section className="account-section" aria-label="Avatar">
        <div className="profile-avatar-preview-wrap">
          <div className="profile-avatar-preview" style={{ backgroundColor: avatarColor }}>
            <img
              src={`/assets/units/${avatarImage}`}
              alt=""
              className="profile-avatar-preview__img"
              onError={(e) => {
                e.currentTarget.style.visibility = 'hidden';
              }}
            />
          </div>
          <p className="account-subtle">Choose a unit icon and background color.</p>
        </div>

        <div className="profile-avatar-icons" role="group" aria-label="Avatar icon">
          {iconsLoading ? (
            <p className="account-subtle">Loading icons…</p>
          ) : (
            iconOptions.map((icon) => (
              <button
                key={icon}
                type="button"
                className={`profile-avatar-icon${icon === avatarImage ? ' profile-avatar-icon--selected' : ''}`}
                onClick={() => {
                  setAvatarImage(icon);
                  setAvatarSaved(false);
                  setAvatarError(null);
                }}
                title={icon.replace(/\.png$/i, '').replace(/_/g, ' ')}
                aria-label={`Use ${icon}`}
                aria-pressed={icon === avatarImage}
              >
                <img src={`/assets/units/${icon}`} alt="" />
              </button>
            ))
          )}
        </div>

        <div className="profile-avatar-colors" role="group" aria-label="Avatar background color">
          {COLOR_PALETTE.map((color) => (
            <button
              key={color}
              type="button"
              className={`profile-avatar-color${color === avatarColor ? ' profile-avatar-color--selected' : ''}`}
              style={{ backgroundColor: color }}
              onClick={() => {
                setAvatarColor(color);
                setAvatarSaved(false);
                setAvatarError(null);
              }}
              title={color}
              aria-label={`Use color ${color}`}
              aria-pressed={color === avatarColor}
            />
          ))}
        </div>

        {avatarDirty && (
          <button
            type="button"
            className="account-btn"
            onClick={() => void handleAvatarSave()}
            disabled={avatarSaving}
          >
            {avatarSaving ? 'Saving…' : 'Save Avatar'}
          </button>
        )}
        {avatarSaved && !avatarError && !avatarDirty && (
          <p className="account-msg account-msg--success">Avatar saved.</p>
        )}
        {avatarError && <p className="account-msg account-msg--error">{avatarError}</p>}
      </section>

      <section className="account-section" aria-label="Username">
        {!usernameEditing ? (
          <div className="profile-username-row">
            <p className="profile-username-display">
              <strong>Username:</strong> {player.username}
            </p>
            <button type="button" className="account-btn" onClick={startUsernameEdit}>
              Change Username
            </button>
          </div>
        ) : (
          <form className="profile-username-form" onSubmit={handleUsernameSubmit}>
            <label className="account-field-label" htmlFor="account-username">
              Username
              <input
                id="account-username"
                type="text"
                className="account-input"
                value={usernameDraft}
                onChange={(e) => {
                  setUsernameSuccess(false);
                  setUsernameError(null);
                  const next = e.target.value.replace(/[^a-zA-Z0-9_]/g, '').slice(0, 32);
                  setUsernameDraft(next);
                }}
                minLength={2}
                maxLength={32}
                autoComplete="username"
                spellCheck={false}
                autoFocus
              />
            </label>
            {usernameError && <p className="account-msg account-msg--error">{usernameError}</p>}
            <div className="profile-username-actions">
              <button
                type="button"
                className="account-btn account-btn--ghost"
                onClick={cancelUsernameEdit}
                disabled={savingUsername}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="account-btn"
                disabled={savingUsername || !usernameDirty || usernameInvalid}
              >
                {savingUsername ? 'Saving…' : 'Save Username'}
              </button>
            </div>
          </form>
        )}
        {usernameSuccess && !usernameError && !usernameEditing && (
          <p className="account-msg account-msg--success">Username saved.</p>
        )}
      </section>

      <section className="account-section" aria-label="Progress">
        <h3 className="account-section__title">Tier &amp; XP</h3>
        <div className="account-xp">
          <div className="account-xp__row">
            <span className="account-xp__tier">Tier —</span>
            <span className="account-xp__xp">XP coming soon</span>
          </div>
          <div
            className="account-xp__bar"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={0}
            aria-label="XP progress"
          >
            <div className="account-xp__fill" style={{ width: '0%' }} />
          </div>
        </div>
      </section>

      <section className="account-section account-section--last">
        <button type="button" className="account-btn account-btn--danger" onClick={handleLogout}>
          Log out
        </button>
      </section>
    </ModalShell>
  );
}
