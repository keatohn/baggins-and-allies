import type { LandscapePlacement } from '../../types/game';

export type LandscapeManifest = Record<string, string[]>;

export const LANDSCAPE_ASSET_BASE = '/assets/landscape';

export function landscapeSrc(placement: Pick<LandscapePlacement, 'img'>): string {
  return `${LANDSCAPE_ASSET_BASE}/${placement.img}`;
}

export async function loadLandscapeManifest(): Promise<LandscapeManifest> {
  try {
    const res = await fetch(`${LANDSCAPE_ASSET_BASE}/manifest.json`, { cache: 'no-store' });
    if (!res.ok) return {};
    const data: unknown = await res.json();
    if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
    const out: LandscapeManifest = {};
    for (const [cat, files] of Object.entries(data as Record<string, unknown>)) {
      if (!Array.isArray(files)) continue;
      out[cat] = files.filter((f): f is string => typeof f === 'string' && f.toLowerCase().endsWith('.png'));
    }
    return out;
  } catch {
    return {};
  }
}

/** Categories of landscape art that may be placed on a territory with this terrain type. */
export function landscapeCategoriesForTerrain(
  terrainType: string | undefined,
  manifest: LandscapeManifest,
): string[] {
  const t = (terrainType ?? '').toLowerCase();
  if (manifest[t]) return [t];
  return [];
}

/** Interior placements of a category suppress that terrain type's emoji fallback on the map. */
export function terrainEmojiSuppressed(
  placements: LandscapePlacement[] | undefined,
  terrainType: string | undefined,
): { mountain: boolean; forest: boolean } {
  const t = (terrainType ?? '').toLowerCase();
  let mountain = false;
  let forest = false;
  for (const p of placements ?? []) {
    if (p.layer !== 'interior') continue;
    if (t === 'mountains' && p.cat === 'mountains') mountain = true;
    if (t === 'forest' && p.cat === 'forest') forest = true;
  }
  return { mountain, forest };
}

export function territoryPathEl(tid: string): SVGPathElement | null {
  try {
    for (const node of document.querySelectorAll('path.admin-graph__path')) {
      const el = node as SVGPathElement;
      if (el.dataset.tid && sameId(el.dataset.tid, tid)) return el;
    }
  } catch {
    /* ignore */
  }
  return null;
}

function sameId(a: string, b: string): boolean {
  return a === b || a.toLowerCase() === b.toLowerCase();
}

export function isPointInTerritoryPath(path: SVGPathElement, x: number, y: number): boolean {
  try {
    const svg = path.ownerSVGElement;
    if (!svg) return false;
    const pt = svg.createSVGPoint();
    pt.x = x;
    pt.y = y;
    return path.isPointInFill(pt);
  } catch {
    return false;
  }
}

/** Nearest point on the territory outline to (x, y), sampled along the path. */
export function nearestPointOnOutline(path: SVGPathElement, x: number, y: number): { x: number; y: number } | null {
  try {
    const total = path.getTotalLength();
    if (!Number.isFinite(total) || total <= 0) return null;
    const samples = Math.min(140, Math.max(24, Math.floor(total / 12)));
    let best: { x: number; y: number; d: number } | null = null;
    for (let i = 0; i < samples; i++) {
      const p = path.getPointAtLength((i * total) / samples);
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (!best || d < best.d) best = { x: p.x, y: p.y, d };
    }
    return best ? { x: Math.round(best.x * 100) / 100, y: Math.round(best.y * 100) / 100 } : null;
  } catch {
    return null;
  }
}

/** Interior auto-placement for one territory: K points with min spacing, random image from the category. */
export function autoGenerateForTerritory(
  path: SVGPathElement,
  category: string,
  images: string[],
  seedInput?: number,
): LandscapePlacement[] {
  if (!images.length) return [];
  let seed = (seedInput ?? Date.now()) % 2147483647;
  if (seed <= 0) seed += 2147483646;
  const rand = () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return ((seed >>> 0) % 100000) / 100000;
  };
  try {
    const bbox = path.getBBox();
    if (!Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height) || bbox.width <= 0 || bbox.height <= 0) {
      return [];
    }
    const area = bbox.width * bbox.height;
    const count = Math.max(1, Math.min(6, Math.floor(area / 90000) + 1));
    const minDist = Math.max(36, Math.min(bbox.width, bbox.height) * 0.18);
    const minDist2 = minDist * minDist;
    const placed: { x: number; y: number }[] = [];
    const out: LandscapePlacement[] = [];
    for (let attempt = 0; attempt < 240 && out.length < count; attempt++) {
      const x = bbox.x + rand() * bbox.width;
      const y = bbox.y + rand() * bbox.height;
      if (!isPointInTerritoryPath(path, x, y)) continue;
      if (placed.some((q) => (q.x - x) ** 2 + (q.y - y) ** 2 < minDist2)) continue;
      placed.push({ x, y });
      out.push({
        cat: category,
        img: images[Math.floor(rand() * images.length)] ?? images[0]!,
        x: Math.round(x * 100) / 100,
        y: Math.round(y * 100) / 100,
        layer: 'interior',
        auto: true,
      });
    }
    return out;
  } catch {
    return [];
  }
}
