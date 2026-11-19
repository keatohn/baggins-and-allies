import type { LandscapePlacement } from '../../types/game';

export type LandscapeManifest = Record<string, string[]>;

/** Manifest key for art that may be placed on any territory, independent of terrain. */
export const LANDSCAPE_FREE_CATEGORY = 'anywhere';

/** Named places (Minas Tirith, Barad-dûr). Hand-placed on any territory; never auto-scattered. */
export const LANDSCAPE_LANDMARK_CATEGORY = 'landmarks';

export const LANDSCAPE_ASSET_BASE = '/assets/landscape';

export function landscapeSrc(placement: Pick<LandscapePlacement, 'img'>): string {
  return `${LANDSCAPE_ASSET_BASE}/${placement.img}`;
}

/** Clockwise degrees to store. A full turn and 0 are omitted. */
export function landscapeRotation(deg: number): number | undefined {
  if (!Number.isFinite(deg)) return undefined;
  const wrapped = ((deg % 360) + 360) % 360;
  const rounded = Math.round(wrapped * 10) / 10;
  return rounded === 0 ? undefined : rounded;
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

/**
 * Categories of landscape art that may be placed on a territory with this terrain type.
 * The free category (`anywhere`) and `landmarks` are included unless `includeFree` is false.
 * Auto-generate scatters terrain art only; landmarks are placed separately on a matching territory.
 */

/** Landmark filename stem → territory ids used when that stem is not itself a territory id. */
const LANDMARK_TERRITORY_ALIASES: Record<string, readonly string[]> = {
  bree: ['bree_fornost'],
  dwarrowdelf: ['khazad_dum', 'moria'],
  elven_halls: ['thranduils_halls'],
  osgiliath: ['west_osgiliath'],
  osgiliath_ruins: ['east_osgiliath'],
};

/** The landmark image that belongs on this territory, if the manifest has one. */
export function landmarkFileForTerritory(territoryId: string, images: readonly string[]): string | null {
  const id = territoryId.toLowerCase();
  const stem = (img: string) => img.replace(/\.png$/i, '').toLowerCase();
  for (const img of images) {
    if (stem(img) === id) return img;
  }
  for (const img of images) {
    if (LANDMARK_TERRITORY_ALIASES[stem(img)]?.some((alias) => alias === id)) return img;
  }
  return null;
}

/** A viewBox point inside the territory, preferring the bbox center. */
export function pointInsideTerritory(path: SVGPathElement): { x: number; y: number } | null {
  try {
    const bbox = path.getBBox();
    if (!Number.isFinite(bbox.width) || bbox.width <= 0 || bbox.height <= 0) return null;
    const round = (n: number) => Math.round(n * 100) / 100;
    const cx = bbox.x + bbox.width / 2;
    const cy = bbox.y + bbox.height / 2;
    if (isPointInTerritoryPath(path, cx, cy)) return { x: round(cx), y: round(cy) };
    for (let i = 1; i < 8; i++) {
      for (let j = 1; j < 8; j++) {
        const x = bbox.x + (bbox.width * i) / 8;
        const y = bbox.y + (bbox.height * j) / 8;
        if (isPointInTerritoryPath(path, x, y)) return { x: round(x), y: round(y) };
      }
    }
    return { x: round(cx), y: round(cy) };
  } catch {
    return null;
  }
}

export function landscapeCategoriesForTerrain(
  terrainType: string | undefined,
  manifest: LandscapeManifest,
  opts?: { includeFree?: boolean; unrestricted?: boolean },
): string[] {
  const t = (terrainType ?? '').toLowerCase();
  if (opts?.unrestricted) {
    return Object.keys(manifest).filter(cat => manifest[cat]?.length && (opts.includeFree !== false
      || (cat !== LANDSCAPE_FREE_CATEGORY && cat !== LANDSCAPE_LANDMARK_CATEGORY))).sort();
  }
  const cats: string[] = [];
  if (manifest[t]?.length) cats.push(t);
  const includeFree = opts?.includeFree !== false;
  if (includeFree) {
    for (const hand of [LANDSCAPE_FREE_CATEGORY, LANDSCAPE_LANDMARK_CATEGORY]) {
      if (manifest[hand]?.length && !cats.includes(hand)) cats.push(hand);
    }
  }
  return cats;
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

/**
 * Per-category auto-generate profile. Packing density is fixed (paint-brush
 * spacing up to the global per-territory cap); scale/scaleJitter still drive
 * display size. perArea/maxCount/spacing are retained for reference but no
 * longer affect placement. Adding a new manifest category is just another table row.
 */
export type LandscapeDensityProfile = {
  /** Pieces per 100k viewBox² of estimated fill area (legacy; no longer drives count). */
  perArea: number;
  /** Hard cap for this category (legacy; global caps are 50/territory, 2500/map). */
  maxCount: number;
  /** Base display scale before jitter. */
  scale: number;
  /** ± fraction of base scale for variety (0.2 → s in [0.8×scale, 1.2×scale]). */
  scaleJitter: number;
  /** Min spacing as a fraction of √(area/count) (legacy; spacing is now fixed). */
  spacing: number;
};

export const LANDSCAPE_DENSITY_PROFILES: Record<string, LandscapeDensityProfile> = {
  forest: { perArea: 14, maxCount: 18, scale: 0.9, scaleJitter: 0.25, spacing: 0.9 },
  mountains: { perArea: 9, maxCount: 12, scale: 1.15, scaleJitter: 0.2, spacing: 1.15 },
  hills: { perArea: 11, maxCount: 14, scale: 1.0, scaleJitter: 0.15, spacing: 1.0 },
  volcano: { perArea: 1, maxCount: 1, scale: 1.3, scaleJitter: 0, spacing: 1 },
};

const DEFAULT_DENSITY_PROFILE: LandscapeDensityProfile = {
  perArea: 10,
  maxCount: 12,
  scale: 1,
  scaleJitter: 0.2,
  spacing: 1,
};

/** Minimum viewBox-space distance between auto-generated pieces (matches the paint brush). */
export const LANDSCAPE_AUTO_MIN_DIST = 36;

/** Global per-territory cap on landscape placements (matches MAX_LANDSCAPE_PER_TERRITORY). */
export const LANDSCAPE_AUTO_MAX_PER_TERRITORY = 50;

/** Something landscape art must yield to: a disc of radius `r` around (x, y). */
export type LandscapeObstacle = { x: number; y: number; r: number };

/** Half-extent of a scale-1 landscape icon in viewBox units (40-unit art box). */
const LANDSCAPE_ICON_HALF = 20;

/**
 * Interior auto-placement for one territory: greedy fill at paint-brush spacing,
 * a single image shared by every piece, size jitter from the density profile, and
 * obstacle clearance (a candidate is dropped when its center is within
 * `obstacle.r + 20·s` of an obstacle). Stops at the global per-territory cap.
 */
export function autoGenerateForTerritory(
  path: SVGPathElement,
  category: string,
  images: string[],
  seedInput?: number,
  obstacles?: readonly LandscapeObstacle[],
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
    const profile = LANDSCAPE_DENSITY_PROFILES[category] ?? DEFAULT_DENSITY_PROFILE;
    // One icon for the whole territory: pick once, reuse for every piece in this run.
    const img = images[Math.floor(rand() * images.length)] ?? images[0]!;
    const minDist2 = LANDSCAPE_AUTO_MIN_DIST * LANDSCAPE_AUTO_MIN_DIST;
    const count = LANDSCAPE_AUTO_MAX_PER_TERRITORY;
    const attempts = count * 60;
    const placed: { x: number; y: number }[] = [];
    const out: LandscapePlacement[] = [];
    for (let attempt = 0; attempt < attempts && out.length < count; attempt++) {
      const x = bbox.x + rand() * bbox.width;
      const y = bbox.y + rand() * bbox.height;
      if (!isPointInTerritoryPath(path, x, y)) continue;
      if (placed.some((q) => (q.x - x) ** 2 + (q.y - y) ** 2 < minDist2)) continue;
      const sRounded = Math.round(profile.scale * (1 + (rand() * 2 - 1) * profile.scaleJitter) * 100) / 100;
      const clearance = (o: LandscapeObstacle) => (o.x - x) ** 2 + (o.y - y) ** 2 < (o.r + LANDSCAPE_ICON_HALF * sRounded) ** 2;
      if (obstacles?.some(clearance)) continue;
      placed.push({ x, y });
      const placement: LandscapePlacement = {
        cat: category,
        img,
        x: Math.round(x * 100) / 100,
        y: Math.round(y * 100) / 100,
        layer: 'interior',
        auto: true,
      };
      if (sRounded !== 1) placement.s = sRounded;
      out.push(placement);
    }
    return out;
  } catch {
    return [];
  }
}

/** Manifest category that owns `img`, or null when no category lists it. */
export function landscapeImageCategory(manifest: LandscapeManifest, img: string): string | null {
  for (const [cat, files] of Object.entries(manifest)) {
    if (files.includes(img)) return cat;
  }
  return null;
}

/** Tint mountain art without affecting its transparent background. */
export function landscapeFilter(placement: Pick<LandscapePlacement, 'cat' | 'darkness'>): string | undefined {
  if (placement.cat !== 'mountains' || !placement.darkness) return undefined;
  const darkness = Math.min(1, Math.max(0, placement.darkness));
  return `grayscale(${darkness}) brightness(${1 - darkness * 0.95})`;
}
