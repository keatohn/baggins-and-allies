/**
 * Land parchment grain. One shared tile, painted over territory fills.
 * Strength is the admin slider (`opacity`, 0–1). `enabled` false turns it off.
 */
import { api } from './services/api';

export const MAP_TEXTURE_DEFAULT_OPACITY = 0.55;

export const MAP_TEXTURE_CONFIG = {
  enabled: true,
  tileSize: 512,
  applyToLandOnly: true,
  src: '/assets/parchment-texture.png',
};

let opacity = MAP_TEXTURE_DEFAULT_OPACITY;
const listeners = new Set<() => void>();

export function clampMapTextureOpacity(raw: number): number {
  if (!Number.isFinite(raw)) return MAP_TEXTURE_DEFAULT_OPACITY;
  return Math.round(Math.min(1, Math.max(0, raw)) * 100) / 100;
}

export function getMapTextureOpacity(): number {
  return opacity;
}

export function setMapTextureOpacity(raw: number): void {
  const next = clampMapTextureOpacity(raw);
  if (next === opacity) return;
  opacity = next;
  listeners.forEach((listener) => listener());
}

export function subscribeMapTextureOpacity(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Load the admin-saved strength. Failures keep the current value. */
export function refreshMapTextureOpacity(): Promise<void> {
  return api
    .getMapTexture()
    .then((r) => setMapTextureOpacity(r.opacity))
    .catch(() => undefined);
}
