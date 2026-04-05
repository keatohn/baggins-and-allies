import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, getAuthToken, setAuthToken } from '../services/api';
import type { AuthPlayer } from '../services/api';
import './Profile.css';

const DEFAULT_MENU_MUSIC_PCT = 50;
const DEFAULT_GAME_MUSIC_PCT = 25;
const DEFAULT_SFX_PCT = 25;

export default function Profile() {
  const navigate = useNavigate();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(true);
  const [usernameEditing, setUsernameEditing] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState('');
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [usernameSuccess, setUsernameSuccess] = useState(false);
  const [savingUsername, setSavingUsername] = useState(false);

  const [menuMusicVolumePct, setMenuMusicVolumePct] = useState(DEFAULT_MENU_MUSIC_PCT);
  const [gameMusicVolumePct, setGameMusicVolumePct] = useState(DEFAULT_GAME_MUSIC_PCT);
  const [sfxVolumePct, setSfxVolumePct] = useState(DEFAULT_SFX_PCT);
  const [muted, setMuted] = useState(false);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [audioSaving, setAudioSaving] = useState(false);
  const isMountedRef = useRef(true);
  const audioSavedSnapshotRef = useRef({
    menuMusic: DEFAULT_MENU_MUSIC_PCT,
    gameMusic: DEFAULT_GAME_MUSIC_PCT,
    sfx: DEFAULT_SFX_PCT,
    muted: false,
  });
  const audioSnapshotRef = useRef({
    menuMusic: DEFAULT_MENU_MUSIC_PCT,
    gameMusic: DEFAULT_GAME_MUSIC_PCT,
    sfx: DEFAULT_SFX_PCT,
    muted: false,
  });

  const applyPlayerAudio = useCallback((p: AuthPlayer) => {
    const a = p.audio;
    if (!a) return;
    const prev = audioSnapshotRef.current;
    const hasMenu = Object.prototype.hasOwnProperty.call(a, 'menu_music_volume');
    const hasGame = Object.prototype.hasOwnProperty.call(a, 'game_music_volume') || Object.prototype.hasOwnProperty.call(a, 'music_volume') || Object.prototype.hasOwnProperty.call(a, 'master_volume');
    const hasSfx = Object.prototype.hasOwnProperty.call(a, 'sfx_volume');
    const gameMusic =
      hasGame && typeof a.game_music_volume === 'number'
        ? a.game_music_volume
        : hasGame && typeof a.music_volume === 'number'
          ? a.music_volume
        : hasGame && typeof a.master_volume === 'number'
          ? a.master_volume
          : prev.gameMusic / 100;
    const menuMusic =
      hasMenu && typeof a.menu_music_volume === 'number'
        ? a.menu_music_volume
        : prev.menuMusic / 100;
    const sfx = hasSfx && typeof a.sfx_volume === 'number' ? a.sfx_volume : prev.sfx / 100;
    const menuPct = Math.round(menuMusic * 100);
    const gamePct = Math.round(gameMusic * 100);
    const sPct = Math.round(sfx * 100);
    setMenuMusicVolumePct(menuPct);
    setGameMusicVolumePct(gamePct);
    setSfxVolumePct(sPct);
    audioSnapshotRef.current = { menuMusic: menuPct, gameMusic: gamePct, sfx: sPct, muted: !!a.muted };
    audioSavedSnapshotRef.current = { menuMusic: menuPct, gameMusic: gamePct, sfx: sPct, muted: !!a.muted };
    if (typeof a.muted === 'boolean') setMuted(a.muted);
  }, []);

  useEffect(() => {
    if (!getAuthToken()) {
      navigate('/', { replace: true });
      return;
    }
    api
      .authMe()
      .then((p) => {
        setPlayer(p);
        setUsernameDraft(p.username);
        applyPlayerAudio(p);
      })
      .catch(() => navigate('/', { replace: true }))
      .finally(() => setLoading(false));
  }, [navigate, applyPlayerAudio]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const handleLogout = () => {
    setAuthToken(null);
    navigate('/', { replace: true });
  };

  const startUsernameEdit = () => {
    if (!player) return;
    setUsernameSuccess(false);
    setUsernameError(null);
    setUsernameDraft(player.username);
    setUsernameEditing(true);
  };

  const cancelUsernameEdit = () => {
    if (!player) return;
    setUsernameDraft(player.username);
    setUsernameError(null);
    setUsernameEditing(false);
  };

  const handleUsernameSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!player) return;
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
      setPlayer(updated);
      setUsernameDraft(updated.username);
      applyPlayerAudio(updated);
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

  const flushAudioSave = useCallback(
    async (menuMusicPct: number, gameMusicPct: number, sfxPct: number, mute: boolean) => {
      setAudioSaving(true);
      setAudioError(null);
      try {
        const updated = await api.updateMyProfile({
          audio: {
            menu_music_volume: menuMusicPct / 100,
            game_music_volume: gameMusicPct / 100,
            music_volume: gameMusicPct / 100,
            sfx_volume: sfxPct / 100,
            muted: mute,
          },
        });
        if (!isMountedRef.current) return;
        setPlayer(updated);
        applyPlayerAudio(updated);
        audioSavedSnapshotRef.current = {
          menuMusic: Math.round((updated.audio?.menu_music_volume ?? updated.audio?.music_volume ?? updated.audio?.master_volume ?? DEFAULT_MENU_MUSIC_PCT / 100) * 100),
          gameMusic: Math.round((updated.audio?.game_music_volume ?? updated.audio?.music_volume ?? updated.audio?.master_volume ?? DEFAULT_GAME_MUSIC_PCT / 100) * 100),
          sfx: Math.round((updated.audio?.sfx_volume ?? DEFAULT_SFX_PCT / 100) * 100),
          muted: !!updated.audio?.muted,
        };
      } catch (err) {
        if (!isMountedRef.current) return;
        const msg = err instanceof Error ? err.message : 'Could not save audio settings';
        const isGeneric = /^(Failed to fetch|Load failed|NetworkError|Network request failed)$/i.test(msg.trim());
        setAudioError(isGeneric ? 'Could not save. Check your connection and try again.' : msg);
      } finally {
        if (isMountedRef.current) setAudioSaving(false);
      }
    },
    [applyPlayerAudio],
  );

  const handleMuteChange = (next: boolean) => {
    setMuted(next);
    setAudioError(null);
    audioSnapshotRef.current = { ...audioSnapshotRef.current, muted: next };
  };

  if (loading) return <div className="profile-page">Loading…</div>;
  if (!player) return null;

  const usernameDirty = usernameDraft.trim() !== player.username;
  const usernameInvalid = !/^[a-zA-Z0-9_]{2,32}$/.test(usernameDraft.trim());
  const audioDirty =
    menuMusicVolumePct !== audioSavedSnapshotRef.current.menuMusic ||
    gameMusicVolumePct !== audioSavedSnapshotRef.current.gameMusic ||
    sfxVolumePct !== audioSavedSnapshotRef.current.sfx ||
    muted !== audioSavedSnapshotRef.current.muted;

  return (
    <div className="profile-page">
      <h1 className="profile-page__title">Profile</h1>

      <div className="profile-page__panels">
        <div className="profile-page__panel profile-page__panel--account">
          <div className="profile-page__username-block">
            {!usernameEditing ? (
              <>
                <p className="profile-page__email">
                  <strong>Email:</strong> {player.email}
                </p>
                <p className="profile-page__username-display">
                  <strong>Username:</strong> {player.username}
                </p>
                <button type="button" className="profile-page__username-action" onClick={startUsernameEdit}>
                  Change Username
                </button>
              </>
            ) : (
              <form className="profile-page__username-edit-form" onSubmit={handleUsernameSubmit}>
                <p className="profile-page__email">
                  <strong>Email:</strong> {player.email}
                </p>
                <label className="profile-page__field-label" htmlFor="profile-username">
                  Username
                  <input
                    id="profile-username"
                    type="text"
                    className="profile-page__input"
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
                {usernameError && <p className="profile-page__msg profile-page__msg--error">{usernameError}</p>}
                <div className="profile-page__username-actions">
                  <button
                    type="button"
                    className="profile-page__username-cancel"
                    onClick={cancelUsernameEdit}
                    disabled={savingUsername}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="profile-page__username-action"
                    disabled={savingUsername || !usernameDirty || usernameInvalid}
                  >
                    {savingUsername ? 'Saving…' : 'Save Username'}
                  </button>
                </div>
              </form>
            )}
            {usernameSuccess && !usernameError && !usernameEditing && (
              <p className="profile-page__msg profile-page__msg--success">Username saved.</p>
            )}
          </div>

          <button type="button" className="profile-page__username-action profile-page__logout" onClick={handleLogout}>
            Log out
          </button>
        </div>

        <div className="profile-page__panel profile-page__panel--audio">
          <section className="profile-page__audio" aria-labelledby="profile-audio-heading">
            <h2 id="profile-audio-heading" className="profile-page__audio-title">
              Audio
            </h2>
            <label className="profile-page__audio-slider-label" htmlFor="profile-menu-music-volume">
              Menu music volume
              <div className="profile-page__audio-slider-row">
                <input
                  id="profile-menu-music-volume"
                  type="range"
                  min={0}
                  max={100}
                  value={menuMusicVolumePct}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setMenuMusicVolumePct(v);
                    setAudioError(null);
                    audioSnapshotRef.current = { ...audioSnapshotRef.current, menuMusic: v };
                  }}
                  disabled={muted || audioSaving}
                  className="profile-page__audio-range"
                />
                <span className="profile-page__audio-pct" aria-live="polite">
                  {muted ? '—' : `${menuMusicVolumePct}%`}
                </span>
              </div>
            </label>
            <label className="profile-page__audio-slider-label" htmlFor="profile-game-music-volume">
              Game music volume
              <div className="profile-page__audio-slider-row">
                <input
                  id="profile-game-music-volume"
                  type="range"
                  min={0}
                  max={100}
                  value={gameMusicVolumePct}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setGameMusicVolumePct(v);
                    setAudioError(null);
                    audioSnapshotRef.current = { ...audioSnapshotRef.current, gameMusic: v };
                  }}
                  disabled={muted || audioSaving}
                  className="profile-page__audio-range"
                />
                <span className="profile-page__audio-pct" aria-live="polite">
                  {muted ? '—' : `${gameMusicVolumePct}%`}
                </span>
              </div>
            </label>
            <label className="profile-page__audio-slider-label" htmlFor="profile-sfx-volume">
              Sound effect volume
              <div className="profile-page__audio-slider-row">
                <input
                  id="profile-sfx-volume"
                  type="range"
                  min={0}
                  max={100}
                  value={sfxVolumePct}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setSfxVolumePct(v);
                    setAudioError(null);
                    audioSnapshotRef.current = { ...audioSnapshotRef.current, sfx: v };
                  }}
                  disabled={muted || audioSaving}
                  className="profile-page__audio-range"
                />
                <span className="profile-page__audio-pct" aria-live="polite">
                  {muted ? '—' : `${sfxVolumePct}%`}
                </span>
              </div>
            </label>
            <div className="profile-page__audio-actions">
              <label className="profile-page__audio-mute">
                <input
                  type="checkbox"
                  checked={muted}
                  onChange={(e) => handleMuteChange(e.target.checked)}
                  disabled={audioSaving}
                />
                Mute all audio
              </label>
              {audioDirty && (
                <button
                  type="button"
                  className="profile-page__audio-save"
                  data-no-ui-click-sfx
                  onClick={() => void flushAudioSave(menuMusicVolumePct, gameMusicVolumePct, sfxVolumePct, muted)}
                  disabled={audioSaving}
                >
                  {audioSaving ? 'Saving…' : 'Save Audio'}
                </button>
              )}
            </div>
            {audioSaving && <p className="profile-page__msg profile-page__msg--subtle">Saving…</p>}
            {audioError && <p className="profile-page__msg profile-page__msg--error">{audioError}</p>}
          </section>
        </div>
      </div>

      <Link
        to="/"
        className="profile-page__menu-btn"
      >
        Menu
      </Link>
    </div>
  );
}
