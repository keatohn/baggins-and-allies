/**
 * Battle-open roster reconstruction for the combat modal.
 *
 * Live territory state drops dead instance ids, so a player who reloads mid-battle (or a
 * spectator who opens mid-fight) only receives surviving units. The combat_log's per-round
 * `*_units_at_start` snapshots rebuild what live props cannot: every stack that ever
 * participated, with the effective stats and specials badges of the round they appear in.
 */

/** One unit from a combat_log / combat_round_resolved `*_units_at_start` snapshot. */
export interface BackendRoundUnit {
  instance_id: string;
  unit_id: string;
  display_name?: string;
  attack?: number;
  defense?: number;
  effective_attack?: number | null;
  effective_defense?: number | null;
  health?: number;
  remaining_health?: number;
  is_archer?: boolean;
  siegework_archetype?: boolean;
  faction?: string;
  passenger_count?: number;
  terror?: boolean;
  terrain_mountain?: boolean;
  terrain_forest?: boolean;
  captain_bonus?: boolean;
  king_bonus?: boolean;
  anti_cavalry?: boolean;
  sea_raider?: boolean;
  archer?: boolean;
  stealth?: boolean;
  bombikazi?: boolean;
  fearless?: boolean;
  hope?: boolean;
  light?: boolean;
  ram?: boolean;
}

/** Display unit for combat modal shelves (must match the shape CombatDisplay renders). */
export interface CombatUnit {
  id: string;
  unitType: string;
  name: string;
  icon: string;
  attack: number;
  defense: number;
  /** When set (e.g. terrain bonus), used for shelf grouping so unit and rolls stay on same row. */
  effectiveAttack?: number;
  /** When set (e.g. terrain bonus), used for shelf grouping so unit and rolls stay on same row. */
  effectiveDefense?: number;
  /** True if unit has archer special (tag or specials list); used to show only archers during prefire. */
  isArcher?: boolean;
  health: number;
  remainingHealth: number;
  /** Defender units only: border color for this unit's faction (not territory owner). */
  factionColor?: string;
  /** Unit's owning faction id (from definitions); used for result-banner plurality tinting. */
  factionId?: string;
  /** Attack only: unit has Terror. */
  hasTerror?: boolean;
  /** Unit is receiving mountain terrain bonus. */
  terrainMountain?: boolean;
  /** Unit is receiving forest terrain bonus. */
  terrainForest?: boolean;
  /** Unit is receiving captain +1 (not the captain himself). */
  hasCaptainBonus?: boolean;
  /** Unit is receiving king +1 (not the king himself). */
  hasKingBonus?: boolean;
  /** Unit is receiving anti-cavalry (pikes) +1. */
  hasAntiCavalry?: boolean;
  /** Attacker only: unit has Sea Raider and is in a sea raid (+1 attack). */
  hasSeaRaider?: boolean;
  /** Defender only: unit is archer (prefire). */
  hasArcher?: boolean;
  /** Attacker only: unit has Stealth (prefire when all attackers have it). */
  hasStealth?: boolean;
  /** Attacker only: unit has Bombikazi (self-destruct with bomb). */
  hasBombikazi?: boolean;
  /** Defender only: unit has Fearless (immune to terror), when attackers have terror. */
  hasFearless?: boolean;
  /** Defender only: unit has Hope (cancels 1 terror), when attackers have terror. */
  hasHope?: boolean;
  /** Unit has Light and the other side has terror, so that terror is fully cancelled. */
  hasLight?: boolean;
  /** Attacker only: unit has ram special (from backend round snapshot / combat_specials). */
  hasRam?: boolean;
  /** Siegework archetype (from backend); used to shelf non-rolling engines in standard combat. */
  siegeworkArchetype?: boolean;
  /** Embarked units on this naval unit (map/modal parity). */
  passengerCount?: number;
}

/** Payload specials flags → CombatUnit badge flags (same contract as the combat modal). */
const ROUND_UNIT_BADGE_FLAGS: Array<[keyof BackendRoundUnit, keyof CombatUnit]> = [
  ['terror', 'hasTerror'],
  ['terrain_mountain', 'terrainMountain'],
  ['terrain_forest', 'terrainForest'],
  ['captain_bonus', 'hasCaptainBonus'],
  ['king_bonus', 'hasKingBonus'],
  ['anti_cavalry', 'hasAntiCavalry'],
  ['sea_raider', 'hasSeaRaider'],
  ['archer', 'hasArcher'],
  ['stealth', 'hasStealth'],
  ['bombikazi', 'hasBombikazi'],
  ['fearless', 'hasFearless'],
  ['hope', 'hasHope'],
  ['light', 'hasLight'],
  ['ram', 'hasRam'],
];

function roundUnitBadgeFlags(bu: BackendRoundUnit): Partial<CombatUnit> {
  const out: Partial<CombatUnit> = {};
  for (const [payloadKey, unitKey] of ROUND_UNIT_BADGE_FLAGS) {
    const v = bu[payloadKey];
    if (typeof v === 'boolean') (out as Record<string, boolean>)[unitKey] = v;
  }
  return out;
}

/**
 * Apply a backend round-start snapshot to combat modal shelves. Display fields (icon, faction
 * color) come from the live/battle-open unit, but effective stats, archer membership, and
 * specials badges always come from the snapshot so unit rows line up with that round's dice
 * buckets exactly like the combat modal does. Returns null when the snapshot is missing
 * (legacy combat_log entries) so callers can fall back to reconstructing from live state.
 */
export function applyRoundStartUnitsAtStart(
  payload: BackendRoundUnit[] | undefined,
  liveUnits: CombatUnit[],
  snapshot: CombatUnit[] | null,
): CombatUnit[] | null {
  if (!Array.isArray(payload) || payload.length === 0) return null;
  const liveMap = new Map(liveUnits.map(u => [u.id, u]));
  const snapMap = new Map((snapshot ?? []).map(u => [u.id, u]));
  return payload.map(bu => {
    const base = liveMap.get(bu.instance_id) ?? snapMap.get(bu.instance_id);
    const fromPayload: Partial<CombatUnit> = {
      ...roundUnitBadgeFlags(bu),
      ...(typeof bu.effective_attack === 'number' ? { effectiveAttack: bu.effective_attack } : {}),
      ...(typeof bu.effective_defense === 'number' ? { effectiveDefense: bu.effective_defense } : {}),
      ...(typeof bu.is_archer === 'boolean' ? { isArcher: bu.is_archer } : {}),
      ...(typeof bu.siegework_archetype === 'boolean' ? { siegeworkArchetype: bu.siegework_archetype } : {}),
      ...(typeof bu.passenger_count === 'number' && bu.passenger_count > 0
        ? { passengerCount: bu.passenger_count }
        : {}),
    };
    if (base) return { ...base, ...fromPayload };
    return {
      id: bu.instance_id,
      unitType: bu.unit_id,
      name: bu.display_name || bu.unit_id,
      icon: `/assets/units/${bu.unit_id}.png`,
      attack: bu.attack ?? 0,
      defense: bu.defense ?? 0,
      health: bu.health ?? 1,
      remainingHealth: bu.remaining_health ?? 0,
      ...(bu.faction && { factionId: bu.faction }),
      ...fromPayload,
    };
  });
}

/**
 * Convert one side's union-of-round-start payload (see unionUnitsAtStartFromCombatLog) into
 * the battle-open display roster, appending any live unit the log never listed. Returns null
 * when the log carries no payload for this side (legacy entries) so callers keep their
 * live-derived fallback.
 */
export function buildBattleOpenRoster(
  payload: BackendRoundUnit[],
  liveUnits: CombatUnit[],
): CombatUnit[] | null {
  const converted = applyRoundStartUnitsAtStart(payload, liveUnits, null);
  if (!converted?.length) return null;
  const have = new Set(converted.map(u => u.id));
  const extras = liveUnits.filter(u => !have.has(u.id));
  return extras.length ? [...converted, ...extras] : converted;
}
