const STORAGE_MUTE = 'gameAudioMuted';
const STORAGE_MUSIC_VOLUME = 'gameAudioMusicVolume';
const STORAGE_MENU_MUSIC_VOLUME = 'gameAudioMenuMusicVolume';
const STORAGE_GAME_MUSIC_VOLUME = 'gameAudioInGameMusicVolume';
const STORAGE_SFX_VOLUME = 'gameAudioSfxVolume';
const LEGACY_STORAGE_VOLUME = 'gameAudioVolume';

export const GAME_AUDIO_BASE = '/assets/audio';
const MENU_AUDIO_BASE = `${GAME_AUDIO_BASE}/menu`;
const LOBBY_AUDIO_BASE = `${GAME_AUDIO_BASE}/lobby`;
const TURN_AUDIO_BASE = `${GAME_AUDIO_BASE}/turn`;
const SFX_AUDIO_BASE = `${GAME_AUDIO_BASE}/sfx`;

export const TURN_AUDIO_EXTS = ['.m4a', '.mp3', '.ogg'] as const;
export const MENU_AUDIO_EXTS = ['.m4a', '.mp3', '.ogg'] as const;
export const GAME_SFX_EXTS = ['.m4a', '.mp3', '.ogg'] as const;

export const TURN_SWITCH_CROSSFADE_MS = 720;
export const TURN_LOOP_CROSSFADE_MS = 620;
export const TURN_FADE_IN_MS = 560;

export const MENU_LOOP_CROSSFADE_MS = 1200;
export const MENU_FADE_IN_MS = 780;

const MENU_AMBIENCE_GAIN = 0.35;

/** Default when no saved preference (game music + SFX only; menu ambience stays louder by default). */
const DEFAULT_GAME_MUSIC_LEVEL = 0.25;
const DEFAULT_SFX_LEVEL = 0.25;

const turnSession = { v: 0 };
const menuSession = { v: 0 };

let lastTurnCueAt = 0;
/** `${factionId}:${trackStem}` — debounce only when the same faction retriggers within the window (e.g. React Strict Mode). */
let lastDebouncedTurnCueKey = '';
/** Last faction we actually started a turn cue for; when this changes, always switch tracks (skip turn, etc.). */
let lastTurnCueFactionId = '';
const TURN_CUE_DEBOUNCE_MS = 900;

let turnLoop: TurnPlaylistPair | null = null;
let menuLoop: MenuLoopPair | null = null;
let currentMenuAmbienceMode: 'menu' | 'lobby' | null = null;
let lastUiClickAt = 0;
const UI_CLICK_DEBOUNCE_MS = 30;
let lastMovementSfxAt = 0;
const MOVEMENT_SFX_DEBOUNCE_MS = 90;
let clickSfxUrl: string | null = null;
const movementSfxUrlCache: Partial<Record<'march' | 'wings' | 'ship', string>> = {};
const movementSfxResolving: Partial<Record<'march' | 'wings' | 'ship', boolean>> = {};
let drumSfxUrl: string | null = null;
let arrowsSfxUrl: string | null = null;
let siegeworksSfxUrl: string | null = null;
let clickSfxResolving = false;
let drumSfxResolving = false;
let arrowsSfxResolving = false;
let siegeworksSfxResolving = false;

/** Last ~1s of siegeworks stinger fades to zero (avoids abrupt file tail). */
const SIEGWORKS_END_FADE_SEC = 1;

type ActiveSiegeworksStinger = { audio: HTMLAudioElement; cleanup: () => void };
let activeSiegeworksStinger: ActiveSiegeworksStinger | null = null;

function teardownMenuPlayback(): void {
  menuSession.v++;
  const prev = menuLoop;
  menuLoop = null;
  currentMenuAmbienceMode = null;
  if (prev) prev.destroy();
}

function readGameMusicVolume(): number {
  if (typeof localStorage === 'undefined') return DEFAULT_GAME_MUSIC_LEVEL;
  if (localStorage.getItem(STORAGE_MUTE) === '1') return 0;
  const gameRaw = localStorage.getItem(STORAGE_GAME_MUSIC_VOLUME);
  const musicRaw = gameRaw == null ? localStorage.getItem(STORAGE_MUSIC_VOLUME) : gameRaw;
  const legacyRaw = musicRaw == null ? localStorage.getItem(LEGACY_STORAGE_VOLUME) : null;
  const raw = parseFloat(musicRaw ?? legacyRaw ?? String(DEFAULT_GAME_MUSIC_LEVEL));
  if (!Number.isFinite(raw)) return DEFAULT_GAME_MUSIC_LEVEL;
  return Math.min(1, Math.max(0, raw));
}

function readMenuMusicVolume(): number {
  if (typeof localStorage === 'undefined') return 0.5;
  if (localStorage.getItem(STORAGE_MUTE) === '1') return 0;
  const menuRaw = localStorage.getItem(STORAGE_MENU_MUSIC_VOLUME);
  const fallbackRaw = menuRaw == null ? localStorage.getItem(STORAGE_MUSIC_VOLUME) : menuRaw;
  const legacyRaw = fallbackRaw == null ? localStorage.getItem(LEGACY_STORAGE_VOLUME) : null;
  const raw = parseFloat(fallbackRaw ?? legacyRaw ?? '0.5');
  if (!Number.isFinite(raw)) return 0.5;
  return Math.min(1, Math.max(0, raw));
}

function readSfxVolume(): number {
  if (typeof localStorage === 'undefined') return DEFAULT_SFX_LEVEL;
  if (localStorage.getItem(STORAGE_MUTE) === '1') return 0;
  const raw = parseFloat(localStorage.getItem(STORAGE_SFX_VOLUME) ?? String(DEFAULT_SFX_LEVEL));
  if (!Number.isFinite(raw)) return DEFAULT_SFX_LEVEL;
  return Math.min(1, Math.max(0, raw));
}

function readVolume(): number {
  return readGameMusicVolume();
}

function sanitizeFactionId(id: string): string {
  return id.replace(/[^a-zA-Z0-9_-]/g, '');
}

/** Single filename → stem under assets/audio/turn; no faction fallback. */
function stemFromMusicFilename(music: string | null | undefined): string | null {
  if (!music || !String(music).trim()) return null;
  const base = String(music).trim().split(/[/\\]/).pop() ?? '';
  const cleaned = base.replace(/[^a-zA-Z0-9_.-]/g, '');
  if (!cleaned) return null;
  const stem = cleaned.replace(/\.(m4a|mp3|ogg|wav)$/i, '');
  return stem || null;
}

/** Ordered stems for the turn playlist; falls back to [factionId] when no valid entries. */
function normalizeTurnTrackStems(
  music: string | string[] | null | undefined,
  fallbackFactionId: string,
): string[] {
  const fb = sanitizeFactionId(fallbackFactionId);
  const raw: string[] = Array.isArray(music)
    ? music.filter((x): x is string => typeof x === 'string')
    : music != null && String(music).trim()
      ? [String(music)]
      : [];
  const out = raw.map((m) => stemFromMusicFilename(m)).filter((s): s is string => Boolean(s));
  if (out.length === 0 && fb) return [fb];
  return out;
}

function smoothStep01(t: number): number {
  const x = Math.min(1, Math.max(0, t));
  return x * x * (3 - 2 * x);
}

function fadeGain(
  audio: HTMLAudioElement,
  fromG: number,
  toG: number,
  durationMs: number,
  session: { v: number },
  sessionId: number,
  onDone?: () => void,
): void {
  if (durationMs <= 0) {
    const m = readGameMusicVolume();
    audio.volume = m * Math.min(1, Math.max(0, toG));
    onDone?.();
    return;
  }
  const t0 = performance.now();
  const step = () => {
    if (sessionId !== session.v) return;
    const m = readGameMusicVolume();
    const u = Math.min(1, (performance.now() - t0) / durationMs);
    const ue = smoothStep01(u);
    const g = fromG + (toG - fromG) * ue;
    audio.volume = m * Math.min(1, Math.max(0, g));
    if (u < 1) requestAnimationFrame(step);
    else onDone?.();
  };
  requestAnimationFrame(step);
}

function fadeGainMenu(
  audio: HTMLAudioElement,
  fromG: number,
  toG: number,
  durationMs: number,
  session: { v: number },
  sessionId: number,
  onDone?: () => void,
): void {
  const cap = MENU_AMBIENCE_GAIN;
  if (durationMs <= 0) {
    const m = readMenuMusicVolume();
    audio.volume = m * cap * Math.min(1, Math.max(0, toG));
    onDone?.();
    return;
  }
  const t0 = performance.now();
  const step = () => {
    if (sessionId !== session.v) return;
    const m = readMenuMusicVolume();
    const u = Math.min(1, (performance.now() - t0) / durationMs);
    const g = fromG + (toG - fromG) * u;
    audio.volume = m * cap * Math.min(1, Math.max(0, g));
    if (u < 1) requestAnimationFrame(step);
    else onDone?.();
  };
  requestAnimationFrame(step);
}

class TurnPlaylistPair {
  private activeIsA = true;
  private handoff = false;
  private readonly onTimeUpdate: () => void;
  private readonly onEnded: () => void;

  readonly sessionId: number;
  readonly stems: readonly string[];
  private playingStemIndex: number;
  readonly a: HTMLAudioElement;
  readonly b: HTMLAudioElement;

  constructor(stems: readonly string[], initialStemIndex: number, firstUrl: string, sessionId: number) {
    this.stems = stems;
    this.playingStemIndex = initialStemIndex;
    this.sessionId = sessionId;
    this.a = new Audio(firstUrl);
    this.b = new Audio();
    this.a.preload = 'auto';
    this.b.preload = 'auto';

    const minDurSec = (TURN_LOOP_CROSSFADE_MS / 1000) * 2.2;
    const fadeSec = TURN_LOOP_CROSSFADE_MS / 1000;

    const startHandoff = () => {
      if (sessionId !== turnSession.v || this.handoff) return;
      const nextStemIndex = (this.playingStemIndex + 1) % this.stems.length;
      const incoming = this.activeIsA ? this.b : this.a;
      if (!incoming.src) return;
      this.handoff = true;
      const outgoing = this.activeIsA ? this.a : this.b;
      incoming.currentTime = 0;
      void incoming.play().then(() => {
        if (sessionId !== turnSession.v) return;
        fadeGain(outgoing, 1, 0, TURN_LOOP_CROSSFADE_MS, turnSession, sessionId, () => {
          outgoing.pause();
          outgoing.currentTime = 0;
        });
        fadeGain(incoming, 0, 1, TURN_LOOP_CROSSFADE_MS, turnSession, sessionId, () => {
          this.activeIsA = !this.activeIsA;
          this.playingStemIndex = nextStemIndex;
          this.handoff = false;
          const idle = this.getIdleElement();
          this.preloadStemForIndex(idle, (this.playingStemIndex + 1) % this.stems.length, sessionId);
        });
      }).catch(() => {
        this.handoff = false;
      });
    };

    this.onTimeUpdate = () => {
      if (sessionId !== turnSession.v) return;
      const active = this.activeIsA ? this.a : this.b;
      const dur = active.duration;
      if (!dur || !isFinite(dur) || dur <= 0) return;
      if (active.paused) return;
      if (dur < minDurSec) return;

      const rem = dur - active.currentTime;
      if (rem <= fadeSec && rem >= 0) {
        startHandoff();
      }
    };

    this.onEnded = () => {
      if (sessionId !== turnSession.v) return;
      const active = this.activeIsA ? this.a : this.b;
      const dur = active.duration;
      if (!dur || !isFinite(dur)) return;
      if (dur >= minDurSec) return;
      startHandoff();
    };

    this.a.addEventListener('timeupdate', this.onTimeUpdate);
    this.b.addEventListener('timeupdate', this.onTimeUpdate);
    this.a.addEventListener('ended', this.onEnded);
    this.b.addEventListener('ended', this.onEnded);

    queueMicrotask(() => {
      if (sessionId !== turnSession.v) return;
      this.preloadStemForIndex(this.b, (initialStemIndex + 1) % this.stems.length, sessionId);
    });
  }

  private preloadStemForIndex(el: HTMLAudioElement, stemIndex: number, sid: number): void {
    const stem = this.stems[stemIndex];
    tryPlayFirstWorkingUrl(
      TURN_AUDIO_EXTS.map((ext) => `${TURN_AUDIO_BASE}/${stem}${ext}`),
      (url) => {
        if (sid !== turnSession.v) return;
        try {
          el.pause();
          el.currentTime = 0;
          el.src = url;
          el.load();
        } catch {}
      },
      () => {
        if (sid !== turnSession.v) return;
        const active = this.activeIsA ? this.a : this.b;
        if (active.src && el !== active) {
          try {
            el.pause();
            el.currentTime = 0;
            el.src = active.src;
            el.load();
          } catch {}
        }
      },
    );
  }

  isCrossfading(): boolean {
    return this.handoff;
  }

  getActiveElement(): HTMLAudioElement {
    return this.activeIsA ? this.a : this.b;
  }

  getIdleElement(): HTMLAudioElement {
    return this.activeIsA ? this.b : this.a;
  }

  destroy(): void {
    this.a.removeEventListener('timeupdate', this.onTimeUpdate);
    this.b.removeEventListener('timeupdate', this.onTimeUpdate);
    this.a.removeEventListener('ended', this.onEnded);
    this.b.removeEventListener('ended', this.onEnded);
    this.a.pause();
    this.b.pause();
    this.a.src = '';
    this.b.src = '';
  }

  startFirstPlay(): Promise<void> {
    this.a.volume = 0;
    return this.a.play().then(() => undefined);
  }
}

class MenuLoopPair {
  private activeIsA = true;
  private handoff = false;
  private readonly onTimeUpdate: () => void;
  private readonly onEnded: () => void;

  readonly sessionId: number;
  readonly a: HTMLAudioElement;
  readonly b: HTMLAudioElement;

  constructor(urlA: string, urlB: string, sessionId: number) {
    this.sessionId = sessionId;
    this.a = new Audio(urlA);
    this.b = new Audio(urlB);
    this.a.preload = 'auto';
    this.b.preload = 'auto';

    const minDurSec = (MENU_LOOP_CROSSFADE_MS / 1000) * 2.2;
    const fadeSec = MENU_LOOP_CROSSFADE_MS / 1000;

    const startHandoff = () => {
      if (sessionId !== menuSession.v || this.handoff) return;
      this.handoff = true;
      const active = this.activeIsA ? this.a : this.b;
      const incoming = this.activeIsA ? this.b : this.a;
      incoming.currentTime = 0;
      const outgoing = active;
      void incoming.play().then(() => {
        if (sessionId !== menuSession.v) return;
        fadeGainMenu(outgoing, 1, 0, MENU_LOOP_CROSSFADE_MS, menuSession, sessionId, () => {
          outgoing.pause();
          outgoing.currentTime = 0;
        });
        fadeGainMenu(incoming, 0, 1, MENU_LOOP_CROSSFADE_MS, menuSession, sessionId, () => {
          this.activeIsA = !this.activeIsA;
          this.handoff = false;
        });
      }).catch(() => {
        this.handoff = false;
      });
    };

    this.onTimeUpdate = () => {
      if (sessionId !== menuSession.v) return;
      const active = this.activeIsA ? this.a : this.b;
      const dur = active.duration;
      if (!dur || !isFinite(dur) || dur <= 0) return;
      if (active.paused) return;
      if (dur < minDurSec) return;

      const rem = dur - active.currentTime;
      if (rem <= fadeSec && rem >= 0) {
        startHandoff();
      }
    };

    this.onEnded = () => {
      if (sessionId !== menuSession.v) return;
      const active = this.activeIsA ? this.a : this.b;
      const dur = active.duration;
      if (!dur || !isFinite(dur)) return;
      if (dur >= minDurSec) return;
      startHandoff();
    };

    this.a.addEventListener('timeupdate', this.onTimeUpdate);
    this.b.addEventListener('timeupdate', this.onTimeUpdate);
    this.a.addEventListener('ended', this.onEnded);
    this.b.addEventListener('ended', this.onEnded);
  }

  isCrossfading(): boolean {
    return this.handoff;
  }

  getActiveElement(): HTMLAudioElement {
    return this.activeIsA ? this.a : this.b;
  }

  getIdleElement(): HTMLAudioElement {
    return this.activeIsA ? this.b : this.a;
  }

  destroy(): void {
    this.a.removeEventListener('timeupdate', this.onTimeUpdate);
    this.b.removeEventListener('timeupdate', this.onTimeUpdate);
    this.a.removeEventListener('ended', this.onEnded);
    this.b.removeEventListener('ended', this.onEnded);
    this.a.pause();
    this.b.pause();
    this.a.src = '';
    this.b.src = '';
  }

  startFirstPlay(): Promise<void> {
    this.a.volume = 0;
    return this.a.play().then(() => undefined);
  }
}

export function isGameAudioMuted(): boolean {
  try {
    return localStorage.getItem(STORAGE_MUTE) === '1';
  } catch {
    return false;
  }
}

export function setGameAudioMuted(muted: boolean): void {
  try {
    localStorage.setItem(STORAGE_MUTE, muted ? '1' : '0');
  } catch {}
  if (muted) stopMenuAmbience();
}

export function getGameAudioVolume(): number {
  return readGameMusicVolume();
}

export function getMenuMusicVolume(): number {
  return readMenuMusicVolume();
}

export function getGameSfxVolume(): number {
  return readSfxVolume();
}

/**
 * Lightweight UI click tone for menu and in-game interactions.
 * Uses existing SFX volume + mute settings from local storage.
 */
export function playUiClickSound(): void {
  const now = performance.now();
  if (now - lastUiClickAt < UI_CLICK_DEBOUNCE_MS) return;
  lastUiClickAt = now;

  const sfx = readSfxVolume();
  if (sfx <= 0.001) return;
  if (clickSfxUrl) {
    const a = new Audio(clickSfxUrl);
    a.volume = Math.min(1, Math.max(0, sfx));
    void a.play().catch(() => {});
    return;
  }
  if (clickSfxResolving) return;
  clickSfxResolving = true;
  const urls = [
    ...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/sword${ext}`),
    ...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/click${ext}`),
  ];
  tryPlayFirstWorkingUrl(urls, (url) => {
    clickSfxResolving = false;
    clickSfxUrl = url;
    const a = new Audio(url);
    a.volume = Math.min(1, Math.max(0, readSfxVolume()));
    void a.play().catch(() => {});
  }, () => {
    clickSfxResolving = false;
  });
}

export function movementSfxCategoryFromUnitDef(
  unitDef: { archetype?: string; tags?: string[] } | undefined,
): 'ground' | 'aerial' | 'naval' {
  if (!unitDef) return 'ground';
  const arch = unitDef.archetype ?? '';
  const tags = unitDef.tags ?? [];
  if (arch === 'aerial' || tags.includes('aerial')) return 'aerial';
  if (arch === 'naval' || tags.includes('naval')) return 'naval';
  return 'ground';
}

function stemForMovementCategory(kind: 'ground' | 'aerial' | 'naval'): 'march' | 'wings' | 'ship' {
  if (kind === 'aerial') return 'wings';
  if (kind === 'naval') return 'ship';
  return 'march';
}

/**
 * Movement and mobilization feedback: ground → march, aerial → wings, naval → ship (assets/audio/sfx).
 */
export function playMovementSfx(kind: 'ground' | 'aerial' | 'naval'): void {
  const now = performance.now();
  if (now - lastMovementSfxAt < MOVEMENT_SFX_DEBOUNCE_MS) return;
  lastMovementSfxAt = now;
  const sfx = readSfxVolume();
  if (sfx <= 0.001) return;

  const stem = stemForMovementCategory(kind);
  const cached = movementSfxUrlCache[stem];
  if (cached) {
    const a = new Audio(cached);
    a.volume = Math.min(1, Math.max(0, sfx));
    void a.play().catch(() => {});
    return;
  }
  if (movementSfxResolving[stem]) return;
  movementSfxResolving[stem] = true;
  const urls = [...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/${stem}${ext}`)];
  tryPlayFirstWorkingUrl(
    urls,
    (url) => {
      movementSfxResolving[stem] = false;
      movementSfxUrlCache[stem] = url;
      const a = new Audio(url);
      a.volume = Math.min(1, Math.max(0, readSfxVolume()));
      void a.play().catch(() => {});
    },
    () => {
      movementSfxResolving[stem] = false;
    },
  );
}

/** @deprecated use playMovementSfx('ground') */
export function playMarchMoveSound(): void {
  playMovementSfx('ground');
}

/** Combat modal: one shot per unit shelf when dice are revealed (may overlap between shelves). */
export function playCombatDiceShelfRevealSound(): void {
  const sfx = readSfxVolume();
  if (sfx <= 0.001) return;
  if (drumSfxUrl) {
    const a = new Audio(drumSfxUrl);
    a.volume = Math.min(1, Math.max(0, sfx));
    void a.play().catch(() => {});
    return;
  }
  if (drumSfxResolving) return;
  drumSfxResolving = true;
  const urls = [...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/drum${ext}`)];
  tryPlayFirstWorkingUrl(urls, (url) => {
    drumSfxResolving = false;
    drumSfxUrl = url;
    const a = new Audio(url);
    a.volume = Math.min(1, Math.max(0, readSfxVolume()));
    void a.play().catch(() => {});
  }, () => {
    drumSfxResolving = false;
  });
}

/** Combat modal: defender archer prefire begins after Continue (not the initial Start). */
export function playArcherPrefireCommenceSound(): void {
  const sfx = readSfxVolume();
  if (sfx <= 0.001) return;
  if (arrowsSfxUrl) {
    const a = new Audio(arrowsSfxUrl);
    a.volume = Math.min(1, Math.max(0, sfx));
    void a.play().catch(() => {});
    return;
  }
  if (arrowsSfxResolving) return;
  arrowsSfxResolving = true;
  const urls = [...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/arrows${ext}`)];
  tryPlayFirstWorkingUrl(urls, (url) => {
    arrowsSfxResolving = false;
    arrowsSfxUrl = url;
    const a = new Audio(url);
    a.volume = Math.min(1, Math.max(0, readSfxVolume()));
    void a.play().catch(() => {});
  }, () => {
    arrowsSfxResolving = false;
  });
}

export function stopSiegeworksRoundCommenceSound(): void {
  if (!activeSiegeworksStinger) return;
  activeSiegeworksStinger.cleanup();
  activeSiegeworksStinger = null;
}

/** Combat modal: siegeworks round begins — one-shot with volume ramp down over the last ~1s. */
export function playSiegeworksRoundCommenceSound(): void {
  const sfx = readSfxVolume();
  if (sfx <= 0.001) return;

  const startPlayback = (url: string) => {
    stopSiegeworksRoundCommenceSound();
    const maxVol = Math.min(1, Math.max(0, sfx));
    const audio = new Audio(url);
    audio.volume = maxVol;

    const onTimeUpdate = () => {
      const d = audio.duration;
      if (!d || !Number.isFinite(d)) return;
      const rem = d - audio.currentTime;
      const fadeLen = Math.min(SIEGWORKS_END_FADE_SEC, d);
      if (rem <= fadeLen) {
        audio.volume = maxVol * Math.max(0, Math.min(1, rem / fadeLen));
      } else {
        audio.volume = maxVol;
      }
    };

    const cleanup = () => {
      audio.removeEventListener('timeupdate', onTimeUpdate);
      audio.removeEventListener('ended', onEnded);
      audio.pause();
      try {
        audio.removeAttribute('src');
        audio.load();
      } catch {}
      if (activeSiegeworksStinger?.audio === audio) activeSiegeworksStinger = null;
    };

    const onEnded = () => {
      cleanup();
    };

    audio.addEventListener('timeupdate', onTimeUpdate);
    audio.addEventListener('ended', onEnded);
    activeSiegeworksStinger = { audio, cleanup };

    void audio.play().catch(() => {
      cleanup();
    });
  };

  if (siegeworksSfxUrl) {
    startPlayback(siegeworksSfxUrl);
    return;
  }
  if (siegeworksSfxResolving) return;
  siegeworksSfxResolving = true;
  const urls = [...GAME_SFX_EXTS.map((ext) => `${SFX_AUDIO_BASE}/siegeworks${ext}`)];
  tryPlayFirstWorkingUrl(
    urls,
    (url) => {
      siegeworksSfxResolving = false;
      siegeworksSfxUrl = url;
      startPlayback(url);
    },
    () => {
      siegeworksSfxResolving = false;
    },
  );
}

export function setGameAudioVolume(level: number): void {
  setGameMusicVolume(level);
}

export function setGameMusicVolume(level: number): void {
  const v = Math.min(1, Math.max(0, level));
  try {
    localStorage.setItem(STORAGE_GAME_MUSIC_VOLUME, String(v));
    localStorage.setItem(STORAGE_MUSIC_VOLUME, String(v));
  } catch {}
  const m = readGameMusicVolume();

  if (turnLoop && !turnLoop.isCrossfading()) {
    turnLoop.getActiveElement().volume = m;
    turnLoop.getIdleElement().volume = 0;
  }
}

export function setMenuMusicVolume(level: number): void {
  const v = Math.min(1, Math.max(0, level));
  try {
    localStorage.setItem(STORAGE_MENU_MUSIC_VOLUME, String(v));
  } catch {}
  const m = readMenuMusicVolume();
  if (menuLoop && !menuLoop.isCrossfading()) {
    menuLoop.getActiveElement().volume = m * MENU_AMBIENCE_GAIN;
    menuLoop.getIdleElement().volume = 0;
  }
}

export function setGameSfxVolume(level: number): void {
  const v = Math.min(1, Math.max(0, level));
  try {
    localStorage.setItem(STORAGE_SFX_VOLUME, String(v));
  } catch {}
}

export function initGameAudioFromStorage(): void {}

export function syncAudioFromAuthPlayer(
  player: {
    audio?: {
      menu_music_volume?: number;
      game_music_volume?: number;
      music_volume?: number;
      sfx_volume?: number;
      master_volume?: number;
      muted?: boolean;
    };
  } | null | undefined,
): void {
  if (!player?.audio) return;
  const a = player.audio;
  const gameMusic =
    typeof a.game_music_volume === 'number'
      ? a.game_music_volume
      : typeof a.music_volume === 'number'
        ? a.music_volume
        : typeof a.master_volume === 'number'
          ? a.master_volume
          : DEFAULT_GAME_MUSIC_LEVEL;
  const menuMusic =
    typeof a.menu_music_volume === 'number'
      ? a.menu_music_volume
      : gameMusic;
  const sfx = typeof a.sfx_volume === 'number' ? a.sfx_volume : DEFAULT_SFX_LEVEL;
  setGameMusicVolume(gameMusic);
  setMenuMusicVolume(menuMusic);
  setGameSfxVolume(sfx);
  setGameAudioMuted(a.muted ?? false);
}

function tryPlayFirstWorkingUrl(
  urls: readonly string[],
  onReady: (url: string) => void,
  onNoneFound?: () => void,
): void {
  let i = 0;
  const tryNext = () => {
    if (i >= urls.length) {
      onNoneFound?.();
      return;
    }
    const url = urls[i++];
    const probe = new Audio();
    const onError = () => {
      probe.removeEventListener('canplay', onCanPlay);
      probe.removeEventListener('error', onError);
      tryNext();
    };
    const onCanPlay = () => {
      probe.removeEventListener('error', onError);
      onReady(url);
    };
    probe.addEventListener('error', onError, { once: true });
    probe.addEventListener('canplay', onCanPlay, { once: true });
    probe.preload = 'auto';
    probe.src = url;
    probe.load();
  };
  tryNext();
}

function tryPlayFirstWorkingMenuTrack(track: 'menu' | 'lobby', onReady: (urlA: string, urlB: string) => void): void {
  const base = track === 'lobby' ? 'fellowship' : 'shire';
  const folder = track === 'lobby' ? LOBBY_AUDIO_BASE : MENU_AUDIO_BASE;
  let i = 0;
  const tryNext = () => {
    if (i >= MENU_AUDIO_EXTS.length) return;
    const ext = MENU_AUDIO_EXTS[i++];
    const urlA = `${folder}/${base}${ext}`;
    const urlB = `${folder}/${base}${ext}`;
    const probeA = new Audio();
    const onErrA = () => {
      probeA.removeEventListener('canplay', onOkA);
      tryNext();
    };
    const onOkA = () => {
      probeA.removeEventListener('error', onErrA);
      const probeB = new Audio();
      const onErrB = () => {
        probeB.removeEventListener('canplay', onOkB);
        tryNext();
      };
      const onOkB = () => {
        probeB.removeEventListener('error', onErrB);
        onReady(urlA, urlB);
      };
      probeB.addEventListener('error', onErrB, { once: true });
      probeB.addEventListener('canplay', onOkB, { once: true });
      probeB.preload = 'auto';
      probeB.src = urlB;
      probeB.load();
    };
    probeA.addEventListener('error', onErrA, { once: true });
    probeA.addEventListener('canplay', onOkA, { once: true });
    probeA.preload = 'auto';
    probeA.src = urlA;
    probeA.load();
  };
  tryNext();
}

export function stopTurnCue(): void {
  turnSession.v++;
  lastDebouncedTurnCueKey = '';
  lastTurnCueFactionId = '';
  const s = turnSession.v;
  const prev = turnLoop;
  turnLoop = null;
  if (!prev) return;
  const active = prev.getActiveElement();
  const m = readVolume();
  const av = active.volume;
  const g = m > 0.001 ? av / m : 0;
  fadeGain(active, g, 0, Math.min(380, TURN_SWITCH_CROSSFADE_MS), turnSession, s, () => {
    prev.destroy();
  });
}

export function stopTurnCueImmediate(): void {
  turnSession.v++;
  lastDebouncedTurnCueKey = '';
  lastTurnCueFactionId = '';
  const prev = turnLoop;
  turnLoop = null;
  if (prev) prev.destroy();
}

export function stopMenuAmbienceImmediate(): void {
  teardownMenuPlayback();
}

/**
 * Start crossfaded turn music for the current faction.
 * @param factionId Used for debouncing and fallback track when `setupMusicFile` is missing/invalid.
 * @param setupMusicFile Optional factions.json `music`: one filename or ordered list; stems must match assets/audio/turn. Multiple tracks cycle (with crossfade) until the turn changes.
 */
export function playFactionTurnCue(factionId: string, setupMusicFile?: string | string[] | null): void {
  const safe = sanitizeFactionId(factionId);
  if (!safe) return;

  const stems = normalizeTurnTrackStems(setupMusicFile, safe);
  if (stems.length === 0) return;

  const now = Date.now();
  const debounceKey = `${safe}:${stems.join('|')}`;
  const sameFactionReplay =
    safe === lastTurnCueFactionId &&
    debounceKey === lastDebouncedTurnCueKey &&
    now - lastTurnCueAt < TURN_CUE_DEBOUNCE_MS;
  if (sameFactionReplay) return;

  teardownMenuPlayback();

  const master = readVolume();
  if (master <= 0) return;

  lastTurnCueAt = now;
  lastDebouncedTurnCueKey = debounceKey;
  lastTurnCueFactionId = safe;

  turnSession.v++;
  const sessionId = turnSession.v;

  // Stop any existing loop synchronously before async URL resolution. Otherwise a skipped turn can
  // start a second playFactionTurnCue while turnLoop is still null (first URL loading), and both
  // callbacks create TurnPlaylistPair — two tracks at once.
  if (turnLoop) {
    turnLoop.destroy();
    turnLoop = null;
  }

  const tryStart = (stemIdx: number) => {
    if (stemIdx >= stems.length) return;
    tryPlayFirstWorkingUrl(
      TURN_AUDIO_EXTS.map((ext) => `${TURN_AUDIO_BASE}/${stems[stemIdx]}${ext}`),
      (url) => {
        if (sessionId !== turnSession.v) return;

        const pair = new TurnPlaylistPair(stems, stemIdx, url, sessionId);
        turnLoop = pair;

        void pair.startFirstPlay().then(() => {
          if (sessionId !== turnSession.v || turnLoop !== pair) return;
          fadeGain(pair.a, 0, 1, TURN_FADE_IN_MS, turnSession, sessionId);
        });
      },
      () => tryStart(stemIdx + 1),
    );
  };
  tryStart(0);
}

export function stopMenuAmbience(): void {
  teardownMenuPlayback();
}

export function startMenuAmbience(mode: 'menu' | 'lobby' = 'menu'): void {
  // Do not skip restart when autoplay was blocked: play() may have rejected while menuLoop
  // still exists, which would leave ambience silent until route change without this check.
  if (menuLoop && currentMenuAmbienceMode === mode) {
    try {
      if (!menuLoop.getActiveElement().paused) return;
    } catch {
      /* keep going */
    }
  }
  stopTurnCueImmediate();
  stopMenuAmbience();
  const vol = readMenuMusicVolume();
  if (vol <= 0) return;

  menuSession.v++;
  const sessionId = menuSession.v;

  tryPlayFirstWorkingMenuTrack(mode, (urlA, urlB) => {
    if (sessionId !== menuSession.v) return;
    const pair = new MenuLoopPair(urlA, urlB, sessionId);
    menuLoop = pair;
    currentMenuAmbienceMode = mode;
    void pair
      .startFirstPlay()
      .then(() => {
        if (sessionId !== menuSession.v || menuLoop !== pair) return;
        fadeGainMenu(pair.a, 0, 1, MENU_FADE_IN_MS, menuSession, sessionId);
      })
      .catch(() => {
        /* Autoplay policy: play() may reject until user gesture; resumeMenuAmbienceIfPaused retries. */
      });
  });
}

/**
 * Call from a user gesture (e.g. pointerdown) when menu/lobby ambience should be audible.
 * Browsers often block programmatic play() until the user interacts with the page.
 */
export function resumeMenuAmbienceIfPaused(): void {
  if (!menuLoop) return;
  if (isGameAudioMuted()) return;
  const vol = readMenuMusicVolume();
  if (vol <= 0.001) return;
  const active = menuLoop.getActiveElement();
  if (!active.paused) return;
  const sid = menuLoop.sessionId;
  void active.play().then(() => {
    if (sid !== menuSession.v || menuLoop?.getActiveElement() !== active) return;
    fadeGainMenu(active, 0, 1, MENU_FADE_IN_MS, menuSession, sid);
  }).catch(() => {});
}

/**
 * In-game faction turn music: iOS / PWA often reject the first play() until a gesture, and may pause on visibility loss.
 * Call from global pointerdown (with menu resume) and optionally on visibilitychange.
 */
export function resumeTurnMusicIfPaused(): void {
  if (!turnLoop) return;
  if (isGameAudioMuted()) return;
  const m = readVolume();
  if (m <= 0.001) return;
  const active = turnLoop.getActiveElement();
  if (!active.paused) return;
  const sid = turnLoop.sessionId;
  void active.play().then(() => {
    if (sid !== turnSession.v || turnLoop?.getActiveElement() !== active) return;
    const fromG = m > 0.001 && active.volume > 0.001 ? Math.min(1, active.volume / m) : 0;
    fadeGain(active, fromG, 1, Math.min(380, TURN_FADE_IN_MS), turnSession, sid);
  }).catch(() => {});
}

function attachTurnMusicVisibilityResume(): void {
  if (typeof document === 'undefined') return;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    resumeTurnMusicIfPaused();
  });
}

attachTurnMusicVisibilityResume();
