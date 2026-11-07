import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import GameMap, { DEFAULT_MAP_BASE } from '../../components/GameMap';
import { TERRITORY_UNIT_PIXEL_NUDGE } from '../../components/mapSpotNudge';
import { api } from '../../services/api';
import type {
  GameState,
  LandscapeOverrides,
  LandscapePlacement,
  MapGraph,
  MapOverridePoint,
  MapOverridesData,
  MapSpotKind,
} from '../../types/game';
import { normalizeMapOverrides } from '../../types/game';
import { MapTextureSlider } from './MapTextureSlider';
import {
  autoGenerateForTerritory,
  isPointInTerritoryPath,
  landmarkFileForTerritory,
  LANDSCAPE_LANDMARK_CATEGORY,
  pointInsideTerritory,
  landscapeCategoriesForTerrain,
  landscapeRotation,
  landscapeSrc,
  loadLandscapeManifest,
  nearestPointOnOutline,
  territoryPathEl,
  type LandscapeManifest,
} from './landscape';
import {
  adjacencyEdges,
  computePathCentroids,
  loadTerritorySvgPaths,
  lookupCentroid,
  resolveTerritoryKey,
  sameTerritoryId,
  territoriesWithEdgeEdits,
  toMapBase,
  type TerritoryEdgeEdit,
  type TerritoryEdgeField,
  type TerritoryPathData,
} from './territoryGraph';
import { MapBuilder } from './MapBuilder';

type Pt = { x: number; y: number };
type Dict = Record<string, Record<string, unknown>>;

type HintKind = 'units' | 'number' | 'icons' | 'logo';

/** Landscape-tab cursor behavior: pan the map, rubber-band select, or stamp along a drag. */
type CursorMode = 'navigate' | 'select' | 'paint';

/** ViewBox-space spacing between paint-mode stamps along a drag path. */
const PAINT_STAMP_SPACING = 36;

/** Minimum viewBox-space distance between painted pieces (dedupe vs existing stamps). */
const PAINT_MIN_DIST = 36;

/** Axis-aligned region in some map's viewBox space (used by the copy dialog). */
type RegionRect = { x1: number; y1: number; x2: number; y2: number };

/** Target territory geometry used to place copied pieces by position. */
type TargetHitArea = {
  tid: string;
  el: SVGPathElement;
  bbox: { x: number; y: number; width: number; height: number };
};

type CopyPlanItem = { tid: string; placement: LandscapePlacement };

type CopyPlan = {
  items: CopyPlanItem[];
  inRegion: number;
  outsideRegion: number;
  unmatched: number;
  capped: number;
};

/** Multi-select key for a placement: landscape territory key + index in its list. */
function placementKey(tid: string, index: number): string {
  return `${tid}#${index}`;
}

function parsePlacementKey(key: string): { tid: string; index: number } | null {
  const sep = key.lastIndexOf('#');
  if (sep <= 0) return null;
  const index = Number(key.slice(sep + 1));
  if (!Number.isInteger(index) || index < 0) return null;
  return { tid: key.slice(0, sep), index };
}

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

const GRAPH_FIELDS: TerritoryEdgeField[] = ['adjacent', 'aerial_adjacent', 'ford_adjacent'];

type EditorHistoryTab = 'icon' | 'landscape' | 'graph';

/** One tab's slice of the map document. Undo restores only this slice. */
type EditorSnapshot = {
  territories?: MapOverridesData['territories'];
  landscape?: LandscapeOverrides | null;
  terrain?: Record<string, string> | null;
  graph?: MapGraph | null;
};

const MAX_EDITOR_HISTORY = 100;

function emptyEditorHistory(): Record<EditorHistoryTab, EditorSnapshot[]> {
  return { icon: [], landscape: [], graph: [] };
}

function snapshotFor(tab: EditorHistoryTab, data: MapOverridesData): EditorSnapshot {
  if (tab === 'icon') return { territories: data.territories };
  if (tab === 'landscape') return { landscape: data.landscape ?? null, terrain: data.terrain ?? null };
  return { graph: data.graph ?? null };
}

function cloneSnapshot(snap: EditorSnapshot): EditorSnapshot {
  return JSON.parse(JSON.stringify(snap)) as EditorSnapshot;
}

function applyEditorSnapshot(base: MapOverridesData, snap: EditorSnapshot): MapOverridesData {
  return nextOverrides(base, snap);
}

const EDGE_TYPES: { field: TerritoryEdgeField; label: string; kind: 'land' | 'aerial' | 'ford' }[] = [
  { field: 'adjacent', label: 'Land', kind: 'land' },
  { field: 'aerial_adjacent', label: 'Aerial', kind: 'aerial' },
  { field: 'ford_adjacent', label: 'Ford', kind: 'ford' },
];

function emptyGraph(): MapGraph {
  return { adjacent: {}, aerial_adjacent: {}, ford_adjacent: {} };
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [];
}

function offsetEdge(pa: Pt, pb: Pt, dist: number): { a: Pt; b: Pt } {
  const dx = pb.x - pa.x;
  const dy = pb.y - pa.y;
  const len = Math.hypot(dx, dy) || 1;
  const ox = (-dy / len) * dist;
  const oy = (dx / len) * dist;
  return {
    a: { x: pa.x + ox, y: pa.y + oy },
    b: { x: pb.x + ox, y: pb.y + oy },
  };
}

/** Map adjacency wins over the setup copy for territories stored on the graph. */
function withGraph(defs: Dict, graph: MapGraph | undefined): Dict {
  if (!graph) return defs;
  const ids = new Set<string>();
  for (const field of GRAPH_FIELDS) {
    for (const id of Object.keys(graph[field] ?? {})) ids.add(id);
  }
  if (!ids.size) return defs;
  const out: Dict = { ...defs };
  for (const id of ids) {
    const key = resolveTerritoryKey(out, id) ?? id;
    const base = { ...(out[key] ?? {}) };
    for (const field of GRAPH_FIELDS) {
      const table = graph[field];
      if (table && Object.prototype.hasOwnProperty.call(table, id)) base[field] = table[id];
    }
    out[key] = base;
  }
  return out;
}

/** Copy a map document, replacing only the fields in the patch. Sibling terrain/landscape/graph stay. */
function nextOverrides(
  base: MapOverridesData,
  patch: {
    territories?: MapOverridesData['territories'];
    landscape?: LandscapeOverrides | null;
    terrain?: Record<string, string> | null;
    graph?: MapGraph | null;
  },
): MapOverridesData {
  const data: MapOverridesData = { territories: patch.territories ?? base.territories };
  const landscape = patch.landscape === undefined ? base.landscape : patch.landscape ?? undefined;
  if (landscape) data.landscape = landscape;
  const terrain = patch.terrain === undefined ? base.terrain : patch.terrain ?? undefined;
  if (terrain) data.terrain = terrain;
  const graph = patch.graph === undefined ? base.graph : patch.graph ?? undefined;
  if (graph) data.graph = graph;
  return data;
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
export function MapOverridesPane({
  territories = {},
  setupId = '',
  onClose,
}: {
  territories?: Dict;
  /** Open setup, preferred when it uses the map being edited. */
  setupId?: string;
  onClose: () => void;
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [territoryDefs, setTerritoryDefs] = useState<Dict>(territories);
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
  const [terrainFilter, setTerrainFilter] = useState('');
  const [stored, setStored] = useState<MapOverridesData | null>(null);
  const [draft, setDraft] = useState<MapOverridesData | null>(null);
  const [past, setPast] = useState(emptyEditorHistory);
  const [future, setFuture] = useState(emptyEditorHistory);
  const [spots, setSpots] = useState<CalculatedSpots | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [editorTab, setEditorTab] = useState<'icon' | 'landscape' | 'builder' | 'graph'>('icon');
  const [edgeField, setEdgeField] = useState<TerritoryEdgeField>('adjacent');
  const [terrainTypes, setTerrainTypes] = useState<string[]>([]);
  const [manifest, setManifest] = useState<LandscapeManifest>({});
  const [placing, setPlacing] = useState<{ cat: string; img: string } | null>(null);
  const [borderMode, setBorderMode] = useState(false);
  const [artMode, setArtMode] = useState<'landscape' | 'landmarks'>('landscape');
  const [selectedPlacement, setSelectedPlacement] = useState<{ tid: string; index: number } | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);
  const [cursorMode, setCursorMode] = useState<CursorMode>('navigate');
  const [band, setBand] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const [multiSel, setMultiSel] = useState<Set<string>>(() => new Set());
  const [paintRadius, setPaintRadius] = useState(0);
  // Copy-from-another-map dialog: source map preview + optional region filter.
  const [copyOpen, setCopyOpen] = useState(false);
  const [copySource, setCopySource] = useState('');
  const [copyLoading, setCopyLoading] = useState(false);
  const [copyErr, setCopyErr] = useState<string | null>(null);
  const [copyPaths, setCopyPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [copyViewBox, setCopyViewBox] = useState({ width: 0, height: 0 });
  const [copyOverrides, setCopyOverrides] = useState<MapOverridesData | null>(null);
  const [copyRegion, setCopyRegion] = useState<RegionRect | null>(null);
  const [copyDrag, setCopyDrag] = useState<RegionRect | null>(null);
  const [targetHitAreas, setTargetHitAreas] = useState<TargetHitArea[]>([]);

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
  const selectDragRef = useRef<{ x: number; y: number; id: number } | null>(null);
  const paintLastRef = useRef<Pt | null>(null);
  /** Pointer that owns the current paint stroke. Null means the button is up, so hover must not stamp. */
  const paintPointerRef = useRef<number | null>(null);
  const copyDragRef = useRef<{ id: number; sx: number; sy: number; x1: number; y1: number } | null>(null);
  const bulkDragRef = useRef<{
    id: number;
    sx: number;
    sy: number;
    scale: number;
    items: { tid: string; index: number; x: number; y: number; layer: 'interior' | 'border' }[];
  } | null>(null);

  const current = draft ?? stored;
  const dirty = draft != null && stored != null && stableJson(draft) !== stableJson(stored);
  const currentRef = useRef(current);
  const storedRef = useRef(stored);
  const pastRef = useRef(past);
  const futureRef = useRef(future);
  /** Set on pointer-down so a drag or slider records one step, not one per move. */
  const armedGesture = useRef<EditorHistoryTab | null>(null);
  const gestureLock = useRef<EditorHistoryTab | null>(null);
  currentRef.current = current;
  storedRef.current = stored;
  pastRef.current = past;
  futureRef.current = future;

  const clearHistory = useCallback(() => {
    const empty = emptyEditorHistory();
    pastRef.current = empty;
    futureRef.current = emptyEditorHistory();
    setPast(pastRef.current);
    setFuture(futureRef.current);
    armedGesture.current = null;
    gestureLock.current = null;
  }, []);

  const recordChange = useCallback((tab: EditorHistoryTab) => {
    if (gestureLock.current === tab) return;
    const cur = currentRef.current ?? storedRef.current;
    if (!cur) return;
    const snap = cloneSnapshot(snapshotFor(tab, cur));
    const nextPast = {
      ...pastRef.current,
      [tab]: [...pastRef.current[tab], snap].slice(-MAX_EDITOR_HISTORY),
    };
    pastRef.current = nextPast;
    setPast(nextPast);
    if (futureRef.current[tab].length) {
      const nextFuture = { ...futureRef.current, [tab]: [] as EditorSnapshot[] };
      futureRef.current = nextFuture;
      setFuture(nextFuture);
    }
    if (armedGesture.current === tab) gestureLock.current = tab;
  }, []);

  const commitDraft = useCallback(
    (tab: EditorHistoryTab, produce: (base: MapOverridesData) => MapOverridesData) => {
      const base = currentRef.current ?? storedRef.current ?? { territories: {} };
      const next = produce(base);
      if (stableJson(next) === stableJson(base)) return;
      recordChange(tab);
      currentRef.current = next;
      setDraft(storedRef.current && stableJson(next) === stableJson(storedRef.current) ? null : next);
      setSaveError(null);
    },
    [recordChange],
  );

  const armGesture = useCallback((tab: EditorHistoryTab) => {
    armedGesture.current = tab;
    gestureLock.current = null;
  }, []);

  const endGesture = useCallback(() => {
    armedGesture.current = null;
    gestureLock.current = null;
  }, []);

  const restoreHistory = useCallback((tab: EditorHistoryTab, snap: EditorSnapshot) => {
    const cur = currentRef.current ?? storedRef.current;
    if (!cur) return;
    const next = applyEditorSnapshot(cur, snap);
    currentRef.current = next;
    setDraft(storedRef.current && stableJson(next) === stableJson(storedRef.current) ? null : next);
    if (tab === 'landscape') {
      setSelectedPlacement(null);
      setMultiSel(new Set());
    }
  }, []);

  const undoTab = useCallback(() => {
    if (editorTab !== 'icon' && editorTab !== 'landscape' && editorTab !== 'graph') return;
    endGesture();
    const stack = pastRef.current[editorTab];
    if (!stack.length) return;
    const prev = stack[stack.length - 1];
    const cur = currentRef.current ?? storedRef.current;
    if (!cur || !prev) return;
    const now = cloneSnapshot(snapshotFor(editorTab, cur));
    const nextPast = { ...pastRef.current, [editorTab]: stack.slice(0, -1) };
    const nextFuture = {
      ...futureRef.current,
      [editorTab]: [now, ...futureRef.current[editorTab]].slice(0, MAX_EDITOR_HISTORY),
    };
    pastRef.current = nextPast;
    futureRef.current = nextFuture;
    setPast(nextPast);
    setFuture(nextFuture);
    restoreHistory(editorTab, prev);
  }, [editorTab, endGesture, restoreHistory]);

  const redoTab = useCallback(() => {
    if (editorTab !== 'icon' && editorTab !== 'landscape' && editorTab !== 'graph') return;
    endGesture();
    const stack = futureRef.current[editorTab];
    if (!stack.length) return;
    const nextSnap = stack[0];
    const cur = currentRef.current ?? storedRef.current;
    if (!cur || !nextSnap) return;
    const now = cloneSnapshot(snapshotFor(editorTab, cur));
    const nextFuture = { ...futureRef.current, [editorTab]: stack.slice(1) };
    const nextPast = {
      ...pastRef.current,
      [editorTab]: [...pastRef.current[editorTab], now].slice(-MAX_EDITOR_HISTORY),
    };
    pastRef.current = nextPast;
    futureRef.current = nextFuture;
    setPast(nextPast);
    setFuture(nextFuture);
    restoreHistory(editorTab, nextSnap);
  }, [editorTab, endGesture, restoreHistory]);
  // Map terrain wins over the setup copy so the landscape palette follows what this map stores.
  const terrainBackedDefs = useMemo(() => {
    const terrain = current?.terrain;
    if (!terrain || !Object.keys(terrain).length) return territoryDefs;
    const out: Dict = { ...territoryDefs };
    for (const [tid, type] of Object.entries(terrain)) {
      const key = resolveTid(out, tid) ?? tid;
      out[key] = { ...(out[key] ?? {}), terrain_type: type };
    }
    return out;
  }, [territoryDefs, current]);

  const graphShown = useMemo(
    () => withGraph(terrainBackedDefs, current?.graph),
    [terrainBackedDefs, current?.graph],
  );
  const centroids = useMemo(
    () => (editorTab === 'graph' ? computePathCentroids(paths, viewBox, mapBase) : {}),
    [editorTab, paths, viewBox, mapBase],
  );

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
    const onLibrary = () => {
      api
        .adminListMaps()
        .then((r) => {
          if (cancelled) return;
          const list = Array.isArray(r?.maps) ? r.maps.filter((m) => typeof m === 'string' && m) : [];
          setMaps(list);
          setMapBase((prev) => (prev && list.includes(prev) ? prev : prev || list[0] || ''));
        })
        .catch(() => undefined);
    };
    window.addEventListener('admin-maps-changed', onLibrary);
    return () => {
      cancelled = true;
      window.removeEventListener('admin-maps-changed', onLibrary);
    };
  }, []);

  // Terrain comes from a setup that uses this map. The open setup is only a match
  // when it is the one on screen; otherwise every region fell back to "land".
  useEffect(() => {
    if (!mapBase) return;
    let cancelled = false;
    api
      .adminListSetups()
      .then(async (res) => {
        const rows = Array.isArray(res?.setups) ? res.setups : [];
        const matches = rows.filter((row) => toMapBase(row.map_asset || row.id) === mapBase);
        const preferred = matches.find((row) => row.id === setupId) ?? matches[0];
        if (!preferred) {
          if (!cancelled) setTerritoryDefs(territories);
          return;
        }
        if (preferred.id === setupId && Object.keys(territories).length > 0) {
          if (!cancelled) setTerritoryDefs(territories);
          return;
        }
        const bundle = await api.adminGetSetup(preferred.id);
        if (!cancelled) setTerritoryDefs((bundle.territories ?? {}) as Dict);
      })
      .catch(() => {
        if (!cancelled) setTerritoryDefs(territories);
      });
    return () => {
      cancelled = true;
    };
  }, [mapBase, setupId, territories, clearHistory]);

  useEffect(() => {
    let cancelled = false;
    loadLandscapeManifest().then((m) => {
      if (!cancelled) setManifest(m);
    });
    api
      .getCatalog()
      .then((catalog) => {
        if (cancelled) return;
        const types = Array.isArray(catalog?.terrain_types)
          ? catalog.terrain_types.filter((t): t is string => typeof t === 'string' && t.length > 0)
          : [];
        setTerrainTypes(types);
      })
      .catch(() => {
        if (!cancelled) setTerrainTypes([]);
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
    clearHistory();
    setSelected(null);
    setPngFailed(false);
    setSearch('');
    setTerrainFilter('');
    setPlacing(null);
    setBorderMode(false);
    setSelectedPlacement(null);
    setCursorMode('navigate');
    setBand(null);
    setMultiSel(new Set());
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

  // --- copy-from-another-map: source data + target hit-test geometry ------

  useEffect(() => {
    if (!copyOpen || !copySource) {
      setCopyLoading(false);
      setCopyErr(null);
      return;
    }
    let cancelled = false;
    setCopyLoading(true);
    setCopyErr(null);
    setCopyOverrides(null);
    setCopyPaths(new Map());
    setCopyRegion(null);
    setCopyDrag(null);
    Promise.all([
      api.getMapOverrides(copySource).then((r) => normalizeMapOverrides(r?.overrides)),
      loadTerritorySvgPaths(copySource),
    ])
      .then(([overrides, { paths: srcPaths, viewBox: srcViewBox }]) => {
        if (cancelled) return;
        setCopyOverrides(overrides);
        setCopyPaths(srcPaths);
        setCopyViewBox(srcViewBox);
        setCopyLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setCopyErr(e instanceof Error ? e.message : 'Failed to load source map');
        setCopyLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [copyOpen, copySource]);

  // Target territory fills for position-based copy. Runs after paint so the
  // freshly loaded SVG paths are in the DOM; bboxes prefilter isPointInFill.
  useEffect(() => {
    if (loading || !wrapRef.current) return;
    const out: TargetHitArea[] = [];
    for (const node of wrapRef.current.querySelectorAll('path.admin-graph__path')) {
      const el = node as SVGPathElement;
      const tid = el.dataset.tid;
      if (!tid) continue;
      try {
        const b = el.getBBox();
        if (Number.isFinite(b.x + b.y + b.width + b.height) && b.width > 0 && b.height > 0) {
          out.push({ tid, el, bbox: { x: b.x, y: b.y, width: b.width, height: b.height } });
        }
      } catch {
        /* ignore */
      }
    }
    setTargetHitAreas(out);
  }, [loading, paths]);

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
    for (const [tid, def] of Object.entries(terrainBackedDefs)) {
      const terrain =
        typeof def.terrain_type === 'string' ? def.terrain_type : typeof def.terrain === 'string' ? def.terrain : 'land';
      out[tid] = {
        name: territoryLabel(tid, terrainBackedDefs),
        terrain,
        stronghold: false,
        produces: 0,
        adjacent: [],
      };
    }
    return out;
  }, [terrainBackedDefs]);

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
    window.addEventListener('pointerup', endGesture);
    window.addEventListener('pointercancel', endGesture);
    return () => {
      window.removeEventListener('pointerup', endGesture);
      window.removeEventListener('pointercancel', endGesture);
    };
  }, [endGesture]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (editorTab === 'builder') return; // the Builder owns Escape (cancel stroke / close its dialogs)
      if (e.key === 'Escape' && copyOpen) {
        e.stopPropagation();
        setCopyOpen(false);
        return;
      }
      if (e.key === 'Escape' && clearConfirmOpen) {
        e.stopPropagation();
        setClearConfirmOpen(false);
        return;
      }
      if (e.key === 'Escape' && band) {
        e.stopPropagation();
        selectDragRef.current = null;
        setBand(null);
        return;
      }
      if (e.key === 'Escape' && multiSel.size) {
        e.stopPropagation();
        setMultiSel(new Set());
        return;
      }
      if (e.key === 'Escape' && placing) {
        e.stopPropagation();
        setPlacing(null);
        return;
      }
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable);
      if (!typing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        e.stopPropagation();
        if (e.shiftKey) redoTab();
        else undoTab();
        return;
      }
      if (!typing && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        e.stopPropagation();
        redoTab();
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
  }, [discardOpen, saving, dirty, onClose, placing, band, multiSel, copyOpen, clearConfirmOpen, editorTab, undoTab, redoTab]);

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
    clearHistory();
    setDiscardOpen(false);
    if (pendingMap) {
      setMapBase(pendingMap);
      setPendingMap(null);
    } else {
      onClose();
    }
  };

  // Native non-passive wheel: React registers onWheel as a passive listener, so
  // preventDefault() inside it is a no-op and the browser rubber-bands the page /
  // modal ("the outside") instead of letting only the map respond. Attach directly
  // with { passive: false }, like MapBuilder and GameMap do.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      // deltaMode: 1 = lines, 2 = pages — normalize to pixels (Firefox wheel, keyboard scroll).
      const mult = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      const dx = e.deltaX * mult;
      const dy = e.deltaY * mult;
      if (!e.ctrlKey) {
        // Two-finger scroll (and plain mouse wheel) pans.
        setTransform((prev) => ({
          ...prev,
          x: prev.x + dx,
          y: prev.y + dy,
        }));
        return;
      }
      // Pinch (ctrlKey) and Ctrl+wheel zoom toward cursor.
      const rect = el.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const factor = dy < 0 ? 1.08 : 1 / 1.08;
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
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
  }, [loading, err, editorTab]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    // Middle-button always pans so the map stays reachable in select/paint modes.
    if (e.button === 1) {
      didDragRef.current = false;
      dragRef.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y, id: e.pointerId };
      return;
    }
    if (e.button !== 0) return;
    didDragRef.current = false;
    const landscape = editorTab === 'landscape';
    if (landscape && cursorMode === 'select') {
      const el = wrapRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      selectDragRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top, id: e.pointerId };
      setBand(null);
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }
    if (landscape && cursorMode === 'paint') {
      armGesture('landscape');
      const el = wrapRef.current;
      if (!el) return;
      paintPointerRef.current = e.pointerId;
      paintLastRef.current = null;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      // Stamp at the press point, then only along this pointer while it stays down.
      paintStampAt(e.clientX, e.clientY);
      paintLastRef.current = viewportToViewBox(e.clientX, e.clientY);
      return;
    }
    dragRef.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y, id: e.pointerId };
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const sd = selectDragRef.current;
    if (sd && e.pointerId === sd.id) {
      const el = wrapRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const x1 = e.clientX - rect.left;
      const y1 = e.clientY - rect.top;
      setBand({
        left: Math.min(sd.x, x1),
        top: Math.min(sd.y, y1),
        width: Math.abs(x1 - sd.x),
        height: Math.abs(y1 - sd.y),
      });
      return;
    }
    if (editorTab === 'landscape' && cursorMode === 'paint') {
      if (paintPointerRef.current === e.pointerId) {
        // Only the pointer that pressed down, and only while that button is held, paints.
        // (A missing last point skips the spacing check.)
        if (!placing || (e.buttons & 1) === 0) return;
        const last = paintLastRef.current;
        const vb = viewportToViewBox(e.clientX, e.clientY);
        if (!last || !vb) return;
        const step = Math.max(PAINT_STAMP_SPACING, paintRadius);
        if (Math.hypot(vb.x - last.x, vb.y - last.y) < step) return;
        paintStampAt(e.clientX, e.clientY);
        paintLastRef.current = vb;
        return;
      }
      // Paint mode: no hover-stamping, and only a live (middle-button) pan may move the map.
      // Falling through unconditionally here let a stale dragRef pan on plain mouse moves.
      if (dragRef.current?.id !== e.pointerId) return;
    }
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

  const listsNeighbor = (from: string, to: string, field: TerritoryEdgeField) =>
    stringList(graphShown[from]?.[field]).some((id) => sameTerritoryId(id, to));

  const toggleGraphEdge = (source: string, neighbor: string) => {
    if (!current) return;
    const sourceKey = resolveTerritoryKey(graphShown, source);
    const neighborKey = resolveTerritoryKey(graphShown, neighbor);
    if (!sourceKey || !neighborKey || sameTerritoryId(sourceKey, neighborKey)) return;
    const turningOn = !listsNeighbor(sourceKey, neighborKey, edgeField);
    const batch: TerritoryEdgeEdit[] = [];
    const add = (field: TerritoryEdgeField, on: boolean) =>
      batch.push({ source: sourceKey, neighbor: neighborKey, field, on, seq: batch.length });
    add(edgeField, turningOn);
    if (turningOn && edgeField === 'adjacent') {
      for (const other of ['aerial_adjacent', 'ford_adjacent'] as const) {
        if (listsNeighbor(sourceKey, neighborKey, other) || listsNeighbor(neighborKey, sourceKey, other)) add(other, false);
      }
    } else if (turningOn && (edgeField === 'aerial_adjacent' || edgeField === 'ford_adjacent')) {
      if (listsNeighbor(sourceKey, neighborKey, 'adjacent') || listsNeighbor(neighborKey, sourceKey, 'adjacent')) {
        add('adjacent', false);
      }
    }
    const edited = territoriesWithEdgeEdits(
      {
        [sourceKey]: graphShown[sourceKey] ?? {},
        [neighborKey]: graphShown[neighborKey] ?? {},
      },
      batch,
    );
    const graph = current.graph
      ? {
          adjacent: { ...current.graph.adjacent },
          aerial_adjacent: { ...current.graph.aerial_adjacent },
          ford_adjacent: { ...current.graph.ford_adjacent },
        }
      : emptyGraph();
    for (const id of [sourceKey, neighborKey]) {
      for (const field of GRAPH_FIELDS) graph[field][id] = stringList(edited[id]?.[field]);
    }
    commitDraft('graph', (base) => nextOverrides(base, { graph }));
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
    // Pan release first: the mode branches below must not strand an active middle-drag
    // pan (a stale dragRef made later mouse moves pan with no button held).
    const pan = dragRef.current;
    if (pan && pan.id === e.pointerId) {
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      dragRef.current = null;
      if (didDragRef.current) return;
    }
    const sd = selectDragRef.current;
    if (sd && e.pointerId === sd.id) {
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      selectDragRef.current = null;
      const el = wrapRef.current;
      if (el) {
        const rect = el.getBoundingClientRect();
        const x2 = e.clientX - rect.left;
        const y2 = e.clientY - rect.top;
        const vx1 = Math.min(sd.x, x2);
        const vy1 = Math.min(sd.y, y2);
        const vx2 = Math.max(sd.x, x2);
        const vy2 = Math.max(sd.y, y2);
        if (vx2 - vx1 < 4 && vy2 - vy1 < 4) {
          // Bare click: plain click clears the selection; modifier clicks leave it alone.
          if (!e.shiftKey && !e.ctrlKey && !e.metaKey) setMultiSel(new Set());
        } else {
          const bx1 = (vx1 - transform.x) / transform.scale;
          const by1 = (vy1 - transform.y) / transform.scale;
          const bx2 = (vx2 - transform.x) / transform.scale;
          const by2 = (vy2 - transform.y) / transform.scale;
          const hits: string[] = [];
          for (const row of landscapeMarkers) {
            const p = row.placement;
            if (p.x >= bx1 && p.x <= bx2 && p.y >= by1 && p.y <= by2) {
              hits.push(placementKey(row.tid, row.index));
            }
          }
          setMultiSel((prev) => {
            if (e.ctrlKey || e.metaKey) {
              const next = new Set(prev);
              for (const h of hits) next.delete(h);
              return next;
            }
            if (e.shiftKey) {
              const next = new Set(prev);
              for (const h of hits) next.add(h);
              return next;
            }
            return new Set(hits);
          });
        }
      }
      setBand(null);
      return;
    }
    if (editorTab === 'landscape' && cursorMode === 'paint') {
      endGesture();
      if (paintPointerRef.current === e.pointerId) {
        try {
          if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* ignore */
        }
        paintPointerRef.current = null;
        paintLastRef.current = null;
      }
      return;
    }
    if (editorTab === 'landscape' && cursorMode === 'select') {
      // Band was cancelled (Escape) or never started — don't fall through to territory selection.
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }
    if (didDragRef.current) return;
    if (editorTab === 'landscape' && placing) {
      tryPlaceLandscape(e.clientX, e.clientY);
      return;
    }
    const tid = territoryIdFromPoint(e.clientX, e.clientY);
    if (editorTab === 'graph') {
      if (!tid) return;
      if (selected && !sameTerritoryId(tid, selected)) toggleGraphEdge(selected, tid);
      else setSelected(tid);
      return;
    }
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
      out.power = isSeaTerritory(tid, terrainBackedDefs) ? (h?.number ?? p?.marker) : p?.marker;
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
    [spots, terrainBackedDefs],
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
      const ovp = overridePositionsFor(tid)?.[storageKindFor(tid, kind, terrainBackedDefs)];
      if (ovp) return { pt: ovp, isOverride: true };
      const calc = calculatedFor(tid)[kind];
      return calc ? { pt: calc, isOverride: false } : null;
    },
    [calculatedFor, overridePositionsFor, terrainBackedDefs],
  );

  const updateEntry = useCallback((tid: string, entry: MapOverridesData['territories'][string] | null) => {
    commitDraft('icon', (base) => {
      const key = resolveTid(base.territories, tid) ?? tid;
      const next = { ...base.territories };
      if (entry === null) delete next[key];
      else next[key] = entry;
      return nextOverrides(base, { territories: next });
    });
  }, [commitDraft]);

  const updateLandscape = useCallback(
    (fn: (prev: LandscapeOverrides | undefined) => LandscapeOverrides | undefined) => {
      commitDraft('landscape', (base) => {
        const nextLandscape = fn(base.landscape);
        return nextOverrides(base, { landscape: nextLandscape ?? null });
      });
    },
    [commitDraft],
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
    setMultiSel(new Set());
  }, [updateLandscape]);

  const setPlacementRotation = useCallback(
    (tid: string, index: number, deg: number) => {
      setPlacementAt(tid, index, { r: landscapeRotation(deg) });
    },
    [setPlacementAt],
  );

  const setBulkRotation = useCallback(
    (deg: number) => {
      if (!multiSel.size) return;
      const next = landscapeRotation(deg);
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const cloned = new Set<string>();
        let changed = false;
        for (const key of multiSel) {
          const parsed = parsePlacementKey(key);
          if (!parsed) continue;
          const list = map[parsed.tid];
          const p = list?.[parsed.index];
          if (!p || (p.r ?? 0) === (next ?? 0)) continue;
          if (!cloned.has(parsed.tid)) {
            map[parsed.tid] = [...list!];
            cloned.add(parsed.tid);
          }
          const updated = { ...p };
          if (next == null) delete updated.r;
          else updated.r = next;
          map[parsed.tid]![parsed.index] = updated;
          changed = true;
        }
        return changed ? { territories: map } : prev;
      });
    },
    [multiSel, updateLandscape],
  );

  const nudgeBulkRotation = useCallback(
    (delta: number) => {
      if (!multiSel.size) return;
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const cloned = new Set<string>();
        let changed = false;
        for (const key of multiSel) {
          const parsed = parsePlacementKey(key);
          if (!parsed) continue;
          const list = map[parsed.tid];
          const p = list?.[parsed.index];
          if (!p) continue;
          const next = landscapeRotation((p.r ?? 0) + delta);
          if ((p.r ?? 0) === (next ?? 0)) continue;
          if (!cloned.has(parsed.tid)) {
            map[parsed.tid] = [...list!];
            cloned.add(parsed.tid);
          }
          const updated = { ...p };
          if (next == null) delete updated.r;
          else updated.r = next;
          map[parsed.tid]![parsed.index] = updated;
          changed = true;
        }
        return changed ? { territories: map } : prev;
      });
    },
    [multiSel, updateLandscape],
  );

  const setBulkScale = useCallback(
    (s: number) => {
      if (!multiSel.size) return;
      const clamped = Math.min(4, Math.max(0.25, s));
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const cloned = new Set<string>();
        let changed = false;
        for (const key of multiSel) {
          const parsed = parsePlacementKey(key);
          if (!parsed) continue;
          const list = map[parsed.tid];
          const p = list?.[parsed.index];
          if (!p || (p.s ?? 1) === clamped) continue;
          if (!cloned.has(parsed.tid)) {
            map[parsed.tid] = [...list!];
            cloned.add(parsed.tid);
          }
          map[parsed.tid]![parsed.index] = { ...p, s: clamped };
          changed = true;
        }
        return changed ? { territories: map } : prev;
      });
    },
    [multiSel, updateLandscape],
  );

  const deleteSelectedPlacements = useCallback(() => {
    if (!multiSel.size) return;
    updateLandscape((prev) => {
      const map = { ...(prev?.territories ?? {}) };
      const byTid = new Map<string, number[]>();
      for (const key of multiSel) {
        const parsed = parsePlacementKey(key);
        if (!parsed) continue;
        const arr = byTid.get(parsed.tid) ?? [];
        arr.push(parsed.index);
        byTid.set(parsed.tid, arr);
      }
      for (const [tid, indices] of byTid) {
        const list = map[tid];
        if (!list) continue;
        const next = [...list];
        // Splice highest-index-first so earlier indices stay valid.
        for (const index of [...indices].sort((a, b) => b - a)) {
          if (index >= 0 && index < next.length) next.splice(index, 1);
        }
        if (next.length) map[tid] = next;
        else delete map[tid];
      }
      return Object.keys(map).length ? { territories: map } : undefined;
    });
    setMultiSel(new Set());
    setSelectedPlacement(null);
  }, [multiSel, updateLandscape]);

  const autoGenerateLandscape = useCallback(() => {
    if (!paths.size) return;
    const landmarksOnly = artMode === 'landmarks';
    setAutoBusy(true);
    try {
      updateLandscape((prev) => {
        const map: Record<string, LandscapePlacement[]> = { ...(prev?.territories ?? {}) };
        // Replace only this pill's previous auto pass. The other set, and anything placed by hand, stays.
        for (const [tid, placements] of Object.entries(map)) {
          map[tid] = placements.filter((p) => {
            if (!p.auto) return true;
            const isLandmark = p.cat === LANDSCAPE_LANDMARK_CATEGORY;
            return landmarksOnly ? !isLandmark : isLandmark;
          });
          if (!map[tid].length) delete map[tid];
        }
        const seed = (Date.now() % 2147483647) || 1;
        let seedAcc = seed;
        const nextSeed = () => {
          seedAcc = (seedAcc * 1103515245 + 12345) % 2147483647;
          return seedAcc || 1;
        };
        let totalPlaced = Object.values(map).reduce((n, list) => n + list.length, 0);
        if (!landmarksOnly) {
          for (const tid of paths.keys()) {
            const defKey = resolveTid(terrainBackedDefs, tid) ?? tid;
            const def = terrainBackedDefs[defKey];
            const terrain =
              def && typeof def.terrain_type === 'string'
                ? def.terrain_type
                : def && typeof def.terrain === 'string'
                  ? def.terrain
                  : '';
            const cats = landscapeCategoriesForTerrain(terrain, manifest, { includeFree: false });
            if (!cats.length) continue;
            const pathEl = territoryPathEl(tid);
            if (!pathEl) continue;
            for (const cat of cats) {
              const images = manifest[cat] ?? [];
              if (!images.length) continue;
              const key = resolveTid(map, tid) ?? tid;
              const existing = map[key] ?? [];
              const room = Math.min(50 - existing.length, 2500 - totalPlaced);
              if (room <= 0) continue;
              const generated = autoGenerateForTerritory(pathEl, cat, images, nextSeed()).slice(0, room);
              if (generated.length) {
                map[key] = [...existing, ...generated];
                totalPlaced += generated.length;
              }
              if (totalPlaced >= 2500) return Object.keys(map).length ? { territories: map } : undefined;
            }
          }
          return Object.keys(map).length ? { territories: map } : undefined;
        }
        const landmarks = manifest[LANDSCAPE_LANDMARK_CATEGORY] ?? [];
        for (const tid of paths.keys()) {
          if (totalPlaced >= 2500) break;
          const img = landmarkFileForTerritory(tid, landmarks);
          if (!img) continue;
          const key = resolveTid(map, tid) ?? tid;
          const existing = map[key] ?? [];
          if (existing.some((p) => p.img === img) || existing.length >= 50) continue;
          const pathEl = territoryPathEl(tid);
          const pt = pathEl ? pointInsideTerritory(pathEl) : null;
          if (!pt) continue;
          map[key] = [
            ...existing,
            {
              cat: LANDSCAPE_LANDMARK_CATEGORY,
              img,
              x: pt.x,
              y: pt.y,
              layer: 'interior',
              auto: true,
              s: 1.5,
            },
          ];
          totalPlaced += 1;
        }
        return Object.keys(map).length ? { territories: map } : undefined;
      });
    } finally {
      setAutoBusy(false);
    }
  }, [artMode, paths, terrainBackedDefs, manifest, updateLandscape]);

  const updateTerrain = useCallback(
    (tid: string, terrain: string) => {
      commitDraft('landscape', (base) => {
        const key = resolveTid(base.terrain, tid) ?? resolveTid(territoryDefs, tid) ?? tid;
        return nextOverrides(base, { terrain: { ...(base.terrain ?? {}), [key]: terrain } });
      });
    },
    [commitDraft, territoryDefs],
  );

  const terrainTypeFor = useCallback(
    (tid: string): string => {
      const defKey = resolveTid(terrainBackedDefs, tid) ?? tid;
      const def = terrainBackedDefs[defKey];
      if (!def) return '';
      return typeof def.terrain_type === 'string'
        ? def.terrain_type
        : typeof def.terrain === 'string'
          ? def.terrain
          : '';
    },
    [terrainBackedDefs],
  );

  const selectedTerrainType = useMemo(() => (selected ? terrainTypeFor(selected) : ''), [selected, terrainTypeFor]);

  const selectedCategories = useMemo(() => {
    const cats = landscapeCategoriesForTerrain(selectedTerrainType, manifest);
    // Border mountains are allowed on any land territory (decorative), independent of terrain type.
    if (borderMode && manifest.mountains && !cats.includes('mountains')) return [...cats, 'mountains'];
    return cats;
  }, [selectedTerrainType, manifest, borderMode]);

  const paletteCategories = useMemo(() => {
    if (artMode === 'landmarks') {
      return (manifest[LANDSCAPE_LANDMARK_CATEGORY]?.length ?? 0) > 0 ? [LANDSCAPE_LANDMARK_CATEGORY] : [];
    }
    return selectedCategories.filter((cat) => cat !== LANDSCAPE_LANDMARK_CATEGORY);
  }, [artMode, manifest, selectedCategories]);

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
      const snapBorder = artMode === 'landscape' && borderMode;
      if (snapBorder) {
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
        layer: snapBorder ? 'border' : 'interior',
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
    [placing, artMode, borderMode, viewportToViewBox, current, updateLandscape],
  );

  /**
   * Paint-mode stamp: fills a disk of `paintRadius` viewBox units (0 = a single
   * point) around the cursor. Candidates are tested against the territory under
   * the cursor only; border mode snaps them to the outline. Points closer than
   * PAINT_MIN_DIST to an existing piece are skipped so re-tracing never stacks.
   */
  const paintStampAt = useCallback(
    (clientX: number, clientY: number) => {
      if (!placing) return;
      const tid = territoryIdFromPoint(clientX, clientY);
      if (!tid) return;
      const pathEl = territoryPathEl(tid);
      if (!pathEl) return;
      const vb = viewportToViewBox(clientX, clientY);
      if (!vb) return;
      const radius = paintRadius;
      const snapBorder = artMode === 'landscape' && borderMode;
      const offsets: Pt[] = [{ x: 0, y: 0 }];
      if (radius > 0) {
        const step = Math.min(PAINT_MIN_DIST, radius);
        const n = Math.ceil(radius / step);
        for (let i = -n; i <= n; i += 1) {
          for (let j = -n; j <= n; j += 1) {
            if (i === 0 && j === 0) continue;
            const dx = i * step;
            const dy = j * step;
            if (dx * dx + dy * dy > radius * radius) continue;
            offsets.push({ x: dx, y: dy });
          }
        }
      }
      const accepted: Pt[] = [];
      for (const o of offsets) {
        let x = vb.x + o.x;
        let y = vb.y + o.y;
        if (snapBorder) {
          const snapped = nearestPointOnOutline(pathEl, x, y);
          if (!snapped) continue;
          // In brush mode only keep outline points within reach of the cursor.
          if (radius > 0 && Math.hypot(snapped.x - vb.x, snapped.y - vb.y) > radius) continue;
          x = snapped.x;
          y = snapped.y;
        } else if (!isPointInTerritoryPath(pathEl, x, y)) {
          continue;
        }
        accepted.push({ x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 });
      }
      if (!accepted.length) return;
      const cat = placing.cat;
      const img = placing.img;
      const layer = snapBorder ? ('border' as const) : ('interior' as const);
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const key = resolveTid(map, tid) ?? tid;
        const list = [...(map[key] ?? [])];
        const before = list.length;
        const minD2 = PAINT_MIN_DIST * PAINT_MIN_DIST;
        for (const p of accepted) {
          if (list.length >= 50) break;
          const tooClose = list.some((q) => (q.x - p.x) ** 2 + (q.y - p.y) ** 2 < minD2);
          if (tooClose) continue;
          list.push({ cat, img, x: p.x, y: p.y, layer });
        }
        if (list.length === before) return prev;
        map[key] = list;
        return { territories: map };
      });
      setSelected(tid);
      setSelectedPlacement(null);
    },
    [placing, artMode, paintRadius, borderMode, viewportToViewBox, updateLandscape],
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
      if (pt) positions[storageKindFor(tid, kind, terrainBackedDefs)] = pt;
    }
    updateEntry(tid, { focus: 'override', positions });
  };

  const setKindPosition = (tid: string, kind: UiKind, pt: MapOverridePoint) => {
    const existing = entryFor(current, tid);
    const positions = {
      ...(existing?.focus === 'override' ? existing.positions : {}),
      [storageKindFor(tid, kind, terrainBackedDefs)]: pt,
    };
    updateEntry(tid, { focus: 'override', positions });
  };

  const resetKind = (tid: string, kind: UiKind) => {
    const existing = entryFor(current, tid);
    if (existing?.focus !== 'override' || !existing.positions) return;
    const positions = { ...existing.positions };
    delete positions[storageKindFor(tid, kind, terrainBackedDefs)];
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
    armGesture('icon');
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
    endGesture();
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
    if (e.button !== 0) return; // middle/right must reach the viewport — middle-button pans the map
    if (editorTab === 'landscape' && cursorMode === 'paint') return; // paint owns left-drag on the viewport
    if (editorTab === 'landscape' && cursorMode === 'select') {
      e.stopPropagation();
      e.preventDefault();
      const key = placementKey(tid, index);
      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        setMultiSel((prev) => {
          const next = new Set(prev);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          return next;
        });
        return;
      }
      // Clicking a member drags the whole group; clicking a non-member selects just it.
      const inGroup = multiSel.has(key);
      const keys = inGroup ? Array.from(multiSel) : [key];
      if (!inGroup) setMultiSel(new Set([key]));
      const map = current?.landscape?.territories;
      const items: { tid: string; index: number; x: number; y: number; layer: 'interior' | 'border' }[] = [];
      for (const k of keys) {
        const parsed = parsePlacementKey(k);
        if (!parsed) continue;
        const p = map?.[parsed.tid]?.[parsed.index];
        if (!p) continue;
        items.push({ tid: parsed.tid, index: parsed.index, x: p.x, y: p.y, layer: p.layer });
      }
      if (!items.length) return;
      try {
        (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      armGesture('landscape');
      bulkDragRef.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, scale: transform.scale, items };
      setSelected(tid);
      return;
    }
    if (editorTab === 'landscape' && cursorMode !== 'navigate') return; // select/paint own left-drag
    if (placing) return; // placing armed: fall through — viewport places on click, pans on drag
    const placement = landscapeFor(tid)[index];
    if (!placement) return;
    e.stopPropagation();
    e.preventDefault();
    setSelected(tid);
    setSelectedPlacement({ tid, index });
    try {
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    armGesture('landscape');
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
    const bd = bulkDragRef.current;
    if (bd && e.pointerId === bd.id) {
      e.stopPropagation(); // the group drag owns this gesture
      if ((e.buttons & 1) === 0) {
        bulkDragRef.current = null; // button already released — drop a stranded drag
        return;
      }
      const el = wrapRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const startVb = {
        x: (bd.sx - rect.left - transform.x) / bd.scale,
        y: (bd.sy - rect.top - transform.y) / bd.scale,
      };
      const vb = viewportToViewBox(e.clientX, e.clientY);
      if (!vb) return;
      const dx = vb.x - startVb.x;
      const dy = vb.y - startVb.y;
      updateLandscape((prev) => {
        const map = { ...(prev?.territories ?? {}) };
        const cloned = new Set<string>();
        let changed = false;
        for (const item of bd.items) {
          const list = map[item.tid];
          const p = list?.[item.index];
          if (!p) continue;
          let x = item.x + dx;
          let y = item.y + dy;
          const pathEl = territoryPathEl(item.tid);
          if (item.layer === 'border') {
            if (pathEl) {
              const snapped = nearestPointOnOutline(pathEl, x, y);
              if (snapped) {
                x = snapped.x;
                y = snapped.y;
              }
            }
          } else if (pathEl && !isPointInTerritoryPath(pathEl, x, y)) {
            continue; // interior pieces stay inside their territory
          }
          x = Math.max(0, Math.min(viewBox.width, x));
          y = Math.max(0, Math.min(viewBox.height, y));
          if (!cloned.has(item.tid)) {
            map[item.tid] = [...list!];
            cloned.add(item.tid);
          }
          map[item.tid]![item.index] = { ...p, x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 };
          changed = true;
        }
        return changed ? { territories: map } : prev;
      });
      return;
    }
    const d = placeDragRef.current;
    if (!d || e.pointerId !== d.id) return; // not ours: let it bubble so pans keep working while the cursor crosses pieces
    e.stopPropagation(); // the piece drag owns this gesture
    if ((e.buttons & 1) === 0) {
      placeDragRef.current = null; // button already released — drop a stranded drag
      return;
    }
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
    const bd = bulkDragRef.current;
    if (bd && e.pointerId === bd.id) {
      e.stopPropagation();
      endGesture();
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      bulkDragRef.current = null;
      return;
    }
    const d = placeDragRef.current;
    if (!d || e.pointerId !== d.id) return; // not ours: let it bubble (viewport click/pan release)
    e.stopPropagation();
    endGesture();
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
      rows.push({ id, label: territoryLabel(id, territoryDefs) });
    }
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [paths, territoryDefs]);

  const filteredChoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    const type = terrainFilter;
    return territoryChoices.filter((row) => {
      if (type && (terrainTypeFor(row.id) || 'land') !== type) return false;
      if (!q) return true;
      return row.label.toLowerCase().includes(q) || row.id.toLowerCase().includes(q);
    });
  }, [territoryChoices, search, terrainFilter, terrainTypeFor]);

  const terrainFilterOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of territoryChoices) {
      const t = terrainTypeFor(row.id) || 'land';
      counts.set(t, (counts.get(t) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([type, count]) => ({ type, count }));
  }, [territoryChoices, terrainTypeFor]);

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
    // Stable order (insertion order): re-sorting by selection moved the pressed element
    // mid-drag, React re-keyed it, and the browser dropped our pointer capture — the
    // piece drag died a few pixels in. Overlap order comes from --active/--multi
    // z-index instead, so selected pieces still sit on top.
    return rows;
  }, [current]);

  const landscapeCount = useMemo(
    () => Object.values(current?.landscape?.territories ?? {}).reduce((n, list) => n + list.length, 0),
    [current],
  );

  // Slider value for the bulk scale control: first selected piece's scale (mixed sets just start there).
  const bulkScaleValue = useMemo(() => {
    for (const key of multiSel) {
      const parsed = parsePlacementKey(key);
      if (!parsed) continue;
      const p = current?.landscape?.territories?.[parsed.tid]?.[parsed.index];
      if (p) return p.s ?? 1;
    }
    return 1;
  }, [multiSel, current]);

  const bulkRotationValue = useMemo(() => {
    for (const key of multiSel) {
      const parsed = parsePlacementKey(key);
      if (!parsed) continue;
      const p = current?.landscape?.territories?.[parsed.tid]?.[parsed.index];
      if (p) return p.r ?? 0;
    }
    return 0;
  }, [multiSel, current]);

  // --- copy from another map ----------------------------------------------

  const otherMaps = useMemo(() => maps.filter((m) => m !== mapBase), [maps, mapBase]);

  const copyPlacements = useMemo(() => {
    const map = copyOverrides?.landscape?.territories;
    if (!map) return [];
    const rows: { tid: string; index: number; p: LandscapePlacement }[] = [];
    for (const [tid, list] of Object.entries(map)) {
      list.forEach((p, index) => rows.push({ tid, index, p }));
    }
    return rows;
  }, [copyOverrides]);

  const normCopyRegion = useMemo<RegionRect | null>(() => {
    if (!copyRegion) return null;
    return {
      x1: Math.min(copyRegion.x1, copyRegion.x2),
      y1: Math.min(copyRegion.y1, copyRegion.y2),
      x2: Math.max(copyRegion.x1, copyRegion.x2),
      y2: Math.max(copyRegion.y1, copyRegion.y2),
    };
  }, [copyRegion]);

  /** Position-based plan: scale each source piece into this map's viewBox and
   *  assign it to whichever target territory contains the point. Respects the
   *  50/territory and 2500/map caps against the current draft. */
  const copyPlan = useMemo<CopyPlan | null>(() => {
    if (copyLoading || copyErr || !copyOverrides || !copyViewBox.width || !viewBox.width) return null;
    if (!targetHitAreas.length) return null;
    const existing = current?.landscape?.territories ?? {};
    const counts: Record<string, number> = {};
    let total = 0;
    for (const [tid, list] of Object.entries(existing)) {
      counts[tid] = list.length;
      total += list.length;
    }
    const scaleX = viewBox.width / copyViewBox.width;
    const scaleY = viewBox.height / copyViewBox.height;
    const region = normCopyRegion;
    const items: CopyPlanItem[] = [];
    let inRegion = 0;
    let outsideRegion = 0;
    let unmatched = 0;
    let capped = 0;
    for (const { p } of copyPlacements) {
      if (region && (p.x < region.x1 || p.x > region.x2 || p.y < region.y1 || p.y > region.y2)) {
        outsideRegion += 1;
        continue;
      }
      inRegion += 1;
      const x = p.x * scaleX;
      const y = p.y * scaleY;
      if (x < 0 || y < 0 || x > viewBox.width || y > viewBox.height) {
        unmatched += 1;
        continue;
      }
      let hit: string | null = null;
      for (const area of targetHitAreas) {
        const b = area.bbox;
        if (x < b.x || y < b.y || x > b.x + b.width || y > b.y + b.height) continue;
        if (isPointInTerritoryPath(area.el, x, y)) {
          hit = area.tid;
          break;
        }
      }
      if (!hit) {
        unmatched += 1;
        continue;
      }
      const key = resolveTid(counts, hit) ?? hit;
      if ((counts[key] ?? 0) >= 50 || total >= 2500) {
        capped += 1;
        continue;
      }
      counts[key] = (counts[key] ?? 0) + 1;
      total += 1;
      const placement: LandscapePlacement = {
        cat: p.cat,
        img: p.img,
        x: Math.round(x * 100) / 100,
        y: Math.round(y * 100) / 100,
        layer: p.layer,
      };
      // Copied pieces are a deliberate import: keep them across the next auto pass.
      if (typeof p.s === 'number' && p.s !== 1) placement.s = Math.min(4, Math.max(0.25, p.s));
      const copiedRotation = landscapeRotation(p.r ?? 0);
      if (copiedRotation != null) placement.r = copiedRotation;
      items.push({ tid: key, placement });
    }
    return { items, inRegion, outsideRegion, unmatched, capped };
  }, [
    copyLoading,
    copyErr,
    copyOverrides,
    copyViewBox,
    viewBox,
    targetHitAreas,
    normCopyRegion,
    copyPlacements,
    current,
  ]);

  const copySummary = useMemo(() => {
    if (!otherMaps.length) return 'No other map to copy from.';
    if (copyLoading) return `Loading ${copySource}…`;
    if (copyErr) return copyErr;
    if (!copyPlan) return 'Select a source map to preview its landscape placements.';
    const bits = [`${copyPlan.items.length} of ${copyPlan.inRegion} pieces ready to copy`];
    if (copyPlan.outsideRegion) bits.push(`${copyPlan.outsideRegion} outside the region`);
    if (copyPlan.unmatched) bits.push(`${copyPlan.unmatched} land off this map`);
    if (copyPlan.capped) bits.push(`${copyPlan.capped} skipped by the 50-per-territory / 2500-per-map caps`);
    return bits.join(' · ');
  }, [otherMaps.length, copyLoading, copyErr, copySource, copyPlan]);

  const openCopyDialog = () => {
    setCopyErr(null);
    setCopyRegion(null);
    setCopyDrag(null);
    setCopySource(otherMaps[0] ?? '');
    setCopyOpen(true);
  };

  const applyCopy = () => {
    const plan = copyPlan;
    if (!plan || !plan.items.length) return;
    const map: Record<string, LandscapePlacement[]> = { ...(current?.landscape?.territories ?? {}) };
    const newKeys: string[] = [];
    let total = 0;
    for (const list of Object.values(map)) total += list.length;
    const byTid = new Map<string, CopyPlanItem[]>();
    for (const item of plan.items) {
      const arr = byTid.get(item.tid);
      if (arr) arr.push(item);
      else byTid.set(item.tid, [item]);
    }
    for (const [tid, items] of byTid) {
      const key = resolveTid(map, tid) ?? tid;
      const existingList = map[key] ?? [];
      const room = Math.min(50 - existingList.length, 2500 - total);
      const add = items.slice(0, Math.max(0, room));
      if (!add.length) continue;
      map[key] = [...existingList, ...add.map((it) => it.placement)];
      add.forEach((_, i) => newKeys.push(placementKey(key, existingList.length + i)));
      total += add.length;
    }
    updateLandscape(() => (Object.keys(map).length ? { territories: map } : undefined));
    if (newKeys.length) {
      // Select the import so it can be bulk-dragged/scaled immediately.
      setMultiSel(new Set(newKeys));
      setSelectedPlacement(null);
      setCursorMode('select');
      setBand(null);
      selectDragRef.current = null;
      bulkDragRef.current = null;
    }
    setCopyOpen(false);
  };

  const copyVbFromEvent = (e: ReactPointerEvent<SVGSVGElement>): Pt | null => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1 || !copyViewBox.width || !copyViewBox.height) return null;
    return {
      x: ((e.clientX - rect.left) / rect.width) * copyViewBox.width,
      y: ((e.clientY - rect.top) / rect.height) * copyViewBox.height,
    };
  };

  const onCopyPreviewDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    const vb = copyVbFromEvent(e);
    if (!vb) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    copyDragRef.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, x1: vb.x, y1: vb.y };
    setCopyDrag({ x1: vb.x, y1: vb.y, x2: vb.x, y2: vb.y });
  };

  const onCopyPreviewMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = copyDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    const vb = copyVbFromEvent(e);
    if (!vb) return;
    setCopyDrag({ x1: d.x1, y1: d.y1, x2: vb.x, y2: vb.y });
  };

  const onCopyPreviewUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = copyDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    try {
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    copyDragRef.current = null;
    const moved = Math.abs(e.clientX - d.sx) + Math.abs(e.clientY - d.sy) >= 4;
    const vb = copyVbFromEvent(e);
    setCopyDrag(null);
    // A bare click clears the region; a real drag commits it.
    if (!moved) setCopyRegion(null);
    else setCopyRegion({ x1: d.x1, y1: d.y1, x2: vb?.x ?? d.x1, y2: vb?.y ?? d.y1 });
  };

  const copyMarkerR = copyViewBox.width
    ? Math.max(6, Math.min(copyViewBox.width, copyViewBox.height) / 120)
    : 10;

  const copyMarkers = useMemo(
    () =>
      copyPlacements.map((row) => (
        <circle
          key={`${row.tid}|${row.index}`}
          className="mapov-copy__marker"
          cx={row.p.x}
          cy={row.p.y}
          r={copyMarkerR}
        />
      )),
    [copyPlacements, copyMarkerR],
  );

  const copyRegionMarkers = useMemo(() => {
    if (!normCopyRegion) return null;
    const r = normCopyRegion;
    return copyPlacements
      .filter((row) => row.p.x >= r.x1 && row.p.x <= r.x2 && row.p.y >= r.y1 && row.p.y <= r.y2)
      .map((row) => (
        <circle
          key={`${row.tid}|${row.index}`}
          className="mapov-copy__marker mapov-copy__marker--hit"
          cx={row.p.x}
          cy={row.p.y}
          r={copyMarkerR}
        />
      ));
  }, [copyPlacements, normCopyRegion, copyMarkerR]);

  const copyBand = copyDrag ?? normCopyRegion;

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
      const pt = positions[storageKindFor(selected, kind, terrainBackedDefs)];
      if (pt) out[kind] = pt;
    }
    return out;
  }, [selected, selectedEntry, terrainBackedDefs]);

  const landEdges = useMemo(
    () => (editorTab === 'graph' ? adjacencyEdges(graphShown, 'adjacent') : []),
    [editorTab, graphShown],
  );
  const aerialEdges = useMemo(
    () => (editorTab === 'graph' ? adjacencyEdges(graphShown, 'aerial_adjacent') : []),
    [editorTab, graphShown],
  );
  const fordEdges = useMemo(
    () => (editorTab === 'graph' ? adjacencyEdges(graphShown, 'ford_adjacent') : []),
    [editorTab, graphShown],
  );
  const selectedGraphKey = selected ? resolveTerritoryKey(graphShown, selected) : null;
  const neighborIds = (field: TerritoryEdgeField) =>
    selectedGraphKey ? stringList(graphShown[selectedGraphKey]?.[field]) : [];
  const landNeighborIds = neighborIds('adjacent');
  const aerialNeighborIds = neighborIds('aerial_adjacent');
  const fordNeighborIds = neighborIds('ford_adjacent');
  const isNeighbor = (tid: string, ids: string[]) => ids.some((id) => sameTerritoryId(id, tid));
  const neighborPathClass = (tid: string) => {
    if (editorTab !== 'graph') return '';
    const land = isNeighbor(tid, landNeighborIds);
    const aerial = isNeighbor(tid, aerialNeighborIds);
    const ford = isNeighbor(tid, fordNeighborIds);
    if (ford && aerial) return ' admin-graph__path--neighbor-ford';
    if (land && aerial) return ' admin-graph__path--neighbor-both';
    if (land) return ' admin-graph__path--neighbor-land';
    if (ford) return ' admin-graph__path--neighbor-ford';
    if (aerial) return ' admin-graph__path--neighbor-aerial';
    return '';
  };

  const description =
    editorTab === 'builder'
      ? 'Draw border lines once — neighbours share the same line. Set the map size, sketch with Pen/Pencil, then Label each enclosed region; Build SVG converts the graph into closed territory paths (saved work autosaves in this browser).'
      : editorTab === 'graph'
      ? 'Choose a territory, then click another to toggle that border. Land replaces aerial and ford. Aerial and ford can both stay. Save writes both sides onto this map and every setup that uses it.'
      : editorTab === 'landscape'
      ? 'Place terrain art constrained by each territory’s type, or switch to Landmarks for named places. Auto-generate replaces only the selected set. Save applies to every setup using this map.'
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
                  setCursorMode('navigate');
                  setBand(null);
                  setMultiSel(new Set());
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
              <button
                type="button"
                className={`admin-mapview__toggle-btn${editorTab === 'builder' ? ' admin-mapview__toggle-btn--active' : ''}`}
                aria-pressed={editorTab === 'builder'}
                onClick={() => {
                  setEditorTab('builder');
                  setPlacing(null);
                  setSelectedPlacement(null);
                  setCursorMode('navigate');
                  setBand(null);
                  setMultiSel(new Set());
                }}
              >
                Builder
              </button>
              <button
                type="button"
                className={`admin-mapview__toggle-btn${editorTab === 'graph' ? ' admin-mapview__toggle-btn--active' : ''}`}
                aria-pressed={editorTab === 'graph'}
                onClick={() => {
                  setEditorTab('graph');
                  setPlacing(null);
                  setSelectedPlacement(null);
                  setCursorMode('navigate');
                  setBand(null);
                  setMultiSel(new Set());
                  setEdgeField('adjacent');
                }}
              >
                Graph
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
            <MapTextureSlider />
          </div>
          <div className="admin-graph__header-actions">
            {editorTab === 'builder' ? null : (
              <>
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={undoTab}
                  disabled={past[editorTab].length === 0}
                  title="Ctrl+Z"
                >
                  Undo
                </button>
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={redoTab}
                  disabled={future[editorTab].length === 0}
                  title="Ctrl+Shift+Z"
                >
                  Redo
                </button>
              </>
            )}
            {editorTab === 'graph' ? (
              <div className="admin-graph__types" role="group" aria-label="Adjacency type">
                {EDGE_TYPES.map((type) => (
                  <button
                    key={type.field}
                    type="button"
                    className={`admin-graph__type admin-graph__type--${type.kind}${edgeField === type.field ? ' admin-graph__type--active' : ''}`}
                    aria-pressed={edgeField === type.field}
                    onClick={() => setEdgeField(type.field)}
                  >
                    {type.label}
                  </button>
                ))}
              </div>
            ) : null}
            {editorTab === 'landscape' ? (
              <>
                <div className="admin-mapview__toggle" role="group" aria-label="Cursor mode">
                  {(
                    [
                      ['navigate', 'Navigate'],
                      ['select', 'Select'],
                      ['paint', 'Paint'],
                    ] as const
                  ).map(([mode, label]) => (
                    <button
                      key={mode}
                      type="button"
                      className={`admin-mapview__toggle-btn${cursorMode === mode ? ' admin-mapview__toggle-btn--active' : ''}`}
                      aria-pressed={cursorMode === mode}
                      onClick={() => {
                        setCursorMode(mode);
                        setBand(null);
                        setMultiSel(new Set());
                        selectDragRef.current = null;
                        paintLastRef.current = null;
                        paintPointerRef.current = null;
                        bulkDragRef.current = null;
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="admin-page__btn"
                  disabled={autoBusy}
                  onClick={autoGenerateLandscape}
                  title={
                    artMode === 'landmarks'
                      ? 'Place each landmark on its territory. Terrain art and hand-placed pieces stay.'
                      : 'Fill terrain art. Landmarks and hand-placed pieces stay.'
                  }
                >
                  {autoBusy ? 'Generating…' : 'Auto-generate'}
                </button>
                <button
                  type="button"
                  className="admin-page__btn"
                  disabled={otherMaps.length === 0}
                  onClick={openCopyDialog}
                  title="Copy landscape placements from another map at the same positions (optionally limited to a region)"
                >
                  Copy from…
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  disabled={landscapeCount === 0}
                  onClick={() => setClearConfirmOpen(true)}
                >
                  Clear all
                </button>
              </>
            ) : null}
            {editorTab === 'builder' ? null : (
              <button
                type="button"
                className={`admin-page__btn${dirty ? ' admin-page__btn--primary' : ''}`}
                disabled={saving || !dirty}
                onClick={() => void save()}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            )}
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
          ) : editorTab === 'graph' ? (
            <ul className="admin-graph__key" aria-label="Edge colors">
              <li>
                <span className="admin-graph__key-swatch admin-graph__key-swatch--land" aria-hidden />
                Adjacent
              </li>
              <li>
                <span className="admin-graph__key-swatch admin-graph__key-swatch--aerial" aria-hidden />
                Aerial adjacent
              </li>
              <li>
                <span className="admin-graph__key-swatch admin-graph__key-swatch--ford" aria-hidden />
                Ford adjacent
              </li>
            </ul>
          ) : editorTab === 'builder' ? (
            <p className="admin-form__micro mapov-landscape-hint">
              Each border is stored once and shared by both neighbouring regions — no more editing two territory
              paths in sync. Outer frame borders are created from the map size; ends snap onto joints or split
              borders they land on so strokes join the shared graph.
            </p>
          ) : (
            <p className="admin-form__micro mapov-landscape-hint">
              {cursorMode === 'select'
                ? 'Select mode: drag a rectangle to select pieces · shift adds · ctrl removes · drag a selected piece to move the group · Esc clears'
                : cursorMode === 'paint'
                  ? placing
                    ? `Paint mode: drag to stamp ${placing.img}${paintRadius ? ` in a ${paintRadius}-unit brush` : ' along the drag'}${borderMode ? ' on borders' : ' inside territories'} · pick another image below to switch`
                    : 'Paint mode: pick an image below, then drag on the map to stamp.'
                  : placing
                    ? `Click the map to place ${placing.img}${borderMode ? ' on the territory border' : ' inside the territory'} · Esc to cancel`
                    : 'Pick an image below, then click the map. Drag existing art to move it.'}
              {landscapeCount ? ` · ${landscapeCount} placed` : ''}
            </p>
          )}
        </div>
        {loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {err ? <p className="admin-page__error">{err}</p> : null}
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
        {!loading && !err && editorTab === 'builder' ? (
          <div className="admin-graph__body">
            <MapBuilder />
          </div>
        ) : !loading && !err ? (
          <div className="admin-graph__body">
            <div className="admin-graph__side mapov-side">
              {editorTab === 'landscape' ? (
                <div className="mapov-art">
                  <div className="admin-mapview__toggle" role="group" aria-label="Art to place">
                    <button
                      type="button"
                      className={`admin-mapview__toggle-btn${artMode === 'landscape' ? ' admin-mapview__toggle-btn--active' : ''}`}
                      aria-pressed={artMode === 'landscape'}
                      onClick={() => {
                        setArtMode('landscape');
                        setPlacing((current) => (current?.cat === LANDSCAPE_LANDMARK_CATEGORY ? null : current));
                      }}
                    >
                      Landscape
                    </button>
                    <button
                      type="button"
                      className={`admin-mapview__toggle-btn${artMode === 'landmarks' ? ' admin-mapview__toggle-btn--active' : ''}`}
                      aria-pressed={artMode === 'landmarks'}
                      onClick={() => {
                        setArtMode('landmarks');
                        setPlacing((current) => (current?.cat === LANDSCAPE_LANDMARK_CATEGORY ? current : null));
                      }}
                    >
                      Landmarks
                    </button>
                  </div>
                  {artMode === 'landscape' ? (
                    <label className="admin-page__checkbox-label">
                      <input
                        type="checkbox"
                        checked={borderMode}
                        onChange={(e) => setBorderMode(e.target.checked)}
                      />
                      Border mode (snap to outline)
                    </label>
                  ) : null}
                  <div className="mapov-art__images">
                    {paletteCategories.length ? (
                      paletteCategories.map((cat) => (
                        <div key={cat} className="mapov-landscape__group">
                          {artMode === 'landscape' ? (
                            <p className="mapov-landscape__group-label">{prettifyId(cat)}</p>
                          ) : null}
                          <div className="mapov-landscape__palette" role="listbox" aria-label={prettifyId(cat)}>
                            {(manifest[cat] ?? []).map((img) => {
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
                            })}
                          </div>
                        </div>
                      ))
                    ) : (
                      <p className="admin-form__micro">
                        {artMode === 'landmarks'
                          ? 'No landmark images.'
                          : selected
                            ? `No landscape art for this terrain type (${selectedTerrainType || 'land'}).${
                                borderMode && manifest.mountains
                                  ? ' Border mountains are still available from the palette.'
                                  : ' Enable Border mode to place mountain ranges on this territory’s edges.'
                              }`
                            : 'Select a territory to see its landscape images.'}
                      </p>
                    )}
                  </div>
                </div>
              ) : null}
              <input
                type="search"
                className="admin-form__input admin-graph__search"
                placeholder="Search territories"
                aria-label="Search territories"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <select
                className="admin-page__select admin-graph__filter"
                aria-label="Filter by territory type"
                value={terrainFilter}
                onChange={(e) => setTerrainFilter(e.target.value)}
              >
                <option value="">All territory types</option>
                {terrainFilterOptions.map((opt) => (
                  <option key={opt.type} value={opt.type}>
                    {prettifyId(opt.type)} ({opt.count})
                  </option>
                ))}
              </select>
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
                    <strong title={selected}>{territoryLabel(selected, territoryDefs)}</strong>
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
                    ) : editorTab === 'landscape' ? (
                      <select
                        className="admin-page__select mapov-inspector__terrain"
                        aria-label="Terrain type"
                        value={selectedTerrainType}
                        onChange={(e) => updateTerrain(selected, e.target.value)}
                      >
                        {selectedTerrainType ? null : <option value="">Select terrain</option>}
                        {selectedTerrainType && !terrainTypes.includes(selectedTerrainType) ? (
                          <option value={selectedTerrainType}>{prettifyId(selectedTerrainType)}</option>
                        ) : null}
                        {terrainTypes.map((type) => (
                          <option key={type} value={type}>
                            {prettifyId(type)}
                          </option>
                        ))}
                      </select>
                    ) : null}
                  </div>
                  {editorTab === 'graph' ? (
                    <p className="admin-form__micro">
                      {EDGE_TYPES.find((type) => type.field === edgeField)?.label ?? 'Land'}:{' '}
                      {(edgeField === 'adjacent'
                        ? landNeighborIds
                        : edgeField === 'aerial_adjacent'
                          ? aerialNeighborIds
                          : fordNeighborIds
                      ).join(', ') || 'none'}
                    </p>
                  ) : editorTab === 'landscape' ? (
                        <ul className="mapov-kinds">
                          {selectedPlacements.map((p, index) => {
                            const isActive = selectedPlacement?.tid === selected && selectedPlacement?.index === index;
                            return (
                              <li
                                key={`${p.img}-${index}`}
                                className={`mapov-kind mapov-kind--placement${isActive ? ' mapov-kind--active' : ''}`}
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
                                  {p.r ? ` · ${Math.round(p.r)}°` : ''}
                                </span>
                                <label className="mapov-landscape__rotate" title="Rotation">
                                  <span className="visually-hidden">Rotation</span>
                                  <button
                                    type="button"
                                    className="mapov-landscape__rot-btn"
                                    aria-label="Rotate left"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setPlacementRotation(selected, index, (p.r ?? 0) - 15);
                                    }}
                                  >
                                    ↺
                                  </button>
                                  <input
                                    type="range"
                                    min={0}
                                    max={359}
                                    step={1}
                                    value={Math.round(p.r ?? 0) % 360}
                                    aria-valuetext={`${Math.round(p.r ?? 0)} degrees`}
                                    onPointerDown={(e) => {
                                      e.stopPropagation();
                                      armGesture('landscape');
                                    }}
                                    onClick={(e) => e.stopPropagation()}
                                    onChange={(e) => setPlacementRotation(selected, index, Number(e.target.value))}
                                  />
                                  <button
                                    type="button"
                                    className="mapov-landscape__rot-btn"
                                    aria-label="Rotate right"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setPlacementRotation(selected, index, (p.r ?? 0) + 15);
                                    }}
                                  >
                                    ↻
                                  </button>
                                </label>
                                <label className="mapov-landscape__scale" title="Scale">
                                  <span className="visually-hidden">Scale</span>
                                  <input
                                    type="range"
                                    min={0.25}
                                    max={4}
                                    step={0.05}
                                    value={p.s ?? 1}
                                    onPointerDown={(e) => {
                                      e.stopPropagation();
                                      armGesture('landscape');
                                    }}
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
              className={`admin-graph__viewport${placing ? ' admin-graph__viewport--placing' : ''}${
                editorTab === 'landscape' && cursorMode === 'select'
                  ? ' admin-graph__viewport--select'
                  : ''
              }${editorTab === 'landscape' && cursorMode === 'paint' ? ' admin-graph__viewport--paint' : ''}`}
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
                      className={`admin-graph__path${selected && sameTerritoryId(tid, selected) ? ' admin-graph__path--selected' : ''}${neighborPathClass(tid)}`}
                    />
                  ))}
                  {editorTab === 'graph'
                    ? landEdges.map(({ a, b }) => {
                        const pa = lookupCentroid(centroids, a);
                        const pb = lookupCentroid(centroids, b);
                        if (!pa || !pb) return null;
                        const hot =
                          selectedGraphKey != null &&
                          (sameTerritoryId(a, selectedGraphKey) || sameTerritoryId(b, selectedGraphKey));
                        return (
                          <line
                            key={`land|${a}|${b}`}
                            x1={pa.x}
                            y1={pa.y}
                            x2={pb.x}
                            y2={pb.y}
                            className={`admin-graph__edge admin-graph__edge--land${hot ? ' admin-graph__edge--hot' : ''}`}
                            pointerEvents="none"
                          />
                        );
                      })
                    : null}
                  {editorTab === 'graph'
                    ? aerialEdges.map(({ a, b }) => {
                        if (
                          fordEdges.some(
                            (edge) =>
                              (sameTerritoryId(edge.a, a) && sameTerritoryId(edge.b, b)) ||
                              (sameTerritoryId(edge.a, b) && sameTerritoryId(edge.b, a)),
                          )
                        ) {
                          return null;
                        }
                        const pa = lookupCentroid(centroids, a);
                        const pb = lookupCentroid(centroids, b);
                        if (!pa || !pb) return null;
                        const pts = offsetEdge(pa, pb, 7);
                        const hot =
                          selectedGraphKey != null &&
                          (sameTerritoryId(a, selectedGraphKey) || sameTerritoryId(b, selectedGraphKey));
                        return (
                          <line
                            key={`aerial|${a}|${b}`}
                            x1={pts.a.x}
                            y1={pts.a.y}
                            x2={pts.b.x}
                            y2={pts.b.y}
                            className={`admin-graph__edge admin-graph__edge--aerial${hot ? ' admin-graph__edge--hot' : ''}`}
                            pointerEvents="none"
                          />
                        );
                      })
                    : null}
                  {editorTab === 'graph'
                    ? fordEdges.map(({ a, b }) => {
                        const pa = lookupCentroid(centroids, a);
                        const pb = lookupCentroid(centroids, b);
                        if (!pa || !pb) return null;
                        const pts = offsetEdge(pa, pb, -7);
                        const hot =
                          selectedGraphKey != null &&
                          (sameTerritoryId(a, selectedGraphKey) || sameTerritoryId(b, selectedGraphKey));
                        return (
                          <line
                            key={`ford|${a}|${b}`}
                            x1={pts.a.x}
                            y1={pts.a.y}
                            x2={pts.b.x}
                            y2={pts.b.y}
                            className={`admin-graph__edge admin-graph__edge--ford${hot ? ' admin-graph__edge--hot' : ''}`}
                            pointerEvents="none"
                          />
                        );
                      })
                    : null}
                </svg>
              </div>
              {band ? (
                <div
                  className="mapov-band"
                  style={{ left: band.left, top: band.top, width: band.width, height: band.height }}
                  aria-hidden
                />
              ) : null}
              {editorTab === 'landscape' && cursorMode === 'select' && multiSel.size > 0 ? (
                <div
                  className="mapov-bulk"
                  role="toolbar"
                  aria-label="Selection actions"
                  onPointerDown={(e) => e.stopPropagation()}
                >
                  <span className="mapov-bulk__count">{multiSel.size} selected</span>
                  <label className="mapov-bulk__scale" title="Scale all selected pieces">
                    Scale
                    <input
                      type="range"
                      min={0.25}
                      max={4}
                      step={0.05}
                      value={bulkScaleValue}
                      onPointerDown={() => armGesture('landscape')}
                      onChange={(e) => setBulkScale(Number(e.target.value))}
                    />
                  </label>
                  <label className="mapov-bulk__scale" title="Rotate all selected pieces">
                    Rotate
                    <button type="button" className="mapov-landscape__rot-btn" aria-label="Rotate selection left" onClick={() => nudgeBulkRotation(-15)}>
                      ↺
                    </button>
                    <input
                      type="range"
                      min={0}
                      max={359}
                      step={1}
                      value={Math.round(bulkRotationValue) % 360}
                      onPointerDown={() => armGesture('landscape')}
                      onChange={(e) => setBulkRotation(Number(e.target.value))}
                    />
                    <button type="button" className="mapov-landscape__rot-btn" aria-label="Rotate selection right" onClick={() => nudgeBulkRotation(15)}>
                      ↻
                    </button>
                  </label>
                  <button
                    type="button"
                    className="admin-page__btn admin-page__btn--danger"
                    onClick={deleteSelectedPlacements}
                  >
                    Delete
                  </button>
                  <button type="button" className="admin-page__btn" onClick={() => setMultiSel(new Set())}>
                    Clear
                  </button>
                </div>
              ) : null}
              {editorTab === 'landscape' && cursorMode === 'paint' ? (
                <div
                  className="mapov-brush"
                  role="toolbar"
                  aria-label="Paint brush"
                  onPointerDown={(e) => e.stopPropagation()}
                >
                  {placing ? (
                    <img
                      className="mapov-brush__thumb"
                      src={landscapeSrc(placing)}
                      alt={placing.img}
                      title={`${placing.cat} · ${placing.img}`}
                    />
                  ) : (
                    <span className="mapov-brush__none">No image armed</span>
                  )}
                  <label
                    className="mapov-brush__scale"
                    title="Brush radius in map units (0 = stamp a single line along the drag)"
                  >
                    Thickness
                    <input
                      type="range"
                      min={0}
                      max={200}
                      step={10}
                      value={paintRadius}
                      onChange={(e) => setPaintRadius(Number(e.target.value))}
                    />
                  </label>
                  <span className="mapov-brush__value">{paintRadius ? String(paintRadius) : 'line'}</span>
                </div>
              ) : null}
              {editorTab === 'landscape' ? (
                <div className="mapov-dots mapov-landscape-markers">
                  {landscapeMarkers.map((row) => {
                    const isActive =
                      selectedPlacement?.tid === row.tid && selectedPlacement?.index === row.index;
                    const isMulti = multiSel.has(placementKey(row.tid, row.index));
                    const x = transform.x + row.placement.x * transform.scale;
                    const y = transform.y + row.placement.y * transform.scale;
                    const size = Math.max(18, 36 * (row.placement.s ?? 1) * Math.min(transform.scale, 1.4));
                    const rotation = row.placement.r
                      ? `translate(-50%, -50%) rotate(${row.placement.r}deg)`
                      : undefined;
                    return (
                      <div
                        key={`${row.tid}|${row.index}|${row.placement.img}`}
                        role="button"
                        tabIndex={0}
                        aria-label={`${territoryLabel(row.tid, territoryDefs)} ${row.placement.img}`}
                        className={`mapov-placement${isActive ? ' mapov-placement--active' : ''}${isMulti ? ' mapov-placement--multi' : ''}${row.placement.layer === 'border' ? ' mapov-placement--border' : ''}${row.placement.auto ? ' mapov-placement--auto' : ''}`}
                        style={{ left: x, top: y, width: size, height: size, transform: rotation }}
                        title={`${territoryLabel(row.tid, territoryDefs)} · ${row.placement.img} · ${row.placement.layer}${placing ? '' : ' — drag to move'}`}
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
              ) : editorTab === 'graph' ? null : (
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
                        aria-label={`${territoryLabel(d.tid, territoryDefs)} ${KIND_LABEL[d.kind]}`}
                        className={`mapov-dot mapov-dot--${d.kind}${d.isOverride ? ' mapov-dot--override' : ''}${isActive ? ' mapov-dot--active' : ''}`}
                        style={{ left: x, top: y, background: KIND_COLOR[d.kind] }}
                        title={`${territoryLabel(d.tid, territoryDefs)} · ${KIND_LABEL[d.kind]} · ${d.isOverride ? 'override' : 'calculated'}${selectedFocus === 'override' && d.tid === selected ? ' — drag to move' : ''}`}
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
          {editorTab === 'builder'
            ? null
            : editorTab === 'landscape'
            ? selected
              ? `${territoryLabel(selected, territoryDefs)} · ${selectedTerrainType || 'land'} · ${selectedPlacements.length} placed${landscapeCount ? ` · ${landscapeCount} total` : ''}`
              : 'No territory selected'
            : editorTab === 'graph'
            ? selectedGraphKey
              ? `${territoryLabel(selectedGraphKey, graphShown)} · ${EDGE_TYPES.find((type) => type.field === edgeField)?.label ?? 'Land'}: ${(edgeField === 'adjacent' ? landNeighborIds : edgeField === 'aerial_adjacent' ? aerialNeighborIds : fordNeighborIds).join(', ') || 'none'}`
              : 'Choose a territory'
            : selected
              ? `${territoryLabel(selected, territoryDefs)} · ${KIND_LABEL[activeKind]}${
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
        {copyOpen ? (
          <div
            className="admin-modal-overlay admin-graph__confirm"
            role="presentation"
            onClick={() => setCopyOpen(false)}
          >
            <div
              className="admin-modal mapov-copy"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-mapov-copy-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-mapov-copy-title" className="admin-modal__title">
                Copy landscape from another map
              </h2>
              <div className="mapov-copy__source">
                <label className="admin-form__label" htmlFor="mapov-copy-source">
                  Source map
                </label>
                <select
                  id="mapov-copy-source"
                  className="admin-page__select"
                  value={copySource}
                  onChange={(e) => setCopySource(e.target.value)}
                  disabled={copyLoading || otherMaps.length === 0}
                >
                  {otherMaps.length === 0 ? <option value="">No other maps</option> : null}
                  {otherMaps.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
              <div className="mapov-copy__preview">
                {copyLoading ? (
                  <p className="admin-form__micro mapov-copy__note">Loading {copySource}…</p>
                ) : copyErr ? (
                  <p className="admin-page__error mapov-copy__note">{copyErr}</p>
                ) : copyPaths.size ? (
                  <svg
                    className="mapov-copy__svg"
                    viewBox={`0 0 ${copyViewBox.width} ${copyViewBox.height}`}
                    style={{ aspectRatio: `${copyViewBox.width} / ${copyViewBox.height}` }}
                    onPointerDown={onCopyPreviewDown}
                    onPointerMove={onCopyPreviewMove}
                    onPointerUp={onCopyPreviewUp}
                    onPointerCancel={onCopyPreviewUp}
                  >
                    {Array.from(copyPaths.entries()).map(([tid, pd]) => (
                      <path key={tid} className="mapov-copy__path" d={pd.d} transform={pd.transform} />
                    ))}
                    <g>{copyMarkers}</g>
                    {copyRegionMarkers ? <g>{copyRegionMarkers}</g> : null}
                    {copyBand ? (
                      <rect
                        className="mapov-copy__band"
                        x={Math.min(copyBand.x1, copyBand.x2)}
                        y={Math.min(copyBand.y1, copyBand.y2)}
                        width={Math.abs(copyBand.x2 - copyBand.x1)}
                        height={Math.abs(copyBand.y2 - copyBand.y1)}
                      />
                    ) : null}
                  </svg>
                ) : (
                  <p className="admin-form__micro mapov-copy__note">
                    No other map available to copy from.
                  </p>
                )}
              </div>
              <p className="admin-form__micro mapov-copy__hint">
                {copyPaths.size && !copyLoading
                  ? 'Drag on the preview to copy only that area; click it to clear the region. Pieces are placed at the same position on this map.'
                  : ''}
              </p>
              <p className="mapov-copy__summary">{copySummary}</p>
              <div className="admin-modal__actions">
                <button type="button" className="admin-page__btn" onClick={() => setCopyOpen(false)}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--primary"
                  disabled={!copyPlan?.items.length}
                  onClick={applyCopy}
                >
                  {copyPlan?.items.length ? `Copy ${copyPlan.items.length} pieces` : 'Copy'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
        {clearConfirmOpen ? (
          <div
            className="admin-modal-overlay admin-graph__confirm"
            role="presentation"
            onClick={() => setClearConfirmOpen(false)}
          >
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-mapov-clear-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-mapov-clear-title" className="admin-modal__title">
                Clear all landscape?
              </h2>
              <p className="admin-form__micro">
                This removes every landscape placement on this map ({landscapeCount} placed).
              </p>
              <div className="admin-modal__actions">
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  onClick={() => {
                    setClearConfirmOpen(false);
                    clearAllLandscape();
                  }}
                >
                  Yes
                </button>
                <button type="button" className="admin-page__btn" onClick={() => setClearConfirmOpen(false)}>
                  Cancel
                </button>
              </div>
            </div>
          </div>
        ) : null}
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
