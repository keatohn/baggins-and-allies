import { useCallback, useEffect, useRef, useState } from 'react';
import {
  setGameMusicVolume,
  setGameSfxVolume,
  setMenuMusicVolume,
} from '../../audio/gameAudio';
import { api } from '../../services/api';
import type { AuthPlayer } from '../../services/api';
import ModalShell from './ModalShell';

const DEFAULT_MENU_MUSIC_PCT = 0;
const DEFAULT_GAME_MUSIC_PCT = 0;
const DEFAULT_SFX_PCT = 0;

interface SettingsModalProps {
  player: AuthPlayer;
  onPlayerChange: (player: AuthPlayer) => void;
  onClose: () => void;
}

/** Audio preferences (lifted from the old Profile page) + read-only account email. */
export default function SettingsModal({ player, onPlayerChange, onClose }: SettingsModalProps) {
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
    const hasGame =
      Object.prototype.hasOwnProperty.call(a, 'game_music_volume') ||
      Object.prototype.hasOwnProperty.call(a, 'music_volume') ||
      Object.prototype.hasOwnProperty.call(a, 'master_volume');
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
    isMountedRef.current = true;
    applyPlayerAudio(player);
    return () => {
      isMountedRef.current = false;
    };
  }, [player, applyPlayerAudio]);

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
        onPlayerChange(updated);
        applyPlayerAudio(updated);
        audioSavedSnapshotRef.current = {
          menuMusic: Math.round(
            (updated.audio?.menu_music_volume ?? updated.audio?.music_volume ?? updated.audio?.master_volume ?? DEFAULT_MENU_MUSIC_PCT / 100) * 100,
          ),
          gameMusic: Math.round(
            (updated.audio?.game_music_volume ?? updated.audio?.music_volume ?? updated.audio?.master_volume ?? DEFAULT_GAME_MUSIC_PCT / 100) * 100,
          ),
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
    [applyPlayerAudio, onPlayerChange],
  );

  const handleMuteChange = (next: boolean) => {
    setMuted(next);
    setAudioError(null);
    audioSnapshotRef.current = { ...audioSnapshotRef.current, muted: next };
  };

  const audioDirty =
    menuMusicVolumePct !== audioSavedSnapshotRef.current.menuMusic ||
    gameMusicVolumePct !== audioSavedSnapshotRef.current.gameMusic ||
    sfxVolumePct !== audioSavedSnapshotRef.current.sfx ||
    muted !== audioSavedSnapshotRef.current.muted;

  return (
    <ModalShell title="Settings" onClose={onClose} className="account-modal--settings">
      <section className="account-section" aria-label="Email">
        <p className="account-email">
          <strong>Email:</strong> {player.email}
        </p>
      </section>

      <section className="account-section account-section--last" aria-labelledby="settings-audio-heading">
        <h3 id="settings-audio-heading" className="account-section__title">
          Audio
        </h3>
        <div className="settings-audio-slider">
          <label className="settings-audio-label" htmlFor="settings-menu-music-volume">
            Menu music volume
          </label>
          <div className="settings-audio-row">
            <input
              id="settings-menu-music-volume"
              type="range"
              min={0}
              max={100}
              value={menuMusicVolumePct}
              onChange={(e) => {
                const v = Number(e.target.value);
                setMenuMusicVolumePct(v);
                setAudioError(null);
                audioSnapshotRef.current = { ...audioSnapshotRef.current, menuMusic: v };
                if (!muted && !audioSaving) setMenuMusicVolume(v / 100);
              }}
              disabled={muted || audioSaving}
              className="settings-audio-range"
            />
            <span className="settings-audio-pct" aria-live="polite">
              {muted ? '—' : `${menuMusicVolumePct}%`}
            </span>
          </div>
        </div>
        <div className="settings-audio-slider">
          <label className="settings-audio-label" htmlFor="settings-game-music-volume">
            Game music volume
          </label>
          <div className="settings-audio-row">
            <input
              id="settings-game-music-volume"
              type="range"
              min={0}
              max={100}
              value={gameMusicVolumePct}
              onChange={(e) => {
                const v = Number(e.target.value);
                setGameMusicVolumePct(v);
                setAudioError(null);
                audioSnapshotRef.current = { ...audioSnapshotRef.current, gameMusic: v };
                if (!muted && !audioSaving) setGameMusicVolume(v / 100);
              }}
              disabled={muted || audioSaving}
              className="settings-audio-range"
            />
            <span className="settings-audio-pct" aria-live="polite">
              {muted ? '—' : `${gameMusicVolumePct}%`}
            </span>
          </div>
        </div>
        <div className="settings-audio-slider">
          <label className="settings-audio-label" htmlFor="settings-sfx-volume">
            Sound effect volume
          </label>
          <div className="settings-audio-row">
            <input
              id="settings-sfx-volume"
              type="range"
              min={0}
              max={100}
              value={sfxVolumePct}
              onChange={(e) => {
                const v = Number(e.target.value);
                setSfxVolumePct(v);
                setAudioError(null);
                audioSnapshotRef.current = { ...audioSnapshotRef.current, sfx: v };
                if (!muted && !audioSaving) setGameSfxVolume(v / 100);
              }}
              disabled={muted || audioSaving}
              className="settings-audio-range"
            />
            <span className="settings-audio-pct" aria-live="polite">
              {muted ? '—' : `${sfxVolumePct}%`}
            </span>
          </div>
        </div>
        <div className="settings-audio-actions">
          <label className="settings-audio-mute">
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
              className="account-btn"
              data-no-ui-click-sfx
              onClick={() => void flushAudioSave(menuMusicVolumePct, gameMusicVolumePct, sfxVolumePct, muted)}
              disabled={audioSaving}
            >
              {audioSaving ? 'Saving…' : 'Save Audio'}
            </button>
          )}
        </div>
        {audioSaving && <p className="account-msg account-msg--subtle">Saving…</p>}
        {audioError && <p className="account-msg account-msg--error">{audioError}</p>}
      </section>
    </ModalShell>
  );
}
