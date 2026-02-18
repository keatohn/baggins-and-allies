import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { DndContext, useDroppable, pointerWithin } from '@dnd-kit/core';
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
  }>;
  territoryUnits: Record<string, { unit_id: string; count: number; instances?: string[] }[]>;
  unitDefs: Record<string, { name: string; icon: string }>;
  unitStats: Record<string, { movement: number }>;
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string }>;
  onTerritorySelect: (territoryId: string | null) => void;
  onUnitSelect: (unit: SelectedUnit | null) => void;
  onUnitMove: (from: string, to: string, unitType: string, count: number) => void;
  isMovementPhase: boolean;
  isCombatMove: boolean;
  isMobilizePhase: boolean;
  hasMobilizationSelected: boolean;
  validMobilizeTerritories?: string[];
  onMobilizationDrop?: (territoryId: string, unitId: string, unitName: string, unitIcon: string, count: number) => void;
  mobilizationTray?: {
    purchases: { unitId: string; name: string; icon: string; count: number }[];
    factionColor: string;
    selectedUnitId: string | null;
    onSelectUnit: (unitId: string | null) => void;
  } | null;
  pendingMoveConfirm: PendingMoveConfirm | null;
  onSetPendingMove: (pending: PendingMoveConfirm | null) => void;
  pendingMoves: PendingMove[];
  highlightedTerritories?: string[];
  availableMoveTargets?: MoveableUnit[];
}

const SVG_VIEWBOX = { width: 1226.6667, height: 1013.3333 };
const IMG_DIMENSIONS = { width: 1840, height: 1520 };
const MIN_SCALE = 0.5;
const MAX_SCALE = 3;

// Droppable territory component
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
  pathData: string;
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
      d={pathData}
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
  onMobilizationDrop,
  mobilizationTray,
  pendingMoveConfirm: _pendingMoveConfirm,
  onSetPendingMove,
  pendingMoves,
  highlightedTerritories = [],
  availableMoveTargets,
}: GameMapProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [svgPaths, setSvgPaths] = useState<Map<string, string>>(new Map());
  const [transform, setTransform] = useState<MapTransform>({ x: 0, y: 0, scale: 1 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
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

  const activeMobilizationItem = useMemo(() => {
    if (!activeDragId || typeof activeDragId !== 'string' || !activeDragId.startsWith('mobilize-')) return null;
    const unitId = activeDragId.replace(/^mobilize-/, '');
    const purchase = mobilizationTray?.purchases?.find(p => p.unitId === unitId) ?? null;
    return purchase ? { ...purchase, factionColor: mobilizationTray?.factionColor ?? '' } : null;
  }, [activeDragId, mobilizationTray?.purchases, mobilizationTray?.factionColor]);

  // Load SVG paths
  useEffect(() => {
    fetch('/test_map.svg')
      .then(res => res.text())
      .then(svgText => {
        const parser = new DOMParser();
        const svgDoc = parser.parseFromString(svgText, 'image/svg+xml');
        const paths = svgDoc.querySelectorAll('path');
        const pathMap = new Map<string, string>();
        
        paths.forEach(path => {
          const label = path.getAttributeNS('http://www.inkscape.org/namespaces/inkscape', 'label');
          const d = path.getAttribute('d');
          if (label && d) {
            pathMap.set(label, d);
          }
        });
        
        setSvgPaths(pathMap);
      });
  }, []);

  // Calculate centroids after paths are loaded
  useEffect(() => {
    if (svgPaths.size === 0 || !svgRef.current) return;

    // Small delay to ensure paths are rendered
    const timer = setTimeout(() => {
      const centroids: Record<string, { x: number; y: number }> = {};
      const pathElements = svgRef.current?.querySelectorAll('path');
      
      pathElements?.forEach(path => {
        const id = path.id?.replace('territory-', '');
        if (!id) return;
        
        try {
          const bbox = path.getBBox();
          centroids[id] = {
            x: bbox.x + bbox.width / 2,
            y: bbox.y + bbox.height / 2,
          };
        } catch {
          // getBBox can fail if path is not rendered
        }
      });
      
      setTerritoryCentroids(centroids);
    }, 100);

    return () => clearTimeout(timer);
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
      
      // Shorten arrow to not overlap with territory centers
      const shortenStart = 30;
      const shortenEnd = 40;
      const startX = fromCentroid.x + (dx / length) * shortenStart;
      const startY = fromCentroid.y + (dy / length) * shortenStart;
      const endX = toCentroid.x - (dx / length) * shortenEnd;
      const endY = toCentroid.y - (dy / length) * shortenEnd;
      
      return {
        ...group,
        startX,
        startY,
        endX,
        endY,
      };
    }).filter(Boolean);
  }, [pendingMoves, territoryCentroids, gameState.phase]);

  // Fit whole map to view on load and when container first gets size (e.g. after layout)
  const initialFitDoneRef = useRef(false);
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;

    const doFit = () => {
      const w = wrapper.clientWidth;
      const h = wrapper.clientHeight;
      if (w <= 0 || h <= 0) return;
      const scaleX = w / IMG_DIMENSIONS.width;
      const scaleY = h / IMG_DIMENSIONS.height;
      const scale = Math.min(scaleX, scaleY);
      const x = (w - IMG_DIMENSIONS.width * scale) / 2;
      const y = (h - IMG_DIMENSIONS.height * scale) / 2;
      setTransform({ x, y, scale });
    };

    if (!initialFitDoneRef.current && wrapper.clientWidth > 0 && wrapper.clientHeight > 0) {
      doFit();
      initialFitDoneRef.current = true;
    }

    const ro = new ResizeObserver(() => {
      if (!initialFitDoneRef.current && wrapper.clientWidth > 0 && wrapper.clientHeight > 0) {
        doFit();
        initialFitDoneRef.current = true;
      }
    });
    ro.observe(wrapper);
    return () => ro.disconnect();
  }, []);

  // Helper to get alliance of a territory
  const getAlliance = useCallback((owner?: string) => {
    if (!owner) return null;
    return factionData[owner]?.alliance || null;
  }, [factionData]);

  // Get valid move targets - backend is source of truth (uses remaining_movement, phase, etc.)
  const getValidTargets = useCallback((fromTerritory: string, unitId: string): Set<string> => {
    if (availableMoveTargets) {
      const validTargets = new Set<string>();
      availableMoveTargets
        .filter(m => m.territory === fromTerritory && m.unit.unit_id === unitId)
        .forEach(m => {
          (m.destinations || []).forEach((dest: string) => validTargets.add(dest));
        });
      return validTargets;
    }
    
    // Fallback only when backend hasn't provided move targets (e.g. not in move phase)
    const territory = territoryData[fromTerritory];
    if (!territory) return new Set();
    
    const movement = unitStats[unitId]?.movement || 1;
    const currentFaction = gameState.current_faction;
    const currentAlliance = getAlliance(currentFaction);
    
    const validTargets = new Set<string>();
    
    // BFS to find all reachable territories within movement range
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
        
        const adjAlliance = getAlliance(adjTerritory.owner);
        const isFriendly = adjTerritory.owner === currentFaction || 
                          (adjAlliance !== null && adjAlliance === currentAlliance);
        const isEnemy = adjAlliance !== null && adjAlliance !== currentAlliance;
        
        if (isCombatMove) {
          if (isFriendly) {
            visited.add(adjId);
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          } else if (isEnemy) {
            visited.add(adjId);
            validTargets.add(adjId);
          }
        } else {
          if (isFriendly) {
            visited.add(adjId);
            validTargets.add(adjId);
            if (remainingMoves > 1) {
              queue.push([adjId, remainingMoves - 1]);
            }
          }
        }
      }
    }
    
    return validTargets;
  }, [availableMoveTargets, territoryData, unitStats, gameState.current_faction, isCombatMove, getAlliance]);

  // Handle drag start
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const data = event.active.data.current;
    if (!data) return;
    setActiveDragId(event.active.id as string);
    if ((data as { type?: string }).type === 'mobilization-unit') {
      setActiveUnit(null);
      setValidDropTargets(new Set(validMobilizeTerritories));
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
  }, [getValidTargets, validMobilizeTerritories]);

  // Handle drag end
  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event;
    const data = active.data.current;
    const isMobilization = (data as { type?: string })?.type === 'mobilization-unit';

    if (isMobilization && over && onMobilizationDrop) {
      const targetTerritory = (over.data.current as { territoryId: string })?.territoryId;
      const { unitId, unitName, icon, count } = (data as { unitId: string; unitName: string; icon: string; count: number });
      if (targetTerritory && validMobilizeTerritories.includes(targetTerritory)) {
        onMobilizationDrop(targetTerritory, unitId, unitName, icon, count);
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
        // Cap by how many units can actually reach this destination (backend reachability)
        if (availableMoveTargets && availableCount > 0) {
          const canReachCount = availableMoveTargets.filter(
            m => m.territory === activeUnit.territoryId
              && m.unit.unit_id === activeUnit.unitId
              && m.destinations.includes(targetTerritory)
          ).length;
          availableCount = Math.min(availableCount, canReachCount);
        }
        if (availableCount <= 0) {
          setActiveUnit(null);
          setActiveDragId(null);
          setValidDropTargets(new Set());
          return;
        }
        onSetPendingMove({
          fromTerritory: activeUnit.territoryId,
          toTerritory: targetTerritory,
          unitId: activeUnit.unitId,
          unitDef: activeUnit.unitDef,
          maxCount: availableCount,
          count: Math.min(activeUnit.count, availableCount),
        });
      }
    }
    setActiveUnit(null);
    setActiveDragId(null);
    setValidDropTargets(new Set());
  }, [activeUnit, validDropTargets, territoryUnits, pendingMoves, availableMoveTargets, onSetPendingMove, onMobilizationDrop, validMobilizeTerritories]);

  // Handle territory click (toggle selection)
  const handleTerritoryClick = useCallback((territoryId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    // Toggle: deselect if already selected, otherwise select
    if (selectedTerritory === territoryId) {
      onTerritorySelect(null);
    } else {
      onTerritorySelect(territoryId);
    }
    onUnitSelect(null);
  }, [selectedTerritory, onTerritorySelect, onUnitSelect]);

  // Handle background click
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    // Don't clear selection if we're dragging the map
    if (isDragging) return;
    
    // Only clear if clicking directly on background elements
    const target = e.target as HTMLElement;
    if (target.classList.contains('map-wrapper') || 
        target.classList.contains('map-background') ||
        target.classList.contains('map-inner')) {
      onTerritorySelect(null);
      onUnitSelect(null);
    }
  }, [isDragging, onTerritorySelect, onUnitSelect]);

  // Pan handlers
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    // Don't start panning if clicking on a unit or territory
    const target = e.target as HTMLElement;
    if (target.closest('.unit-token') || target.tagName === 'path') return;
    
    setIsDragging(true);
    setDragStart({ x: e.clientX - transform.x, y: e.clientY - transform.y });
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

  // Zoom handler
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    if (!wrapperRef.current) return;
    
    const wrapper = wrapperRef.current;
    const rect = wrapper.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;
    
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, transform.scale * delta));
    
    // Zoom toward mouse position
    const scaleRatio = newScale / transform.scale;
    let newX = mouseX - (mouseX - transform.x) * scaleRatio;
    let newY = mouseY - (mouseY - transform.y) * scaleRatio;
    
    // Clamp
    const scaledWidth = IMG_DIMENSIONS.width * newScale;
    const scaledHeight = IMG_DIMENSIONS.height * newScale;
    const minX = Math.min(0, wrapper.clientWidth - scaledWidth);
    const minY = Math.min(0, wrapper.clientHeight - scaledHeight);
    
    newX = Math.max(minX, Math.min(0, newX));
    newY = Math.max(minY, Math.min(0, newY));
    
    setTransform({ x: newX, y: newY, scale: newScale });
  }, [transform]);

  // Clamp position to keep map in view
  const clampPosition = useCallback((x: number, y: number, scale: number) => {
    if (!wrapperRef.current) return { x, y };
    
    const wrapper = wrapperRef.current;
    const scaledWidth = IMG_DIMENSIONS.width * scale;
    const scaledHeight = IMG_DIMENSIONS.height * scale;
    
    // Calculate boundaries - keep map within viewport
    const minX = Math.min(0, wrapper.clientWidth - scaledWidth);
    const minY = Math.min(0, wrapper.clientHeight - scaledHeight);
    const maxX = Math.max(0, wrapper.clientWidth - scaledWidth);
    const maxY = Math.max(0, wrapper.clientHeight - scaledHeight);
    
    return {
      x: Math.max(minX, Math.min(maxX, x)),
      y: Math.max(minY, Math.min(maxY, y)),
    };
  }, []);

  // Zoom controls
  const zoomIn = () => {
    if (!wrapperRef.current) return;
    const newScale = Math.min(MAX_SCALE, transform.scale * 1.2);
    const clamped = clampPosition(transform.x, transform.y, newScale);
    setTransform({ ...clamped, scale: newScale });
  };

  const zoomOut = () => {
    if (!wrapperRef.current) return;
    const newScale = Math.max(MIN_SCALE, transform.scale / 1.2);
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

  // Check if current faction owns a territory
  const isOwnedByCurrentFaction = (territoryId: string) => {
    const territory = territoryData[territoryId];
    return territory?.owner === gameState.current_faction;
  };

  return (
    <DndContext
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      collisionDetection={pointerWithin}
    >
      <div className={`map-container ${mobilizationTray ? 'map-container--with-tray' : ''}`}>
        <div className="map-content">
        <div
          ref={wrapperRef}
          className={`map-wrapper ${isDragging ? 'panning' : ''}`}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          onWheel={handleWheel}
          onClick={handleBackgroundClick}
        >
          <div
            className="map-inner"
            style={{
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
            }}
          >
            <img
              className="map-background"
              src="/test_map.png"
              alt="Map"
              draggable={false}
            />
            
            <svg
              ref={svgRef}
              className="map-svg"
              viewBox={`0 0 ${SVG_VIEWBOX.width} ${SVG_VIEWBOX.height}`}
            >
              {Array.from(svgPaths.entries()).map(([territoryId, pathData]) => {
                const territory = territoryData[territoryId];
                const owner = territory?.owner;
                const color = owner ? factionData[owner]?.color : 'transparent';
                const isSelected = selectedTerritory === territoryId;
                const isValidDrop = validDropTargets.has(territoryId);
                
                // Highlight valid mobilize territories from backend when a unit is selected
                const isValidMobilizationTarget = isMobilizePhase && 
                  hasMobilizationSelected && 
                  validMobilizeTerritories.includes(territoryId);
                
                // Check if this territory is in the highlighted list (e.g., retreat destinations)
                const isExternallyHighlighted = highlightedTerritories.includes(territoryId);
                
                return (
                  <DroppableTerritory
                    key={territoryId}
                    territoryId={territoryId}
                    pathData={pathData}
                    color={color}
                    isSelected={isSelected}
                    isHighlighted={isValidMobilizationTarget || isExternallyHighlighted}
                    isValidDrop={isValidDrop || isValidMobilizationTarget || isExternallyHighlighted}
                    onClick={(e) => handleTerritoryClick(territoryId, e)}
                  />
                );
              })}
              
              {/* Arrow marker definitions */}
              <defs>
                {/* Combat move arrowhead - small pointed arrow */}
                <marker
                  id="arrowhead-combat"
                  markerWidth="4"
                  markerHeight="4"
                  refX="3"
                  refY="2"
                  orient="auto"
                >
                  <polygon 
                    points="0,0 4,2 0,4" 
                    fill="#c62828"
                  />
                </marker>
                {/* Non-combat move arrowhead */}
                <marker
                  id="arrowhead-move"
                  markerWidth="4"
                  markerHeight="4"
                  refX="3"
                  refY="2"
                  orient="auto"
                >
                  <polygon 
                    points="0,0 4,2 0,4" 
                    fill="#2e7d32"
                  />
                </marker>
              </defs>
              
              {/* Move arrows - simple lines with arrowheads */}
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
            
            <div className="unit-layer">
              {Object.entries(territoryUnits).map(([territoryId, units]) => {
                const centroid = territoryCentroids[territoryId];
                if (!centroid || units.length === 0) return null;
                
                const screenPos = svgToScreen(centroid.x, centroid.y);
                const canDrag = isMovementPhase && isOwnedByCurrentFaction(territoryId);
                
                // Get the faction that owns this territory to determine unit border color
                const territoryOwner = territoryData[territoryId]?.owner;
                const ownerFactionColor = territoryOwner ? factionData[territoryOwner]?.color : undefined;
                
                return (
                  <div
                    key={territoryId}
                    className="territory-units"
                    style={{
                      left: screenPos.x,
                      top: screenPos.y,
                    }}
                  >
                    {units.map(({ unit_id, count }) => {
                      // Get faction from unit_id (e.g., "gondor_infantry" -> "gondor")
                      const unitFaction = unit_id.split('_')[0];
                      const unitFactionColor = factionData[unitFaction]?.color || ownerFactionColor;
                      
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
                        />
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
        
        <DragOverlay activeUnit={activeUnit} activeMobilizationItem={activeMobilizationItem} />
        
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
            faction={gameState.current_faction}
            factionColor={mobilizationTray.factionColor}
            selectedUnitId={mobilizationTray.selectedUnitId}
            onSelectUnit={mobilizationTray.onSelectUnit}
            activeDragId={activeDragId}
          />
        )}
      </div>
    </DndContext>
  );
}

export default GameMap;
