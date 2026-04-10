import { useState, useEffect, useRef, useCallback, useMemo, type PointerEvent as ReactPointerEvent } from 'react';
import { DndContext, useDroppable, rectIntersection, pointerWithin, closestCenter, useSensors, useSensor, PointerSensor } from '@dnd-kit/core';
import type { CollisionDetection, DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import { useDraggable } from '@dnd-kit/core';
import { CSS } from '@dnd-kit/utilities';
import type { GameState, SelectedUnit, MapTransform, PendingMove } from '../types/game';
import DraggableUnit from './DraggableUnit';
import DragOverlay, { type BulkDragOverlayStack } from './DragOverlay';
import MobilizationTray from './MobilizationTray';
import NavalTray, { type BoatInTray } from './NavalTray';
import './GameMap.css';
import { sortSeaZoneIdsByNumericSuffix, seaZonesReachableBySailFrom } from '../seaZoneSort';
import {
  directFordOnlyLandPair,
  fordEscortOdMultiplier,
  fordShortcutRequiresEscortLead,
  isFordCrosser,
  minFordEdgesForLandMove,
  pendingFordCrosserLeadFromOrigin,
  remainingFordEscortSlotsClient,
  resolveTerritoryGraphKey,
  usesFordEscortBudget,
} from '../fordEscort';

/** Walk ancestors — hit target may be inside the path, not the node that carries `id`. */
function territoryIdsUnderPoint(clientX: number, clientY: number): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  try {
    const stack = document.elementsFromPoint(clientX, clientY);
    for (const node of stack) {
      let el: Element | null = node instanceof Element ? node : null;
      while (el) {
        const idAttr = el.getAttribute?.('id');
        if (idAttr?.startsWith('territory-')) {
          const tid = idAttr.slice('territory-'.length).trim();
          if (tid && !seen.has(tid)) {
            seen.add(tid);
            out.push(tid);
          }
          break;
        }
        el = el.parentElement;
      }
    }
  } catch {
    /* ignore */
  }
  return out;
}

/** Prefer a territory id that is actually in the highlighted valid set (topmost first). */
function pickValidTerritoryUnderPoint(
  clientX: number,
  clientY: number,
  validDropTargets: Set<string>,
  resolveTerritoryDropId: (s: string) => string,
): string {
  for (const tid of territoryIdsUnderPoint(clientX, clientY)) {
    const r = resolveTerritoryDropId(tid);
    if (r && validDropTargets.has(r)) return r;
    if (validDropTargets.has(tid)) return tid;
  }
  return '';
}

/**
 * DragOverlay is not measured for collisions; rectIntersection uses the source draggable, which stays
 * under the map cursor origin. Use pointer position against droppable rects so drops register on territories.
 */
const mapCollisionDetection: CollisionDetection = (args) => {
  const byPointer = pointerWithin(args);
  if (byPointer.length > 0) return byPointer;
  const byRect = rectIntersection(args);
  if (byRect.length > 0) return byRect;
  return closestCenter(args);
};

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

/** Normalize sea zone id to canonical form sea_zone_n (e.g. sea_zone_9). Accepts sea_zone9 or sea_zone_9. */
function canonicalSeaZoneId(tid: string): string {
  if (!tid || typeof tid !== 'string') return tid || '';
  const m = tid.trim().match(/^sea_zone_*(\d+)$/i);
  return m ? 'sea_zone_' + m[1] : tid.trim();
}

/** Faction id for map stack order (same as unit token border: segment in factionData, else unit def faction, else first segment). */
function factionKeyForUnitType(
  unit_id: string,
  unitDefs: Record<string, { faction?: string; cost?: number } | undefined>,
  factionData: Record<string, { name?: string; color?: string } | undefined>,
): string {
  const parts = unit_id.split('_');
  const factionFromId = parts.find((p) => factionData[p]);
  const defFaction = unitDefs[unit_id]?.faction;
  return factionFromId ?? defFaction ?? parts[0] ?? '';
}

/** Land / overlay: faction, then count (desc), then cost/power (desc), then unit_id. */
function compareMapUnitStacks(
  a: { unit_id: string; count: number },
  b: { unit_id: string; count: number },
  unitDefs: Record<string, { faction?: string; cost?: number } | undefined>,
  factionData: Record<string, { name?: string; color?: string } | undefined>,
): number {
  const fa = factionKeyForUnitType(a.unit_id, unitDefs, factionData);
  const fb = factionKeyForUnitType(b.unit_id, unitDefs, factionData);
  if (fa !== fb) return fa.localeCompare(fb);
  if (b.count !== a.count) return b.count - a.count;
  const costA = unitDefs[a.unit_id]?.cost ?? 0;
  const costB = unitDefs[b.unit_id]?.cost ?? 0;
  if (costB !== costA) return costB - costA;
  return a.unit_id.localeCompare(b.unit_id);
}

/** Every instance ID already on a pending move from this hex (same phase). */
function committedInstanceIdsFromHex(
  pendingMoves: PendingMove[] | undefined,
  fromTid: string,
  fromCanon: string,
  phase: string,
): Set<string> {
  const out = new Set<string>();
  if (!pendingMoves?.length) return out;
  for (const m of pendingMoves) {
    const sameFrom = m.from === fromTid || canonicalSeaZoneId(m.from || '') === fromCanon;
    if (!sameFrom) continue;
    if (m.phase != null && phase && m.phase !== phase) continue;
    for (const iid of m.unit_instance_ids ?? []) out.add(iid);
  }
  return out;
}

type FullUnitRow = {
  instance_id: string;
  unit_id: string;
  loaded_onto?: string | null;
  remaining_movement?: number;
};

/**
 * Minimum remaining_movement among movable units that bulk "All" would include (friendly stacks,
 * not committed to pending, excluding passengers — they move with their boat). If any included
 * unit has 0, bulk-all is invalid.
 */
function minRemainingMovementForBulkAll(
  territoryId: string,
  territoryUnits: Record<string, { unit_id: string; count: number }[]>,
  territoryUnitsFull: Record<string, FullUnitRow[]>,
  currentFaction: string,
  factionData: Record<string, { alliance?: string }>,
  unitDefs: Record<string, { faction?: string }>,
  pendingMoves: PendingMove[] | undefined,
  phase: string,
): number | null {
  const stacks = territoryUnits[territoryId] || [];
  const full = territoryUnitsFull[territoryId] ?? territoryUnitsFull[canonicalSeaZoneId(territoryId)] ?? [];
  const fromCanon = canonicalSeaZoneId(territoryId);
  const committed = committedInstanceIdsFromHex(pendingMoves, territoryId, fromCanon, phase);

  const friendlyStacks = stacks.filter((s) => {
    const parts = s.unit_id.split('_');
    const factionFromId = parts.find((p) => factionData[p]);
    const defFaction = unitDefs[s.unit_id]?.faction;
    const uf = factionFromId ?? defFaction ?? parts[0];
    return uf === currentFaction;
  });
  if (friendlyStacks.length <= 1) return null;

  let globalMin: number | null = null;
  for (const s of friendlyStacks) {
    const instances = full.filter(
      (u) =>
        u.unit_id === s.unit_id &&
        !committed.has(u.instance_id) &&
        !u.loaded_onto,
    );
    for (const u of instances) {
      const rm = typeof u.remaining_movement === 'number' ? u.remaining_movement : 0;
      if (globalMin === null || rm < globalMin) globalMin = rm;
    }
  }
  return globalMin;
}

function isSeaTerrainId(
  tid: string,
  territoryData: Record<string, { terrain?: string } | undefined>,
): boolean {
  return territoryData[tid]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
}

/** Match backend pending_move_is_same_phase_load_into_sea (move_type may be omitted on legacy JSON). */
function pendingMoveIsSamePhaseLoadIntoSea(
  m: PendingMove,
  seaZoneId: string,
  territoryData: Record<string, { terrain?: string } | undefined>,
  phase: string,
): boolean {
  if (m.phase !== phase) return false;
  const toCanon = canonicalSeaZoneId(m.to);
  const canonSea = canonicalSeaZoneId(seaZoneId);
  if (toCanon !== canonSea && m.to !== seaZoneId) return false;
  if (m.move_type === 'load') return true;
  if (m.move_type != null && m.move_type !== '') return false;
  const fromSea = isSeaTerrainId(m.from, territoryData);
  const toSea = isSeaTerrainId(m.to, territoryData);
  return !fromSea && toSea;
}

/** Add destination to set and, for sea zones, add canonical form (sea_zone_n) so drop/highlight works. */
function addDestinationWithSeaZoneAlias(set: Set<string>, d: string): void {
  set.add(d);
  const canonical = canonicalSeaZoneId(d);
  if (canonical !== d) set.add(canonical);
}

/**
 * Non-combat land destinations only (mirrors get_reachable_territories_for_unit non_combat_move filter):
 * friendly, allied, or empty unownable neutral — never enemy/hostile/neutral-with-enemies/ownable neutral.
 */
function isNonCombatNavalOffloadLand(
  rawLandId: string,
  territoryData: Record<string, { terrain?: string; owner?: string; ownable?: boolean } | undefined>,
  territoryUnits: Record<string, { unit_id: string }[]>,
  unitDefs: Record<string, { faction?: string } | undefined>,
  factionData: Record<string, { alliance: string }>,
  currentFaction: string,
): boolean {
  const landKey = resolveTerritoryGraphKey(
    rawLandId,
    territoryData as Record<string, { adjacent?: string[]; ford_adjacent?: string[] } | undefined>,
  );
  const t = territoryData[landKey] ?? territoryData[rawLandId];
  if (!t) return false;
  if (t.terrain === 'sea' || /^sea_zone_?\d+$/i.test(landKey)) return false;
  const cfAlliance = factionData[currentFaction]?.alliance;
  const effO = t.owner;
  const isNeutral = effO == null || effO === '';
  const isOwnable = t.ownable !== false;
  const stacks = territoryUnits[landKey] ?? territoryUnits[rawLandId] ?? [];
  let neutralHasEnemies = false;
  if (isNeutral && cfAlliance) {
    for (const s of stacks) {
      const f = unitDefs[s.unit_id]?.faction;
      const fd = f ? factionData[f] : undefined;
      if (!fd) {
        neutralHasEnemies = true;
        break;
      }
      if (fd.alliance !== cfAlliance) {
        neutralHasEnemies = true;
        break;
      }
    }
  }
  if (isNeutral) {
    return !neutralHasEnemies && !isOwnable;
  }
  if (effO === currentFaction) return true;
  const ownerFd = factionData[effO as string];
  return Boolean(ownerFd && cfAlliance && ownerFd.alliance === cfAlliance);
}

/**
 * Land units declared to load into this sea zone this phase (not applied until phase end).
 * Same-phase offload/raid UI must treat these as passengers — do not require drag instanceIds or boat matching.
 */
function countPendingPassengersLoadingIntoSeaZone(
  seaTerritoryId: string,
  gamePhase: string,
  pendingMoves: PendingMove[] | undefined,
  territoryData: Record<string, { terrain?: string } | undefined>,
): number {
  let n = 0;
  if (!pendingMoves?.length) return 0;
  for (const m of pendingMoves) {
    if (!pendingMoveIsSamePhaseLoadIntoSea(m, seaTerritoryId, territoryData, gamePhase)) continue;
    n += m.unit_instance_ids?.length ?? m.count ?? 0;
  }
  return n;
}

/**
 * Remaining passenger slots for land→sea load: sum over current faction's boats in the zone
 * (per-boat capacity minus onboard and pending loads assigned to that boat), minus pending loads
 * to this sea zone that do not yet specify a boat.
 */
function getLandToSeaLoadCapacityRemaining(
  seaZoneId: string,
  fullUnits: { instance_id: string; unit_id: string; loaded_onto?: string | null }[],
  unitDefs: Record<string, { faction?: string; transport_capacity?: number }>,
  navalUnitIds: Set<string>,
  currentFaction: string,
  pendingMoves: PendingMove[] | undefined,
  gamePhase: string,
  territoryData: Record<string, { terrain?: string } | undefined>,
): number {
  const boats = fullUnits.filter((u) => {
    if (!navalUnitIds.has(u.unit_id) || u.loaded_onto) return false;
    return unitDefs[u.unit_id]?.faction === currentFaction;
  });
  let total = 0;
  for (const boat of boats) {
    const cap = Number((unitDefs[boat.unit_id] as { transport_capacity?: number } | undefined)?.transport_capacity ?? 0);
    const onboard = fullUnits.filter((u) => u.loaded_onto === boat.instance_id).length;
    let pendingOnto = 0;
    for (const m of pendingMoves ?? []) {
      if (!pendingMoveIsSamePhaseLoadIntoSea(m, seaZoneId, territoryData, gamePhase)) continue;
      if (m.load_onto_boat_instance_id !== boat.instance_id) continue;
      pendingOnto += m.unit_instance_ids?.length ?? m.count ?? 0;
    }
    total += Math.max(0, cap - onboard - pendingOnto);
  }
  let unassignedPending = 0;
  for (const m of pendingMoves ?? []) {
    if (!pendingMoveIsSamePhaseLoadIntoSea(m, seaZoneId, territoryData, gamePhase)) continue;
    if (m.load_onto_boat_instance_id) continue;
    unassignedPending += m.unit_instance_ids?.length ?? m.count ?? 0;
  }
  return Math.max(0, total - unassignedPending);
}

function unitIsAerial(
  unitId: string,
  unitDefs: Record<string, { tags?: string[]; archetype?: string } | undefined>,
): boolean {
  const ud = unitDefs[unitId];
  return ud?.archetype === 'aerial' || !!(ud?.tags && ud.tags.includes('aerial'));
}

/**
 * Land units: only highlight sea zones as load destinations when the unit has `transportable` in tags
 * and at least one friendly boat in that hex has remaining capacity (matches handleDragEnd land→sea checks).
 * Aerial: strip all sea hexes from highlights during non_combat_move (cannot land at sea); combat_move unchanged (naval combat).
 */
function filterLandUnitSeaLoadDestinations(
  validTargets: Set<string>,
  unitId: string,
  territoryData: Record<string, { terrain?: string; adjacent?: string[] } | undefined>,
  territoryUnitsFull: Record<string, { instance_id: string; unit_id: string; loaded_onto?: string | null }[]>,
  unitDefs: Record<string, { tags?: string[]; archetype?: string }>,
  navalUnitIds: Set<string>,
  currentFaction: string,
  pendingMoves: PendingMove[] | undefined,
  phase: string,
): void {
  if (navalUnitIds.has(unitId)) return;
  const isAerial = unitIsAerial(unitId, unitDefs);
  if (phase !== 'combat_move' && phase !== 'non_combat_move') return;
  const isSeaT = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
  // Aerial units cannot end movement in a sea hex during non-combat (they may still fly to sea in combat for naval battles).
  if (isAerial && phase === 'non_combat_move') {
    for (const tid of [...validTargets]) {
      if (!isSeaT(tid)) continue;
      validTargets.delete(tid);
      const c = canonicalSeaZoneId(tid);
      if (c !== tid) validTargets.delete(c);
    }
    return;
  }
  if (isAerial) return;
  const ud = unitDefs[unitId];
  const isTransportable = (ud?.tags ?? []).includes('transportable');
  for (const tid of [...validTargets]) {
    if (!isSeaT(tid)) continue;
    if (!isTransportable) {
      validTargets.delete(tid);
      const c = canonicalSeaZoneId(tid);
      if (c !== tid) validTargets.delete(c);
      continue;
    }
    const full =
      territoryUnitsFull[tid] ??
      territoryUnitsFull[canonicalSeaZoneId(tid)] ??
      [];
    const cap = getLandToSeaLoadCapacityRemaining(
      tid,
      full,
      unitDefs as Record<string, { faction?: string; transport_capacity?: number }>,
      navalUnitIds,
      currentFaction,
      pendingMoves,
      phase,
      territoryData,
    );
    if (cap <= 0) {
      validTargets.delete(tid);
      const c = canonicalSeaZoneId(tid);
      if (c !== tid) validTargets.delete(c);
    }
  }
}

/** True when dragging naval from a sea hex with no passengers aboard and no pending loads into this sea (no raid/offload highlights). */
function navalDragSeaNoPassengers(
  fromTerritory: string,
  unitId: string,
  navalDrag: { passengerCount: number; instanceIds?: string[] } | undefined,
  gamePhase: string,
  territoryData: Record<string, { terrain?: string; adjacent?: string[] }>,
  _territoryUnitsFull: Record<string, { instance_id: string; unit_id: string }[]> | undefined,
  pendingMoves: PendingMove[] | undefined,
  navalUnitIds: Set<string> | undefined,
): boolean {
  if (!navalUnitIds?.has(unitId)) return false;
  const t = territoryData[fromTerritory];
  const seaOrigin = t?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(fromTerritory);
  if (!seaOrigin) return false;
  if (gamePhase !== 'combat_move' && gamePhase !== 'non_combat_move') return false;
  const onBoat = navalDrag?.passengerCount ?? 0;
  const pendingPax = countPendingPassengersLoadingIntoSeaZone(fromTerritory, gamePhase, pendingMoves, territoryData);
  return onBoat + pendingPax <= 0;
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
  /**
   * Map stack drag: multiple ships of the same type in one sea zone. Each inner list is [boatInstanceId, ...passengerInstanceIds].
   * `count` / `maxCount` are number of ships; confirm submits flattened slice of the first `count` stacks.
   */
  navalBoatStacks?: string[][];
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
  /** From API: territory id -> movement cost (for ford escort highlight merge). */
  destinationCosts?: Record<string, number>;
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
  unitDefs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[]; home_territory_ids?: string[]; cost?: number; transport_capacity?: number }>;
  unitStats: Record<string, { movement: number }>;
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string; capital?: string }>;
  onTerritorySelect: (territoryId: string | null) => void;
  /** When provided (e.g. during movement phase), double-clicking a multi-boat stack in a sea zone opens the naval tray. */
  onSeaZoneStackClick?: (territoryId: string) => void;
  onUnitSelect: (unit: SelectedUnit | null) => void;
  onUnitMove: (from: string, to: string, unitType: string, count: number) => void;
  /** Bulk move from selected owned territory: send each stack (unit type) as its own pending move to destination. */
  onBulkMoveDrop?: (fromTerritory: string, toTerritory: string) => void;
  /** False when viewing another faction's turn (spectator / not your turn). Hides interactive controls that act for the current player. */
  canAct?: boolean;
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
  onMobilizationAllDrop?: (
    territoryId: string,
    units: { unitId: string; unitName: string; unitIcon: string; count: number }[]
  ) => void;
  onCampDrop?: (campIndex: number, territoryId: string) => void;
  mobilizationTray?: {
    purchases: { unitId: string; name: string; icon: string; count: number }[];
    pendingCamps: { campIndex: number; options?: string[] }[];
    factionColor: string;
    selectedUnitId: string | null;
    selectedCampIndex: number | null;
    onSelectUnit: (unitId: string | null) => void;
    onSelectCamp: (campIndex: number | null) => void;
    mobilizationAllValidZones?: string[];
    canMobilizeAll?: boolean;
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
  /** Defender boats in a mobilization naval standoff (must fight or sail away). */
  forcedNavalCombatInstanceIds?: string[];
  /**
   * Sea zone ids (raw + canonical) where clicking the boat stack should open the naval tray even with no passengers
   * aboard yet — e.g. pending land→sea loads into a hex with multiple boats (after user closed the tray with X).
   */
  seaZoneIdsEligibleForNavalTrayStackClick?: Set<string>;
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
  /**
   * Canonical destination while sidebar confirm is open (move, bulk move, mobilize, bulk mobilize).
   * Keeps the destination territory highlighted on the map after tap (e.g. mobile).
   */
  mobilizationPendingDestination?: string | null;
}

const MAX_SCALE = 3;

const EMPTY_ELIGIBLE_SEA_ZONES_FOR_TRAY = new Set<string>();

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

/**
 * Shift unit stacks relative to the marker anchor (SVG viewBox px). Medium territories often get a
 * single pole-of-inaccessibility for both marker row and units, so tokens sit on stronghold/home art.
 * Marker keeps the computed spot; only the unit layer moves. Tune per territory id (see Osgiliath fixed centroids above).
 */
const TERRITORY_UNIT_OFFSET_FROM_MARKER: Record<string, { dx: number; dy: number }> = {
  dunharrow: { dx: 0, dy: 52 },
  /** Extra vertical gap vs marker row; southern coast needs room before bottom clamp (see CLAMP_INSET_Y_BOTTOM). */
  umbar: { dx: 0, dy: 108 },
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
  highlightMuted = false,
  onClick,
}: {
  territoryId: string;
  pathData: TerritoryPathData;
  color: string;
  isSeaZone: boolean;
  isSelected: boolean;
  isHighlighted: boolean;
  isValidDrop: boolean;
  /** Weaker outline — other valid mobilization zones while a destination is pending confirm. */
  highlightMuted?: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const tid = typeof territoryId === 'string' ? territoryId : (territoryId != null && typeof territoryId === 'object' && 'id' in (territoryId as object) ? String((territoryId as { id: string }).id) : (territoryId != null && typeof territoryId === 'object' && 'territoryId' in (territoryId as object) ? String((territoryId as { territoryId: string }).territoryId) : String(territoryId ?? '')));
  const { setNodeRef, isOver } = useDroppable({
    id: `territory-${tid}`,
    data: { territoryId: tid },
  });
  const showStrongHighlight = (isHighlighted || isValidDrop) && !highlightMuted;
  const pathClass = `territory-path ${isSeaZone ? 'territory-path--sea' : 'territory-path--svg-glow'} ${isSelected ? 'selected' : ''} ${showStrongHighlight ? 'highlight' : ''} ${highlightMuted ? 'highlight-muted' : ''} ${isOver && isValidDrop ? 'drop-target' : ''}`;
  const safeColor = color || '#d4c4a8';
  const glowVars = territoryGlowFromHex(safeColor);
  const glowFilterId = isSeaZone ? undefined : `territory-glow-${safeColor.replace(/^#/, '')}`;
  const pathStyle = {
    ['--territory-glow' as string]: safeColor,
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
        fill={isSeaZone ? 'url(#sea-wave-pattern)' : safeColor}
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

function DraggableAllStacksButton({
  territoryId,
  disabled,
  onTapPrepPointerDown,
}: {
  territoryId: string;
  disabled: boolean;
  onTapPrepPointerDown?: (territoryId: string, e: ReactPointerEvent<HTMLDivElement>) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `all-stacks-${territoryId}`,
    data: { type: 'bulk-all', territoryId },
    disabled,
  });
  /* Wrapper keeps translateX(-50%) centering; inner gets only dnd-kit transform (otherwise inline transform wipes CSS centering). */
  const dragStyle: React.CSSProperties = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.45 : 1,
    zIndex: isDragging ? 1000 : undefined,
  };
  return (
    <div className="all-stacks-drag-btn-wrap">
      <div
        ref={setNodeRef}
        className={`all-stacks-drag-btn${isDragging ? ' dragging' : ''}`}
        style={dragStyle}
        title="Drag all your stacks to a destination, or tap All then tap a highlighted territory (mobile)"
        aria-label="Move all stacks: drag to destination or tap then tap territory"
        onPointerDownCapture={(e) => onTapPrepPointerDown?.(territoryId, e)}
        {...listeners}
        {...attributes}
        role="button"
        tabIndex={attributes.tabIndex ?? 0}
      >
        All
      </div>
    </div>
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
  unitStats: _unitStats,
  factionData,
  onTerritorySelect,
  onSeaZoneStackClick,
  onUnitSelect,
  onUnitMove: _onUnitMove,
  onBulkMoveDrop,
  canAct = true,
  isMovementPhase,
  isCombatMove: _isCombatMove,
  isMobilizePhase,
  hasMobilizationSelected,
  validMobilizeTerritories = [],
  validMobilizeSeaZones = [],
  navalUnitIds = new Set<string>(),
  remainingMobilizationCapacity = {},
  remainingHomeSlots = {},
  onMobilizationDrop,
  onMobilizationAllDrop,
  onCampDrop,
  mobilizationTray,
  navalTray,
  onCloseNavalTray,
  pendingLoadBoatOptions,
  onChooseBoatForLoad,
  pendingLoadPassengers,
  loadAllocation,
  onLoadAllocationChange,
  mobilizationPendingDestination = null,
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
  forcedNavalCombatInstanceIds = [],
  seaZoneIdsEligibleForNavalTrayStackClick = EMPTY_ELIGIBLE_SEA_ZONES_FOR_TRAY,
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
  const forcedNavalCombatInstanceIdSet = useMemo(
    () => new Set(forcedNavalCombatInstanceIds),
    [forcedNavalCombatInstanceIds]
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
  const [bulkDragOverlay, setBulkDragOverlay] = useState<{ stacks: BulkDragOverlayStack[] } | null>(null);
  const [activeDragId, setActiveDragId] = useState<string | null>(null);
  const activeDragIdRef = useRef<string | null>(null);
  /** When set, user tapped a unit (no drag); show valid destinations and next tap on territory = drop */
  const [tapSelectedUnit, setTapSelectedUnit] = useState<{
    unitId: string;
    territoryId: string;
    count: number;
    unitDef?: { name: string; icon: string };
    factionColor?: string;
    isNaval?: boolean;
    instanceIds?: string[];
    passengerCount?: number;
  } | null>(null);
  const tapStartRef = useRef<{ territoryId: string; unitId: string; x: number; y: number } | null>(null);
  /** Touch: long-press boat stack (2+ ships) opens naval tray without a separate "list" control. */
  const navalTrayLongPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const navalTrayLongPressStartRef = useRef<{ x: number; y: number; territoryId: string } | null>(null);
  const [coarsePointer, setCoarsePointer] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches,
  );
  useEffect(() => {
    const mq = window.matchMedia('(pointer: coarse)');
    const fn = () => setCoarsePointer(mq.matches);
    mq.addEventListener('change', fn);
    return () => mq.removeEventListener('change', fn);
  }, []);

  useEffect(
    () => () => {
      if (navalTrayLongPressTimerRef.current != null) {
        clearTimeout(navalTrayLongPressTimerRef.current);
        navalTrayLongPressTimerRef.current = null;
      }
    },
    [],
  );
  /** Last pointer position while any map drag is active — prefer `elementsFromPoint` + valid targets over dnd-kit `over` (bbox collisions). */
  const lastMapDragPointerRef = useRef<{ x: number; y: number } | null>(null);
  /** Tap All (no drag) then tap destination — mobile-friendly bulk move */
  const bulkAllTapStartRef = useRef<{ territoryId: string; x: number; y: number } | null>(null);
  const [tapBulkAllFromTerritory, setTapBulkAllFromTerritory] = useState<string | null>(null);
  /** Tap tray “All” then tap territory — mirrors single-stack tap mobilization on mobile */
  const [tapMobilizationAll, setTapMobilizationAll] = useState(false);
  const [mapControlsCollapsed, setMapControlsCollapsed] = useState(false);
  const [mapKeyOpen, setMapKeyOpen] = useState(false);
  /** On touch: which territory's unit stack is expanded (tap stack to expand, then tap unit to select) */
  const [expandedStackKey, setExpandedStackKey] = useState<string | null>(null);
  /** Set when user commits a mobilization destination (drop/tap); mutes other valid zones until confirm ends. Cleared when pending confirm clears in parent. */
  const [mobilizationDestinationClickCanon, setMobilizationDestinationClickCanon] = useState<string | null>(null);
  const prevMobilizationPendingDestRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    activeDragIdRef.current = activeDragId;
  }, [activeDragId]);

  /** After confirm/cancel, parent clears pending — restore full valid-zone highlights for the next mobilization. */
  useEffect(() => {
    const cur = mobilizationPendingDestination?.trim() || null;
    if (prevMobilizationPendingDestRef.current === undefined) {
      prevMobilizationPendingDestRef.current = cur;
      return;
    }
    const prev = prevMobilizationPendingDestRef.current;
    if (prev != null && cur == null) {
      setMobilizationDestinationClickCanon(null);
    }
    prevMobilizationPendingDestRef.current = cur;
  }, [mobilizationPendingDestination]);

  useEffect(() => {
    if (!isMobilizePhase) {
      setMobilizationDestinationClickCanon(null);
    }
  }, [isMobilizePhase]);

  useEffect(() => {
    if (!tapMobilizationAll) return;
    const uid = mobilizationTray?.selectedUnitId ?? null;
    const camp = mobilizationTray?.selectedCampIndex ?? null;
    if (uid != null || camp != null) {
      setTapMobilizationAll(false);
      setValidDropTargets(new Set());
      setBulkDragOverlay(null);
    }
  }, [tapMobilizationAll, mobilizationTray?.selectedUnitId, mobilizationTray?.selectedCampIndex]);

  useEffect(() => {
    if (!activeDragId) {
      lastMapDragPointerRef.current = null;
      return;
    }
    const onMove = (e: PointerEvent) => {
      lastMapDragPointerRef.current = { x: e.clientX, y: e.clientY };
    };
    window.addEventListener('pointermove', onMove, { capture: true, passive: true });
    return () => window.removeEventListener('pointermove', onMove, { capture: true });
  }, [activeDragId]);

  // Require 10px movement before starting drag so a simple click/tap selects unit and shows destinations
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
          const isSeaZone = /^sea_zone_?\d+$/i.test(territoryId);
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

      for (const [tid, off] of Object.entries(TERRITORY_UNIT_OFFSET_FROM_MARKER)) {
        const pos = positions[tid];
        if (!pos) continue;
        const marker = pos.marker;
        const unit = { x: marker.x + off.dx, y: marker.y + off.dy };
        positions[tid] = { marker, unit };
        // Keep centroid at marker anchor so move/combat arrows (which use territoryCentroids) don’t
        // originate from the nudged unit stack (e.g. Umbar’s line shooting up from under the hex).
        centroids[tid] = marker;
      }

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

    const isSea = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);

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

  /** Map SVG / hit-test / stack keys → keys present in territoryData (case, sea_zone_9 vs sea_zone_9). */
  const resolveTerritoryDropId = useMemo(() => {
    const byLower = new Map<string, string>();
    for (const k of Object.keys(territoryData || {})) {
      byLower.set(k.toLowerCase(), k);
    }
    return (tid: string): string => {
      if (!tid || typeof tid !== 'string') return '';
      const t = tid.trim();
      if (!t) return '';
      if (territoryData[t]) return t;
      const lowerHit = byLower.get(t.toLowerCase());
      if (lowerHit) return lowerHit;
      const seaCanon = canonicalSeaZoneId(t);
      if (territoryData[seaCanon]) return seaCanon;
      const seaLower = byLower.get(seaCanon.toLowerCase());
      if (seaLower) return seaLower;
      return t;
    };
  }, [territoryData]);

  // Valid move targets come only from available-actions (backend pathfinding). No client BFS — empty highlights mean fix API/state sync, not paper over it here.
  /** Naval drags from sea: strip raid/offload land unless passengers or pending loads into this sea. */
  const getValidTargets = useCallback((
    fromTerritory: string,
    unitId: string,
    navalDrag?: { passengerCount: number; instanceIds?: string[] },
  ): Set<string> => {
    if (!availableMoveTargets?.length) {
      return new Set();
    }
    const fordGraphForMatch = territoryData as Record<string, { adjacent?: string[]; ford_adjacent?: string[] } | undefined>;
    const fromKeyForMatch = resolveTerritoryGraphKey(fromTerritory, fordGraphForMatch);
    const fromCanon = canonicalSeaZoneId(fromTerritory);
    const matches = availableMoveTargets.filter((m) => {
      if (m.unit.unit_id !== unitId) return false;
      const mKey = resolveTerritoryGraphKey(m.territory, fordGraphForMatch);
      return (
        mKey === fromKeyForMatch ||
        canonicalSeaZoneId(m.territory) === fromCanon ||
        m.territory === fromTerritory
      );
    });

    const validTargets = new Set<string>();
    if (matches.length > 0) {
      for (const m of matches) {
        const dests = m.destinations || [];
        for (const d of dests) {
          addDestinationWithSeaZoneAlias(validTargets, d);
        }
      }
      filterLandUnitSeaLoadDestinations(
        validTargets,
        unitId,
        territoryData,
        territoryUnitsFull ?? {},
        unitDefs,
        navalUnitIds ?? new Set(),
        gameState.current_faction,
        pendingMoves,
        gameState.phase,
      );
    }
    // Do not return early when validTargets is empty — ford escort + pending `to` merge below can still add land destinations.

    const escortRemainingMovement = (): number => {
      const rm0 = matches[0]?.unit?.remaining_movement;
      if (typeof rm0 === 'number' && Number.isFinite(rm0)) return rm0;
      const full =
        territoryUnitsFull?.[fromKeyForMatch] ??
        territoryUnitsFull?.[fromTerritory] ??
        [];
      let best = 0;
      for (const u of full) {
        if (u.unit_id !== unitId) continue;
        const r = (u as { remaining_movement?: number }).remaining_movement;
        if (typeof r === 'number' && r > best) best = r;
      }
      if (best > 0) return best;
      return _unitStats[unitId]?.movement ?? 0;
    };

    if ((gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move') && navalUnitIds?.has(unitId) && validTargets.size > 0) {
      const isSeaT = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
      const emptySeaNaval = navalDragSeaNoPassengers(
        fromTerritory,
        unitId,
        navalDrag,
        gameState.phase,
        territoryData,
        territoryUnitsFull,
        pendingMoves,
        navalUnitIds,
      );
      if (emptySeaNaval) {
        const onlySea = new Set<string>();
        for (const tid of validTargets) {
          if (isSeaT(tid)) addDestinationWithSeaZoneAlias(onlySea, tid);
        }
        validTargets.clear();
        for (const tid of onlySea) validTargets.add(tid);
      } else {
        const onBoat = navalDrag?.passengerCount ?? 0;
        const pendingPax = countPendingPassengersLoadingIntoSeaZone(
          fromTerritory,
          gameState.phase,
          pendingMoves,
          territoryData,
        );
        if (onBoat + pendingPax > 0) {
          const seaZoneIds = [...validTargets].filter(isSeaT);
          for (const sz of seaZoneIds) {
            for (const adjId of territoryData[sz]?.adjacent || []) {
              if (!territoryData[adjId] || isSeaT(adjId)) continue;
              if (
                gameState.phase === 'non_combat_move' &&
                !isNonCombatNavalOffloadLand(
                  adjId,
                  territoryData,
                  territoryUnits,
                  unitDefs,
                  factionData,
                  gameState.current_faction,
                )
              ) {
                continue;
              }
              addDestinationWithSeaZoneAlias(validTargets, adjId);
            }
          }
        }
      }
    }
    /*
     * Ford escort: API sometimes omits ford-only destinations for transportable stacks even after a
     * ford crosser has declared a lead pending move. Merge crosser reach (same origin, min ford edges ≥ 1)
     * so escorted units get valid highlights and drop targets (server already accepts these moves).
     */
    if (
      (gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move') &&
      usesFordEscortBudget(unitDefs[unitId]) &&
      pendingFordCrosserLeadFromOrigin(
        fromTerritory,
        gameState.phase,
        pendingMoves,
        territoryData as Record<string, { adjacent?: string[]; ford_adjacent?: string[] } | undefined>,
        unitDefs as Record<string, { archetype?: string; tags?: string[]; specials?: string[] } | undefined>,
        territoryUnitsFull ?? {},
      )
    ) {
      const fordGraph = territoryData as Record<string, { adjacent?: string[]; ford_adjacent?: string[] } | undefined>;
      const fromKey = resolveTerritoryGraphKey(fromTerritory, fordGraph);
      const fordSlots = remainingFordEscortSlotsClient(
        fromKey,
        gameState.phase,
        pendingMoves,
        fordGraph,
        territoryUnitsFull ?? {},
        unitDefs as Record<
          string,
          | {
              specials?: string[];
              tags?: string[];
              archetype?: string;
              transport_capacity?: number;
              faction?: string;
            }
          | undefined
        >,
        gameState.current_faction,
        new Set(),
      );
      const stackCount =
        (territoryUnits[fromTerritory] || []).find((s) => s.unit_id === unitId)?.count ?? 1;
      if (fordSlots > 0) {
        const escortRm = escortRemainingMovement();
        for (const m of availableMoveTargets) {
          if (!isFordCrosser(unitDefs[m.unit.unit_id])) continue;
          const mTerr = m.territory;
          const mCanon = canonicalSeaZoneId(mTerr);
          if (
            mCanon !== fromCanon &&
            mTerr !== fromTerritory &&
            resolveTerritoryGraphKey(mTerr, fordGraph) !== fromKey
          ) {
            continue;
          }
          const costs = m.destinationCosts;
          for (const d of m.destinations) {
            const dk = resolveTerritoryGraphKey(d, fordGraph);
            if (!fordShortcutRequiresEscortLead(fromKey, dk, fordGraph)) continue;
            const od = fordEscortOdMultiplier(fromKey, dk, fordGraph);
            if (od < 1 || fordSlots < od * stackCount) continue;
            if (costs) {
              const c = costs[d] ?? costs[dk];
              if (c !== undefined && c > escortRm) continue;
            }
            addDestinationWithSeaZoneAlias(validTargets, d);
          }
        }
        // Crosser may be absent from moveable_units (e.g. row omitted); still highlight escort drop = pending `to`.
        const fullUnits =
          territoryUnitsFull?.[fromKey] ?? territoryUnitsFull?.[fromTerritory] ?? [];
        const byIid = new Map(fullUnits.map((u) => [u.instance_id, u]));
        const pendingToSeen = new Set<string>();
        for (const pm of pendingMoves ?? []) {
          if (pm.phase !== gameState.phase) continue;
          const mt = pm.move_type;
          if (mt === 'load' || mt === 'offload' || mt === 'sail') continue;
          if (resolveTerritoryGraphKey(pm.from, fordGraph) !== fromKey) continue;
          const rawTo = (pm.to || '').trim();
          if (!rawTo || pendingToSeen.has(rawTo)) continue;
          const toK = resolveTerritoryGraphKey(rawTo, fordGraph);
          if (!fordShortcutRequiresEscortLead(fromKey, toK, fordGraph)) continue;
          const odPm = fordEscortOdMultiplier(fromKey, toK, fordGraph);
          if (odPm < 1 || fordSlots < odPm * stackCount) continue;
          const iids = pm.unit_instance_ids ?? [];
          let pmHasCrosser = false;
          if (!iids.length) {
            if (pm.primary_unit_id && isFordCrosser(unitDefs[pm.primary_unit_id])) pmHasCrosser = true;
          } else {
            for (const iid of iids) {
              const row = byIid.get(iid);
              if (row && isFordCrosser(unitDefs[row.unit_id])) {
                pmHasCrosser = true;
                break;
              }
            }
            if (!pmHasCrosser && pm.primary_unit_id && isFordCrosser(unitDefs[pm.primary_unit_id]) && iids.length === 1) {
              pmHasCrosser = true;
            }
          }
          if (!pmHasCrosser) continue;
          pendingToSeen.add(rawTo);
          addDestinationWithSeaZoneAlias(validTargets, rawTo);
        }
      }
    }
    return validTargets;
  }, [
    availableMoveTargets,
    territoryData,
    territoryUnits,
    territoryUnitsFull,
    unitDefs,
    factionData,
    _unitStats,
    gameState.current_faction,
    gameState.phase,
    navalUnitIds,
    pendingMoves,
  ]);

  const territoryMatchesValidDrop = useCallback(
    (territoryId: string) => {
      if (validDropTargets.has(territoryId)) return true;
      const r = resolveTerritoryDropId(territoryId);
      return r !== '' && validDropTargets.has(r);
    },
    [validDropTargets, resolveTerritoryDropId],
  );

  const computeBulkAllMoveData = useCallback(
    (fromTerritory: string): { validTargets: Set<string>; stacks: BulkDragOverlayStack[] } => {
      const stackUnits = territoryUnits[fromTerritory] || [];
      const currentFaction = gameState.current_faction;
      const friendlyStacks = stackUnits.filter((s) => {
        const parts = s.unit_id.split('_');
        const factionFromId = parts.find((p) => factionData[p]);
        const defFaction = unitDefs[s.unit_id]?.faction;
        const uf = factionFromId ?? defFaction ?? parts[0];
        return uf === currentFaction;
      });
      const sortedFriendly = [...friendlyStacks].sort((a, b) =>
        compareMapUnitStacks(a, b, unitDefs, factionData),
      );
      const crosserStacks = sortedFriendly.filter((s) => isFordCrosser(unitDefs[s.unit_id]));
      const escortStacks = sortedFriendly.filter((s) => usesFordEscortBudget(unitDefs[s.unit_id]));
      const neutralStacks = sortedFriendly.filter(
        (s) => !isFordCrosser(unitDefs[s.unit_id]) && !usesFordEscortBudget(unitDefs[s.unit_id]),
      );
      let bulkTargets: Set<string> | null = null;
      if (crosserStacks.length > 0 && escortStacks.length > 0) {
        const intersectStacks = (stacks: typeof sortedFriendly): Set<string> => {
          let acc: Set<string> | null = null;
          for (const s of stacks) {
            const t = getValidTargets(fromTerritory, s.unit_id);
            if (acc === null) acc = new Set<string>(t);
            else acc = new Set([...acc].filter((id: string) => t.has(id)));
          }
          return acc ?? new Set<string>();
        };
        const tCross = intersectStacks(crosserStacks);
        const tEsc = intersectStacks(escortStacks);
        const tOthers = neutralStacks.length ? intersectStacks(neutralStacks) : null;
        const fordGraph = territoryData as Record<
          string,
          { adjacent?: string[]; ford_adjacent?: string[] } | undefined
        >;
        const fromKey = resolveTerritoryGraphKey(fromTerritory, fordGraph);
        const escortFigureCount = escortStacks.reduce((sum, s) => sum + s.count, 0);
        const fordSlots = remainingFordEscortSlotsClient(
          fromKey,
          gameState.phase,
          pendingMoves,
          fordGraph,
          territoryUnitsFull ?? {},
          unitDefs as Record<
            string,
            | {
                specials?: string[];
                tags?: string[];
                archetype?: string;
                transport_capacity?: number;
                faction?: string;
              }
            | undefined
          >,
          currentFaction,
          new Set(),
        );
        const fordExtras = new Set<string>();
        if (fordSlots > 0 && escortFigureCount > 0) {
          for (const d of tCross) {
            const dk = resolveTerritoryGraphKey(d, fordGraph);
            if (!fordShortcutRequiresEscortLead(fromKey, dk, fordGraph)) continue;
            const od = fordEscortOdMultiplier(fromKey, dk, fordGraph);
            if (od < 1 || fordSlots < od * escortFigureCount) continue;
            fordExtras.add(d);
          }
        }
        const escortOrFord = new Set<string>(tEsc);
        for (const d of fordExtras) {
          if (tCross.has(d)) escortOrFord.add(d);
        }
        bulkTargets = new Set<string>();
        for (const d of tCross) {
          if (!escortOrFord.has(d)) continue;
          if (tOthers && !tOthers.has(d)) continue;
          bulkTargets.add(d);
        }
      } else {
        for (const s of sortedFriendly) {
          const t = getValidTargets(fromTerritory, s.unit_id);
          if (bulkTargets === null) {
            bulkTargets = new Set<string>(t);
          } else {
            const next = new Set<string>();
            for (const id of bulkTargets) {
              if (t.has(id)) next.add(id);
            }
            bulkTargets = next;
          }
        }
      }
      const stacks: BulkDragOverlayStack[] = sortedFriendly.map((s) => {
        const parts = s.unit_id.split('_');
        const factionFromId = parts.find((p) => factionData[p]);
        const colorFromId = factionFromId ? factionData[factionFromId].color : null;
        const defFaction = unitDefs[s.unit_id]?.faction;
        const colorFromDef = defFaction && factionData[defFaction] ? factionData[defFaction].color : null;
        const ud = unitDefs[s.unit_id];
        return {
          unitId: s.unit_id,
          count: s.count,
          unitDef: {
            name:
              (ud as { display_name?: string; name?: string })?.display_name ??
              (ud as { name?: string })?.name ??
              s.unit_id,
            icon: (ud as { icon?: string })?.icon ?? '',
          },
          factionColor: colorFromId ?? colorFromDef ?? undefined,
          isNaval: navalUnitIds.has(s.unit_id),
          passengerCount: 0,
        };
      });
      return { validTargets: bulkTargets ?? new Set(), stacks };
    },
    [
      territoryUnits,
      territoryData,
      territoryUnitsFull,
      gameState.current_faction,
      gameState.phase,
      factionData,
      unitDefs,
      getValidTargets,
      navalUnitIds,
      pendingMoves,
    ],
  );

  // Detect tap on unit (pointer down + up with <10px move): show valid destinations for click-then-click move
  const handleUnitPointerDownCapture = useCallback((territoryId: string, unitId: string, e: ReactPointerEvent<HTMLElement>) => {
    tapStartRef.current = { territoryId, unitId, x: e.clientX, y: e.clientY };
  }, []);
  useEffect(() => {
    const handler = (e: PointerEvent) => {
      const bulkStart = bulkAllTapStartRef.current;
      if (bulkStart) {
        const dx = e.clientX - bulkStart.x;
        const dy = e.clientY - bulkStart.y;
        bulkAllTapStartRef.current = null;
        if (dx * dx + dy * dy >= 100) return;
        if (activeDragIdRef.current !== null) return;
        if (!canAct) return;
        setTapSelectedUnit(null);
        setTapMobilizationAll(false);
        const { validTargets } = computeBulkAllMoveData(bulkStart.territoryId);
        setValidDropTargets(validTargets);
        setTapBulkAllFromTerritory(bulkStart.territoryId);
        setBulkDragOverlay(null);
        return;
      }

      const start = tapStartRef.current;
      if (!start) return;
      const dx = e.clientX - start.x;
      const dy = e.clientY - start.y;
      const distanceSq = dx * dx + dy * dy;
      tapStartRef.current = null;
      if (distanceSq >= 100) return; // 10px threshold
      if (activeDragIdRef.current !== null) return; // Was a drag
      if (!canAct) return;
      setTapMobilizationAll(false);
      // Build tap-selected unit from territory/unit and show valid destinations
      const stacks = territoryUnits[start.territoryId] || [];
      const stack = stacks.find(s => s.unit_id === start.unitId);
      if (!stack) return;
      const parts = start.unitId.split('_');
      const factionFromId = parts.find(p => factionData[p]);
      const defFaction = unitDefs[start.unitId]?.faction;
      const unitFaction = factionFromId ?? defFaction ?? parts[0];
      if (unitFaction !== gameState.current_faction) return;
      const unitFactionColor = factionFromId && factionData[factionFromId] ? factionData[factionFromId].color : (defFaction && factionData[defFaction] ? factionData[defFaction].color : undefined);
      const instanceIdsForUnit = (territoryUnitsFull?.[start.territoryId] || []).filter(u => u.unit_id === start.unitId).map(u => u.instance_id);
      const isSeaTap = territoryData[start.territoryId]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(start.territoryId);
      let navalDrag: { passengerCount: number; instanceIds?: string[] } | undefined;
      if (navalUnitIds.has(start.unitId) && isSeaTap && territoryUnitsFull?.[start.territoryId]?.length) {
        const full = territoryUnitsFull[start.territoryId];
        const boatsOfType = full.filter(u => u.unit_id === start.unitId && navalUnitIds.has(u.unit_id));
        const boatIds = boatsOfType.map(b => b.instance_id);
        const pax = full.filter(u => u.loaded_onto && boatIds.includes(u.loaded_onto));
        navalDrag = {
          passengerCount: pax.length,
          instanceIds: [...boatIds, ...pax.map(p => p.instance_id)],
        };
      }
      setTapSelectedUnit({
        unitId: start.unitId,
        territoryId: start.territoryId,
        count: stack.count,
        unitDef: unitDefs[start.unitId] ? { name: (unitDefs[start.unitId] as { display_name?: string; name?: string })?.display_name ?? (unitDefs[start.unitId] as { name?: string })?.name ?? start.unitId, icon: (unitDefs[start.unitId] as { icon?: string })?.icon ?? '' } : undefined,
        factionColor: unitFactionColor,
        isNaval: navalUnitIds.has(start.unitId),
        instanceIds: instanceIdsForUnit.length > 0 ? instanceIdsForUnit : undefined,
        passengerCount: navalDrag?.passengerCount ?? 0,
      });
      setValidDropTargets(getValidTargets(start.territoryId, start.unitId, navalDrag));
    };
    document.addEventListener('pointerup', handler);
    return () => document.removeEventListener('pointerup', handler);
  }, [
    territoryUnits,
    territoryUnitsFull,
    territoryData,
    unitDefs,
    factionData,
    navalUnitIds,
    getValidTargets,
    gameState.phase,
    gameState.current_faction,
    pendingMoves,
    computeBulkAllMoveData,
    canAct,
  ]);

  const handleTapMobilizeAllFromTray = useCallback(() => {
    if (!mobilizationTray?.canMobilizeAll || (mobilizationTray.purchases?.length ?? 0) <= 1) return;
    mobilizationTray.onSelectUnit(null);
    mobilizationTray.onSelectCamp(null);
    setTapMobilizationAll(true);
    const zones = mobilizationTray.mobilizationAllValidZones ?? [];
    setValidDropTargets(new Set(zones));
    const purchases = mobilizationTray.purchases ?? [];
    const stacks: BulkDragOverlayStack[] = purchases.map(p => ({
      unitId: p.unitId,
      count: p.count,
      unitDef: { name: p.name, icon: p.icon },
      factionColor: mobilizationTray.factionColor ?? undefined,
      isNaval: navalUnitIds.has(p.unitId),
      passengerCount: 0,
    }));
    setBulkDragOverlay(stacks.length > 0 ? { stacks } : null);
    setTapSelectedUnit(null);
    setTapBulkAllFromTerritory(null);
  }, [mobilizationTray, navalUnitIds]);

  // Handle drag start
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const data = event.active.data.current;
    if (!data) return;
    const ae = event.activatorEvent as PointerEvent | MouseEvent | undefined;
    if (ae && typeof ae.clientX === 'number' && typeof ae.clientY === 'number') {
      lastMapDragPointerRef.current = { x: ae.clientX, y: ae.clientY };
    }
    setTapSelectedUnit(null); // Clear tap selection when starting a drag
    setTapBulkAllFromTerritory(null);
    setTapMobilizationAll(false);
    bulkAllTapStartRef.current = null;
    setBulkDragOverlay(null);
    setActiveDragId(event.active.id as string);
    if (!canAct) {
      setValidDropTargets(new Set());
      setBulkDragOverlay(null);
      setActiveUnit(null);
      return;
    }
    if ((data as { type?: string }).type === 'bulk-all') {
      const fromTerritory = (data as { territoryId?: string }).territoryId;
      if (fromTerritory) {
        const { validTargets, stacks } = computeBulkAllMoveData(fromTerritory);
        setValidDropTargets(validTargets);
        setBulkDragOverlay(stacks.length > 0 ? { stacks } : null);
      } else {
        setValidDropTargets(new Set());
        setBulkDragOverlay(null);
      }
      setActiveUnit(null);
      return;
    }

    if ((data as { type?: string }).type === 'mobilization-all') {
      const purchases = mobilizationTray?.purchases ?? [];
      const zones = mobilizationTray?.mobilizationAllValidZones ?? [];
      setValidDropTargets(new Set(zones));

      const stacks: BulkDragOverlayStack[] = purchases.map(p => ({
        unitId: p.unitId,
        count: p.count,
        unitDef: { name: p.name, icon: p.icon },
        factionColor: mobilizationTray?.factionColor ?? undefined,
        isNaval: navalUnitIds.has(p.unitId),
        passengerCount: 0,
      }));
      setBulkDragOverlay(stacks.length > 0 ? { stacks } : null);
      setActiveUnit(null);
      return;
    }

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
    const isSeaFrom = territoryData[territoryId]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(territoryId);
    const navalDrag =
      navalUnitIds.has(unitId) && isSeaFrom
        ? { passengerCount: passengerCount ?? 0, instanceIds }
        : undefined;
    setValidDropTargets(getValidTargets(territoryId, unitId, navalDrag));
  }, [
    getValidTargets,
    computeBulkAllMoveData,
    validMobilizeTerritories,
    validMobilizeSeaZones,
    navalUnitIds,
    remainingMobilizationCapacity,
    remainingHomeSlots,
    mobilizationTray?.pendingCamps,
    mobilizationTray?.purchases,
    mobilizationTray?.factionColor,
    mobilizationTray?.mobilizationAllValidZones,
    territoriesWithPendingCampPlacement,
    territoryData,
    canAct,
  ]);

  // Handle drag end
  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const ptrAtEnd = lastMapDragPointerRef.current;
    lastMapDragPointerRef.current = null;

    const { active, over } = event;
    const data = active.data.current;
    const dataType = (data as { type?: string })?.type;
    const isBulkAll = dataType === 'bulk-all';
    const isMobilizationAll = dataType === 'mobilization-all';
    const isMobilizationCamp = dataType === 'mobilization-camp';
    const isMobilization = dataType === 'mobilization-unit';

    if (!canAct) {
      setTapBulkAllFromTerritory(null);
      setTapMobilizationAll(false);
      setBulkDragOverlay(null);
      setActiveUnit(null);
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (isBulkAll) {
      let storeTarget = '';
      const ptr = ptrAtEnd;
      if (ptr) {
        storeTarget = pickValidTerritoryUnderPoint(ptr.x, ptr.y, validDropTargets, resolveTerritoryDropId);
      }
      if (!storeTarget && over) {
        const overId = over.id != null ? String(over.id) : '';
        const raw = (over.data.current as { territoryId?: unknown })?.territoryId;
        const targetFromData =
          typeof raw === 'string'
            ? raw.trim()
            : raw != null && typeof raw === 'object' && 'territoryId' in (raw as object)
              ? String((raw as { territoryId: string }).territoryId).trim()
              : '';
        const targetFromId = overId.startsWith('territory-') ? overId.slice('territory-'.length).trim() : '';
        const targetTerritory =
          targetFromData && targetFromData !== '[object Object]'
            ? targetFromData
            : targetFromId && targetFromId !== '[object Object]'
              ? targetFromId
              : '';
        const isBadDest =
          !targetTerritory ||
          targetTerritory === '[object Object]' ||
          targetTerritory.includes('object');
        storeTarget = targetTerritory && !isBadDest ? targetTerritory : '';
      }
      if (!storeTarget && event.collisions?.length) {
        const hit = event.collisions.find((c) => String(c.id).startsWith('territory-'));
        if (hit) {
          const tid = String(hit.id).slice('territory-'.length).trim();
          if (tid && tid !== '[object Object]' && !tid.includes('object')) storeTarget = tid;
        }
      }
      if (!storeTarget && ptr) {
        const raw = territoryIdsUnderPoint(ptr.x, ptr.y)[0] ?? '';
        if (raw) storeTarget = raw;
      }
      if (storeTarget) {
        const resolvedId = resolveTerritoryDropId(storeTarget) || storeTarget;
        const isValidDest = validDropTargets.has(resolvedId) || validDropTargets.has(storeTarget);
        if (isValidDest) {
          const destToUse = validDropTargets.has(resolvedId) ? resolvedId : storeTarget;
          const fromTerritory = (data as { territoryId?: string }).territoryId;
          if (fromTerritory) onBulkMoveDrop?.(fromTerritory, destToUse.trim());
        }
      }
      setTapBulkAllFromTerritory(null);
      setBulkDragOverlay(null);
      setActiveUnit(null);
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (isMobilizationAll) {
      let storeTarget = '';
      const ptr = ptrAtEnd;
      if (ptr) {
        storeTarget = pickValidTerritoryUnderPoint(ptr.x, ptr.y, validDropTargets, resolveTerritoryDropId);
      }
      if (!storeTarget && over) {
        const targetTerritory = (over.data.current as { territoryId?: unknown })?.territoryId;
        if (typeof targetTerritory === 'string') storeTarget = targetTerritory;
      }

      if (storeTarget && validDropTargets.size > 0) {
        const resolvedId = resolveTerritoryDropId(storeTarget) || storeTarget;
        const isValidDest = validDropTargets.has(resolvedId) || validDropTargets.has(storeTarget);
        if (isValidDest) {
          const destToUse = validDropTargets.has(resolvedId) ? resolvedId : storeTarget;
          const purchases = mobilizationTray?.purchases ?? [];
          const units = purchases.map(p => ({
            unitId: p.unitId,
            unitName: p.name,
            unitIcon: p.icon,
            count: p.count,
          }));
          if (units.length > 1) {
            const destCanon = resolveTerritoryDropId(destToUse.trim()) || destToUse.trim();
            setMobilizationDestinationClickCanon(destCanon);
            onMobilizationAllDrop?.(destToUse.trim(), units);
          }
        }
      }

      setTapMobilizationAll(false);
      setTapBulkAllFromTerritory(null);
      setBulkDragOverlay(null);
      setActiveUnit(null);
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (isMobilizationCamp && onCampDrop) {
      let targetTerritory = '';
      const ptr = ptrAtEnd;
      if (ptr && validDropTargets.size > 0) {
        targetTerritory = pickValidTerritoryUnderPoint(ptr.x, ptr.y, validDropTargets, resolveTerritoryDropId);
      }
      if (!targetTerritory && over) {
        const t = (over.data.current as { territoryId?: string })?.territoryId;
        if (typeof t === 'string') targetTerritory = t;
      }
      if (!targetTerritory && ptr) {
        const raw = territoryIdsUnderPoint(ptr.x, ptr.y)[0] ?? '';
        if (raw) {
          const r = resolveTerritoryDropId(raw) || raw;
          if (validDropTargets.has(r) || validDropTargets.has(raw)) targetTerritory = validDropTargets.has(r) ? r : raw;
        }
      }
      const campIndex = (data as { campIndex: number }).campIndex;
      if (targetTerritory && validDropTargets.has(targetTerritory)) {
        onCampDrop(campIndex, targetTerritory);
      }
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    if (isMobilization && onMobilizationDrop) {
      let targetTerritory = '';
      const ptr = ptrAtEnd;
      if (ptr && validDropTargets.size > 0) {
        targetTerritory = pickValidTerritoryUnderPoint(ptr.x, ptr.y, validDropTargets, resolveTerritoryDropId);
      }
      if (!targetTerritory && over) {
        const t = (over.data.current as { territoryId?: string })?.territoryId;
        if (typeof t === 'string') targetTerritory = t;
      }
      if (!targetTerritory && ptr) {
        const raw = territoryIdsUnderPoint(ptr.x, ptr.y)[0] ?? '';
        if (raw) {
          const r = resolveTerritoryDropId(raw) || raw;
          if (validDropTargets.has(r) || validDropTargets.has(raw)) targetTerritory = validDropTargets.has(r) ? r : raw;
        }
      }
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
          const destCanon = resolveTerritoryDropId(targetTerritory) || targetTerritory;
          setMobilizationDestinationClickCanon(destCanon);
          onMobilizationDrop(targetTerritory, unitId, unitName, icon, cappedCount);
        }
      }
      setActiveUnit(null);
      setActiveDragId(null);
      setValidDropTargets(new Set());
      return;
    }

    const unitForDrop = (active.id === 'tap-move' ? (active.data.current as typeof activeUnit) : activeUnit);
    if (unitForDrop) {
      let storeTarget = '';
      const ptr = ptrAtEnd;
      if (ptr && validDropTargets.size > 0) {
        storeTarget = pickValidTerritoryUnderPoint(ptr.x, ptr.y, validDropTargets, resolveTerritoryDropId);
      }
      if (!storeTarget && over) {
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
        storeTarget = (targetTerritory && !isBadDest) ? targetTerritory : '';
      }
      if (!storeTarget && event.collisions?.length) {
        const hit = event.collisions.find((c) => String(c.id).startsWith('territory-'));
        if (hit) {
          const tid = String(hit.id).slice('territory-'.length).trim();
          if (tid && tid !== '[object Object]' && !tid.includes('object')) storeTarget = tid;
        }
      }
      if (!storeTarget && ptr) {
        const raw = territoryIdsUnderPoint(ptr.x, ptr.y)[0] ?? '';
        if (raw) {
          const r = resolveTerritoryDropId(raw) || raw;
          if (validDropTargets.has(r) || validDropTargets.has(raw)) {
            storeTarget = validDropTargets.has(r) ? r : raw;
          }
        }
      }
      const backendDestId = resolveTerritoryDropId(storeTarget);
      // Only accept drop on a valid destination (backend reachability). Invalid = no dialog, units snap back.
      const resolvedId = backendDestId || storeTarget;
      const isValidDest = validDropTargets.has(resolvedId) || validDropTargets.has(storeTarget);
      const dropAccepted = !!storeTarget && isValidDest;
      if (dropAccepted) {
        const destToStash = resolvedId.trim();
        if (destToStash) _onDropDestination?.(destToStash);
        storeTarget = backendDestId || storeTarget; // prefer canonical id so confirm sends backend-canonical value (e.g. sea_zone_11)
        const isSeaT = (tid: string) => territoryData[tid]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
        // Offload / sea raid (naval, drop on land): find all sea zones adjacent to the land that the boat can reach (current zone or any destination). Include hostile zones so user can choose. Only show zone picker when multiple options.
        let effectiveToTerritory = storeTarget;
        let seaRaidSeaZoneOptions: string[] | undefined;
        let storeToTerritory = storeTarget;
        const isOffloadOrSeaRaid = (gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move') && unitForDrop.isNaval && !isSeaT(storeTarget);
        if (isOffloadOrSeaRaid) {
          const currentSeaRaw = typeof unitForDrop.territoryId === 'string' ? unitForDrop.territoryId : '';
          const currentSeaCanon = canonicalSeaZoneId(currentSeaRaw);
          // Sail reachability (matches backend get_sea_zones_reachable_by_sail). Do NOT use combat_move
          // destinations alone — those omit empty/friendly seas, which wrongly dropped valid offload zones
          // and left only hostile hexes → single "option" and hostile default.
          let maxSteps = 1;
          if (availableMoveTargets) {
            const mms = availableMoveTargets.filter(
              m =>
                (canonicalSeaZoneId(m.territory) === currentSeaCanon || m.territory === currentSeaRaw) &&
                m.unit.unit_id === unitForDrop.unitId
            );
            for (const m of mms) {
              const rm = m.unit?.remaining_movement;
              if (typeof rm === 'number' && rm > maxSteps) maxSteps = rm;
            }
          }
          const sailReach = seaZonesReachableBySailFrom(
            currentSeaRaw,
            maxSteps,
            territoryData,
            territoryUnits,
            gameState.current_faction,
            factionData,
            unitDefs,
          );
          const adj = territoryData[storeTarget]?.adjacent || [];
          const seaZonesAdjacentToLand = adj.filter((id: string) => isSeaT(id));
          const optionsCanon = seaZonesAdjacentToLand
            .map((sz: string) => canonicalSeaZoneId(sz))
            .filter((sz: string) => sailReach.has(sz));
          const options = sortSeaZoneIdsByNumericSuffix([...new Set(optionsCanon)]);
          if (options.length >= 1) {
            const getAlliance = (owner?: string) => (owner ? factionData[owner]?.alliance ?? null : null);
            const curAlliance = getAlliance(gameState.current_faction);
            const isSeaHostile = (sz: string): boolean => {
              const stacks = territoryUnits[sz] || territoryUnits[canonicalSeaZoneId(sz)] || [];
              return stacks.some((s) => {
                const uf = unitDefs[s.unit_id]?.faction;
                if (!uf || uf === gameState.current_faction) return false;
                const oa = getAlliance(uf);
                return oa != null && curAlliance != null && oa !== curAlliance;
              });
            };
            const pickDefaultSeaForMatch = (): string => {
              if (options.length === 1) return options[0];
              const atCurrent = options.find((o) => canonicalSeaZoneId(o) === currentSeaCanon);
              if (atCurrent) return atCurrent;
              const nonHostile = options.filter((o) => !isSeaHostile(o));
              if (nonHostile.length > 0) return sortSeaZoneIdsByNumericSuffix(nonHostile)[0];
              return options[0];
            };
            effectiveToTerritory = pickDefaultSeaForMatch();
            storeToTerritory = storeTarget;
            seaRaidSeaZoneOptions = options;
          }
        }
        // Max count = units in territory of this type minus already committed in other pending moves
        const totalInTerritory = (territoryUnits[unitForDrop.territoryId] || [])
          .filter(u => u.unit_id === unitForDrop.unitId)
          .reduce((s, u) => s + u.count, 0);
        const fromTidForPending = typeof unitForDrop.territoryId === 'string' ? unitForDrop.territoryId : '';
        const fromCanonForPending = canonicalSeaZoneId(fromTidForPending);
        const gp = gameState.phase ?? '';
        const fullForSource =
          (fromTidForPending && territoryUnitsFull?.[fromTidForPending]) ||
          territoryUnitsFull?.[fromCanonForPending] ||
          [];
        const committedIdsThisHex = committedInstanceIdsFromHex(
          pendingMoves,
          fromTidForPending,
          fromCanonForPending,
          gp,
        );
        // Prefer instance-id accounting (matches backend). Pending move unitType must not be inferred
        // from instance id strings (fragile for every faction); server sends primary_unit_id + we map in App.
        let committedElsewhere = 0;
        if (fullForSource.length > 0) {
          const byInstance = new Map(fullForSource.map(u => [u.instance_id, u]));
          for (const iid of committedIdsThisHex) {
            const u = byInstance.get(iid);
            if (!u || u.unit_id !== unitForDrop.unitId) continue;
            if (unitForDrop.isNaval) {
              if (navalUnitIds.has(u.unit_id)) committedElsewhere += 1;
            } else if (!navalUnitIds.has(u.unit_id)) {
              committedElsewhere += 1;
            }
          }
        } else {
          committedElsewhere = pendingMoves
            .filter(m => {
              const sameFrom =
                m.from === fromTidForPending ||
                canonicalSeaZoneId(m.from || '') === fromCanonForPending;
              return sameFrom && m.unitType === unitForDrop.unitId;
            })
            .reduce((s, m) => s + m.count, 0);
        }
        let availableCount = Math.max(0, totalInTerritory - committedElsewhere);
        // Show confirm dialog whenever we have units to move; backend will validate reachability on submit.
        if (availableCount <= 0) {
          setActiveUnit(null);
          setActiveDragId(null);
          setValidDropTargets(new Set());
          return;
        }
        const fromCanon = canonicalSeaZoneId(unitForDrop.territoryId);
        const destIncludes = (dests: string[] | undefined, tid: string) =>
          (dests ?? []).some(d => d === tid || canonicalSeaZoneId(d) === canonicalSeaZoneId(tid));
        /** Instance IDs the backend says can reach this destination (rm>0 and path exists). Drag stack can include exhausted units — confirm must not. */
        const movableInstanceIdsForDest = new Set(
          (availableMoveTargets ?? [])
            .filter(
              (m) =>
                (canonicalSeaZoneId(m.territory) === fromCanon || m.territory === unitForDrop.territoryId) &&
                m.unit.unit_id === unitForDrop.unitId &&
                destIncludes(m.destinations, effectiveToTerritory),
            )
            .map((m) => m.unit.instance_id),
        );
        const matches = (availableMoveTargets ?? []).filter(
          m =>
            (canonicalSeaZoneId(m.territory) === fromCanon || m.territory === unitForDrop.territoryId) &&
            m.unit.unit_id === unitForDrop.unitId &&
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
        const useInstanceIds = unitForDrop.instanceIds;
        // Land: only instance IDs still in this hex, not on a pending move, and able to reach this drop (backend moveable_units).
        const availableLandInstanceIds =
          !unitForDrop.isNaval && fullForSource.length > 0
            ? fullForSource
                .filter(
                  u =>
                    u.unit_id === unitForDrop.unitId &&
                    !navalUnitIds.has(u.unit_id) &&
                    !committedIdsThisHex.has(u.instance_id) &&
                    movableInstanceIdsForDest.has(u.instance_id),
                )
                .map(u => u.instance_id)
            : null;
        if (
          !unitForDrop.isNaval &&
          fullForSource.length > 0 &&
          availableLandInstanceIds !== null &&
          availableLandInstanceIds.length === 0
        ) {
          setActiveUnit(null);
          setActiveDragId(null);
          setValidDropTargets(new Set());
          return;
        }

        let moveCount: number;
        let moveMaxCount: number;
        let boatOptions: string[][] | undefined;
        let instanceIdsToUse: string[] | undefined;
        let navalBoatStacks: string[][] | undefined;

        if (availableLandInstanceIds !== null) {
          if (availableLandInstanceIds.length > 0) {
            instanceIdsToUse = [...availableLandInstanceIds];
            moveCount = availableLandInstanceIds.length;
            moveMaxCount = availableLandInstanceIds.length;
          } else {
            instanceIdsToUse = undefined;
            moveCount = Math.min(unitForDrop.count, availableCount);
            moveMaxCount = availableCount;
          }
        } else {
          let fromDrag = useInstanceIds && useInstanceIds.length > 0 ? [...useInstanceIds] : undefined;
          if (fromDrag && !unitForDrop.isNaval && movableInstanceIdsForDest.size > 0) {
            const filteredDrag = fromDrag.filter((id) => movableInstanceIdsForDest.has(id));
            if (filteredDrag.length === 0) {
              setActiveUnit(null);
              setActiveDragId(null);
              setValidDropTargets(new Set());
              return;
            }
            fromDrag = filteredDrag;
          }
          instanceIdsToUse = fromDrag;
          moveCount = instanceIdsToUse
            ? Math.min(instanceIdsToUse.length, availableCount)
            : Math.min(unitForDrop.count, availableCount);
          moveMaxCount = instanceIdsToUse
            ? Math.min(instanceIdsToUse.length, availableCount)
            : availableCount;
        }
        if (instanceIdsToUse && unitForDrop.isNaval && territoryUnitsFull?.[unitForDrop.territoryId]) {
          const fullUnits = territoryUnitsFull[unitForDrop.territoryId];
          const boatsOfType = fullUnits.filter(u => u.unit_id === unitForDrop.unitId && navalUnitIds.has(u.unit_id));
          const boatInstanceIdSet = new Set(boatsOfType.map(b => b.instance_id));
          const options = boatsOfType.map(boat => {
            const passengers = fullUnits.filter(u => u.loaded_onto === boat.instance_id);
            return [boat.instance_id, ...passengers.map(p => p.instance_id)];
          });
          if (options.length > 1) {
            // Tray drag: exactly one boat id in payload. Map stack drag: all boats of this type + their passengers.
            const dragBoatIds = (useInstanceIds ?? []).filter(id => boatInstanceIdSet.has(id));
            if (dragBoatIds.length === 1) {
              navalBoatStacks = undefined;
              const chosen = options.find(op => op[0] === dragBoatIds[0]);
              if (chosen) {
                instanceIdsToUse = [...chosen];
              }
            } else if (dragBoatIds.length > 1) {
              const stacksForDrag = options.filter(op => dragBoatIds.includes(op[0]));
              if (stacksForDrag.length >= 2) {
                navalBoatStacks = stacksForDrag;
                instanceIdsToUse = stacksForDrag.flat();
              } else if (stacksForDrag.length === 1) {
                navalBoatStacks = undefined;
                instanceIdsToUse = [...stacksForDrag[0]];
              } else {
                navalBoatStacks = undefined;
                const sameMakeup = options.every(op => op.length === options[0].length);
                if (sameMakeup) {
                  instanceIdsToUse = [...options[0]];
                } else {
                  boatOptions = options;
                  instanceIdsToUse = [...options[0]];
                }
              }
            } else {
              navalBoatStacks = undefined;
              const sameMakeup = options.every(op => op.length === options[0].length);
              if (sameMakeup) {
                instanceIdsToUse = [...options[0]];
              } else {
                boatOptions = options;
                instanceIdsToUse = [...options[0]];
              }
            }
          }
        }
        // Land → sea (embark): transportable land units only. Aerial → sea in combat is naval battle, not loading.
        const isLandToSeaLoad =
          !unitForDrop.isNaval && !unitIsAerial(unitForDrop.unitId, unitDefs) && isSeaT(storeTarget);
        if (isLandToSeaLoad && territoryUnitsFull?.[storeTarget]) {
          const seaCap = getLandToSeaLoadCapacityRemaining(
            storeTarget,
            territoryUnitsFull[storeTarget],
            unitDefs,
            navalUnitIds,
            gameState.current_faction,
            pendingMoves,
            gameState.phase,
            territoryData,
          );
          if (seaCap <= 0) {
            setActiveUnit(null);
            setActiveDragId(null);
            setValidDropTargets(new Set());
            return;
          }
          moveMaxCount = Math.min(moveMaxCount, seaCap);
          if (instanceIdsToUse && instanceIdsToUse.length > moveMaxCount) {
            instanceIdsToUse = instanceIdsToUse.slice(0, moveMaxCount);
          }
          if (instanceIdsToUse) {
            moveCount = instanceIdsToUse.length;
            moveMaxCount = instanceIdsToUse.length;
          } else {
            moveCount = Math.min(moveCount, moveMaxCount);
          }
        }
        // Land → land across ford-only link: cap confirm count by remaining escort slots / min ford edges (transport_capacity sum, not a hardcoded 2).
        const isLandToLandFordCap =
          !unitForDrop.isNaval &&
          !isSeaT(storeTarget) &&
          (gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move');
        if (isLandToLandFordCap && usesFordEscortBudget(unitDefs[unitForDrop.unitId])) {
          const fromKey = resolveTerritoryGraphKey(unitForDrop.territoryId, territoryData);
          const toKey = resolveTerritoryGraphKey(effectiveToTerritory, territoryData);
          let mf = minFordEdgesForLandMove(fromKey, toKey, territoryData);
          if (mf === 0 && directFordOnlyLandPair(fromKey, toKey, territoryData)) mf = 1;
          if (mf !== null && mf >= 1) {
            const lead = pendingFordCrosserLeadFromOrigin(
              unitForDrop.territoryId,
              gameState.phase,
              pendingMoves,
              territoryData,
              unitDefs,
              territoryUnitsFull ?? {},
            );
            if (lead) {
              const slots = remainingFordEscortSlotsClient(
                fromKey,
                gameState.phase,
                pendingMoves,
                territoryData,
                territoryUnitsFull ?? {},
                unitDefs,
                gameState.current_faction,
                new Set(),
              );
              const maxByFord = Math.max(0, Math.floor(slots / mf));
              moveMaxCount = Math.min(moveMaxCount, maxByFord);
              if (moveMaxCount <= 0) {
                setActiveUnit(null);
                setActiveDragId(null);
                setValidDropTargets(new Set());
                return;
              }
              if (instanceIdsToUse && instanceIdsToUse.length > moveMaxCount) {
                instanceIdsToUse = instanceIdsToUse.slice(0, moveMaxCount);
              }
              if (instanceIdsToUse) {
                moveCount = instanceIdsToUse.length;
                moveMaxCount = instanceIdsToUse.length;
              } else {
                moveCount = Math.min(moveCount, moveMaxCount);
              }
            }
          }
        }
        // Load (land -> sea): when destination has 2+ boats, build boatOptions so tray can show allocation / "Load into this boat"
        if (
          instanceIdsToUse &&
          !unitForDrop.isNaval &&
          !unitIsAerial(unitForDrop.unitId, unitDefs) &&
          territoryUnitsFull?.[storeTarget] &&
          isSeaT(storeTarget)
        ) {
          const fullUnits = territoryUnitsFull[storeTarget];
          const boats = fullUnits.filter(u => navalUnitIds.has(u.unit_id));
          if (boats.length >= 2) {
            boatOptions = boats.map(boat => [boat.instance_id, ...instanceIdsToUse]);
          }
        }
        // Confirm UI: count ships, not passengers. Multi-boat map drag: +/- = number of ships. Single ship raid/offload: 1.
        if (navalBoatStacks && navalBoatStacks.length > 1) {
          moveCount = navalBoatStacks.length;
          moveMaxCount = navalBoatStacks.length;
        } else if (isOffloadOrSeaRaid && unitForDrop.isNaval && instanceIdsToUse && instanceIdsToUse.length > 0) {
          moveCount = 1;
          moveMaxCount = 1;
        }
        const fromId = typeof unitForDrop.territoryId === 'string' ? unitForDrop.territoryId : String((unitForDrop.territoryId as { id?: string; territoryId?: string })?.territoryId ?? (unitForDrop.territoryId as { id?: string })?.id ?? unitForDrop.territoryId);
        const toStrStored = (typeof storeToTerritory === 'string' ? storeToTerritory : String((storeToTerritory as { id?: string; territoryId?: string })?.territoryId ?? (storeToTerritory as { id?: string })?.id ?? storeToTerritory)).trim();
        if (toStrStored === '[object Object]' || !toStrStored) return; // never store bad destination
        _onDropDestination?.(toStrStored);
        onSetPendingMove({
          fromTerritory: fromId,
          toTerritory: toStrStored,
          unitId: unitForDrop.unitId,
          unitDef: unitForDrop.unitDef,
          maxCount: moveMaxCount,
          count: moveCount,
          chargeThrough: paths.length <= 1 ? chargeThrough : undefined,
          chargePathOptions: paths.length > 1 ? paths : undefined,
          ...(instanceIdsToUse && instanceIdsToUse.length > 0 ? { instanceIds: instanceIdsToUse } : {}),
          ...(boatOptions && boatOptions.length > 1 ? { boatOptions } : {}),
          ...(navalBoatStacks && navalBoatStacks.length > 1 ? { navalBoatStacks } : {}),
          ...(seaRaidSeaZoneOptions && seaRaidSeaZoneOptions.length >= 1 ? { seaRaidSeaZoneOptions } : {}),
        });
      }
    }
    setTapBulkAllFromTerritory(null);
    setBulkDragOverlay(null);
    setActiveUnit(null);
    setActiveDragId(null);
    setValidDropTargets(new Set());
  }, [activeUnit, validDropTargets, territoryUnits, territoryUnitsFull, pendingMoves, availableMoveTargets, navalUnitIds, onSetPendingMove, _onDropDestination, onBulkMoveDrop, onMobilizationDrop, onMobilizationAllDrop, onCampDrop, gameState.phase, gameState.current_faction, factionData, unitDefs, territoryData, resolveTerritoryDropId, canAct]);

  const handleDragCancel = useCallback(() => {
    lastMapDragPointerRef.current = null;
    setTapBulkAllFromTerritory(null);
    setTapMobilizationAll(false);
    setBulkDragOverlay(null);
    setActiveUnit(null);
    setActiveDragId(null);
    setValidDropTargets(new Set());
  }, []);

  // Handle territory click (toggle selection, or tap-to-drop when unit was tapped first)
  const handleTerritoryClick = useCallback((territoryId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const dx = e.clientX - panStartPos.current.x;
    const dy = e.clientY - panStartPos.current.y;
    if (dx * dx + dy * dy >= PAN_CLICK_THRESHOLD_PX * PAN_CLICK_THRESHOLD_PX) return; // Was a pan, not a click

    // Tap-to-drop bulk: All then destination (mobile)
    if (canAct && tapBulkAllFromTerritory && territoryMatchesValidDrop(territoryId)) {
      const resolvedId = resolveTerritoryDropId(territoryId) || territoryId;
      const destToUse = validDropTargets.has(resolvedId) ? resolvedId : territoryId;
      onBulkMoveDrop?.(tapBulkAllFromTerritory, destToUse.trim());
      setTapBulkAllFromTerritory(null);
      setValidDropTargets(new Set());
      return;
    }
    // Tray “All” then tap valid mobilization destination (mobile)
    if (canAct && tapMobilizationAll && territoryMatchesValidDrop(territoryId)) {
      const resolvedId = resolveTerritoryDropId(territoryId) || territoryId;
      const destToUse = validDropTargets.has(resolvedId) ? resolvedId : territoryId;
      const purchases = mobilizationTray?.purchases ?? [];
      const units = purchases.map(p => ({
        unitId: p.unitId,
        unitName: p.name,
        unitIcon: p.icon,
        count: p.count,
      }));
      if (units.length > 1) {
        const destCanon = resolveTerritoryDropId(destToUse.trim()) || destToUse.trim();
        setMobilizationDestinationClickCanon(destCanon);
        onMobilizationAllDrop?.(destToUse.trim(), units);
      }
      setTapMobilizationAll(false);
      setValidDropTargets(new Set());
      setBulkDragOverlay(null);
      return;
    }
    // Tap-to-drop: user previously tapped a unit; this territory is a valid destination
    if (canAct && tapSelectedUnit && territoryMatchesValidDrop(territoryId)) {
      const syntheticEvent = {
        active: { id: 'tap-move', data: { current: tapSelectedUnit } },
        over: { id: `territory-${territoryId}`, data: { current: { territoryId } } },
        activatorEvent: e.nativeEvent,
        collisions: null,
        delta: { x: 0, y: 0 },
      } as unknown as DragEndEvent;
      handleDragEnd(syntheticEvent);
      setTapSelectedUnit(null);
      return;
    }
    const hadMobilizationAllTap = tapMobilizationAll;
    const hadAnyTapMove = tapSelectedUnit != null || tapBulkAllFromTerritory != null || tapMobilizationAll;
    setTapSelectedUnit(null);
    setTapBulkAllFromTerritory(null);
    setTapMobilizationAll(false);
    if (hadAnyTapMove) {
      setValidDropTargets(new Set());
      if (hadMobilizationAllTap) setBulkDragOverlay(null);
    }
    setExpandedStackKey(null);
    if (selectedTerritory === territoryId) {
      onTerritorySelect(null);
    } else {
      onTerritorySelect(territoryId);
    }
    onUnitSelect(null);
  }, [
    selectedTerritory,
    onTerritorySelect,
    onUnitSelect,
    tapSelectedUnit,
    tapBulkAllFromTerritory,
    tapMobilizationAll,
    mobilizationTray?.purchases,
    validDropTargets,
    territoryMatchesValidDrop,
    resolveTerritoryDropId,
    onBulkMoveDrop,
    onMobilizationAllDrop,
    handleDragEnd,
    canAct,
  ]);

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
    setTapSelectedUnit(null);
    setTapBulkAllFromTerritory(null);
    setTapMobilizationAll(false);
    setValidDropTargets(new Set());
    setBulkDragOverlay(null);
    setExpandedStackKey(null);
    onTerritorySelect(null);
    onUnitSelect(null);
  }, [onTerritorySelect, onUnitSelect]);

  // Pan handlers: allow pan from anywhere (including territory paths); only treat as territory click if drag was minimal
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest('.unit-token')) return; // Don't start pan when pressing on a unit (so unit drag works)
    if (target.closest('.all-stacks-drag-btn')) return; // Bulk "All" stack drag uses dnd-kit
    if (target.closest('.mobilize-all-btn')) return; // Mobilization tray “All”: tap or dnd-kit drag
    if (target.closest('.territory-units--sea-stack')) return; // Don't start pan when clicking boat stack (opens naval tray)
    if (target.closest('.sea-zone-tray-open-btn')) return; // Open boat list (mobile / tap)

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

  // Touch: pinch to zoom, pan with one or two fingers
  const touchPinchRef = useRef<{ distance: number; centerX: number; centerY: number; scale: number; x: number; y: number } | null>(null);
  const touchPanRef = useRef<{ startX: number; startY: number; startTransformX: number; startTransformY: number } | null>(null);
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const getTouchCenter = (touches: TouchList) => {
      if (touches.length === 0) return { x: 0, y: 0 };
      let x = 0, y = 0;
      for (let i = 0; i < touches.length; i++) {
        x += touches[i].clientX;
        y += touches[i].clientY;
      }
      return { x: x / touches.length, y: y / touches.length };
    };
    const getTouchDistance = (touches: TouchList) => {
      if (touches.length < 2) return 0;
      const a = touches[0], b = touches[1];
      return Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY);
    };
    const handleTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        const center = getTouchCenter(e.touches);
        touchPinchRef.current = {
          distance: getTouchDistance(e.touches),
          centerX: center.x - rect.left,
          centerY: center.y - rect.top,
          scale: transformRef.current.scale,
          x: transformRef.current.x,
          y: transformRef.current.y,
        };
        touchPanRef.current = null;
      } else if (e.touches.length === 1 && !touchPinchRef.current) {
        const target = e.target as HTMLElement;
        if (
          target.closest('.unit-token') ||
          target.closest('.all-stacks-drag-btn') ||
          target.closest('.mobilize-all-btn') ||
          target.closest('.territory-units--sea-stack') ||
          target.closest('.sea-zone-tray-open-btn')
        )
          return;
        const x = e.touches[0].clientX;
        const y = e.touches[0].clientY;
        touchPanRef.current = {
          startX: x,
          startY: y,
          startTransformX: transformRef.current.x,
          startTransformY: transformRef.current.y,
        };
        // Match mousedown so synthesized click on territories uses the same origin as touch (mobile tap-to-move / mobilization).
        panStartPos.current = { x, y };
      }
    };
    const handleTouchMove = (e: TouchEvent) => {
      if (e.touches.length === 2 && touchPinchRef.current) {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        const distance = getTouchDistance(e.touches);
        const center = getTouchCenter(e.touches);
        const centerX = center.x - rect.left;
        const centerY = center.y - rect.top;
        const scaleRatio = distance / touchPinchRef.current.distance;
        const newScale = Math.max(
          Math.max(0.1, fitScaleRef.current),
          Math.min(MAX_SCALE, touchPinchRef.current.scale * scaleRatio)
        );
        const ratio = newScale / touchPinchRef.current.scale;
        let newX = centerX - (centerX - touchPinchRef.current.x) * ratio;
        let newY = centerY - (centerY - touchPinchRef.current.y) * ratio;
        const dims = imgDimensionsRef.current;
        const scaledW = dims.width * newScale;
        const scaledH = dims.height * newScale;
        const minX = Math.min(0, el.clientWidth - scaledW);
        const minY = Math.min(0, el.clientHeight - scaledH);
        const maxX = Math.max(0, el.clientWidth - scaledW);
        const maxY = Math.max(0, el.clientHeight - scaledH);
        newX = Math.max(minX, Math.min(maxX, newX));
        newY = Math.max(minY, Math.min(maxY, newY));
        setTransform({ x: newX, y: newY, scale: newScale });
      } else if (e.touches.length === 1 && touchPanRef.current) {
        e.preventDefault();
        const dx = e.touches[0].clientX - touchPanRef.current.startX;
        const dy = e.touches[0].clientY - touchPanRef.current.startY;
        let newX = touchPanRef.current.startTransformX + dx;
        let newY = touchPanRef.current.startTransformY + dy;
        const dims = imgDimensionsRef.current;
        const scaledW = dims.width * transformRef.current.scale;
        const scaledH = dims.height * transformRef.current.scale;
        const minX = Math.min(0, el.clientWidth - scaledW);
        const minY = Math.min(0, el.clientHeight - scaledH);
        const maxX = Math.max(0, el.clientWidth - scaledW);
        const maxY = Math.max(0, el.clientHeight - scaledH);
        newX = Math.max(minX, Math.min(maxX, newX));
        newY = Math.max(minY, Math.min(maxY, newY));
        setTransform(prev => ({ ...prev, x: newX, y: newY }));
      }
    };
    const handleTouchEnd = (e: TouchEvent) => {
      if (e.touches.length < 2) touchPinchRef.current = null;
      if (e.touches.length < 1) touchPanRef.current = null;
    };
    el.addEventListener('touchstart', handleTouchStart, { passive: false });
    el.addEventListener('touchmove', handleTouchMove, { passive: false });
    el.addEventListener('touchend', handleTouchEnd, { passive: true });
    el.addEventListener('touchcancel', handleTouchEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', handleTouchStart);
      el.removeEventListener('touchmove', handleTouchMove);
      el.removeEventListener('touchend', handleTouchEnd);
      el.removeEventListener('touchcancel', handleTouchEnd);
    };
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

  // Clamp overlay anchors to the map image. Sides/top stay generous (e.g. far_harad); bottom is tighter so
  // southern territories (Umbar, etc.) can place unit stacks without the cap yanking them onto the marker row.
  const CLAMP_INSET_X = 80;
  const CLAMP_INSET_Y_TOP = 80;
  const CLAMP_INSET_Y_BOTTOM = 28;
  const clampToMap = (p: { x: number; y: number }) => ({
    x: Math.max(CLAMP_INSET_X, Math.min(IMG_DIMENSIONS.width - CLAMP_INSET_X, p.x)),
    y: Math.max(CLAMP_INSET_Y_TOP, Math.min(IMG_DIMENSIONS.height - CLAMP_INSET_Y_BOTTOM, p.y)),
  });

  // Fallback position when territory has units but no centroid (e.g. not in SVG yet). Deterministic per territoryId so they don't stack.
  const fallbackPositionForTerritory = (territoryId: string) => {
    let h = 0;
    for (let i = 0; i < territoryId.length; i++) h = (h * 31 + territoryId.charCodeAt(i)) >>> 0;
    const t = (h % 1000) / 1000;
    const u = ((h >> 10) % 1000) / 1000;
    const innerW = IMG_DIMENSIONS.width - CLAMP_INSET_X * 2;
    const innerH = IMG_DIMENSIONS.height - CLAMP_INSET_Y_TOP - CLAMP_INSET_Y_BOTTOM;
    return clampToMap({
      x: CLAMP_INSET_X + t * innerW,
      y: CLAMP_INSET_Y_TOP + u * innerH,
    });
  };

  return (
    <div className="game-map-root">
    <DndContext
      sensors={sensors}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      onDragCancel={handleDragCancel}
      collisionDetection={mapCollisionDetection}
    >
      <div className={`map-container ${mobilizationTray || navalTray ? 'map-container--with-tray' : ''}`}>
        <div className="map-content">
          {mapKeyOpen && (
            <div className="map-key-strip" role="region" aria-label="Map key">
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⛺</span> Camp</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>🌲</span> Forest</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>🏠</span> Home</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⛰️</span> Mountains</span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden>⚓</span> Port</span>
              <span className="map-key-item">
                <img src="/bridge.png" alt="" className="map-key-img" aria-hidden />
                Bridge
              </span>
              <span className="map-key-item">
                <img src="/ford.png" alt="" className="map-key-img" aria-hidden />
                Ford
              </span>
              <span className="map-key-item"><span className="map-key-icon" aria-hidden></span> Strongholds have faction logo (capitals larger)</span>
            </div>
          )}
          <div className="map-main">
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
                      const defA = territoryData[idA] ?? territoryData[resolveTerritoryDropId(idA)];
                      const defB = territoryData[idB] ?? territoryData[resolveTerritoryDropId(idB)];
                      const seaA = defA?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(String(idA));
                      const seaB = defB?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(String(idB));
                      if (seaA === seaB) return 0;
                      return seaA ? 1 : -1;
                    })
                    .map(([tid, pathData]) => {
                      const territoryId =
                        typeof tid === 'string'
                          ? tid
                          : tid != null && typeof tid === 'object' && 'id' in (tid as object)
                            ? String((tid as { id: string }).id)
                            : tid != null && typeof tid === 'object' && 'territoryId' in (tid as object)
                              ? String((tid as { territoryId: string }).territoryId)
                              : String(tid ?? '');
                      if (!territoryId || territoryId === '[object Object]') return null;
                      const stateKey = resolveTerritoryDropId(territoryId);
                      const territory = territoryData[territoryId] ?? territoryData[stateKey];
                      const owner = territory?.owner;
                      const isNonOwnable = territory && (territory.ownable === false);
                      const isSeaZone = territory?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(territoryId);
                      // Definitions may load after game state (e.g. create-game nav + getGame without embedded defs);
                      // missing faction palette must not yield undefined — glow/filter code calls .replace on color.
                      const color = isSeaZone
                        ? '#2d4258'
                        : owner
                          ? (factionData[owner]?.color ?? '#d4c4a8')
                          : isNonOwnable
                            ? '#7a7a7a'
                            : '#d4c4a8';
                      const isSelected =
                        selectedTerritory === territoryId ||
                        selectedTerritory === stateKey;
                      const isValidDrop = territoryMatchesValidDrop(territoryId);
                      const hasUnitsToMobilize = (mobilizationTray?.purchases?.length ?? 0) > 0;
                      const selectedLandUnitId = mobilizationTray?.selectedUnitId && !navalUnitIds.has(mobilizationTray.selectedUnitId) ? mobilizationTray.selectedUnitId : null;
                      const capLand = remainingMobilizationCapacity[territoryId] ?? remainingMobilizationCapacity[stateKey] ?? 0;
                      const homeForTerr = remainingHomeSlots[territoryId] ?? remainingHomeSlots[stateKey] ?? {};
                      const hasMobilizationRoom =
                        capLand > 0 ||
                        (selectedLandUnitId ? (homeForTerr[selectedLandUnitId] ?? 0) > 0 : Object.values(homeForTerr).some((n: number) => n > 0));
                      const isValidMobilizationTarget =
                        isMobilizePhase &&
                        (validMobilizeTerritories.includes(territoryId) || validMobilizeTerritories.includes(stateKey)) &&
                        hasMobilizationRoom &&
                        (activeDragId != null ? isValidDrop : (hasMobilizationSelected || hasUnitsToMobilize));
                      const pendingSidebarDest = (mobilizationPendingDestination ?? '').trim();
                      const matchesPendingSidebarDest =
                        pendingSidebarDest.length > 0 &&
                        (territoryId === pendingSidebarDest ||
                          stateKey === pendingSidebarDest ||
                          resolveTerritoryDropId(territoryId) === resolveTerritoryDropId(pendingSidebarDest));
                      const isExternallyHighlighted =
                        highlightedTerritories.includes(territoryId) ||
                        highlightedTerritories.includes(stateKey) ||
                        matchesPendingSidebarDest;
                      const isCampPlacementTarget =
                        isMobilizePhase &&
                        ((validCampTerritories.length > 0 &&
                          (validCampTerritories.includes(territoryId) || validCampTerritories.includes(stateKey))) ||
                          territoryMatchesValidDrop(territoryId));
                      const isValidMobilizationTargetSea =
                        isMobilizePhase &&
                        (validMobilizeSeaZones.includes(territoryId) || validMobilizeSeaZones.includes(stateKey)) &&
                        hasMobilizationRoom &&
                        (activeDragId != null ? isValidDrop : (hasMobilizationSelected || (mobilizationTray?.purchases?.length ?? 0) > 0));
                      const isMobilizationZone = isValidMobilizationTarget || isValidMobilizationTargetSea;
                      const thisCanon = resolveTerritoryDropId(territoryId) || territoryId;
                      const mobilizationMuted =
                        mobilizationDestinationClickCanon != null &&
                        isMobilizationZone &&
                        thisCanon !== mobilizationDestinationClickCanon;
                      const mobilizationStrong = isMobilizationZone && !mobilizationMuted;
                      const isTapMoveTarget =
                        (tapSelectedUnit != null || tapBulkAllFromTerritory != null || tapMobilizationAll) &&
                        territoryMatchesValidDrop(territoryId);
                      return (
                        <DroppableTerritory
                          key={territoryId}
                          territoryId={territoryId}
                          pathData={pathData}
                          color={color}
                          isSeaZone={!!isSeaZone}
                          isSelected={isSelected}
                          isHighlighted={mobilizationStrong || isExternallyHighlighted || isCampPlacementTarget || isTapMoveTarget}
                          isValidDrop={isValidDrop || mobilizationStrong || isExternallyHighlighted}
                          highlightMuted={mobilizationMuted}
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
                    <marker id="arrowhead-combat" markerWidth="5" markerHeight="5" refX="3.5" refY="2.5" orient="auto">
                      <polygon points="0,0 5,2.5 0,5" fill="#c62828" />
                    </marker>
                    <marker id="arrowhead-move" markerWidth="5" markerHeight="5" refX="3.5" refY="2.5" orient="auto">
                      <polygon points="0,0 5,2.5 0,5" fill="#2e7d32" />
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
                        strokeWidth="4"
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
                      const ids = def.home_territory_ids ?? [];
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
                    const isSeaZone = territory.terrain === 'sea' || /^sea_zone_?\d+$/i.test(territoryId);
                    const seaZoneNum = isSeaZone ? (territoryId.match(/(\d+)$/)?.[1] ?? '') : null;
                    const terrainType = (territory.terrain || '').toLowerCase();
                    const showTerrainMountain = terrainType === 'mountains';
                    const showTerrainForest = terrainType === 'forest';
                    const showTerrainIcon = showTerrainMountain || showTerrainForest;
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
                                  <div className="stronghold-faction-hp-group">
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
                                    {((territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0) > 0 && (() => {
                                      const base = (territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0;
                                      const current = (territory as { stronghold_current_health?: number }).stronghold_current_health ?? base;
                                      return (
                                        <div
                                          className="stronghold-hp-bars"
                                          title={`Stronghold HP: ${current}/${base}`}
                                          style={{ ['--faction-color' as string]: factionData[territory.owner!]?.color ?? '#888' }}
                                        >
                                          {Array.from({ length: base }, (_, i) => (
                                            <span key={i} className="stronghold-hp-bar" data-filled={i < current ? 'true' : 'false'} />
                                          ))}
                                        </div>
                                      );
                                    })()}
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
                                      {showTerrainMountain && (
                                        <div className="territory-marker terrain-marker" title="Mountain">⛰️</div>
                                      )}
                                      {showTerrainForest && (
                                        <div className="territory-marker terrain-marker" title="Forest">🌲</div>
                                      )}
                                    </div>
                                  )}
                                  {showFactionLogo && (
                                    <div className="stronghold-faction-hp-group">
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
                                      {((territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0) > 0 && (
                                        <div
                                          className="stronghold-hp-bars"
                                          title={`Stronghold HP: ${(territory as { stronghold_current_health?: number }).stronghold_current_health ?? (territory as { stronghold_base_health?: number }).stronghold_base_health}/${(territory as { stronghold_base_health?: number }).stronghold_base_health}`}
                                          style={{ ['--faction-color' as string]: factionData[territory.owner!]?.color ?? '#888' }}
                                        >
                                          {Array.from({ length: (territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0 }, (_, i) => {
                                            const current = (territory as { stronghold_current_health?: number }).stronghold_current_health ?? (territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0;
                                            return <span key={i} className="stronghold-hp-bar" data-filled={i < current ? 'true' : 'false'} />;
                                          })}
                                        </div>
                                      )}
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
                    const isSeaZone = territory?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(territoryId);
                    const fullUnits = territoryUnitsFull?.[territoryId];

                    // Sea zone with full unit data: ships (+ passengers on those rows). Non-naval surface units (e.g. aerial after naval battle) render in a second row — do not return null when boats are gone.
                    if (isSeaZone && fullUnits?.length && navalUnitIds?.size) {
                      const navalUnits = fullUnits.filter(u => navalUnitIds.has(u.unit_id));
                      const totalBoats = navalUnits.length;
                      const totalPassengers = fullUnits.filter(u => u.loaded_onto).length;
                      const eligiblePending =
                        seaZoneIdsEligibleForNavalTrayStackClick.has(territoryId) ||
                        seaZoneIdsEligibleForNavalTrayStackClick.has(canonicalSeaZoneId(territoryId));
                      const showTrayDblClick = totalBoats > 1;

                      const boatCountByType = new Map<string, number>();
                      for (const u of navalUnits) {
                        boatCountByType.set(u.unit_id, (boatCountByType.get(u.unit_id) ?? 0) + 1);
                      }
                      const navalTypes = [...boatCountByType.keys()];
                      navalTypes.sort((a, b) =>
                        compareMapUnitStacks(
                          { unit_id: a, count: boatCountByType.get(a) ?? 0 },
                          { unit_id: b, count: boatCountByType.get(b) ?? 0 },
                          unitDefs,
                          factionData,
                        ),
                      );

                      const surfaceNonNaval = fullUnits.filter(
                        (u) => !navalUnitIds.has(u.unit_id) && !u.loaded_onto,
                      );
                      const otherByType = new Map<string, typeof fullUnits>();
                      for (const u of surfaceNonNaval) {
                        if (!otherByType.has(u.unit_id)) otherByType.set(u.unit_id, []);
                        otherByType.get(u.unit_id)!.push(u);
                      }
                      const otherTypes = [...otherByType.keys()];
                      otherTypes.sort((a, b) =>
                        compareMapUnitStacks(
                          { unit_id: a, count: otherByType.get(a)?.length ?? 0 },
                          { unit_id: b, count: otherByType.get(b)?.length ?? 0 },
                          unitDefs,
                          factionData,
                        ),
                      );

                      if (navalTypes.length === 0 && otherTypes.length === 0) return null;

                      const useStacked = navalTypes.length >= 3;
                      const handleStackDoubleClick = showTrayDblClick
                        ? (e: React.MouseEvent) => {
                          e.stopPropagation();
                          onSeaZoneStackClick?.(territoryId);
                        }
                        : undefined;
                      const handleOpenNavalTrayClick = showTrayDblClick
                        ? (e: React.MouseEvent) => {
                          e.stopPropagation();
                          onSeaZoneStackClick?.(territoryId);
                        }
                        : undefined;
                      const cancelNavalTrayLongPress = () => {
                        if (navalTrayLongPressTimerRef.current != null) {
                          clearTimeout(navalTrayLongPressTimerRef.current);
                          navalTrayLongPressTimerRef.current = null;
                        }
                        const s = navalTrayLongPressStartRef.current;
                        if (s?.territoryId === territoryId) navalTrayLongPressStartRef.current = null;
                      };
                      const handleNavalStackPointerDown = (e: ReactPointerEvent) => {
                        if (!coarsePointer || !showTrayDblClick || !onSeaZoneStackClick) return;
                        if (e.button !== 0) return;
                        cancelNavalTrayLongPress();
                        navalTrayLongPressStartRef.current = { x: e.clientX, y: e.clientY, territoryId };
                        navalTrayLongPressTimerRef.current = window.setTimeout(() => {
                          navalTrayLongPressTimerRef.current = null;
                          navalTrayLongPressStartRef.current = null;
                          onSeaZoneStackClick(territoryId);
                        }, 520);
                      };
                      const handleNavalStackPointerMove = (e: ReactPointerEvent) => {
                        const s = navalTrayLongPressStartRef.current;
                        if (!s || s.territoryId !== territoryId) return;
                        const dx = e.clientX - s.x;
                        const dy = e.clientY - s.y;
                        if (dx * dx + dy * dy > 144) cancelNavalTrayLongPress();
                      };
                      const left = screenPos.x;
                      const top = screenPos.y + unitOffsetY + powerBadgeOffsetY;
                      return (
                        <div
                          key={territoryId}
                          className="territory-units territory-units--sea-combined"
                          style={{
                            left,
                            top,
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'flex-start',
                            gap: 4,
                          }}
                        >
                          {navalTypes.length > 0 && (
                            <div
                              className={`territory-units territory-units--sea-stack-row ${showTrayDblClick ? 'territory-units--sea-stack' : ''}`}
                              style={{ position: 'relative', left: 0, top: 0, display: 'flex', alignItems: 'flex-end', gap: 6, flexWrap: 'wrap' }}
                            >
                              {showTrayDblClick && onSeaZoneStackClick && (
                                <button
                                  type="button"
                                  className="sea-zone-tray-open-btn sea-zone-tray-open-btn--fine-pointer"
                                  onClick={handleOpenNavalTrayClick}
                                  title="Ship list and passengers (double-click stack also works)"
                                  aria-label={`Open ship list for ${territory?.name ?? territoryId}`}
                                >
                                  <span className="sea-zone-tray-open-btn__icon" aria-hidden>
                                    ☰
                                  </span>
                                  <span className="sea-zone-tray-open-btn__text">Ships</span>
                                </button>
                              )}
                              <div
                                className={`territory-units ${useStacked ? 'territory-units--stacked' : ''}`}
                                style={{ position: 'relative', left: 0, top: 0 }}
                                onDoubleClick={handleStackDoubleClick}
                                onPointerDown={handleNavalStackPointerDown}
                                onPointerMove={handleNavalStackPointerMove}
                                onPointerUp={cancelNavalTrayLongPress}
                                onPointerCancel={cancelNavalTrayLongPress}
                                onPointerLeave={cancelNavalTrayLongPress}
                                title={
                                  showTrayDblClick
                                    ? coarsePointer
                                      ? `Long-press this stack for ship list & passengers, or drag to move.${
                                          eligiblePending && totalPassengers < 1 ? ' Pending loads into this zone.' : ''
                                        }`
                                      : `Double-click stack or use the list control for ships & passengers. Drag a ship stack to move.${
                                          eligiblePending && totalPassengers < 1 ? ' Pending loads into this zone.' : ''
                                        }`
                                    : undefined
                                }
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
                                const canDrag =
                                  canAct && isMovementPhase && unitFaction === gameState.current_faction;
                                return (
                                  <span
                                    key={`${territoryId}-${unit_id}`}
                                    className={`unit-token-tap-wrapper${tapSelectedUnit?.territoryId === territoryId && tapSelectedUnit?.unitId === unit_id ? ' unit-token-tap-wrapper--selected' : ''}`}
                                    onPointerDownCapture={(ev) => handleUnitPointerDownCapture(territoryId, unit_id, ev)}
                                    style={{ display: 'inline-block' }}
                                  >
                                    <DraggableUnit
                                      id={`${territoryId}-${unit_id}`}
                                      unitId={unit_id}
                                      territoryId={territoryId}
                                      count={boatCount}
                                      unitDef={unitDefs[unit_id]}
                                      isSelected={selectedUnit?.territory === territoryId && selectedUnit?.unitType === unit_id}
                                      disabled={!canDrag}
                                      factionColor={unitFactionColor}
                                      showAerialMustMove={aerialMustMoveKeySet.has(`${territoryId}_${unit_id}`)}
                                      showNavalMustAttack={
                                        gameState.phase === 'combat_move' &&
                                        instanceIds.some((id) => loadedNavalMustAttackInstanceIdSet.has(id))
                                      }
                                      showForcedNavalStandoff={
                                        gameState.phase === 'combat_move' &&
                                        instanceIds.some(
                                          (id) =>
                                            forcedNavalCombatInstanceIdSet.has(id) &&
                                            !loadedNavalMustAttackInstanceIdSet.has(id)
                                        )
                                      }
                                      isNaval
                                      passengerCount={passengerCount}
                                      instanceIds={instanceIds}
                                    />
                                  </span>
                                );
                              })}
                              </div>
                            </div>
                          )}
                          {otherTypes.length > 0 && (
                            <div className="territory-units-token-row" style={{ position: 'relative', left: 0, top: 0 }}>
                              {otherTypes.map((unit_id) => {
                                const list = otherByType.get(unit_id)!;
                                const count = list.length;
                                const instanceIdsForUnit = list.map((u) => u.instance_id);
                                const parts = unit_id.split('_');
                                const factionFromId = parts.find(p => factionData[p]);
                                const colorFromId = factionFromId ? factionData[factionFromId].color : null;
                                const defFaction = unitDefs[unit_id]?.faction;
                                const colorFromDef = defFaction && factionData[defFaction] ? factionData[defFaction].color : null;
                                const unitFactionColor = colorFromId ?? colorFromDef ?? NEUTRAL_UNIT_BORDER;
                                const unitFaction = factionFromId ?? defFaction ?? parts[0];
                                const canDrag =
                                  canAct && isMovementPhase && unitFaction === gameState.current_faction;
                                return (
                                  <span
                                    key={`${territoryId}-${unit_id}-sea-surface`}
                                    className={`unit-token-tap-wrapper${tapSelectedUnit?.territoryId === territoryId && tapSelectedUnit?.unitId === unit_id ? ' unit-token-tap-wrapper--selected' : ''}`}
                                    onPointerDownCapture={(ev) => handleUnitPointerDownCapture(territoryId, unit_id, ev)}
                                    style={{ display: 'inline-block' }}
                                  >
                                    <DraggableUnit
                                      id={`${territoryId}-${unit_id}-sea-surface`}
                                      unitId={unit_id}
                                      territoryId={territoryId}
                                      count={count}
                                      unitDef={unitDefs[unit_id]}
                                      isSelected={selectedUnit?.territory === territoryId && selectedUnit?.unitType === unit_id}
                                      disabled={!canDrag}
                                      factionColor={unitFactionColor}
                                      showAerialMustMove={aerialMustMoveKeySet.has(`${territoryId}_${unit_id}`)}
                                      showNavalMustAttack={false}
                                      showForcedNavalStandoff={false}
                                      isNaval={false}
                                      instanceIds={instanceIdsForUnit}
                                    />
                                  </span>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    }

                    // Land (or sea without full data): stacked tokens by unit type
                    const stackCount = units.length;
                    const useStacked = stackCount >= 3;
                    const sortedUnits = [...units].sort((a, b) =>
                      compareMapUnitStacks(a, b, unitDefs, factionData),
                    );

                    const bulkMinRm = minRemainingMovementForBulkAll(
                      territoryId,
                      territoryUnits,
                      territoryUnitsFull,
                      gameState.current_faction,
                      factionData,
                      unitDefs,
                      pendingMoves,
                      gameState.phase,
                    );
                    /** Bulk "All" only when every stack is the current turn faction (not allies you can't command). */
                    const allStacksAreCurrentTurnFaction = units.every((s) => {
                      const parts = s.unit_id.split('_');
                      const factionFromId = parts.find((p) => factionData[p]);
                      const defFaction = unitDefs[s.unit_id]?.faction;
                      const uf = factionFromId ?? defFaction ?? parts[0];
                      return uf === gameState.current_faction;
                    });
                    const canShowAllDrag =
                      canAct &&
                      isMovementPhase &&
                      allStacksAreCurrentTurnFaction &&
                      stackCount > 1 &&
                      bulkMinRm != null &&
                      bulkMinRm > 0;
                    return (
                      <div
                        key={territoryId}
                        className={`territory-units ${useStacked ? 'territory-units--stacked' : ''} ${useStacked && expandedStackKey === territoryId ? 'territory-units--stack-expanded' : ''}`}
                        style={{
                          left: screenPos.x,
                          top: screenPos.y + unitOffsetY + powerBadgeOffsetY,
                        }}
                      >
                        {canShowAllDrag && (
                          <DraggableAllStacksButton
                            territoryId={territoryId}
                            disabled={!isMovementPhase || !canAct}
                            onTapPrepPointerDown={(tid, ev) => {
                              bulkAllTapStartRef.current = { territoryId: tid, x: ev.clientX, y: ev.clientY };
                            }}
                          />
                        )}
                        <div className="territory-units-token-row">
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
                          const canDrag =
                            canAct && isMovementPhase && unitFaction === gameState.current_faction;
                          const instanceIdsForUnit = (territoryUnitsFull?.[territoryId] || []).filter(u => u.unit_id === unit_id).map(u => u.instance_id);
                          const showNavalMustAttackStacked =
                            gameState.phase === 'combat_move' &&
                            navalUnitIds.has(unit_id) &&
                            instanceIdsForUnit.some((id) => loadedNavalMustAttackInstanceIdSet.has(id));
                          const showForcedNavalStandoffStacked =
                            gameState.phase === 'combat_move' &&
                            navalUnitIds.has(unit_id) &&
                            instanceIdsForUnit.some(
                              (id) =>
                                forcedNavalCombatInstanceIdSet.has(id) &&
                                !loadedNavalMustAttackInstanceIdSet.has(id)
                            );

                          return (
                            <span
                              key={`${territoryId}-${unit_id}`}
                              className={`unit-token-tap-wrapper${tapSelectedUnit?.territoryId === territoryId && tapSelectedUnit?.unitId === unit_id ? ' unit-token-tap-wrapper--selected' : ''}`}
                              onPointerDownCapture={(ev) => handleUnitPointerDownCapture(territoryId, unit_id, ev)}
                              style={{ display: 'inline-block' }}
                            >
                              <DraggableUnit
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
                                showForcedNavalStandoff={showForcedNavalStandoffStacked}
                                isNaval={navalUnitIds.has(unit_id)}
                                instanceIds={instanceIdsForUnit.length > 0 ? instanceIdsForUnit : undefined}
                              />
                            </span>
                          );
                        })}
                        {useStacked && expandedStackKey !== territoryId && (
                          <div
                            className="territory-units-expand-overlay"
                            onClick={(e) => { e.stopPropagation(); setExpandedStackKey(territoryId); }}
                            onPointerDown={(e) => e.stopPropagation()}
                            role="button"
                            tabIndex={0}
                            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpandedStackKey(territoryId); } }}
                            aria-label="Expand unit stack"
                          />
                        )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          <DragOverlay
            bulkDragOverlay={bulkDragOverlay}
            activeUnit={activeUnit}
            activeMobilizationItem={activeMobilizationItem}
            activeCampDrag={activeCampDrag}
            factionColor={mobilizationTray?.factionColor}
          />

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
        </div>

        {mobilizationTray && (
          <MobilizationTray
            isOpen={true}
            purchases={mobilizationTray.purchases}
            pendingCamps={mobilizationTray.pendingCamps}
            faction={gameState.current_faction}
            factionColor={mobilizationTray.factionColor}
            canMobilizeAll={mobilizationTray.canMobilizeAll ?? false}
            selectedUnitId={mobilizationTray.selectedUnitId}
            selectedCampIndex={mobilizationTray.selectedCampIndex}
            onSelectUnit={mobilizationTray.onSelectUnit}
            onSelectCamp={mobilizationTray.onSelectCamp}
            onTapMobilizeAll={handleTapMobilizeAllFromTray}
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
    </div>
  );
}

export default GameMap;
