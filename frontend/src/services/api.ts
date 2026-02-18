/**
 * API service for communicating with the Baggins & Allies game backend.
 */

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000';

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
}

export interface ApiPendingMove {
  from_territory: string;
  to_territory: string;
  unit_instance_ids: string[];
  phase: string;
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
}

export interface ApiFactionStats {
  factions: Record<string, FactionStatEntry>;
  alliances: Record<string, FactionStatEntry>;
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

export interface ActionResponse {
  state: ApiGameState;
  events: ApiEvent[];
  dice_rolls?: {
    attacker: number[];
    defender: number[];
  };
}

export interface AvailableActionsResponse {
  faction: string;
  phase: string;
  can_end_phase: boolean;
  can_end_turn?: boolean;
  purchasable_units?: ApiPurchasableUnit[];
  /** Max units that can be mobilized this turn (purchase phase). Total purchased cannot exceed this. */
  mobilization_capacity?: number;
  /** Units already purchased this turn (purchase phase). */
  purchased_units_count?: number;
  moveable_units?: ApiMoveableUnit[];
  combat_territories?: ApiCombatTerritory[];
  active_combat?: ApiActiveCombat;
  retreat_options?: ApiRetreatOptions;
  mobilize_options?: ApiMobilizeOptions;
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
    max_reach: number;
    by_distance: Record<number, string[]>;
  };
}

export interface ApiCombatTerritory {
  territory_id: string;
  attacker_count: number;
  defender_count: number;
}

export interface ApiRetreatOptions {
  can_retreat: boolean;
  valid_destinations: string[];
}

export interface ApiMobilizeOptions {
  /** Territory IDs where faction can mobilize (strongholds they own). Backend sends "territories". */
  territories?: string[];
  available_strongholds?: string[];
  pending_units: ApiUnitStack[];
  capacity?: { total_capacity: number; territories: { territory_id: string; power: number }[] };
  total_capacity?: number;
}

export interface Definitions {
  units: Record<string, ApiUnitDefinition>;
  territories: Record<string, ApiTerritoryDefinition>;
  factions: Record<string, ApiFactionDefinition>;
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

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const token = getAuthToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers: { ...headers, ...(options?.headers as Record<string, string>) },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || 'API request failed');
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
  players: { player_id: string; faction_id: string | null }[];
}

export interface GameListItem {
  id: string;
  name: string;
  game_code: string | null;
  status: string;
  created_at: string | null;
}

export const api = {
  // Auth
  register: (email: string, username: string, password: string) =>
    fetchJson<AuthResponse>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, username, password }),
    }),
  login: (email: string, password: string) =>
    fetchJson<AuthResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  authMe: () => fetchJson<AuthPlayer>('/auth/me'),

  // Games (create, list, join)
  createGame: (name: string, isMultiplayer: boolean) =>
    fetchJson<{ game_id: string; game_code: string | null; name: string }>('/games/create', {
      method: 'POST',
      body: JSON.stringify({ name, is_multiplayer: isMultiplayer }),
    }),
  listGames: () => fetchJson<{ games: GameListItem[] }>('/games'),
  joinGame: (gameCode: string) =>
    fetchJson<{ game_id: string; name: string }>('/games/join', {
      method: 'POST',
      body: JSON.stringify({ game_code: gameCode.trim().toUpperCase() }),
    }),
  getGameMeta: (gameId: string) => fetchJson<GameMeta>(`/games/${gameId}/meta`),

  // Get static definitions
  getDefinitions: () => fetchJson<Definitions>('/definitions'),

  // Create a new game (legacy in-memory, no auth)
  createGameLegacy: (gameId: string) =>
    fetchJson<GameStateResponse>('/games', {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
    }),

  // Get game state
  getGame: (gameId: string) =>
    fetchJson<GameStateResponse>(`/games/${gameId}`),
  
  // Get available actions
  getAvailableActions: (gameId: string) =>
    fetchJson<AvailableActionsResponse>(`/games/${gameId}/available-actions`),
  
  // Purchase units
  purchase: (gameId: string, purchases: Record<string, number>) =>
    fetchJson<ActionResponse>(`/games/${gameId}/purchase`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, purchases }),
    }),
  
  // Move units (declares a pending move)
  move: (gameId: string, fromTerritory: string, toTerritory: string, unitInstanceIds: string[]) =>
    fetchJson<ActionResponse>(`/games/${gameId}/move`, {
      method: 'POST',
      body: JSON.stringify({
        game_id: gameId,
        from_territory: fromTerritory,
        to_territory: toTerritory,
        unit_instance_ids: unitInstanceIds,
      }),
    }),
  
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
  
  // Initiate combat
  initiateCombat: (gameId: string, territoryId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/combat/initiate`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId, territory_id: territoryId }),
    }),
  
  // Continue combat
  continueCombat: (gameId: string) =>
    fetchJson<ActionResponse>(`/games/${gameId}/combat/continue`, {
      method: 'POST',
      body: JSON.stringify({ game_id: gameId }),
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
};

export default api;
