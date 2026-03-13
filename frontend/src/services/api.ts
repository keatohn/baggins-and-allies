/**
 * API service for communicating with the Baggins & Allies game backend.
 */

const API_BASE =
  import.meta.env.VITE_API_URL ??
  (import.meta.env.DEV ? '/api' : 'http://localhost:8000');

const AUTH_TOKEN_KEY = 'baggins_auth_token';

export function getAuthToken(): string | null {
  return localStorage.getItem(AUTH_TOKEN_KEY);
}

export function setAuthToken(token: string | null): void {
  if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
  else localStorage.removeItem(AUTH_TOKEN_KEY);
}

// ===== Types =====

export interface GameStateResponse {
  game_id: string;
  state: ApiGameState;
  /** Faction IDs in turn order (from setup). Same as state.turn_order; top-level so UI never misses it. */
  turn_order?: string[] | null;
  /** Pending camps from state; top-level so tray never misses them. Same shape as state.pending_camps. */
  pending_camps?: ApiGameState['pending_camps'];
  /** This game's definitions snapshot (when loaded from DB). Use instead of /definitions when present. */
  definitions?: Definitions;
  /** True if the authenticated player is assigned to the current faction (can perform actions). */
  can_act?: boolean;
}

export interface ApiPendingMove {
  from_territory: string;
  to_territory: string;
  unit_instance_ids: string[];
  phase: string;
  /** "load" | "offload" | "sail" for sea transport; omitted for normal moves */
  move_type?: string | null;
}

export interface ApiPendingMobilization {
  destination: string;
  units: { unit_id: string; count: number }[];
}

export interface FactionStatEntry {
  territories: number;
  strongholds: number;
  power: number;
  power_per_turn: number;
  /** Currently alive unit instances for this faction */
  units?: number;
  /** Sum of power cost for all active units (UP = Unit power) */
  unit_power?: number;
}

export interface ApiFactionStats {
  factions: Record<string, FactionStatEntry>;
  alliances: Record<string, FactionStatEntry>;
  /** Strongholds with no owner (e.g. Moria). Shown as gray segment in header bar. */
  neutral_strongholds?: number;
}

export interface ApiGameState {
  turn_number: number;
  current_faction: string;
  phase: string;
  territories: Record<string, ApiTerritory>;
  faction_resources: Record<string, Record<string, number>>;
  faction_purchased_units: Record<string, ApiUnitStack[]>;
  pending_moves: ApiPendingMove[];
  pending_mobilizations?: ApiPendingMobilization[];
  active_combat: ApiActiveCombat | null;
  winner: string | null;
  faction_stats?: ApiFactionStats;
  /** Camp definition IDs that are still standing (destroyed when territory is captured). */
  camps_standing?: string[];
  /** Map asset filename for this game (e.g. "test_map.svg"). Omitted/null = legacy default. */
  map_asset?: string | null;
  /** Faction IDs in turn order (from setup). Empty/omitted = use alphabetical. */
  turn_order?: string[];
  /** Camps purchased this turn; must be placed during mobilization. Index = camp_index for place_camp. */
  pending_camps?: { territory_options: string[]; placed_territory_id?: string | null }[];
  /** Queued camp placements (applied at end of mobilization phase, like pending_mobilizations). */
  pending_camp_placements?: { camp_index: number; territory_id: string }[];
  /** Placed purchased camps: camp_id (e.g. purchased_camp_<territory_id>) -> territory_id. Used to show camp icon on map. */
  dynamic_camps?: Record<string, string>;
  /** Defender casualty order per territory: "best_unit" | "best_defense". */
  territory_defender_casualty_order?: Record<string, string>;
}

export interface ApiTerritory {
  owner: string | null;
  original_owner: string | null;
  units: ApiUnit[];
}

export interface ApiUnit {
  instance_id: string;
  unit_id: string;
  remaining_movement: number;
  remaining_health: number;
  base_movement: number;
  base_health: number;
  /** Sea transport: instance_id of naval unit carrying this unit (only for land units in sea). */
  loaded_onto?: string | null;
}

export interface ApiUnitStack {
  unit_id: string;
  count: number;
}

export interface ApiActiveCombat {
  attacker_faction: string;
  territory_id: string;
  attacker_instance_ids: string[];
  round_number: number;
  combat_log: ApiCombatRound[];
  /** False only after defender archer prefire until round 1 is run; retreat disallowed until then. */
  attackers_have_rolled?: boolean;
}

export interface ApiCombatRound {
  round_number: number;
  attacker_rolls: number[];
  defender_rolls: number[];
  attacker_hits: number;
  defender_hits: number;
  attacker_casualties: string[];
  defender_casualties: string[];
  attackers_remaining: number;
  defenders_remaining: number;
}

export interface ApiEvent {
  type: string;
  payload: Record<string, unknown>;
}

/** Round 1 terror: defenders forced to re-roll (attacker special). */
export interface TerrorRerollResponse {
  applied: boolean;
  instance_ids?: string[];
  initial_rolls_by_instance?: Record<string, number[]>;
  /** Initial defender dice by stat (before re-roll) for UI. Keys are stat numbers as strings. */
  defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>;
  /** Per-stat indices of dice that were re-rolled (for red X and re-rolled shelf). Keys are stat numbers as strings. */
  defender_rerolled_indices_by_stat?: Record<string, number[]>;
}

export interface ActionResponse {
  state: ApiGameState;
  events: ApiEvent[];
  /** True if the authenticated player can still perform actions (their faction's turn). */
  can_act?: boolean;
  /** When moving sea->land with multiple valid offload sea zones: client must resubmit with offload_sea_zone_id. */
  need_offload_sea_choice?: boolean;
  valid_offload_sea_zones?: string[];
  dice_rolls?: {
    attacker: number[];
    defender: number[];
  };
  /** Set when round 1 terror was applied (attackers with terror forced defender re-rolls). */
  terror_reroll?: TerrorRerollResponse;
}

export interface AvailableActionsResponse {
  faction: string;
  phase: string;
  can_end_phase: boolean;
  can_end_turn?: boolean;
  purchasable_units?: ApiPurchasableUnit[];
  /** Max units that can be mobilized this turn (purchase phase). Total purchased cannot exceed this. */
  mobilization_capacity?: number;
  /** Land mobilization capacity (camps + home slots). Land units purchased cannot exceed this. */
  mobilization_land_capacity?: number;
  /** Land capacity from camps only (excl. home). Used with cart to show denominator = camp + home slots from cart. */
  mobilization_camp_land_capacity?: number;
  /** Sea mobilization capacity (port-adjacent sea zones). Naval units purchased cannot exceed this. */
  mobilization_sea_capacity?: number;
  /** Units already purchased this turn (purchase phase). */
  purchased_units_count?: number;
  /** Power cost to purchase one camp (0 = camps not purchasable). */
  camp_cost?: number;
  moveable_units?: ApiMoveableUnit[];
  /** Aerial units in enemy territory that must move to friendly before ending non-combat move phase. */
  aerial_units_must_move?: { territory_id: string; unit_id: string; instance_id: string }[];
  /** Boat instance IDs that received a load this combat move and must attack before ending phase (per boat, not per sea zone). */
  loaded_naval_must_attack_instance_ids?: string[];
  combat_territories?: ApiCombatTerritory[];
  /** Sea raid options: land territories attackable from a friendly sea zone (no enemies there). */
  sea_raid_targets?: { territory_id: string; sea_zone_id: string }[];
  active_combat?: ApiActiveCombat;
  retreat_options?: ApiRetreatOptions;
  mobilize_options?: ApiMobilizeOptions;
  /** Pending camps to place (mobilization phase); fallback if state.pending_camps missing. */
  pending_camps?: { territory_options: string[]; placed_territory_id?: string | null }[];
}

export interface ApiPurchasableUnit {
  unit_id: string;
  display_name: string;
  cost: Record<string, number>;
  max_affordable: number;
  attack: number;
  defense: number;
  movement: number;
  health: number;
  dice?: number;
}

export interface ApiMoveableUnit {
  territory: string;
  unit: ApiUnit;
  destinations: {
    max_reach?: number;
    by_distance?: Record<number, string[]>;
  } | Record<string, number>;
  /** Cavalry charging: destination_id -> list of charge_through paths (empty enemy territory IDs). */
  charge_routes?: Record<string, string[][]>;
}

export interface ApiCombatTerritory {
  territory_id: string;
  attacker_count: number;
  defender_count: number;
  attacker_unit_ids?: string[];
  defender_unit_ids?: string[];
}

export interface ApiRetreatOptions {
  can_retreat: boolean;
  valid_destinations: string[];
}

export interface ApiMobilizeOptions {
  /** Territory IDs where faction can mobilize land units (owned territories with a camp). */
  territories?: string[];
  /** Sea zone IDs where faction can mobilize naval units (adjacent to an owned port). */
  sea_zones?: string[];
  available_strongholds?: string[];
  pending_units: ApiUnitStack[];
  capacity?: {
    total_capacity: number;
    territories: { territory_id: string; power: number }[];
    sea_zones?: { sea_zone_id: string; power: number }[];
  };
  total_capacity?: number;
}

export interface ApiCampDefinition {
  id: string;
  territory_id: string;
}

export interface ApiPortDefinition {
  id: string;
  territory_id: string;
}

export interface SpecialDefinition {
  name: string;
  description: string;
  /** Short code shown in combat modal when this special is active (e.g. T, M, FR). */
  display_code?: string;
}

export interface Definitions {
  units: Record<string, ApiUnitDefinition>;
  territories: Record<string, ApiTerritoryDefinition>;
  factions: Record<string, ApiFactionDefinition>;
  camps?: Record<string, ApiCampDefinition>;
  ports?: Record<string, ApiPortDefinition>;
  /** Unit special ability definitions (setup-specific). Key = special id. */
  specials?: Record<string, SpecialDefinition>;
  /** Display order for specials in the Specials modal. */
  specials_order?: string[];
}

export interface ApiUnitDefinition {
  id: string;
  display_name: string;
  faction: string;
  archetype: string;
  tags: string[];
  cost: Record<string, number>;
  attack: number;
  defense: number;
  movement: number;
  health: number;
  dice: number;
  purchasable: boolean;
  unique: boolean;
  icon?: string;
  transport_capacity?: number;
  downgrade_to?: string | null;
  specials?: string[];
  /** Single home territory (backward compat). */
  home_territory_id?: string | null;
  /** Multiple home territories (deploy 1 per territory per mobilization). */
  home_territory_ids?: string[] | null;
}

export interface ApiTerritoryDefinition {
  id: string;
  display_name: string;
  adjacent: string[];
  produces: Record<string, number>;
  terrain_type: string;
  is_stronghold: boolean;
  ownable: boolean;
}

export interface ApiFactionDefinition {
  id: string;
  display_name: string;
  alliance: string;
  color: string;
  capital: string;
  icon?: string;
}

// ===== API Functions =====
const AUTH_REQUEST_TIMEOUT_MS = 15000;

async function fetchWithTimeout(
  url: string,
  options: RequestInit & { timeoutMs?: number }
): Promise<Response> {
  const { timeoutMs, ...fetchOptions } = options;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(fetchOptions.headers as Record<string, string>),
  };
  if (getAuthToken()) headers['Authorization'] = `Bearer ${getAuthToken()}`;

  if (!timeoutMs) {
    return fetch(`${API_BASE}${url}`, { ...fetchOptions, headers });
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${API_BASE}${url}`, { ...fetchOptions, headers, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const token = getAuthToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const mergedHeaders = { ...(options?.headers as Record<string, string>), ...headers };
  const response = await fetch(`${API_BASE}${url}`, {
    ...options,
    credentials: 'include',
    headers: mergedHeaders,
  });

  if (!response.ok) {
    if (response.status === 401) setAuthToken(null);
    const text = await response.text();
    let message = response.statusText;
    try {
      const error = text ? JSON.parse(text) : {};
      const detail = error?.detail;
      message =
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? (detail as { msg?: string }[]).map((d) => d.msg).filter(Boolean).join('; ') || message
            : message;
    } catch {
      if (text && text.length < 200) message = text;
    }
    throw new Error(message || 'API request failed');
  }

  return response.json();
}

// Auth types
export interface AuthPlayer {
  id: string;
  email: string;
  username: string;
}

export interface AuthResponse {
  access_token: string;
  player: AuthPlayer;
}

export interface GameMeta {
  id: string;
  name: string;
  game_code: string | null;
  status: string;
  created_at: string | null;
  /** Host (creator) player id; only they can start the game. */
  created_by?: string | null;
  players: { player_id: string; faction_id: string | null }[];
  /** Lobby only: faction_id -> player_id (who claimed each faction). */
  lobby_claims?: Record<string, string>;
  /** Lobby: player_id -> username for display. */
  player_usernames?: Record<string, string>;
  /** Lobby: scenario chosen at create (display_name + context from manifest). */
  scenario?: { display_name: string; context?: Record<string, unknown> } | null;
}

export interface GameListItem {
  id: string;
  name: string;
  game_code: string | null;
  status: string;
  created_at: string | null;
  /** Host (creator) player id; only they can delete the game. */
  created_by?: string | null;
  turn_number?: number | null;
  phase?: string | null;
  current_faction?: string | null;
  current_faction_display_name?: string | null;
  current_faction_icon?: string | null;
  current_player_username?: string | null;
  /** Same as in-game header: alliances + neutral_strongholds for the list stronghold bar. */
  faction_stats?: ApiFactionStats | null;
  /** Lobby only: number of players in the game. */
  lobby_players?: number | null;
  /** Lobby only: number of factions claimed. */
  lobby_factions_claimed?: number | null;
  /** Lobby only: total factions (turn order length). */
  lobby_factions_total?: number | null;
}

/** Setup/scenario from GET /setups (manifest id, display_name, map_asset, optional context for UX). */
export interface SetupInfo {
  id: string;
  display_name: string;
  map_asset: string;
  context?: {
    year?: string;
    map?: string;
    faction_count?: number;
    factions?: string[];
  };
}

async function authFetchJson<T>(url: string, body: object): Promise<T> {
  let response: Response;
  try {
    response = await fetchWithTimeout(url, {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: AUTH_REQUEST_TIMEOUT_MS,
    });
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      throw new Error(`Request timed out. Is the backend running at ${API_BASE}?`);
    }
    throw err;
  }
  if (!response.ok) {
    const text = await response.text();
    let message = response.statusText;
    try {
      const error = text ? JSON.parse(text) : {};
      const detail = error?.detail;
      message =
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? (detail as { msg?: string }[]).map((d) => d.msg).filter(Boolean).join('; ') || message
            : message;
    } catch {
      if (text && text.length < 200) message = text;
    }
    throw new Error(message || 'API request failed');
  }
  return response.json();
}

export const api = {
  // Auth (with timeout so we don't hang if backend is down)
  register: (email: string, username: string, password: string) =>
    authFetchJson<AuthResponse>('/auth/register', { email, username, password }),
  login: (email: string, password: string) =>
    authFetchJson<AuthResponse>('/auth/login', { email, password }),
  authMe: () => fetchJson<AuthPlayer>('/auth/me'),

  // Games (create, list, join)
  getSetups: () =>
    fetchJson<{ setups: SetupInfo[] }>('/setups'),
  createGame: (name: string, isMultiplayer: boolean, setupId?: string) =>
    fetchJson<{ game_id: string; game_code: string | null; name: string; state?: ApiGameState; turn_order?: string[] }>('/games/create', {
      method: 'POST',
      body: JSON.stringify({
        name,
        is_multiplayer: isMultiplayer,
        ...(setupId != null && { setup_id: setupId }),
      }),
    }),
  listGames: () =>
    fetchJson<{ games: GameListItem[] }>(`/games?_=${Date.now()}`, { cache: 'no-store' }),
  joinGame: (gameCode: string) =>
    fetchJson<{ game_id: string; name: string }>('/games/join', {
      method: 'POST',
      body: JSON.stringify({ game_code: gameCode.trim().toUpperCase() }),
    }),
  getGameMeta: (gameId: string) => fetchJson<GameMeta>(`/games/${gameId}/meta`),
  claimFaction: (gameId: string, factionId: string, claim: boolean) =>
    fetchJson<{ lobby_claims: Record<string, string> }>(`/games/${gameId}/claim-faction`, {
      method: 'POST',
      body: JSON.stringify({ faction_id: factionId, claim }),
    }),
  startGame: (gameId: string) =>
    fetchJson<{ message: string; status: string }>(`/games/${gameId}/start`, {
      method: 'POST',
    }),
  forfeitGame: (gameId: string) =>
    fetchJson<{ message: string }>(`/games/${gameId}/forfeit`, {
      method: 'POST',
    }),

  // Get static definitions
  getDefinitions: () => fetchJson<Definitions>('/definitions'),

  // Create a new game (legacy in-memory, no auth)
  createGameLegacy: (gameId: string) =>
    fetchJson<GameStateResponse>('/games', {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),

  // Get game state (no-store so DB updates like map_asset are visible without reload)
  getGame: (gameId: string) =>
    fetchJson<GameStateResponse>(`/games/${gameId}`, { cache: 'no-store' }),

  deleteGame: (gameId: string) =>
    fetchJson<{ message: string }>(`/games/${gameId}`, { method: 'DELETE' }),
  
  // Get available actions
  getAvailableActions: (gameId: string) =>
    fetchJson<AvailableActionsResponse>(`/games/${gameId}/available-actions`),
  
  // Purchase units
  purchase: (gameId: string, purchases: Record<string, number>) =>
    fetchJson<ActionResponse>(`/games/${gameId}/purchase`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, purchases }),
    }),

  // Purchase one camp (purchase phase; cost from setup)
  purchaseCamp: (gameId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/purchase-camp`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),

  // Place a purchased camp on a territory (mobilization phase) — immediate. Prefer queueCampPlacement.
  placeCamp: (gameId: string, campIndex: number, territoryId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/place-camp`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, camp_index: campIndex, territory_id: territoryId }),
    }),

  // Queue a camp placement (applied at end of mobilization phase, like unit mobilizations)
  queueCampPlacement: (gameId: string, campIndex: number, territoryId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/queue-camp-placement`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, camp_index: campIndex, territory_id: territoryId }),
    }),

  // Cancel a queued camp placement
  cancelCampPlacement: (gameId: string, placementIndex: number) =>
    fetchJson<ActionResponse>(`/games/${gameId}/cancel-camp-placement`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, placement_index: placementIndex }),
    }),

  // Move units (declares a pending move). chargeThrough for cavalry charging (empty enemy territories to conquer).
  // loadOntoBoatInstanceId: when loading to sea, assign passengers only to this boat.
  // offloadSeaZoneId: when moving sea->land and multiple sea zones can offload, send the chosen one (after need_offload_sea_choice).
  move: (gameId: string, fromTerritory: string, toTerritory: string, unitInstanceIds: string[], chargeThrough?: string[], loadOntoBoatInstanceId?: string | null, offloadSeaZoneId?: string | null) => {
    const ids = Array.from(unitInstanceIds, (id: unknown) =>
      typeof id === 'string' ? id : (id != null && typeof id === 'object' && 'instance_id' in id ? String((id as { instance_id: unknown }).instance_id) : '')
    ).filter(Boolean);
    const toStr =
      typeof toTerritory === 'string'
        ? toTerritory
        : toTerritory != null && typeof toTerritory === 'object'
          ? String((toTerritory as { territoryId?: string; id?: string; territory_id?: string }).territoryId ?? (toTerritory as { id?: string }).id ?? (toTerritory as { territory_id?: string }).territory_id ?? '')
          : '';
    const fromStr =
      typeof fromTerritory === 'string'
        ? fromTerritory
        : fromTerritory != null && typeof fromTerritory === 'object'
          ? String((fromTerritory as { territoryId?: string; id?: string }).territoryId ?? (fromTerritory as { id?: string }).id ?? '')
          : '';
    const safeFrom = (typeof fromStr === 'string' && fromStr && fromStr !== '[object Object]') ? fromStr : (typeof fromTerritory === 'string' && fromTerritory !== '[object Object]' ? fromTerritory : '');
    const safeTo = (typeof toStr === 'string' && toStr && toStr !== '[object Object]') ? toStr : (typeof toTerritory === 'string' && toTerritory !== '[object Object]' ? toTerritory : '');
    if (!safeTo.trim()) {
      throw new Error('No destination specified');
    }
    if (!safeFrom.trim()) {
      throw new Error('No origin specified');
    }
    const body: Record<string, unknown> = {
      game_id: String(gameId),
      from_territory: safeFrom,
      to_territory: safeTo,
      unit_instance_ids: ids,
    };
    if (chargeThrough && chargeThrough.length > 0) {
      body.charge_through = Array.from(chargeThrough, (s: unknown) => typeof s === 'string' ? s : String(s));
    }
    if (loadOntoBoatInstanceId != null && loadOntoBoatInstanceId !== '') {
      body.load_onto_boat_instance_id = String(loadOntoBoatInstanceId);
    }
    if (offloadSeaZoneId != null && offloadSeaZoneId !== '') {
      body.offload_sea_zone_id = String(offloadSeaZoneId);
    }
    return fetchJson<ActionResponse>(`/games/${gameId}/move`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },
  
  // Cancel a pending move
  cancelMove: (gameId: string, moveIndex: number) =>
    fetchJson<ActionResponse>(`/games/${gameId}/cancel-move`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, move_index: moveIndex }),
    }),

  cancelMobilization: (gameId: string, mobilizationIndex: number) =>
    fetchJson<ActionResponse>(`/games/${gameId}/cancel-mobilization`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, mobilization_index: mobilizationIndex }),
    }),
  
  // Initiate combat (seaZoneId required for sea raid: attackers in sea zone, target = territoryId land)
  initiateCombat: (gameId: string, territoryId: string, seaZoneId?: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/combat/initiate`, {
      method: 'POST',
      body: JSON.stringify({
        game_id: gameId,
        territory_id: territoryId,
        ...(seaZoneId != null && seaZoneId !== '' && { sea_zone_id: seaZoneId }),
      }),
    }),
  
  // Continue combat (optional casualty_order: "best_unit" | "best_attack", must_conquer: boolean)
  continueCombat: (gameId: string, options?: { casualty_order?: string; must_conquer?: boolean }) =>
    fetchJson<ActionResponse>(`/games/${gameId}/combat/continue`, {
      method: 'POST',
      body: JSON.stringify({
        game_id: gameId,
        ...(options?.casualty_order != null && { casualty_order: options.casualty_order }),
        ...(options?.must_conquer != null && { must_conquer: options.must_conquer }),
      }),
    }),
  
  // Set defender casualty order for a territory (owner only, any phase)
  setTerritoryDefenderCasualtyOrder: (gameId: string, territoryId: string, casualtyOrder: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/set-territory-defender-casualty-order`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, territory_id: territoryId, casualty_order: casualtyOrder }),
    }),

  // Retreat from combat
  retreat: (gameId: string, retreatTo: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/combat/retreat`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, retreat_to: retreatTo }),
    }),
  
  // Mobilize units
  mobilize: (gameId: string, destination: string, units: { unit_id: string; count: number }[]) =>
    fetchJson<ActionResponse>(`/games/${gameId}/mobilize`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, destination, units }),
    }),
  
  // End phase
  endPhase: (gameId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/end-phase`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),
  
  // End turn
  endTurn: (gameId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/end-turn`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),

  /** Force end current faction's turn (advances to next; empty factions get turn_skipped). Used by forfeit when player leaves on their turn. Remove only the Skip Turn button for production; keep this endpoint. */
  skipTurn: (gameId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/skip-turn`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),
};

export default api;
