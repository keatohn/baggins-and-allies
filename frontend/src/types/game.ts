// Game state types

export type FactionId = string;
export type Alliance = string;

// Note: collect_income happens automatically at end of turn
export type GamePhase =
  | 'purchase'
  | 'combat_move'
  | 'combat'
  | 'non_combat_move'
  | 'mobilize';

export interface FactionDefinition {
  id: string;
  name: string;
  alliance: string;
  color: string;
  icon: string;
}

export interface UnitDefinition {
  id: string;
  name: string;
  display_name: string;
  cost: number;
  attack: number;
  defense: number;
  movement: number;
  health: number;
  dice: number;
  unit_type: 'land' | 'air' | 'naval';
  icon: string;
}

export interface TerritoryDefinition {
  id: string;
  name: string;
  terrain: string;
  is_stronghold: boolean;
  is_capital: boolean;
  is_neutral: boolean;
  ownable: boolean;
  produces: Record<string, number>;
  adjacent: string[];
  original_owner?: FactionId;
}

export interface Unit {
  id: string;
  unit_type: string;
  faction: FactionId;
  remaining_health: number;
  remaining_movement: number;
}

export interface Territory {
  id: string;
  owner?: FactionId;
  units: Unit[];
}

// These must be defined before GameState since it references them
export interface PendingMove {
  id: string; // Unique ID for this move (e.g., "move_0")
  from: string; // From territory ID
  to: string; // To territory ID
  unitType: string; // e.g., 'gondor_infantry'
  count: number;
  phase: 'combat_move' | 'non_combat_move'; // Which phase this move was declared in
}

export interface PendingMobilization {
  id: string;
  destination: string;
  units: { unit_id: string; count: number }[];
}

export interface DeclaredBattle {
  territory: string;
  attacker_units: string[];
  defender_units: string[];
}

export interface GameState {
  turn_number: number;
  current_faction: FactionId;
  phase: GamePhase;
  territories: Record<string, Territory>;
  faction_resources: Record<FactionId, Record<string, number>>;
  pending_purchases: Record<string, number>;
  pending_moves: PendingMove[];
  pending_mobilizations: PendingMobilization[];
  declared_battles: DeclaredBattle[];
}

// UI State types
export interface SelectedUnit {
  territory: string;
  unitType: string;
  count: number;
}

export interface MapTransform {
  x: number;
  y: number;
  scale: number;
}

// Event types for logging
export interface GameEvent {
  id: string;
  type: string;
  message: string;
  timestamp: number;
}
