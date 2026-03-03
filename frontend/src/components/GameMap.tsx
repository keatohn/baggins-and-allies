import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { DndContext, useDroppable, pointerWithin, useSensors, useSensor, PointerSensor } from '@dnd-kit/core';
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import type { GameState, SelectedUnit, MapTransform, PendingMove } from '../types/game';
import DraggableUnit from './DraggableUnit';
import DragOverlay from './DragOverlay';
import MobilizationTray from './MobilizationTray';
import './GameMap.css';

export interface PendingMoveConfirm {
  fromTerritory: string;
  toTerritory: string;
  unitId: string;
  unitDef?: { name: string; icon: string };
  maxCount: number;
  count: number;
  /** Cavalry charging: empty enemy territory IDs to conquer (order). */
  chargeThrough?: string[];
  /** When multiple charge paths exist, list of options (each option = array of territory IDs). User must pick one before confirming. */
  chargePathOptions?: string[][];
}

// Backend-provided movement data
interface MoveableUnit {
  territory: string;
  unit: {
    instance_id: string;
    unit_id: string;
    remaining_movement: number;
  };
  destinations: string[];
  /** Cavalry: destination_id -> list of charge_through paths. */
  charge_routes?: Record<string, string[][]>;
}

interface GameMapProps {
  gameState: GameState;
  selectedTerritory: string | null;
  selectedUnit: SelectedUnit | null;
  territoryData: Record<string, {
    name: string;
    owner?: string;
    terrain: string;
    stronghold: boolean;
    produces: number;
    adjacent: string[];
    hasCamp?: boolean;
    isCapital?: boolean;
    ownable?: boolean;
  }>;
  territoryUnits: Record<string, { unit_id: string; count: number; instances?: string[] }[]>;
  unitDefs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[] }>;
  unitStats: Record<string, { movement: number }>;
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string; capital?: string }>;
  onTerritorySelect: (territoryId: string | null) => void;
  onUnitSelect: (unit: SelectedUnit | null) => void;
  onUnitMove: (from: string, to: string, unitType: string, count: number) => void;
  isMovementPhase: boolean;
  isCombatMove: boolean;
  isMobilizePhase: boolean;
  hasMobilizationSelected: boolean;
  validMobilizeTerritories?: string[];
  /** Per-territory remaining mobilization capacity (power minus pending). Used to only highlight territories that have room. */
  remainingMobilizationCapacity?: Record<string, number>;
  onMobilizationDrop?: (territoryId: string, unitId: string, unitName: string, unitIcon: string, count: number) => void;
  onCampDrop?: (campIndex: number, territoryId: string) => void;
  mobilizationTray?: {
    purchases: { unitId: string; name: string; icon: string; count: number }[];
    pendingCamps: { campIndex: number; options?: string[] }[];
    factionColor: string;
    selectedUnitId: string | null;
    selectedCampIndex: number | null;
    onSelectUnit: (unitId: string | null) => void;
    onSelectCamp: (campIndex: number | null) => void;
  } | null;
  /** When placing a camp, these territories are valid targets (highlighted). */
  validCampTerritories?: string[];
  /** Territory IDs that already have a pending camp placement (exclude from camp drop targets). */
  territoriesWithPendingCampPlacement?: string[];
  pendingMoveConfirm: PendingMoveConfirm | null;
  onSetPendingMove: (pending: PendingMoveConfirm | null) => void;
  pendingMoves: PendingMove[];
  highlightedTerritories?: string[];
  availableMoveTargets?: MoveableUnit[];
  /** Aerial units that must move to friendly territory (from backend). Show caution icon on these. */
  aerialUnitsMustMove?: { territory_id: string; unit_id: string; instance_id: string }[];
}

const MAX_SCALE = 3;

/**
 * Map base name (no extension) -> viewBox and display dimensions.
 * Same base name in public/: <base>.png = background image, <base>.svg = territory borders only.
 *
 * For 50–80 territories with readable unit icons:
 * - Use a large canvas (e.g. 2500×1900 to 3500×2600) so each territory has enough
 *   room for unit stacks and markers. Users pan/zoom to their region.
 * - Keep viewBox and dimensions equal so 1 SVG unit = 1 display pixel at scale 1.
 * - In Inkscape: set document size to these dimensions, draw paths, set each path's
 *   label (or id) to the backend territory id so the app can match them.
 */
const MAP_CONFIG: Record<string, { viewBox: { width: number; height: number }; dimensions: { width: number; height: number } }> = {
  'test_map': { viewBox: { width: 1226.6667, height: 1013.3333 }, dimensions: { width: 1840, height: 1520 } },
  'baggins_and_allies_map_0.1': { viewBox: { width: 1303.07, height: 980.47 }, dimensions: { width: 1303.07, height: 980.47 } },
  'baggins_and_allies_map_1.0': { viewBox: { width: 3500, height: 2600 }, dimensions: { width: 3500, height: 2600 } },
};
const DEFAULT_MAP_BASE = 'test_map';

/** Semi-circle centroids in viewBox coords. Each path is half a circle; arc center after transform is shared, so we offset by 4r/(3π) along the bisector so east = right half, west = left half. r=90, 4*90/(3*π) ≈ 38.197. */
const OSGILIATH_OFFSET = (4 * 90) / (3 * Math.PI);
const OSGILIATH_CENTROIDS: Record<string, { x: number; y: number }> = {
  east_osgiliath: { x: 2218.6035 + OSGILIATH_OFFSET, y: 1761.675 },
  west_osgiliath: { x: 2217.97 - OSGILIATH_OFFSET, y: 1761.089 },
};

function toMapBase(name: string | null | undefined): string {
  if (!name || !name.trim()) return DEFAULT_MAP_BASE;
  const s = name.trim().replace(/\.(svg|png)$/i, '');
  return s || DEFAULT_MAP_BASE;
}

function getMapConfig(mapBase: string) {
  return MAP_CONFIG[mapBase] ?? MAP_CONFIG[DEFAULT_MAP_BASE];
}

export type TerritoryPathData = { d: string; transform?: string };

// Droppable territory component (used when we render path overlay)
function DroppableTerritory({
  territoryId,
  pathData,
  color,
  isSelected,
  isHighlighted,
  isValidDrop,
  onClick,
}: {
  territoryId: string;
  pathData: TerritoryPathData;
  color: string;
  isSelected: boolean;
  isHighlighted: boolean;
  isValidDrop: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `territory-${territoryId}`,
    data: { territoryId },
  });

  return (
    <path
      ref={setNodeRef as React.Ref<SVGPathElement>}
      id={`territory-${territoryId}`}
      d={pathData.d}
      transform={pathData.transform}
      fill={color}
      className={`territory-path ${isSelected ? 'selected' : ''} ${isHighlighted || isValidDrop ? 'highlight' : ''} ${isOver && isValidDrop ? 'drop-target' : ''}`}
      onClick={onClick}
    />
  );
}

function GameMap({
  gameState,
  selectedTerritory,
  selectedUnit,
  territoryData,
  territoryUnits,
  unitDefs,
  unitStats,
  factionData,
  onTerritorySelect,
  onUnitSelect,
  onUnitMove: _onUnitMove,
  isMovementPhase,
  isCombatMove,
  isMobilizePhase,
  hasMobilizationSelected,
  validMobilizeTerritories = [],
  remainingMobilizationCapacity = {},
  onMobilizationDrop,
  onCampDrop,
  mobilizationTray,
  pendingMoveConfirm: _pendingMoveConfirm,
  onSetPendingMove,
  pendingMoves,
  highlightedTerritories = [],
  validCampTerritories = [],
  territoriesWithPendingCampPlacement = [],
  availableMoveTargets,
  aerialUnitsMustMove = [],
}: GameMapProps) {
  const aerialMustMoveKeySet = useMemo(
    () => new Set((aerialUnitsMustMove ?? []).map(u => `${u.territory_id}_${u.unit_id}`)),
    [aerialUnitsMustMove]
  );
  const wrapperRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [svgPaths, setSvgPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [loadedSvgDimensions, setLoadedSvgDimensions] = useState<{
    viewBox: { width: number; height: number };
    dimensions: { width: number; height: number };
  } | null>(null);
  // Start slightly zoomed out so we're not stuck on top-left before fit effect runs
  const [transform, setTransform] = useState<MapTransform>(() => ({ x: 0, y: 0, scale: 0.25 }));
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const panStartPos = useRef({ x: 0, y: 0 });
  const transformRef = useRef(transform);
  const imgDimensionsRef = useRef({ width: 0, height: 0 });
  const PAN_CLICK_THRESHOLD_PX = 5;

  useEffect(() => {
    transformRef.current = transform;
  }, [transform]);
  const [territoryCentroids, setTerritoryCentroids] = useState<Record<string, { x: number; y: number }>>({});
  const [validDropTargets, setValidDropTargets] = useState<Set<string>>(new Set());
  const [activeUnit, setActiveUnit] = useState<{
    unitId: string;
    territoryId: string;
    count: number;
    unitDef?: { name: string; icon: string };
    factionColor?: string;
  } | null>(null);
  const [activeDragId, setActiveDragId] = useState<string | null>(null);

  // Require 10px movement before starting drag so a simple click selects instead of hiding the item
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 10 } })
  );

  const mapBase = toMapBase(gameState.map_asset);
  const mapConfig = getMapConfig(mapBase);
  const pathsUrl = `/${mapBase}.svg`;
  // Use dimensions read from the loaded SVG when available so overlay matches the file exactly
  const SVG_VIEWBOX = loadedSvgDimensions?.viewBox ?? mapConfig.viewBox;
  const IMG_DIMENSIONS = loadedSvgDimensions?.dimensions ?? mapConfig.dimensions;
  imgDimensionsRef.current = IMG_DIMENSIONS;

  // gameState.map_asset is a single root name; we derive PNG and SVG URLs from it.
  // Always try PNG first for the current map; only use SVG if this map's PNG fails (so we never show a previous map's URL).
  const publicBase = typeof window !== 'undefined' ? window.location.origin : '';
  const pngUrl = `${publicBase}/${mapBase}.png`;
  const svgFallbackUrl = `${publicBase}/${mapBase}.svg`;
  const [pngFailedForMap, setPngFailedForMap] = useState<string | null>(null);
  useEffect(() => {
    setPngFailedForMap(null);
  }, [mapBase]);
  const handleBgImageError = useCallback(
    (e: React.SyntheticEvent<HTMLImageElement>) => {
      const failedSrc = (e.currentTarget as HTMLImageElement)?.currentSrc ?? '';
      if (failedSrc.includes(`${mapBase}.png`)) setPngFailedForMap(mapBase);
    },
    [mapBase]
  );
  const imageUrl = pngFailedForMap === mapBase ? svgFallbackUrl : pngUrl;

  const activeMobilizationItem = useMemo(() => {
    if (!activeDragId || typeof activeDragId !== 'string' || !activeDragId.startsWith('mobilize-')) return null;
    if (activeDragId.startsWith('mobilize-camp-')) return null; // camps use activeCampDrag
    const unitId = activeDragId.replace(/^mobilize-/, '');
    const purchase = mobilizationTray?.purchases?.find(p => p.unitId === unitId) ?? null;
    return purchase ? { ...purchase, factionColor: mobilizationTray?.factionColor ?? '' } : null;
  }, [activeDragId, mobilizationTray?.purchases, mobilizationTray?.factionColor]);

  const activeCampDrag = useMemo(() => {
    if (!activeDragId || typeof activeDragId !== 'string' || !activeDragId.startsWith('mobilize-camp-')) return null;
    const match = activeDragId.match(/^mobilize-camp-(\d+)$/);
    return match ? { campIndex: parseInt(match[1], 10) } : null;
  }, [activeDragId]);

  // Load SVG paths (re-run when this game's map asset changes).
  // Background is PNG (same base name as SVG); SVG is used only for territory borders/paths.
  // If the SVG has a "svg_borders" layer we use only that; otherwise all paths from the root.
  // Paths must have Inkscape label or non-generic id to match backend territory IDs. Fallback to default map if 404.
  const INKSCAPE_NS = 'http://www.inkscape.org/namespaces/inkscape';
  useEffect(() => {
    setSvgPaths(new Map());
    setLoadedSvgDimensions(null);
    const loadSvg = (url: string) =>
      fetch(url, { cache: 'no-store' })
        .then((res) => (res.ok ? res.text() : Promise.reject(new Error(`${res.status}`))))
        .then((svgText) => {
          const parser = new DOMParser();
          const svgDoc = parser.parseFromString(svgText, 'image/svg+xml');
          const root = svgDoc.querySelector('svg');
          let viewBox = mapConfig.viewBox;
          let dimensions = mapConfig.dimensions;
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
            const wAttr = root.getAttribute('width');
            const hAttr = root.getAttribute('height');
            if (wAttr != null && hAttr != null) {
              const w = parseFloat(wAttr);
              const h = parseFloat(hAttr);
              if (Number.isFinite(w) && Number.isFinite(h)) dimensions = { width: w, height: h };
            }
            if (dimensions === mapConfig.dimensions && viewBox !== mapConfig.viewBox) {
              dimensions = viewBox;
            }
            setLoadedSvgDimensions({ viewBox, dimensions });
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
          const getTerritoryId = (el: Element): string | null => {
            const label = el.getAttributeNS(INKSCAPE_NS, 'label');
            const id = el.getAttribute('id')?.trim();
            const rawId = (id && !/^path\d+$/i.test(id) ? id : null) || label;
            if (!rawId) return null;
            return rawId
              .toLowerCase()
              .replace(/\s+/g, '_')
              .replace(/'/g, '')
              .replace(/-/g, '_');
          };
          pathRoot.querySelectorAll('path').forEach((path) => {
            const d = path.getAttribute('d');
            if (!d) return;
            const territoryId = getTerritoryId(path);
            if (!territoryId) return;
            const transform = path.getAttribute('transform')?.trim() || undefined;
            pathMap.set(territoryId, { d, transform });
          });
          pathRoot.querySelectorAll('circle').forEach((circle) => {
            const cx = parseFloat(circle.getAttribute('cx') ?? '0');
            const cy = parseFloat(circle.getAttribute('cy') ?? '0');
            const r = parseFloat(circle.getAttribute('r') ?? '0');
            if (!Number.isFinite(cx + cy + r)) return;
            const territoryId = getTerritoryId(circle);
            if (!territoryId) return;
            // Path for circle centered at (cx, cy): start at (cx+r, cy), arc to (cx-r, cy), arc back
            const d = `M ${cx + r} ${cy} a ${r} ${r} 0 1 1 ${-2 * r} 0 a ${r} ${r} 0 1 1 ${2 * r} 0`;
            pathMap.set(territoryId, { d });
          });
          setSvgPaths(pathMap);
        });
    loadSvg(pathsUrl).catch(() => {
      if (pathsUrl !== `/${DEFAULT_MAP_BASE}.svg`) loadSvg(`/${DEFAULT_MAP_BASE}.svg`);
    });
  }, [pathsUrl, mapConfig.viewBox, mapConfig.dimensions]);

  // Calculate marker positions: use "pole of inaccessibility" (point inside shape farthest from boundary) when possible
  useEffect(() => {
    if (svgPaths.size === 0 || !svgRef.current) {
      setTerritoryCentroids({});
      return;
    }

    const computeCentroids = () => {
      const centroids: Record<string, { x: number; y: number }> = {};
      // First: compute from path data (temp SVG) for every territory so we never miss one (e.g. neutral moria/withered_heath).
      // This runs regardless of DOM so all territories in svgPaths get a centroid.
      svgPaths.forEach((pathData, territoryId) => {
        const known = OSGILIATH_CENTROIDS[territoryId];
        if (known) {
          centroids[territoryId] = known;
          return;
        }
        try {
          const viewBox = { w: SVG_VIEWBOX.width, h: SVG_VIEWBOX.height };
          const tmp = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          tmp.setAttribute('viewBox', `0 0 ${viewBox.w} ${viewBox.h}`);
          tmp.setAttribute('width', '1');
          tmp.setAttribute('height', '1');
          tmp.style.position = 'absolute';
          tmp.style.left = '-9999px';
          const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          path.setAttribute('d', pathData.d);
          if (pathData.transform) path.setAttribute('transform', pathData.transform);
          tmp.appendChild(path);
          document.body.appendChild(tmp);
          const bbox = path.getBBox();
          document.body.removeChild(tmp);
          if (Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height)) {
            centroids[territoryId] = {
              x: bbox.x + bbox.width / 2,
              y: bbox.y + bbox.height / 2,
            };
          }
        } catch {
          /* ignore */
        }
      });
      // Optional refinement: use in-DOM paths for "pole of inaccessibility" when available (better centering)
      const svg = svgRef.current;
      if (!svg) return centroids;
      const pathElements = svg.querySelectorAll('path[id^="territory-"]');
      pathElements?.forEach((pathEl) => {
        const path = pathEl as SVGPathElement;
        const id = path.id?.replace('territory-', '');
        if (!id) return;
        try {
          // Skip transformed paths here; in-DOM getBBox() often returns local (untransformed) coords. We already have centroid from path-data pass above.
          if (path.getAttribute('transform')) return;
          const bbox = path.getBBox();
          const cx = bbox.x + bbox.width / 2;
          const cy = bbox.y + bbox.height / 2;
          const pt = svg.createSVGPoint();
          const isInside = (x: number, y: number) => {
            pt.x = x;
            pt.y = y;
            return path.isPointInFill(pt);
          };
          // Sample boundary points for distance-to-edge
          const boundaryPts: { x: number; y: number }[] = [];
          try {
            const totalLen = path.getTotalLength();
            const numSamples = Math.min(60, Math.max(20, Math.floor(totalLen / 15)));
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
          // Grid search for point inside with maximum distance to boundary (pole of inaccessibility)
          const gridSteps = 12;
          let best = { x: cx, y: cy, d: 0 };
          for (let i = 0; i <= gridSteps; i++) {
            for (let j = 0; j <= gridSteps; j++) {
              const x = bbox.x + (bbox.width * i) / gridSteps;
              const y = bbox.y + (bbox.height * j) / gridSteps;
              if (isInside(x, y)) {
                const d = distToBoundary(x, y);
                if (d > best.d) best = { x, y, d };
              }
            }
          }
          if (best.d > 0) {
            centroids[id] = { x: best.x, y: best.y };
            return;
          }
          if (isInside(cx, cy)) {
            centroids[id] = { x: cx, y: cy };
            return;
          }
          for (let i = 1; i < 8; i++) {
            for (let j = 1; j < 8; j++) {
              const x = bbox.x + (bbox.width * i) / 8;
              const y = bbox.y + (bbox.height * j) / 8;
              if (isInside(x, y)) {
                centroids[id] = { x, y };
                return;
              }
            }
          }
          centroids[id] = { x: cx, y: cy };
        } catch {
          // getBBox / isPointInFill can fail for transformed or complex paths; fallback to bbox center so markers still show
          try {
            const bbox = path.getBBox();
            if (id && Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height)) {
              centroids[id] = {
                x: bbox.x + bbox.width / 2,
                y: bbox.y + bbox.height / 2,
              };
            }
          } catch {
            /* ignore */
          }
        }
      });
      return centroids;
    };

    let timer2: ReturnType<typeof setTimeout> | null = null;
    const timer1 = setTimeout(() => {
      const centroids = computeCentroids();
      setTerritoryCentroids(centroids);
      if (Object.keys(centroids).length < svgPaths.size) {
        timer2 = setTimeout(() => {
          setTerritoryCentroids((prev) => {
            const next = computeCentroids();
            return Object.keys(next).length > Object.keys(prev).length ? next : prev;
          });
        }, 200);
      }
    }, 200);

    return () => {
      clearTimeout(timer1);
      if (timer2 != null) clearTimeout(timer2);
    };
  }, [svgPaths]);

  // Compute move arrows to render - only for current phase moves
  const moveArrows = useMemo(() => {
    if (Object.keys(territoryCentroids).length === 0) return [];
    
    // Filter moves to only show current phase's moves
    const currentPhaseMoves = pendingMoves.filter(move => {
      if (gameState.phase === 'combat_move') return move.phase === 'combat_move';
      if (gameState.phase === 'non_combat_move') return move.phase === 'non_combat_move';
      return false; // Don't show arrows during other phases
    });
    
    // Group moves by from->to
    const moveGroups: Record<string, { 
      from: string; 
      to: string; 
      isCombat: boolean;
      count: number;
    }> = {};
    
    currentPhaseMoves.forEach(move => {
      // move.from / move.to are source and destination (from_territory / to_territory from backend)
      const from = move.from;
      const to = move.to;
      const key = `${from}->${to}`;
      if (!moveGroups[key]) {
        moveGroups[key] = {
          from,
          to,
          isCombat: move.phase === 'combat_move',
          count: 0,
        };
      }
      moveGroups[key].count += move.count;
    });
    
    return Object.values(moveGroups).map(group => {
      const fromCentroid = territoryCentroids[group.from];
      const toCentroid = territoryCentroids[group.to];
      
      if (!fromCentroid || !toCentroid) return null;
      
      // Calculate arrow path
      const dx = toCentroid.x - fromCentroid.x;
      const dy = toCentroid.y - fromCentroid.y;
      const length = Math.sqrt(dx * dx + dy * dy);
      if (length < 1e-6) return null; // Skip degenerate arrow

      // Shorten arrow so it doesn't overlap territory centers; never shorten more than segment length
      const shortenStart = 30;
      const shortenEnd = 40;
      const totalShorten = Math.min(shortenStart + shortenEnd, length * 0.9);
      const startRatio = totalShorten > 0 ? shortenStart / (shortenStart + shortenEnd) : 0;
      const startX = fromCentroid.x + (dx / length) * totalShorten * startRatio;
      const startY = fromCentroid.y + (dy / length) * totalShorten * startRatio;
      const endX = toCentroid.x - (dx / length) * totalShorten * (1 - startRatio);
      const endY = toCentroid.y - (dy / length) * totalShorten * (1 - startRatio);
      
      return {
        ...group,
        startX,
        startY,
        endX,
        endY,
      };
    }).filter(Boolean);
  }, [pendingMoves, territoryCentroids, gameState.phase]);

  // Fit whole map to view once per map load. Run at 0, 50, 200, 500ms so we catch layout (wrapper is often 0x0 on first paint).
  // Store fit scale so we never zoom out past it.
  const fitScaleRef = useRef<number>(0.25);
  const fitDoneRef = useRef(false);
  const lastMapBaseRef = useRef<string>(mapBase);
  if (lastMapBaseRef.current !== mapBase) {
    lastMapBaseRef.current = mapBase;
    fitDoneRef.current = false;
  }
  useEffect(() => {
    if (loadedSvgDimensions) fitDoneRef.current = false;
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const mapW = IMG_DIMENSIONS.width;
    const mapH = IMG_DIMENSIONS.height;
    if (!Number.isFinite(mapW) || !Number.isFinite(mapH) || mapW <= 0 || mapH <= 0) return;

    const doFitOnce = () => {
      if (fitDoneRef.current) return;
      const w = wrapperRef.current?.clientWidth ?? 0;
      const h = wrapperRef.current?.clientHeight ?? 0;
      if (w <= 0 || h <= 0) return;
      fitDoneRef.current = true;
      const scaleX = w / mapW;
      const scaleY = h / mapH;
      const scale = Math.min(scaleX, scaleY);
      fitScaleRef.current = scale;
      const x = (w - mapW * scale) / 2;
      const y = (h - mapH * scale) / 2;
      setTransform({ x, y, scale });
    };

    doFitOnce();
    const t1 = setTimeout(doFitOnce, 50);
    const t2 = setTimeout(doFitOnce, 200);
    const t3 = setTimeout(doFitOnce, 500);

    const ro = new ResizeObserver(doFitOnce);
    ro.observe(wrapper);

    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(t3);
      ro.disconnect();
    };
  }, [mapBase, IMG_DIMENSIONS.width, IMG_DIMENSIONS.height, loadedSvgDimensions]);

  // Helper to get alliance of a territory
  const getAlliance = useCallback((owner?: string) => {
    if (!owner) return null;
    return factionData[owner]?.alliance || null;
  }, [factionData]);

  // Get valid move targets - backend is source of truth (uses remaining_movement). Fallback BFS only when backend gave us entries but empty destinations (e.g. units in neutral).
  // Aerial units: can only attack if they can reach friendly territory with remaining moves (must land in friendly before phase end).
  const getValidTargets = useCallback((fromTerritory: string, unitId: string): Set<string> => {
    const matches = availableMoveTargets
      ? availableMoveTargets.filter(m => m.territory === fromTerritory && m.unit.unit_id === unitId)
      : [];
    let movement: number;
    if (availableMoveTargets && matches.length > 0) {
      // Use UNION of destinations so we show all targets any unit in the stack can reach (max range).
      // User can then drop on a far territory and the confirm popup only allows moving as many units as can reach it.
      const validTargets = new Set<string>();
      for (const m of matches) {
        for (const d of (m.destinations || [])) {
          validTargets.add(d);
        }
      }
      if (validTargets.size > 0) return validTargets;
      // Backend included this origin/unit but returned no destinations → use remaining_movement for fallback BFS
      movement = Math.max(0, ...matches.map(m => m.unit?.remaining_movement ?? 0));
      if (movement <= 0) return new Set();
    } else if (availableMoveTargets) {
      // Backend gave no entries for this (territory, unitId) → unit(s) have 0 remaining movement; don't show any targets
      return new Set();
    } else {
      // No backend data (e.g. not in move phase) - use unit type base movement
      movement = unitStats[unitId]?.movement || 1;
    }

    const territory = territoryData[fromTerritory];
    if (!territory) return new Set();
    const currentFaction = gameState.current_faction;
    const currentAlliance = getAlliance(currentFaction);
    const ud = unitDefs[unitId] as { archetype?: string; tags?: string[] } | undefined;
    const isAerial = ud?.archetype === 'aerial' || (ud?.tags && ud.tags.includes('aerial'));

    // Helper: can we reach any allied territory from this territory with movesLeft? (for aerial return-path rule; matches backend: only allied-owned counts, not neutral)
    const canReachFriendlyFrom = (fromTid: string, movesLeft: number): boolean => {
      if (movesLeft < 0) return false;
      const fromT = territoryData[fromTid];
      if (!fromT) return false;
      const owner = fromT.owner;
      const isNeutral = owner == null || owner === undefined || owner === '';
      const alliance = getAlliance(owner);
      const isFriendly = !isNeutral && (owner === currentFaction || (alliance !== null && alliance === currentAlliance));
      if (isFriendly) return true;
      if (movesLeft === 0) return false;
      const visited = new Set<string>([fromTid]);
      const queue: [string, number][] = [[fromTid, 0]];
      while (queue.length > 0) {
        const [tid, steps] = queue.shift()!;
        if (steps >= movesLeft) continue;
        const t = territoryData[tid];
        if (!t) continue;
        for (const adjId of t.adjacent) {
          if (visited.has(adjId)) continue;
          visited.add(adjId);
          const adj = territoryData[adjId];
          if (!adj) continue;
          const aOwner = adj.owner;
          const aNeutral = aOwner == null || aOwner === undefined || aOwner === '';
          const aAlliance = getAlliance(aOwner);
          const aFriendly = !aNeutral && (aOwner === currentFaction || (aAlliance !== null && aAlliance === currentAlliance));
          if (aFriendly) return true;
          queue.push([adjId, steps + 1]);
        }
      }
      return false;
    };

    const validTargets = new Set<string>();
    const queue: [string, number][] = [[fromTerritory, movement]];
    const visited = new Set<string>([fromTerritory]);

    while (queue.length > 0) {
      const [currentId, remainingMoves] = queue.shift()!;
      const current = territoryData[currentId];
      if (!current) continue;

      for (const adjId of current.adjacent) {
        if (visited.has(adjId)) continue;

        const adjTerritory = territoryData[adjId];
        if (!adjTerritory) continue;

        const adjOwner = adjTerritory.owner;
        const isNeutral = adjOwner == null || adjOwner === undefined || adjOwner === '';
        const adjAlliance = getAlliance(adjOwner);
        const isFriendly = !isNeutral && (adjOwner === currentFaction ||
                          (adjAlliance !== null && adjAlliance === currentAlliance));
        const isEnemy = !isNeutral && adjAlliance !== null && adjAlliance !== currentAlliance;
        const movesAfterLanding = remainingMoves - 1;

        if (isCombatMove) {
          if (isFriendly) {
            visited.add(adjId);
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          } else if (isEnemy) {
            visited.add(adjId);
            const enemyHasUnits = (territoryUnits[adjId] || []).length > 0;
            if (!isAerial) {
              validTargets.add(adjId);
            } else {
              // Aerial: combat move only into territories that have units to attack
              if (enemyHasUnits && canReachFriendlyFrom(adjId, movesAfterLanding)) {
                validTargets.add(adjId);
              }
            }
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          } else if (isNeutral) {
            visited.add(adjId);
            const ownable = adjTerritory.ownable !== false;
            const stacks = territoryUnits[adjId] || [];
            const hasEnemyUnits = stacks.some(
              (s) => unitDefs[s.unit_id]?.faction !== currentFaction &&
                getAlliance(unitDefs[s.unit_id]?.faction) !== currentAlliance
            );
            // Empty unowned ownable: ground only (conquer). Aerial cannot combat-move into empty.
            if (ownable && !hasEnemyUnits) {
              if (!isAerial) validTargets.add(adjId);
            } else if (hasEnemyUnits && (!isAerial || canReachFriendlyFrom(adjId, movesAfterLanding))) {
              validTargets.add(adjId);
            }
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          }
        } else {
          if (isFriendly) {
            visited.add(adjId);
            validTargets.add(adjId);
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          } else if (isNeutral) {
            const hasEnemyUnits = (territoryUnits[adjId] || []).some(
              (s) => unitDefs[s.unit_id]?.faction !== currentFaction &&
                getAlliance(unitDefs[s.unit_id]?.faction) !== currentAlliance
            );
            visited.add(adjId);
            const ownable = adjTerritory.ownable !== false;
            if (!hasEnemyUnits && !ownable) validTargets.add(adjId);
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          }
        }
      }
    }

    return validTargets;
  }, [availableMoveTargets, territoryData, territoryUnits, unitDefs, unitStats, gameState.current_faction, isCombatMove, getAlliance]);

  // Handle drag start
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const data = event.active.data.current;
    if (!data) return;
    setActiveDragId(event.active.id as string);
    if ((data as { type?: string }).type === 'mobilization-camp') {
      setActiveUnit(null);
      const campIndex = (data as { campIndex: number }).campIndex;
      const options = mobilizationTray?.pendingCamps?.find(p => p.campIndex === campIndex)?.options ?? [];
      const blocked = new Set(territoriesWithPendingCampPlacement);
      // Only allow territories with power > 0 (so units can mobilize there)
      const valid = options.filter(
        (t: string) => !blocked.has(t) && (territoryData[t]?.produces ?? 0) > 0
      );
      setValidDropTargets(new Set(valid));
      return;
    }
    if ((data as { type?: string }).type === 'mobilization-unit') {
      setActiveUnit(null);
      // Any camp with room is a valid drop target; we cap count at drop time to remaining capacity
      const withRoom = validMobilizeTerritories.filter(
        tid => (remainingMobilizationCapacity[tid] ?? 0) > 0
      );
      setValidDropTargets(new Set(withRoom));
      return;
    }
    const { unitId, territoryId, count, unitDef, factionColor } = data as {
      unitId: string;
      territoryId: string;
      count: number;
      unitDef?: { name: string; icon: string };
      factionColor?: string;
    };
    setActiveUnit({ unitId, territoryId, count, unitDef, factionColor });
    setValidDropTargets(getValidTargets(territoryId, unitId));
  }, [getValidTargets, validMobilizeTerritories, remainingMobilizationCapacity, mobilizationTray?.pendingCamps, territoriesWithPendingCampPlacement, territoryData]);

  // Handle drag end
  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event;
    const data = active.data.current;
    const dataType = (data as { type?: string })?.type;
    const isMobilizationCamp = dataType === 'mobilization-camp';
    const isMobilization = dataType === 'mobilization-unit';

    if (isMobilizationCamp && over && onCampDrop) {
      const targetTerritory = (over.data.current as { territoryId: string })?.territoryId;
      const campIndex = (data as { campIndex: number }).campIndex;
      if (targetTerritory && validDropTargets.has(targetTerritory)) {
        onCampDrop(campIndex, targetTerritory);
      }
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (isMobilization && over && onMobilizationDrop) {
      const targetTerritory = (over.data.current as { territoryId: string })?.territoryId;
      const { unitId, unitName, icon, count } = (data as { unitId: string; unitName: string; icon: string; count: number });
      if (targetTerritory && validDropTargets.has(targetTerritory)) {
        const remaining = remainingMobilizationCapacity[targetTerritory] ?? 0;
        const cappedCount = Math.min(count, remaining);
        if (cappedCount > 0) {
          onMobilizationDrop(targetTerritory, unitId, unitName, icon, cappedCount);
        }
      }
      setActiveUnit(null);
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (over && activeUnit) {
      const targetTerritory = (over.data.current as { territoryId: string })?.territoryId;
      if (targetTerritory && validDropTargets.has(targetTerritory)) {
        // Max count = units in territory of this type minus already committed in other pending moves
        const totalInTerritory = (territoryUnits[activeUnit.territoryId] || [])
          .filter(u => u.unit_id === activeUnit.unitId)
          .reduce((s, u) => s + u.count, 0);
        const committedElsewhere = pendingMoves
          .filter(m => m.from === activeUnit.territoryId && m.unitType === activeUnit.unitId)
          .reduce((s, m) => s + m.count, 0);
        let availableCount = Math.max(0, totalInTerritory - committedElsewhere);
        // Cap by how many units can actually reach this destination when backend provided move targets for this origin
        // (e.g. owned territory). When origin is unownable/neutral, backend may omit it from moveable_units; then
        // validDropTargets came from fallback BFS—don't cap to 0, allow the move so the confirm popup shows.
        if (availableMoveTargets && availableCount > 0) {
          const canReachCount = availableMoveTargets.filter(
            m => m.territory === activeUnit.territoryId
              && m.unit.unit_id === activeUnit.unitId
              && m.destinations.includes(targetTerritory)
          ).length;
          if (canReachCount > 0) {
            availableCount = Math.min(availableCount, canReachCount);
          }
        }
        if (availableCount <= 0) {
          setActiveUnit(null);
          setActiveDragId(null);
          setValidDropTargets(new Set());
          return;
        }
        const matches = (availableMoveTargets ?? []).filter(
          m => m.territory === activeUnit.territoryId && m.unit.unit_id === activeUnit.unitId && m.destinations.includes(targetTerritory)
        );
        // Prefer the move entry that has the most charge path options for this destination (in case multiple units match)
        const match = matches.length > 0
          ? matches.reduce((best, m) => {
              const cr = m.charge_routes && typeof m.charge_routes === 'object' && !Array.isArray(m.charge_routes) ? m.charge_routes : {};
              const raw = cr[targetTerritory];
              const n = Array.isArray(raw) ? raw.length : 0;
              const bestRaw = best?.charge_routes && typeof best.charge_routes === 'object' ? best.charge_routes[targetTerritory] : undefined;
              const bestN = Array.isArray(bestRaw) ? bestRaw.length : 0;
              return n >= bestN ? m : best;
            })
          : undefined;
        const rawPaths = match?.charge_routes && typeof match.charge_routes === 'object' ? match.charge_routes[targetTerritory] : undefined;
        const paths = Array.isArray(rawPaths) ? rawPaths : [];
        const singlePath = paths.length === 1 ? paths[0] : undefined;
        const chargeThrough = singlePath?.length ? singlePath : (paths[0]?.length ? paths[0] : undefined);
        onSetPendingMove({
          fromTerritory: activeUnit.territoryId,
          toTerritory: targetTerritory,
          unitId: activeUnit.unitId,
          unitDef: activeUnit.unitDef,
          maxCount: availableCount,
          count: Math.min(activeUnit.count, availableCount),
          chargeThrough: paths.length <= 1 ? chargeThrough : undefined,
          chargePathOptions: paths.length > 1 ? paths : undefined,
        });
      }
    }
    setActiveUnit(null);
    setActiveDragId(null);
    setValidDropTargets(new Set());
  }, [activeUnit, validDropTargets, territoryUnits, pendingMoves, availableMoveTargets, onSetPendingMove, onMobilizationDrop, onCampDrop]);

  // Handle territory click (toggle selection); ignore if user panned (drag > threshold)
  const handleTerritoryClick = useCallback((territoryId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const dx = e.clientX - panStartPos.current.x;
    const dy = e.clientY - panStartPos.current.y;
    if (dx * dx + dy * dy >= PAN_CLICK_THRESHOLD_PX * PAN_CLICK_THRESHOLD_PX) return; // Was a pan, not a click
    if (selectedTerritory === territoryId) {
      onTerritorySelect(null);
    } else {
      onTerritorySelect(territoryId);
    }
    onUnitSelect(null);
  }, [selectedTerritory, onTerritorySelect, onUnitSelect]);

  // Handle background click: clear selection when clicking map background; ignore if we just panned
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const isBackground = target.classList?.contains('map-wrapper') ||
      target.classList?.contains('map-background') ||
      target.classList?.contains('map-inner');
    if (!isBackground) return;
    const dx = e.clientX - panStartPos.current.x;
    const dy = e.clientY - panStartPos.current.y;
    if (dx * dx + dy * dy >= PAN_CLICK_THRESHOLD_PX * PAN_CLICK_THRESHOLD_PX) return; // Was a pan, not a click
    onTerritorySelect(null);
    onUnitSelect(null);
  }, [onTerritorySelect, onUnitSelect]);

  // Pan handlers: allow pan from anywhere (including territory paths); only treat as territory click if drag was minimal
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest('.unit-token')) return; // Don't start pan when pressing on a unit (so unit drag works)
    
    setIsDragging(true);
    setDragStart({ x: e.clientX - transform.x, y: e.clientY - transform.y });
    panStartPos.current = { x: e.clientX, y: e.clientY };
  }, [transform]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isDragging || !wrapperRef.current) return;
    
    const wrapper = wrapperRef.current;
    let newX = e.clientX - dragStart.x;
    let newY = e.clientY - dragStart.y;
    
    // Clamp to boundaries
    const scaledWidth = IMG_DIMENSIONS.width * transform.scale;
    const scaledHeight = IMG_DIMENSIONS.height * transform.scale;
    const minX = Math.min(0, wrapper.clientWidth - scaledWidth);
    const minY = Math.min(0, wrapper.clientHeight - scaledHeight);
    
    newX = Math.max(minX, Math.min(0, newX));
    newY = Math.max(minY, Math.min(0, newY));
    
    setTransform(prev => ({ ...prev, x: newX, y: newY }));
  }, [isDragging, dragStart, transform.scale]);

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  // Non-passive wheel listener: pinch (ctrlKey) = zoom, 2-finger drag = pan
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      if (!wrapperRef.current) return;
      const t = transformRef.current;
      const wrapper = wrapperRef.current;
      const dims = imgDimensionsRef.current;

      const scaledW = dims.width * t.scale;
      const scaledH = dims.height * t.scale;
      const minX = Math.min(0, wrapper.clientWidth - scaledW);
      const minY = Math.min(0, wrapper.clientHeight - scaledH);
      const maxX = Math.max(0, wrapper.clientWidth - scaledW);
      const maxY = Math.max(0, wrapper.clientHeight - scaledH);

      if (e.ctrlKey) {
        // Pinch zoom: zoom toward cursor
        const rect = wrapper.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;
        const delta = e.deltaY > 0 ? 0.9 : 1.1;
        const minScale = Math.max(0.1, fitScaleRef.current);
        const newScale = Math.max(minScale, Math.min(MAX_SCALE, t.scale * delta));
        const scaleRatio = newScale / t.scale;
        let newX = mouseX - (mouseX - t.x) * scaleRatio;
        let newY = mouseY - (mouseY - t.y) * scaleRatio;
        const newScaledW = dims.width * newScale;
        const newScaledH = dims.height * newScale;
        const newMinX = Math.min(0, wrapper.clientWidth - newScaledW);
        const newMinY = Math.min(0, wrapper.clientHeight - newScaledH);
        const newMaxX = Math.max(0, wrapper.clientWidth - newScaledW);
        const newMaxY = Math.max(0, wrapper.clientHeight - newScaledH);
        newX = Math.max(newMinX, Math.min(newMaxX, newX));
        newY = Math.max(newMinY, Math.min(newMaxY, newY));
        setTransform({ x: newX, y: newY, scale: newScale });
      } else {
        // 2-finger drag: pan by delta
        let newX = Math.max(minX, Math.min(maxX, t.x + e.deltaX));
        let newY = Math.max(minY, Math.min(maxY, t.y + e.deltaY));
        setTransform({ ...t, x: newX, y: newY });
      }
    };
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
  }, []);

  // Clamp position to keep map in view
  const clampPosition = useCallback((x: number, y: number, scale: number) => {
    if (!wrapperRef.current) return { x, y };
    
    const wrapper = wrapperRef.current;
    const scaledWidth = IMG_DIMENSIONS.width * scale;
    const scaledHeight = IMG_DIMENSIONS.height * scale;
    
    // Calculate boundaries - keep map within viewport (allow positive x/y when map is smaller than viewport to center it)
    const minX = Math.min(0, wrapper.clientWidth - scaledWidth);
    const minY = Math.min(0, wrapper.clientHeight - scaledHeight);
    const maxX = Math.max(0, wrapper.clientWidth - scaledWidth);
    const maxY = Math.max(0, wrapper.clientHeight - scaledHeight);
    
    return {
      x: Math.max(minX, Math.min(maxX, x)),
      y: Math.max(minY, Math.min(maxY, y)),
    };
  }, [IMG_DIMENSIONS.width, IMG_DIMENSIONS.height]);

  // Zoom controls
  const zoomIn = () => {
    if (!wrapperRef.current) return;
    const newScale = Math.min(MAX_SCALE, transform.scale * 1.2);
    const clamped = clampPosition(transform.x, transform.y, newScale);
    setTransform({ ...clamped, scale: newScale });
  };

  const zoomOut = () => {
    if (!wrapperRef.current) return;
    const minScale = Math.max(0.1, fitScaleRef.current);
    const newScale = Math.max(minScale, transform.scale / 1.2);
    const clamped = clampPosition(transform.x, transform.y, newScale);
    setTransform({ ...clamped, scale: newScale });
  };

  const resetView = () => {
    if (!wrapperRef.current) return;
    
    const wrapper = wrapperRef.current;
    const scaleX = wrapper.clientWidth / IMG_DIMENSIONS.width;
    const scaleY = wrapper.clientHeight / IMG_DIMENSIONS.height;
    const scale = Math.min(scaleX, scaleY);
    
    const x = (wrapper.clientWidth - IMG_DIMENSIONS.width * scale) / 2;
    const y = (wrapper.clientHeight - IMG_DIMENSIONS.height * scale) / 2;
    
    setTransform({ x, y, scale });
  };

  // Pan controls
  const PAN_AMOUNT = 100;
  
  const panUp = () => {
    const newY = transform.y + PAN_AMOUNT;
    const clamped = clampPosition(transform.x, newY, transform.scale);
    setTransform(prev => ({ ...prev, ...clamped }));
  };
  
  const panDown = () => {
    const newY = transform.y - PAN_AMOUNT;
    const clamped = clampPosition(transform.x, newY, transform.scale);
    setTransform(prev => ({ ...prev, ...clamped }));
  };
  
  const panLeft = () => {
    const newX = transform.x + PAN_AMOUNT;
    const clamped = clampPosition(newX, transform.y, transform.scale);
    setTransform(prev => ({ ...prev, ...clamped }));
  };
  
  const panRight = () => {
    const newX = transform.x - PAN_AMOUNT;
    const clamped = clampPosition(newX, transform.y, transform.scale);
    setTransform(prev => ({ ...prev, ...clamped }));
  };

  // Convert SVG coords to screen coords for unit placement
  const svgToScreen = (svgX: number, svgY: number) => {
    const scaleX = IMG_DIMENSIONS.width / SVG_VIEWBOX.width;
    const scaleY = IMG_DIMENSIONS.height / SVG_VIEWBOX.height;
    return {
      x: svgX * scaleX,
      y: svgY * scaleY,
    };
  };

  // Clamp position to map bounds with inset so markers/units fit comfortably inside (e.g. far_harad)
  const CLAMP_INSET = 80;
  const clampToMap = (p: { x: number; y: number }) => ({
    x: Math.max(CLAMP_INSET, Math.min(IMG_DIMENSIONS.width - CLAMP_INSET, p.x)),
    y: Math.max(CLAMP_INSET, Math.min(IMG_DIMENSIONS.height - CLAMP_INSET, p.y)),
  });

  // Fallback position when territory has units but no centroid (e.g. not in SVG yet). Deterministic per territoryId so they don't stack.
  const fallbackPositionForTerritory = (territoryId: string) => {
    let h = 0;
    for (let i = 0; i < territoryId.length; i++) h = (h * 31 + territoryId.charCodeAt(i)) >>> 0;
    const t = (h % 1000) / 1000;
    const u = ((h >> 10) % 1000) / 1000;
    const margin = CLAMP_INSET * 2;
    return clampToMap({
      x: margin + t * (IMG_DIMENSIONS.width - margin * 2),
      y: margin + u * (IMG_DIMENSIONS.height - margin * 2),
    });
  };

  return (
    <DndContext
      sensors={sensors}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      collisionDetection={pointerWithin}
    >
      <div className={`map-container ${mobilizationTray ? 'map-container--with-tray' : ''}`}>
        <div className="map-content">
        <div
          ref={wrapperRef}
          className={`map-wrapper ${isDragging ? 'panning' : ''}`}
          data-map-base={mapBase}
          data-scale={transform.scale.toFixed(3)}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          onClick={handleBackgroundClick}
        >
          <div
            className="map-inner"
            style={{
              width: IMG_DIMENSIONS.width,
              height: IMG_DIMENSIONS.height,
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
            }}
          >
            <img
              key={mapBase}
              className="map-background"
              src={`${imageUrl}?v=1`}
              alt="Map"
              draggable={false}
              onError={handleBgImageError}
              style={{
                width: IMG_DIMENSIONS.width,
                height: IMG_DIMENSIONS.height,
                objectFit: 'fill',
                display: 'block',
                opacity: 0.35,
              }}
            />
            <svg
              ref={svgRef}
              className="map-svg"
              width={IMG_DIMENSIONS.width}
              height={IMG_DIMENSIONS.height}
              viewBox={`0 0 ${SVG_VIEWBOX.width} ${SVG_VIEWBOX.height}`}
              preserveAspectRatio="none"
            >
              {Array.from(svgPaths.entries()).map(([territoryId, pathData]) => {
                const territory = territoryData[territoryId];
                const owner = territory?.owner;
                const isNonOwnable = territory && (territory.ownable === false);
                const color = owner
                  ? factionData[owner]?.color
                  : isNonOwnable
                    ? '#7a7a7a'
                    : '#d4c4a8';
                const isSelected = selectedTerritory === territoryId;
                const isValidDrop = validDropTargets.has(territoryId);
                const hasUnitsToMobilize = (mobilizationTray?.purchases?.length ?? 0) > 0;
                const hasMobilizationRoom = (remainingMobilizationCapacity[territoryId] ?? 0) > 0;
                // Highlight camps that have room; only don't highlight when pending has filled that camp's capacity.
                const isValidMobilizationTarget = isMobilizePhase && validMobilizeTerritories.includes(territoryId) && hasMobilizationRoom && (activeDragId != null ? isValidDrop : (hasMobilizationSelected || hasUnitsToMobilize));
                const isExternallyHighlighted = highlightedTerritories.includes(territoryId);
                const isCampPlacementTarget = isMobilizePhase && (validCampTerritories.length > 0 && validCampTerritories.includes(territoryId) || validDropTargets.has(territoryId));
                return (
                  <DroppableTerritory
                    key={territoryId}
                    territoryId={territoryId}
                    pathData={pathData}
                    color={color}
                    isSelected={isSelected}
                    isHighlighted={isValidMobilizationTarget || isExternallyHighlighted || isCampPlacementTarget}
                    isValidDrop={isValidDrop || isValidMobilizationTarget || isExternallyHighlighted}
                    onClick={(e) => handleTerritoryClick(territoryId, e)}
                  />
                );
              })}
              <defs>
                <marker id="arrowhead-combat" markerWidth="4" markerHeight="4" refX="3" refY="2" orient="auto">
                  <polygon points="0,0 4,2 0,4" fill="#c62828" />
                </marker>
                <marker id="arrowhead-move" markerWidth="4" markerHeight="4" refX="3" refY="2" orient="auto">
                  <polygon points="0,0 4,2 0,4" fill="#2e7d32" />
                </marker>
              </defs>
              {moveArrows.map((arrow, idx) => arrow && (
                <line
                  key={`arrow-${idx}`}
                  className="move-arrow"
                  x1={arrow.startX}
                  y1={arrow.startY}
                  x2={arrow.endX}
                  y2={arrow.endY}
                  stroke={arrow.isCombat ? '#c62828' : '#2e7d32'}
                  strokeWidth="3"
                  strokeLinecap="round"
                  strokeDasharray={arrow.isCombat ? 'none' : '8,4'}
                  markerEnd={`url(#arrowhead-${arrow.isCombat ? 'combat' : 'move'})`}
                  opacity="0.9"
                />
              ))}
            </svg>

            {/* Territory markers (camps, strongholds) and power production badges). Requires territory in game state (e.g. create new game for east/west Osgiliath if missing). */}
            <div className="territory-markers-layer">
              {Object.keys(territoryCentroids).map((territoryId) => {
                const centroid = territoryCentroids[territoryId];
                const territory = territoryData[territoryId];
                if (!centroid || !territory) return null;
                const screenPos = svgToScreen(centroid.x, centroid.y);
                const hasCamp = territory.hasCamp === true;
                const showFactionLogo = territory.stronghold && territory.owner && factionData[territory.owner];
                const showNeutralStronghold = territory.stronghold && !territory.owner;
                const isCapital =
                  territory.isCapital === true ||
                  Object.values(factionData).some((f) => f.capital === territoryId);
                const power = Number(territory.produces ?? 0);
                // Ownable territories with 0 power: no production icon on map (avoid showing "0")
                const showPower = power > 0;
                const showStronghold = showFactionLogo || showNeutralStronghold;
                const showCamp = hasCamp && !isCapital;
                const campAndStrongholdRow = showCamp && showStronghold;
                const powerAndCampOnly = showPower && showCamp && !showStronghold;
                if (!showCamp && !showStronghold && !showPower) return null;
                const clampedMarkerPos = clampToMap(screenPos);
                return (
                  <div
                    key={territoryId}
                    className="territory-markers"
                    style={{
                      left: clampedMarkerPos.x,
                      top: clampedMarkerPos.y,
                    }}
                  >
                    {powerAndCampOnly ? (
                      <div className="territory-markers-row territory-markers-row--power-camp">
                        <div
                          className="territory-power-badge territory-power-badge--inline"
                          title={`${territory.name}: ${power} power`}
                          aria-hidden
                        >
                          {power}
                        </div>
                        <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                          <span className="camp-marker-emoji" aria-hidden>⛺</span>
                        </div>
                      </div>
                    ) : (
                      <>
                        {showPower && !powerAndCampOnly && (
                          <div
                            className={`territory-power-badge${showStronghold ? ' territory-power-badge--above-logo' : ''}`}
                            title={`${territory.name}: ${power} power`}
                            aria-hidden
                          >
                            {power}
                          </div>
                        )}
                        {campAndStrongholdRow ? (
                          <div className="territory-markers-row">
                            {showFactionLogo && (
                              <div
                                className={`territory-marker faction-marker ${isCapital ? 'faction-marker--capital' : ''}`}
                                title={territory.name + (isCapital ? ' (Capital)' : ' (Stronghold)')}
                              >
                                <img
                                  src={factionData[territory.owner!].icon}
                                  alt=""
                                  width={isCapital ? 60 : 44}
                                  height={isCapital ? 60 : 44}
                                />
                              </div>
                            )}
                            {showNeutralStronghold && (
                              <div
                                className="territory-marker neutral-stronghold-marker"
                                title={territory.name + ' (Neutral stronghold)'}
                                aria-hidden
                              />
                            )}
                            <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                              <span className="camp-marker-emoji" aria-hidden>⛺</span>
                            </div>
                          </div>
                        ) : (
                          !powerAndCampOnly && (
                            <>
                              {showCamp && (
                                <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                                  <span className="camp-marker-emoji" aria-hidden>⛺</span>
                                </div>
                              )}
                              {showFactionLogo && (
                                <div
                                  className={`territory-marker faction-marker ${isCapital ? 'faction-marker--capital' : ''}`}
                                  title={territory.name + (isCapital ? ' (Capital)' : ' (Stronghold)')}
                                >
                                  <img
                                    src={factionData[territory.owner!].icon}
                                    alt=""
                                    width={isCapital ? 60 : 44}
                                    height={isCapital ? 60 : 44}
                                  />
                                </div>
                              )}
                              {showNeutralStronghold && (
                                <div
                                  className="territory-marker neutral-stronghold-marker"
                                  title={territory.name + ' (Neutral stronghold)'}
                                  aria-hidden
                                />
                              )}
                            </>
                          )
                        )}
                      </>
                    )}
                  </div>
                );
              })}
            </div>
            
            <div className="unit-layer">
              {Object.entries(territoryUnits).map(([territoryId, units]) => {
                if (units.length === 0) return null;
                const centroid = territoryCentroids[territoryId];
                const territory = territoryData[territoryId];
                const hasStrongholdMarker = territory?.stronghold === true;
                const hasPowerBadge = Number(territory?.produces ?? 0) > 0;
                const screenPos = centroid
                  ? clampToMap(svgToScreen(centroid.x, centroid.y))
                  : fallbackPositionForTerritory(territoryId);
                const unitOffsetY = hasStrongholdMarker ? 40 : 0;
                // Strongholds/capitals: keep units higher so they don't fall off; other territories with power: push units down below badge
                const powerBadgeOffsetY = hasPowerBadge ? (hasStrongholdMarker ? 28 : 54) : 0;

                const NEUTRAL_UNIT_BORDER = '#888888';

                return (
                  <div
                    key={territoryId}
                    className="territory-units"
                    style={{
                      left: screenPos.x,
                      top: screenPos.y + unitOffsetY + powerBadgeOffsetY,
                    }}
                  >
                    {units.map(({ unit_id, count }) => {
                      // Border = unit's faction ONLY. Never territory owner. Prefer faction name found in unit_id (e.g. rider_of_rohan → rohan) so Rohan units get Rohan color even when def.faction is "freepeoples".
                      const parts = unit_id.split('_');
                      const factionFromId = parts.find(p => factionData[p]);
                      const colorFromId = factionFromId ? factionData[factionFromId].color : null;
                      const defFaction = unitDefs[unit_id]?.faction;
                      const colorFromDef = defFaction && factionData[defFaction] ? factionData[defFaction].color : null;
                      const unitFactionColor = colorFromId ?? colorFromDef ?? NEUTRAL_UNIT_BORDER;
                      // Draggable during movement phase if this unit belongs to the current faction (regardless of territory owner)
                      const unitFaction = factionFromId ?? defFaction ?? parts[0];
                      const canDrag = isMovementPhase && (unitFaction === gameState.current_faction);

                      return (
                        <DraggableUnit
                          key={`${territoryId}-${unit_id}`}
                          id={`${territoryId}-${unit_id}`}
                          unitId={unit_id}
                          territoryId={territoryId}
                          count={count}
                          unitDef={unitDefs[unit_id]}
                          isSelected={selectedUnit?.territory === territoryId && selectedUnit?.unitType === unit_id}
                          disabled={!canDrag}
                          factionColor={unitFactionColor}
                          showAerialMustMove={aerialMustMoveKeySet.has(`${territoryId}_${unit_id}`)}
                        />
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
        
        <DragOverlay activeUnit={activeUnit} activeMobilizationItem={activeMobilizationItem} activeCampDrag={activeCampDrag} factionColor={mobilizationTray?.factionColor} />
        
        <div className="map-controls">
          <div className="controls-row">
            <button onClick={panUp} title="Pan Up">▲</button>
          </div>
          <div className="controls-row">
            <button onClick={panLeft} title="Pan Left">◀</button>
            <button onClick={resetView} title="Reset View">⌂</button>
            <button onClick={panRight} title="Pan Right">▶</button>
          </div>
          <div className="controls-row">
            <button onClick={panDown} title="Pan Down">▼</button>
          </div>
          <div className="controls-row zoom-controls">
            <button onClick={zoomOut} title="Zoom Out">−</button>
            <button onClick={zoomIn} title="Zoom In">+</button>
          </div>
        </div>
        </div>

        {mobilizationTray && (
          <MobilizationTray
            isOpen={true}
            purchases={mobilizationTray.purchases}
            pendingCamps={mobilizationTray.pendingCamps}
            faction={gameState.current_faction}
            factionColor={mobilizationTray.factionColor}
            selectedUnitId={mobilizationTray.selectedUnitId}
            selectedCampIndex={mobilizationTray.selectedCampIndex}
            onSelectUnit={mobilizationTray.onSelectUnit}
            onSelectCamp={mobilizationTray.onSelectCamp}
            activeDragId={activeDragId}
          />
        )}
      </div>
    </DndContext>
  );
}

export default GameMap;
