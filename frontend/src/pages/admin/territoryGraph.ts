export type TerritoryPathData = { d: string; transform?: string };

export const DEFAULT_MAP_BASE = 'wotr_map_1.2';

const MAP_VIEWBOX: Record<string, { width: number; height: number }> = {
  'wotr_map_1.0': { width: 3500, height: 2600 },
  'wotr_map_1.1': { width: 3500, height: 2600 },
  'wotr_map_1.2': { width: 3500, height: 2600 },
  'motw_map_1.2': { width: 3500, height: 1650 },
  'wotla_map_1.0': { width: 3500, height: 2600 },
};

const OSGILIATH_OFFSET = (4 * 90) / (3 * Math.PI);
const OSGILIATH_CENTROIDS_WOTR: Record<string, { x: number; y: number }> = {
  east_osgiliath: { x: 2218.6035 + OSGILIATH_OFFSET, y: 1761.675 },
  west_osgiliath: { x: 2217.97 - OSGILIATH_OFFSET, y: 1761.089 },
};
const OSGILIATH_CENTROIDS_MOTW_1_2: Record<string, { x: number; y: number }> = {
  east_osgiliath: { x: 2210.6035 + OSGILIATH_OFFSET, y: 809.675 },
  west_osgiliath: { x: 2209.97 - OSGILIATH_OFFSET, y: 809.089 },
};

export function toMapBase(name: string | null | undefined): string {
  if (!name || !name.trim()) return DEFAULT_MAP_BASE;
  const s = name.trim().replace(/\.(svg|png)$/i, '');
  return s || DEFAULT_MAP_BASE;
}

export function viewBoxForMap(mapBase: string): { width: number; height: number } {
  return MAP_VIEWBOX[mapBase] ?? MAP_VIEWBOX[DEFAULT_MAP_BASE];
}

function osgiliathCentroid(mapBase: string, territoryId: string): { x: number; y: number } | undefined {
  const table = mapBase.startsWith('wotr_map_')
    ? OSGILIATH_CENTROIDS_WOTR
    : mapBase === 'motw_map_1.2'
      ? OSGILIATH_CENTROIDS_MOTW_1_2
      : undefined;
  return table?.[territoryId];
}

export function canonicalSeaZoneId(tid: string): string {
  if (!tid || typeof tid !== 'string') return tid || '';
  const m = tid.trim().match(/^sea_zone_*(\d+)$/i);
  return m ? `sea_zone_${m[1]}` : tid.trim();
}

const INKSCAPE_NS = 'http://www.inkscape.org/namespaces/inkscape';

function getTerritoryId(el: Element): string | null {
  const id = el.getAttribute('id')?.trim();
  const label = el.getAttributeNS(INKSCAPE_NS, 'label');
  const useId = id && !/^path\d+$/i.test(id);
  const rawId = useId ? id : (label || null);
  if (!rawId) return null;
  if (useId) return rawId.toLowerCase();
  return rawId
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/'/g, '')
    .replace(/-/g, '_');
}

export async function loadTerritorySvgPaths(
  mapBase: string,
): Promise<{ paths: Map<string, TerritoryPathData>; viewBox: { width: number; height: number } }> {
  const fallback = viewBoxForMap(mapBase);
  const tryUrl = async (url: string) => {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(String(res.status));
    return res.text();
  };
  let svgText: string;
  try {
    svgText = await tryUrl(`/${mapBase}.svg`);
  } catch {
    if (mapBase === DEFAULT_MAP_BASE) throw new Error('Map SVG not found');
    svgText = await tryUrl(`/${DEFAULT_MAP_BASE}.svg`);
  }
  const parser = new DOMParser();
  const svgDoc = parser.parseFromString(svgText, 'image/svg+xml');
  const root = svgDoc.querySelector('svg');
  let viewBox = fallback;
  if (root) {
    const vb = root.getAttribute('viewBox');
    if (vb) {
      const parts = vb.trim().split(/\s+/);
      if (parts.length >= 4) {
        const w = parseFloat(parts[2]);
        const h = parseFloat(parts[3]);
        if (Number.isFinite(w) && Number.isFinite(h)) viewBox = { width: w, height: h };
      }
    }
  }
  let pathParent: Element | null = null;
  const allGroups = svgDoc.querySelectorAll('g');
  for (let i = 0; i < allGroups.length; i++) {
    const label = allGroups[i].getAttributeNS(INKSCAPE_NS, 'label');
    if (label === 'svg_borders') {
      pathParent = allGroups[i];
      break;
    }
  }
  const pathRoot = pathParent ?? svgDoc;
  const pathMap = new Map<string, TerritoryPathData>();
  pathRoot.querySelectorAll('path').forEach((path) => {
    const d = path.getAttribute('d');
    if (!d) return;
    const territoryId = getTerritoryId(path);
    if (!territoryId) return;
    const transform = path.getAttribute('transform')?.trim() || undefined;
    pathMap.set(territoryId, { d, transform });
  });
  pathRoot.querySelectorAll('circle').forEach((circle) => {
    const idRaw = circle.getAttribute('id')?.trim() ?? '';
    if (idRaw.toLowerCase().startsWith('hint_')) return;
    const cx = parseFloat(circle.getAttribute('cx') ?? '0');
    const cy = parseFloat(circle.getAttribute('cy') ?? '0');
    const r = parseFloat(circle.getAttribute('r') ?? '0');
    if (!Number.isFinite(cx + cy + r)) return;
    const territoryId = getTerritoryId(circle);
    if (!territoryId) return;
    const d = `M ${cx + r} ${cy} a ${r} ${r} 0 1 1 ${-2 * r} 0 a ${r} ${r} 0 1 1 ${2 * r} 0`;
    pathMap.set(territoryId, { d });
  });
  return { paths: pathMap, viewBox };
}

function poleOfInaccessibility(path: SVGPathElement): { x: number; y: number } | null {
  const bbox = path.getBBox();
  if (!Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height) || bbox.width <= 0 || bbox.height <= 0) {
    return null;
  }
  const cx = bbox.x + bbox.width / 2;
  const cy = bbox.y + bbox.height / 2;
  const svg = path.ownerSVGElement;
  if (!svg) return { x: cx, y: cy };
  const pt = svg.createSVGPoint();
  const isInside = (x: number, y: number) => {
    pt.x = x;
    pt.y = y;
    try {
      return path.isPointInFill(pt);
    } catch {
      return false;
    }
  };
  const boundaryPts: { x: number; y: number }[] = [];
  try {
    const totalLen = path.getTotalLength();
    const numSamples = Math.min(50, Math.max(20, Math.floor(totalLen / 15)));
    for (let k = 0; k < numSamples; k++) {
      const p = path.getPointAtLength((k * totalLen) / numSamples);
      boundaryPts.push({ x: p.x, y: p.y });
    }
  } catch {
    boundaryPts.push({ x: bbox.x, y: bbox.y }, { x: bbox.x + bbox.width, y: bbox.y + bbox.height });
  }
  const distToBoundary = (x: number, y: number) => {
    let min = Infinity;
    for (const b of boundaryPts) {
      const d = (x - b.x) ** 2 + (y - b.y) ** 2;
      if (d < min) min = d;
    }
    return Math.sqrt(min);
  };
  const gridSteps = 12;
  let bestSpot = { x: cx, y: cy, d: -1 };
  for (let i = 0; i <= gridSteps; i++) {
    for (let j = 0; j <= gridSteps; j++) {
      const x = bbox.x + (bbox.width * i) / gridSteps;
      const y = bbox.y + (bbox.height * j) / gridSteps;
      if (!isInside(x, y)) continue;
      const d = distToBoundary(x, y);
      if (d > bestSpot.d) bestSpot = { x, y, d };
    }
  }
  if (bestSpot.d >= 0) return { x: bestSpot.x, y: bestSpot.y };
  if (isInside(cx, cy)) return { x: cx, y: cy };
  for (let i = 1; i < 8; i++) {
    for (let j = 1; j < 8; j++) {
      const x = bbox.x + (bbox.width * i) / 8;
      const y = bbox.y + (bbox.height * j) / 8;
      if (isInside(x, y)) return { x, y };
    }
  }
  return { x: cx, y: cy };
}

export function computePathCentroids(
  paths: Map<string, TerritoryPathData>,
  viewBox: { width: number; height: number },
  mapBase: string,
): Record<string, { x: number; y: number }> {
  const centroids: Record<string, { x: number; y: number }> = {};
  paths.forEach((pathData, territoryId) => {
    const known = osgiliathCentroid(mapBase, territoryId);
    if (known) {
      centroids[territoryId] = known;
      return;
    }
    let tmp: SVGSVGElement | null = null;
    try {
      tmp = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      tmp.setAttribute('viewBox', `0 0 ${viewBox.width} ${viewBox.height}`);
      tmp.setAttribute('width', '1');
      tmp.setAttribute('height', '1');
      tmp.style.position = 'absolute';
      tmp.style.left = '-9999px';
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', pathData.d);
      if (pathData.transform) path.setAttribute('transform', pathData.transform);
      tmp.appendChild(path);
      document.body.appendChild(tmp);
      const c = poleOfInaccessibility(path);
      if (c) centroids[territoryId] = c;
    } catch {
      /* ignore */
    } finally {
      try {
        if (tmp?.parentNode) document.body.removeChild(tmp);
      } catch {
        /* ignore */
      }
    }
  });
  return centroids;
}

export function sameTerritoryId(a: string, b: string): boolean {
  if (a === b) return true;
  if (a.toLowerCase() === b.toLowerCase()) return true;
  return canonicalSeaZoneId(a).toLowerCase() === canonicalSeaZoneId(b).toLowerCase();
}

export function resolveTerritoryKey(
  territories: Record<string, unknown>,
  id: string,
): string | null {
  if (id in territories) return id;
  const lower = id.toLowerCase();
  if (lower in territories) return lower;
  const sea = canonicalSeaZoneId(id);
  if (sea in territories) return sea;
  const seaLower = sea.toLowerCase();
  if (seaLower in territories) return seaLower;
  for (const k of Object.keys(territories)) {
    if (k.toLowerCase() === lower) return k;
    if (canonicalSeaZoneId(k).toLowerCase() === seaLower) return k;
  }
  return null;
}

export function lookupCentroid(
  centroids: Record<string, { x: number; y: number }>,
  id: string,
): { x: number; y: number } | undefined {
  if (centroids[id]) return centroids[id];
  const lower = id.toLowerCase();
  if (centroids[lower]) return centroids[lower];
  const sea = canonicalSeaZoneId(id);
  if (centroids[sea]) return centroids[sea];
  const seaLower = sea.toLowerCase();
  if (centroids[seaLower]) return centroids[seaLower];
  return undefined;
}

export function adjacencyEdges(
  territories: Record<string, Record<string, unknown>>,
  field: 'adjacent' | 'aerial_adjacent' = 'adjacent',
): { a: string; b: string }[] {
  const seen = new Set<string>();
  const out: { a: string; b: string }[] = [];
  for (const [from, t] of Object.entries(territories)) {
    const adj = Array.isArray(t[field]) ? t[field] : [];
    for (const raw of adj) {
      if (typeof raw !== 'string' || !raw.trim()) continue;
      const to = raw.trim();
      const a = from < to ? from : to;
      const b = from < to ? to : from;
      const key = `${a}\0${b}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ a, b });
    }
  }
  return out;
}
