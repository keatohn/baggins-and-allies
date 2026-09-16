import { useEffect, useRef, useState } from 'react';
import TURN_MUSIC_M4A from 'virtual:turn-music-m4a';
import MENU_MUSIC_M4A from 'virtual:menu-music-m4a';
import LOBBY_MUSIC_M4A from 'virtual:lobby-music-m4a';
import SFX_M4A from 'virtual:sfx-m4a';
import { GAME_AUDIO_BASE, startMenuAmbience, stopMenuAmbience } from '../../audio/gameAudio';

export type AudioKind = 'turn' | 'menu' | 'lobby' | 'sfx';

const GROUPS: { kind: AudioKind; label: string; files: string[] }[] = [
  { kind: 'turn', label: 'Turn music', files: TURN_MUSIC_M4A },
  { kind: 'menu', label: 'Menu', files: MENU_MUSIC_M4A },
  { kind: 'lobby', label: 'Lobby', files: LOBBY_MUSIC_M4A },
  { kind: 'sfx', label: 'SFX', files: SFX_M4A },
];

function relPath(kind: AudioKind, file: string): string {
  return `${kind}/${file}`;
}

function clampPct(n: number): number {
  return Math.min(200, Math.max(0, Math.round(n)));
}

function pctFor(gains: Record<string, number>, rel: string): number {
  const v = gains[rel];
  return typeof v === 'number' && Number.isFinite(v) ? clampPct(v) : 100;
}

export function AudioPanel({
  gains,
  onGainsChange,
}: {
  gains: Record<string, number>;
  onGainsChange: (next: Record<string, number>) => void;
}) {
  const previewRef = useRef<HTMLAudioElement | null>(null);
  const [playingRel, setPlayingRel] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      previewRef.current?.pause();
      previewRef.current = null;
      startMenuAmbience('menu');
    };
  }, []);

  const setPct = (rel: string, pct: number) => {
    const next = { ...gains };
    if (pct === 100) delete next[rel];
    else next[rel] = pct;
    onGainsChange(next);
  };

  const stopPreview = (resumeMenu = true) => {
    previewRef.current?.pause();
    previewRef.current = null;
    setPlayingRel(null);
    if (resumeMenu) startMenuAmbience('menu');
  };

  const playPreview = (kind: AudioKind, file: string) => {
    const rel = relPath(kind, file);
    if (playingRel === rel) {
      stopPreview(true);
      return;
    }
    stopPreview(false);
    stopMenuAmbience();
    const audio = new Audio(`${GAME_AUDIO_BASE}/${rel}`);
    audio.volume = Math.min(1, Math.max(0, pctFor(gains, rel) / 100));
    audio.addEventListener('ended', () => {
      if (previewRef.current === audio) {
        previewRef.current = null;
        setPlayingRel(null);
        startMenuAmbience('menu');
      }
    });
    previewRef.current = audio;
    setPlayingRel(rel);
    void audio.play().catch(() => {
      if (previewRef.current === audio) {
        previewRef.current = null;
        setPlayingRel(null);
        startMenuAmbience('menu');
      }
    });
  };

  return (
    <div className="admin-form">
      <p className="admin-form__micro">
        Per-file mix is global (not per setup). Each percent multiplies each player&apos;s profile volume in-game.
        Play preview uses only this percent (not your profile sliders) so you can compare files, and it pauses menu
        music until you stop. Default 100%. Range 0–200%.
      </p>
      {GROUPS.map((group) => (
        <section key={group.kind} className="admin-audio-group">
          <h3 className="admin-form__subtitle">{group.label}</h3>
          {group.files.length === 0 ? (
            <p className="admin-form__micro">No {group.kind} .m4a files found.</p>
          ) : (
            <table className="admin-audio-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Volume</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {group.files.map((file) => {
                  const rel = relPath(group.kind, file);
                  const pct = pctFor(gains, rel);
                  return (
                    <tr key={rel}>
                      <td>
                        <code className="admin-audio-file">{file}</code>
                      </td>
                      <td>
                        <label className="admin-audio-pct">
                          <input
                            type="number"
                            min={0}
                            max={200}
                            className="admin-form__input admin-form__input--narrow"
                            value={String(pct)}
                            onChange={(e) => {
                              const v = e.target.value.trim();
                              if (v === '') {
                                setPct(rel, 100);
                                return;
                              }
                              const n = Number(v);
                              if (!Number.isFinite(n)) return;
                              setPct(rel, clampPct(n));
                            }}
                          />
                          <span>%</span>
                        </label>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="admin-page__btn"
                          onClick={() => playPreview(group.kind, file)}
                        >
                          {playingRel === rel ? 'Stop' : 'Play'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </div>
  );
}
