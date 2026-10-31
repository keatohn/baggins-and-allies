import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import GameMap, { DEFAULT_MAP_BASE } from '../../components/GameMap';
import { TERRITORY_UNIT_PIXEL_NUDGE } from '../../components/mapSpotNudge';
import { api } from '../../services/api';
import type {
  GameState,
  LandscapeOverrides,
  LandscapePlacement,
  MapOverridePoint,
  MapOverridesData,
  MapSpotKind,
} from '../../types/game';
import { normalizeMapOverrides } from '../../types/game';
import {
  autoGenerateForTerritory,
  isPointInTerritoryPath,
  landscapeCategoriesForTerrain,
  landscapeSrc,
  loadLandscapeManifest,
  nearestPointOnOutline,
  territoryPathEl,
  type LandscapeManifest,
} from './landscape';
import { loadTerritorySvgPaths, sameTerritoryId, type TerritoryPathData } from './territoryGraph';

type Pt = { x: number; y: number };
type Dict = Record<string, Record<string, unknown>>;

type HintKind = 'units' | 'number' | 'icons' | 'logo';

type CalculatedSpots = {
  centroids: Record<string, Pt>;
  positions: Record<string, { marker: Pt; unit: Pt; center?: Pt }>;
  hints: Record<string, Partial<Record<HintKind, Pt>>>;
};

/**
 * Editor shows exactly four color dots per territory (one per color):
 * blue units, red production/sea-number, yellow faction logo, green "everything else"
 * (territory type, homes, camps, ports, signals, rings). Red stores as `marker` on land
 * and `number` on sea zones (they never coexist). Green stores as `icons` and anchors
 * all non-logo/power/unit icons in GameMap.
 */
type UiKind = 'unit' | 'power' | 'logo' | 'icons';

const SPOT_KINDS: { kind: UiKind; label: string; color: string; hint?: string }[] = [
  { kind: 'unit', label: 'Units', color: '#2563eb' },
  { kind: 'power', label: 'Production / sea number', color: '#dc2626' },
  { kind: 'logo', label: 'Faction logo', color: '#eab308' },
  {
    kind: 'icons',
    label: 'Other icons',
    color: '#16a34a',
    hint: 'Territory type, homes, camps, ports, signals, rings',
  },
];
const KIND_COLOR: Record<UiKind, string> = Object.fromEntries(
  SPOT_KINDS.map((row) => [row.kind, row.color]),
) as Record<UiKind, string>;
const KIND_LABEL: Record<UiKind, string> = Object.fromEntries(
  SPOT_KINDS.map((row) => [row.kind, row.label]),
) as Record<UiKind, string>;
/** Kinds every territory shows; logo/icons only appear when calculated or manually placed. */
const ALWAYS_KINDS: UiKind[] = ['unit', 'power'];
const EXTRA_KINDS: UiKind[] = ['logo', 'icons'];

/** Sea zones (terrain sea or sea_zone_N id) use the `number` storage kind for the red dot. */
function isSeaTerritory(tid: string, territories: Dict): boolean {
  const def = territories[resolveTid(territories, tid) ?? tid];
  const terrain =
    def && typeof def.terrain_type === 'string'
      ? def.terrain_type
      : def && typeof def.terrain === 'string'
        ? def.terrain
        : '';
  return terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
}

/** Map a UI color kind to the MapSpotKind stored in the overrides blob. */
function storageKindFor(tid: string, uiKind: UiKind, territories: Dict): MapSpotKind {
  if (uiKind === 'power') return isSeaTerritory(tid, territories) ? 'number' : 'marker';
  return uiKind;
}

const NOOP = () => {};

/** Object keys sorted so re-adding/removing a territory does not count as a change by itself. */
function stableJson(value: unknown): string {
  return JSON.stringify(value, (_k, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
}

function prettifyId(id: string): string {
  return id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function territoryLabel(id: string, territories: Dict): string {
  const name = territories[id] && typeof territories[id].display_name === 'string' ? (territories[id].display_name as string).trim() : '';
  return name || prettifyId(id);
}

function resolveTid<T>(dict: Record<string, T> | undefined, tid: string): string | null {
  if (!dict) return null;
  if (tid in dict) return tid;
  const lower = tid.toLowerCase();
  if (lower in dict) return lower;
  for (const key of Object.keys(dict)) {
    if (key.toLowerCase() === lower) return key;
  }
  return null;
}

function territoryIdFromPoint(clientX: number, clientY: number): string | null {
  try {
    for (const node of document.elementsFromPoint(clientX, clientY)) {
      if (!(node instanceof Element)) continue;
      const path = node.closest('path.admin-graph__path');
      const tid = path instanceof SVGPathElement ? path.dataset.tid : undefined;
      if (tid) return tid;
    }
  } catch {
    /* ignore */
  }
  return null;
}

function entryFor(data: MapOverridesData | null, tid: string): MapOverridesData['territories'][string] | undefined {
  if (!data) return undefined;
  const key = resolveTid(data.territories, tid);
  return key ? data.territories[key] : undefined;
}

function fmt(pt: MapOverridePoint): string {
  return `${Math.round(pt.x)}, ${Math.round(pt.y)}`;
}

/**
 * Map-level icon/location override editor: pick a map, see every calculated spot as a color-coded
 * dot, flip a territory from "calculated" to "override", then drag its dots within the territory.
 * Saves the whole overrides blob for the map (shared by every setup using it).
 */
export function MapOverridesPane({ territories = {}, onClose }: { territories?: Dict; onClose: () => void }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [maps, setMaps] = useState<string[]>([]);
  const [mapBase, setMapBase] = useState('');
  const [pendingMap, setPendingMap] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paths, setPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [viewBox, setViewBox] = useState({ width: 3500, height: 2600 });
  const [pngFailed, setPngFailed] = useState(false);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [selected, setSelected] = useState<string | null>(null);
  const [activeKind, setActiveKind] = useState<UiKind>('unit');
  const [search, setSearch] = useState('');
  const [stored, setStored] = useState<MapOverridesData | null>(null);
  const [draft, setDraft] = useState<MapOverridesData | null>(null);
  const [spots, setSpots] = useState<CalculatedSpots | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [editorTab, setEditorTab] = useState<'icon' | 'landscape'>('icon');
  const [manifest, setManifest] = useState<LandscapeManifest>({});
  const [placing, setPlacing] = useState<{ cat: string; img: string } | null>(null);
  const [borderMode, setBorderMode] = useState(false);
  const [selectedPlacement, setSelectedPlacement] = useState<{ tid: string; index: number } | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);

  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number; id: number } | null>(null);
  const didDragRef = useRef(false);
  const dotDragRef = useRef<{
    tid: string;
    kind: UiKind;
    id: number;
    sx: number;
    sy: number;
    scale: number;
    startPt: Pt;
  } | null>(null);
  const placeDragRef = useRef<{
    tid: string;
    index: number;
    id: number;
    sx: number;
    sy: number;
    scale: number;
    startPt: Pt;
    layer: 'interior' | 'border';
  } | null>(null);

  const current = draft ?? stored;
  const dirty = draft != null && stored != null && stableJson(draft) !== stableJson(stored);

  // --- data loading -------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    api
      .adminListMaps()
      .then((r) => {
        if (cancelled) return;
        const list = Array.isArray(r?.maps) ? r.maps.filter((m) => typeof m === 'string' && m) : [];
        setMaps(list);
        setMapBase((prev) => prev || list[0] || '');
        if (!list.length) {
          setErr('No maps found.');
          setLoading(false);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : 'Failed to list maps');
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    loadLandscapeManifest().then((m) => {
      if (!cancelled) setManifest(m);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!mapBase) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErr(null);
    setPaths(new Map());
    setSpots(null);
    setStored(null);
    setDraft(null);
    setSelected(null);
    setPngFailed(false);
    setSearch('');
    setPlacing(null);
    setBorderMode(false);
    setSelectedPlacement(null);
    setEditorTab('icon');
    api
      .getMapOverrides(mapBase)
      .then((r) => {
        if (!cancelled) setStored(normalizeMapOverrides(r?.overrides));
      })
      .catch(() => {
        if (!cancelled) setStored({ territories: {} });
      });
    loadTerritorySvgPaths(mapBase)
      .then(({ paths: nextPaths, viewBox: vb }) => {
        if (cancelled) return;
        setPaths(nextPaths);
        setViewBox(vb);
        setLoading(false);
      })
      .catch((e) => {
        if (!cancelled) {
          setErr(e instanceof Error ? e.message : 'Failed to load map SVG');
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [mapBase]);

  // --- calculated spot oracle (hidden GameMap) ----------------------------

  const onSpotsCalculated = useCallback((next: CalculatedSpots) => {
    setSpots((prev) => (prev && stableJson(prev) === stableJson(next) ? prev : next));
  }, []);

  const oracleGameState = useMemo<GameState>(
    () => ({
      turn_number: 1,
      current_faction: '',
      phase: 'purchase',
      territories: {},
      faction_resources: {},
      pending_purchases: {},
      pending_moves: [],
      pending_mobilizations: [],
      pending_camp_placements: [],
      declared_battles: [],
      map_asset: mapBase || DEFAULT_MAP_BASE,
    }),
    [mapBase],
  );

  const oracleTerritoryData = useMemo(() => {
    const out: Record<string, { name: string; owner?: string; terrain: string; stronghold: boolean; produces: number; adjacent: string[] }> = {};
    for (const [tid, def] of Object.entries(territories)) {
      const terrain =
        typeof def.terrain_type === 'string' ? def.terrain_type : typeof def.terrain === 'string' ? def.terrain : 'land';
      out[tid] = {
        name: territoryLabel(tid, territories),
        terrain,
        stronghold: false,
        produces: 0,
        adjacent: [],
      };
    }
    return out;
  }, [territories]);

  // --- viewport fit / pan / zoom -----------------------------------------

  useEffect(() => {
    if (loading || err) return;
    const el = wrapRef.current;
    if (!el) return;
    let lastW = -1;
    let lastH = -1;
    const fit = () => {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      if (Math.abs(r.width - lastW) < 1 && Math.abs(r.height - lastH) < 1) return;
      lastW = r.width;
      lastH = r.height;
      const s = Math.min(r.width / viewBox.width, r.height / viewBox.height);
      setTransform({
        scale: s,
        x: (r.width - viewBox.width * s) / 2,
        y: (r.height - viewBox.height * s) / 2,
      });
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [loading, err, viewBox.width, viewBox.height]);

  const requestClose = () => {
    if (saving) return;
    if (dirty) {
      setPendingMap(null);
      setDiscardOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && placing) {
        e.stopPropagation();
        setPlacing(null);
        return;
      }
      if (e.key !== 'Escape') return;
      if (discardOpen) {
        setDiscardOpen(false);
        setPendingMap(null);
        return;
      }
      if (saving) return;
      if (dirty) {
        setPendingMap(null);
        setDiscardOpen(true);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [discardOpen, saving, dirty, onClose, placing]);

  const changeMap = (next: string) => {
    if (!next || next === mapBase) return;
    if (dirty) {
      setPendingMap(next);
      setDiscardOpen(true);
      return;
    }
    setMapBase(next);
  };

  const confirmDiscard = () => {
    setDraft(null);
    setDiscardOpen(false);
    if (pendingMap) {
      setMapBase(pendingMap);
      setPendingMap(null);
    } else {
      onClose();
    }
  };

  const onWheel = (e: ReactWheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = wrapRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.08 : 1 / 1.08;
    setTransform((prev) => {
      const nextScale = Math.min(8, Math.max(0.25, prev.scale * factor));
      const k = nextScale / prev.scale;
      return {
        scale: nextScale,
        x: mx - (mx - prev.x) * k,
        y: my - (my - prev.y) * k,
      };
    });
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    didDragRef.current = false;
    dragRef.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y, id: e.pointerId };
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d || e.pointerId !== d.id) return;
    const dist = Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y);
    if (!didDragRef.current) {
      if (dist < 10) return;
      didDragRef.current = true;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    }
    setTransform((prev) => ({
      ...prev,
      x: d.tx + (e.clientX - d.x),
      y: d.ty + (e.clientY - d.y),
    }));
  };

  const viewportToViewBox = useCallback(
    (clientX: number, clientY: number): Pt | null => {
      const el = wrapRef.current;
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return {
        x: (clientX - rect.left - transform.x) / transform.scale,
        y: (clientY - rect.top - transform.y) / transform.scale,
      };
    },
    [transform],
  );

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (d && e.pointerId === d.id) {
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    }
    dragRef.current = null;
    if (didDragRef.current) return;
    if (editorTab === 'landscape' && placing) {
      tryPlaceLandscape(e.clientX, e.clientY);
      return;
    }
    const tid = territoryIdFromPoint(e.clientX, e.clientY);
    if (tid) {
      setSelected(tid);
      setActiveKind('unit');
      setSelectedPlacement(null);
    }
  };

  // --- spots / overrides --------------------------------------------------

  const calculatedFor = useCallback(
    (tid: string): Partial<Record<UiKind, Pt>> => {
      if (!spots) return {};
      const pKey = resolveTid(spots.positions, tid);
      const hKey = resolveTid(spots.hints, tid);
      const p = pKey ? spots.positions[pKey] : undefined;
      const h = hKey ? spots.hints[hKey] : undefined;
      const out: Partial<Record<UiKind, Pt>> = {};
      if (p?.unit) out.unit = p.unit;
      // Red = production (land / marker) or sea-zone number (sea / number hint, else marker).
      out.power = isSeaTerritory(tid, territories) ? (h?.number ?? p?.marker) : p?.marker;
      if (h?.logo) out.logo = h.logo;
      if (h?.icons) out.icons = h.icons;
      // Reproduce the render-time unit pixel nudge (viewBox ≈ screen px, 1:1 per MAP_CONFIG).
      // Skipped when an SVG units hint anchors the stack — those bypass the nudge in GameMap.
      if (out.unit && !h?.units) {
        const nudge = TERRITORY_UNIT_PIXEL_NUDGE[tid] ?? TERRITORY_UNIT_PIXEL_NUDGE[resolveTid(TERRITORY_UNIT_PIXEL_NUDGE, tid) ?? ''];
        if (nudge) out.unit = { x: out.unit.x + (nudge.x ?? 0), y: out.unit.y + (nudge.y ?? 0) };
      }
      return out;
    },
    [spots, territories],
  );

  const overridePositionsFor = useCallback(
    (tid: string): Partial<Record<MapSpotKind, MapOverridePoint>> | undefined => {
      const entry = entryFor(current, tid);
      return entry?.focus === 'override' ? entry.positions : undefined;
    },
    [current],
  );

  const effectiveFor = useCallback(
    (tid: string, kind: UiKind): { pt: MapOverridePoint; isOverride: boolean } | null => {
      const ovp = overridePositionsFor(tid)?.[storageKindFor(tid, kind, territories)];
      if (ovp) return { pt: ovp, isOverride: true };
      const calc = calculatedFor(tid)[kind];
      return calc ? { pt: calc, isOverride: false } : null;
    },
    [calculatedFor, overridePositionsFor, territories],
  );

  const updateEntry = useCallback((tid: string, entry: MapOverridesData['territories'][string] | null) => {
    setDraft((prevDraft) => {
      const base = prevDraft ?? stored ?? { territories: {} };
      const key = resolveTid(base.territories, tid) ?? tid;
      const next = { ...base.territories };
      if (entry === null) delete next[key];
      else next[key] = entry;
      const data: MapOverridesData = { territories: next };
      if (base.landscape) data.landscape = base.landscape;
      return stableJson(data) === stableJson(base) ? prevDraft ?? base : data;
    });
    setSaveError(null);
  }, [stored]);

  const updateLandscape = useCallback(
    (fn: (prev: LandscapeOverrides | undefined) => LandscapeOverrides | undefined) => {
      setDraft((prevDraft) => {
        const base = prevDraft ?? stored ?? { territories: {} };
        const nextLandscape = fn(base.landscape);
        const data: MapOverridesData = { territories: base.territories };
        if (nextLandscape) data.landscape = nextLandscape;
        return stableJson(data) === stableJson(base) ? prevDraft ?? base : data;
      });
      setSaveError(null);
    },
    [stored],
  );

  const landscapeFor = useCallback(
    (tid: string): LandscapePlacement[] => {
      const map = current?.landscape?.territories;
      if (!map) return [];
      const key = resolveTid(map, tid);
      return key ? (map[key] ?? []) : [];
    },
    [current],
  );

  const setPlacementAt = useCallback(
    (tid: string, index: number, patch: Partial<LandscapePlacement> | null) => {
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const key = resolveTid(map, tid) ?? tid;
        const list = [...(map[key] ?? [])];
        if (patch === null) {
          list.splice(index, 1);
        } else if (list[index]) {
          list[index] = { ...list[index]!, ...patch };
        } else {
          return prev;
        }
        if (list.length) map[key] = list;
        else delete map[key];
        return Object.keys(map).length ? { territories: map } : undefined;
      });
      setSelectedPlacement(patch === null ? null : { tid, index });
    },
    [updateLandscape],
  );

  const clearAllLandscape = useCallback(() => {
    updateLandscape(() => undefined);
    setSelectedPlacement(null);
    setPlacing(null);
  }, [updateLandscape]);

  const autoGenerateLandscape = useCallback(() => {
    if (!paths.size) return;
    setAutoBusy(true);
    try {
      updateLandscape((prev) => {
        const map: Record<string, LandscapePlacement[]> = { ...(prev?.territories ?? {}) };
        for (const [tid, placements] of Object.entries(map)) {
          map[tid] = placements.filter((p) => !p.auto);
          if (!map[tid].length) delete map[tid];
        }
        const seed = (Date.now() % 2147483647) || 1;
        let seedAcc = seed;
        const nextSeed = () => {
          seedAcc = (seedAcc * 1103515245 + 12345) % 2147483647;
          return seedAcc || 1;
        };
        for (const tid of paths.keys()) {
          const defKey = resolveTid(territories, tid) ?? tid;
          const def = territories[defKey];
          const terrain =
            def && typeof def.terrain_type === 'string'
              ? def.terrain_type
              : def && typeof def.terrain === 'string'
                ? def.terrain
                : '';
          const cats = landscapeCategoriesForTerrain(terrain, manifest);
          if (!cats.length) continue;
          const pathEl = territoryPathEl(tid);
          if (!pathEl) continue;
          for (const cat of cats) {
            const images = manifest[cat] ?? [];
            if (!images.length) continue;
            const key = resolveTid(map, tid) ?? tid;
            const generated = autoGenerateForTerritory(pathEl, cat, images, nextSeed());
            if (generated.length) map[key] = [...(map[key] ?? []), ...generated];
          }
        }
        return Object.keys(map).length ? { territories: map } : undefined;
      });
    } finally {
      setAutoBusy(false);
    }
  }, [paths, territories, manifest, updateLandscape]);

  const selectedTerrainType = useMemo(() => {
    if (!selected) return '';
    const defKey = resolveTid(territories, selected) ?? selected;
    const def = territories[defKey];
    if (!def) return '';
    return typeof def.terrain_type === 'string'
      ? def.terrain_type
      : typeof def.terrain === 'string'
        ? def.terrain
        : '';
  }, [selected, territories]);

  const selectedCategories = useMemo(() => {
    const cats = landscapeCategoriesForTerrain(selectedTerrainType, manifest);
    // Border mountains are allowed on any land territory (decorative), independent of terrain type.
    if (borderMode && manifest.mountains && !cats.includes('mountains')) return [...cats, 'mountains'];
    return cats;
  }, [selectedTerrainType, manifest, borderMode]);

  const selectedPlacements = useMemo(
    () => (selected ? landscapeFor(selected) : []),
    [selected, landscapeFor],
  );

  const tryPlaceLandscape = useCallback(
    (clientX: number, clientY: number) => {
      if (!placing) return false;
      const tid = territoryIdFromPoint(clientX, clientY);
      if (!tid) return false;
      const vb = viewportToViewBox(clientX, clientY);
      if (!vb) return false;
      let x = vb.x;
      let y = vb.y;
      if (borderMode) {
        const pathEl = territoryPathEl(tid);
        if (!pathEl) return false;
        const snapped = nearestPointOnOutline(pathEl, x, y);
        if (!snapped) return false;
        x = snapped.x;
        y = snapped.y;
      } else {
        const pathEl = territoryPathEl(tid);
        if (pathEl && !isPointInTerritoryPath(pathEl, x, y)) return false;
      }
      const key = resolveTid(current?.landscape?.territories, tid) ?? tid;
      const placement: LandscapePlacement = {
        cat: placing.cat,
        img: placing.img,
        x: Math.round(x * 100) / 100,
        y: Math.round(y * 100) / 100,
        layer: borderMode ? 'border' : 'interior',
      };
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const list = [...(map[key] ?? []), placement];
        if (list.length > 50) return prev;
        map[key] = list;
        return { territories: map };
      });
      setSelected(tid);
      setSelectedPlacement({ tid: key, index: (current?.landscape?.territories?.[key]?.length ?? 0) });
      return true;
    },
    [placing, borderMode, viewportToViewBox, current, updateLandscape],
  );

  const setFocus = (tid: string, focus: 'calculated' | 'override') => {
    if (focus === 'calculated') {
      updateEntry(tid, null);
      return;
    }
    const existing = entryFor(current, tid);
    if (existing?.focus === 'override') return;
    const calc = calculatedFor(tid);
    const positions: Partial<Record<MapSpotKind, MapOverridePoint>> = {};
    for (const kind of [...ALWAYS_KINDS, ...EXTRA_KINDS]) {
      const pt = calc[kind];
      if (pt) positions[storageKindFor(tid, kind, territories)] = pt;
    }
    updateEntry(tid, { focus: 'override', positions });
  };

  const setKindPosition = (tid: string, kind: UiKind, pt: MapOverridePoint) => {
    const existing = entryFor(current, tid);
    const positions = {
      ...(existing?.focus === 'override' ? existing.positions : {}),
      [storageKindFor(tid, kind, territories)]: pt,
    };
    updateEntry(tid, { focus: 'override', positions });
  };

  const resetKind = (tid: string, kind: UiKind) => {
    const existing = entryFor(current, tid);
    if (existing?.focus !== 'override' || !existing.positions) return;
    const positions = { ...existing.positions };
    delete positions[storageKindFor(tid, kind, territories)];
    updateEntry(tid, { focus: 'override', positions });
  };

  const addKind = (tid: string, kind: UiKind) => {
    const calc = calculatedFor(tid);
    const seed = calc[kind] ?? calc.power ?? calc.unit;
    if (!seed) return;
    setKindPosition(tid, kind, seed);
    setActiveKind(kind);
  };

  // --- dot dragging (constrained to the territory) ------------------------

  const onDotPointerDown = (e: ReactPointerEvent<HTMLDivElement>, tid: string, kind: UiKind) => {
    e.stopPropagation();
    e.preventDefault();
    if (e.button !== 0) return;
    setSelected(tid);
    setActiveKind(kind);
    const entry = entryFor(current, tid);
    if (entry?.focus !== 'override') return; // calculated dots are not draggable until focus flips
    const spot = effectiveFor(tid, kind);
    if (!spot) return;
    try {
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dotDragRef.current = {
      tid,
      kind,
      id: e.pointerId,
      sx: e.clientX,
      sy: e.clientY,
      scale: transform.scale,
      startPt: { x: spot.pt.x, y: spot.pt.y },
    };
  };

  const onDotPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = dotDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    // Constrain: the cursor must be over this territory's path (same hit test as selection).
    const overTid = territoryIdFromPoint(e.clientX, e.clientY);
    if (!overTid || !sameTerritoryId(overTid, d.tid)) return;
    const dx = (e.clientX - d.sx) / d.scale;
    const dy = (e.clientY - d.sy) / d.scale;
    const nx = Math.max(0, Math.min(viewBox.width, d.startPt.x + dx));
    const ny = Math.max(0, Math.min(viewBox.height, d.startPt.y + dy));
    setKindPosition(d.tid, d.kind, { x: nx, y: ny });
  };

  const onDotPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = dotDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    try {
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dotDragRef.current = null;
  };

  // --- landscape placement dragging ---------------------------------------

  const onPlacementPointerDown = (e: ReactPointerEvent<HTMLDivElement>, tid: string, index: number) => {
    e.stopPropagation();
    e.preventDefault();
    if (e.button !== 0) return;
    if (placing) return; // placing mode takes priority; don't start a drag
    const placement = landscapeFor(tid)[index];
    if (!placement) return;
    setSelected(tid);
    setSelectedPlacement({ tid, index });
    try {
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    placeDragRef.current = {
      tid,
      index,
      id: e.pointerId,
      sx: e.clientX,
      sy: e.clientY,
      scale: transform.scale,
      startPt: { x: placement.x, y: placement.y },
      layer: placement.layer,
    };
  };

  const onPlacementPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = placeDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    const el = wrapRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const startVb = {
      x: (d.sx - rect.left - transform.x) / d.scale,
      y: (d.sy - rect.top - transform.y) / d.scale,
    };
    const vb = viewportToViewBox(e.clientX, e.clientY);
    if (!vb) return;
    let x = d.startPt.x + (vb.x - startVb.x);
    let y = d.startPt.y + (vb.y - startVb.y);
    const pathEl = territoryPathEl(d.tid);
    if (d.layer === 'border') {
      if (pathEl) {
        const snapped = nearestPointOnOutline(pathEl, x, y);
        if (snapped) {
          x = snapped.x;
          y = snapped.y;
        }
      }
    } else if (pathEl && !isPointInTerritoryPath(pathEl, x, y)) {
      return; // interior drags stay inside the polygon
    }
    setPlacementAt(d.tid, d.index, { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 });
  };

  const onPlacementPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = placeDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    try {
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    placeDragRef.current = null;
  };

  // --- save ---------------------------------------------------------------

  const save = async () => {
    if (!dirty || !draft || !mapBase || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res = await api.adminPutMapOverrides(mapBase, draft);
      setStored(normalizeMapOverrides(res?.overrides ?? draft));
      setDraft(null);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // --- derived lists ------------------------------------------------------

  const territoryChoices = useMemo(() => {
    const seen = new Set<string>();
    const rows: { id: string; label: string }[] = [];
    for (const id of paths.keys()) {
      if (seen.has(id)) continue;
      seen.add(id);
      rows.push({ id, label: territoryLabel(id, territories) });
    }
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [paths, territories]);

  const filteredChoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return territoryChoices;
    return territoryChoices.filter(
      (row) => row.label.toLowerCase().includes(q) || row.id.toLowerCase().includes(q),
    );
  }, [territoryChoices, search]);

  const dots = useMemo(() => {
    if (!spots) return [];
    const rows: { tid: string; kind: UiKind; pt: MapOverridePoint; isOverride: boolean }[] = [];
    for (const tid of paths.keys()) {
      for (const { kind } of SPOT_KINDS) {
        const spot = effectiveFor(tid, kind);
        if (!spot) continue;
        rows.push({ tid, kind, pt: spot.pt, isOverride: spot.isOverride });
      }
    }
    // Selected territory's dots (and the active kind) render last so they stay clickable when overlapping.
    rows.sort((a, b) => {
      const aSel = a.tid === selected ? 1 : 0;
      const bSel = b.tid === selected ? 1 : 0;
      if (aSel !== bSel) return aSel - bSel;
      const aAct = a.tid === selected && a.kind === activeKind ? 1 : 0;
      const bAct = b.tid === selected && b.kind === activeKind ? 1 : 0;
      if (aAct !== bAct) return aAct - bAct;
      return 0;
    });
    return rows;
  }, [spots, paths, effectiveFor, selected, activeKind]);

  const overrideCount = useMemo(
    () => Object.values(current?.territories ?? {}).filter((e) => e.focus === 'override').length,
    [current],
  );

  const landscapeMarkers = useMemo(() => {
    const map = current?.landscape?.territories;
    if (!map) return [];
    const rows: { tid: string; index: number; placement: LandscapePlacement }[] = [];
    for (const [tid, list] of Object.entries(map)) {
      list.forEach((placement, index) => {
        rows.push({ tid, index, placement });
      });
    }
    // Selected territory's placements render last so they stay clickable when overlapping.
    rows.sort((a, b) => {
      const aSel = a.tid === selected ? 1 : 0;
      const bSel = b.tid === selected ? 1 : 0;
      if (aSel !== bSel) return aSel - bSel;
      return 0;
    });
    return rows;
  }, [current, selected]);

  const landscapeCount = useMemo(
    () => Object.values(current?.landscape?.territories ?? {}).reduce((n, list) => n + list.length, 0),
    [current],
  );

  const selectedEntry = selected ? entryFor(current, selected) : undefined;
  const selectedFocus: 'calculated' | 'override' = selectedEntry?.focus === 'override' ? 'override' : 'calculated';
  const selectedCalc = useMemo(
    () => (selected ? calculatedFor(selected) : {}),
    [selected, calculatedFor],
  );
  const selectedOverride = useMemo(() => {
    if (!selected || selectedEntry?.focus !== 'override') return {};
    const positions = selectedEntry.positions ?? {};
    const out: Partial<Record<UiKind, MapOverridePoint>> = {};
    for (const { kind } of SPOT_KINDS) {
      const pt = positions[storageKindFor(selected, kind, territories)];
      if (pt) out[kind] = pt;
    }
    return out;
  }, [selected, selectedEntry, territories]);

  const description =
    editorTab === 'landscape'
      ? 'Place terrain art constrained by each territory’s type. Auto-generate fills interiors; Border mode snaps to outlines. Emoji fallback stays until interior art exists for that type. Save applies to every setup using this map.'
      : spots
        ? 'Four dots per territory: blue units · red production/sea number · yellow faction logo · green other icons (territory type, homes, camps, ports, signals, rings). Pick a territory, switch it to Override, then drag its dots. Save applies to every setup using this map.'
        : 'Loading calculated spots…';

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={requestClose}>
      <div
        className="admin-modal admin-modal--graph"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-mapov-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="admin-graph__header">
          <div className="admin-graph__title-row">
            <h2 id="admin-mapov-title" className="admin-modal__title">
              Map editor
            </h2>
            <div className="admin-mapview__toggle" role="group" aria-label="Editor mode">
              <button
                type="button"
                className={`admin-mapview__toggle-btn${editorTab === 'icon' ? ' admin-mapview__toggle-btn--active' : ''}`}
                aria-pressed={editorTab === 'icon'}
                onClick={() => {
                  setEditorTab('icon');
                  setPlacing(null);
                  setSelectedPlacement(null);
                }}
              >
                Icon
              </button>
              <button
                type="button"
                className={`admin-mapview__toggle-btn${editorTab === 'landscape' ? ' admin-mapview__toggle-btn--active' : ''}`}
                aria-pressed={editorTab === 'landscape'}
                onClick={() => setEditorTab('landscape')}
              >
                Landscape
              </button>
            </div>
            <label className="admin-graph__prop">
              Map
              <select
                className="admin-page__select"
                value={mapBase}
                onChange={(e) => changeMap(e.target.value)}
                disabled={maps.length === 0}
              >
                {maps.length === 0 ? <option value="">No maps</option> : null}
                {maps.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="admin-graph__header-actions">
            {editorTab === 'landscape' ? (
              <>
                <button
                  type="button"
                  className="admin-page__btn"
                  disabled={autoBusy}
                  onClick={autoGenerateLandscape}
                  title="Place interior art on every territory whose terrain type has landscape images (keeps manual placements)"
                >
                  {autoBusy ? 'Generating…' : 'Auto-generate'}
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  disabled={landscapeCount === 0}
                  onClick={clearAllLandscape}
                >
                  Clear all
                </button>
              </>
            ) : null}
            <button
              type="button"
              className={`admin-page__btn${dirty ? ' admin-page__btn--primary' : ''}`}
              disabled={saving || !dirty}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="admin-page__btn" onClick={requestClose} disabled={saving}>
              Close
            </button>
          </div>
          <p className="admin-form__micro">{description}</p>
          {editorTab === 'icon' ? (
            <ul className="admin-graph__key mapov-legend" aria-label="Spot colors">
              {SPOT_KINDS.map((row) => (
                <li key={row.kind}>
                  <span
                    className="mapov-legend__swatch"
                    style={{ background: row.color }}
                    aria-hidden
                  />
                  {row.label}
                </li>
              ))}
            </ul>
          ) : (
            <p className="admin-form__micro mapov-landscape-hint">
              {placing
                ? `Click the map to place ${placing.img}${borderMode ? ' on the territory border' : ' inside the territory'} · Esc to cancel`
                : 'Pick an image below, then click the map. Drag existing art to move it.'}
              {landscapeCount ? ` · ${landscapeCount} placed` : ''}
            </p>
          )}
        </div>
        {loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {err ? <p className="admin-page__error">{err}</p> : null}
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
        {!loading && !err ? (
          <div className="admin-graph__body">
            <div className="admin-graph__side mapov-side">
              <input
                type="search"
                className="admin-form__input admin-graph__search"
                placeholder="Search territories"
                aria-label="Search territories"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <div className="admin-graph__list" role="listbox" aria-label="Territories">
                {filteredChoices.map((row) => {
                  const overridden = entryFor(current, row.id)?.focus === 'override';
                  return (
                    <button
                      key={row.id}
                      type="button"
                      role="option"
                      aria-selected={selected === row.id}
                      className={`admin-graph__list-item${selected === row.id ? ' admin-graph__list-item--selected' : ''}`}
                      onClick={() => {
                        setSelected(row.id);
                        setActiveKind('unit');
                        setSelectedPlacement(null);
                      }}
                    >
                      {row.label}
                      {overridden ? <span className="mapov-list-badge" title="Override">●</span> : null}
                    </button>
                  );
                })}
                {filteredChoices.length ? null : <p className="admin-graph__list-empty">No matches</p>}
              </div>
              {selected ? (
                <div className="mapov-inspector">
                  <div className="mapov-inspector__head">
                    <strong title={selected}>{territoryLabel(selected, territories)}</strong>
                    {editorTab === 'icon' ? (
                      <div className="admin-mapview__toggle" role="group" aria-label="Spot focus">
                        <button
                          type="button"
                          className={`admin-mapview__toggle-btn${selectedFocus === 'calculated' ? ' admin-mapview__toggle-btn--active' : ''}`}
                          aria-pressed={selectedFocus === 'calculated'}
                          onClick={() => setFocus(selected, 'calculated')}
                        >
                          Calculated
                        </button>
                        <button
                          type="button"
                          className={`admin-mapview__toggle-btn${selectedFocus === 'override' ? ' admin-mapview__toggle-btn--active' : ''}`}
                          aria-pressed={selectedFocus === 'override'}
                          onClick={() => setFocus(selected, 'override')}
                        >
                          Override
                        </button>
                      </div>
                    ) : (
                      <span className="mapov-kind__pos" title="Terrain type">{selectedTerrainType || 'land'}</span>
                    )}
                  </div>
                  {editorTab === 'landscape' ? (
                    selectedCategories.length ? (
                      <div className="mapov-landscape">
                        <div className="mapov-landscape__modes">
                          <label className="admin-page__checkbox-label">
                            <input
                              type="checkbox"
                              checked={borderMode}
                              onChange={(e) => setBorderMode(e.target.checked)}
                            />
                            Border mode (snap to outline)
                          </label>
                        </div>
                        <div className="mapov-landscape__palette" role="listbox" aria-label="Landscape images">
                          {selectedCategories.flatMap((cat) =>
                            (manifest[cat] ?? []).map((img) => {
                              const isPlacing = placing?.img === img && placing?.cat === cat;
                              return (
                                <button
                                  key={`${cat}/${img}`}
                                  type="button"
                                  role="option"
                                  aria-selected={isPlacing}
                                  className={`mapov-landscape__thumb${isPlacing ? ' mapov-landscape__thumb--active' : ''}`}
                                  title={`${cat} · ${img}`}
                                  onClick={() => {
                                    setPlacing(isPlacing ? null : { cat, img });
                                    setSelectedPlacement(null);
                                  }}
                                >
                                  <img src={landscapeSrc({ img })} alt={img} draggable={false} />
                                </button>
                              );
                            }),
                          )}
                        </div>
                        <ul className="mapov-kinds">
                          {selectedPlacements.map((p, index) => {
                            const isActive = selectedPlacement?.tid === selected && selectedPlacement?.index === index;
                            return (
                              <li
                                key={`${p.img}-${index}`}
                                className={`mapov-kind${isActive ? ' mapov-kind--active' : ''}`}
                                onClick={() => setSelectedPlacement({ tid: selected, index })}
                              >
                                <img
                                  className="mapov-landscape__row-img"
                                  src={landscapeSrc(p)}
                                  alt=""
                                  draggable={false}
                                />
                                <span className="mapov-kind__label">
                                  {p.img.replace(/\.png$/i, '')}
                                  {p.layer === 'border' ? ' · border' : ''}
                                  {p.auto ? ' · auto' : ''}
                                </span>
                                <label className="mapov-landscape__scale" title="Scale">
                                  <span className="visually-hidden">Scale</span>
                                  <input
                                    type="range"
                                    min={0.25}
                                    max={4}
                                    step={0.05}
                                    value={p.s ?? 1}
                                    onClick={(e) => e.stopPropagation()}
                                    onChange={(e) => setPlacementAt(selected, index, { s: Number(e.target.value) })}
                                  />
                                </label>
                                <button
                                  type="button"
                                  className="admin-page__btn mapov-kind__btn"
                                  title="Remove this placement"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setPlacementAt(selected, index, null);
                                  }}
                                >
                                  ×
                                </button>
                              </li>
                            );
                          })}
                          {selectedPlacements.length ? null : (
                            <li className="mapov-kind mapov-kind--absent">
                              <span className="mapov-kind__label">No landscape art placed yet</span>
                            </li>
                          )}
                        </ul>
                      </div>
                    ) : (
                      <p className="admin-form__micro">
                        No landscape art for this terrain type ({selectedTerrainType || 'land'}).
                        {borderMode && manifest.mountains
                          ? ' Border mountains are still available from the palette.'
                          : ' Enable Border mode to place mountain ranges on this territory’s edges.'}
                      </p>
                    )
                  ) : selectedFocus === 'override' ? (
                    <ul className="mapov-kinds">
                      {SPOT_KINDS.map(({ kind, hint }) => {
                        const ovp = selectedOverride[kind];
                        const calc = selectedCalc[kind];
                        const shown = ovp ?? calc;
                        if (!shown && !ALWAYS_KINDS.includes(kind)) {
                          return (
                            <li key={kind} className="mapov-kind mapov-kind--absent">
                              <span className="mapov-kind__swatch" style={{ background: KIND_COLOR[kind] }} aria-hidden />
                              <span className="mapov-kind__label" title={hint}>
                                {KIND_LABEL[kind]}
                              </span>
                              <button type="button" className="admin-page__btn mapov-kind__btn" onClick={() => addKind(selected, kind)}>
                                + Place
                              </button>
                            </li>
                          );
                        }
                        if (!shown) return null;
                        return (
                          <li
                            key={kind}
                            className={`mapov-kind${activeKind === kind ? ' mapov-kind--active' : ''}${ovp ? ' mapov-kind--override' : ''}`}
                            onClick={() => setActiveKind(kind)}
                          >
                            <span className="mapov-kind__swatch" style={{ background: KIND_COLOR[kind] }} aria-hidden />
                            <span className="mapov-kind__label" title={hint}>
                              {KIND_LABEL[kind]}
                            </span>
                            <span className="mapov-kind__pos" title={ovp ? 'Override' : 'Calculated'}>
                              {fmt(shown)}
                              {ovp ? '' : ' · calc'}
                            </span>
                            {ovp ? (
                              <button
                                type="button"
                                className="admin-page__btn mapov-kind__btn"
                                title="Reset this spot to calculated"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  resetKind(selected, kind);
                                }}
                              >
                                Reset
                              </button>
                            ) : null}
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    <p className="admin-form__micro">
                      Spots on the map are calculated. Switch to Override to move them.
                    </p>
                  )}
                </div>
              ) : (
                <p className="admin-form__micro">Choose a territory from the list or the map.</p>
              )}
            </div>
            <div
              ref={wrapRef}
              className={`admin-graph__viewport${placing ? ' admin-graph__viewport--placing' : ''}`}
              onWheel={onWheel}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
            >
              <div
                className="admin-graph__world"
                style={{
                  width: viewBox.width,
                  height: viewBox.height,
                  transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
                }}
              >
                {pngFailed ? null : (
                  <img
                    className="admin-graph__landscape"
                    src={`/${mapBase}.png`}
                    alt=""
                    draggable={false}
                    onError={() => setPngFailed(true)}
                  />
                )}
                <svg
                  className="admin-graph__svg"
                  viewBox={`0 0 ${viewBox.width} ${viewBox.height}`}
                  width={viewBox.width}
                  height={viewBox.height}
                >
                  {Array.from(paths.entries()).map(([tid, pathData]) => (
                    <path
                      key={tid}
                      data-tid={tid}
                      d={pathData.d}
                      transform={pathData.transform}
                      className={`admin-graph__path${selected && sameTerritoryId(tid, selected) ? ' admin-graph__path--selected' : ''}`}
                    />
                  ))}
                </svg>
              </div>
              {editorTab === 'landscape' ? (
                <div className="mapov-dots mapov-landscape-markers">
                  {landscapeMarkers.map((row) => {
                    const isActive =
                      selectedPlacement?.tid === row.tid && selectedPlacement?.index === row.index;
                    const x = transform.x + row.placement.x * transform.scale;
                    const y = transform.y + row.placement.y * transform.scale;
                    const size = Math.max(18, 36 * (row.placement.s ?? 1) * Math.min(transform.scale, 1.4));
                    return (
                      <div
                        key={`${row.tid}|${row.index}|${row.placement.img}`}
                        role="button"
                        tabIndex={0}
                        aria-label={`${territoryLabel(row.tid, territories)} ${row.placement.img}`}
                        className={`mapov-placement${isActive ? ' mapov-placement--active' : ''}${row.placement.layer === 'border' ? ' mapov-placement--border' : ''}${row.placement.auto ? ' mapov-placement--auto' : ''}`}
                        style={{ left: x, top: y, width: size, height: size }}
                        title={`${territoryLabel(row.tid, territories)} · ${row.placement.img} · ${row.placement.layer}${placing ? '' : ' — drag to move'}`}
                        onPointerDown={(e) => onPlacementPointerDown(e, row.tid, row.index)}
                        onPointerMove={onPlacementPointerMove}
                        onPointerUp={onPlacementPointerUp}
                        onPointerCancel={onPlacementPointerUp}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            setSelected(row.tid);
                            setSelectedPlacement({ tid: row.tid, index: row.index });
                          }
                        }}
                      >
                        <img src={landscapeSrc(row.placement)} alt="" draggable={false} />
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="mapov-dots">
                  {dots.map((d) => {
                    const isActive = d.tid === selected && d.kind === activeKind;
                    const x = transform.x + d.pt.x * transform.scale;
                    const y = transform.y + d.pt.y * transform.scale;
                    return (
                      <div
                        key={`${d.tid}|${d.kind}`}
                        role="button"
                        tabIndex={0}
                        aria-label={`${territoryLabel(d.tid, territories)} ${KIND_LABEL[d.kind]}`}
                        className={`mapov-dot mapov-dot--${d.kind}${d.isOverride ? ' mapov-dot--override' : ''}${isActive ? ' mapov-dot--active' : ''}`}
                        style={{ left: x, top: y, background: KIND_COLOR[d.kind] }}
                        title={`${territoryLabel(d.tid, territories)} · ${KIND_LABEL[d.kind]} · ${d.isOverride ? 'override' : 'calculated'}${selectedFocus === 'override' && d.tid === selected ? ' — drag to move' : ''}`}
                        onPointerDown={(e) => onDotPointerDown(e, d.tid, d.kind)}
                        onPointerMove={onDotPointerMove}
                        onPointerUp={onDotPointerUp}
                        onPointerCancel={onDotPointerUp}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            setSelected(d.tid);
                            setActiveKind(d.kind);
                          }
                        }}
                      />
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        ) : null}
        <p className="admin-graph__status">
          {editorTab === 'landscape'
            ? selected
              ? `${territoryLabel(selected, territories)} · ${selectedTerrainType || 'land'} · ${selectedPlacements.length} placed${landscapeCount ? ` · ${landscapeCount} total` : ''}`
              : 'No territory selected'
            : selected
              ? `${territoryLabel(selected, territories)} · ${KIND_LABEL[activeKind]}${
                  selectedFocus === 'override' ? ' · override' : ' · calculated'
                }${overrideCount ? ` · ${overrideCount} overridden` : ''}`
              : 'No territory selected'}
        </p>
        {/* Hidden spot oracle: a mounted-but-invisible GameMap reports the exact calculated anchors. */}
        <div className="mapov-oracle" aria-hidden="true">
          <GameMap
            key={mapBase || DEFAULT_MAP_BASE}
            gameState={oracleGameState}
            selectedTerritory={null}
            selectedUnit={null}
            territoryData={oracleTerritoryData}
            territoryUnits={{}}
            rings={[]}
            unitDefs={{}}
            unitStats={{}}
            factionData={{}}
            onTerritorySelect={NOOP}
            onUnitSelect={NOOP}
            onUnitMove={NOOP}
            canAct={false}
            isMovementPhase={false}
            isCombatMove={false}
            isMobilizePhase={false}
            hasMobilizationSelected={false}
            pendingMoveConfirm={null}
            onSetPendingMove={NOOP}
            pendingMoves={[]}
            onSpotsCalculated={onSpotsCalculated}
          />
        </div>
        {discardOpen ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation" onClick={() => { setDiscardOpen(false); setPendingMap(null); }}>
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-mapov-discard-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-mapov-discard-title" className="admin-modal__title">
                {pendingMap ? 'Discard changes and switch maps?' : 'Close without saving?'}
              </h2>
              <p className="admin-form__micro">Unsaved icon overrides will be discarded.</p>
              <div className="admin-modal__actions">
                <button type="button" className="admin-page__btn" onClick={() => { setDiscardOpen(false); setPendingMap(null); }}>
                  Keep editing
                </button>
                <button type="button" className="admin-page__btn admin-page__btn--danger" onClick={confirmDiscard}>
                  {pendingMap ? 'Discard and switch' : 'Discard and close'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default MapOverridesPane;
