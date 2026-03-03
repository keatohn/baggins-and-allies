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
import LobbyModal from './components/LobbyModal';
import './App.css';

export interface PendingMobilization {
  unitId: string;
  unitName: string;
  unitIcon: string;
  toTerritory: string;
  maxCount: number;
  count: number;
}

// Game ID from URL when opened via /game/:gameId; fallback for legacy/dev
const DEFAULT_GAME_ID = 'game_1';

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
  const [pendingMoveConfirm, setPendingMoveConfirm] = useState<PendingMoveConfirm | null>(null);
  const [activeCombat, setActiveCombat] = useState<DeclaredBattle | null>(null);
  const [pendingMobilization, setPendingMobilization] = useState<PendingMobilization | null>(null);
  const [selectedMobilizationUnit, setSelectedMobilizationUnit] = useState<string | null>(null);
  /** Index into pending_camps when placing a camp during mobilization. */
  const [selectedCampIndex, setSelectedCampIndex] = useState<number | null>(null);
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
  /** Game meta for lobby modal (when status is lobby) */
  const [gameMeta, setGameMeta] = useState<GameMeta | null>(null);
  const [lobbyDismissed, setLobbyDismissed] = useState(false);

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
    setLobbyDismissed(false);
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
    const defs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[] }> = {};
    for (const [id, unit] of Object.entries(definitions.units)) {
      const u = unit as { display_name: string; icon?: string; faction?: string; archetype?: string; tags?: string[] };
      defs[id] = {
        name: u.display_name,
        icon: `/assets/units/${u.icon || `${id}.png`}`,
        faction: u.faction,
        archetype: u.archetype,
        tags: u.tags,
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
      ownable: boolean;
    }> = {};
    for (const [id, territory] of Object.entries(definitions.territories)) {
      defs[id] = {
        name: territory.display_name,
        terrain: territory.terrain_type,
        stronghold: territory.is_stronghold,
        produces: (territory.produces?.power as number) || 0,
        adjacent: territory.adjacent,
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
    const campsStanding = Array.isArray(backendState.camps_standing) ? backendState.camps_standing : [];
    const dynamicCamps = backendState.dynamic_camps && typeof backendState.dynamic_camps === 'object' ? backendState.dynamic_camps : {};
    const factions = definitions?.factions ?? {};
    const territoryHasCamp = (tid: string) =>
      Object.values(dynamicCamps).includes(tid) ||
      campsStanding.some(
        (campId) => campsObj[campId] && (campsObj[campId] as { territory_id?: string }).territory_id === tid
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
      hasCamp: boolean;
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
          isCapital: territoryIsCapital(id),
        }
        : {
          name: id.replace(/_/g, ' '),
          owner: territory.owner as FactionId | undefined,
          terrain: 'land',
          stronghold: false,
          produces: 0,
          adjacent: [],
          hasCamp: territoryHasCamp(id),
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
      // Group units by unit_id
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

  /** Unit specials for display: tags that are specials (exclude ground, mounted), plus archetype-derived. "anti cavalry" with dash. Sorted alphabetically. */
  const getUnitSpecials = useCallback((u: { tags?: string[]; archetype?: string; specials?: string[] }) => {
    const out = new Set<string>();
    const exclude = new Set(['ground', 'mounted']);
    (u.tags || []).filter(t => !exclude.has(t)).forEach(t => out.add(t));
    if (u.archetype === 'archer') out.add('archer');
    if (u.archetype === 'cavalry') out.add('charging');
    (u.specials || []).forEach(s => out.add(s));
    const format = (s: string) => {
      const normalized = s.replace(/_/g, ' ').replace(/-/g, ' ').toLowerCase().trim();
      if (normalized === 'anti cavalry') return 'Anti-Cavalry';
      return s.replace(/_/g, ' ').replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    };
    return [...out].map(format).sort((a, b) => a.localeCompare(b));
  }, []);

  // All units grouped by faction (for Unit Stats modal), ordered by cost ascending
  const unitsByFaction = useMemo(() => {
    if (!definitions?.units || !unitDefs) return {};
    const byFaction: Record<string, Array<{ id: string; name: string; icon: string; cost: number; attack: number; defense: number; dice: number; movement: number; health: number; specials: string[] }>> = {};
    for (const [id, u] of Object.entries(definitions.units)) {
      const faction = u.faction;
      if (!byFaction[faction]) byFaction[faction] = [];
      const cost = typeof u.cost === 'object' && u.cost?.power != null ? u.cost.power : 0;
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
        specials: getUnitSpecials(u),
      });
    }
    for (const fid of Object.keys(byFaction)) {
      byFaction[fid].sort((a, b) => a.cost - b.cost);
    }
    return byFaction;
  }, [definitions, unitDefs, getUnitSpecials]);

  // Purchasable units for current faction
  const availableUnits = useMemo(() => {
    if (!availableActions?.purchasable_units) return [];
    return availableActions.purchasable_units.map(u => ({
      id: u.unit_id,
      name: u.display_name || u.unit_id,
      icon: unitDefs[u.unit_id]?.icon || `/assets/units/${u.unit_id}.png`,
      cost: u.cost || {},
      attack: u.attack,
      defense: u.defense,
      movement: u.movement,
      health: u.health,
      dice: u.dice ?? 1,
    }));
  }, [availableActions, unitDefs]);

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

  // Refresh game state and available actions from backend
  const refreshState = useCallback(async () => {
    try {
      const [stateRes, actionsRes] = await Promise.all([
        api.getGame(GAME_ID),
        api.getAvailableActions(GAME_ID),
      ]);
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
  }, []);

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

    // Build declared battles from combat territories in available actions
    const declaredBattles: DeclaredBattle[] = (availableActions?.combat_territories || []).map(ct => ({
      territory: ct.territory_id,
      attacker_units: [],
      defender_units: [],
    }));

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
  }, [backendState, availableActions]);

  // Per-territory mobilization cap (units ≤ territory's power production)
  const mobilizationTerritoryPower = useMemo(() => {
    const list = availableActions?.mobilize_options?.capacity?.territories;
    if (!Array.isArray(list)) return {} as Record<string, number>;
    return Object.fromEntries(
      list.map((t: { territory_id: string; power: number }) => [t.territory_id, t.power ?? 0])
    );
  }, [availableActions?.mobilize_options?.capacity?.territories]);

  // Backend valid territories for mobilization (owned territories with a camp). From available-actions only.
  const validMobilizeTerritories = useMemo(
    () => availableActions?.mobilize_options?.territories ?? availableActions?.mobilize_options?.available_strongholds ?? [],
    [availableActions]
  );

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

  // --- Action Handlers ---

  const handleTerritorySelect = useCallback((territoryId: string | null) => {
    // Camp placement: click a valid territory → set pending (confirm/cancel in sidebar, like units)
    if (gameState.phase === 'mobilize' && selectedCampIndex !== null && territoryId && validCampTerritories.includes(territoryId)) {
      setPendingCampPlacement({ campIndex: selectedCampIndex, territoryId });
      return;
    }
    if (gameState.phase === 'mobilize' && selectedMobilizationUnit && territoryId) {
      if (validMobilizeTerritories.includes(territoryId)) {
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
  }, [gameState.phase, selectedCampIndex, validCampTerritories, selectedMobilizationUnit, mobilizablePurchases, validMobilizeTerritories, remainingMobilizationCapacity, addLogEntry, refreshState]);

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
    (gameState.phase === 'non_combat_move' && availableActions?.can_end_phase === false);
  const endPhaseDisabledReason =
    gameState.phase === 'combat'
      ? 'Resolve all battles before ending combat phase'
      : gameState.phase === 'mobilize'
        ? (mobilizablePurchases.length > 0 ? 'Deploy all purchased units before ending mobilization phase' : unplacedCamps.length > 0 ? 'Place all camps first (or click End phase to sync)' : undefined)
        : gameState.phase === 'non_combat_move' && aerialMustMove.length > 0
          ? 'Move all aerial units to friendly territory before ending phase'
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

  const handleUnitMove = useCallback((_fromTerritory: string, _toTerritory: string, _unitType: string, _count: number) => {
    // Moves are now handled through handleConfirmMove
  }, []);

  const handleConfirmMove = useCallback(async () => {
    if (!pendingMoveConfirm || !backendState) return;

    const { fromTerritory, toTerritory, count } = pendingMoveConfirm;

    // Get unit instance IDs for the move
    const territory = backendState.territories[fromTerritory];
    if (!territory) return;

    // Exclude instance IDs already committed in other pending moves from this territory (same phase)
    const currentPhase = gameState.phase;
    const committedInstanceIds = new Set(
      (backendState.pending_moves || [])
        .filter((m: { from_territory: string; phase: string }) => m.from_territory === fromTerritory && m.phase === currentPhase)
        .flatMap((m: { unit_instance_ids: string[] }) => m.unit_instance_ids)
    );

    // Pick instances of this unit type that are not already in a pending move
    const unitInstances = territory.units
      .filter(u => u.unit_id === pendingMoveConfirm.unitId && !committedInstanceIds.has(u.instance_id))
      .slice(0, count)
      .map(u => u.instance_id);

    if (unitInstances.length < count) {
      addLogEntry('Not enough units available (some already in other moves)', 'error');
      return;
    }

    try {
      const result = await api.move(
        GAME_ID,
        fromTerritory,
        toTerritory,
        unitInstances,
        pendingMoveConfirm.chargeThrough
      );
      setBackendState(result.state);
      if (result.can_act !== undefined) setCanAct(result.can_act);
      if (result.events) addBackendEvents(result.events);

      // Track that moves were made this phase (for confirmation dialogs)
      if (gameState.phase === 'combat_move') {
        setHasCombatMovedThisPhase(true);
      } else if (gameState.phase === 'non_combat_move') {
        setHasNonCombatMovedThisPhase(true);
      }

      setPendingMoveConfirm(null);
      setSelectedUnit(null);

      // Refresh available actions
      const actionsRes = await api.getAvailableActions(GAME_ID);
      setAvailableActions(actionsRes);
    } catch (err) {
      addLogEntry(`Move failed: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
    }
  }, [pendingMoveConfirm, backendState, gameState.phase, addLogEntry, addBackendEvents]);

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
  const handleStartCombatRound = useCallback(async (): Promise<{
    round: { roundNumber: number; attackerRolls: Record<number, { value: number; target: number; isHit: boolean }[]>; defenderRolls: Record<number, { value: number; target: number; isHit: boolean }[]>; attackerHits: number; defenderHits: number; attackerCasualties: string[]; defenderCasualties: string[] };
    combatOver: boolean;
    attackerWon: boolean;
    terrorReroll?: { applied: boolean; instance_ids?: string[]; initial_rolls_by_instance?: Record<string, number[]>; defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>; defender_rerolled_indices_by_stat?: Record<string, number[]> };
  } | null> => {
    if (!activeCombat) return null;
    const isFirstRound = !backendState?.active_combat;
    try {
      const res = isFirstRound
        ? await api.initiateCombat(GAME_ID, activeCombat.territory)
        : await api.continueCombat(GAME_ID);
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

  // Combat display props: used for ready phase (modal open, no round run yet) and for faction/retreat.
  // Once a round runs, CombatDisplay uses the round payload (attackerUnitsAtStart/defenderUnitsAtStart) as the single source of truth.
  const combatDisplayProps = useMemo(() => {
    if (!activeCombat || !backendState || !definitions) return null;

    const territory = currentTerritoryData[activeCombat.territory];
    const attackerFaction = gameState.current_faction;

    const backendTerritory = backendState.territories[activeCombat.territory];
    const initialAttackerUnits: CombatUnit[] = [];
    const initialDefenderUnits: CombatUnit[] = [];

    // Use backend as single source of truth for combat modifiers and specials (no parallel logic)
    const combatStatModifiers = (backendState as { combat_stat_modifiers?: { attacker?: Record<string, number>; defender?: Record<string, number> } }).combat_stat_modifiers;
    const combatSpecials = (backendState as { combat_specials?: { attacker?: Record<string, { terror?: boolean; terrainMountain?: boolean; terrainForest?: boolean; captain?: boolean; antiCavalry?: boolean }>; defender?: Record<string, { terror?: boolean; terrainMountain?: boolean; terrainForest?: boolean; captain?: boolean; antiCavalry?: boolean }> } }).combat_specials;
    const modsAttacker = combatStatModifiers?.attacker ?? {};
    const modsDefender = combatStatModifiers?.defender ?? {};
    const specialsAttacker = combatSpecials?.attacker ?? {};
    const specialsDefender = combatSpecials?.defender ?? {};

    if (backendTerritory) {
      for (const unit of backendTerritory.units) {
        const unitDef = definitions.units[unit.unit_id];
        if (!unitDef) continue;

        const tags: string[] = (unitDef as { tags?: string[] }).tags ?? [];
        const archetype = (unitDef as { archetype?: string }).archetype ?? '';
        const isArcher = archetype === 'archer' || tags.includes('archer');
        const isAttackerUnit = unitDef.faction === attackerFaction;
        const totalMod = isAttackerUnit ? (modsAttacker[unit.instance_id] ?? 0) : (modsDefender[unit.instance_id] ?? 0);
        const effectiveAttack = isAttackerUnit ? unitDef.attack + totalMod : undefined;
        const effectiveDefense = !isAttackerUnit ? unitDef.defense + totalMod : undefined;
        const specials = isAttackerUnit ? specialsAttacker[unit.instance_id] : specialsDefender[unit.instance_id];

        const unitFaction = (unitDef as { faction?: string }).faction;
        const combatUnit: CombatUnit = {
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
          ...(unitFaction && unitDef.faction !== attackerFaction && {
            factionColor: factionData[unitFaction]?.color ?? undefined,
          }),
          ...(specials?.terror && { hasTerror: true }),
          ...(specials?.terrainMountain && { terrainMountain: true }),
          ...(specials?.terrainForest && { terrainForest: true }),
          ...(specials?.captain && { hasCaptainBonus: true }),
          ...(specials?.antiCavalry && { hasAntiCavalry: true }),
        };

        if (unitDef.faction === attackerFaction) {
          initialAttackerUnits.push(combatUnit);
        } else {
          initialDefenderUnits.push(combatUnit);
        }
      }
    }

    // Sort by instance_id so order matches backend (dice grouping and shelves align)
    initialAttackerUnits.sort((a, b) => (a.id || '').localeCompare(b.id || ''));
    initialDefenderUnits.sort((a, b) => (a.id || '').localeCompare(b.id || ''));

    const retreatOptions = validRetreatDestinations.map(destId => ({
      territoryId: destId,
      territoryName: currentTerritoryData[destId]?.name || destId,
    }));

    // Defending territory (header/border) = faction that owns the territory
    const defendingTerritoryOwnerFaction = (territory?.owner || '') as string;

    const ac = backendState?.active_combat as { attackers_have_rolled?: boolean } | undefined;
    const canRetreat =
      (ac ? ac.attackers_have_rolled !== false : true) && retreatOptions.length > 0;

    return {
      territoryName: territory?.name || activeCombat.territory,
      attackerFaction,
      defendingTerritoryOwnerFaction,
      initialAttackerUnits,
      initialDefenderUnits,
      retreatOptions,
      canRetreat,
    };
  }, [activeCombat, backendState, currentTerritoryData, gameState.current_faction, definitions, unitDefs, validRetreatDestinations, factionData]);

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
    handleCombatEnd(attackerWon ? 'attacker_wins' : 'defender_wins');
  }, [handleCombatEnd]);

  const handleCombatCancel = useCallback(() => {
    setActiveCombat(null);
    setHighlightedTerritories([]);
  }, []);

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
              unitDefs={unitDefs}
              unitStats={unitStats}
              factionData={factionData}
              onTerritorySelect={handleTerritorySelect}
              onUnitSelect={handleUnitSelect}
              onUnitMove={handleUnitMove}
              isMovementPhase={isMovementPhase}
              isCombatMove={gameState.phase === 'combat_move'}
              isMobilizePhase={gameState.phase === 'mobilize'}
              hasMobilizationSelected={selectedMobilizationUnit !== null}
              validMobilizeTerritories={validMobilizeTerritories}
              remainingMobilizationCapacity={remainingMobilizationCapacity}
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
              pendingMoveConfirm={pendingMoveConfirm}
              onUpdateMoveCount={handleUpdateMoveCount}
              onConfirmMove={handleConfirmMove}
              onCancelMove={handleCancelMove}
              onChooseChargePath={handleChooseChargePath}
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
            />
          )}
        </div>
      </main>

      <PurchaseModal
        isOpen={isPurchaseModalOpen}
        factionColor={factionData[gameState.current_faction]?.color}
        availableResources={currentResources}
        availableUnits={availableUnits}
        currentPurchases={gameState.phase === 'purchase' ? purchaseCart : gameState.pending_purchases}
        currentCamps={gameState.phase === 'purchase' ? purchaseCampsCount : 0}
        maxCamps={maxCampsPurchasable}
        mobilizationCapacity={availableActions?.mobilization_capacity}
        purchasedUnitsCount={gameState.phase === 'purchase' ? Object.values(purchaseCart).reduce((s, q) => s + q, 0) : (availableActions?.purchased_units_count ?? 0)}
        campCost={availableActions?.camp_cost}
        onPurchase={handlePurchase}
        onClose={handleClosePurchase}
      />

      {/* Combat Display */}
      {combatDisplayProps && (
        <CombatDisplay
          isOpen={true}
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
          onStartRound={handleStartCombatRound}
          onRetreat={handleCombatRetreat}
          onClose={handleCombatClose}
          onCancel={handleCombatCancel}
          onHighlightTerritories={setHighlightedTerritories}
        />
      )}

      {gameMeta?.status === 'lobby' && gameMeta?.game_code != null && !lobbyDismissed && (
        <LobbyModal meta={gameMeta} onClose={() => setLobbyDismissed(true)} />
      )}
    </div>
  );
}

export default App;
