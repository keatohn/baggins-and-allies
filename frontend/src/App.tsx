import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import type { GameState, GamePhase, FactionId, GameEvent, SelectedUnit, DeclaredBattle } from './types/game';
import Header from './components/Header';
import GameMap, { type PendingMoveConfirm } from './components/GameMap';
import Sidebar from './components/Sidebar';
import PurchaseModal from './components/PurchaseModal';
import CombatDisplay, { type CombatRound } from './components/CombatDisplay';
import api, {
  type ApiGameState,
  type ApiEvent,
  type Definitions,
  type AvailableActionsResponse,
  type GameMeta,
} from './services/api';
import LobbyView from './components/LobbyView';
import './App.css';

export interface PendingMobilization {
  unitId: string;
  unitName: string;
  unitIcon: string;
  toTerritory: string;
  maxCount: number;
  count: number;
}

const DEFAULT_GAME_ID = 'game_1';

/** Poll game state this often (ms) so other players' actions appear without refresh. */
const GAME_POLL_INTERVAL_MS = 3000;

/** Backend returns destinations as { territory_id: cost }. Return list of territory IDs. */
function normalizeMoveDestinations(
  destinations: Record<string, number> | { by_distance?: Record<number, string[]> } | undefined
): string[] {
  if (!destinations || typeof destinations !== 'object') return [];
  if ('by_distance' in destinations && destinations.by_distance) {
    return Object.values(destinations.by_distance).flat();
  }
  return Object.keys(destinations);
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
  isArcher?: boolean;
  health: number;
  remainingHealth: number;
  remainingMovement?: number;
  factionColor?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasAntiCavalry?: boolean;
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
};

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
  const [activeCombat, setActiveCombat] = useState<DeclaredBattle | null>(null);
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    localStorage.getItem('sidebarCollapsed') === '1'
  );
  /** Sea zone selected to show naval tray (boats + passengers) during combat_move or non_combat_move. */
  const [selectedSeaZoneForNavalTray, setSelectedSeaZoneForNavalTray] = useState<string | null>(null);
  /** Purchase phase: cart of units to buy; applied on End phase, not on Confirm */
  const [purchaseCart, setPurchaseCart] = useState<Record<string, number>>({});
  /** Purchase phase: number of camps to buy; applied on End phase (after units). */
  const [purchaseCampsCount, setPurchaseCampsCount] = useState(0);
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

  // When switching games, clear backend state (or use passed initial state for the new game)
  useEffect(() => {
    setBackendState(initialStateProp ?? null);
  }, [GAME_ID, initialStateProp]);

  useEffect(() => {
    localStorage.setItem('sidebarWidth', String(sidebarWidth));
  }, [sidebarWidth]);
  useEffect(() => {
    localStorage.setItem('sidebarCollapsed', sidebarCollapsed ? '1' : '0');
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (backendState && backendState.phase !== 'purchase') {
      setPurchaseCart({});
      setPurchaseCampsCount(0);
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

  // Derived data from backend definitions (archetype/tags for aerial return-path rule in combat move)
  const unitDefs = useMemo(() => {
    if (!definitions) return {};
    const defs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[]; home_territory_id?: string; home_territory_ids?: string[]; cost?: number }> = {};
    for (const [id, unit] of Object.entries(definitions.units)) {
      const u = unit as { display_name: string; icon?: string; faction?: string; archetype?: string; tags?: string[]; home_territory_id?: string; home_territory_ids?: string[]; cost?: number | { power?: number } };
      const cost = typeof u.cost === 'object' && u.cost?.power != null ? u.cost.power : (typeof u.cost === 'number' ? u.cost : 0);
      const singleHome = u.home_territory_id != null ? [u.home_territory_id] : [];
      const multiHome = Array.isArray(u.home_territory_ids) ? u.home_territory_ids : [];
      const homeIds = [...new Set([...singleHome, ...multiHome])];
      defs[id] = {
        name: u.display_name,
        icon: `/assets/units/${u.icon || `${id}.png`}`,
        faction: u.faction,
        archetype: u.archetype,
        tags: u.tags,
        home_territory_id: u.home_territory_id,
        home_territory_ids: homeIds.length > 0 ? homeIds : undefined,
        cost,
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
      produces: number;
      adjacent: string[];
      aerial_adjacent?: string[];
      ownable: boolean;
    }> = {};
    for (const [id, territory] of Object.entries(definitions.territories)) {
      defs[id] = {
        name: territory.display_name,
        terrain: territory.terrain_type,
        stronghold: territory.is_stronghold,
        produces: (territory.produces?.power as number) || 0,
        adjacent: territory.adjacent,
        aerial_adjacent: (territory as { aerial_adjacent?: string[] }).aerial_adjacent ?? [],
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
      produces: number;
      adjacent: string[];
      aerial_adjacent?: string[];
      hasCamp: boolean;
      hasPort: boolean;
      isCapital: boolean;
      ownable?: boolean;
    }> = {};

    for (const [id, territory] of Object.entries(backendState.territories)) {
      const def = territoryDefs[id];
      result[id] = def
        ? {
          ...def,
          owner: territory.owner as FactionId | undefined,
          hasCamp: territoryHasCamp(id),
          hasPort: territoryHasPort(id),
          isCapital: territoryIsCapital(id),
        }
        : {
          name: id.replace(/_/g, ' '),
          owner: territory.owner as FactionId | undefined,
          terrain: 'land',
          stronghold: false,
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
  }, [backendState, territoryDefs, definitions]);

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
      r[tid] = (t.units || []).map((u: { instance_id: string; unit_id: string; loaded_onto?: string | null }) => ({
        instance_id: u.instance_id,
        unit_id: u.unit_id,
        loaded_onto: u.loaded_onto ?? undefined,
      }));
    }
    return r;
  }, [backendState]);

  // Per-stack unit rows with remaining movement (non-combat move phase only, for selected territory)
  const territoryUnitStacksWithMovement = useMemo(() => {
    if (!backendState || !selectedTerritory || backendState.phase !== 'non_combat_move') return null;
    const territory = backendState.territories[selectedTerritory];
    if (!territory?.units?.length) return null;
    const keyed: Record<string, { unit_id: string; remaining_movement: number; count: number }> = {};
    for (const u of territory.units) {
      const key = `${u.unit_id}:${u.remaining_movement}`;
      if (!keyed[key]) keyed[key] = { unit_id: u.unit_id, remaining_movement: u.remaining_movement, count: 0 };
      keyed[key].count += 1;
    }
    return Object.values(keyed).sort((a, b) => b.remaining_movement - a.remaining_movement);
  }, [backendState, selectedTerritory]);

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

  /** Unit specials for display: tags that are specials (exclude land, mounted), plus archetype-derived. "anti cavalry" with dash. Sorted alphabetically. Home gets "Home (Territory Name)" when territoryNames provided. */
  const getUnitSpecials = useCallback((
    u: { tags?: string[]; archetype?: string; specials?: string[] },
    opts?: { homeTerritoryDisplayNames?: string[] }
  ) => {
    const out = new Set<string>();
    const exclude = new Set(['land', 'mounted']);
    (u.tags || []).filter(t => !exclude.has(t)).forEach(t => out.add(t));
    if (u.archetype === 'archer') out.add('archer');
    if (u.archetype === 'cavalry') out.add('charging');
    if (u.archetype === 'aerial') out.add('aerial');
    (u.specials || []).forEach(s => out.add(s));
    const format = (s: string) => {
      const normalized = s.replace(/_/g, ' ').replace(/-/g, ' ').toLowerCase().trim();
      if (normalized === 'anti cavalry') return 'Anti-Cavalry';
      return s.replace(/_/g, ' ').replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    };
    const names = [...out].map(format).sort((a, b) => a.localeCompare(b));
    if (out.has('home') && opts?.homeTerritoryDisplayNames?.length) {
      return names.map(n => n === 'Home' ? `Home (${opts.homeTerritoryDisplayNames!.join(', ')})` : n);
    }
    return names;
  }, []);

  /** Home territory display names for a unit (from definitions). */
  const getHomeTerritoryDisplayNames = useCallback((u: { home_territory_id?: string | null; home_territory_ids?: string[] | null }, territories: Record<string, { display_name: string }>) => {
    const ids = u.home_territory_ids?.length ? u.home_territory_ids : (u.home_territory_id ? [u.home_territory_id] : []);
    return ids.map(tid => territories[tid]?.display_name ?? tid).filter(Boolean);
  }, []);

  // All units grouped by faction (for Unit Stats modal), ordered by cost ascending. Specials include "Home (Territory Name)".
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
      byFaction[fid].sort((a, b) => a.cost - b.cost);
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

  // Purchasable units for current faction (with land/naval split for purchase modal tabs)
  const availableUnits = useMemo(() => {
    if (!availableActions?.purchasable_units) return [];
    return availableActions.purchasable_units.map(u => {
      const def = definitions?.units?.[u.unit_id] as { specials?: string[] } | undefined;
      const specialsCount = Array.isArray(def?.specials) ? def.specials.length : 0;
      const ud = unitDefs[u.unit_id] as { home_territory_ids?: string[]; home_territory_id?: string } | undefined;
      const homeTerritoryCount = (ud?.home_territory_ids?.length ?? (ud?.home_territory_id ? 1 : 0)) || 0;
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
        isNaval: navalUnitIds.has(u.unit_id),
        specialsCount,
        homeTerritoryCount,
      };
    });
  }, [availableActions, unitDefs, navalUnitIds, definitions?.units]);

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

  const addLogEntry = useCallback((message: string, type: string = 'info') => {
    const event: GameEvent = {
      id: `${Date.now()}-${Math.random()}`,
      type,
      message,
      timestamp: Date.now(),
    };
    setEventLog(prev => [event, ...prev]);
  }, []);

  // Add backend events to log
  const addBackendEvents = useCallback((events: ApiEvent[]) => {
    events.forEach(e => addLogEntry(e.payload?.message as string || e.type, e.type));
  }, [addLogEntry]);

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
            if (stateRes.definitions) {
              setDefinitions(stateRes.definitions);
              gotDefinitionsFromGame = true;
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

  // Poll game state so other players' actions appear live (1s when spectating a battle, else 3s)
  const pollIntervalMs = spectatingBattle ? 1000 : GAME_POLL_INTERVAL_MS;
  useEffect(() => {
    if (!GAME_ID) return;
    const t = setInterval(() => refreshState(), pollIntervalMs);
    return () => clearInterval(t);
  }, [GAME_ID, refreshState, pollIntervalMs]);

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

    // Convert territories to frontend format
    const territories: Record<string, { id: string; owner?: string; units: any[] }> = {};
    for (const [id, territory] of Object.entries(backendState.territories)) {
      territories[id] = {
        id,
        owner: territory.owner || undefined,
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

    // Build declared battles: use attacker_unit_ids/defender_unit_ids from API for preview.
    // Exclude territories that are sea raid targets so we only have one entry per territory (the sea raid one with sea_zone_id); otherwise initiating without sea_zone_id fails with "Naval units cannot attack on land".
    const seaRaidTerritoryIds = new Set((availableActions?.sea_raid_targets || []).map((t: { territory_id: string }) => t.territory_id));
    const combatTerrs = (availableActions?.combat_territories || [])
      .filter((ct: { territory_id: string }) => !seaRaidTerritoryIds.has(ct.territory_id))
      .map((ct): DeclaredBattle => {
        const c = ct as { territory_id: string; attacker_unit_ids?: string[]; defender_unit_ids?: string[]; sea_zone_id?: string };
        return {
          territory: c.territory_id,
          sea_zone_id: c.sea_zone_id,
          attacker_units: Array.isArray(c.attacker_unit_ids) ? c.attacker_unit_ids : [],
          defender_units: Array.isArray(c.defender_unit_ids) ? c.defender_unit_ids : [],
        };
      });
    const isNavalUnit = (unitId: string) => {
      const d = definitions?.units?.[unitId] as { archetype?: string; tags?: string[] } | undefined;
      return d && ((d.archetype ?? '') === 'naval' || (d.tags ?? []).includes('naval'));
    };
    const seaRaids = (availableActions?.sea_raid_targets || []).map((t): DeclaredBattle => {
      const seaZoneId = t.sea_zone_id;
      const tid = t.territory_id;
      const attackerIds: string[] = [];
      const defenderIds: string[] = [];
      if (seaZoneId && backendState.territories?.[seaZoneId]) {
        const seaUnits = (backendState.territories[seaZoneId] as { units?: { instance_id: string; unit_id: string }[] }).units ?? [];
        const faction = backendState.current_faction;
        for (const u of seaUnits) {
          const def = definitions?.units?.[u.unit_id] as { faction?: string } | undefined;
          if (def?.faction === faction && !isNavalUnit(u.unit_id)) attackerIds.push(u.instance_id);
        }
      }
      if (tid && backendState.territories?.[tid]) {
        const landUnits = (backendState.territories[tid] as { units?: { instance_id: string; unit_id: string }[] }).units ?? [];
        const faction = backendState.current_faction;
        for (const u of landUnits) {
          const def = definitions?.units?.[u.unit_id] as { faction?: string } | undefined;
          if (def?.faction !== faction && def != null) defenderIds.push(u.instance_id);
        }
      }
      return { territory: tid, sea_zone_id: seaZoneId, attacker_units: attackerIds, defender_units: defenderIds };
    });
    const declaredBattles: DeclaredBattle[] = [...combatTerrs, ...seaRaids];

    // Use backend's pending_moves directly
    // Instance ID format: faction_unitid_number (e.g., gondor_gondor_infantry_001)
    // To extract unit_id: remove first part (faction) and last part (number)
    const pendingMoves = (backendState.pending_moves || []).map((move, idx) => ({
      id: `move_${idx}`,
      from: move.from_territory,
      to: move.to_territory,
      unitType: move.unit_instance_ids[0]?.split('_').slice(1, -1).join('_') || '', // Extract unit_id from instance ID
      count: move.unit_instance_ids.length,
      phase: move.phase as 'combat_move' | 'non_combat_move',
      move_type: move.move_type ?? undefined,
    }));

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
  }, [backendState, availableActions, definitions]);

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
    const territories = availableActions?.mobilize_options?.capacity?.territories;
    if (!Array.isArray(territories)) return out;
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
    for (const t of territories) {
      const home = (t as { territory_id?: string; power?: number; home_unit_capacity?: Record<string, number> }).home_unit_capacity;
      if (!home || typeof home !== 'object') continue;
      const tid = t.territory_id as string;
      if (!tid) continue;
      out[tid] = {};
      for (const unitId of Object.keys(home)) {
        const cap = Number(home[unitId]) || 1;
        const used = pendingByDestAndUnit[tid]?.[unitId] ?? 0;
        out[tid][unitId] = Math.max(0, cap - used);
      }
    }
    return out;
  }, [availableActions?.mobilize_options?.capacity?.territories, backendState?.pending_mobilizations]);

  // Naval tray: boats + passengers for selected sea zone (movement phases only); pending loads shown as passengers on first boat
  const navalTrayData = useMemo(() => {
    const seaZoneId = selectedSeaZoneForNavalTray;
    if (!seaZoneId || !navalUnitIds.size) return null;
    const fullUnits = territoryUnitsFull[seaZoneId] ?? [];
    const boats = fullUnits.filter((u) => navalUnitIds.has(u.unit_id));
    const loadMovesToZone = (backendState?.pending_moves ?? []).filter(
      (m) => m.to_territory === seaZoneId && m.move_type === 'load'
    );
    const pendingLoadByUnit: Record<string, number> = {};
    for (const move of loadMovesToZone) {
      const uids = move.unit_instance_ids ?? [];
      for (const instanceId of uids) {
        const unitId = instanceId.split('_').slice(1, -1).join('_') || '';
        if (unitId) pendingLoadByUnit[unitId] = (pendingLoadByUnit[unitId] ?? 0) + 1;
      }
    }
    const pendingPassengerIcons = Object.entries(pendingLoadByUnit).flatMap(([unitId, count]) =>
      Array.from({ length: count }, () => ({
        unitId,
        name: unitDefs[unitId]?.name ?? unitId,
        icon: unitDefs[unitId]?.icon ?? `/assets/units/${unitId}.png`,
      }))
    );
    const boatsForTray = boats.map((boat, idx) => {
      const passengers = fullUnits.filter((u) => u.loaded_onto === boat.instance_id);
      const passengerIcons = passengers.map((p) => ({
        unitId: p.unit_id,
        name: unitDefs[p.unit_id]?.name ?? p.unit_id,
        icon: unitDefs[p.unit_id]?.icon ?? `/assets/units/${p.unit_id}.png`,
        instanceId: p.instance_id,
      }));
      const withPending = idx === 0 ? [...passengerIcons, ...pendingPassengerIcons] : passengerIcons;
      return {
        boatInstanceId: boat.instance_id,
        unitId: boat.unit_id,
        name: unitDefs[boat.unit_id]?.name ?? boat.unit_id,
        icon: unitDefs[boat.unit_id]?.icon ?? `/assets/units/${boat.unit_id}.png`,
        passengers: withPending,
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
    backendState?.pending_moves,
  ]);

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

  // When tray opens for load with 2+ boats, init loadAllocation so all passengers start on first boat (user can drag to reassign)
  useEffect(() => {
    const pm = pendingMoveConfirm;
    if (!pm?.boatOptions || pm.boatOptions.length < 2 || pm.loadAllocation != null) return;
    if (pendingLoadPassengerInstanceIds.length === 0) return;
    const firstBoatId = pm.boatOptions[0]?.[0];
    if (!firstBoatId) return;
    const initial = { [firstBoatId]: [...pendingLoadPassengerInstanceIds] };
    loadAllocationRef.current = initial;
    setPendingMoveConfirmState((prev) => (prev ? { ...prev, loadAllocation: initial } : prev));
  }, [pendingMoveConfirm?.boatOptions, pendingMoveConfirm?.loadAllocation, pendingLoadPassengerInstanceIds]);

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
          const remaining = remainingMobilizationCapacity[territoryId] ?? 0;
          const maxCount = Math.min(purchase.count, remaining);
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
  }, [gameState.phase, selectedCampIndex, validCampTerritories, selectedMobilizationUnit, mobilizablePurchases, validMobilizeTerritories, validMobilizeSeaZones, navalUnitIds, remainingMobilizationCapacity, addLogEntry, refreshState]);

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
    const isSea = terrain === 'sea' || /^sea_zone\d*$/i.test(to);
    if (!isSea) return;
    const fromTerrain = currentTerritoryData[typeof pendingMoveConfirm.fromTerritory === 'string' ? pendingMoveConfirm.fromTerritory : '']?.terrain;
    const fromSea = fromTerrain === 'sea' || /^sea_zone\d*$/i.test(String(pendingMoveConfirm.fromTerritory ?? ''));
    const isLoad = !fromSea && isSea;
    if (!isLoad) return;
    const boatsInZone = (territoryUnitsFull[to] ?? []).filter((u) => navalUnitIds.has(u.unit_id));
    if (boatsInZone.length >= 2) setSelectedSeaZoneForNavalTray(to);
  }, [pendingMoveConfirm?.toTerritory, pendingMoveConfirm?.fromTerritory, pendingMoveConfirm, currentTerritoryData, territoryUnitsFull, navalUnitIds]);

  /** Drop camp on territory → set pending; user confirms or cancels in sidebar (like unit mobilization). */
  const handleCampDrop = useCallback((campIndex: number, territoryId: string) => {
    setPendingCampPlacement({ campIndex, territoryId });
  }, []);

  const handleConfirmCampPlacement = useCallback(async () => {
    if (!pendingCampPlacement) return;
    const { campIndex, territoryId } = pendingCampPlacement;
    const territoryName = currentTerritoryData[territoryId]?.name ?? territoryId;
    try {
      const result = await api.queueCampPlacement(GAME_ID, campIndex, territoryId);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events?.length) addBackendEvents(result.events);
      else addLogEntry(`Camp planned for ${territoryName}`, 'info');
      setPendingCampPlacement(null);
      setSelectedCampIndex(null);
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(err instanceof Error ? err.message : 'Camp placement failed', 'error');
    }
  }, [pendingCampPlacement, currentTerritoryData, addLogEntry, addBackendEvents]);

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

  const hasPurchaseCart =
    Object.values(purchaseCart).some(qty => qty > 0) || purchaseCampsCount > 0;

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
  }, [gameState.phase, gameState.declared_battles, hasPurchaseCart, purchaseCart, purchaseCampsCount, hasCombatMovedThisPhase, hasNonCombatMovedThisPhase, pendingEndPhaseConfirm, mobilizablePurchases, aerialMustMove, addLogEntry, addBackendEvents, refreshState]);

  const handleConfirmEndPhase = useCallback(() => {
    setPendingEndPhaseConfirm(null);
    handleEndPhase();
  }, [handleEndPhase]);

  const handleCancelEndPhase = useCallback(() => {
    setPendingEndPhaseConfirm(null);
  }, []);

  /** TEMPORARY DEV: Skip current faction's turn (advances to next; empty factions get turn_skipped). Remove only the button before release; keep the endpoint (used by forfeit). */
  const handleSkipTurn = useCallback(async () => {
    try {
      const result = await api.skipTurn(GAME_ID);
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);
      await refreshState();
    } catch (err) {
      addLogEntry(`Skip turn failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      await refreshState();
    }
  }, [addLogEntry, addBackendEvents, refreshState]);

  const handleOpenPurchase = useCallback(() => {
    if (backendState?.winner) return;
    setIsPurchaseModalOpen(true);
  }, [backendState?.winner]);

  const handleClosePurchase = useCallback(() => {
    setIsPurchaseModalOpen(false);
  }, []);

  /** Confirm = save cart only; resources stay unchanged until End phase */
  const handlePurchase = useCallback((purchases: Record<string, number>, campsCount: number = 0) => {
    setPurchaseCart(purchases);
    setPurchaseCampsCount(campsCount);
    setIsPurchaseModalOpen(false);
  }, []);

  const handleUpdateMoveCount = useCallback((count: number) => {
    setPendingMoveConfirm(prev => prev ? { ...prev, count } : null);
  }, []);

  const handleCancelMove = useCallback(() => {
    setPendingMoveConfirm(null);
  }, []);

  const handleChooseChargePath = useCallback((path: string[]) => {
    setPendingMoveConfirm(prev => prev ? { ...prev, chargeThrough: path, chargePathOptions: undefined } : null);
  }, []);

  const handleChooseBoat = useCallback((option: string[]) => {
    // option = [boatInstanceId, ...passengerInstanceIds]; store passenger IDs for move, boat for load_onto_boat
    const boatId = option[0];
    const passengerIds = option.slice(1);
    setPendingMoveConfirm(prev =>
      prev
        ? {
          ...prev,
          instanceIds: passengerIds.length > 0 ? passengerIds : prev.instanceIds,
          loadOntoBoatInstanceId: boatId,
          boatOptions: undefined,
        }
        : null
    );
  }, []);

  const handleRequestSeaRaidZoneChoice = useCallback(() => {
    setPendingMoveConfirm(prev => prev ? { ...prev, seaRaidAwaitingZoneChoice: true } : null);
  }, []);

  const handleUnitMove = useCallback((_fromTerritory: string, _toTerritory: string, _unitType: string, _count: number) => {
    // Moves are now handled through handleConfirmMove
  }, []);

  const handleConfirmMove = useCallback(async (overrideToTerritory?: string) => {
    if (!pendingMoveConfirm || !backendState) return;

    const { fromTerritory, count, instanceIds } = pendingMoveConfirm;
    const toId = (v: unknown): string =>
      typeof v === 'string' ? v : (v != null && typeof v === 'object' && 'id' in (v as object) ? String((v as { id: string }).id) : (v != null && typeof v === 'object' && 'territoryId' in (v as object) ? String((v as { territoryId: string }).territoryId) : String(v ?? '')));
    const fromStr = toId(fromTerritory);
    const fromSea = currentTerritoryData[fromStr]?.terrain === 'sea' || /^sea_zone\d*$/i.test(fromStr);

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
      !/^sea_zone\d*$/i.test(storedTo);
    const isSeaRaid = gameState.phase === 'combat_move' && fromSea && toLand;
    const isOffload = gameState.phase === 'non_combat_move' && fromSea && toLand;
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

    let unitInstances: string[];
    if (instanceIds && instanceIds.length > 0) {
      unitInstances = instanceIds;
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

    if (!(instanceIds && instanceIds.length > 0) && unitInstances.length < count) {
      addLogEntry('Not enough units available (some already in other moves)', 'error');
      return;
    }

    // Use only primitives so JSON.stringify in api.move never sees cyclic refs from state/drag data
    const unitInstanceIds: string[] = Array.from(unitInstances, (id: unknown) =>
      typeof id === 'string' ? id : (id != null && typeof id === 'object' && 'instance_id' in id ? String((id as { instance_id: unknown }).instance_id) : '')
    ).filter(Boolean);
    const chargeThrough = Array.isArray(pendingMoveConfirm.chargeThrough)
      ? pendingMoveConfirm.chargeThrough.map((s: unknown) => (typeof s === 'string' ? s : String(s)))
      : undefined;
    // Use ref so we always have the latest allocation (drag updates can be one tick behind the confirm click)
    const loadAllocation = loadAllocationRef.current ?? pendingMoveConfirm.loadAllocation;
    const toSea = destination && (currentTerritoryData[destination]?.terrain === 'sea' || /^sea_zone\d*$/i.test(destination));

    try {
      // Sea->land with chosen sea zone: backend does sail + offload in one request.
      const needsSailThenOffload = (isSeaRaid || isOffload) && valid(storedTo) && toSea && destination !== fromStr;
      if (needsSailThenOffload) {
        const result = await api.move(
          String(GAME_ID),
          fromStr,
          storedTo,
          unitInstanceIds,
          chargeThrough,
          undefined,
          destination
        );
        if (result.need_offload_sea_choice && result.valid_offload_sea_zones?.length) {
          setPendingOffloadSeaChoice({
            from: fromStr,
            to: storedTo,
            unitInstanceIds,
            validSeaZones: result.valid_offload_sea_zones,
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
            boatInstanceId
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
      const result = await api.move(
        String(GAME_ID),
        fromStr,
        destination,
        unitInstanceIds,
        chargeThrough,
        loadOntoBoatId
      );
      if (result.need_offload_sea_choice && result.valid_offload_sea_zones?.length) {
        setPendingOffloadSeaChoice({
          from: fromStr,
          to: destination,
          unitInstanceIds,
          validSeaZones: result.valid_offload_sea_zones,
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
      const result = await api.move(
        String(GAME_ID),
        from,
        to,
        ids,
        undefined,
        undefined,
        chosenSeaZoneId
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
  }, [pendingOffloadSeaChoice, backendState?.phase, addLogEntry, addBackendEvents]);

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
      addLogEntry('Move cancelled', 'info');

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
      addLogEntry('Mobilization cancelled', 'info');
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
  const handleStartCombatRound = useCallback(async (casualtyOrder?: string, mustConquer?: boolean): Promise<{
    round: CombatRound;
    combatOver: boolean;
    attackerWon: boolean;
    terrorReroll?: { applied: boolean; instance_ids?: string[]; initial_rolls_by_instance?: Record<string, number[]>; defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>; defender_rerolled_indices_by_stat?: Record<string, number[]> };
  } | null> => {
    if (!activeCombat) return null;
    const isFirstRound = !backendState?.active_combat;
    try {
      const res = isFirstRound
        ? await api.initiateCombat(GAME_ID, activeCombat.territory, activeCombat.sea_zone_id)
        : await api.continueCombat(GAME_ID, { casualty_order: casualtyOrder, must_conquer: mustConquer });
      setBackendState(res.state);
      if (res.can_act !== undefined) setCanAct(res.can_act);
      if (res.events) addBackendEvents(res.events);

      // Refetch available-actions so retreat_options.valid_destinations is present when user chooses retreat
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);

      const roundEvents = (res.events || []).filter((e: { type: string }) => e.type === 'combat_round_resolved');
      const roundEvent = roundEvents.length === 0
        ? undefined
        : roundEvents.length === 1
          ? roundEvents[0]
          : roundEvents.find((e: { payload?: { round_number?: number } }) => e.payload?.round_number === 1) ?? roundEvents[roundEvents.length - 1];
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
        terror_applied?: boolean;
        attacker_units_at_start: BackendCombatUnit[];
        defender_units_at_start: BackendCombatUnit[];
      };

      const toRolls = (diceByStat: Record<string, { rolls: number[]; hits: number }>) => {
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

      const backendUnitToCombatUnit = (bu: BackendCombatUnit): CombatUnit => ({
        id: bu.instance_id,
        unitType: bu.unit_id,
        name: bu.display_name,
        icon: unitDefs[bu.unit_id]?.icon ?? `/assets/units/${bu.unit_id}.png`,
        attack: bu.attack,
        defense: bu.defense,
        ...(bu.effective_attack != null && { effectiveAttack: bu.effective_attack }),
        ...(bu.effective_defense != null && { effectiveDefense: bu.effective_defense }),
        isArcher: bu.is_archer ?? false,
        ...(bu.is_archer && { hasArcher: true }),
        health: bu.health,
        remainingHealth: bu.remaining_health,
        ...(bu.remaining_movement != null && { remainingMovement: bu.remaining_movement }),
        ...(factionData[bu.faction] && { factionColor: factionData[bu.faction].color }),
        ...(bu.terror && { hasTerror: true }),
        ...(bu.terrain_mountain && { terrainMountain: true }),
        ...(bu.terrain_forest && { terrainForest: true }),
        ...(bu.captain_bonus && { hasCaptainBonus: true }),
        ...(bu.anti_cavalry && { hasAntiCavalry: true }),
      });

      const round: CombatRound = {
        roundNumber: p.round_number,
        attackerRolls: toRolls(p.attacker_dice),
        defenderRolls: toRolls(p.defender_dice),
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
        terrorApplied: p.terror_applied ?? false,
        attackerUnitsAtStart: (Array.isArray(p.attacker_units_at_start) ? p.attacker_units_at_start : []).map(backendUnitToCombatUnit),
        defenderUnitsAtStart: (Array.isArray(p.defender_units_at_start) ? p.defender_units_at_start : []).map(backendUnitToCombatUnit),
      };

      const combatOver = !res.state.active_combat;
      const attackerWon = endEvent?.payload
        ? (endEvent.payload as { winner?: string }).winner === 'attacker'
        : (combatOver && (p as { defenders_remaining?: number }).defenders_remaining === 0);

      const terrorReroll = res.terror_reroll ?? undefined;
      return { round, combatOver, attackerWon: !!attackerWon, terrorReroll };
    } catch (err) {
      addLogEntry(`Combat round failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      return null;
    }
  }, [activeCombat, backendState?.active_combat, addBackendEvents, addLogEntry, unitDefs, factionData]);

  const handleCombatEnd = useCallback(async (result: 'attacker_wins' | 'defender_wins' | 'retreat') => {
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
    } else {
      addLogEntry(`Attack on ${territoryName} repelled!`, 'combat');
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

      setPendingMobilization(null);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Mobilization failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [pendingMobilization, addLogEntry, addBackendEvents]);

  const handleCloseMobilizationConfirm = useCallback(() => {
    setPendingMobilization(null);
  }, []);

  const handleMobilizationDrop = useCallback((territoryId: string, unitId: string, unitName: string, unitIcon: string, count: number) => {
    const purchase = mobilizablePurchases.find(p => p.unitId === unitId);
    if (!purchase) return;
    const remaining = remainingMobilizationCapacity[territoryId] ?? 0;
    const maxCount = Math.min(purchase.count, remaining);
    if (maxCount <= 0) return;
    setPendingMobilization({
      unitId,
      unitName,
      unitIcon,
      toTerritory: territoryId,
      maxCount,
      count: Math.min(count, maxCount),
    });
  }, [mobilizablePurchases, remainingMobilizationCapacity]);

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

  const ac = backendState?.active_combat as { territory_id?: string; sea_zone_id?: string; combat_log?: unknown[]; attacker_instance_ids?: string[] } | undefined;
  const acTerritoryId = ac?.territory_id;
  const acSeaZoneId = ac?.sea_zone_id;
  const acAttackerInstanceIds = ac?.attacker_instance_ids ?? [];

  // Effective combat: active player's open combat, or spectator's chosen battle (any declared battle can be opened to view units)
  const effectiveCombat = useMemo(() => {
    if (activeCombat) return activeCombat;
    return spectatingBattle ?? null;
  }, [activeCombat, spectatingBattle]);

  // Combat display props: used for ready phase (modal open, no round run yet) and for faction/retreat.
  // Once a round runs, CombatDisplay uses the round payload (attackerUnitsAtStart/defenderUnitsAtStart) as the single source of truth.
  const combatDisplayProps = useMemo(() => {
    if (!effectiveCombat || !backendState || !definitions) return null;

    const territory = currentTerritoryData[effectiveCombat.territory];
    const attackerFaction = gameState.current_faction;

    const backendTerritory = backendState.territories[effectiveCombat.territory];
    const initialAttackerUnits: CombatUnit[] = [];
    const initialDefenderUnits: CombatUnit[] = [];

    const isActiveCombat =
      acTerritoryId === effectiveCombat.territory &&
      (effectiveCombat.sea_zone_id == null ? !acSeaZoneId : acSeaZoneId === effectiveCombat.sea_zone_id);

    const combatStatModifiers = (backendState as { combat_stat_modifiers?: { attacker?: Record<string, number>; defender?: Record<string, number> } }).combat_stat_modifiers;
    const combatSpecials = (backendState as {
      combat_specials?: {
        attacker?: Record<string, { terror?: boolean; terrainMountain?: boolean; terrainForest?: boolean; captain?: boolean; antiCavalry?: boolean; seaRaider?: boolean; stealth?: boolean; bombikazi?: boolean }>;
        defender?: Record<string, { terror?: boolean; terrainMountain?: boolean; terrainForest?: boolean; captain?: boolean; antiCavalry?: boolean; archer?: boolean; fearless?: boolean; hope?: boolean }>;
      };
    }).combat_specials;
    const modsAttacker = combatStatModifiers?.attacker ?? {};
    const modsDefender = combatStatModifiers?.defender ?? {};
    const specialsAttacker = combatSpecials?.attacker ?? {};
    const specialsDefender = combatSpecials?.defender ?? {};

    const buildCombatUnit = (unit: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number }, isAttackerUnit: boolean): CombatUnit | null => {
      const unitDef = definitions.units[unit.unit_id];
      if (!unitDef) return null;
      const tags: string[] = (unitDef as { tags?: string[] }).tags ?? [];
      const archetype = (unitDef as { archetype?: string }).archetype ?? '';
      const isArcher = archetype === 'archer' || tags.includes('archer');
      const totalMod = isAttackerUnit ? (modsAttacker[unit.instance_id] ?? 0) : (modsDefender[unit.instance_id] ?? 0);
      const effectiveAttack = isAttackerUnit ? unitDef.attack + totalMod : undefined;
      const effectiveDefense = !isAttackerUnit ? unitDef.defense + totalMod : undefined;
      const specials = isAttackerUnit ? specialsAttacker[unit.instance_id] : specialsDefender[unit.instance_id];
      const unitFaction = (unitDef as { faction?: string }).faction;
      return {
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
        ...(unitFaction && unitDef.faction !== attackerFaction && { factionColor: factionData[unitFaction]?.color ?? undefined }),
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
      };
    };

    if (isActiveCombat && backendTerritory) {
      const isSeaRaid = Boolean(acSeaZoneId);
      const attackerIdSet = new Set(acAttackerInstanceIds);
      const isNavalUnit = (unitId: string) => {
        const def = definitions?.units?.[unitId] as { archetype?: string; tags?: string[] } | undefined;
        if (!def) return false;
        return (def.archetype ?? '') === 'naval' || (def.tags ?? []).includes('naval');
      };

      if (isSeaRaid) {
        // Sea raid: attackers are LAND units in the sea zone (passengers); defenders are on the LAND territory. Boats stay in sea and do not fight.
        const seaZone = backendState.territories[acSeaZoneId!] as { units?: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number }[] } | undefined;
        if (seaZone?.units) {
          for (const unit of seaZone.units) {
            if (!attackerIdSet.has(unit.instance_id) || isNavalUnit(unit.unit_id)) continue;
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
        // Land or naval combat: both sides in the same territory, split by faction.
        for (const unit of backendTerritory.units ?? []) {
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
      const isNavalForPreview = (unitId: string) => {
        const d = definitions?.units?.[unitId] as { archetype?: string; tags?: string[] } | undefined;
        return d && ((d.archetype ?? '') === 'naval' || (d.tags ?? []).includes('naval'));
      };
      const territories = backendState.territories || {};
      for (const tid of Object.keys(territories)) {
        const ter = territories[tid] as { units?: { instance_id: string; unit_id: string; remaining_health: number; remaining_movement?: number }[] };
        if (!ter?.units) continue;
        for (const unit of ter.units) {
          if (attackerIds.has(unit.instance_id)) {
            if (isSeaRaidPreview && isNavalForPreview(unit.unit_id)) continue;
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

    const defendingTerritoryOwnerFaction = (territory?.owner || '') as string;

    const acForRetreat = backendState?.active_combat as { attackers_have_rolled?: boolean; casualty_order_attacker?: string; must_conquer?: boolean } | undefined;
    const canRetreat =
      (acForRetreat ? acForRetreat.attackers_have_rolled !== false : true) && retreatOptions.length > 0;

    const territoryDefenderOrder = (backendState?.territory_defender_casualty_order ?? {})[effectiveCombat.territory] ?? 'best_unit';

    // When spectator and combat just ended: show result then auto-close. Infer winner from territory ownership.
    const combatEndResult =
      !canAct && spectatingBattle && effectiveCombat.territory === spectatingBattle.territory && !backendState?.active_combat
        ? (() => {
            const postTerritory = backendState.territories?.[effectiveCombat.territory] as { owner?: string } | undefined;
            const newOwner = postTerritory?.owner;
            const attackerWon = newOwner === attackerFaction;
            const defenderWon = newOwner === defendingTerritoryOwnerFaction;
            return { attackerWon, defenderWon };
          })()
        : null;

    const combatLog = isActiveCombat && Array.isArray(ac?.combat_log) ? ac.combat_log : undefined;

    return {
      territoryName: territory?.name || effectiveCombat.territory,
      attackerFaction,
      defendingTerritoryOwnerFaction,
      initialAttackerUnits,
      initialDefenderUnits,
      retreatOptions,
      canRetreat,
      casualtyPriorityAttacker: acForRetreat?.casualty_order_attacker ?? 'best_unit',
      casualtyPriorityDefender: territoryDefenderOrder,
      mustConquer: acForRetreat?.must_conquer ?? false,
      combatLog,
      combatEndResult,
    };
  }, [effectiveCombat, backendState, currentTerritoryData, gameState.current_faction, definitions, unitDefs, validRetreatDestinations, factionData, canAct, spectatingBattle, ac, acTerritoryId, acSeaZoneId, acAttackerInstanceIds]);

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

  const handleCombatClose = useCallback((attackerWon: boolean, _survivingAttackers?: unknown) => {
    if (spectatingBattle) {
      setSpectatingBattle(null);
      return;
    }
    handleCombatEnd(attackerWon ? 'attacker_wins' : 'defender_wins');
  }, [spectatingBattle, handleCombatEnd]);

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

  const isLobby = gameMeta?.status === 'lobby';
  const isMovementPhase = gameState.phase === 'combat_move' || gameState.phase === 'non_combat_move';
  const currentResources = gameState.faction_resources[gameState.current_faction] || {};
  const currentPower = currentResources.power || 0; // Keep for header display

  return (
    <div className="app">
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
        specials={definitions?.specials}
        specialsOrder={definitions?.specials_order ?? []}
        unitsBySpecial={unitsBySpecial}
      />

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
              isMovementPhase={isMovementPhase}
              isCombatMove={gameState.phase === 'combat_move'}
              isMobilizePhase={gameState.phase === 'mobilize'}
              hasMobilizationSelected={selectedMobilizationUnit !== null}
              validMobilizeTerritories={validMobilizeTerritories}
              validMobilizeSeaZones={validMobilizeSeaZones}
              navalUnitIds={navalUnitIds}
              remainingMobilizationCapacity={remainingMobilizationCapacity}
              remainingHomeSlots={remainingHomeSlots}
              onMobilizationDrop={handleMobilizationDrop}
              mobilizationTray={
                gameState.phase === 'mobilize' ? {
                  purchases: mobilizablePurchases,
                  pendingCamps: unplacedCamps.map(c => ({ campIndex: c.campIndex, options: c.options })),
                  factionColor: factionData[gameState.current_faction]?.color || '#3a6ea5',
                  selectedUnitId: selectedMobilizationUnit,
                  selectedCampIndex,
                  onSelectUnit: setSelectedMobilizationUnit,
                  onSelectCamp: setSelectedCampIndex,
                } : null
              }
              onCampDrop={gameState.phase === 'mobilize' ? handleCampDrop : undefined}
              validCampTerritories={selectedCampIndex !== null ? validCampTerritories : []}
              territoriesWithPendingCampPlacement={gameState.phase === 'mobilize' ? Array.from(territoriesWithPendingCampPlacement) : []}
              pendingMoveConfirm={pendingMoveConfirm}
              onSetPendingMove={setPendingMoveConfirm}
              onDropDestination={setDropDestination}
              pendingMoves={gameState.pending_moves}
              highlightedTerritories={highlightedTerritories}
              availableMoveTargets={availableActions?.moveable_units?.map(m => ({
                territory: m.territory,
                unit: m.unit,
                destinations: normalizeMoveDestinations(m.destinations),
                // Normalize so charge_routes is always a dict (dest id -> list of paths); backend may omit for non-cavalry
                charge_routes:
                  m.charge_routes && typeof m.charge_routes === 'object' && !Array.isArray(m.charge_routes)
                    ? m.charge_routes
                    : {},
              }))}
              aerialUnitsMustMove={aerialMustMove}
              loadedNavalMustAttackInstanceIds={availableActions?.loaded_naval_must_attack_instance_ids ?? []}
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
              pendingLoadBoatOptions={
                pendingMoveConfirm?.boatOptions &&
                  pendingMoveConfirm.boatOptions.length > 1 &&
                  navalTrayData?.seaZoneId === (typeof pendingMoveConfirm.toTerritory === 'string' ? pendingMoveConfirm.toTerritory : '')
                  ? pendingMoveConfirm.boatOptions
                  : undefined
              }
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
          style={sidebarCollapsed ? undefined : { width: sidebarWidth }}
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
              onSkipTurn={handleSkipTurn}
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
              onCancelPendingMove={handleCancelPendingMove}
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
            factionName: factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.name || combatDisplayProps.defendingTerritoryOwnerFaction,
            factionIcon: factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.icon || '',
            factionColor: factionData[combatDisplayProps.defendingTerritoryOwnerFaction]?.color || '#666',
            units: combatDisplayProps.initialDefenderUnits,
          }}
          retreatOptions={combatDisplayProps.retreatOptions}
          canRetreat={combatDisplayProps.canRetreat}
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
