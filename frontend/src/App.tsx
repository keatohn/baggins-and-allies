import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type { GameState, GamePhase, FactionId, GameEvent, SelectedUnit, DeclaredBattle } from './types/game';
import Header from './components/Header';
import GameMap, { type PendingMoveConfirm } from './components/GameMap';
import Sidebar from './components/Sidebar';
import CombatSimulatorPanel from './components/CombatSimulatorPanel';
import PurchaseModal from './components/PurchaseModal';
import CombatDisplay, { type CombatRound } from './components/CombatDisplay';
import api, {
  type ApiGameState,
  type ApiEvent,
  type ApiMoveableUnit,
  type Definitions,
  type AvailableActionsResponse,
  type GameMeta,
} from './services/api';
import LobbyView from './components/LobbyView';
import { sortSeaZoneIdsByNumericSuffix, canonicalSeaZoneId } from './seaZoneSort';
import {
  fordShortcutRequiresEscortLead,
  isFordCrosser,
  resolveTerritoryGraphKey,
  usesFordEscortBudget,
} from './fordEscort';
import {
  movementSfxCategoryFromUnitDef,
  playFactionTurnCue,
  playMovementSfx,
  startMenuAmbience,
  stopMenuAmbience,
  stopTurnCueImmediate,
} from './audio/gameAudio';
import './App.css';

/** API/DB may send combat integers as strings; strict `typeof === 'number'` would show 0 in the UI. */
function coerceBattleInt(v: unknown, fallback = 0): number {
  if (typeof v === 'number' && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? Math.trunc(n) : fallback;
  }
  return fallback;
}

/** Sum attacker_hits from combat_log (damage dealt to the defender side, incl. stronghold). */
function sumAttackerHitsFromCombatLog(log: unknown[] | undefined | null): number {
  if (!Array.isArray(log)) return 0;
  let s = 0;
  for (const e of log) {
    if (!e || typeof e !== 'object') continue;
    const o = e as Record<string, unknown>;
    s += coerceBattleInt(o.attacker_hits ?? o.attackerHits);
  }
  return s;
}

/** Sum defender_hits from combat_log (damage dealt to the attacker side). */
function sumDefenderHitsFromCombatLog(log: unknown[] | undefined | null): number {
  if (!Array.isArray(log)) return 0;
  let s = 0;
  for (const e of log) {
    if (!e || typeof e !== 'object') continue;
    const o = e as Record<string, unknown>;
    s += coerceBattleInt(o.defender_hits ?? o.defenderHits);
  }
  return s;
}

/** True when the modal's battle (effectiveCombat) is the same as backend active_combat (sea-zone ids normalized). */
function battleDisplayedMatchesActiveCombat(
  effective: { territory: string; sea_zone_id?: string | null },
  ac: { territory_id?: string; sea_zone_id?: string | null } | null | undefined,
): boolean {
  if (!effective?.territory || !ac?.territory_id) return false;
  const effTerr = canonicalSeaZoneId(effective.territory);
  const acTerr = canonicalSeaZoneId(ac.territory_id);
  if (effTerr !== acTerr) return false;
  const effSea = effective.sea_zone_id ? canonicalSeaZoneId(String(effective.sea_zone_id)) : '';
  const acSea = ac.sea_zone_id ? canonicalSeaZoneId(String(ac.sea_zone_id)) : '';
  if (!effSea && !acSea) return true;
  return effSea === acSea;
}

function unitIsAerial(
  unitId: string,
  unitDefs: Record<string, { archetype?: string; tags?: string[] } | undefined>,
): boolean {
  const d = unitDefs[unitId];
  return d?.archetype === 'aerial' || (d != null && Array.isArray(d.tags) && d.tags.includes('aerial'));
}

/** Same load-into-sea semantics as navalTrayData's loadMovesToZone (phase + sea to + load / land→sea). */
function pendingMoveIsSeaLoadForTray(
  m: { phase?: string; move_type?: string | null; from_territory?: string; to_territory?: string },
  gamePhase: string,
  territoryData: Record<string, { terrain?: string } | undefined>,
): boolean {
  if (m.phase !== gamePhase) return false;
  const to = String(m.to_territory ?? '').trim();
  const toSea = territoryData[to]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(to);
  if (!toSea) return false;
  if (m.move_type === 'load') return true;
  if (m.move_type != null && m.move_type !== '') return false;
  const from = String(m.from_territory ?? '').trim();
  const fromSea = territoryData[from]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(from);
  return !fromSea;
}

function shouldSendAvoidForcedNavalCombat(
  phase: string,
  fromTerr: string,
  toTerr: string,
  unitInstanceIds: string[],
  territoryData: Record<string, { terrain?: string } | undefined>,
  territories: Record<string, { units?: { instance_id: string; unit_id: string }[] } | undefined> | undefined,
  unitDefs: Record<string, { archetype?: string; tags?: string[] } | undefined> | undefined,
  forcedNavalIds: string[] | undefined,
): boolean {
  if (phase !== 'combat_move' || !forcedNavalIds?.length || !territories) return false;
  const fromSea =
    Boolean(fromTerr) &&
    (territoryData[fromTerr]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(fromTerr));
  const toSea =
    Boolean(toTerr) &&
    (territoryData[toTerr]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(toTerr));
  if (!fromSea || !toSea || fromTerr === toTerr) return false;
  const forced = new Set(forcedNavalIds);
  const fromUnits = territories[fromTerr]?.units ?? [];
  const navalInMove = unitInstanceIds.filter((id) => {
    const u = fromUnits.find((x) => x.instance_id === id);
    if (!u || !unitDefs) return false;
    const ud = unitDefs[u.unit_id];
    if (!ud) return false;
    return ud.archetype === 'naval' || Boolean(ud.tags?.includes('naval'));
  });
  return navalInMove.length > 0 && navalInMove.every((id) => forced.has(id));
}

export interface PendingMobilization {
  unitId: string;
  unitName: string;
  unitIcon: string;
  toTerritory: string;
  maxCount: number;
  count: number;
}

export interface BulkMoveConfirmStack {
  unitId: string;
  unitName: string;
  count: number;
  instanceIds: string[];
  destForApi: string;
  chargeThrough?: string[];
}

export interface BulkMoveConfirmState {
  fromTerritory: string;
  toTerritory: string;
  stacks: BulkMoveConfirmStack[];
}

export interface BulkMobilizeConfirmState {
  toTerritory: string;
  units: { unitId: string; unitName: string; unitIcon: string; count: number }[];
}

const DEFAULT_GAME_ID = 'game_1';
const FORFEIT_NOTIFICATION_STORAGE_KEY = (gameId: string) => `forfeit_notification_dismissed_${gameId}`;

/** Poll game state this often (ms) so other players' actions appear without refresh. */
const GAME_POLL_INTERVAL_MS = 3000;

/** Delay between each AI action so the human can follow what the computer is doing (ms). */
const AI_STEP_DELAY_MS = 2500;

const MOVE_DEST_META_KEYS = new Set(['max_reach', 'by_distance', 'instance_ids', 'is_enemy', 'cost', 'max_units']);

/** Territory id -> movement cost from API `destinations` object (excludes meta keys). */
function extractDestinationCosts(
  destinations:
    | Record<string, number | unknown>
    | { by_distance?: Record<number, string[]> | Record<string, string[]> }
    | undefined
    | null,
): Record<string, number> | undefined {
  if (!destinations || typeof destinations !== 'object' || Array.isArray(destinations)) return undefined;
  const d = destinations as Record<string, unknown>;
  const out: Record<string, number> = {};
  for (const k of Object.keys(d)) {
    if (MOVE_DEST_META_KEYS.has(k)) continue;
    const val = d[k];
    if (typeof val === 'number' && Number.isFinite(val)) out[k] = val;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** Backend returns destinations as { territory_id: cost }. Return list of territory IDs. */
function normalizeMoveDestinations(
  destinations:
    | Record<string, number | unknown>
    | { by_distance?: Record<number, string[]> | Record<string, string[]> }
    | undefined
    | null,
): string[] {
  if (!destinations) return [];
  if (Array.isArray(destinations)) {
    return (destinations as unknown[]).map((x) => String(x)).filter(Boolean);
  }
  if (typeof destinations !== 'object') return [];
  const d = destinations as Record<string, unknown>;
  if ('by_distance' in d && d.by_distance != null && typeof d.by_distance === 'object' && !Array.isArray(d.by_distance)) {
    const bd = d.by_distance as Record<string, unknown>;
    const keys = Object.keys(bd);
    if (keys.length > 0) {
      return keys
        .sort((a, b) => Number(a) - Number(b))
        .flatMap((k) => {
          const v = bd[k];
          return Array.isArray(v) ? (v as unknown[]).map((x) => String(x)) : [];
        });
    }
  }
  const out: string[] = [];
  for (const k of Object.keys(d)) {
    if (MOVE_DEST_META_KEYS.has(k)) continue;
    const val = d[k];
    if (typeof val === 'number' && Number.isFinite(val)) out.push(k);
  }
  return out;
}

function unitDefFordFields(unit: unknown): {
  archetype?: string;
  tags?: string[];
  specials?: string[];
} | undefined {
  if (!unit || typeof unit !== 'object') return undefined;
  const o = unit as Record<string, unknown>;
  return {
    archetype: typeof o.archetype === 'string' ? o.archetype : undefined,
    tags: Array.isArray(o.tags) ? (o.tags as string[]) : undefined,
    specials: Array.isArray(o.specials) ? (o.specials as string[]) : undefined,
  };
}

/** Build per-unit-type stacks for bulk "All" move (same rules as submit). Destinations must use normalizeMoveDestinations — API never sends an array. */
function buildBulkMoveStacks(
  backendState: ApiGameState,
  moveables: ApiMoveableUnit[],
  currentPhase: string,
  fromTerritory: string,
  toTerritory: string,
  definitions: Definitions | null,
): BulkMoveConfirmStack[] {
  const territories = backendState.territories ?? {};
  const resolveFromKey = (tid: string): string => {
    if (territories[tid]) return tid;
    const c = canonicalSeaZoneId(tid);
    if (territories[c]) return c;
    return tid;
  };
  const fromKey = resolveFromKey(fromTerritory);
  const fromTerr = territories[fromKey];
  if (!fromTerr?.units?.length) return [];

  const destMatchesList = (dests: string[], to: string): string | null => {
    if (!dests.length) return null;
    const t = to.trim();
    const tc = canonicalSeaZoneId(t);
    for (const d of dests) {
      if (d === t || canonicalSeaZoneId(d) === tc) return d;
    }
    return null;
  };

  const sameFromTerritory = (mTerr: string) =>
    mTerr === fromKey || canonicalSeaZoneId(mTerr) === canonicalSeaZoneId(fromKey);

  const sourceUnits = fromTerr.units as Array<{ instance_id: string; unit_id: string }>;

  const committed = new Set(
    (backendState.pending_moves ?? [])
      .filter(
        (m: { from_territory?: string; phase?: string }) =>
          sameFromTerritory(String(m.from_territory ?? '')) && m.phase === currentPhase,
      )
      .flatMap((m: { unit_instance_ids?: string[] }) => m.unit_instance_ids ?? []),
  );

  const byUnitType = new Map<string, string[]>();
  for (const u of sourceUnits) {
    if (committed.has(u.instance_id)) continue;
    if (!byUnitType.has(u.unit_id)) byUnitType.set(u.unit_id, []);
    byUnitType.get(u.unit_id)!.push(u.instance_id);
  }

  const destList = (m: ApiMoveableUnit) => normalizeMoveDestinations(m.destinations);
  const stacks: BulkMoveConfirmStack[] = [];

  for (const [unitId, candidateIds] of byUnitType.entries()) {
    const movableIds = new Set(
      moveables
        .filter((m) => {
          const destHit = destMatchesList(destList(m), toTerritory);
          return sameFromTerritory(m.territory) && m.unit.unit_id === unitId && destHit != null;
        })
        .map((m) => m.unit.instance_id),
    );
    const ids = candidateIds.filter((id) => movableIds.has(id));
    if (ids.length === 0) continue;

    const destForApi =
      moveables
        .filter((m) => sameFromTerritory(m.territory) && m.unit.unit_id === unitId && ids.includes(m.unit.instance_id))
        .map((m) => destMatchesList(destList(m), toTerritory))
        .find((d) => d != null) ?? canonicalSeaZoneId(toTerritory);

    const firstMatch = moveables.find(
      (m) =>
        sameFromTerritory(m.territory) &&
        m.unit.unit_id === unitId &&
        m.unit.instance_id === ids[0] &&
        destMatchesList(destList(m), toTerritory) != null,
    );
    const cr = firstMatch?.charge_routes;
    const chargePaths =
      cr?.[destForApi] ?? cr?.[toTerritory] ?? cr?.[canonicalSeaZoneId(toTerritory)];
    const chargeThrough =
      Array.isArray(chargePaths) && chargePaths.length > 0 ? chargePaths[0] : undefined;

    const unitName = definitions?.units?.[unitId]?.display_name ?? unitId;

    stacks.push({
      unitId,
      unitName,
      count: ids.length,
      instanceIds: ids,
      destForApi,
      chargeThrough,
    });
  }

  /* API lists ford destinations only for ford crossers; escorted transportables get no ford hexes.
     When a crosser uses a ford escort move (min ford ≥ 1 or direct river-ford pair), attach other
     transportable stacks from this hex for the same destination so bulk confirm can submit combined moves. */
  if (definitions?.territories && stacks.length > 0) {
    const fordGraph: Record<string, { adjacent?: string[]; ford_adjacent?: string[]; terrain?: string }> = {};
    for (const [tid, t] of Object.entries(definitions.territories)) {
      const ter = t as { adjacent?: string[]; ford_adjacent?: string[]; terrain_type?: string };
      fordGraph[tid] = {
        adjacent: Array.isArray(ter.adjacent) ? ter.adjacent : [],
        ford_adjacent: Array.isArray(ter.ford_adjacent) ? ter.ford_adjacent : [],
        terrain: ter.terrain_type,
      };
    }
    const crosserStack = stacks.find((s) => isFordCrosser(unitDefFordFields(definitions.units?.[s.unitId])));
    if (crosserStack) {
      const fromGk = resolveTerritoryGraphKey(fromKey, fordGraph);
      const toGk = resolveTerritoryGraphKey(crosserStack.destForApi.trim(), fordGraph);
      if (fordShortcutRequiresEscortLead(fromGk, toGk, fordGraph)) {
        for (const [unitId, candidateIds] of byUnitType.entries()) {
          const udf = unitDefFordFields(definitions.units?.[unitId]);
          if (!usesFordEscortBudget(udf) || isFordCrosser(udf)) continue;
          if (stacks.some((st) => st.unitId === unitId)) continue;
          const unitName = (definitions.units?.[unitId] as { display_name?: string } | undefined)?.display_name ?? unitId;
          stacks.push({
            unitId,
            unitName,
            count: candidateIds.length,
            instanceIds: [...candidateIds],
            destForApi: crosserStack.destForApi,
            chargeThrough: undefined,
          });
        }
      }
    }
  }

  return stacks;
}

/** API pending move shape for load-allocation math (snake_case from backend). */
type ApiPendingMoveForLoad = {
  phase?: string;
  from_territory?: string;
  to_territory?: string;
  move_type?: string | null;
  load_onto_boat_instance_id?: string | null;
  unit_instance_ids?: string[];
};

/**
 * Split passengers across boats (sorted by instance_id, same as backend apply). Subtracts onboard,
 * explicit pending loads per boat, and simulates auto-assigned pending loads before placing new passengers.
 */
function computeInitialLoadAllocation(
  boatOptionRows: string[][],
  passengerInstanceIds: string[],
  seaUnits: { instance_id: string; unit_id: string; loaded_onto?: string | null }[],
  unitsById: Record<string, { transport_capacity?: number } | undefined>,
  pendingMoves: ApiPendingMoveForLoad[],
  gamePhase: string,
  toSeaTerritoryId: string,
  territoryData: Record<string, { terrain?: string } | undefined>,
): Record<string, string[]> | null {
  if (!boatOptionRows.length || !passengerInstanceIds.length || !seaUnits.length) return null;
  const toSea = toSeaTerritoryId.trim();
  const pendingLoadsIntoThisSea = (m: ApiPendingMoveForLoad): boolean => {
    if (m.phase !== gamePhase) return false;
    const t = String(m.to_territory ?? '').trim();
    if (canonicalSeaZoneId(t) !== canonicalSeaZoneId(toSea) && t !== toSea) return false;
    if (m.move_type === 'load') return true;
    if (m.move_type != null && m.move_type !== '') return false;
    const f = String(m.from_territory ?? '').trim();
    const fromSea = territoryData[f]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(f);
    const toS = territoryData[t]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(t);
    return !fromSea && toS;
  };
  let unassignedPending = 0;
  const explicitByBoat: Record<string, number> = {};
  for (const m of pendingMoves) {
    if (!pendingLoadsIntoThisSea(m)) continue;
    const n = m.unit_instance_ids?.length ?? 0;
    const bid = (m.load_onto_boat_instance_id ?? '').trim();
    if (bid) explicitByBoat[bid] = (explicitByBoat[bid] ?? 0) + n;
    else unassignedPending += n;
  }
  const boatIds = [...new Set(boatOptionRows.map((row) => row[0]).filter(Boolean) as string[])].sort((a, b) =>
    a.localeCompare(b),
  );
  const rem = boatIds.map((bid) => {
    const boat = seaUnits.find((u) => u.instance_id === bid);
    const cap = boat
      ? Number((unitsById[boat.unit_id] as { transport_capacity?: number } | undefined)?.transport_capacity ?? 0)
      : 0;
    const used = seaUnits.filter((u) => u.loaded_onto === bid).length;
    const explicit = explicitByBoat[bid] ?? 0;
    return { bid, left: Math.max(0, cap - used - explicit) };
  });
  let u = unassignedPending;
  for (const r of rem) {
    const take = Math.min(r.left, u);
    r.left -= take;
    u -= take;
  }
  const initial: Record<string, string[]> = Object.fromEntries(boatIds.map((b) => [b, []]));
  let bi = 0;
  for (const pid of passengerInstanceIds) {
    while (bi < rem.length && rem[bi].left <= 0) bi++;
    if (bi >= rem.length) return null;
    initial[rem[bi].bid].push(pid);
    rem[bi].left -= 1;
  }
  return initial;
}

// Helper type for combat units (remainingMovement used for casualty order). Must match CombatDisplay CombatUnit.
type CombatUnit = {
  id: string;
  unitType: string;
  name: string;
  icon: string;
  attack: number;
  defense: number;
  effectiveAttack?: number;
  effectiveDefense?: number;
  /** Archetype/tag archer (row visibility); not the archer special badge. */
  isArcher?: boolean;
  health: number;
  remainingHealth: number;
  remainingMovement?: number;
  factionColor?: string;
  factionId?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasAntiCavalry?: boolean;
  hasSeaRaider?: boolean;
  /** Defender: archer special badge (prefire round only); from backend `archer`. */
  hasArcher?: boolean;
  hasStealth?: boolean;
  hasBombikazi?: boolean;
  hasFearless?: boolean;
  hasHope?: boolean;
  /** Attacker: unit has ram special (from backend / definitions). */
  hasRam?: boolean;
  siegeworkArchetype?: boolean;
  /** Naval: count of units embarked on this boat (combat modal / map parity). */
  passengerCount?: number;
};

// Backend round payload: units at start of round (single source of truth for combat display)
type BackendCombatUnit = {
  instance_id: string;
  unit_id: string;
  display_name: string;
  attack: number;
  defense: number;
  effective_attack?: number | null;
  effective_defense?: number | null;
  health: number;
  remaining_health: number;
  remaining_movement?: number;
  is_archer?: boolean;
  faction: string;
  terror?: boolean;
  terrain_mountain?: boolean;
  terrain_forest?: boolean;
  captain_bonus?: boolean;
  anti_cavalry?: boolean;
  sea_raider?: boolean;
  /** Defender archer special (AR badge); only true during archer prefire snapshot. */
  archer?: boolean;
  stealth?: boolean;
  bombikazi?: boolean;
  fearless?: boolean;
  hope?: boolean;
  ram?: boolean;
  siegework_archetype?: boolean;
  passenger_count?: number;
};

/** Per-instance flags from API combat_specials (union of attacker- and defender-side keys). */
type CombatSpecialsInstance = {
  terror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  captain?: boolean;
  antiCavalry?: boolean;
  seaRaider?: boolean;
  archer?: boolean;
  stealth?: boolean;
  bombikazi?: boolean;
  fearless?: boolean;
  hope?: boolean;
  ram?: boolean;
};

/**
 * While phase is combat_move or combat, the server queues incoming owners in pending_captures (true owner
 * updates at end of combat, with liberation). After that, pending_captures should be empty — but if a stale
 * copy lingers in client/API cache, overlaying it would show the capturer instead of liberated owner until
 * a later refetch. Only merge pending_captures in phases where the engine still uses them.
 */
function pendingCapturesOverlayForPhase(
  phase: string | undefined | null,
  raw: Record<string, string> | undefined | null,
): Record<string, string> {
  if (phase !== 'combat_move' && phase !== 'combat') {
    return {};
  }
  if (!raw || typeof raw !== 'object') {
    return {};
  }
  return raw;
}

/**
 * Pending capture lists the attacking faction; at end of combat the server restores original_owner when
 * that faction is allied with the original owner. Apply the same rule for UI so allied liberations never
 * flash the capturer's color.
 */
function displayOwnerForPendingCapture(
  capturer: string,
  apiTerritory: { owner?: string | null; original_owner?: string | null },
  factionAlliances: Record<string, { alliance?: string } | undefined>,
): string {
  const c = capturer.trim();
  if (!c) return c;
  const raw = apiTerritory.original_owner;
  const original = typeof raw === 'string' && raw.trim() !== '' ? raw.trim() : '';
  if (!original || original === c) return c;
  const ca = factionAlliances[c]?.alliance;
  const oa = factionAlliances[original]?.alliance;
  if (ca != null && oa != null && ca === oa) return original;
  return c;
}

interface AppProps {
  /** When provided (e.g. from route /game/:gameId), use this game. */
  gameId?: string;
  /** When provided (e.g. from Create Game navigation), use as initial backend state so turn_order etc. show immediately. */
  initialState?: ApiGameState | null;
}

function App({ gameId: gameIdProp, initialState: initialStateProp }: AppProps) {
  const GAME_ID = gameIdProp ?? DEFAULT_GAME_ID;
  // Backend state (use initialState from navigation when we just created this game)
  const [definitions, setDefinitions] = useState<Definitions | null>(null);
  const [backendState, setBackendState] = useState<ApiGameState | null>(initialStateProp ?? null);
  const [availableActions, setAvailableActions] = useState<AvailableActionsResponse | null>(null);
  const [canAct, setCanAct] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // UI state
  const [selectedTerritory, setSelectedTerritory] = useState<string | null>(null);
  const [selectedUnit, setSelectedUnit] = useState<SelectedUnit | null>(null);
  const [eventLog, setEventLog] = useState<GameEvent[]>([]);
  const [isPurchaseModalOpen, setIsPurchaseModalOpen] = useState(false);
  const [pendingEndPhaseConfirm, setPendingEndPhaseConfirm] = useState<string | null>(null);
  const [pendingMoveConfirmState, setPendingMoveConfirmState] = useState<PendingMoveConfirm | null>(null);
  /** Set from GameMap on every accepted drop so Confirm always has the destination. */
  const lastDropDestinationRef = useRef<string>('');
  /** Latest load allocation from tray drags; confirm uses this so it's never stale (React state can lag behind clicks). */
  const loadAllocationRef = useRef<Record<string, string[]> | null>(null);
  const aiStepInProgressRef = useRef(false);
  const aiStepTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const setDropDestination = useCallback((territoryId: string) => {
    lastDropDestinationRef.current = territoryId?.trim() ?? '';
  }, []);
  const setPendingMoveConfirm = useCallback((action: PendingMoveConfirm | null | ((prev: PendingMoveConfirm | null) => PendingMoveConfirm | null)) => {
    setPendingMoveConfirmState((prev) => {
      const next = typeof action === 'function' ? action(prev) : action;
      if (next?.toTerritory && typeof next.toTerritory === 'string' && next.toTerritory.trim()) {
        lastDropDestinationRef.current = next.toTerritory.trim();
      }
      if (!next) {
        lastDropDestinationRef.current = '';
        loadAllocationRef.current = null;
      }
      return next;
    });
  }, []);
  const pendingMoveConfirm = pendingMoveConfirmState;
  const [bulkMoveConfirm, setBulkMoveConfirm] = useState<BulkMoveConfirmState | null>(null);
  const [bulkMobilizeConfirm, setBulkMobilizeConfirm] = useState<BulkMobilizeConfirmState | null>(null);
  const [activeCombat, setActiveCombat] = useState<DeclaredBattle | null>(null);
  /** Last combat_log while a battle is open — updated from live active_combat, and final round appended here when combat ends (API clears active_combat before persist effect runs). */
  const persistedCombatLogRef = useRef<{ key: string; log: unknown[] } | null>(null);
  /** When !canAct: which battle the spectator has chosen to view (only the active battle is openable). */
  const [spectatingBattle, setSpectatingBattle] = useState<DeclaredBattle | null>(null);
  const [pendingMobilization, setPendingMobilization] = useState<PendingMobilization | null>(null);
  const [selectedMobilizationUnit, setSelectedMobilizationUnit] = useState<string | null>(null);
  /** Index into pending_camps when placing a camp during mobilization. */
  const [selectedCampIndex, setSelectedCampIndex] = useState<number | null>(null);
  /** When move (sea->land) returns need_offload_sea_choice: user must pick which sea zone to sail to. */
  const [pendingOffloadSeaChoice, setPendingOffloadSeaChoice] = useState<{
    from: string;
    to: string;
    unitInstanceIds: string[];
    validSeaZones: string[];
  } | null>(null);
  /** Pending camp placement (drag or click on territory); confirm/cancel like unit mobilization. */
  const [pendingCampPlacement, setPendingCampPlacement] = useState<{ campIndex: number; territoryId: string } | null>(null);
  const [pendingRetreat, setPendingRetreat] = useState<DeclaredBattle | null>(null);
  const [highlightedTerritories, setHighlightedTerritories] = useState<string[]>([]);
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const w = localStorage.getItem('sidebarWidth');
    return w != null ? Math.min(600, Math.max(260, Number(w))) : 360;
  });
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth : 1200,
  );
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    localStorage.getItem('sidebarCollapsed') === '1'
  );
  const [combatSimModalOpen, setCombatSimModalOpen] = useState(false);
  /** Sea zone selected to show naval tray (boats + passengers) during combat_move or non_combat_move. */
  const [selectedSeaZoneForNavalTray, setSelectedSeaZoneForNavalTray] = useState<string | null>(null);
  /** Purchase phase: cart of units to buy; applied on End phase, not on Confirm */
  const [purchaseCart, setPurchaseCart] = useState<Record<string, number>>({});
  /** Purchase phase: number of camps to buy; applied on End phase (after units). */
  const [purchaseCampsCount, setPurchaseCampsCount] = useState(0);
  /** Purchase phase: stronghold repairs to apply on End phase. List of { territory_id, hp_to_add }. */
  const [purchaseRepairs, setPurchaseRepairs] = useState<{ territory_id: string; hp_to_add: number }[]>([]);
  const resizeStartRef = useRef<{ x: number; width: number } | null>(null);
  /** Turn order from create-game navigation so init() doesn't overwrite it with a stale/empty fetch. */
  const initialTurnOrderRef = useRef<string[] | null>(null);
  if (initialStateProp?.turn_order?.length) {
    initialTurnOrderRef.current = initialStateProp.turn_order;
  } else if (initialStateProp == null) {
    initialTurnOrderRef.current = null;
  }
  /** Game meta for lobby (when status is lobby) */
  const [gameMeta, setGameMeta] = useState<GameMeta | null>(null);
  /** Setup id for current game (from backend). Pass to combat sim so it uses same unit definitions. */
  const [gameSetupId, setGameSetupId] = useState<string | null>(null);

  useEffect(() => {
    setSelectedSeaZoneForNavalTray(null);
  }, [backendState?.current_faction]);

  const lastTurnAudioKeyRef = useRef('');
  /** When `current_faction` changes (skip turn, end turn, auto-skipped factions), force a new cue even if key dedupe would match. */
  const prevTurnAudioFactionRef = useRef<string | null>(null);
  useEffect(() => {
    lastTurnAudioKeyRef.current = '';
    prevTurnAudioFactionRef.current = null;
  }, [GAME_ID]);

  useEffect(() => {
    return () => stopTurnCueImmediate();
  }, []);

  /** Faction turn loop: follows game `current_faction` for all players; stops in lobby / game over. */
  useEffect(() => {
    const stopTurnMusic = () => {
      stopTurnCueImmediate();
      lastTurnAudioKeyRef.current = '';
      prevTurnAudioFactionRef.current = null;
    };

    if (!backendState?.current_faction || backendState.winner) {
      stopTurnMusic();
      return;
    }
    if (!gameMeta || gameMeta.status === 'lobby') {
      stopTurnMusic();
      return;
    }

    const cf = backendState.current_faction;
    if (prevTurnAudioFactionRef.current !== null && prevTurnAudioFactionRef.current !== cf) {
      lastTurnAudioKeyRef.current = '';
    }
    prevTurnAudioFactionRef.current = cf;

    const musicRaw = definitions?.factions?.[cf]?.music;
    const musicKey = Array.isArray(musicRaw) ? musicRaw.join('|') : (musicRaw ?? '');
    const key = `${GAME_ID}:${backendState.turn_number ?? 0}:${cf}:${musicKey}`;
    if (lastTurnAudioKeyRef.current === key) return;
    lastTurnAudioKeyRef.current = key;
    playFactionTurnCue(cf, musicRaw ?? null);
  }, [
    GAME_ID,
    definitions?.factions,
    gameMeta,
    gameMeta?.status,
    backendState?.current_faction,
    backendState?.turn_number,
    backendState?.winner,
  ]);

  // When switching games, clear backend state (or use passed initial state for the new game)
  useEffect(() => {
    setBackendState(initialStateProp ?? null);
  }, [GAME_ID, initialStateProp]);

  useEffect(() => {
    localStorage.setItem('sidebarWidth', String(sidebarWidth));
  }, [sidebarWidth]);

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  /** Sidebar inline width can exceed narrow viewports; cap so map stays usable (esp. mobile). */
  const sidebarWidthCapped = useMemo(() => {
    if (sidebarCollapsed) return sidebarWidth;
    const cap =
      viewportWidth < 400
        ? Math.min(210, Math.floor(viewportWidth * 0.54))
        : viewportWidth < 480
          ? Math.min(230, Math.floor(viewportWidth * 0.56))
          : viewportWidth < 768
            ? Math.min(300, Math.floor(viewportWidth * 0.38))
            : viewportWidth < 1100
              ? Math.min(520, Math.floor(viewportWidth * 0.42))
              : 600;
    return Math.min(sidebarWidth, Math.max(196, cap));
  }, [sidebarCollapsed, sidebarWidth, viewportWidth]);
  useEffect(() => {
    localStorage.setItem('sidebarCollapsed', sidebarCollapsed ? '1' : '0');
  }, [sidebarCollapsed]);

  /** Move confirmation lives in the sidebar — expand when any move needs confirming (esp. mobile). */
  useEffect(() => {
    if (pendingMoveConfirm || bulkMoveConfirm || pendingOffloadSeaChoice || bulkMobilizeConfirm) {
      setSidebarCollapsed(false);
    }
  }, [pendingMoveConfirm, bulkMoveConfirm, pendingOffloadSeaChoice, bulkMobilizeConfirm]);
  useEffect(() => {
    if (backendState && backendState.phase !== 'purchase') {
      setPurchaseCart({});
      setPurchaseCampsCount(0);
      setPurchaseRepairs([]);
    }
  }, [backendState?.phase]);

  useEffect(() => {
    if (!GAME_ID || GAME_ID === DEFAULT_GAME_ID) return;
    api.getGameMeta(GAME_ID).then(setGameMeta).catch(() => setGameMeta(null));
  }, [GAME_ID]);


  // Track if actions have been performed this phase (for confirmation dialogs)
  const [hasCombatMovedThisPhase, setHasCombatMovedThisPhase] = useState(false);
  const [hasNonCombatMovedThisPhase, setHasNonCombatMovedThisPhase] = useState(false);
  const [battlesCompletedThisPhase, setBattlesCompletedThisPhase] = useState(0);
  /** Snapshot of how many combat moves were declared when we left combat_move (so we can tell "no battles" vs "all uncontested" in combat). */
  const [combatMovesDeclaredThisPhase, setCombatMovesDeclaredThisPhase] = useState(0);
  /** Forfeit notification: dismissed this session; persisted in localStorage for once-per-user-per-game. */
  const [forfeitToastDismissed, setForfeitToastDismissed] = useState(false);
  useEffect(() => {
    setForfeitToastDismissed(false);
  }, [GAME_ID]);

  // Derived data from backend definitions (archetype/tags for aerial return-path rule in combat move)
  const unitDefs = useMemo(() => {
    if (!definitions) return {};
    const defs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[]; specials?: string[]; home_territory_ids?: string[]; cost?: number; transport_capacity?: number }> = {};
    for (const [id, unit] of Object.entries(definitions.units)) {
      const u = unit as { display_name: string; icon?: string; faction?: string; archetype?: string; tags?: string[]; specials?: string[]; home_territory_ids?: string[]; cost?: number | { power?: number }; transport_capacity?: number };
      const cost = typeof u.cost === 'object' && u.cost?.power != null ? u.cost.power : (typeof u.cost === 'number' ? u.cost : 0);
      const homeIds = Array.isArray(u.home_territory_ids) ? u.home_territory_ids : [];
      defs[id] = {
        name: u.display_name,
        icon: `/assets/units/${u.icon || `${id}.png`}`,
        faction: u.faction,
        archetype: u.archetype,
        tags: u.tags,
        specials: u.specials,
        home_territory_ids: homeIds.length > 0 ? homeIds : undefined,
        cost,
        transport_capacity: u.transport_capacity,
      };
    }
    return defs;
  }, [definitions]);

  const factionData: Record<string, { name: string; icon: string; color: string; alliance: string; capital: string }> = useMemo(() => {
    if (!definitions) return {};

    const data: Record<string, { name: string; icon: string; color: string; alliance: string; capital: string }> = {};
    for (const [id, faction] of Object.entries(definitions.factions)) {
      data[id] = {
        name: faction.display_name,
        icon: `/assets/factions/${faction.icon || `${id}.png`}`,
        color: faction.color,
        alliance: faction.alliance,
        capital: faction.capital ?? '',
      };
    }
    return data;
  }, [definitions]);

  const territoryDefs = useMemo(() => {
    if (!definitions) return {};
    const defs: Record<string, {
      name: string;
      terrain: string;
      stronghold: boolean;
      stronghold_base_health: number;
      produces: number;
      adjacent: string[];
      aerial_adjacent?: string[];
      ford_adjacent?: string[];
      ownable: boolean;
    }> = {};
    for (const [id, territory] of Object.entries(definitions.territories)) {
      defs[id] = {
        name: territory.display_name,
        terrain: territory.terrain_type,
        stronghold: territory.is_stronghold,
        stronghold_base_health: Math.max(0, (territory as { stronghold_base_health?: number }).stronghold_base_health ?? 0),
        produces: (territory.produces?.power as number) || 0,
        adjacent: territory.adjacent,
        aerial_adjacent: (territory as { aerial_adjacent?: string[] }).aerial_adjacent ?? [],
        ford_adjacent: (territory as { ford_adjacent?: string[] }).ford_adjacent ?? [],
        ownable: (territory as { ownable?: boolean }).ownable !== false,
      };
    }
    return defs;
  }, [definitions]);

  // Build territory data from backend state (includes hasCamp from standing camps, isCapital from faction definitions)
  const currentTerritoryData = useMemo(() => {
    if (!backendState || !territoryDefs) return {};
    const camps = definitions?.camps;
    const campsObj = camps && typeof camps === 'object' && !Array.isArray(camps) ? camps : {};
    const ports = definitions?.ports;
    const portsObj = ports && typeof ports === 'object' && !Array.isArray(ports) ? ports : {};
    const campsStanding = Array.isArray(backendState.camps_standing) ? backendState.camps_standing : [];
    const dynamicCamps = backendState.dynamic_camps && typeof backendState.dynamic_camps === 'object' ? backendState.dynamic_camps : {};
    const factions = definitions?.factions ?? {};
    const territoryHasCamp = (tid: string) =>
      Object.values(dynamicCamps).includes(tid) ||
      campsStanding.some(
        (campId) => campsObj[campId] && (campsObj[campId] as { territory_id?: string }).territory_id === tid
      );
    const territoryHasPort = (tid: string) =>
      Object.values(portsObj).some(
        (p) => (p as { territory_id?: string }).territory_id === tid
      );
    const territoryIsCapital = (tid: string) =>
      Object.values(factions).some(
        (f) => (f as { capital?: string }).capital === tid
      );

    const result: Record<string, {
      name: string;
      owner?: FactionId;
      terrain: string;
      stronghold: boolean;
      stronghold_base_health: number;
      stronghold_current_health: number;
      produces: number;
      adjacent: string[];
      aerial_adjacent?: string[];
      ford_adjacent?: string[];
      hasCamp: boolean;
      hasPort: boolean;
      isCapital: boolean;
      ownable?: boolean;
    }> = {};

    const pendingCaptures = pendingCapturesOverlayForPhase(
      backendState.phase,
      backendState.pending_captures as Record<string, string> | undefined,
    );

    for (const [id, territory] of Object.entries(backendState.territories)) {
      const def = territoryDefs[id];
      const backendT = territory as { stronghold_current_health?: number | null };
      const baseHp = def?.stronghold_base_health ?? 0;
      const currentHp = backendT.stronghold_current_health != null ? backendT.stronghold_current_health : baseHp;
      const ownerFromPending = pendingCaptures[id];
      const resolvedOwner = (
        ownerFromPending != null && ownerFromPending !== ''
          ? displayOwnerForPendingCapture(
            ownerFromPending,
            territory as { owner?: string | null; original_owner?: string | null },
            factionData,
          )
          : territory.owner
      ) as FactionId | undefined;
      result[id] = def
        ? {
          ...def,
          owner: resolvedOwner,
          hasCamp: territoryHasCamp(id),
          hasPort: territoryHasPort(id),
          isCapital: territoryIsCapital(id),
          stronghold_current_health: currentHp,
        }
        : {
          name: id.replace(/_/g, ' '),
          owner: resolvedOwner,
          terrain: 'land',
          stronghold: false,
          stronghold_base_health: 0,
          stronghold_current_health: 0,
          produces: 0,
          adjacent: [],
          aerial_adjacent: [],
          hasCamp: territoryHasCamp(id),
          hasPort: territoryHasPort(id),
          isCapital: territoryIsCapital(id),
          ownable: true,
        };
    }
    return result;
  }, [backendState, territoryDefs, definitions, factionData]);

  // Build unit data from backend state
  const currentTerritoryUnits = useMemo(() => {
    if (!backendState) return {};
    const result: Record<string, { unit_id: string; count: number; instances: string[] }[]> = {};

    for (const [territoryId, territory] of Object.entries(backendState.territories)) {
      const unitCounts: Record<string, { count: number; instances: string[] }> = {};
      for (const unit of territory.units) {
        if (!unitCounts[unit.unit_id]) {
          unitCounts[unit.unit_id] = { count: 0, instances: [] };
        }
        unitCounts[unit.unit_id].count += 1;
        unitCounts[unit.unit_id].instances.push(unit.instance_id);
      }

      result[territoryId] = Object.entries(unitCounts).map(([unit_id, data]) => ({
        unit_id,
        count: data.count,
        instances: data.instances,
      }));
    }
    return result;
  }, [backendState]);

  // Full unit list per territory (for sea zones: boats + loaded_onto so we can show passenger count per boat)
  const territoryUnitsFull = useMemo(() => {
    if (!backendState) return {};
    const r: Record<string, { instance_id: string; unit_id: string; loaded_onto?: string | null }[]> = {};
    for (const [tid, t] of Object.entries(backendState.territories)) {
      r[tid] = (t.units || []).map(
        (u: {
          instance_id: string;
          unit_id: string;
          loaded_onto?: string | null;
          remaining_movement?: number;
        }) => ({
          instance_id: u.instance_id,
          unit_id: u.unit_id,
          loaded_onto: u.loaded_onto ?? undefined,
          remaining_movement: typeof u.remaining_movement === 'number' ? u.remaining_movement : 0,
        }),
      );
    }
    return r;
  }, [backendState]);

  // Per-stack unit rows with remaining movement (non-combat move phase only, for selected territory).
  // Only units the current player may move (same faction as current turn — not allies).
  const territoryUnitStacksWithMovement = useMemo(() => {
    if (!backendState || !selectedTerritory || backendState.phase !== 'non_combat_move') return null;
    const territory = backendState.territories[selectedTerritory];
    if (!territory?.units?.length) return null;
    const currentFaction = backendState.current_faction;
    if (!currentFaction) return null;
    const keyed: Record<string, { unit_id: string; remaining_movement: number; count: number }> = {};
    for (const u of territory.units) {
      const parts = u.unit_id.split('_');
      const factionFromId = parts.find((p) => factionData[p]);
      const defFaction = unitDefs[u.unit_id]?.faction;
      const uf = factionFromId ?? defFaction ?? parts[0];
      if (uf !== currentFaction) continue;
      const key = `${u.unit_id}:${u.remaining_movement}`;
      if (!keyed[key]) keyed[key] = { unit_id: u.unit_id, remaining_movement: u.remaining_movement, count: 0 };
      keyed[key].count += 1;
    }
    const rows = Object.values(keyed);
    if (rows.length === 0) return null;
    return rows.sort((a, b) => b.remaining_movement - a.remaining_movement);
  }, [backendState, selectedTerritory, unitDefs, factionData]);

  // Unit stats for movement values
  const unitStats = useMemo(() => {
    if (!definitions) return {};
    const stats: Record<string, { movement: number; attack: number; defense: number; health: number; cost: number }> = {};
    for (const [id, unit] of Object.entries(definitions.units)) {
      stats[id] = {
        movement: unit.movement,
        attack: unit.attack,
        defense: unit.defense,
        health: unit.health,
        cost: unit.cost?.power || 0, // Cost is Record<string, number>, get power cost
      };
    }
    return stats;
  }, [definitions]);

  /** Raw special ids for a unit (for indexing by special). */
  const getUnitSpecialIds = useCallback((u: { tags?: string[]; archetype?: string; specials?: string[] }) => {
    const out = new Set<string>();
    const exclude = new Set(['land', 'mounted']);
    (u.tags || []).filter(t => !exclude.has(t)).forEach(t => out.add(t));
    if (u.archetype === 'archer') out.add('archer');
    if (u.archetype === 'cavalry') out.add('charging');
    if (u.archetype === 'aerial') out.add('aerial');
    (u.specials || []).forEach(s => out.add(s));
    return [...out];
  }, []);

  /** Unit Stats modal SP column: only ids defined in setup specials.json; labels use catalog `name`. Home → "Home (…)" when provided. */
  const getUnitSpecials = useCallback((
    u: { specials?: string[] },
    opts?: { homeTerritoryDisplayNames?: string[] }
  ) => {
    const catalog = definitions?.specials as Record<string, { name?: string }> | undefined;
    const inCatalog = (id: string) => {
      if (!id || id === 'order') return false;
      const entry = catalog?.[id];
      return typeof entry === 'object' && entry !== null && 'name' in entry;
    };
    const formatFallback = (s: string) => {
      const normalized = s.replace(/_/g, ' ').replace(/-/g, ' ').toLowerCase().trim();
      if (normalized === 'anti cavalry') return 'Anti-Cavalry';
      return s.replace(/_/g, ' ').replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    };
    const ids = [...new Set((u.specials || []).filter(inCatalog))];
    const names = ids
      .map((sid) => {
        const n = typeof catalog?.[sid]?.name === 'string' ? catalog[sid].name!.trim() : '';
        return n || formatFallback(sid);
      })
      .sort((a, b) => a.localeCompare(b));
    if (ids.includes('home') && opts?.homeTerritoryDisplayNames?.length) {
      return names.map(n => n === 'Home' ? `Home (${opts.homeTerritoryDisplayNames!.join(', ')})` : n);
    }
    return names;
  }, [definitions?.specials]);

  /** Home territory display names for a unit (from definitions). */
  const getHomeTerritoryDisplayNames = useCallback((u: { home_territory_ids?: string[] | null }, territories: Record<string, { display_name: string }>) => {
    const ids = u.home_territory_ids ?? [];
    return ids.map(tid => territories[tid]?.display_name ?? tid).filter(Boolean);
  }, []);

  // All units grouped by faction (for Unit Stats modal): cost, dice, attack, specials count, then name.
  const unitsByFaction = useMemo(() => {
    if (!definitions?.units || !unitDefs) return {};
    const territories = definitions.territories ?? {};
    const byFaction: Record<string, Array<{ id: string; name: string; icon: string; cost: number; attack: number; defense: number; dice: number; movement: number; health: number; specials: string[] }>> = {};
    for (const [id, u] of Object.entries(definitions.units)) {
      const faction = u.faction;
      if (!byFaction[faction]) byFaction[faction] = [];
      const cost = typeof u.cost === 'object' && u.cost?.power != null ? u.cost.power : 0;
      const homeNames = getHomeTerritoryDisplayNames(u, territories);
      byFaction[faction].push({
        id,
        name: u.display_name,
        icon: unitDefs[id]?.icon ?? `/assets/units/${id}.png`,
        cost,
        attack: u.attack,
        defense: u.defense,
        dice: u.dice ?? 1,
        movement: u.movement,
        health: u.health,
        specials: getUnitSpecials(u, homeNames.length ? { homeTerritoryDisplayNames: homeNames } : undefined),
      });
    }
    for (const fid of Object.keys(byFaction)) {
      byFaction[fid].sort((a, b) => {
        if (a.cost !== b.cost) return a.cost - b.cost;
        if (a.dice !== b.dice) return a.dice - b.dice;
        if (a.attack !== b.attack) return a.attack - b.attack;
        const lenA = a.specials.length;
        const lenB = b.specials.length;
        if (lenA !== lenB) return lenA - lenB;
        const byName = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
        if (byName !== 0) return byName;
        return a.id.localeCompare(b.id);
      });
    }
    return byFaction;
  }, [definitions, unitDefs, getUnitSpecials, getHomeTerritoryDisplayNames]);

  // Per-special list of units (for Specials modal): ordered by faction, then cost, then name.
  const unitsBySpecial = useMemo(() => {
    if (!definitions?.units || !definitions?.territories) return {};
    const territories = definitions.territories;
    const factions = definitions.factions ?? {};
    const factionOrder: string[] = (() => {
      const good: string[] = [];
      const evil: string[] = [];
      const other: string[] = [];
      Object.keys(factions).forEach(fid => {
        const a = (factions[fid] as { alliance?: string })?.alliance;
        if (a === 'good') good.push(fid);
        else if (a === 'evil') evil.push(fid);
        else other.push(fid);
      });
      return [...good.sort(), ...evil.sort(), ...other.sort()];
    })();
    const bySpecial: Record<string, Array<{ unitId: string; name: string; faction: string; factionDisplayName: string; cost: number; homeTerritoryDisplayNames?: string[] }>> = {};
    for (const [unitId, u] of Object.entries(definitions.units)) {
      const cost = typeof u.cost === 'object' && u.cost?.power != null ? u.cost.power : 0;
      const specialIds = getUnitSpecialIds(u);
      const homeNames = getHomeTerritoryDisplayNames(u, territories);
      const factionDisplayName = (factions[u.faction] as { display_name?: string } | undefined)?.display_name ?? u.faction;
      for (const specialId of specialIds) {
        if (!bySpecial[specialId]) bySpecial[specialId] = [];
        bySpecial[specialId].push({
          unitId,
          name: u.display_name,
          faction: u.faction,
          factionDisplayName,
          cost,
          ...(specialId === 'home' && homeNames.length ? { homeTerritoryDisplayNames: homeNames } : {}),
        });
      }
    }
    for (const specialId of Object.keys(bySpecial)) {
      bySpecial[specialId].sort((a, b) => {
        const fa = factionOrder.indexOf(a.faction);
        const fb = factionOrder.indexOf(b.faction);
        if (fa !== fb) return (fa === -1 ? 999 : fa) - (fb === -1 ? 999 : fb);
        if (a.cost !== b.cost) return a.cost - b.cost;
        return a.name.localeCompare(b.name);
      });
    }
    return bySpecial;
  }, [definitions?.units, definitions?.territories, definitions?.factions, getUnitSpecialIds, getHomeTerritoryDisplayNames]);

  // Naval unit IDs (for purchase modal Sea tab and mobilization: naval -> sea zone)
  const navalUnitIds = useMemo(() => {
    const units = definitions?.units;
    if (!units || typeof units !== 'object') return new Set<string>();
    const set = new Set<string>();
    for (const [id, u] of Object.entries(units)) {
      const arch = (u as { archetype?: string }).archetype;
      const tags = (u as { tags?: string[] }).tags;
      if (arch === 'naval' || (Array.isArray(tags) && tags.includes('naval'))) set.add(id);
    }
    return set;
  }, [definitions?.units]);

  /** Siegework archetype units get their own Purchase modal tab (after Sea). */
  const siegeworkUnitIds = useMemo(() => {
    const units = definitions?.units;
    if (!units || typeof units !== 'object') return new Set<string>();
    const set = new Set<string>();
    for (const [id, u] of Object.entries(units)) {
      const arch = (u as { archetype?: string }).archetype;
      const tags = (u as { tags?: string[] }).tags;
      if (arch === 'siegework' || (Array.isArray(tags) && tags.includes('siegework'))) set.add(id);
    }
    return set;
  }, [definitions?.units]);

  // Purchasable units for current faction (with land/naval/siege split for purchase modal tabs)
  const availableUnits = useMemo(() => {
    if (!availableActions?.purchasable_units) return [];
    return availableActions.purchasable_units.map(u => {
      const def = definitions?.units?.[u.unit_id] as { specials?: string[] } | undefined;
      const specialsDefs = definitions?.specials as Record<string, unknown> | undefined;
      // Only ids with an entry in setup specials.json (object defs). Drops naval/land/transportable/siegework etc.
      const specIds = (Array.isArray(def?.specials) ? def.specials : []).filter((sid) => {
        if (typeof sid !== 'string' || !sid || !specialsDefs) return false;
        const entry = specialsDefs[sid];
        return entry != null && typeof entry === 'object' && !Array.isArray(entry);
      });
      const specialLabels = specIds.map((sid) => {
        const sd = specialsDefs?.[sid] as { name?: string } | undefined;
        const n = typeof sd?.name === 'string' ? sd.name.trim() : '';
        return n || sid.replace(/_/g, ' ');
      });
      const ud = unitDefs[u.unit_id] as { home_territory_ids?: string[] } | undefined;
      const homeTerritoryCount = ud?.home_territory_ids?.length ?? 0;
      const isNaval = navalUnitIds.has(u.unit_id);
      return {
        id: u.unit_id,
        name: u.display_name || u.unit_id,
        icon: unitDefs[u.unit_id]?.icon || `/assets/units/${u.unit_id}.png`,
        cost: u.cost || {},
        attack: u.attack,
        defense: u.defense,
        movement: u.movement,
        health: u.health,
        dice: u.dice ?? 1,
        isNaval,
        isSiegework: !isNaval && siegeworkUnitIds.has(u.unit_id),
        specialLabels,
        homeTerritoryCount,
      };
    });
  }, [availableActions, unitDefs, navalUnitIds, siegeworkUnitIds, definitions?.units, definitions?.specials]);

  // Mobilizable purchases
  const mobilizablePurchases = useMemo(() => {
    if (!backendState || !definitions) return [];
    // Use faction_purchased_units from backend state
    const purchases = backendState.faction_purchased_units?.[backendState.current_faction] || [];
    return purchases
      .filter(p => p.count > 0)
      .map(p => ({
        unitId: p.unit_id,
        name: definitions.units[p.unit_id]?.display_name || p.unit_id,
        icon: unitDefs[p.unit_id]?.icon || `/assets/units/${p.unit_id}.png`,
        count: p.count,
      }));
  }, [backendState, definitions, unitDefs]);

  // Unplaced camps (purchased this turn); must be placed during mobilization.
  // Exclude: already placed (placed_territory_id set) or queued (in pending_camp_placements).
  const unplacedCamps = useMemo(() => {
    const isMobilize = backendState?.phase === 'mobilization';
    const raw =
      backendState?.pending_camps ??
      (isMobilize ? availableActions?.pending_camps : undefined);
    const list = Array.isArray(raw) ? (raw as { territory_options?: string[]; placed_territory_id?: string | null }[]) : [];
    const queuedIndices = new Set(
      (backendState?.pending_camp_placements ?? []).map((p: { camp_index: number }) => p.camp_index)
    );
    return list
      .map((c, i) => ({
        campIndex: i,
        options: c.territory_options ?? [],
        placed: typeof c.placed_territory_id === 'string' && c.placed_territory_id.length > 0,
        queued: queuedIndices.has(i),
      }))
      .filter(c => !c.placed && !c.queued);
  }, [backendState?.pending_camps, backendState?.pending_camp_placements, backendState?.phase, availableActions?.pending_camps]);

  // Clear camp selection when the selected camp is no longer unplaced (placed or queued)
  useEffect(() => {
    if (selectedCampIndex != null && !unplacedCamps.some(c => c.campIndex === selectedCampIndex)) {
      setSelectedCampIndex(null);
    }
  }, [selectedCampIndex, unplacedCamps]);

  // Territory IDs that already have a pending camp placement (so no second camp can target them)
  const territoriesWithPendingCampPlacement = useMemo(
    () =>
      new Set(
        (backendState?.pending_camp_placements ?? []).map((p: { territory_id: string }) => p.territory_id)
      ),
    [backendState?.pending_camp_placements]
  );

  // Valid territories for placing the currently selected camp (exclude pending placements and 0-power territories).
  const validCampTerritories = useMemo(() => {
    if (selectedCampIndex == null) return [];
    const isMobilize = backendState?.phase === 'mobilization';
    const list =
      backendState?.pending_camps ??
      (isMobilize ? availableActions?.pending_camps : undefined) ??
      [];
    const camp = Array.isArray(list) ? (list as { territory_options?: string[] }[])[selectedCampIndex] : undefined;
    const options = (camp?.territory_options ?? []) as string[];
    return options.filter(
      t => !territoriesWithPendingCampPlacement.has(t) && (currentTerritoryData[t]?.produces ?? 0) > 0
    );
  }, [backendState?.pending_camps, backendState?.pending_camp_placements, availableActions?.pending_camps, selectedCampIndex, territoriesWithPendingCampPlacement, currentTerritoryData]);

  const addLogEntry = useCallback((message: string, type: string = 'info', payload?: GameEvent['payload']) => {
    const event: GameEvent = {
      id: `${Date.now()}-${Math.random()}`,
      type,
      message,
      timestamp: Date.now(),
      ...(payload && Object.keys(payload).length > 0 ? { payload } : undefined),
    };
    setEventLog(prev => [event, ...prev]);
  }, []);

  // Add backend events to log (only entries with a message; store full payload for filtering)
  const addBackendEvents = useCallback((events: ApiEvent[]) => {
    const ts = Date.now();
    const newEntries: GameEvent[] = events
      .map(e => {
        const msg = (e.payload?.message as string) ?? e.type;
        if (!msg) return null;
        return {
          id: `${ts}-${Math.random()}-${e.type}`,
          type: e.type,
          message: msg,
          timestamp: ts,
          payload: e.payload as GameEvent['payload'],
        } as GameEvent;
      })
      .filter((e): e is GameEvent => e != null);
    if (newEntries.length > 0) {
      setEventLog(prev => [...newEntries, ...prev]);
    }
  }, []);

  // Refresh game state and available actions from backend (and meta so lobby→start is detected by all clients)
  const refreshState = useCallback(async () => {
    try {
      const [stateRes, actionsRes, metaRes] = await Promise.all([
        api.getGame(GAME_ID),
        api.getAvailableActions(GAME_ID),
        api.getGameMeta(GAME_ID).catch(() => null),
      ]);
      if (metaRes) setGameMeta(metaRes);
      setBackendState({
        ...stateRes.state,
        pending_camps: stateRes.pending_camps ?? stateRes.state?.pending_camps ?? [],
      });
      if (stateRes.definitions) setDefinitions(stateRes.definitions);
      setCanAct(stateRes.can_act ?? true);
      setAvailableActions(actionsRes);
      setGameSetupId(stateRes.setup_id ?? null);
      if (Array.isArray(stateRes.event_log) && stateRes.event_log.length > 0) {
        const withMessage = stateRes.event_log.filter(
          (e) => e.payload && (e.payload.message as string)
        ) as { type: string; payload: Record<string, unknown> & { message?: string } }[];
        const asGameEvents: GameEvent[] = withMessage
          .map((e, i) => ({
            id: `persisted-${GAME_ID}-${i}`,
            type: e.type,
            message: (e.payload.message as string) || e.type,
            timestamp: 0,
            payload: e.payload as GameEvent['payload'],
          }))
          .reverse();
        setEventLog(asGameEvents);
      }
      setError(null);
    } catch (err) {
      // Game may have been deleted — create it (legacy) and load again only for default dev game
      if (GAME_ID !== DEFAULT_GAME_ID) {
        setError(err instanceof Error ? err.message : 'Failed to load game');
        return;
      }
      try {
        const createRes = await api.createGameLegacy(GAME_ID);
        setBackendState(createRes.state);
        setCanAct(true);
        const actionsRes = await api.getAvailableActions(GAME_ID);
        setAvailableActions(actionsRes);
        setError(null);
      } catch (createErr) {
        console.error('Failed to refresh state:', createErr);
        setError(createErr instanceof Error ? createErr.message : 'Failed to refresh state');
      }
    }
  }, [GAME_ID]);

  const handleGameStarted = useCallback(async () => {
    const metaRes = await api.getGameMeta(GAME_ID);
    setGameMeta(metaRes);
    await refreshState();
  }, [GAME_ID, refreshState]);

  // Initialize game on mount (or when GAME_ID changes)
  useEffect(() => {
    async function init() {
      setLoading(true);
      try {
        let gotDefinitionsFromGame = false;
        try {
          const stateRes = await api.getGame(GAME_ID);
          const hadInitialState = initialTurnOrderRef.current != null;
          if (hadInitialState) {
            // Keep create response as source of truth; only pull definitions and can_act from fetch
            setCanAct(stateRes.can_act ?? true);
            setGameSetupId(stateRes.setup_id ?? null);
            if (stateRes.definitions) {
              setDefinitions(stateRes.definitions);
              gotDefinitionsFromGame = true;
            }
            // Do not overwrite backendState so turn_order from create stays
          } else {
            setBackendState((prev) => {
              const next = stateRes.state;
              const fromTop = stateRes.turn_order?.length ? stateRes.turn_order : null;
              const fromState = next?.turn_order?.length ? next.turn_order : null;
              const fromPrev = prev?.turn_order?.length ? prev.turn_order : null;
              const order = fromTop ?? fromState ?? fromPrev ?? next?.turn_order;
              const pendingCamps = stateRes.pending_camps ?? next?.pending_camps ?? [];
              const base = order ? { ...next, turn_order: order } : next;
              return { ...base, pending_camps: pendingCamps };
            });
            setCanAct(stateRes.can_act ?? true);
            setGameSetupId(stateRes.setup_id ?? null);
            if (stateRes.definitions) {
              setDefinitions(stateRes.definitions);
              gotDefinitionsFromGame = true;
            }
            if (Array.isArray(stateRes.event_log) && stateRes.event_log.length > 0) {
              const withMessage = stateRes.event_log.filter(
                (e: { payload?: { message?: string } }) => e.payload?.message
              );
              const asGameEvents: GameEvent[] = withMessage
                .map((e: { type: string; payload: Record<string, unknown> }, i: number) => ({
                  id: `persisted-${GAME_ID}-${i}`,
                  type: e.type,
                  message: (e.payload?.message as string) || e.type,
                  timestamp: 0,
                  payload: e.payload as GameEvent['payload'],
                }))
                .reverse();
              setEventLog(asGameEvents);
            }
          }
        } catch {
          if (GAME_ID === DEFAULT_GAME_ID) {
            const createRes = await api.createGameLegacy(GAME_ID);
            setBackendState(createRes.state);
            setCanAct(true);
            addLogEntry('New game created!', 'info');
          } else {
            throw new Error('Game not found');
          }
        }
        if (!gotDefinitionsFromGame) {
          const defs = await api.getDefinitions();
          setDefinitions(defs);
        }

        // Get available actions
        const actionsRes = await api.getAvailableActions(GAME_ID);
        setAvailableActions(actionsRes);
        setError(null);
      } catch (err) {
        console.error('Init error:', err);
        setError(err instanceof Error ? err.message : 'Failed to initialize game');
      } finally {
        setLoading(false);
      }
    }
    init();
  }, [addLogEntry, GAME_ID]);

  // Poll game state only in multiplayer so other players' actions appear live (1s when spectating a battle, else 3s)
  const isMultiplayer = gameMeta?.is_multiplayer ?? Boolean(gameMeta?.game_code);
  const pollIntervalMs = spectatingBattle ? 1000 : GAME_POLL_INTERVAL_MS;
  useEffect(() => {
    if (!GAME_ID || !isMultiplayer) return;
    const t = setInterval(() => refreshState(), pollIntervalMs);
    return () => clearInterval(t);
  }, [GAME_ID, refreshState, pollIntervalMs, isMultiplayer]);

  // AI turn: run one ai-step, then wait AI_STEP_DELAY_MS so the human can follow, then refresh and repeat until human turn or game over
  const isAITurn = Boolean(
    GAME_ID &&
    backendState &&
    !backendState.winner &&
    gameMeta?.ai_factions?.length &&
    gameMeta.ai_factions.includes(backendState.current_faction ?? '')
  );
  useEffect(() => {
    if (!isAITurn || aiStepInProgressRef.current) return;
    aiStepInProgressRef.current = true;
    (async () => {
      try {
        const result = await api.aiStep(GAME_ID);
        setBackendState(result.state);
        if (result.can_act !== undefined) setCanAct(result.can_act);
        if (result.events?.length) addBackendEvents(result.events);
        const actionsRes = await api.getAvailableActions(GAME_ID);
        setAvailableActions(actionsRes);
      } catch (err) {
        addLogEntry(err instanceof Error ? err.message : 'AI step failed', 'error');
        await refreshState();
      } finally {
        aiStepTimeoutRef.current = setTimeout(() => {
          aiStepInProgressRef.current = false;
          aiStepTimeoutRef.current = null;
          refreshState();
        }, AI_STEP_DELAY_MS);
      }
    })();
  }, [isAITurn, GAME_ID, addBackendEvents, addLogEntry, refreshState, backendState]);

  // Clear AI step timeout on unmount
  useEffect(() => {
    return () => {
      if (aiStepTimeoutRef.current) clearTimeout(aiStepTimeoutRef.current);
    };
  }, []);

  // Clear phase-specific state when phase changes
  useEffect(() => {
    if (!backendState) return;

    const currentPhase = backendState.phase;

    // Clear combat move tracking when leaving combat_move phase
    if (currentPhase !== 'combat_move') {
      setHasCombatMovedThisPhase(false);
    }

    // Clear non-combat move tracking when leaving non_combat_move phase
    if (currentPhase !== 'non_combat_move') {
      setHasNonCombatMovedThisPhase(false);
    }

    // Clear combat tracking when leaving combat phase
    if (currentPhase !== 'combat') {
      setBattlesCompletedThisPhase(0);
      setActiveCombat(null);
      setPendingRetreat(null);
    }

    // Snapshot combat moves count while in combat_move so we can show correct message in combat when all uncontested
    if (backendState?.phase === 'combat_move') {
      const count = (backendState.pending_moves || []).filter((m: { phase: string }) => m.phase === 'combat_move').length;
      setCombatMovesDeclaredThisPhase(count);
    } else if (backendState?.phase !== 'combat') {
      setCombatMovesDeclaredThisPhase(0);
    }
  }, [backendState?.phase, backendState?.pending_moves]);

  // Build a frontend-compatible GameState from backend state
  const gameState: GameState = useMemo(() => {
    if (!backendState) {
      return {
        turn_number: 1,
        current_faction: '',
        phase: 'purchase' as GamePhase,
        territories: {},
        faction_resources: {},
        pending_purchases: {},
        pending_moves: [],
        pending_mobilizations: [],
        pending_camp_placements: [],
        declared_battles: [],
        map_asset: undefined,
        turn_order: undefined,
      };
    }

    // Convert territories to frontend format (pending_captures only while combat_move/combat)
    const pcMove = pendingCapturesOverlayForPhase(
      backendState.phase,
      backendState.pending_captures as Record<string, string> | undefined,
    );
    const territories: Record<string, { id: string; owner?: string; units: any[] }> = {};
    for (const [id, territory] of Object.entries(backendState.territories)) {
      const po = pcMove[id];
      const ownerResolved =
        po != null && po !== ''
          ? displayOwnerForPendingCapture(
            po,
            territory as { owner?: string | null; original_owner?: string | null },
            factionData,
          )
          : territory.owner;
      territories[id] = {
        id,
        owner: ownerResolved || undefined,
        units: territory.units,
      };
    }

    // Convert faction_resources directly from backend (dynamic, not hardcoded)
    const factionResources: Record<string, Record<string, number>> = {};
    for (const [factionId, resources] of Object.entries(backendState.faction_resources)) {
      factionResources[factionId] = resources;
    }

    // Get pending purchases for current faction
    const factionPurchases = backendState.faction_purchased_units?.[backendState.current_faction] || [];
    const pendingPurchases: Record<string, number> = {};
    factionPurchases.forEach(p => {
      pendingPurchases[p.unit_id] = p.count;
    });

    // Declared battles: backend combat_territories only (includes sea raids with sea_zone_id from territory_sea_raid_from).
    // Do not merge sea_raid_targets — that lists every adjacent land to your navy and would show bogus duplicate battles.
    const declaredBattles: DeclaredBattle[] = (availableActions?.combat_territories || []).map((ct): DeclaredBattle => {
      const c = ct as { territory_id: string; attacker_unit_ids?: string[]; defender_unit_ids?: string[]; sea_zone_id?: string };
      return {
        territory: c.territory_id,
        sea_zone_id: c.sea_zone_id,
        attacker_units: Array.isArray(c.attacker_unit_ids) ? c.attacker_unit_ids : [],
        defender_units: Array.isArray(c.defender_unit_ids) ? c.defender_unit_ids : [],
      };
    });

    // Use backend's pending_moves directly. Prefer primary_unit_id from server; else roster lookup;
    // never trust parsing instance id strings (format is implementation detail).
    const pendingMoves = (backendState.pending_moves || []).map((move, idx) => {
      const fromT = move.from_territory;
      const firstIid = move.unit_instance_ids?.[0];
      const fromServer = (move as { primary_unit_id?: string }).primary_unit_id;
      let unitType = typeof fromServer === 'string' && fromServer.trim() ? fromServer.trim() : '';
      if (!unitType && firstIid && fromT && backendState.territories?.[fromT]?.units) {
        const row = (backendState.territories[fromT].units as { instance_id?: string; unit_id?: string }[]).find(
          u => u.instance_id === firstIid,
        );
        if (row?.unit_id) unitType = String(row.unit_id);
      }
      if (!unitType && typeof firstIid === 'string') {
        unitType = firstIid.split('_').slice(1, -1).join('_') || '';
      }
      return {
        id: `move_${idx}`,
        from: move.from_territory,
        to: move.to_territory,
        unitType,
        count: move.unit_instance_ids.length,
        phase: move.phase as 'combat_move' | 'non_combat_move',
        move_type: move.move_type ?? undefined,
        unit_instance_ids: [...(move.unit_instance_ids || [])],
        load_onto_boat_instance_id: move.load_onto_boat_instance_id ?? null,
        ...(typeof fromServer === 'string' && fromServer.trim() ? { primary_unit_id: fromServer.trim() } : {}),
      };
    });

    const pendingMobilizations = (backendState.pending_mobilizations || []).map((pm, idx) => ({
      id: `mob_${idx}`,
      destination: pm.destination,
      units: pm.units,
    }));

    const pendingCampPlacements = (backendState.pending_camp_placements || []).map(p => ({
      camp_index: p.camp_index,
      territory_id: p.territory_id,
    }));

    return {
      turn_number: backendState.turn_number,
      current_faction: backendState.current_faction,
      phase: (backendState.phase === 'mobilization' ? 'mobilize' : backendState.phase) as GamePhase,
      territories,
      faction_resources: factionResources,
      pending_purchases: pendingPurchases,
      pending_moves: pendingMoves,
      pending_mobilizations: pendingMobilizations,
      pending_camp_placements: pendingCampPlacements,
      declared_battles: declaredBattles,
      map_asset: backendState.map_asset ?? undefined,
      turn_order: initialTurnOrderRef.current ?? backendState.turn_order ?? undefined,
    };
  }, [backendState, availableActions, definitions, factionData]);

  // Per-destination mobilization cap: territory_id/sea_zone_id -> power (land from camp territories, naval from port-adjacent sea zones)
  const mobilizationTerritoryPower = useMemo(() => {
    const out: Record<string, number> = {};
    const territories = availableActions?.mobilize_options?.capacity?.territories;
    if (Array.isArray(territories)) {
      for (const t of territories) {
        out[t.territory_id] = t.power ?? 0;
      }
    }
    const seaZones = availableActions?.mobilize_options?.capacity?.sea_zones;
    if (Array.isArray(seaZones)) {
      for (const s of seaZones) {
        out[s.sea_zone_id] = s.power ?? 0;
      }
    }
    return out;
  }, [availableActions?.mobilize_options?.capacity?.territories, availableActions?.mobilize_options?.capacity?.sea_zones]);

  // Land: owned territories with a camp. Naval: sea zones adjacent to an owned port.
  const validMobilizeTerritories = useMemo(
    () => availableActions?.mobilize_options?.territories ?? availableActions?.mobilize_options?.available_strongholds ?? [],
    [availableActions]
  );
  const validMobilizeSeaZones = useMemo(
    () => availableActions?.mobilize_options?.sea_zones ?? [],
    [availableActions]
  );
  const hasPort = (validMobilizeSeaZones?.length ?? 0) > 0;

  // Remaining capacity per territory (power minus units already pending to that destination)
  const remainingMobilizationCapacity = useMemo(() => {
    const power = { ...mobilizationTerritoryPower };
    const pending = backendState?.pending_mobilizations ?? [];
    for (const pm of pending) {
      const dest = pm.destination;
      if (dest && power[dest] !== undefined) {
        const sum = (pm.units ?? []).reduce((s: number, u: { count?: number }) => s + (u.count ?? 0), 0);
        power[dest] = Math.max(0, (power[dest] ?? 0) - sum);
      }
    }
    return power;
  }, [mobilizationTerritoryPower, backendState?.pending_mobilizations]);

  // Home territories: remaining "home" slots per territory per unit type (0 or 1). Used so land units can deploy to home without a camp.
  const remainingHomeSlots = useMemo(() => {
    const out: Record<string, Record<string, number>> = {};
    const pending = backendState?.pending_mobilizations ?? [];
    const pendingByDestAndUnit: Record<string, Record<string, number>> = {};
    for (const pm of pending) {
      const dest = pm.destination ?? '';
      if (!dest) continue;
      for (const u of pm.units ?? []) {
        const uid = u.unit_id ?? '';
        if (!uid) continue;
        if (!pendingByDestAndUnit[dest]) pendingByDestAndUnit[dest] = {};
        pendingByDestAndUnit[dest][uid] = (pendingByDestAndUnit[dest][uid] ?? 0) + (u.count ?? 0);
      }
    }
    const mergeHome = (tid: string, home: Record<string, number> | undefined) => {
      if (!home || typeof home !== 'object') return;
      if (!out[tid]) out[tid] = {};
      for (const unitId of Object.keys(home)) {
        const cap = Number(home[unitId]) || 1;
        const used = pendingByDestAndUnit[tid]?.[unitId] ?? 0;
        out[tid][unitId] = Math.max(0, cap - used);
      }
    };
    const territories = availableActions?.mobilize_options?.capacity?.territories;
    if (Array.isArray(territories)) {
      for (const t of territories) {
        const tid = (t as { territory_id?: string }).territory_id;
        if (!tid) continue;
        mergeHome(tid, (t as { home_unit_capacity?: Record<string, number> }).home_unit_capacity);
      }
    }
    const portTerritories = availableActions?.mobilize_options?.capacity?.port_territories;
    if (Array.isArray(portTerritories)) {
      for (const p of portTerritories) {
        const tid = (p as { territory_id?: string }).territory_id;
        if (!tid) continue;
        mergeHome(tid, (p as { home_unit_capacity?: Record<string, number> }).home_unit_capacity);
      }
    }
    return out;
  }, [
    availableActions?.mobilize_options?.capacity?.territories,
    availableActions?.mobilize_options?.capacity?.port_territories,
    backendState?.pending_mobilizations,
  ]);

  // Bulk "All" mobilization: show only when there exists a single destination
  // that can accept every remaining purchase stack in the tray.
  const mobilizationAllValidZones = useMemo(() => {
    const purchases = mobilizablePurchases;
    if (purchases.length <= 1) return [];

    const hasNaval = purchases.some(p => navalUnitIds.has(p.unitId));
    const hasLand = purchases.some(p => !navalUnitIds.has(p.unitId));
    if (hasNaval && hasLand) return [];

    const totalCount = purchases.reduce((s, p) => s + p.count, 0);
    const candidateZones = hasNaval ? validMobilizeSeaZones : validMobilizeTerritories;
    if (candidateZones.length === 0) return [];

    if (hasNaval) {
      // Naval mobilization goes to sea zones; no home-slot fallback.
      return candidateZones.filter(destId => (remainingMobilizationCapacity[destId] ?? 0) >= totalCount);
    }

    // Land mobilization: camp capacity is shared across unit types (camp-first).
    // If camp capacity exists, require it to cover *all* units; otherwise require all stacks deployable via home.
    return candidateZones.filter(destId => {
      const campRemaining = remainingMobilizationCapacity[destId] ?? 0;
      if (campRemaining > 0) return campRemaining >= totalCount;
      return purchases.every(p => (remainingHomeSlots[destId]?.[p.unitId] ?? 0) >= p.count);
    });
  }, [
    mobilizablePurchases,
    navalUnitIds,
    validMobilizeSeaZones,
    validMobilizeTerritories,
    remainingMobilizationCapacity,
    remainingHomeSlots,
  ]);

  // Naval tray: boats + passengers for selected sea zone (movement phases only). Pending loads are not yet in
  // territory state — distribute preview icons per boat using load_onto_boat_instance_id when set, else the same
  // sequential fill the backend uses at phase end (sorted boats, fill each up to remaining slots).
  const navalTrayData = useMemo(() => {
    const seaZoneId = selectedSeaZoneForNavalTray;
    if (!seaZoneId || !navalUnitIds.size || !backendState) return null;
    const fullUnits = territoryUnitsFull[seaZoneId] ?? [];
    const boats = fullUnits.filter((u) => navalUnitIds.has(u.unit_id));
    const canonSea = canonicalSeaZoneId(seaZoneId);
    const loadMovesToZone = (backendState.pending_moves ?? []).filter((m) => {
      if (m.phase !== gameState.phase) return false;
      const to = String(m.to_territory ?? '').trim();
      if (canonicalSeaZoneId(to) !== canonSea && to !== seaZoneId) return false;
      if (m.move_type === 'load') return true;
      if (m.move_type != null && m.move_type !== '') return false;
      const from = String(m.from_territory ?? '').trim();
      const fromSea =
        currentTerritoryData[from]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(from);
      const toSea =
        currentTerritoryData[to]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(to);
      return !fromSea && toSea;
    });
    const sortedLoadMoves = [...loadMovesToZone].sort((a, b) => {
      const af = String(a.from_territory ?? '');
      const bf = String(b.from_territory ?? '');
      if (af !== bf) return af.localeCompare(bf);
      const at = String(a.to_territory ?? '');
      const bt = String(b.to_territory ?? '');
      if (at !== bt) return at.localeCompare(bt);
      const aIds = (a.unit_instance_ids ?? []).join('\0');
      const bIds = (b.unit_instance_ids ?? []).join('\0');
      return aIds.localeCompare(bIds);
    });
    const faction = gameState.current_faction ?? '';
    let navalBoatsSorted = boats
      .filter((u) => unitDefs[u.unit_id]?.faction === faction)
      .sort((a, b) => a.instance_id.localeCompare(b.instance_id));
    if (navalBoatsSorted.length === 0) {
      navalBoatsSorted = [...boats].sort((a, b) => a.instance_id.localeCompare(b.instance_id));
    }
    const iconForInstance = (fromTerritory: string, instanceId: string) => {
      const fromT = backendState.territories?.[fromTerritory];
      const u = fromT?.units?.find((x) => x.instance_id === instanceId);
      const parsedId = instanceId.split('_').slice(1, -1).join('_') || '';
      const unitId = u?.unit_id ?? parsedId;
      return {
        unitId,
        name: unitDefs[unitId]?.name ?? unitId,
        icon: unitDefs[unitId]?.icon ?? `/assets/units/${unitId}.png`,
        instanceId,
      };
    };
    const pendingByBoat: Record<string, { unitId: string; name: string; icon: string; instanceId: string }[]> = {};
    const virtualPending: Record<string, number> = {};
    for (const move of sortedLoadMoves) {
      const fromT = String(move.from_territory ?? '').trim();
      const ids = [...(move.unit_instance_ids ?? [])].sort((x, y) => x.localeCompare(y));
      const explicit = (move.load_onto_boat_instance_id ?? '').trim() || null;
      if (explicit) {
        for (const iid of ids) {
          if (!pendingByBoat[explicit]) pendingByBoat[explicit] = [];
          pendingByBoat[explicit].push(iconForInstance(fromT, iid));
          virtualPending[explicit] = (virtualPending[explicit] ?? 0) + 1;
        }
        continue;
      }
      let pidx = 0;
      for (const boat of navalBoatsSorted) {
        const bid = boat.instance_id;
        const cap = Number((unitDefs[boat.unit_id] as { transport_capacity?: number } | undefined)?.transport_capacity ?? 0);
        const onboard = fullUnits.filter((u) => u.loaded_onto === bid).length;
        const used = virtualPending[bid] ?? 0;
        let slots = Math.max(0, cap - onboard - used);
        while (slots > 0 && pidx < ids.length) {
          if (!pendingByBoat[bid]) pendingByBoat[bid] = [];
          pendingByBoat[bid].push(iconForInstance(fromT, ids[pidx]));
          virtualPending[bid] = (virtualPending[bid] ?? 0) + 1;
          pidx++;
          slots--;
        }
      }
    }
    const boatsForTray = boats.map((boat) => {
      const passengers = fullUnits.filter((u) => u.loaded_onto === boat.instance_id);
      const passengerIcons = passengers.map((p) => ({
        unitId: p.unit_id,
        name: unitDefs[p.unit_id]?.name ?? p.unit_id,
        icon: unitDefs[p.unit_id]?.icon ?? `/assets/units/${p.unit_id}.png`,
        instanceId: p.instance_id,
      }));
      const pendingIcons = pendingByBoat[boat.instance_id] ?? [];
      return {
        boatInstanceId: boat.instance_id,
        unitId: boat.unit_id,
        name: unitDefs[boat.unit_id]?.name ?? boat.unit_id,
        icon: unitDefs[boat.unit_id]?.icon ?? `/assets/units/${boat.unit_id}.png`,
        passengers: [...passengerIcons, ...pendingIcons],
        transportCapacity: Number((unitDefs[boat.unit_id] as { transport_capacity?: number } | undefined)?.transport_capacity ?? 0),
      };
    });
    const seaZoneName = currentTerritoryData[seaZoneId]?.name ?? seaZoneId.replace(/_/g, ' ');
    const factionColor = gameState.current_faction ? factionData[gameState.current_faction]?.color : undefined;
    return { seaZoneId, seaZoneName, boats: boatsForTray, factionColor: factionColor ?? '#1a4d8c' };
  }, [
    selectedSeaZoneForNavalTray,
    territoryUnitsFull,
    navalUnitIds,
    unitDefs,
    currentTerritoryData,
    gameState.current_faction,
    factionData,
    backendState,
    gameState.phase,
  ]);

  /** Sea zones where stack click should open the tray after closing it: ≥2 boats + pending load into that zone this phase. */
  const seaZoneIdsEligibleForNavalTrayStackClick = useMemo(() => {
    const out = new Set<string>();
    if (!backendState?.pending_moves) return out;
    const phase = gameState.phase;
    if (phase !== 'combat_move' && phase !== 'non_combat_move') return out;
    for (const m of backendState.pending_moves) {
      if (!pendingMoveIsSeaLoadForTray(m, phase, currentTerritoryData)) continue;
      const to = String(m.to_territory ?? '').trim();
      if (!to) continue;
      const canon = canonicalSeaZoneId(to);
      const full = territoryUnitsFull[to] ?? territoryUnitsFull[canon] ?? [];
      const boats = full.filter((u) => navalUnitIds.has(u.unit_id));
      if (boats.length >= 2) {
        out.add(to);
        out.add(canon);
      }
    }
    return out;
  }, [backendState?.pending_moves, gameState.phase, currentTerritoryData, territoryUnitsFull, navalUnitIds]);

  // Pending load: instance IDs we're loading (when load into 2+ boats, for tray allocation)
  const pendingLoadPassengerInstanceIds = useMemo(() => {
    const pm = pendingMoveConfirm;
    if (!pm?.boatOptions || pm.boatOptions.length < 2 || !backendState) return [];
    const fromStr = typeof pm.fromTerritory === 'string' ? pm.fromTerritory.trim() : '';
    const count = pm.count ?? 0;
    const unitId = pm.unitId ?? '';
    if (!fromStr || count <= 0 || !unitId) return [];
    const territory = backendState.territories?.[fromStr];
    if (!territory?.units) return [];
    const currentPhase = gameState.phase;
    const committed = new Set(
      (backendState.pending_moves ?? [])
        .filter((m: { from_territory?: string; phase?: string }) => m.from_territory === fromStr && m.phase === currentPhase)
        .flatMap((m: { unit_instance_ids?: string[] }) => m.unit_instance_ids ?? [])
    );
    return territory.units
      .filter((u: { unit_id: string; instance_id: string }) => u.unit_id === unitId && !committed.has(u.instance_id))
      .slice(0, count)
      .map((u: { instance_id: string }) => u.instance_id);
  }, [pendingMoveConfirm, backendState, gameState.phase]);

  const pendingLoadPassengers = useMemo(() => {
    const pm = pendingMoveConfirm;
    const unitId = pm?.unitId ?? '';
    const def = unitId ? unitDefs[unitId] : undefined;
    const name = def?.name ?? unitId;
    const icon = def?.icon ?? `/assets/units/${unitId}.png`;
    return pendingLoadPassengerInstanceIds.map((instanceId) => ({
      instanceId,
      unitId,
      name,
      icon,
    }));
  }, [pendingMoveConfirm?.unitId, pendingLoadPassengerInstanceIds, unitDefs]);

  // When tray opens for load with 2+ boats, init loadAllocation (sorted boats, pending loads consume slots).
  useEffect(() => {
    const pm = pendingMoveConfirm;
    if (!pm?.boatOptions || pm.boatOptions.length < 2 || pm.loadAllocation != null) return;
    if (pendingLoadPassengerInstanceIds.length === 0) return;
    const toSea = typeof pm.toTerritory === 'string' ? pm.toTerritory.trim() : '';
    const full = backendState?.territories?.[toSea]?.units as
      | { instance_id: string; unit_id: string; loaded_onto?: string | null }[]
      | undefined;
    if (!full?.length || !definitions?.units) return;
    const initial = computeInitialLoadAllocation(
      pm.boatOptions,
      pendingLoadPassengerInstanceIds,
      full,
      definitions.units as Record<string, { transport_capacity?: number } | undefined>,
      backendState?.pending_moves ?? [],
      gameState.phase,
      toSea,
      currentTerritoryData,
    );
    if (!initial) return;
    loadAllocationRef.current = initial;
    setPendingMoveConfirmState((prev) => (prev ? { ...prev, loadAllocation: initial } : prev));
  }, [
    pendingMoveConfirm?.boatOptions,
    pendingMoveConfirm?.loadAllocation,
    pendingMoveConfirm?.toTerritory,
    pendingLoadPassengerInstanceIds,
    backendState?.territories,
    backendState?.pending_moves,
    definitions?.units,
    currentTerritoryData,
    gameState.phase,
  ]);

  const handleLoadAllocationChange = useCallback((allocation: Record<string, string[]>) => {
    loadAllocationRef.current = allocation;
    setPendingMoveConfirmState((prev) => (prev ? { ...prev, loadAllocation: allocation } : prev));
  }, []);

  // --- Action Handlers ---

  const handleTerritorySelect = useCallback((territoryId: string | null) => {
    // Camp placement: click a valid territory → set pending (confirm/cancel in sidebar, like units)
    if (gameState.phase === 'mobilize' && selectedCampIndex !== null && territoryId && validCampTerritories.includes(territoryId)) {
      setPendingCampPlacement({ campIndex: selectedCampIndex, territoryId });
      return;
    }
    if (gameState.phase === 'mobilize' && selectedMobilizationUnit && territoryId) {
      const validDestinations = navalUnitIds.has(selectedMobilizationUnit) ? validMobilizeSeaZones : validMobilizeTerritories;
      if (validDestinations.includes(territoryId)) {
        const purchase = mobilizablePurchases.find(p => p.unitId === selectedMobilizationUnit);
        if (purchase) {
          const campRemaining = remainingMobilizationCapacity[territoryId] ?? 0;
          const homeRemaining = remainingHomeSlots[territoryId]?.[selectedMobilizationUnit] ?? 0;
          const maxCount = campRemaining > 0
            ? Math.min(purchase.count, campRemaining)
            : homeRemaining > 0
              ? Math.min(purchase.count, 1)
              : 0;
          if (maxCount <= 0) return;
          setPendingMobilization({
            unitId: selectedMobilizationUnit,
            unitName: purchase.name,
            unitIcon: purchase.icon,
            toTerritory: territoryId,
            maxCount,
            count: Math.min(purchase.count, maxCount),
          });
          setSelectedMobilizationUnit(null);
          return;
        }
      }
    }
    setSelectedTerritory(territoryId);
    // Naval tray opens only when boat stack is clicked (via onSeaZoneStackClick), not on territory click
    setSelectedSeaZoneForNavalTray(null);
  }, [gameState.phase, selectedCampIndex, validCampTerritories, selectedMobilizationUnit, mobilizablePurchases, validMobilizeTerritories, validMobilizeSeaZones, navalUnitIds, remainingMobilizationCapacity, remainingHomeSlots, addLogEntry, refreshState]);

  /** Click on boat stack in a sea zone (movement phases): open naval tray for that sea zone. */
  const handleSeaZoneStackClick = useCallback((territoryId: string) => {
    setSelectedSeaZoneForNavalTray(territoryId);
    setSelectedTerritory(territoryId);
  }, []);

  /** When pending load into a sea zone with multiple boats, open naval tray (choose boat when different makeups; view/rearrange when same). */
  useEffect(() => {
    const to = typeof pendingMoveConfirm?.toTerritory === 'string' ? pendingMoveConfirm.toTerritory : '';
    if (!to || !pendingMoveConfirm) return;
    const terrain = currentTerritoryData[to]?.terrain;
    const isSea = terrain === 'sea' || /^sea_zone_?\d+$/i.test(to);
    if (!isSea) return;
    const fromTerrain = currentTerritoryData[typeof pendingMoveConfirm.fromTerritory === 'string' ? pendingMoveConfirm.fromTerritory : '']?.terrain;
    const fromSea = fromTerrain === 'sea' || /^sea_zone_?\d+$/i.test(String(pendingMoveConfirm.fromTerritory ?? ''));
    const uid = pendingMoveConfirm.unitId;
    const ud = uid ? unitDefs[uid] : undefined;
    const isAerial =
      ud?.archetype === 'aerial' || (Array.isArray(ud?.tags) && ud.tags.includes('aerial'));
    const isLoad = !fromSea && isSea && !isAerial;
    if (!isLoad) return;
    const boatsInZone = (territoryUnitsFull[to] ?? []).filter((u) => navalUnitIds.has(u.unit_id));
    if (boatsInZone.length >= 2) setSelectedSeaZoneForNavalTray(to);
  }, [pendingMoveConfirm?.toTerritory, pendingMoveConfirm?.fromTerritory, pendingMoveConfirm?.unitId, pendingMoveConfirm, currentTerritoryData, territoryUnitsFull, navalUnitIds, unitDefs]);

  /** Drop camp on territory → set pending; user confirms or cancels in sidebar (like unit mobilization). */
  const handleCampDrop = useCallback((campIndex: number, territoryId: string) => {
    setPendingCampPlacement({ campIndex, territoryId });
  }, []);

  const handleConfirmCampPlacement = useCallback(async () => {
    if (!pendingCampPlacement) return;
    const { campIndex, territoryId } = pendingCampPlacement;
    try {
      const result = await api.queueCampPlacement(GAME_ID, campIndex, territoryId);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events?.length) addBackendEvents(result.events);
      setPendingCampPlacement(null);
      setSelectedCampIndex(null);
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(err instanceof Error ? err.message : 'Camp placement failed', 'error');
    }
  }, [pendingCampPlacement, addLogEntry, addBackendEvents]);

  const handleCancelCampPlacement = useCallback(() => {
    setPendingCampPlacement(null);
  }, []);

  const handleCancelQueuedCampPlacement = useCallback(async (placementIndex: number) => {
    try {
      await api.cancelCampPlacement(GAME_ID, placementIndex);
      await refreshState();
    } catch (err) {
      addLogEntry(err instanceof Error ? err.message : 'Cancel camp placement failed', 'error');
    }
  }, [addLogEntry, refreshState]);

  const handleSetTerritoryDefenderCasualtyOrder = useCallback(async (territoryId: string, casualtyOrder: 'best_unit' | 'best_defense') => {
    try {
      const res = await api.setTerritoryDefenderCasualtyOrder(GAME_ID, territoryId, casualtyOrder);
      setBackendState(res.state);
      if (res.can_act !== undefined) setCanAct(res.can_act);
    } catch (err) {
      addLogEntry(err instanceof Error ? err.message : 'Failed to set defender casualty order', 'error');
    }
  }, [addLogEntry]);

  const handleUnitSelect = useCallback((unit: SelectedUnit | null) => {
    setSelectedUnit(unit);
  }, []);

  // Max camps = non-camp territories owned by current faction (each camp must be placed on a distinct such territory)
  const maxCampsPurchasable = useMemo(() => {
    if (gameState.phase !== 'purchase') return 0;
    return Object.values(currentTerritoryData).filter(
      (t) => t.owner === gameState.current_faction && !t.hasCamp
    ).length;
  }, [gameState.phase, gameState.current_faction, currentTerritoryData]);

  // Strongholds owned by current faction that have current_hp < base (repairable in purchase phase)
  const repairableStrongholds = useMemo(() => {
    if (gameState.phase !== 'purchase' || !gameState.current_faction) return [];
    return Object.entries(currentTerritoryData)
      .filter(
        ([, t]) =>
          t.owner === gameState.current_faction &&
          t.stronghold &&
          (t.stronghold_base_health ?? 0) > 0 &&
          (t.stronghold_current_health ?? t.stronghold_base_health ?? 0) < (t.stronghold_base_health ?? 0)
      )
      .map(([territoryId, t]) => ({
        territoryId,
        name: t.name,
        currentHp: t.stronghold_current_health ?? t.stronghold_base_health ?? 0,
        baseHp: t.stronghold_base_health ?? 0,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [gameState.phase, gameState.current_faction, currentTerritoryData]);

  /** Per-HP repair cost from manifest (persisted on game state). Prefer max(actions, state) so a missing actions field still picks up state. */
  const strongholdRepairCostPerHp = useMemo(() => {
    const parse = (v: unknown) => {
      const n = typeof v === 'number' ? v : Number(v);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    };
    const n = Math.max(
      parse(availableActions?.stronghold_repair_cost),
      parse(backendState?.stronghold_repair_cost)
    );
    return n > 0 ? n : 0;
  }, [availableActions?.stronghold_repair_cost, backendState?.stronghold_repair_cost]);

  const hasPurchaseCart =
    Object.values(purchaseCart).some(qty => qty > 0) || purchaseCampsCount > 0 || purchaseRepairs.length > 0;

  const aerialMustMove = availableActions?.aerial_units_must_move ?? [];

  const endPhaseDisabled =
    (gameState.phase === 'combat' && gameState.declared_battles.length > 0) ||
    (gameState.phase === 'mobilize' && mobilizablePurchases.length > 0) ||
    (gameState.phase === 'non_combat_move' && availableActions?.can_end_phase === false) ||
    (gameState.phase === 'combat_move' && availableActions?.can_end_phase === false);
  const endPhaseDisabledReason =
    gameState.phase === 'combat'
      ? 'Resolve all battles before ending combat phase'
      : gameState.phase === 'mobilize'
        ? (mobilizablePurchases.length > 0 ? 'Deploy all purchased units before ending mobilization phase' : unplacedCamps.length > 0 ? 'Place all camps first (or click End phase to sync)' : undefined)
        : gameState.phase === 'non_combat_move' && aerialMustMove.length > 0
          ? 'Move all aerial units to friendly territory before ending phase'
          : gameState.phase === 'combat_move' && availableActions?.can_end_phase === false
            ? 'Sea zones that received a load must attack (naval combat or sea raid) before ending phase'
            : undefined;

  const handleEndPhase = useCallback(async () => {
    if (gameState.phase === 'purchase' && !pendingEndPhaseConfirm) {
      if (!hasPurchaseCart) {
        setPendingEndPhaseConfirm('purchase');
        return;
      }
    }

    if (gameState.phase === 'combat_move' && !hasCombatMovedThisPhase && !pendingEndPhaseConfirm) {
      setPendingEndPhaseConfirm('combat_move');
      return;
    }

    if (gameState.phase === 'non_combat_move' && !hasNonCombatMovedThisPhase && !pendingEndPhaseConfirm) {
      setPendingEndPhaseConfirm('non_combat_move');
      return;
    }

    // Aerial check: server uses state-after-pending-moves (can_end_phase). Don't block here on aerialMustMove.length
    // or we'd block even when pending moves satisfy the requirement.

    // Prevent ending combat phase with unresolved battles
    if (gameState.phase === 'combat' && gameState.declared_battles.length > 0) {
      addLogEntry('Cannot end combat phase - unresolved battles remain!', 'error');
      return;
    }

    // Prevent ending mobilization if purchases exist but aren't mobilized
    const hasUnmobilizedPurchases = mobilizablePurchases.length > 0;
    if (gameState.phase === 'mobilize' && hasUnmobilizedPurchases) {
      addLogEntry('Cannot end mobilization - units still need to be deployed!', 'error');
      return;
    }

    setPendingEndPhaseConfirm(null);

    try {
      if (gameState.phase === 'purchase' && hasPurchaseCart) {
        const hasUnitPurchases = Object.values(purchaseCart).some(q => q > 0);
        if (hasUnitPurchases) {
          const purchaseResult = await api.purchase(GAME_ID, purchaseCart);
          setBackendState(purchaseResult.state);
          if (purchaseResult.can_act !== undefined) setCanAct(purchaseResult.can_act);
          if (purchaseResult.events) addBackendEvents(purchaseResult.events);
          setPurchaseCart({});
        }
        for (let i = 0; i < purchaseCampsCount; i++) {
          const campResult = await api.purchaseCamp(GAME_ID);
          setBackendState(campResult.state);
          if (campResult.can_act !== undefined) setCanAct(campResult.can_act);
          if (campResult.events) addBackendEvents(campResult.events);
        }
        setPurchaseCampsCount(0);
        if (purchaseRepairs.length > 0) {
          const repairResult = await api.repairStronghold(GAME_ID, purchaseRepairs);
          setBackendState(repairResult.state);
          if (repairResult.can_act !== undefined) setCanAct(repairResult.can_act);
          if (repairResult.events) addBackendEvents(repairResult.events);
          setPurchaseRepairs([]);
        }
      }

      const result = await api.endPhase(GAME_ID);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      // Refetch full state so pending_camps and faction_purchased_units are authoritative (avoids stuck mobilization)
      await refreshState();

      setHasCombatMovedThisPhase(false);
      setHasNonCombatMovedThisPhase(false);
      setBattlesCompletedThisPhase(0);

      setSelectedTerritory(null);
      setSelectedUnit(null);
    } catch (err) {
      addLogEntry(`Failed to end phase: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      // Refetch so we sync pending_camps / units; may unstick mobilization if state was missing camps
      await refreshState();
    }
  }, [gameState.phase, gameState.declared_battles, hasPurchaseCart, purchaseCart, purchaseCampsCount, purchaseRepairs, hasCombatMovedThisPhase, hasNonCombatMovedThisPhase, pendingEndPhaseConfirm, mobilizablePurchases, aerialMustMove, addLogEntry, addBackendEvents, refreshState]);

  const handleConfirmEndPhase = useCallback(() => {
    setPendingEndPhaseConfirm(null);
    handleEndPhase();
  }, [handleEndPhase]);

  const handleCancelEndPhase = useCallback(() => {
    setPendingEndPhaseConfirm(null);
  }, []);

  const handleOpenPurchase = useCallback(() => {
    if (backendState?.winner) return;
    setIsPurchaseModalOpen(true);
  }, [backendState?.winner]);

  const handleClosePurchase = useCallback(() => {
    setIsPurchaseModalOpen(false);
  }, []);

  /** Confirm = save cart only; resources stay unchanged until End phase */
  const handlePurchase = useCallback((
    purchases: Record<string, number>,
    campsCount: number = 0,
    repairs: { territory_id: string; hp_to_add: number }[] = []
  ) => {
    setPurchaseCart(purchases);
    setPurchaseCampsCount(campsCount);
    setPurchaseRepairs(repairs);
    setIsPurchaseModalOpen(false);
  }, []);

  const handleUpdateMoveCount = useCallback((count: number) => {
    setPendingMoveConfirm((prev) => {
      if (!prev) return null;
      const next = { ...prev, count };
      if (prev.boatOptions && prev.boatOptions.length >= 2) {
        loadAllocationRef.current = null;
        return { ...next, loadAllocation: undefined };
      }
      return next;
    });
  }, []);

  const handleCancelMove = useCallback(() => {
    setPendingMoveConfirm(null);
  }, []);

  const handleChooseChargePath = useCallback((path: string[]) => {
    setPendingMoveConfirm(prev => prev ? { ...prev, chargeThrough: path, chargePathOptions: undefined } : null);
  }, []);

  const handleChooseBoat = useCallback((option: string[]) => {
    // Land→sea load: option = [boatInstanceId, ...passengerInstanceIds]; passengers move, boat via load_onto_boat.
    // Sea→sea / sea→land: move the whole stack (boat + embarked); do not set load_onto_boat (that is for load only).
    const boatId = option[0];
    const passengerIds = option.slice(1);
    setPendingMoveConfirm((prev) => {
      if (!prev) return null;
      const fromStr = typeof prev.fromTerritory === 'string' ? prev.fromTerritory.trim() : '';
      const fromSea =
        Boolean(fromStr) &&
        (currentTerritoryData[fromStr]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(fromStr));
      if (fromSea) {
        return {
          ...prev,
          instanceIds: [...option],
          loadOntoBoatInstanceId: undefined,
          boatOptions: undefined,
          navalBoatStacks: undefined,
        };
      }
      return {
        ...prev,
        instanceIds: passengerIds.length > 0 ? passengerIds : prev.instanceIds,
        loadOntoBoatInstanceId: boatId,
        boatOptions: undefined,
        navalBoatStacks: undefined,
      };
    });
  }, [currentTerritoryData]);

  const handleRequestSeaRaidZoneChoice = useCallback(() => {
    setPendingMoveConfirm(prev => prev ? { ...prev, seaRaidAwaitingZoneChoice: true } : null);
  }, []);

  const handleUnitMove = useCallback((_fromTerritory: string, _toTerritory: string, _unitType: string, _count: number) => {
    // Moves are now handled through handleConfirmMove
  }, []);

  const handleConfirmMove = useCallback(async (overrideToTerritory?: string) => {
    if (!pendingMoveConfirm || !backendState) return;

    const { fromTerritory, count, instanceIds, maxCount: pendingMaxCount } = pendingMoveConfirm;
    const toId = (v: unknown): string =>
      typeof v === 'string' ? v : (v != null && typeof v === 'object' && 'id' in (v as object) ? String((v as { id: string }).id) : (v != null && typeof v === 'object' && 'territoryId' in (v as object) ? String((v as { territoryId: string }).territoryId) : String(v ?? '')));
    const fromStr = toId(fromTerritory);
    const fromSea = currentTerritoryData[fromStr]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(fromStr);

    // Destination = drop location. Same source as panel display; toId() handles string or { id/territoryId }.
    const stateTo = toId(pendingMoveConfirm.toTerritory).trim();
    const valid = (s: string) => s && s !== '[object Object]';
    const storedTo = valid(stateTo) ? stateTo : '';

    // Override when user picked a sea zone (from options panel or passed to handleConfirmMove).
    const effectiveOverride = overrideToTerritory ?? pendingMoveConfirm?.chosenSeaZoneId;
    const overrideStr =
      effectiveOverride != null && effectiveOverride !== ''
        ? (typeof effectiveOverride === 'string' ? effectiveOverride : toId(effectiveOverride)).trim()
        : '';
    const toLand =
      storedTo !== '' &&
      currentTerritoryData[storedTo]?.terrain !== 'sea' &&
      !/^sea_zone_?\d+$/i.test(storedTo);

    let unitInstances: string[];
    /** Sea raid/offload: one "move" = boat + passengers; maxCount is 1 but instanceIds lists every piece. */
    let instanceIdsAreAtomicGroup = false;
    const navalStacks = pendingMoveConfirm.navalBoatStacks;
    if (navalStacks && navalStacks.length >= 2) {
      const n = Math.min(Math.max(0, count), navalStacks.length);
      if (n < 1) {
        addLogEntry('Move failed: choose at least one ship', 'error');
        return;
      }
      unitInstances = navalStacks.slice(0, n).flat();
      instanceIdsAreAtomicGroup = true;
    } else if (instanceIds && instanceIds.length > 0) {
      instanceIdsAreAtomicGroup =
        typeof pendingMaxCount === 'number' && pendingMaxCount === 1 && instanceIds.length > 1;
      unitInstances = instanceIdsAreAtomicGroup
        ? [...instanceIds]
        : instanceIds.slice(0, Math.min(Math.max(0, count), instanceIds.length));
    } else {
      const territory = backendState.territories[fromStr];
      if (!territory) return;
      const currentPhase = gameState.phase;
      const committedInstanceIds = new Set(
        (backendState.pending_moves || [])
          .filter((m: { from_territory: string; phase: string }) => m.from_territory === fromStr && m.phase === currentPhase)
          .flatMap((m: { unit_instance_ids: string[] }) => m.unit_instance_ids)
      );
      unitInstances = territory.units
        .filter(u => u.unit_id === pendingMoveConfirm.unitId && !committedInstanceIds.has(u.instance_id))
        .slice(0, count)
        .map(u => u.instance_id);
    }

    if (!instanceIdsAreAtomicGroup && unitInstances.length < count) {
      addLogEntry('Not enough units available (some already in other moves)', 'error');
      return;
    }

    // Use only primitives so JSON.stringify in api.move never sees cyclic refs from state/drag data
    const unitInstanceIds: string[] = Array.from(unitInstances, (id: unknown) =>
      typeof id === 'string' ? id : (id != null && typeof id === 'object' && 'instance_id' in id ? String((id as { instance_id: unknown }).instance_id) : '')
    ).filter(Boolean);

    const unitDefsForAerial = definitions?.units as
      | Record<string, { archetype?: string; tags?: string[] } | undefined>
      | undefined;
    const allMovingAreAerial =
      fromSea &&
      toLand &&
      unitInstanceIds.length > 0 &&
      (() => {
        const terr = backendState.territories[fromStr];
        if (!terr?.units) return false;
        const idSet = new Set(unitInstanceIds);
        const moving = terr.units.filter((u) => idSet.has(u.instance_id));
        if (moving.length !== unitInstanceIds.length) return false;
        return moving.every((u) => unitIsAerial(u.unit_id, unitDefsForAerial ?? {}));
      })();

    const isSeaRaid = gameState.phase === 'combat_move' && fromSea && toLand && !allMovingAreAerial;
    const isOffload = gameState.phase === 'non_combat_move' && fromSea && toLand && !allMovingAreAerial;
    const singleSeaRaidZone =
      (isSeaRaid || isOffload) && pendingMoveConfirm.seaRaidSeaZoneOptions?.length === 1
        ? (pendingMoveConfirm.seaRaidSeaZoneOptions[0] || '').trim()
        : '';

    // Destination: drop target or chosen sea zone. For offload/sea raid, when the (single or chosen) zone is the boat's current zone, destination is the land (offload only); otherwise the sea zone (sail then offload).
    const destinationRaw = valid(overrideStr) ? overrideStr : valid(singleSeaRaidZone) ? singleSeaRaidZone : valid(storedTo) ? storedTo : valid(lastDropDestinationRef.current.trim()) ? lastDropDestinationRef.current.trim() : '';
    const destination = ((isSeaRaid || isOffload) && valid(storedTo) && (destinationRaw === fromStr) ? storedTo : destinationRaw).trim();
    if (!destination) {
      addLogEntry('Move failed: No destination specified', 'error');
      return;
    }

    const chargeThrough = Array.isArray(pendingMoveConfirm.chargeThrough)
      ? pendingMoveConfirm.chargeThrough.map((s: unknown) => (typeof s === 'string' ? s : String(s)))
      : undefined;
    // Use ref so we always have the latest allocation (drag updates can be one tick behind the confirm click)
    let loadAllocation = loadAllocationRef.current ?? pendingMoveConfirm.loadAllocation;
    const toSea = Boolean(
      destination &&
      (currentTerritoryData[destination]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(destination)),
    );
    if (
      toSea &&
      !fromSea &&
      pendingMoveConfirm.boatOptions &&
      pendingMoveConfirm.boatOptions.length >= 2 &&
      definitions?.units &&
      backendState?.territories?.[destination]?.units
    ) {
      const hasSplit =
        loadAllocation &&
        Object.values(loadAllocation).some((ids) => Array.isArray(ids) && ids.length > 0);
      if (!hasSplit && unitInstanceIds.length > 0) {
        const full = backendState.territories[destination].units as {
          instance_id: string;
          unit_id: string;
          loaded_onto?: string | null;
        }[];
        const computed = computeInitialLoadAllocation(
          pendingMoveConfirm.boatOptions,
          unitInstanceIds,
          full,
          definitions.units as Record<string, { transport_capacity?: number } | undefined>,
          backendState.pending_moves ?? [],
          gameState.phase,
          destination,
          currentTerritoryData,
        );
        if (computed) {
          loadAllocation = computed;
          loadAllocationRef.current = computed;
        }
      }
    }

    try {
      const moveSfxCat = movementSfxCategoryFromUnitDef(definitions?.units?.[pendingMoveConfirm.unitId]);
      playMovementSfx(moveSfxCat);

      // Sea->land with chosen sea zone: backend does sail + offload in one request.
      const needsSailThenOffload = (isSeaRaid || isOffload) && valid(storedTo) && toSea && destination !== fromStr;
      if (needsSailThenOffload) {
        const avoidForced = shouldSendAvoidForcedNavalCombat(
          gameState.phase,
          fromStr,
          storedTo,
          unitInstanceIds,
          currentTerritoryData,
          backendState.territories,
          definitions?.units,
          availableActions?.forced_naval_combat_instance_ids,
        );
        const result = await api.move(
          String(GAME_ID),
          fromStr,
          storedTo,
          unitInstanceIds,
          chargeThrough,
          undefined,
          destination,
          avoidForced,
        );
        if (result.need_offload_sea_choice && result.valid_offload_sea_zones?.length) {
          setPendingOffloadSeaChoice({
            from: fromStr,
            to: storedTo,
            unitInstanceIds,
            validSeaZones: sortSeaZoneIdsByNumericSuffix([...result.valid_offload_sea_zones]),
          });
          setBackendState(result.state);
          if (result.can_act !== undefined) setCanAct(result.can_act);
        } else {
          setBackendState(result.state);
          if (result.can_act !== undefined) setCanAct(result.can_act);
          if (result.events) addBackendEvents(result.events);
          if (gameState.phase === 'combat_move') setHasCombatMovedThisPhase(true);
          else if (gameState.phase === 'non_combat_move') setHasNonCombatMovedThisPhase(true);
          setPendingMoveConfirm(null);
          setSelectedUnit(null);
          const actionsRes = await api.getAvailableActions(GAME_ID);
          setAvailableActions(actionsRes);
        }
        return;
      }

      // Load into 2+ boats with user allocation: submit one move per boat with load_onto_boat_instance_id
      if (loadAllocation && toSea && !fromSea) {
        const boatsWithUnits = Object.entries(loadAllocation).filter(([, ids]) => ids.length > 0);
        if (boatsWithUnits.length === 0) {
          addLogEntry('No units allocated to any boat', 'error');
          return;
        }
        for (const [boatInstanceId, ids] of boatsWithUnits) {
          const result = await api.move(
            String(GAME_ID),
            fromStr,
            destination,
            ids,
            undefined,
            boatInstanceId,
            undefined,
            false,
          );
          setBackendState(result.state);
          if (result.can_act !== undefined) setCanAct(result.can_act);
          if (result.events) addBackendEvents(result.events);
        }
        if (gameState.phase === 'combat_move') setHasCombatMovedThisPhase(true);
        else if (gameState.phase === 'non_combat_move') setHasNonCombatMovedThisPhase(true);
        loadAllocationRef.current = null;
        setPendingMoveConfirm(null);
        setSelectedUnit(null);
        const actionsRes = await api.getAvailableActions(GAME_ID);
        setAvailableActions(actionsRes);
        return;
      }

      const loadOntoBoatId =
        toSea && !fromSea && pendingMoveConfirm.loadOntoBoatInstanceId
          ? pendingMoveConfirm.loadOntoBoatInstanceId
          : undefined;
      const avoidForcedMain = shouldSendAvoidForcedNavalCombat(
        gameState.phase,
        fromStr,
        destination,
        unitInstanceIds,
        currentTerritoryData,
        backendState.territories,
        definitions?.units,
        availableActions?.forced_naval_combat_instance_ids,
      );
      const result = await api.move(
        String(GAME_ID),
        fromStr,
        destination,
        unitInstanceIds,
        chargeThrough,
        loadOntoBoatId,
        undefined,
        avoidForcedMain,
      );
      if (result.need_offload_sea_choice && result.valid_offload_sea_zones?.length) {
        setPendingOffloadSeaChoice({
          from: fromStr,
          to: destination,
          unitInstanceIds,
          validSeaZones: sortSeaZoneIdsByNumericSuffix([...result.valid_offload_sea_zones]),
        });
        setBackendState(result.state);
        if (result.can_act !== undefined) setCanAct(result.can_act);
        setPendingMoveConfirm(null);
      } else {
        setBackendState(result.state);
        if (result.can_act !== undefined) setCanAct(result.can_act);
        if (result.events) addBackendEvents(result.events);
        if (gameState.phase === 'combat_move') setHasCombatMovedThisPhase(true);
        else if (gameState.phase === 'non_combat_move') setHasNonCombatMovedThisPhase(true);
        setPendingMoveConfirm(null);
        setSelectedUnit(null);
        const actionsRes = await api.getAvailableActions(GAME_ID);
        setAvailableActions(actionsRes);
      }
    } catch (err) {
      addLogEntry(`Move failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [pendingMoveConfirm, backendState, gameState.phase, currentTerritoryData, availableActions, definitions, addLogEntry, addBackendEvents]);

  const handleBulkMoveDrop = useCallback(
    (fromTerritory: string, toTerritory: string) => {
      if (!backendState || !availableActions) {
        addLogEntry('Cannot plan bulk move right now.', 'error');
        return;
      }
      const territories = backendState.territories ?? {};
      const resolveFromKey = (tid: string): string => {
        if (territories[tid]) return tid;
        const c = canonicalSeaZoneId(tid);
        if (territories[c]) return c;
        return tid;
      };
      const fromKey = resolveFromKey(fromTerritory);
      const fromTerr = territories[fromKey];
      if (!fromTerr?.units?.length) {
        addLogEntry('No units in that territory to move.', 'error');
        return;
      }

      const moveables = availableActions.moveable_units ?? [];
      const stacks = buildBulkMoveStacks(
        backendState,
        moveables,
        gameState.phase,
        fromTerritory,
        toTerritory,
        definitions,
      );
      if (stacks.length === 0) {
        addLogEntry('No stacks can move to that destination from here.', 'error');
        return;
      }

      setPendingMoveConfirm(null);
      setBulkMoveConfirm({
        fromTerritory: fromKey,
        toTerritory: toTerritory.trim(),
        stacks,
      });
    },
    [backendState, availableActions, gameState.phase, definitions, addLogEntry, setPendingMoveConfirm],
  );

  const handleCancelBulkMove = useCallback(() => {
    setBulkMoveConfirm(null);
  }, []);

  const handleConfirmBulkMove = useCallback(async () => {
    if (!bulkMoveConfirm || !GAME_ID || !backendState) return;
    const { fromTerritory, stacks } = bulkMoveConfirm;
    const cats = new Set(
      stacks.map((s) => movementSfxCategoryFromUnitDef(definitions?.units?.[s.unitId])),
    );
    const bulkSfx = cats.has('naval') ? 'naval' : cats.has('aerial') ? 'aerial' : 'ground';
    playMovementSfx(bulkSfx);

    setBulkMoveConfirm(null);

    const fordSubmitOrder = [...stacks].sort((a, b) => {
      const ac = isFordCrosser(unitDefFordFields(definitions?.units?.[a.unitId]));
      const bc = isFordCrosser(unitDefFordFields(definitions?.units?.[b.unitId]));
      if (ac === bc) return 0;
      return ac ? -1 : 1;
    });

    for (const stack of fordSubmitOrder) {
      try {
        const res = await api.move(GAME_ID, fromTerritory, stack.destForApi, stack.instanceIds, stack.chargeThrough);
        setBackendState(res.state);
        if (res.can_act !== undefined) setCanAct(res.can_act);
        if (res.events?.length) addBackendEvents(res.events);
        if (gameState.phase === 'combat_move') setHasCombatMovedThisPhase(true);
        else if (gameState.phase === 'non_combat_move') setHasNonCombatMovedThisPhase(true);
      } catch (err) {
        addLogEntry(err instanceof Error ? err.message : 'Bulk move failed', 'error');
        break;
      }
    }
    setSelectedUnit(null);
    try {
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch {
      /* ignore refresh failure */
    }
  }, [bulkMoveConfirm, GAME_ID, backendState, gameState.phase, definitions, addBackendEvents, addLogEntry]);

  const handleChooseSeaRaidSeaZone = useCallback((seaZoneId: string) => {
    setPendingMoveConfirm(prev => prev ? { ...prev, chosenSeaZoneId: seaZoneId, seaRaidSeaZoneOptions: undefined } : null);
  }, []);

  const handleCancelOffloadSeaChoice = useCallback(() => {
    setPendingOffloadSeaChoice(null);
  }, []);

  const handleChooseOffloadSeaZone = useCallback(async (chosenSeaZoneId: string) => {
    if (!pendingOffloadSeaChoice || !chosenSeaZoneId) return;
    const { from, to, unitInstanceIds: ids } = pendingOffloadSeaChoice;
    setPendingOffloadSeaChoice(null);
    try {
      const avoidForcedOffload = shouldSendAvoidForcedNavalCombat(
        backendState?.phase ?? '',
        from,
        chosenSeaZoneId,
        ids,
        currentTerritoryData,
        backendState?.territories,
        definitions?.units,
        availableActions?.forced_naval_combat_instance_ids,
      );
      const result = await api.move(
        String(GAME_ID),
        from,
        to,
        ids,
        undefined,
        undefined,
        chosenSeaZoneId,
        avoidForcedOffload,
      );
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);
      if (backendState?.phase === 'combat_move') setHasCombatMovedThisPhase(true);
      else if (backendState?.phase === 'non_combat_move') setHasNonCombatMovedThisPhase(true);
      setPendingMoveConfirm(null);
      setSelectedUnit(null);
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Move failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [
    pendingOffloadSeaChoice,
    backendState?.phase,
    backendState?.territories,
    GAME_ID,
    currentTerritoryData,
    definitions?.units,
    availableActions?.forced_naval_combat_instance_ids,
    addLogEntry,
    addBackendEvents,
  ]);

  const handleCancelPendingMove = useCallback(async (moveId: string) => {
    // Extract index from move ID (format: "move_N")
    const moveIndex = parseInt(moveId.replace('move_', ''), 10);
    if (isNaN(moveIndex)) {
      addLogEntry('Invalid move ID', 'error');
      return;
    }

    try {
      const result = await api.cancelMove(GAME_ID, moveIndex);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Failed to cancel move: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [addLogEntry, addBackendEvents]);

  const handleCancelMobilization = useCallback(async (mobilizationIndex: number) => {
    try {
      const result = await api.cancelMobilization(GAME_ID, mobilizationIndex);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Failed to cancel mobilization: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [addLogEntry, addBackendEvents]);

  // Clicking a battle (red button) only opens the combat modal; no backend call until "Start" is pressed
  const handleOpenCombat = useCallback((battle: DeclaredBattle) => {
    setActiveCombat(battle);
  }, []);

  /** Start a combat round via the backend (rolls dice, applies round, clears active_combat when combat ends). */
  /** Start round 1 (initiate) or next round (continue). Called when user clicks "Start" or "Continue" in combat modal. */
  const handleStartCombatRound = useCallback(async (casualtyOrder?: string, mustConquer?: boolean, fuseBomb?: boolean): Promise<{
    round: CombatRound;
    combatOver: boolean;
    attackerWon: boolean;
    defenderWon: boolean;
    terrorReroll?: { applied: boolean; terror_reroll_count?: number; instance_ids?: string[]; initial_rolls_by_instance?: Record<string, number[]>; defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>; defender_rerolled_indices_by_stat?: Record<string, number[]> };
  } | null> => {
    if (!activeCombat) return null;
    const isFirstRound = !backendState?.active_combat;
    try {
      const res = isFirstRound
        ? await api.initiateCombat(GAME_ID, activeCombat.territory, activeCombat.sea_zone_id, {
          fuse_bomb: fuseBomb !== false,
        })
        : await api.continueCombat(GAME_ID, { casualty_order: casualtyOrder, must_conquer: mustConquer });
      setBackendState(res.state);
      if (res.can_act !== undefined) setCanAct(res.can_act);
      if (res.events) addBackendEvents(res.events);

      // Refetch available-actions so retreat_options.valid_destinations is present when user chooses retreat
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);

      const roundEvents = (res.events || []).filter((e: { type: string }) => e.type === 'combat_round_resolved');
      // Backend may append several combat_round_resolved in one response (e.g. old initiate: siegework
      // then round 1). All use round_number 0 except normal combat. Preferring round_number === 1
      // skipped siegework entirely. Events are in chronological order — show the first round this action resolved.
      const roundEvent = roundEvents.length === 0 ? undefined : roundEvents[0];
      const endEvent = res.events?.find((e: { type: string }) => e.type === 'combat_ended');
      if (!roundEvent?.payload) return null;

      // Backend combat_round_resolved is the full UI contract: dice, hits, casualties, and units at round start.
      const p = roundEvent.payload as {
        round_number: number;
        attacker_dice: Record<string, { rolls: number[]; hits: number }>;
        defender_dice: Record<string, { rolls: number[]; hits: number }>;
        attacker_hits: number;
        defender_hits: number;
        attacker_casualties: string[];
        defender_casualties: string[];
        attacker_wounded?: string[];
        defender_wounded?: string[];
        attacker_hits_by_unit_type?: Record<string, number>;
        defender_hits_by_unit_type?: Record<string, number>;
        is_archer_prefire?: boolean;
        is_stealth_prefire?: boolean;
        is_siegeworks_round?: boolean;
        terror_applied?: boolean;
        terror_reroll_count?: number;
        attacker_dice_siegework_split?: Record<
          string,
          { ram?: { rolls?: number[]; hits?: number }; flex?: { rolls?: number[]; hits?: number } }
        >;
        attacker_units_at_start: BackendCombatUnit[];
        defender_units_at_start: BackendCombatUnit[];
      };

      const toDefenderRolls = (diceByStat: Record<string, { rolls: number[]; hits: number }>) => {
        const out: Record<number, { value: number; target: number; isHit: boolean }[]> = {};
        for (const [statStr, data] of Object.entries(diceByStat || {})) {
          const stat = Number(statStr);
          out[stat] = (data.rolls || []).map((value: number) => ({
            value,
            target: stat,
            isHit: value <= stat,
          }));
        }
        return out;
      };

      type AttackerDicePayload = {
        rolls?: number[];
        segments?: Array<{
          rolls: number[];
          hits?: number;
          on_ladder?: boolean;
          unit_type?: string;
          unit_count?: number;
        }>;
      };
      const toAttackerRolls = (diceByStat: Record<string, AttackerDicePayload>) => {
        const swSplit = p.attacker_dice_siegework_split;
        if (swSplit && Object.keys(swSplit).length > 0) {
          const out: Record<
            number,
            | { mode: 'flat'; rolls: { value: number; target: number; isHit: boolean }[] }
            | {
              mode: 'ladder';
              segments: Array<{
                rolls: { value: number; target: number; isHit: boolean }[];
                onLadder: boolean;
                unitType: string;
                unitCount: number;
              }>;
            }
            | {
              mode: 'siegework_ram_flex';
              ram: { rolls: { value: number; target: number; isHit: boolean }[] };
              flex: { rolls: { value: number; target: number; isHit: boolean }[] };
            }
          > = {};
          for (const [statStr, buckets] of Object.entries(swSplit)) {
            const stat = Number(statStr);
            const mapRolls = (raw: number[] | undefined) =>
              (raw ?? []).map((value: number) => ({
                value,
                target: stat,
                isHit: value <= stat,
              }));
            out[stat] = {
              mode: 'siegework_ram_flex',
              ram: { rolls: mapRolls(buckets.ram?.rolls) },
              flex: { rolls: mapRolls(buckets.flex?.rolls) },
            };
          }
          return out;
        }
        const out: Record<
          number,
          | { mode: 'flat'; rolls: { value: number; target: number; isHit: boolean }[] }
          | {
            mode: 'ladder';
            segments: Array<{
              rolls: { value: number; target: number; isHit: boolean }[];
              onLadder: boolean;
              unitType: string;
              unitCount: number;
            }>;
          }
        > = {};
        for (const [statStr, data] of Object.entries(diceByStat || {})) {
          const stat = Number(statStr);
          const segs = data.segments;
          if (Array.isArray(segs) && segs.length > 0) {
            out[stat] = {
              mode: 'ladder',
              segments: segs.map((s) => ({
                rolls: (s.rolls || []).map((value: number) => ({
                  value,
                  target: stat,
                  isHit: value <= stat,
                })),
                onLadder: !!s.on_ladder,
                unitType: String(s.unit_type || ''),
                unitCount: typeof s.unit_count === 'number' ? s.unit_count : (s.rolls?.length ?? 0),
              })),
            };
          } else {
            out[stat] = {
              mode: 'flat',
              rolls: (data.rolls || []).map((value: number) => ({
                value,
                target: stat,
                isHit: value <= stat,
              })),
            };
          }
        }
        return out;
      };

      const backendUnitToCombatUnit = (bu: BackendCombatUnit): CombatUnit => ({
        id: bu.instance_id,
        unitType: bu.unit_id,
        name: bu.display_name,
        icon: unitDefs[bu.unit_id]?.icon ?? `/assets/units/${bu.unit_id}.png`,
        attack: bu.attack,
        defense: bu.defense,
        // Round snapshot: effective_* matches dice grouping (captain, terrain); fall back to base so shelves never drift from roll buckets.
        effectiveAttack: bu.effective_attack ?? bu.attack,
        effectiveDefense: bu.effective_defense ?? bu.defense,
        isArcher: bu.is_archer ?? false,
        health: bu.health,
        remainingHealth: bu.remaining_health,
        ...(bu.remaining_movement != null && { remainingMovement: bu.remaining_movement }),
        ...(bu.faction && { factionId: bu.faction }),
        ...(factionData[bu.faction] && { factionColor: factionData[bu.faction].color }),
        ...(bu.terror && { hasTerror: true }),
        ...(bu.terrain_mountain && { terrainMountain: true }),
        ...(bu.terrain_forest && { terrainForest: true }),
        ...(bu.captain_bonus && { hasCaptainBonus: true }),
        ...(bu.anti_cavalry && { hasAntiCavalry: true }),
        ...(bu.sea_raider && { hasSeaRaider: true }),
        ...(bu.archer && { hasArcher: true }),
        ...(bu.stealth && { hasStealth: true }),
        ...(bu.bombikazi && { hasBombikazi: true }),
        ...(bu.fearless && { hasFearless: true }),
        ...(bu.hope && { hasHope: true }),
        ...(bu.ram && { hasRam: true }),
        ...(bu.siegework_archetype && { siegeworkArchetype: true }),
        ...(typeof bu.passenger_count === 'number' && bu.passenger_count > 0
          ? { passengerCount: bu.passenger_count }
          : {}),
      });

      const ladderInfantryIdsRaw = (p as unknown as { ladder_infantry_instance_ids?: unknown })
        .ladder_infantry_instance_ids;

      const round: CombatRound = {
        roundNumber: p.round_number,
        attackerRolls: toAttackerRolls(p.attacker_dice as Record<string, AttackerDicePayload>),
        defenderRolls: toDefenderRolls(p.defender_dice),
        attackerHits: p.attacker_hits ?? 0,
        defenderHits: p.defender_hits ?? 0,
        attackerCasualties: Array.isArray(p.attacker_casualties) ? p.attacker_casualties : [],
        defenderCasualties: Array.isArray(p.defender_casualties) ? p.defender_casualties : [],
        attackerWounded: Array.isArray(p.attacker_wounded) ? p.attacker_wounded : [],
        defenderWounded: Array.isArray(p.defender_wounded) ? p.defender_wounded : [],
        attackerHitsByUnitType: p.attacker_hits_by_unit_type ?? {},
        defenderHitsByUnitType: p.defender_hits_by_unit_type ?? {},
        isArcherPrefire: p.is_archer_prefire ?? false,
        isStealthPrefire: p.is_stealth_prefire ?? false,
        isSiegeworksRound: p.is_siegeworks_round ?? false,
        terrorApplied: p.terror_applied ?? false,
        terrorRerollCount: typeof p.terror_reroll_count === 'number' ? p.terror_reroll_count : undefined,
        attackerUnitsAtStart: (Array.isArray(p.attacker_units_at_start) ? p.attacker_units_at_start : []).map(backendUnitToCombatUnit),
        defenderUnitsAtStart: (Array.isArray(p.defender_units_at_start) ? p.defender_units_at_start : []).map(backendUnitToCombatUnit),
        ...(Array.isArray(ladderInfantryIdsRaw)
          ? { ladderInfantryInstanceIds: ladderInfantryIdsRaw.map(x => String(x)) }
          : {}),
      };

      const combatOver = !res.state.active_combat;
      let attackerWon = false;
      let defenderWon = false;
      const winner =
        endEvent?.payload && typeof (endEvent.payload as { winner?: string }).winner === 'string'
          ? (endEvent.payload as { winner: string }).winner
          : null;
      if (winner === 'attacker') attackerWon = true;
      else if (winner === 'defender') defenderWon = true;
      else if (winner === 'draw') {
        /* mutual destruction / stalemate — both false */
      } else if (combatOver) {
        const ar = (p as { attackers_remaining?: number }).attackers_remaining;
        const dr = (p as { defenders_remaining?: number }).defenders_remaining;
        if (typeof ar === 'number' && typeof dr === 'number') {
          attackerWon = dr === 0 && ar > 0;
          defenderWon = ar === 0 && dr > 0;
        }
      }

      const terrorReroll = res.terror_reroll ?? undefined;

      // Final round: response state has no active_combat, so the persist effect never copies combat_log.
      // Append this round's hits so combat_log-based cumulative totals include the killing round.
      if (combatOver && activeCombat) {
        const seaRaw = activeCombat.sea_zone_id;
        const sea =
          seaRaw != null && String(seaRaw).trim() !== '' ? canonicalSeaZoneId(String(seaRaw)) : '';
        const key = `${canonicalSeaZoneId(activeCombat.territory)}:${sea || '-'}`;
        const hitEntry: Record<string, unknown> = {
          attacker_hits: p.attacker_hits ?? 0,
          defender_hits: p.defender_hits ?? 0,
        };
        const existing = persistedCombatLogRef.current;
        if (existing?.key === key) {
          persistedCombatLogRef.current = { key, log: [...existing.log, hitEntry] };
        } else {
          persistedCombatLogRef.current = { key, log: [hitEntry] };
        }
      }

      return { round, combatOver, attackerWon, defenderWon, terrorReroll };
    } catch (err) {
      addLogEntry(`Combat round failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      return null;
    }
  }, [activeCombat, backendState?.active_combat, addBackendEvents, addLogEntry, unitDefs, factionData]);

  const handleCombatEnd = useCallback(async (result: 'attacker_wins' | 'defender_wins' | 'draw' | 'retreat') => {
    if (!activeCombat) return;

    if (result === 'retreat') {
      // Show retreat selection
      setPendingRetreat(activeCombat);
      setActiveCombat(null);
      return;
    }

    // Close the combat modal when user clicks Close (attacker_wins or defender_wins)
    const territoryName = currentTerritoryData[activeCombat.territory]?.name || activeCombat.territory;

    if (result === 'attacker_wins') {
      addLogEntry(`${factionData[gameState.current_faction]?.name} conquered ${territoryName}!`, 'combat');
    } else if (result === 'defender_wins') {
      addLogEntry(`Attack on ${territoryName} repelled!`, 'combat');
    } else {
      addLogEntry(`Battle at ${territoryName} ended in mutual destruction.`, 'combat');
    }

    setBattlesCompletedThisPhase(prev => prev + 1);
    setActiveCombat(null);

    // Refresh state to get updated territories
    await refreshState();
  }, [activeCombat, currentTerritoryData, factionData, gameState.current_faction, addLogEntry, refreshState]);

  // Get valid retreat destinations from backend
  const validRetreatDestinations = useMemo(() => {
    return availableActions?.retreat_options?.valid_destinations || [];
  }, [availableActions]);

  const handleConfirmRetreat = useCallback(async (destinationId: string) => {
    if (!pendingRetreat) return;

    try {
      const result = await api.retreat(GAME_ID, destinationId);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      setBattlesCompletedThisPhase(prev => prev + 1);
      setPendingRetreat(null);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Retreat failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [pendingRetreat, addLogEntry, addBackendEvents]);

  const handleCancelRetreat = useCallback(() => {
    if (pendingRetreat) {
      setActiveCombat(pendingRetreat);
      setPendingRetreat(null);
    }
  }, [pendingRetreat]);

  // Mobilization handlers
  const handleUpdateMobilizationCount = useCallback((count: number) => {
    setPendingMobilization(prev => prev ? { ...prev, count } : null);
  }, []);

  const handleConfirmMobilization = useCallback(async () => {
    if (!pendingMobilization) return;

    const { unitId, count, toTerritory } = pendingMobilization;

    try {
      const result = await api.mobilize(GAME_ID, toTerritory, [{ unit_id: unitId, count }]);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      const mobCat = movementSfxCategoryFromUnitDef(definitions?.units?.[unitId]);
      playMovementSfx(mobCat);

      setPendingMobilization(null);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Mobilization failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [pendingMobilization, currentTerritoryData, definitions?.units, addLogEntry, addBackendEvents]);

  const handleCloseMobilizationConfirm = useCallback(() => {
    setPendingMobilization(null);
  }, []);

  const handleMobilizationDrop = useCallback((territoryId: string, unitId: string, unitName: string, unitIcon: string, count: number) => {
    const purchase = mobilizablePurchases.find(p => p.unitId === unitId);
    if (!purchase) return;
    const campRemaining = remainingMobilizationCapacity[territoryId] ?? 0;
    const homeRemaining = remainingHomeSlots[territoryId]?.[unitId] ?? 0;
    const maxCount = campRemaining > 0
      ? Math.min(purchase.count, campRemaining)
      : homeRemaining > 0
        ? Math.min(purchase.count, homeRemaining)
        : 0;
    if (maxCount <= 0) return;
    setPendingMobilization({
      unitId,
      unitName,
      unitIcon,
      toTerritory: territoryId,
      maxCount,
      count: Math.min(count, maxCount),
    });
  }, [mobilizablePurchases, remainingMobilizationCapacity, remainingHomeSlots]);

  const handleMobilizationAllDrop = useCallback(
    (
      territoryId: string,
      units: { unitId: string; unitName: string; unitIcon: string; count: number }[]
    ) => {
      if (!territoryId || units.length <= 1) return;
      // Bulk confirm replaces the single-stack mobilization confirm (if any).
      setPendingMobilization(null);
      setSelectedMobilizationUnit(null);
      setBulkMobilizeConfirm({
        toTerritory: territoryId.trim(),
        units: units.map(u => ({
          unitId: u.unitId,
          unitName: u.unitName,
          unitIcon: u.unitIcon,
          count: u.count,
        })),
      });
    },
    []
  );

  const handleConfirmBulkMobilize = useCallback(async () => {
    if (!bulkMobilizeConfirm) return;

    const { toTerritory, units } = bulkMobilizeConfirm;
    if (!toTerritory || !units || units.length <= 1) return;
    const dest = toTerritory.trim();
    try {
      // One API call per unit type so the backend creates separate pending_mobilizations entries.
      // Cancel (×) in the sidebar removes a single index — a single batch would cancel every stack at once.
      for (const u of units) {
        const result = await api.mobilize(GAME_ID, dest, [{ unit_id: u.unitId, count: u.count }]);
        setBackendState(result.state);
        if (result.can_act !== undefined) setCanAct(result.can_act);
        if (result.events) addBackendEvents(result.events);
      }

      const mobCats = new Set(units.map((u) => movementSfxCategoryFromUnitDef(definitions?.units?.[u.unitId])));
      const bulkMobSfx = mobCats.has('naval') ? 'naval' : mobCats.has('aerial') ? 'aerial' : 'ground';
      playMovementSfx(bulkMobSfx);

      setBulkMobilizeConfirm(null);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Mobilization failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [bulkMobilizeConfirm, GAME_ID, currentTerritoryData, definitions?.units, addBackendEvents, addLogEntry]);

  const handleCancelBulkMobilize = useCallback(() => {
    setBulkMobilizeConfirm(null);
  }, []);

  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    resizeStartRef.current = { x: e.clientX, width: sidebarWidth };
  }, [sidebarWidth]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (resizeStartRef.current === null) return;
      const dx = resizeStartRef.current.x - e.clientX;
      const newWidth = Math.min(600, Math.max(260, resizeStartRef.current.width + dx));
      setSidebarWidth(newWidth);
      resizeStartRef.current = { x: e.clientX, width: newWidth };
    };
    const onUp = () => { resizeStartRef.current = null; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  const ac = backendState?.active_combat as
    | {
      territory_id?: string;
      sea_zone_id?: string;
      combat_log?: unknown[];
      attacker_instance_ids?: string[];
      ladder_equipment_count?: number;
    }
    | null
    | undefined;
  const acTerritoryId = ac?.territory_id;
  const acSeaZoneId = ac?.sea_zone_id;
  const acAttackerInstanceIds = ac?.attacker_instance_ids ?? [];

  // Effective combat: active player's open combat, or spectator's chosen battle (any declared battle can be opened to view units)
  const effectiveCombat = useMemo(() => {
    if (activeCombat) return activeCombat;
    return spectatingBattle ?? null;
  }, [activeCombat, spectatingBattle]);

  /** Stable key for the battle currently shown in the combat modal (land + optional sea raid zone). */
  const openBattleKey = useMemo(() => {
    if (!effectiveCombat) return '';
    const seaRaw = effectiveCombat.sea_zone_id;
    const sea =
      seaRaw != null && String(seaRaw).trim() !== '' ? canonicalSeaZoneId(String(seaRaw)) : '';
    return `${canonicalSeaZoneId(effectiveCombat.territory)}:${sea || '-'}`;
  }, [effectiveCombat]);

  const prevOpenBattleKeyRef = useRef<string>('');

  useEffect(() => {
    if (openBattleKey !== prevOpenBattleKeyRef.current) {
      persistedCombatLogRef.current = null;
      prevOpenBattleKeyRef.current = openBattleKey;
    }
  }, [openBattleKey]);

  useEffect(() => {
    if (!openBattleKey || !effectiveCombat || !backendState?.active_combat) return;
    const live = backendState.active_combat as {
      territory_id?: string;
      sea_zone_id?: string | null;
      combat_log?: unknown[];
    };
    if (!battleDisplayedMatchesActiveCombat(effectiveCombat, live)) return;
    const log = live.combat_log;
    if (Array.isArray(log)) {
      persistedCombatLogRef.current = { key: openBattleKey, log };
    }
  }, [openBattleKey, effectiveCombat, backendState?.active_combat]);

  // Combat display props: used for ready phase (modal open, no round run yet) and for faction/retreat.
  // Once a round runs, CombatDisplay uses the round payload (attackerUnitsAtStart/defenderUnitsAtStart) as the single source of truth.
  const combatDisplayProps = useMemo(() => {
    if (!effectiveCombat || !backendState || !definitions) return null;

    const territory = currentTerritoryData[effectiveCombat.territory];
    const attackerFaction = gameState.current_faction;

    const backendTerritory = backendState.territories[effectiveCombat.territory];
    /** Persisted owner only — do not use currentTerritoryData.owner (merges pending_captures). */
    const territoryOwnerRaw = (backendTerritory as { owner?: string | null } | undefined)?.owner;
    const ownerIdNormalized =
      territoryOwnerRaw != null && String(territoryOwnerRaw).trim() !== ''
        ? String(territoryOwnerRaw)
        : '';
    const attackerAlliance =
      (definitions.factions?.[attackerFaction] as { alliance?: string } | undefined)?.alliance ?? '';

    const initialAttackerUnits: CombatUnit[] = [];
    const initialDefenderUnits: CombatUnit[] = [];

    const isActiveCombat = battleDisplayedMatchesActiveCombat(effectiveCombat, ac);

    const combatStatModifiers = (backendState as { combat_stat_modifiers?: { attacker?: Record<string, number>; defender?: Record<string, number> } }).combat_stat_modifiers;
    const combatSpecials = (backendState as {
      combat_specials?: { attacker?: Record<string, CombatSpecialsInstance>; defender?: Record<string, CombatSpecialsInstance> };
    }).combat_specials;
    /** Paired bombikazi use bomb's attack for shelf placement so they show next to the bomb. */
    const combatAttackerEffectiveAttackOverride = (backendState as { combat_attacker_effective_attack_override?: Record<string, number> }).combat_attacker_effective_attack_override ?? {};
    const modsAttacker = combatStatModifiers?.attacker ?? {};
    const modsDefender = combatStatModifiers?.defender ?? {};
    const specialsAttacker = combatSpecials?.attacker ?? {};
    const specialsDefender = combatSpecials?.defender ?? {};

    const isNavalUnitId = (unitId: string) => {
      const def = definitions?.units?.[unitId] as { archetype?: string; tags?: string[] } | undefined;
      if (!def) return false;
      return (def.archetype ?? '') === 'naval' || (def.tags ?? []).includes('naval');
    };

    const isAerialUnitId = (unitId: string) => {
      const def = definitions?.units?.[unitId] as { archetype?: string; tags?: string[] } | undefined;
      if (!def) return false;
      return (def.archetype ?? '') === 'aerial' || (def.tags ?? []).includes('aerial');
    };

    /** Matches engine participates_in_sea_hex_naval_combat: surface naval + aerial in sea hex, not passengers. */
    const unitParticipatesInSeaHexCombat = (unit: { unit_id: string; loaded_onto?: string | null }) => {
      if ((unit as { loaded_onto?: string | null }).loaded_onto) return false;
      return isNavalUnitId(unit.unit_id) || isAerialUnitId(unit.unit_id);
    };

    const buildCombatUnit = (
      unit: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number; loaded_onto?: string | null },
      isAttackerUnit: boolean
    ): CombatUnit | null => {
      const unitDef = definitions.units[unit.unit_id];
      if (!unitDef) return null;
      const tags: string[] = (unitDef as { tags?: string[] }).tags ?? [];
      const archetype = (unitDef as { archetype?: string }).archetype ?? '';
      const specialsList: string[] = (unitDef as { specials?: string[] }).specials ?? [];
      const isArcher = tags.includes('archer') || specialsList.includes('archer');
      const totalMod = isAttackerUnit ? (modsAttacker[unit.instance_id] ?? 0) : (modsDefender[unit.instance_id] ?? 0);
      const effectiveAttack = isAttackerUnit
        ? (combatAttackerEffectiveAttackOverride[unit.instance_id] ?? unitDef.attack + totalMod)
        : undefined;
      const effectiveDefense = !isAttackerUnit ? unitDef.defense + totalMod : undefined;
      const specials = isAttackerUnit ? specialsAttacker[unit.instance_id] : specialsDefender[unit.instance_id];
      const unitFaction = (unitDef as { faction?: string }).faction;
      const out: CombatUnit = {
        id: unit.instance_id,
        unitType: unit.unit_id,
        name: unitDef.display_name,
        icon: unitDefs[unit.unit_id]?.icon || `/assets/units/${unit.unit_id}.png`,
        attack: unitDef.attack,
        defense: unitDef.defense,
        ...(effectiveAttack !== undefined && { effectiveAttack }),
        ...(effectiveDefense !== undefined && { effectiveDefense }),
        isArcher,
        health: unitDef.health,
        remainingHealth: unit.remaining_health,
        remainingMovement: unit.remaining_movement ?? 0,
        ...(unitFaction && { factionId: unitFaction }),
        ...(unitFaction && { factionColor: factionData[unitFaction]?.color ?? undefined }),
        ...(specials?.terror && { hasTerror: true }),
        ...(specials?.terrainMountain && { terrainMountain: true }),
        ...(specials?.terrainForest && { terrainForest: true }),
        ...(specials?.captain && { hasCaptainBonus: true }),
        ...(specials?.antiCavalry && { hasAntiCavalry: true }),
        ...(specials?.seaRaider && { hasSeaRaider: true }),
        ...(specials?.archer && { hasArcher: true }),
        ...(specials?.stealth && { hasStealth: true }),
        ...(specials?.bombikazi && { hasBombikazi: true }),
        ...(specials?.fearless && { hasFearless: true }),
        ...(specials?.hope && { hasHope: true }),
        ...(specials?.ram && { hasRam: true }),
        ...(archetype === 'siegework' && { siegeworkArchetype: true }),
      };
      if (
        currentTerritoryData[effectiveCombat.territory]?.terrain === 'sea'
        && isNavalUnitId(unit.unit_id)
        && backendTerritory?.units?.length
      ) {
        const n = backendTerritory.units.filter(
          (u: { loaded_onto?: string | null }) => u.loaded_onto === unit.instance_id
        ).length;
        if (n > 0) out.passengerCount = n;
      }
      return out;
    };

    if (isActiveCombat && backendTerritory) {
      const isSeaRaid = Boolean(acSeaZoneId);
      const attackerIdSet = new Set(acAttackerInstanceIds);

      if (isSeaRaid) {
        // Sea raid: attackers are LAND units in the sea zone (passengers); defenders are on the LAND territory. Boats stay in sea and do not fight.
        const seaZone = backendState.territories[acSeaZoneId!] as { units?: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number }[] } | undefined;
        if (seaZone?.units) {
          for (const unit of seaZone.units) {
            if (!attackerIdSet.has(unit.instance_id) || isNavalUnitId(unit.unit_id)) continue;
            const combatUnit = buildCombatUnit(unit, true);
            if (combatUnit) initialAttackerUnits.push(combatUnit);
          }
        }
        for (const unit of backendTerritory.units ?? []) {
          const isDefenderUnit = definitions.units[unit.unit_id]?.faction !== attackerFaction;
          if (!isDefenderUnit) continue;
          const combatUnit = buildCombatUnit(unit, false);
          if (combatUnit) initialDefenderUnits.push(combatUnit);
        }
      } else {
        // Land or same-hex combat. Sea hex: naval + aerial (not embarked); matches backend roster.
        const isNavalHexCombat = currentTerritoryData[effectiveCombat.territory]?.terrain === 'sea';
        for (const unit of backendTerritory.units ?? []) {
          if (isNavalHexCombat) {
            if (!unitParticipatesInSeaHexCombat(unit)) continue;
          }
          const isAttackerUnit = definitions.units[unit.unit_id]?.faction === attackerFaction;
          const combatUnit = buildCombatUnit(unit, isAttackerUnit);
          if (combatUnit) {
            if (isAttackerUnit) initialAttackerUnits.push(combatUnit);
            else initialDefenderUnits.push(combatUnit);
          }
        }
      }
    } else {
      // Preview: resolve attacker_units and defender_units from declared_battle by scanning all territories. For sea raid, only show land units as attackers.
      const attackerIds = new Set(effectiveCombat.attacker_units ?? []);
      const defenderIds = new Set(effectiveCombat.defender_units ?? []);
      const isSeaRaidPreview = Boolean(effectiveCombat.sea_zone_id);
      const territories = backendState.territories || {};
      for (const tid of Object.keys(territories)) {
        const ter = territories[tid] as {
          units?: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number; loaded_onto?: string | null }[];
        };
        if (!ter?.units) continue;
        const terrSea = currentTerritoryData[tid]?.terrain === 'sea';
        for (const unit of ter.units) {
          if (terrSea) {
            if (!unitParticipatesInSeaHexCombat(unit)) continue;
          }
          if (attackerIds.has(unit.instance_id)) {
            if (isSeaRaidPreview && isNavalUnitId(unit.unit_id)) continue;
            const c = buildCombatUnit(unit, true);
            if (c) initialAttackerUnits.push(c);
          } else if (defenderIds.has(unit.instance_id)) {
            const c = buildCombatUnit(unit, false);
            if (c) initialDefenderUnits.push(c);
          }
        }
      }
    }

    initialAttackerUnits.sort((a, b) => (a.id || '').localeCompare(b.id || ''));
    initialDefenderUnits.sort((a, b) => (a.id || '').localeCompare(b.id || ''));

    const retreatOptions = validRetreatDestinations.map(destId => ({
      territoryId: destId,
      territoryName: currentTerritoryData[destId]?.name || destId,
    }));

    /** Modal defender header: blank for unowned territory, or when owner would duplicate attacker side (same faction or alliance). */
    const defendingTerritoryOwnerFaction = (() => {
      if (!ownerIdNormalized) return '';
      if (ownerIdNormalized === attackerFaction) return '';
      const ownerAlliance =
        (definitions.factions?.[ownerIdNormalized] as { alliance?: string } | undefined)?.alliance ?? '';
      if (attackerAlliance && ownerAlliance && ownerAlliance === attackerAlliance) return '';
      return ownerIdNormalized;
    })();

    const acForRetreat = backendState?.active_combat as {
      attackers_have_rolled?: boolean;
      casualty_order_attacker?: string;
      must_conquer?: boolean;
      sea_zone_id?: string | null;
    } | undefined;
    const seaRaidCombat = Boolean(acForRetreat?.sea_zone_id);
    const canRetreat =
      (acForRetreat ? acForRetreat.attackers_have_rolled !== false : true) &&
      retreatOptions.length > 0 &&
      !seaRaidCombat;

    const territoryDefenderOrder = (backendState?.territory_defender_casualty_order ?? {})[effectiveCombat.territory] ?? 'best_unit';

    // When spectator and combat just ended: show result then auto-close. Prefer combat_ended event (correct for sea/naval); else territory owner.
    const combatEndResult =
      !canAct && spectatingBattle && effectiveCombat.territory === spectatingBattle.territory && !backendState?.active_combat
        ? (() => {
          const endEvt = eventLog.find(
            (e) =>
              e.type === 'combat_ended' &&
              (e.payload as { territory?: string } | undefined)?.territory === effectiveCombat.territory
          );
          const w = endEvt?.payload && typeof (endEvt.payload as { winner?: string }).winner === 'string'
            ? (endEvt.payload as { winner: string }).winner
            : null;
          if (w === 'attacker' || w === 'defender' || w === 'draw') {
            if (w === 'draw') return { attackerWon: false, defenderWon: false };
            return { attackerWon: w === 'attacker', defenderWon: w === 'defender' };
          }
          const postTerritory = backendState.territories?.[effectiveCombat.territory] as {
            owner?: string | null;
            original_owner?: string | null;
          } | undefined;
          const newOwnerRaw = postTerritory?.owner;
          const pcOverlay = pendingCapturesOverlayForPhase(
            backendState.phase,
            backendState.pending_captures as Record<string, string> | undefined,
          );
          const pendingCap = pcOverlay[effectiveCombat.territory];
          const effectivePostOwner =
            pendingCap != null && String(pendingCap).trim() !== ''
              ? displayOwnerForPendingCapture(String(pendingCap).trim(), postTerritory ?? {}, factionData)
              : newOwnerRaw;
          const normOwner = (o: unknown) =>
            o === null || o === undefined || (typeof o === 'string' && o.trim() === '') ? '' : String(o);
          const attackerWon = normOwner(effectivePostOwner) === attackerFaction;
          const defenderWon =
            normOwner(effectivePostOwner) === ownerIdNormalized && attackerFaction !== ownerIdNormalized;
          return { attackerWon, defenderWon };
        })()
        : null;

    const liveCombatLog = isActiveCombat && Array.isArray(ac?.combat_log) ? ac.combat_log : undefined;
    const snapCombatLog =
      !liveCombatLog &&
        persistedCombatLogRef.current?.key === openBattleKey &&
        Array.isArray(persistedCombatLogRef.current.log)
        ? persistedCombatLogRef.current.log
        : undefined;
    const combatLog = liveCombatLog ?? snapCombatLog;

    const acCumulative = ac as { cumulative_hits_received_by_attacker?: unknown; cumulative_hits_received_by_defender?: unknown } | undefined;
    const cumAttBackend = coerceBattleInt(acCumulative?.cumulative_hits_received_by_attacker);
    const cumDefBackend = coerceBattleInt(acCumulative?.cumulative_hits_received_by_defender);
    const logForSums = Array.isArray(combatLog) ? combatLog : undefined;
    const cumAttFromLog = sumDefenderHitsFromCombatLog(logForSums);
    const cumDefFromLog = sumAttackerHitsFromCombatLog(logForSums);
    const canReconcileCumulative =
      isActiveCombat ||
      (Array.isArray(combatLog) &&
        combatLog.length > 0 &&
        !!openBattleKey &&
        persistedCombatLogRef.current?.key === openBattleKey);
    const cumulativeHitsReceivedByAttacker = canReconcileCumulative
      ? Math.max(cumAttBackend, cumAttFromLog)
      : cumAttBackend;
    const cumulativeHitsReceivedByDefender = canReconcileCumulative
      ? Math.max(cumDefBackend, cumDefFromLog)
      : cumDefBackend;

    const defenderStrongholdHp =
      territory && (territory.stronghold_base_health ?? 0) > 0
        ? {
          current: territory.stronghold_current_health ?? territory.stronghold_base_health ?? 0,
          base: territory.stronghold_base_health ?? 0,
        }
        : undefined;

    const siegeworksPending = (ac as { combat_siegeworks_pending?: boolean })?.combat_siegeworks_pending ?? false;
    const archerPrefirePending = (ac as { combat_archer_prefire_pending?: boolean })?.combat_archer_prefire_pending ?? false;
    const siegeworksAttackerInstanceIds = (ac as { combat_siegeworks_attacker_instance_ids?: string[] })
      ?.combat_siegeworks_attacker_instance_ids;
    const siegeworksDefenderInstanceIds = (ac as { combat_siegeworks_defender_instance_ids?: string[] })
      ?.combat_siegeworks_defender_instance_ids;
    const ladderInfantryInstanceIds = (ac as { ladder_infantry_instance_ids?: string[] })?.ladder_infantry_instance_ids ?? [];
    const ladderEquipmentCount =
      typeof ac?.ladder_equipment_count === 'number' ? ac.ladder_equipment_count : 0;

    const attackerRamUnitTypes = Object.entries(definitions?.units ?? {})
      .filter(([, u]) => {
        const sp = (u as { specials?: string[] }).specials;
        return Array.isArray(sp) && sp.includes('ram');
      })
      .map(([id]) => id);

    const combatUnitDefs = Object.fromEntries(
      Object.entries(definitions?.units ?? {}).map(([id, u]) => {
        const ud = u as { archetype?: string; specials?: string[] };
        return [id, { archetype: ud.archetype, specials: ud.specials ?? [] }];
      }),
    );

    const attackerHasFuseBombOption =
      initialAttackerUnits.some((u) => {
        const ud = definitions?.units?.[u.unitType] as { tags?: string[] } | undefined;
        const tags = ud?.tags ?? [];
        return tags.includes('bomb') || u.unitType === 'bomb';
      }) &&
      initialAttackerUnits.some((u) => {
        const sp = (definitions?.units?.[u.unitType] as { specials?: string[] } | undefined)?.specials;
        return Array.isArray(sp) && sp.includes('bombikazi');
      });

    return {
      territoryName: territory?.name || effectiveCombat.territory,
      attackerFaction,
      defendingTerritoryOwnerFaction,
      initialAttackerUnits,
      initialDefenderUnits,
      retreatOptions,
      canRetreat,
      seaRaidCombat,
      siegeworksPending,
      archerPrefirePending,
      siegeworksAttackerInstanceIds,
      siegeworksDefenderInstanceIds,
      casualtyPriorityAttacker: acForRetreat?.casualty_order_attacker ?? 'best_unit',
      casualtyPriorityDefender: territoryDefenderOrder,
      mustConquer: acForRetreat?.must_conquer ?? false,
      combatLog,
      combatEndResult,
      cumulativeHitsReceivedByAttacker,
      cumulativeHitsReceivedByDefender,
      defenderStrongholdHp,
      ladderInfantryInstanceIds,
      ladderEquipmentCount,
      attackerRamUnitTypes,
      combatUnitDefs,
      attackerHasFuseBombOption,
    };
  }, [effectiveCombat, openBattleKey, backendState, currentTerritoryData, gameState.current_faction, definitions, unitDefs, validRetreatDestinations, factionData, canAct, spectatingBattle, ac, acTerritoryId, acSeaZoneId, acAttackerInstanceIds, eventLog]);

  const handleCombatRetreat = useCallback(async (destinationId: string) => {
    if (!combatDisplayProps) return;
    try {
      const result = await api.retreat(GAME_ID, destinationId);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      setBattlesCompletedThisPhase(prev => prev + 1);
      setActiveCombat(null);
      setHighlightedTerritories([]);

      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);

      addLogEntry(`Retreated from ${combatDisplayProps.territoryName}!`, 'combat');
    } catch (err) {
      addLogEntry(`Retreat failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [combatDisplayProps, addBackendEvents, addLogEntry]);

  const handleCombatClose = useCallback(
    (outcome: { attackerWon: boolean; defenderWon: boolean }, _survivingAttackers?: unknown) => {
      if (spectatingBattle) {
        setSpectatingBattle(null);
        return;
      }
      if (outcome.attackerWon) handleCombatEnd('attacker_wins');
      else if (outcome.defenderWon) handleCombatEnd('defender_wins');
      else handleCombatEnd('draw');
    },
    [spectatingBattle, handleCombatEnd]
  );

  const handleCombatCancel = useCallback(() => {
    if (spectatingBattle) {
      setSpectatingBattle(null);
      return;
    }
    setActiveCombat(null);
    setHighlightedTerritories([]);
  }, [spectatingBattle]);

  // Turn order for ticker – must run before any early return to keep hook count stable
  const turnOrderForTicker = useMemo(() => {
    const fromRef = initialTurnOrderRef.current;
    const fromState = backendState?.turn_order;
    if (fromRef?.length) return fromRef;
    if (fromState?.length) return fromState;
    if (!definitions?.factions) return [];
    const factions = Object.keys(definitions.factions);
    const good: string[] = [];
    const evil: string[] = [];
    factions.forEach((fid) => {
      const a = (definitions.factions[fid] as { alliance?: string })?.alliance;
      if (a === 'good') good.push(fid);
      else if (a === 'evil') evil.push(fid);
    });
    return [...good.sort(), ...evil.sort()];
  }, [backendState, definitions?.factions]);

  const gameOverWinner = backendState?.winner ?? null;
  const gameOverDisplay = useMemo(() => {
    if (!gameOverWinner) return null;
    const label = gameOverWinner === 'good' ? 'Good' : gameOverWinner === 'evil' ? 'Evil' : gameOverWinner;
    const color =
      Object.values(factionData).find((f) => f.alliance === gameOverWinner)?.color ??
      (gameOverWinner === 'good' ? '#2d5a27' : '#6b2d2d');
    return { label, color };
  }, [gameOverWinner, factionData]);

  const dismissForfeitToast = useCallback(() => {
    setForfeitToastDismissed(true);
    try {
      localStorage.setItem(FORFEIT_NOTIFICATION_STORAGE_KEY(GAME_ID), '1');
    } catch {
      // ignore
    }
  }, [GAME_ID]);

  const isLobby = gameMeta?.status === 'lobby';
  useEffect(() => {
    if (isLobby) startMenuAmbience('lobby');
    else stopMenuAmbience();
  }, [isLobby]);

  // Loading/error states
  if (loading) {
    return (
      <div className="app loading">
        <div className="loading-spinner">Loading game...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="app error">
        <div className="error-message">
          <h2>Error</h2>
          <p>{error}</p>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    );
  }

  // Safety check for missing data
  if (!definitions || !backendState) {
    return (
      <div className="app loading">
        <div className="loading-spinner">Loading game data...</div>
      </div>
    );
  }

  const isMovementPhase = gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move';
  const currentResources = gameState.faction_resources[gameState.current_faction] || {};
  const currentPower = currentResources.power || 0; // Keep for header display
  const currentFactionColor = gameState.current_faction
    ? factionData[gameState.current_faction]?.color
    : undefined;

  const showForfeitToast = Boolean(
    GAME_ID &&
    gameMeta?.forfeited_player_ids?.length &&
    gameMeta?.status !== 'lobby' &&
    backendState &&
    !forfeitToastDismissed &&
    typeof localStorage !== 'undefined' &&
    !localStorage.getItem(FORFEIT_NOTIFICATION_STORAGE_KEY(GAME_ID))
  );
  const forfeitedNames = showForfeitToast && gameMeta?.forfeited_player_ids?.length && gameMeta?.player_usernames
    ? gameMeta.forfeited_player_ids
      .map((pid) => gameMeta.player_usernames![pid] ?? `Player ${pid}`)
      .join(', ')
    : '';
  const newHostName =
    showForfeitToast && gameMeta?.host_forfeited && gameMeta?.created_by && gameMeta?.player_usernames
      ? gameMeta.player_usernames[gameMeta.created_by] ?? `Player ${gameMeta.created_by}`
      : '';
  const forfeitToastHostLine = newHostName ? ` ${newHostName} is now the host.` : '';

  return (
    <div className="app">
      {showForfeitToast && (
        <div className="forfeit-notification-toast" role="status">
          <p>
            {forfeitedNames} {gameMeta!.forfeited_player_ids!.length === 1 ? 'has' : 'have'} forfeited. Their turns will be skipped.{forfeitToastHostLine}
          </p>
          <button type="button" className="forfeit-notification-toast-close" onClick={dismissForfeitToast} aria-label="Dismiss">×</button>
        </div>
      )}
      {gameOverDisplay && (
        <div
          className="game-over-overlay"
          role="alert"
          aria-live="assertive"
        >
          <div
            className="game-over-banner"
            style={{ backgroundColor: `${gameOverDisplay.color}40`, borderColor: gameOverDisplay.color }}
          >
            <span className="game-over-title">Game Over</span>
            <span className="game-over-winner">{gameOverDisplay.label} wins!</span>
          </div>
        </div>
      )}

      <Header
        gameState={gameState}
        turnOrderForTicker={turnOrderForTicker}
        factionData={factionData}
        effectivePower={currentPower}
        factionStats={backendState?.faction_stats}
        unitsByFaction={unitsByFaction}
        gameName={gameMeta?.name ?? null}
        setupDisplayName={gameMeta?.scenario?.display_name ?? null}
        specials={definitions?.specials}
        specialsOrder={definitions?.specials_order ?? []}
        unitsBySpecial={unitsBySpecial}
        onOpenCombatSim={() => setCombatSimModalOpen(true)}
      />

      {/* Combat Simulator modal: always mounted so form state persists when closed */}
      <div
        className="combat-sim-modal-overlay"
        style={{ display: combatSimModalOpen ? 'flex' : 'none' }}
        onClick={() => setCombatSimModalOpen(false)}
        role="dialog"
        aria-modal="true"
        aria-label="Battle Simulator"
      >
        <div className="combat-sim-modal" onClick={(e) => e.stopPropagation()}>
          <header className="combat-sim-modal-header">
            <h2>Battle Simulator</h2>
            <button type="button" className="close-btn" onClick={() => setCombatSimModalOpen(false)} aria-label="Close">×</button>
          </header>
          <div className="combat-sim-modal-body">
            <CombatSimulatorPanel
              definitions={definitions}
              territoryUnits={currentTerritoryUnits}
              unitDefs={unitDefs}
              territoryData={currentTerritoryData}
              factionData={factionData}
              gameId={GAME_ID}
              setupId={gameSetupId}
              territoryDefenderCasualtyOrder={backendState?.territory_defender_casualty_order ?? {}}
              embedded
            />
          </div>
        </div>
      </div>

      <main className="main-content">
        <div className="map-and-tray">
          <div className="map-wrap">
            <GameMap
              gameState={gameState}
              selectedTerritory={selectedTerritory}
              selectedUnit={selectedUnit}
              territoryData={currentTerritoryData}
              territoryUnits={currentTerritoryUnits}
              territoryUnitsFull={territoryUnitsFull}
              unitDefs={unitDefs}
              unitStats={unitStats}
              factionData={factionData}
              onTerritorySelect={handleTerritorySelect}
              onSeaZoneStackClick={isMovementPhase ? handleSeaZoneStackClick : undefined}
              onUnitSelect={handleUnitSelect}
              onUnitMove={handleUnitMove}
              onBulkMoveDrop={handleBulkMoveDrop}
              canAct={canAct}
              isMovementPhase={isMovementPhase}
              isCombatMove={gameState.phase === 'combat_move'}
              isMobilizePhase={gameState.phase === 'mobilize'}
              hasMobilizationSelected={selectedMobilizationUnit !== null}
              validMobilizeTerritories={validMobilizeTerritories}
              validMobilizeSeaZones={validMobilizeSeaZones}
              navalUnitIds={navalUnitIds}
              remainingMobilizationCapacity={remainingMobilizationCapacity}
              remainingHomeSlots={remainingHomeSlots}
              onMobilizationDrop={canAct ? handleMobilizationDrop : undefined}
              onMobilizationAllDrop={canAct ? handleMobilizationAllDrop : undefined}
              mobilizationTray={
                gameState.phase === 'mobilize' && canAct ? {
                  purchases: mobilizablePurchases,
                  pendingCamps: unplacedCamps.map(c => ({ campIndex: c.campIndex, options: c.options })),
                  factionColor: factionData[gameState.current_faction]?.color || '#3a6ea5',
                  selectedUnitId: selectedMobilizationUnit,
                  selectedCampIndex,
                  onSelectUnit: setSelectedMobilizationUnit,
                  onSelectCamp: setSelectedCampIndex,
                  mobilizationAllValidZones,
                  canMobilizeAll: mobilizationAllValidZones.length > 0,
                } : null
              }
              onCampDrop={gameState.phase === 'mobilize' && canAct ? handleCampDrop : undefined}
              validCampTerritories={canAct && selectedCampIndex !== null ? validCampTerritories : []}
              territoriesWithPendingCampPlacement={gameState.phase === 'mobilize' && canAct ? Array.from(territoriesWithPendingCampPlacement) : []}
              pendingMoveConfirm={pendingMoveConfirm}
              onSetPendingMove={setPendingMoveConfirm}
              onDropDestination={setDropDestination}
              pendingMoves={gameState.pending_moves}
              highlightedTerritories={highlightedTerritories}
              availableMoveTargets={availableActions?.moveable_units?.map(m => ({
                territory: m.territory,
                unit: m.unit,
                destinations: normalizeMoveDestinations(m.destinations),
                destinationCosts: extractDestinationCosts(m.destinations),
                // Normalize so charge_routes is always a dict (dest id -> list of paths); backend may omit for non-cavalry
                charge_routes:
                  m.charge_routes && typeof m.charge_routes === 'object' && !Array.isArray(m.charge_routes)
                    ? m.charge_routes
                    : {},
              }))}
              aerialUnitsMustMove={aerialMustMove}
              loadedNavalMustAttackInstanceIds={availableActions?.loaded_naval_must_attack_instance_ids ?? []}
              forcedNavalCombatInstanceIds={availableActions?.forced_naval_combat_instance_ids ?? []}
              seaZoneIdsEligibleForNavalTrayStackClick={seaZoneIdsEligibleForNavalTrayStackClick}
              navalTray={
                isMovementPhase && navalTrayData
                  ? {
                    seaZoneId: navalTrayData.seaZoneId,
                    seaZoneName: navalTrayData.seaZoneName,
                    boats: navalTrayData.boats,
                    factionColor: navalTrayData.factionColor,
                  }
                  : null
              }
              onCloseNavalTray={() => setSelectedSeaZoneForNavalTray(null)}
              pendingLoadBoatOptions={(() => {
                const opts = pendingMoveConfirm?.boatOptions;
                if (!opts || opts.length < 2) return undefined;
                const pmTo =
                  typeof pendingMoveConfirm.toTerritory === 'string' ? pendingMoveConfirm.toTerritory.trim() : '';
                const pmFrom =
                  typeof pendingMoveConfirm.fromTerritory === 'string' ? pendingMoveConfirm.fromTerritory.trim() : '';
                const fromSea =
                  Boolean(pmFrom) &&
                  (currentTerritoryData[pmFrom]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(pmFrom));
                const traySea = navalTrayData?.seaZoneId;
                const aligns =
                  !traySea ||
                  traySea === pmTo ||
                  canonicalSeaZoneId(traySea) === canonicalSeaZoneId(pmTo) ||
                  (fromSea &&
                    (traySea === pmFrom || canonicalSeaZoneId(traySea) === canonicalSeaZoneId(pmFrom)));
                return aligns ? opts : undefined;
              })()}
              onChooseBoatForLoad={handleChooseBoat}
              pendingLoadPassengers={pendingLoadPassengers}
              loadAllocation={pendingMoveConfirm?.loadAllocation}
              onLoadAllocationChange={handleLoadAllocationChange}
            />
          </div>
        </div>

        {!sidebarCollapsed && (
          <div
            className="sidebar-resize-handle"
            onMouseDown={handleResizeStart}
            title="Drag to resize panel"
          />
        )}

        <div
          className={`sidebar-wrapper ${sidebarCollapsed ? 'collapsed' : ''}`}
          style={sidebarCollapsed ? undefined : { width: sidebarWidthCapped }}
        >
          <button
            type="button"
            className="sidebar-toggle"
            onClick={() => setSidebarCollapsed(c => !c)}
            title={sidebarCollapsed ? 'Show panel' : 'Hide panel'}
          >
            {sidebarCollapsed ? '◀' : '▶'}
          </button>
          {!sidebarCollapsed && (
            <Sidebar
              canAct={canAct}
              gameOver={!!backendState?.winner}
              turnAccentColor={currentFactionColor}
              gameState={gameState}
              selectedTerritory={selectedTerritory}
              territoryData={currentTerritoryData}
              territoryUnits={currentTerritoryUnits}
              territoryUnitStacksWithMovement={territoryUnitStacksWithMovement}
              unitDefs={unitDefs}
              factionData={factionData}
              eventLog={eventLog}
              onEndPhase={handleEndPhase}
              onOpenPurchase={handleOpenPurchase}
              onInitiateCombat={handleOpenCombat}
              pendingEndPhaseConfirm={pendingEndPhaseConfirm}
              hasPurchaseCart={hasPurchaseCart}
              endPhaseDisabled={endPhaseDisabled}
              endPhaseDisabledReason={endPhaseDisabledReason}
              onConfirmEndPhase={handleConfirmEndPhase}
              onCancelEndPhase={handleCancelEndPhase}
              pendingMoveConfirm={pendingMoveConfirm}
              onUpdateMoveCount={handleUpdateMoveCount}
              onConfirmMove={handleConfirmMove}
              onCancelMove={handleCancelMove}
              onChooseChargePath={handleChooseChargePath}
              onChooseSeaRaidSeaZone={handleChooseSeaRaidSeaZone}
              onRequestSeaRaidZoneChoice={handleRequestSeaRaidZoneChoice}
              pendingOffloadSeaChoice={pendingOffloadSeaChoice}
              onChooseOffloadSeaZone={handleChooseOffloadSeaZone}
              onCancelOffloadSeaChoice={handleCancelOffloadSeaChoice}
              bulkMoveConfirm={bulkMoveConfirm}
              onConfirmBulkMove={handleConfirmBulkMove}
              onCancelBulkMove={handleCancelBulkMove}
              onCancelPendingMove={handleCancelPendingMove}
              bulkMobilizeConfirm={bulkMobilizeConfirm}
              onConfirmBulkMobilize={handleConfirmBulkMobilize}
              onCancelBulkMobilize={handleCancelBulkMobilize}
              pendingMobilization={pendingMobilization}
              onUpdateMobilizationCount={handleUpdateMobilizationCount}
              onConfirmMobilization={handleConfirmMobilization}
              onCancelMobilization={handleCloseMobilizationConfirm}
              onCancelPendingMobilization={handleCancelMobilization}
              pendingCampPlacement={pendingCampPlacement}
              onConfirmCampPlacement={handleConfirmCampPlacement}
              onCancelCampPlacement={handleCancelCampPlacement}
              pendingCampPlacements={gameState.pending_camp_placements}
              onCancelQueuedCampPlacement={handleCancelQueuedCampPlacement}
              pendingMobilizations={gameState.pending_mobilizations}
              battlesCompletedThisPhase={battlesCompletedThisPhase}
              combatMovesDeclaredThisPhase={combatMovesDeclaredThisPhase}
              pendingRetreat={pendingRetreat}
              validRetreatDestinations={validRetreatDestinations}
              onConfirmRetreat={handleConfirmRetreat}
              onCancelRetreat={handleCancelRetreat}
              hasUnmobilizedPurchases={mobilizablePurchases.length > 0 || unplacedCamps.length > 0}
              aerialUnitsMustMove={aerialMustMove}
              territoryDefenderCasualtyOrder={backendState?.territory_defender_casualty_order ?? {}}
              onSetTerritoryDefenderCasualtyOrder={handleSetTerritoryDefenderCasualtyOrder}
              activeCombatTerritoryId={acTerritoryId ?? null}
              activeCombatSeaZoneId={acSeaZoneId ?? null}
              onSpectateBattle={setSpectatingBattle}
              isCurrentFactionAI={isAITurn}
              forcedNavalStandoffSeaZoneIds={availableActions?.forced_naval_standoff_sea_zone_ids ?? []}
            />
          )}
        </div>
      </main>

      <PurchaseModal
        key={gameState.current_faction}
        isOpen={isPurchaseModalOpen}
        factionColor={factionData[gameState.current_faction]?.color}
        availableResources={currentResources}
        availableUnits={availableUnits}
        hasPort={hasPort}
        currentPurchases={gameState.phase === 'purchase' ? purchaseCart : gameState.pending_purchases}
        currentCamps={gameState.phase === 'purchase' ? purchaseCampsCount : 0}
        maxCamps={maxCampsPurchasable}
        mobilizationCapacity={availableActions?.mobilization_capacity}
        mobilizationLandCapacity={availableActions?.mobilization_land_capacity}
        mobilizationCampLandCapacity={availableActions?.mobilization_camp_land_capacity}
        mobilizationSeaCapacity={availableActions?.mobilization_sea_capacity}
        purchasedUnitsCount={gameState.phase === 'purchase' ? Object.values(purchaseCart).reduce((s, q) => s + q, 0) : (availableActions?.purchased_units_count ?? 0)}
        campCost={availableActions?.camp_cost}
        strongholdRepairCost={strongholdRepairCostPerHp}
        repairableStrongholds={repairableStrongholds}
        currentRepairs={purchaseRepairs}
        onPurchase={handlePurchase}
        onClose={handleClosePurchase}
      />

      {/* Combat Display */}
      {combatDisplayProps && (
        <CombatDisplay
          isOpen={true}
          readOnly={!canAct}
          territoryName={combatDisplayProps.territoryName}
          attacker={{
            faction: combatDisplayProps.attackerFaction,
            factionName: factionData[combatDisplayProps.attackerFaction]?.name || combatDisplayProps.attackerFaction,
            factionIcon: factionData[combatDisplayProps.attackerFaction]?.icon || '',
            factionColor: factionData[combatDisplayProps.attackerFaction]?.color || '#666',
            units: combatDisplayProps.initialAttackerUnits,
          }}
          defender={{
            faction: combatDisplayProps.defendingTerritoryOwnerFaction,
            factionName: combatDisplayProps.defendingTerritoryOwnerFaction
              ? factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.name
              || combatDisplayProps.defendingTerritoryOwnerFaction
              : '',
            factionIcon: combatDisplayProps.defendingTerritoryOwnerFaction
              ? factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.icon || ''
              : '',
            factionColor: combatDisplayProps.defendingTerritoryOwnerFaction
              ? factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.color || '#666'
              : '',
            units: combatDisplayProps.initialDefenderUnits,
          }}
          retreatOptions={combatDisplayProps.retreatOptions}
          canRetreat={combatDisplayProps.canRetreat}
          seaRaidCombat={combatDisplayProps.seaRaidCombat}
          siegeworksPending={combatDisplayProps.siegeworksPending}
          archerPrefirePending={combatDisplayProps.archerPrefirePending}
          siegeworksAttackerInstanceIds={combatDisplayProps.siegeworksAttackerInstanceIds}
          siegeworksDefenderInstanceIds={combatDisplayProps.siegeworksDefenderInstanceIds}
          casualtyPriorityAttacker={combatDisplayProps.casualtyPriorityAttacker}
          casualtyPriorityDefender={combatDisplayProps.casualtyPriorityDefender}
          mustConquer={combatDisplayProps.mustConquer}
          onStartRound={handleStartCombatRound}
          onRetreat={handleCombatRetreat}
          onClose={handleCombatClose}
          onCancel={handleCombatCancel}
          onHighlightTerritories={setHighlightedTerritories}
          specials={definitions?.specials}
          combatLog={!canAct ? combatDisplayProps.combatLog : undefined}
          combatEndResult={!canAct ? combatDisplayProps.combatEndResult : undefined}
          cumulativeHitsReceivedByAttacker={combatDisplayProps.cumulativeHitsReceivedByAttacker}
          cumulativeHitsReceivedByDefender={combatDisplayProps.cumulativeHitsReceivedByDefender}
          defenderStrongholdHp={combatDisplayProps.defenderStrongholdHp}
          ladderInfantryInstanceIds={combatDisplayProps.ladderInfantryInstanceIds}
          ladderEquipmentCount={combatDisplayProps.ladderEquipmentCount}
          attackerRamUnitTypes={combatDisplayProps.attackerRamUnitTypes}
          combatUnitDefs={combatDisplayProps.combatUnitDefs}
          attackerHasFuseBombOption={combatDisplayProps.attackerHasFuseBombOption}
        />
      )}

      {isLobby && (
        <LobbyView
          gameId={GAME_ID}
          meta={gameMeta!}
          definitions={definitions}
          turnOrder={turnOrderForTicker}
          onMetaUpdate={setGameMeta}
          onGameStarted={handleGameStarted}
        />
      )}
    </div>
  );
}

export default App;
