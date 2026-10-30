// Game state types

export type FactionId = string;
export type Alliance = string;

// Note: territory income is credited to faction_resources when the faction ends their turn
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
  /** "load" | "offload" | "sail" for sea transport */
  move_type?: string | null;
  unit_instance_ids?: string[];
  load_onto_boat_instance_id?: string | null;
  /** From server: canonical unit_id for this declaration (do not parse instance id strings). */
  primary_unit_id?: string;
}

export interface PendingMobilization {
  id: string;
  destination: string;
  units: { unit_id: string; count: number }[];
}

export interface DeclaredBattle {
  territory: string;
  /** Set for sea raid: attackers are in this sea zone, territory is the land target. */
  sea_zone_id?: string;
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
  /** Queued camp placements (applied at end of mobilization phase). */
  pending_camp_placements: { camp_index: number; territory_id: string }[];
  declared_battles: DeclaredBattle[];
  /** Map asset filename for this game (e.g. "test_map.svg"). Omitted/null = legacy default. */
  map_asset?: string | null;
  /** Faction IDs in turn order (from setup). Empty/omitted = use alphabetical. */
  turn_order?: string[];
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

// Map-level icon/location overrides (admin-edited, shared by every setup using the map).
// Positions are SVG viewBox coordinates — the same space as the in-SVG hint_* circles.
export type MapSpotKind = 'unit' | 'marker' | 'center' | 'number' | 'icons' | 'logo';
export type MapOverrideFocus = 'calculated' | 'override';
export type MapOverridePoint = { x: number; y: number };

export interface MapOverrideTerritoryEntry {
  /** "calculated" = ignore overrides for this territory; "override" = use the listed positions (omitted kinds keep calculated). */
  focus: MapOverrideFocus;
  positions?: Partial<Record<MapSpotKind, MapOverridePoint>>;
}

export interface MapOverridesData {
  territories: Record<string, MapOverrideTerritoryEntry>;
}

/** Accept `[x, y]` or `{ x, y }`; anything else → null. */
export function normalizeMapOverridePoint(raw: unknown): MapOverridePoint | null {
  let x: unknown;
  let y: unknown;
  if (Array.isArray(raw) && raw.length === 2) {
    [x, y] = raw;
  } else if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const rec = raw as Record<string, unknown>;
    x = rec.x;
    y = rec.y;
  } else {
    return null;
  }
  if (typeof x !== 'number' || typeof y !== 'number' || !Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

export function emptyMapOverrides(): MapOverridesData {
  return { territories: {} };
}

/** Lenient read of a server payload into the canonical shape (drops malformed entries). */
export function normalizeMapOverrides(raw: unknown): MapOverridesData {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return emptyMapOverrides();
  const source = (raw as Record<string, unknown>).territories;
  if (!source || typeof source !== 'object' || Array.isArray(source)) return emptyMapOverrides();
  const out: Record<string, MapOverrideTerritoryEntry> = {};
  for (const [tid, entryRaw] of Object.entries(source as Record<string, unknown>)) {
    if (!entryRaw || typeof entryRaw !== 'object' || Array.isArray(entryRaw)) continue;
    const entry = entryRaw as Record<string, unknown>;
    if (entry.focus === 'calculated') {
      out[tid] = { focus: 'calculated' };
      continue;
    }
    if (entry.focus !== 'override') continue;
    const positions: Partial<Record<MapSpotKind, MapOverridePoint>> = {};
    if (entry.positions && typeof entry.positions === 'object' && !Array.isArray(entry.positions)) {
      for (const [kind, ptRaw] of Object.entries(entry.positions as Record<string, unknown>)) {
        const pt = normalizeMapOverridePoint(ptRaw);
        if (pt && (kind === 'unit' || kind === 'marker' || kind === 'center' || kind === 'number' || kind === 'icons' || kind === 'logo')) {
          positions[kind] = pt;
        }
      }
    }
    out[tid] = { focus: 'override', positions };
  }
  return { territories: out };
}

// Event types for logging (backend-enriched events include payload with turn_number, phase, faction, message)
export interface GameEvent {
  id: string;
  type: string;
  message: string;
  timestamp: number;
  /** Present when event came from backend; used for filtering by turn/phase/faction */
  payload?: {
    turn_number?: number;
    phase?: string;
    faction?: string;
    message?: string;
    [key: string]: unknown;
  };
}
