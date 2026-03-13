import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { DndContext, useDroppable, rectIntersection, useSensors, useSensor, PointerSensor } from '@dnd-kit/core';
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import type { GameState, SelectedUnit, MapTransform, PendingMove } from '../types/game';
import DraggableUnit from './DraggableUnit';
import DragOverlay from './DragOverlay';
import MobilizationTray from './MobilizationTray';
import NavalTray, { type BoatInTray } from './NavalTray';
import './GameMap.css';

/** Compute stroke (darkened) and glow rgba from hex for cross-browser territory glow without color-mix(). */
function territoryGlowFromHex(hex: string): { stroke: string; glowRgba: string; glowRgbaSoft: string } {
  const h = (hex || '').replace(/^#/, '');
  if (!/^[0-9a-fA-F]{3}$|^[0-9a-fA-F]{6}$/.test(h)) {
    return { stroke: '#444', glowRgba: 'rgba(0,0,0,0.25)', glowRgbaSoft: 'rgba(0,0,0,0.15)' };
  }
  const r = h.length === 3 ? parseInt(h[0] + h[0], 16) : parseInt(h.slice(0, 2), 16);
  const g = h.length === 3 ? parseInt(h[1] + h[1], 16) : parseInt(h.slice(2, 4), 16);
  const b = h.length === 3 ? parseInt(h[2] + h[2], 16) : parseInt(h.slice(4, 6), 16);
  const sr = Math.round(r * 0.45);
  const sg = Math.round(g * 0.45);
  const sb = Math.round(b * 0.45);
  const stroke = `#${sr.toString(16).padStart(2, '0')}${sg.toString(16).padStart(2, '0')}${sb.toString(16).padStart(2, '0')}`;
  return {
    stroke,
    glowRgba: `rgba(${r},${g},${b},0.7)`,
    glowRgbaSoft: `rgba(${r},${g},${b},0.4)`,
  };
}

/** Normalize sea zone id so "sea_zone_9" and "sea_zone9" match (backend vs map/SVG). */
function canonicalSeaZoneId(tid: string): string {
  if (!tid || typeof tid !== 'string') return tid || '';
  const m = tid.trim().match(/^sea_zone_*(\d+)$/i);
  return m ? 'sea_zone' + m[1] : tid.trim();
}

/** Add destination to set and, for sea zones, add the alternate form (backend "sea_zone9" vs map "sea_zone_9") so drop/highlight works either way. */
function addDestinationWithSeaZoneAlias(set: Set<string>, d: string): void {
  set.add(d);
  const withUnderscore = d.match(/^sea_zone(\d+)$/i);
  const withoutUnderscore = d.match(/^sea_zone_(\d+)$/i);
  if (withUnderscore) set.add('sea_zone_' + withUnderscore[1]);
  if (withoutUnderscore) set.add('sea_zone' + withoutUnderscore[1]);
}

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
  /** When set (e.g. sea transport boat + passengers), use these instance IDs for the move instead of deriving from unitId + count. */
  instanceIds?: string[];
  /** When multiple boats with different passenger makeups: list of options (each option = [boatInstanceId, ...passengerInstanceIds]). User picks one. */
  boatOptions?: string[][];
  /** When user chose "Load into this boat" (one boat): passenger instance IDs go in move; this is the boat to load onto. */
  loadOntoBoatInstanceId?: string;
  /** When loading into 2+ boats: allocation of passenger instance ID -> boat instance ID (user can drag in tray). Used to submit one move per boat. */
  loadAllocation?: Record<string, string[]>;
  /** When offloading to land adjacent to multiple sea zones: list of sea zone IDs to choose from. User picks one. */
  offloadSeaZoneOptions?: string[];
  /** When sea raid (combat move sea→land): sea zone IDs from which raid can be conducted. User picks one (like charge path), then confirm. */
  seaRaidSeaZoneOptions?: string[];
  /** After user picked a sea zone from seaRaidSeaZoneOptions (multi), this is the chosen zone; then confirm panel is shown. */
  chosenSeaZoneId?: string;
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
    aerial_adjacent?: string[];
    hasCamp?: boolean;
    hasPort?: boolean;
    isCapital?: boolean;
    ownable?: boolean;
  }>;
  territoryUnits: Record<string, { unit_id: string; count: number; instances?: string[] }[]>;
  /** Full unit list per territory (for sea zones: boats + loaded_onto to show passenger count per boat). */
  territoryUnitsFull?: Record<string, { instance_id: string; unit_id: string; loaded_onto?: string | null }[]>;
  unitDefs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[]; home_territory_id?: string; home_territory_ids?: string[]; cost?: number }>;
  unitStats: Record<string, { movement: number }>;
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string; capital?: string }>;
  onTerritorySelect: (territoryId: string | null) => void;
  /** When provided (e.g. during movement phase), clicking the boat stack in a sea zone opens the naval tray. */
  onSeaZoneStackClick?: (territoryId: string) => void;
  onUnitSelect: (unit: SelectedUnit | null) => void;
  onUnitMove: (from: string, to: string, unitType: string, count: number) => void;
  isMovementPhase: boolean;
  isCombatMove: boolean;
  isMobilizePhase: boolean;
  hasMobilizationSelected: boolean;
  validMobilizeTerritories?: string[];
  /** Sea zone IDs valid for naval mobilization (adjacent to owned port). */
  validMobilizeSeaZones?: string[];
  /** Set of unit IDs that are naval (mobilize to sea zone, not territory). */
  navalUnitIds?: Set<string>;
  /** Per-territory/sea-zone remaining mobilization capacity (power minus pending). Used to only highlight destinations that have room. */
  remainingMobilizationCapacity?: Record<string, number>;
  /** Per-territory, per-unit remaining home slots (1 per home territory per unit type). Enables deploy to home without camp. */
  remainingHomeSlots?: Record<string, Record<string, number>>;
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
  /** Called with the drop destination as soon as a move drop is accepted. Ensures destination is never lost before confirm. */
  onDropDestination?: (territoryId: string) => void;
  pendingMoves: PendingMove[];
  highlightedTerritories?: string[];
  availableMoveTargets?: MoveableUnit[];
  /** Land territory_id -> sea zone IDs that can conduct a sea raid to it (from backend sea_raid_targets). When dropping naval on land, use this so user can pick which sea zone. */
  /** Aerial units that must move to friendly territory (from backend). Show caution icon on these. */
  aerialUnitsMustMove?: { territory_id: string; unit_id: string; instance_id: string }[];
  /** Boat instance IDs that received a load this combat move and must attack before ending phase. Show caution on those boats. */
  loadedNavalMustAttackInstanceIds?: string[];
  /** When set, show naval tray (boats + passengers) for the selected sea zone during movement phases. */
  navalTray?: { seaZoneId: string; seaZoneName: string; boats: BoatInTray[]; factionColor: string } | null;
  onCloseNavalTray?: () => void;
  /** When loading into a zone with multiple boats (different makeups), user chooses boat from tray. */
  pendingLoadBoatOptions?: string[][];
  onChooseBoatForLoad?: (instanceIds: string[]) => void;
  /** Pending load: passengers being allocated (for drag between boats in tray). */
  pendingLoadPassengers?: { instanceId: string; unitId: string; name: string; icon: string }[];
  /** Which boat each pending passenger instance ID is assigned to (boatInstanceId -> instanceIds[]). */
  loadAllocation?: Record<string, string[]>;
  onLoadAllocationChange?: (allocation: Record<string, string[]>) => void;
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

// Droppable territory: fill layer (receives clicks) + stroke layer (drawn on top so border shows on shared edges)
function DroppableTerritory({
  territoryId,
  pathData,
  color,
  isSeaZone,
  isSelected,
  isHighlighted,
  isValidDrop,
  onClick,
}: {
  territoryId: string;
  pathData: TerritoryPathData;
  color: string;
  isSeaZone: boolean;
  isSelected: boolean;
  isHighlighted: boolean;
  isValidDrop: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const tid = typeof territoryId === 'string' ? territoryId : (territoryId != null && typeof territoryId === 'object' && 'id' in (territoryId as object) ? String((territoryId as { id: string }).id) : (territoryId != null && typeof territoryId === 'object' && 'territoryId' in (territoryId as object) ? String((territoryId as { territoryId: string }).territoryId) : String(territoryId ?? '')));
  const { setNodeRef, isOver } = useDroppable({
    id: `territory-${tid}`,
    data: { territoryId: tid },
  });
  const pathClass = `territory-path ${isSeaZone ? 'territory-path--sea' : 'territory-path--svg-glow'} ${isSelected ? 'selected' : ''} ${isHighlighted || isValidDrop ? 'highlight' : ''} ${isOver && isValidDrop ? 'drop-target' : ''}`;
  const glowVars = territoryGlowFromHex(color);
  const glowFilterId = isSeaZone ? undefined : `territory-glow-${color.replace(/^#/, '')}`;
  const pathStyle = {
    ['--territory-glow' as string]: color,
    ['--territory-stroke' as string]: glowVars.stroke,
    ['--territory-glow-rgba' as string]: glowVars.glowRgba,
    ['--territory-glow-rgba-soft' as string]: glowVars.glowRgbaSoft,
  };
  return (
    <g aria-hidden>
      <path
        ref={setNodeRef as React.Ref<SVGPathElement>}
        id={`territory-${tid}`}
        d={pathData.d}
        transform={pathData.transform}
        fill={isSeaZone ? 'url(#sea-wave-pattern)' : color}
        stroke="none"
        style={pathStyle}
        className={`${pathClass} territory-path-fill`}
        onClick={onClick}
      />
      <path
        d={pathData.d}
        transform={pathData.transform}
        fill="none"
        style={pathStyle}
        className={`${pathClass} territory-path-stroke`}
        filter={glowFilterId ? `url(#${glowFilterId})` : undefined}
        pointerEvents="none"
      />
    </g>
  );
}

function GameMap({
  gameState,
  selectedTerritory,
  selectedUnit,
  territoryData,
  territoryUnits,
  territoryUnitsFull = {},
  unitDefs,
  unitStats,
  factionData,
  onTerritorySelect,
  onSeaZoneStackClick,
  onUnitSelect,
  onUnitMove: _onUnitMove,
  isMovementPhase,
  isCombatMove,
  isMobilizePhase,
  hasMobilizationSelected,
  validMobilizeTerritories = [],
  validMobilizeSeaZones = [],
  navalUnitIds = new Set<string>(),
  remainingMobilizationCapacity = {},
  remainingHomeSlots = {},
  onMobilizationDrop,
  onCampDrop,
  mobilizationTray,
  navalTray,
  onCloseNavalTray,
  pendingLoadBoatOptions,
  onChooseBoatForLoad,
  pendingLoadPassengers,
  loadAllocation,
  onLoadAllocationChange,
  pendingMoveConfirm: _pendingMoveConfirm,
  onDropDestination: _onDropDestination,
  onSetPendingMove,
  pendingMoves,
  highlightedTerritories = [],
  validCampTerritories = [],
  territoriesWithPendingCampPlacement = [],
  availableMoveTargets,
  aerialUnitsMustMove = [],
  loadedNavalMustAttackInstanceIds = [],
}: GameMapProps) {
  /** Unique territory colors for SVG glow filters (Safari doesn't render CSS drop-shadow on SVG). */
  const uniqueGlowColors = useMemo(() => {
    const s = new Set<string>(['#2d4258', '#d4c4a8', '#7a7a7a']);
    Object.values(factionData || {}).forEach((f: { color?: string }) => f?.color && s.add(f.color));
    return Array.from(s);
  }, [factionData]);

  const aerialMustMoveKeySet = useMemo(
    () => new Set((aerialUnitsMustMove ?? []).map(u => `${u.territory_id}_${u.unit_id}`)),
    [aerialUnitsMustMove]
  );
  const loadedNavalMustAttackInstanceIdSet = useMemo(
    () => new Set(loadedNavalMustAttackInstanceIds),
    [loadedNavalMustAttackInstanceIds]
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
  /** For larger territories: marker position (pp/logo/emojis) and unit position (stacks) can differ to avoid overlap. */
  const [territoryPositions, setTerritoryPositions] = useState<Record<string, { marker: { x: number; y: number }; unit: { x: number; y: number } }>>({});
  const [validDropTargets, setValidDropTargets] = useState<Set<string>>(new Set());
  const [activeUnit, setActiveUnit] = useState<{
    unitId: string;
    territoryId: string;
    count: number;
    unitDef?: { name: string; icon: string };
    factionColor?: string;
    isNaval?: boolean;
    instanceIds?: string[];
    passengerCount?: number;
  } | null>(null);
  const [activeDragId, setActiveDragId] = useState<string | null>(null);
  const [mapControlsCollapsed, setMapControlsCollapsed] = useState(false);
  const [mapKeyOpen, setMapKeyOpen] = useState(false);

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

  // Use two spots (marker + unit) when territory is large enough in any dimension: by area (big blobs) or by
  // width/height (long thin territories). Kept high so circular capitals stay single-spot.
  const TERRITORY_TWO_SPOT_AREA_THRESHOLD = 100000;
  const TERRITORY_TWO_SPOT_WIDTH_THRESHOLD = 250;
  const TERRITORY_TWO_SPOT_HEIGHT_THRESHOLD = 250;

  // Calculate marker and unit positions: pole of inaccessibility; for large territories, a second pole for units
  useEffect(() => {
    if (svgPaths.size === 0 || !svgRef.current) {
      setTerritoryCentroids({});
      setTerritoryPositions({});
      return;
    }

    const computeCentroidsAndPositions = (): { centroids: Record<string, { x: number; y: number }>; positions: Record<string, { marker: { x: number; y: number }; unit: { x: number; y: number } }> } => {
      const centroids: Record<string, { x: number; y: number }> = {};
      const positions: Record<string, { marker: { x: number; y: number }; unit: { x: number; y: number } }> = {};
      const bboxCenter = (b: DOMRect) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 });

      // First pass: from path data (temp SVG) so every territory has a fallback
      svgPaths.forEach((pathData, territoryId) => {
        const known = OSGILIATH_CENTROIDS[territoryId];
        if (known) {
          centroids[territoryId] = known;
          positions[territoryId] = { marker: known, unit: known };
          return;
        }
        let tmp: SVGSVGElement | null = null;
        try {
          const viewBox = { w: SVG_VIEWBOX.width, h: SVG_VIEWBOX.height };
          tmp = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
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
          if (!Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height)) {
            document.body.removeChild(tmp);
            return;
          }
          const area = bbox.width * bbox.height;
          const isSeaZone = /^sea_zone\d*$/i.test(territoryId);
          const useTwoSpotsFirstPass =
            isSeaZone ||
            area >= TERRITORY_TWO_SPOT_AREA_THRESHOLD ||
            bbox.width >= TERRITORY_TWO_SPOT_WIDTH_THRESHOLD ||
            bbox.height >= TERRITORY_TWO_SPOT_HEIGHT_THRESHOLD;
          if (useTwoSpotsFirstPass) {
            try {
              const cx = bbox.x + bbox.width / 2;
              const cy = bbox.y + bbox.height / 2;
              const svg = path.ownerSVGElement;
              if (svg) {
                const pt = svg.createSVGPoint();
                const isInside = (x: number, y: number) => {
                  pt.x = x;
                  pt.y = y;
                  return path.isPointInFill(pt);
                };
                const boundaryPts: { x: number; y: number }[] = [];
                const totalLen = path.getTotalLength();
                const numSamples = Math.min(50, Math.max(20, Math.floor(totalLen / 15)));
                for (let k = 0; k < numSamples; k++) {
                  const p = path.getPointAtLength((k * totalLen) / numSamples);
                  boundaryPts.push({ x: p.x, y: p.y });
                }
                const distToBoundary = (x: number, y: number) => {
                  let min = Infinity;
                  for (const b of boundaryPts) {
                    const d = (x - b.x) ** 2 + (y - b.y) ** 2;
                    if (d < min) min = d;
                  }
                  return Math.sqrt(min);
                };
                const dist = (a: { x: number; y: number }, b: { x: number; y: number }) =>
                  Math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2);
                const gridSteps = 12;
                let bestSpot = { x: cx, y: cy, d: 0 };
                for (let i = 0; i <= gridSteps; i++) {
                  for (let j = 0; j <= gridSteps; j++) {
                    const x = bbox.x + (bbox.width * i) / gridSteps;
                    const y = bbox.y + (bbox.height * j) / gridSteps;
                    if (isInside(x, y)) {
                      const d = distToBoundary(x, y);
                      if (d > bestSpot.d) bestSpot = { x, y, d };
                    }
                  }
                }
                const unit = { x: bestSpot.x, y: bestSpot.y };
                let bestMarker = { x: unit.x, y: unit.y, score: 0 };
                for (let i = 0; i <= gridSteps; i++) {
                  for (let j = 0; j <= gridSteps; j++) {
                    const x = bbox.x + (bbox.width * i) / gridSteps;
                    const y = bbox.y + (bbox.height * j) / gridSteps;
                    if (isInside(x, y)) {
                      const toBoundary = distToBoundary(x, y);
                      const toUnit = dist(unit, { x, y });
                      const score = Math.min(toBoundary, toUnit);
                      if (score > bestMarker.score) bestMarker = { x, y, score };
                    }
                  }
                }
                const marker = { x: bestMarker.x, y: bestMarker.y };
                const markerUnitDist = dist(marker, unit);
                if (isSeaZone && markerUnitDist < 2) {
                  // Sea zone: two spots ended up same/overlapping — line them up along bbox
                  const dx = bbox.width >= bbox.height ? Math.max(8, bbox.width * 0.15) : 0;
                  const dy = bbox.width < bbox.height ? Math.max(8, bbox.height * 0.15) : 0;
                  const c = bboxCenter(bbox);
                  positions[territoryId] = {
                    marker: { x: c.x - dx, y: c.y - dy },
                    unit: { x: c.x + dx, y: c.y + dy },
                  };
                  centroids[territoryId] = { x: c.x + dx, y: c.y + dy };
                } else {
                  centroids[territoryId] = unit;
                  positions[territoryId] = { marker, unit };
                }
              } else {
                const c = bboxCenter(bbox);
                if (isSeaZone) {
                  const dx = bbox.width >= bbox.height ? Math.max(8, bbox.width * 0.15) : 0;
                  const dy = bbox.width < bbox.height ? Math.max(8, bbox.height * 0.15) : 0;
                  positions[territoryId] = {
                    marker: { x: c.x - dx, y: c.y - dy },
                    unit: { x: c.x + dx, y: c.y + dy },
                  };
                  centroids[territoryId] = { x: c.x + dx, y: c.y + dy };
                } else {
                  centroids[territoryId] = c;
                  positions[territoryId] = { marker: c, unit: c };
                }
              }
            } catch {
              const c = bboxCenter(bbox);
              if (isSeaZone) {
                const dx = Math.max(8, bbox.width * 0.15);
                const dy = Math.max(8, bbox.height * 0.15);
                const horizontal = bbox.width >= bbox.height;
                positions[territoryId] = horizontal
                  ? { marker: { x: c.x - dx, y: c.y }, unit: { x: c.x + dx, y: c.y } }
                  : { marker: { x: c.x, y: c.y - dy }, unit: { x: c.x, y: c.y + dy } };
                centroids[territoryId] = horizontal ? { x: c.x + dx, y: c.y } : { x: c.x, y: c.y + dy };
              } else {
                centroids[territoryId] = c;
                positions[territoryId] = { marker: c, unit: c };
              }
            }
          } else {
            const c = bboxCenter(bbox);
            centroids[territoryId] = c;
            positions[territoryId] = { marker: c, unit: c };
          }
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

      const svg = svgRef.current;
      if (!svg) return { centroids, positions };

      const pathElements = svg.querySelectorAll('path[id^="territory-"]');
      pathElements?.forEach((pathEl) => {
        const path = pathEl as SVGPathElement;
        const id = path.id?.replace('territory-', '');
        if (!id) return;
        try {
          if (path.getAttribute('transform')) return;
          const bbox = path.getBBox();
          const area = bbox.width * bbox.height;
          const cx = bbox.x + bbox.width / 2;
          const cy = bbox.y + bbox.height / 2;
          const pt = svg.createSVGPoint();
          const isInside = (x: number, y: number) => {
            pt.x = x;
            pt.y = y;
            return path.isPointInFill(pt);
          };
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
          const dist = (a: { x: number; y: number }, b: { x: number; y: number }) =>
            Math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2);

          const gridSteps = 12;
          // Pole of inaccessibility = point with most room (max distance to boundary); assign to units
          let bestSpot = { x: cx, y: cy, d: 0 };
          for (let i = 0; i <= gridSteps; i++) {
            for (let j = 0; j <= gridSteps; j++) {
              const x = bbox.x + (bbox.width * i) / gridSteps;
              const y = bbox.y + (bbox.height * j) / gridSteps;
              if (isInside(x, y)) {
                const d = distToBoundary(x, y);
                if (d > bestSpot.d) bestSpot = { x, y, d };
              }
            }
          }
          if (bestSpot.d <= 0) {
            if (isInside(cx, cy)) bestSpot = { x: cx, y: cy, d: 0 };
            else {
              for (let i = 1; i < 8; i++) {
                for (let j = 1; j < 8; j++) {
                  const x = bbox.x + (bbox.width * i) / 8;
                  const y = bbox.y + (bbox.height * j) / 8;
                  if (isInside(x, y)) {
                    bestSpot = { x, y, d: 0 };
                    break;
                  }
                }
              }
            }
          }
          const unit = { x: bestSpot.x, y: bestSpot.y };
          centroids[id] = unit;

          let marker: { x: number; y: number };
          const useTwoSpots =
            area >= TERRITORY_TWO_SPOT_AREA_THRESHOLD ||
            bbox.width >= TERRITORY_TWO_SPOT_WIDTH_THRESHOLD ||
            bbox.height >= TERRITORY_TWO_SPOT_HEIGHT_THRESHOLD;
          if (useTwoSpots) {
            let bestMarker = { x: unit.x, y: unit.y, score: 0 };
            for (let i = 0; i <= gridSteps; i++) {
              for (let j = 0; j <= gridSteps; j++) {
                const x = bbox.x + (bbox.width * i) / gridSteps;
                const y = bbox.y + (bbox.height * j) / gridSteps;
                if (isInside(x, y)) {
                  const toBoundary = distToBoundary(x, y);
                  const toUnit = dist(unit, { x, y });
                  const score = Math.min(toBoundary, toUnit);
                  if (score > bestMarker.score) bestMarker = { x, y, score };
                }
              }
            }
            marker = { x: bestMarker.x, y: bestMarker.y };
          } else {
            marker = unit;
          }
          positions[id] = { marker, unit };
        } catch {
          try {
            const bbox = path.getBBox();
            if (id && Number.isFinite(bbox.x + bbox.y + bbox.width + bbox.height)) {
              const c = bboxCenter(bbox);
              centroids[id] = c;
              positions[id] = { marker: c, unit: c };
            }
          } catch {
            /* ignore */
          }
        }
      });

      return { centroids, positions };
    };

    let timer2: ReturnType<typeof setTimeout> | null = null;
    const run = () => {
      const { centroids, positions } = computeCentroidsAndPositions();
      setTerritoryCentroids(centroids);
      setTerritoryPositions(positions);
      if (Object.keys(centroids).length < svgPaths.size) {
        timer2 = setTimeout(run, 200);
      }
    };
    const timer1 = setTimeout(run, 200);

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
      isLoad?: boolean;
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
          isLoad: move.move_type === 'load',
          count: 0,
        };
      }
      moveGroups[key].count += move.count;
      if (move.move_type === 'load') moveGroups[key].isLoad = true;
    });

    const isSea = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone\d*$/i.test(tid);

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

      const isLoad = group.isLoad ?? (!isSea(group.from) && isSea(group.to));
      return {
        ...group,
        isLoad,
        startX,
        startY,
        endX,
        endY,
        midX: (startX + endX) / 2,
        midY: (startY + endY) / 2,
      };
    }).filter(Boolean);
  }, [pendingMoves, territoryCentroids, gameState.phase, territoryData]);

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
    const fromCanon = canonicalSeaZoneId(fromTerritory);
    const matches = availableMoveTargets
      ? availableMoveTargets.filter(
          m => (canonicalSeaZoneId(m.territory) === fromCanon || m.territory === fromTerritory) && m.unit.unit_id === unitId
        )
      : [];
    let movement: number;
    if (availableMoveTargets && matches.length > 0) {
      // Use UNION of destinations so we show all targets any unit in the stack can reach (max range).
      // User can then drop on a far territory and the confirm popup only allows moving as many units as can reach it.
      const validTargets = new Set<string>();
      for (const m of matches) {
        for (const d of (m.destinations || [])) {
          addDestinationWithSeaZoneAlias(validTargets, d);
        }
      }
      if (validTargets.size > 0) {
        // Naval: allow dropping on land adjacent to any reachable sea zone (sea raid in combat_move, offload in non_combat_move); user will pick which sea zone.
        if ((gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move') && navalUnitIds?.has(unitId)) {
          const isSeaT = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone\d*$/i.test(tid);
          const seaZoneIds = [...validTargets].filter(isSeaT);
          for (const sz of seaZoneIds) {
            for (const adjId of (territoryData[sz]?.adjacent || [])) {
              if (!territoryData[adjId] || isSeaT(adjId)) continue;
              addDestinationWithSeaZoneAlias(validTargets, adjId);
            }
          }
        }
        return validTargets;
      }
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

    // Neighbors for BFS: adjacent + aerial_adjacent when unit is aerial (deduped)
    const getNeighborIds = (t: { adjacent?: string[]; aerial_adjacent?: string[] } | undefined): string[] => {
      if (!t) return [];
      const adj = t.adjacent ?? [];
      if (!isAerial) return adj;
      const extra = t.aerial_adjacent ?? [];
      const seen = new Set(adj);
      const out = [...adj];
      for (const id of extra) {
        if (!seen.has(id)) {
          seen.add(id);
          out.push(id);
        }
      }
      return out;
    };

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
        for (const adjId of getNeighborIds(t)) {
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

      for (const adjId of getNeighborIds(current)) {
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
  }, [availableMoveTargets, territoryData, territoryUnits, unitDefs, unitStats, gameState.current_faction, gameState.phase, isCombatMove, getAlliance, navalUnitIds]);

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
      const unitId = (data as { unitId?: string }).unitId;
      const isNaval = unitId ? navalUnitIds.has(unitId) : false;
      const validDestinations = isNaval ? validMobilizeSeaZones : validMobilizeTerritories;
      const withRoom = validDestinations.filter((id: string) => {
        if (isNaval) return (remainingMobilizationCapacity[id] ?? 0) > 0;
        const campRoom = (remainingMobilizationCapacity[id] ?? 0) > 0;
        const homeRoom = unitId ? (remainingHomeSlots[id]?.[unitId] ?? 0) > 0 : false;
        return campRoom || homeRoom;
      });
      setValidDropTargets(new Set(withRoom));
      return;
    }
    const { unitId, territoryId, count, unitDef, factionColor, instanceIds, passengerCount } = data as {
      unitId: string;
      territoryId: string;
      count: number;
      unitDef?: { name: string; icon: string };
      factionColor?: string;
      instanceIds?: string[];
      passengerCount?: number;
    };
    setActiveUnit({ unitId, territoryId, count, unitDef, factionColor, isNaval: navalUnitIds.has(unitId), instanceIds, passengerCount: passengerCount ?? 0 });
    setValidDropTargets(getValidTargets(territoryId, unitId));
  }, [getValidTargets, validMobilizeTerritories, validMobilizeSeaZones, navalUnitIds, remainingMobilizationCapacity, remainingHomeSlots, mobilizationTray?.pendingCamps, territoriesWithPendingCampPlacement, territoryData]);

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
        const campRemaining = remainingMobilizationCapacity[targetTerritory] ?? 0;
        const homeRemaining = unitId ? (remainingHomeSlots[targetTerritory]?.[unitId] ?? 0) : 0;
        const cappedCount = campRemaining > 0
          ? Math.min(count, campRemaining)
          : homeRemaining > 0
            ? Math.min(count, 1)
            : 0;
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
      const overId = over.id != null ? String(over.id) : '';
      const raw = (over.data.current as { territoryId?: unknown })?.territoryId;
      const targetFromData =
        typeof raw === 'string'
          ? raw.trim()
          : raw != null && typeof raw === 'object' && 'territoryId' in (raw as object)
            ? String((raw as { territoryId: string }).territoryId).trim()
            : '';
      const targetFromId = overId.startsWith('territory-') ? overId.slice('territory-'.length).trim() : '';
      const targetTerritory = (targetFromData && targetFromData !== '[object Object]') ? targetFromData : (targetFromId && targetFromId !== '[object Object]' ? targetFromId : '');
      const isBadDest = !targetTerritory || targetTerritory === '[object Object]' || targetTerritory.includes('object');
      let storeTarget = (targetTerritory && !isBadDest) ? targetTerritory : '';
      // Resolve map/SVG territory id to backend territory id (e.g. SVG "sea_zone_11" -> backend "sea_zone11") so we always store and send an id the backend knows
      const resolveToBackendId = (tid: string): string => {
        if (!tid || typeof tid !== 'string') return '';
        const t = tid.trim();
        if (!t) return '';
        if (territoryData[t]) return t;
        const seaMatch = t.match(/^sea_zone_*(\d+)$/i);
        if (seaMatch) {
          const canonical = 'sea_zone' + seaMatch[1];
          if (territoryData[canonical]) return canonical;
        }
        return t;
      };
      const backendDestId = resolveToBackendId(storeTarget);
      // Only accept drop on a valid destination (backend reachability). Invalid = no dialog, units snap back.
      const resolvedId = backendDestId || storeTarget;
      const isValidDest = validDropTargets.has(resolvedId) || validDropTargets.has(storeTarget);
      const dropAccepted = !!storeTarget && isValidDest;
      if (dropAccepted) {
        const destToStash = resolvedId.trim();
        if (destToStash) _onDropDestination?.(destToStash);
        storeTarget = backendDestId || storeTarget; // prefer backend id so confirm sends backend-canonical value (e.g. sea_zone11)
        const isSeaT = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone\d*$/i.test(tid);
        // Offload / sea raid (naval, drop on land): find all sea zones adjacent to the land that the boat can reach (current zone or any destination). Include hostile zones so user can choose. Only show zone picker when multiple options.
        let effectiveToTerritory = storeTarget;
        let seaRaidSeaZoneOptions: string[] | undefined;
        let storeToTerritory = storeTarget;
        const isOffloadOrSeaRaid = (gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move') && activeUnit.isNaval && !isSeaT(storeTarget);
        if (isOffloadOrSeaRaid) {
          const destSet = new Set<string>();
          const currentSeaRaw = typeof activeUnit.territoryId === 'string' ? activeUnit.territoryId : '';
          const currentSeaCanon = canonicalSeaZoneId(currentSeaRaw);
          if (availableMoveTargets) {
            const mms = availableMoveTargets.filter(
              m =>
                (canonicalSeaZoneId(m.territory) === currentSeaCanon || m.territory === currentSeaRaw) &&
                m.unit.unit_id === activeUnit.unitId
            );
            for (const m of mms) for (const d of (m.destinations || [])) destSet.add(canonicalSeaZoneId(d));
          }
          const adj = territoryData[storeTarget]?.adjacent || [];
          const seaZonesAdjacentToLand = adj.filter((id: string) => isSeaT(id));
          const reachable = new Set<string>([currentSeaCanon, ...destSet]);
          const optionsCanon = seaZonesAdjacentToLand
            .map((sz: string) => canonicalSeaZoneId(sz))
            .filter((sz: string) => reachable.has(sz));
          const options = [...new Set(optionsCanon)];
          if (options.length >= 1) {
            effectiveToTerritory = options.find((sz) => destSet.has(sz)) ?? options[0];
            storeToTerritory = storeTarget;
            seaRaidSeaZoneOptions = options;
          }
        }
        // Max count = units in territory of this type minus already committed in other pending moves
        const totalInTerritory = (territoryUnits[activeUnit.territoryId] || [])
          .filter(u => u.unit_id === activeUnit.unitId)
          .reduce((s, u) => s + u.count, 0);
        const committedElsewhere = pendingMoves
          .filter(m => m.from === activeUnit.territoryId && m.unitType === activeUnit.unitId)
          .reduce((s, m) => s + m.count, 0);
        let availableCount = Math.max(0, totalInTerritory - committedElsewhere);
        // Show confirm dialog whenever we have units to move; backend will validate reachability on submit.
        if (availableCount <= 0) {
          setActiveUnit(null);
          setActiveDragId(null);
          setValidDropTargets(new Set());
          return;
        }
        const fromCanon = canonicalSeaZoneId(activeUnit.territoryId);
        const destIncludes = (dests: string[] | undefined, tid: string) =>
          (dests ?? []).some(d => d === tid || canonicalSeaZoneId(d) === canonicalSeaZoneId(tid));
        const matches = (availableMoveTargets ?? []).filter(
          m =>
            (canonicalSeaZoneId(m.territory) === fromCanon || m.territory === activeUnit.territoryId) &&
            m.unit.unit_id === activeUnit.unitId &&
            destIncludes(m.destinations, effectiveToTerritory)
        );
        // Prefer the move entry that has the most charge path options for this destination (in case multiple units match)
        const match = matches.length > 0
          ? matches.reduce((best, m) => {
            const cr = m.charge_routes && typeof m.charge_routes === 'object' && !Array.isArray(m.charge_routes) ? m.charge_routes : {};
            const raw = cr[effectiveToTerritory];
            const n = Array.isArray(raw) ? raw.length : 0;
            const bestRaw = best?.charge_routes && typeof best.charge_routes === 'object' ? best.charge_routes[effectiveToTerritory] : undefined;
            const bestN = Array.isArray(bestRaw) ? bestRaw.length : 0;
            return n >= bestN ? m : best;
          })
          : undefined;
        const rawPaths = match?.charge_routes && typeof match.charge_routes === 'object' ? match.charge_routes[effectiveToTerritory] : undefined;
        const paths = Array.isArray(rawPaths) ? rawPaths : [];
        const singlePath = paths.length === 1 ? paths[0] : undefined;
        const chargeThrough = singlePath?.length ? singlePath : (paths[0]?.length ? paths[0] : undefined);
        const useInstanceIds = activeUnit.instanceIds;
        const moveCount = useInstanceIds ? useInstanceIds.length : Math.min(activeUnit.count, availableCount);
        const moveMaxCount = useInstanceIds ? useInstanceIds.length : availableCount;
        let boatOptions: string[][] | undefined;
        let instanceIdsToUse: string[] | undefined = useInstanceIds && useInstanceIds.length > 0 ? useInstanceIds : undefined;
        if (instanceIdsToUse && activeUnit.isNaval && territoryUnitsFull?.[activeUnit.territoryId]) {
          const fullUnits = territoryUnitsFull[activeUnit.territoryId];
          const boatsOfType = fullUnits.filter(u => u.unit_id === activeUnit.unitId && navalUnitIds.has(u.unit_id));
          const options = boatsOfType.map(boat => {
            const passengers = fullUnits.filter(u => u.loaded_onto === boat.instance_id);
            return [boat.instance_id, ...passengers.map(p => p.instance_id)];
          });
          if (options.length > 1) {
            const sameMakeup = options.every(op => op.length === options[0].length);
            if (sameMakeup) {
              instanceIdsToUse = options[0];
            } else {
              boatOptions = options;
              instanceIdsToUse = options[0];
            }
          }
        }
        // Load (land -> sea): when destination has 2+ boats, build boatOptions so tray can show allocation / "Load into this boat"
        if (instanceIdsToUse && !activeUnit.isNaval && territoryUnitsFull?.[storeTarget] && isSeaT(storeTarget)) {
          const fullUnits = territoryUnitsFull[storeTarget];
          const boats = fullUnits.filter(u => navalUnitIds.has(u.unit_id));
          if (boats.length >= 2) {
            boatOptions = boats.map(boat => [boat.instance_id, ...instanceIdsToUse]);
          }
        }
        const fromId = typeof activeUnit.territoryId === 'string' ? activeUnit.territoryId : String((activeUnit.territoryId as { id?: string; territoryId?: string })?.territoryId ?? (activeUnit.territoryId as { id?: string })?.id ?? activeUnit.territoryId);
        const toStrStored = (typeof storeToTerritory === 'string' ? storeToTerritory : String((storeToTerritory as { id?: string; territoryId?: string })?.territoryId ?? (storeToTerritory as { id?: string })?.id ?? storeToTerritory)).trim();
        if (toStrStored === '[object Object]' || !toStrStored) return; // never store bad destination
        _onDropDestination?.(toStrStored);
        onSetPendingMove({
          fromTerritory: fromId,
          toTerritory: toStrStored,
          unitId: activeUnit.unitId,
          unitDef: activeUnit.unitDef,
          maxCount: moveMaxCount,
          count: moveCount,
          chargeThrough: paths.length <= 1 ? chargeThrough : undefined,
          chargePathOptions: paths.length > 1 ? paths : undefined,
          ...(instanceIdsToUse && instanceIdsToUse.length > 0 ? { instanceIds: instanceIdsToUse } : {}),
          ...(boatOptions && boatOptions.length > 1 ? { boatOptions } : {}),
          ...(seaRaidSeaZoneOptions && seaRaidSeaZoneOptions.length >= 1 ? { seaRaidSeaZoneOptions } : {}),
        });
      }
    }
    setActiveUnit(null);
    setActiveDragId(null);
    setValidDropTargets(new Set());
  }, [activeUnit, validDropTargets, territoryUnits, territoryUnitsFull, pendingMoves, availableMoveTargets, navalUnitIds, onSetPendingMove, _onDropDestination, onMobilizationDrop, onCampDrop, gameState.phase, territoryData]);

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
      target.classList?.contains('map-art-on-top') ||
      target.classList?.contains('map-inner') ||
      target.classList?.contains('map-svg');
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
    if (target.closest('.territory-units--sea-stack')) return; // Don't start pan when clicking boat stack (opens naval tray)

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
      collisionDetection={rectIntersection}
    >
      <div className={`map-container ${mobilizationTray || navalTray ? 'map-container--with-tray' : ''}`}>
        <div className="map-content">
          {mapKeyOpen && (
            <div className="map-key-strip" role="region" aria-label="Map key">
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⛺</span> Camp</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>🌲</span> Forest</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>🏰</span> Fortress</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>🏠</span> Home</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⛰️</span> Mountains</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⚓</span> Port</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden></span> Strongholds have faction logo (capitals larger)</span>
            </div>
          )}
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
                width: IMG_DIMENSIONS.width + 16,
                height: IMG_DIMENSIONS.height + 16,
                transform: `translate(${transform.x - 8}px, ${transform.y - 8}px) scale(${transform.scale})`,
              }}
            >
              <div
                className="map-inner-content"
                style={{
                  width: IMG_DIMENSIONS.width,
                  height: IMG_DIMENSIONS.height,
                }}
              >
                <svg
                  ref={svgRef}
                  className="map-svg"
                  width={IMG_DIMENSIONS.width}
                  height={IMG_DIMENSIONS.height}
                  viewBox={`0 0 ${SVG_VIEWBOX.width} ${SVG_VIEWBOX.height}`}
                  preserveAspectRatio="none"
                >
                  {Array.from(svgPaths.entries())
                    .sort(([idA], [idB]) => {
                      const seaA = territoryData[idA]?.terrain === 'sea';
                      const seaB = territoryData[idB]?.terrain === 'sea';
                      if (seaA === seaB) return 0;
                      return seaA ? 1 : -1;
                    })
                    .map(([tid, pathData]) => {
                      const territoryId = typeof tid === 'string' ? tid : (tid != null && typeof tid === 'object' && 'id' in (tid as object) ? String((tid as { id: string }).id) : (tid != null && typeof tid === 'object' && 'territoryId' in (tid as object) ? String((tid as { territoryId: string }).territoryId) : String(tid ?? '')));
                      if (!territoryId || territoryId === '[object Object]') return null;
                      const territory = territoryData[territoryId];
                      const owner = territory?.owner;
                      const isNonOwnable = territory && (territory.ownable === false);
                      const isSeaZone = territory?.terrain === 'sea' || /^sea_zone\d*$/i.test(territoryId);
                      const color = isSeaZone
                        ? '#2d4258'
                        : owner
                          ? factionData[owner]?.color
                          : isNonOwnable
                            ? '#7a7a7a'
                            : '#d4c4a8';
                      const isSelected = selectedTerritory === territoryId;
                      const isValidDrop = validDropTargets.has(territoryId);
                      const hasUnitsToMobilize = (mobilizationTray?.purchases?.length ?? 0) > 0;
                      const selectedLandUnitId = mobilizationTray?.selectedUnitId && !navalUnitIds.has(mobilizationTray.selectedUnitId) ? mobilizationTray.selectedUnitId : null;
                      const hasMobilizationRoom = (remainingMobilizationCapacity[territoryId] ?? 0) > 0
                        || (selectedLandUnitId ? (remainingHomeSlots[territoryId]?.[selectedLandUnitId] ?? 0) > 0 : Object.values(remainingHomeSlots[territoryId] ?? {}).some((n: number) => n > 0));
                      const isValidMobilizationTarget = isMobilizePhase && validMobilizeTerritories.includes(territoryId) && hasMobilizationRoom && (activeDragId != null ? isValidDrop : (hasMobilizationSelected || hasUnitsToMobilize));
                      const isExternallyHighlighted = highlightedTerritories.includes(territoryId);
                      const isCampPlacementTarget = isMobilizePhase && (validCampTerritories.length > 0 && validCampTerritories.includes(territoryId) || validDropTargets.has(territoryId));
                      const isValidMobilizationTargetSea = isMobilizePhase && validMobilizeSeaZones.includes(territoryId) && hasMobilizationRoom && (activeDragId != null ? isValidDrop : (hasMobilizationSelected || (mobilizationTray?.purchases?.length ?? 0) > 0));
                      return (
                        <DroppableTerritory
                          key={territoryId}
                          territoryId={territoryId}
                          pathData={pathData}
                          color={color}
                          isSeaZone={!!isSeaZone}
                          isSelected={isSelected}
                          isHighlighted={isValidMobilizationTarget || isValidMobilizationTargetSea || isExternallyHighlighted || isCampPlacementTarget}
                          isValidDrop={isValidDrop || isValidMobilizationTarget || isValidMobilizationTargetSea || isExternallyHighlighted}
                          onClick={(e) => handleTerritoryClick(territoryId, e)}
                        />
                      );
                    })}
                  <defs>
                    {/* Native SVG glow filters so Safari shows the halo (Safari ignores CSS drop-shadow on SVG) */}
                    {uniqueGlowColors.map((hex) => {
                      const { glowRgba } = territoryGlowFromHex(hex);
                      const id = `territory-glow-${hex.replace(/^#/, '')}`;
                      return (
                        <filter key={id} id={id} x="-50%" y="-50%" width="200%" height="200%">
                          <feGaussianBlur in="SourceGraphic" stdDeviation="2" result="blur" />
                          <feFlood floodColor={glowRgba} result="flood" />
                          <feComposite in2="blur" in="flood" operator="in" result="coloredBlur" />
                          <feMerge>
                            <feMergeNode in="coloredBlur" />
                            <feMergeNode in="SourceGraphic" />
                          </feMerge>
                        </filter>
                      );
                    })}
                    {/* Ocean wave texture for sea zones: base blue with repeating wave curves */}
                    <pattern id="sea-wave-pattern" x="0" y="0" width="120" height="60" patternUnits="userSpaceOnUse">
                      <rect width="120" height="60" fill="#2d4258" />
                      {/* Lighter wave crests */}
                      <path d="M0 20 Q30 12 60 20 T120 20 M0 45 Q30 37 60 45 T120 45" fill="none" stroke="rgba(120,160,200,0.42)" strokeWidth="3.5" strokeLinecap="round" />
                      <path d="M0 32 Q25 26 50 32 T100 32 T120 32" fill="none" stroke="rgba(160,195,220,0.32)" strokeWidth="2.25" strokeLinecap="round" />
                      {/* Darker troughs for depth */}
                      <path d="M0 38 Q30 44 60 38 T120 38" fill="none" stroke="rgba(15,30,45,0.52)" strokeWidth="2.75" strokeLinecap="round" />
                    </pattern>
                    <marker id="arrowhead-combat" markerWidth="4" markerHeight="4" refX="3" refY="2" orient="auto">
                      <polygon points="0,0 4,2 0,4" fill="#c62828" />
                    </marker>
                    <marker id="arrowhead-move" markerWidth="4" markerHeight="4" refX="3" refY="2" orient="auto">
                      <polygon points="0,0 4,2 0,4" fill="#2e7d32" />
                    </marker>
                  </defs>
                  {moveArrows.map((arrow, idx) => arrow && (
                    <g key={`arrow-${idx}`}>
                      <line
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
                      {arrow.isLoad && (
                        <text
                          x={arrow.midX}
                          y={arrow.midY}
                          textAnchor="middle"
                          dominantBaseline="middle"
                          className="move-arrow-load-emoji"
                          fontSize="14"
                        >
                          ⚓
                        </text>
                      )}
                    </g>
                  ))}
                </svg>

                {/* Map art PNG on top of territory colors so artwork (mountains, labels, etc.) is in front. Use a PNG with white/background areas made transparent so territory fill shows through; pointer-events: none so clicks hit the SVG. */}
                <img
                  key={mapBase}
                  className="map-art-on-top"
                  src={`${imageUrl}?v=1`}
                  alt=""
                  aria-hidden
                  draggable={false}
                  onError={handleBgImageError}
                  style={{
                    width: IMG_DIMENSIONS.width,
                    height: IMG_DIMENSIONS.height,
                    objectFit: 'fill',
                    display: 'block',
                  }}
                />

                {/* Optional overlay: details (mountains, bridges, etc.) on top of territory colors; pointer-events: none so clicks hit the SVG below */}
                <img
                  key={`${mapBase}-overlay`}
                  className="map-overlay"
                  src={`/${mapBase}_overlay.png`}
                  alt=""
                  aria-hidden
                  draggable={false}
                  onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
                  style={{
                    width: IMG_DIMENSIONS.width,
                    height: IMG_DIMENSIONS.height,
                    objectFit: 'fill',
                  }}
                />

                {/* Territory markers (camps, strongholds) and power production badges). Requires territory in game state (e.g. create new game for east/west Osgiliath if missing). */}
                <div className="territory-markers-layer">
                  {Object.keys(territoryCentroids).map((territoryId) => {
                    const markerPos = territoryPositions[territoryId]?.marker ?? territoryCentroids[territoryId];
                    const territory = territoryData[territoryId];
                    if (!markerPos || !territory) return null;
                    const screenPos = svgToScreen(markerPos.x, markerPos.y);
                    const hasCamp = territory.hasCamp === true;
                    const hasPort = territory.hasPort === true;
                    const hasHome = Object.values(unitDefs).some((def) => {
                      const single = def.home_territory_id != null ? [def.home_territory_id] : [];
                      const multi = def.home_territory_ids ?? [];
                      const ids = [...new Set([...single, ...multi])] as string[];
                      if (ids.length === 0 || !ids.includes(territoryId)) return false;
                      return !territory.owner || def.faction === territory.owner;
                    });
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
                    const isSeaZone = territory.terrain === 'sea' || /^sea_zone\d*$/i.test(territoryId);
                    const seaZoneNum = isSeaZone ? (territoryId.match(/(\d+)$/)?.[1] ?? '') : null;
                    const terrainType = (territory.terrain || '').toLowerCase();
                    const showTerrainFortress = terrainType === 'fortress' && !showStronghold;
                    const showTerrainMountain = terrainType === 'mountains';
                    const showTerrainForest = terrainType === 'forest';
                    const showTerrainIcon = showTerrainFortress || showTerrainMountain || showTerrainForest;
                    // Inline row when we have power and any of camp/port/home/terrain (no stronghold) — keeps power beside markers, no overlap
                    const powerAndMarkersInline = showPower && (showCamp || hasPort || hasHome || showTerrainIcon) && !showStronghold;
                    if (!showCamp && !showStronghold && !showPower && !hasPort && !hasHome && !(isSeaZone && seaZoneNum) && !showTerrainIcon) return null;
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
                        {isSeaZone && seaZoneNum ? (
                          <div
                            className="sea-zone-number"
                            title={`Sea zone ${seaZoneNum}`}
                            aria-hidden
                          >
                            {seaZoneNum}
                          </div>
                        ) : powerAndMarkersInline ? (
                          <div className="territory-markers-row territory-markers-row--power-camp">
                            {showPower && (
                              <div
                                className="territory-power-badge territory-power-badge--inline"
                                title={`${territory.name}: ${power} power`}
                                aria-hidden
                              >
                                {power}
                              </div>
                            )}
                            {hasHome && (
                              <div className="territory-marker home-marker" title="Home territory (deploy 1 unit without camp)">
                                <span className="home-marker-emoji" aria-hidden>🏠</span>
                              </div>
                            )}
                            {showCamp && (
                              <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                                <span className="camp-marker-emoji" aria-hidden>⛺</span>
                              </div>
                            )}
                            {hasPort && (
                              <div className="territory-marker port-marker" title="Port (naval mobilization)">
                                <span className="port-marker-emoji" aria-hidden>⚓</span>
                              </div>
                            )}
                            {showTerrainFortress && (
                              <div className="territory-marker terrain-marker" title="Fortress">🏰</div>
                            )}
                            {showTerrainMountain && (
                              <div className="territory-marker terrain-marker" title="Mountain">⛰️</div>
                            )}
                            {showTerrainForest && (
                              <div className="territory-marker terrain-marker" title="Forest">🌲</div>
                            )}
                          </div>
                        ) : (
                          <>
                            {showPower && !powerAndMarkersInline && (
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
                                {hasHome && (
                                  <div className="territory-marker home-marker" title="Home territory (deploy 1 unit without camp)">
                                    <span className="home-marker-emoji" aria-hidden>🏠</span>
                                  </div>
                                )}
                                <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                                  <span className="camp-marker-emoji" aria-hidden>⛺</span>
                                </div>
                                {hasPort && (
                                  <div className="territory-marker port-marker" title="Port (naval mobilization)">
                                    <span className="port-marker-emoji" aria-hidden>⚓</span>
                                  </div>
                                )}
                                {showTerrainFortress && (
                                  <div className="territory-marker terrain-marker" title="Fortress">🏰</div>
                                )}
                                {showTerrainMountain && (
                                  <div className="territory-marker terrain-marker" title="Mountain">⛰️</div>
                                )}
                                {showTerrainForest && (
                                  <div className="territory-marker terrain-marker" title="Forest">🌲</div>
                                )}
                              </div>
                            ) : (
                              !powerAndMarkersInline && (
                                <>
                                  {(showCamp || hasPort || hasHome || showTerrainIcon) && (
                                    <div className="territory-markers-row territory-markers-row--power-camp">
                                      {hasHome && (
                                        <div className="territory-marker home-marker" title="Home territory (deploy 1 unit without camp)">
                                          <span className="home-marker-emoji" aria-hidden>🏠</span>
                                        </div>
                                      )}
                                      {showCamp && (
                                        <div className="territory-marker camp-marker" title="Camp (mobilization point)">
                                          <span className="camp-marker-emoji" aria-hidden>⛺</span>
                                        </div>
                                      )}
                                      {hasPort && (
                                        <div className="territory-marker port-marker" title="Port (naval mobilization)">
                                          <span className="port-marker-emoji" aria-hidden>⚓</span>
                                        </div>
                                      )}
                                      {showTerrainFortress && (
                                        <div className="territory-marker terrain-marker" title="Fortress">🏰</div>
                                      )}
                                      {showTerrainMountain && (
                                        <div className="territory-marker terrain-marker" title="Mountain">⛰️</div>
                                      )}
                                      {showTerrainForest && (
                                        <div className="territory-marker terrain-marker" title="Forest">🌲</div>
                                      )}
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
                    const unitPos = territoryPositions[territoryId]?.unit ?? territoryCentroids[territoryId];
                    const markerPos = territoryPositions[territoryId]?.marker ?? territoryCentroids[territoryId];
                    const territory = territoryData[territoryId];
                    const hasStrongholdMarker = territory?.stronghold === true;
                    const hasPowerBadge = Number(territory?.produces ?? 0) > 0;
                    const useSeparateUnitSpot = unitPos && markerPos && (unitPos.x !== markerPos.x || unitPos.y !== markerPos.y);
                    const screenPos = unitPos
                      ? clampToMap(svgToScreen(unitPos.x, unitPos.y))
                      : fallbackPositionForTerritory(territoryId);
                    /* When territory has a dedicated unit spot, no offset; otherwise offset below markers */
                    const unitOffsetY = useSeparateUnitSpot ? 0 : (hasStrongholdMarker ? 38 : 6);
                    const powerBadgeOffsetY = useSeparateUnitSpot ? 0 : (hasPowerBadge ? (hasStrongholdMarker ? 26 : 52) : (hasStrongholdMarker ? 0 : 6));

                    const NEUTRAL_UNIT_BORDER = '#888888';
                    const isSeaZone = territory?.terrain === 'sea' || /^sea_zone\d*$/i.test(territoryId);
                    const fullUnits = territoryUnitsFull?.[territoryId];

                    // Sea zone with full unit data. Show tray on click only when multiple boats AND at least one passenger; else allow drag from stack
                    if (isSeaZone && fullUnits?.length && navalUnitIds?.size) {
                      const navalUnits = fullUnits.filter(u => navalUnitIds.has(u.unit_id));
                      const totalBoats = navalUnits.length;
                      const totalPassengers = fullUnits.filter(u => u.loaded_onto).length;
                      const showTrayOnClick = totalBoats > 1 && totalPassengers >= 1;

                      const navalTypes = [...new Set(navalUnits.map(u => u.unit_id))];
                      if (navalTypes.length === 0) return null;
                      const useStacked = navalTypes.length >= 3;
                      const handleStackClick = showTrayOnClick
                        ? (e: React.MouseEvent) => {
                          e.stopPropagation();
                          onSeaZoneStackClick?.(territoryId);
                        }
                        : undefined;
                      return (
                        <div
                          key={territoryId}
                          role={showTrayOnClick ? 'button' : undefined}
                          tabIndex={showTrayOnClick ? 0 : undefined}
                          className={`territory-units ${useStacked ? 'territory-units--stacked' : ''} ${showTrayOnClick ? 'territory-units--sea-stack' : ''}`}
                          style={{
                            left: screenPos.x,
                            top: screenPos.y + unitOffsetY + powerBadgeOffsetY,
                          }}
                          onClick={handleStackClick}
                          onKeyDown={showTrayOnClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSeaZoneStackClick?.(territoryId); } } : undefined}
                          aria-label={showTrayOnClick ? `Boats in ${territoryData[territoryId]?.name ?? territoryId}. Click to open naval tray.` : undefined}
                        >
                          {navalTypes.map((unit_id) => {
                            const boatsOfType = fullUnits.filter(u => u.unit_id === unit_id);
                            const boatCount = boatsOfType.length;
                            const boatInstanceIds = boatsOfType.map(b => b.instance_id);
                            const passengerUnits = fullUnits.filter(u => u.loaded_onto && boatInstanceIds.includes(u.loaded_onto));
                            const passengerCount = passengerUnits.length;
                            const instanceIds = [...boatInstanceIds, ...passengerUnits.map(p => p.instance_id)];
                            const parts = unit_id.split('_');
                            const factionFromId = parts.find(p => factionData[p]);
                            const colorFromId = factionFromId ? factionData[factionFromId].color : null;
                            const defFaction = unitDefs[unit_id]?.faction;
                            const colorFromDef = defFaction && factionData[defFaction] ? factionData[defFaction].color : null;
                            const unitFactionColor = colorFromId ?? colorFromDef ?? NEUTRAL_UNIT_BORDER;
                            const unitFaction = factionFromId ?? defFaction ?? parts[0];
                            const canDrag = !showTrayOnClick && isMovementPhase && (unitFaction === gameState.current_faction);
                            return (
                              <DraggableUnit
                                key={`${territoryId}-${unit_id}`}
                                id={`${territoryId}-${unit_id}`}
                                unitId={unit_id}
                                territoryId={territoryId}
                                count={boatCount}
                                unitDef={unitDefs[unit_id]}
                                isSelected={selectedUnit?.territory === territoryId && selectedUnit?.unitType === unit_id}
                                disabled={!canDrag}
                                factionColor={unitFactionColor}
                                showAerialMustMove={aerialMustMoveKeySet.has(`${territoryId}_${unit_id}`)}
                                showNavalMustAttack={gameState.phase === 'combat_move' && instanceIds.some(id => loadedNavalMustAttackInstanceIdSet.has(id))}
                                isNaval
                                passengerCount={passengerCount}
                                instanceIds={instanceIds}
                              />
                            );
                          })}
                        </div>
                      );
                    }

                    // Land (or sea without full data): stacked tokens by unit type
                    const stackCount = units.length;
                    const useStacked = stackCount >= 3;
                    const sortedUnits = [...units].sort((a, b) => {
                      if (b.count !== a.count) return b.count - a.count;
                      const costA = unitDefs[a.unit_id]?.cost ?? 0;
                      const costB = unitDefs[b.unit_id]?.cost ?? 0;
                      if (costB !== costA) return costB - costA;
                      return a.unit_id.localeCompare(b.unit_id);
                    });

                    return (
                      <div
                        key={territoryId}
                        className={`territory-units ${useStacked ? 'territory-units--stacked' : ''}`}
                        style={{
                          left: screenPos.x,
                          top: screenPos.y + unitOffsetY + powerBadgeOffsetY,
                        }}
                      >
                        {sortedUnits.map(({ unit_id, count }) => {
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
                          const instanceIdsForUnit = (territoryUnitsFull?.[territoryId] || []).filter(u => u.unit_id === unit_id).map(u => u.instance_id);
                          const showNavalMustAttackStacked = gameState.phase === 'combat_move' && navalUnitIds.has(unit_id) && instanceIdsForUnit.some(id => loadedNavalMustAttackInstanceIdSet.has(id));

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
                              showNavalMustAttack={showNavalMustAttackStacked}
                              isNaval={navalUnitIds.has(unit_id)}
                              instanceIds={instanceIdsForUnit.length > 0 ? instanceIdsForUnit : undefined}
                            />
                          );
                        })}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          <DragOverlay activeUnit={activeUnit} activeMobilizationItem={activeMobilizationItem} activeCampDrag={activeCampDrag} factionColor={mobilizationTray?.factionColor} />

          <div className={`map-controls ${mapControlsCollapsed ? 'map-controls--collapsed' : ''}`}>
            {mapControlsCollapsed ? (
              <button
                type="button"
                className="map-controls-toggle"
                onClick={() => setMapControlsCollapsed(false)}
                title="Show map controls"
                aria-label="Show map controls"
              >
                ◀
              </button>
            ) : (
              <>
                <div className="map-controls-top-row">
                  <button
                    type="button"
                    className="map-controls-toggle map-controls-toggle--hide"
                    onClick={() => setMapControlsCollapsed(true)}
                    title="Hide map controls"
                    aria-label="Hide map controls"
                  >
                    ▶
                  </button>
                  <button
                    type="button"
                    className="map-controls-key-btn"
                    onClick={() => setMapKeyOpen(prev => !prev)}
                    title={mapKeyOpen ? 'Hide map key' : 'Show map key'}
                    aria-label={mapKeyOpen ? 'Hide map key' : 'Show map key'}
                  >
                    <svg className="map-controls-key-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden><circle cx="8" cy="15" r="4" /><path d="M10.85 12.15L19 4" /><path d="M18 5l2 2" /><path d="M15 8l2 2" /></svg>
                  </button>
                </div>
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
              </>
            )}
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
        {navalTray && (
          <NavalTray
            isOpen={true}
            seaZoneId={navalTray.seaZoneId}
            seaZoneName={navalTray.seaZoneName}
            boats={navalTray.boats}
            factionColor={navalTray.factionColor}
            onClose={onCloseNavalTray ?? (() => { })}
            pendingLoadBoatOptions={pendingLoadBoatOptions}
            onChooseBoatForLoad={onChooseBoatForLoad}
            pendingLoadPassengers={pendingLoadPassengers}
            loadAllocation={loadAllocation}
            onLoadAllocationChange={onLoadAllocationChange}
          />
        )}
      </div>
    </DndContext>
  );
}

export default GameMap;
