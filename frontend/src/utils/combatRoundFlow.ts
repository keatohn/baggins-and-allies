/**
 * Combat modal round-request flow helpers.
 *
 * Continue computes the *prospective* cumulative casualty lists up-front and passes them into
 * the round request, but they are committed to state only after the request succeeds. A failed
 * request must leave the last resolved round (dice, hits, red Xs) on screen so the user can
 * retry — folding or clearing first wiped the displayed round and permanently folded casualties
 * for a round the UI never got a result for.
 */

/** Prior-round casualties plus the currently displayed round's casualties (ids are unique per battle). */
export function prospectiveCumulativeCasualties(
  previous: readonly string[],
  currentRoundCasualties: readonly string[] | undefined,
): string[] {
  return [...previous, ...(currentRoundCasualties ?? [])];
}

/**
 * Phase to restore when a round request fails: a displayed round returns to the decision state
 * (Continue / Retreat); with no round on screen the modal returns to ready (Start).
 */
export function phaseAfterFailedRoll(hasDisplayedRound: boolean): 'ready' | 'awaiting_decision' {
  return hasDisplayedRound ? 'awaiting_decision' : 'ready';
}

/** Display fields a roster unit must expose to be grouped into eliminated stacks (structural subset of the modal's unit shape). */
export interface StackSourceUnit {
  id: string;
  unitType: string;
  name: string;
  icon: string;
  health: number;
  attack: number;
  defense: number;
  effectiveAttack?: number;
  effectiveDefense?: number;
  factionColor?: string;
  factionId?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasKingBonus?: boolean;
  hasAntiCavalry?: boolean;
  hasSeaRaider?: boolean;
  hasArcher?: boolean;
  hasStealth?: boolean;
  hasBombikazi?: boolean;
  hasFearless?: boolean;
  hasHope?: boolean;
  hasLight?: boolean;
  hasRam?: boolean;
}

/** Cumulative eliminated stack (all units in this group died). Shown with red X, no rolls. */
export interface EliminatedStack {
  unitType: string;
  /** Must match the shelf group key (`unitType::factionId|factionColor::health`) so an eliminated stack never duplicates a roster row. */
  unitKey?: string;
  name: string;
  icon: string;
  health: number;
  statValue: number;
  count: number;
  factionColor?: string;
  factionId?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasKingBonus?: boolean;
  hasAntiCavalry?: boolean;
  hasSeaRaider?: boolean;
  hasArcher?: boolean;
  hasStealth?: boolean;
  hasBombikazi?: boolean;
  hasFearless?: boolean;
  hasHope?: boolean;
  hasLight?: boolean;
  hasRam?: boolean;
}

/** Shelf stat used to place an attacker stack (effective stat when the round supplied one). */
export const attackerStackStat = (u: StackSourceUnit): number => u.effectiveAttack ?? u.attack;

/** Shelf stat used to place a defender stack (effective stat when the round supplied one). */
export const defenderStackStat = (u: StackSourceUnit): number => u.effectiveDefense ?? u.defense;

/**
 * Group a roster by (stat, unit type, faction) and emit one stack per group whose members
 * are all casualties — the cumulative red-X ghosts shown in later rounds. Groups that are
 * only partially dead are omitted: survivors stay on the shelf and the dead members show
 * as casualty counts. `unitKey` uses the same format as the shelf's own group keys, so
 * callers rendering these stacks alongside a roster skip the duplicate row.
 */
export function fullyEliminatedStacks(
  units: readonly StackSourceUnit[],
  casualtyIds: readonly string[],
  getStat: (unit: StackSourceUnit) => number,
): EliminatedStack[] {
  const casualtySet = new Set(casualtyIds);
  const groups = new Map<string, StackSourceUnit[]>();
  for (const unit of units) {
    const key = `${getStat(unit)}_${unit.unitType}_${unit.factionId ?? unit.factionColor ?? ''}`;
    const existing = groups.get(key);
    if (existing) existing.push(unit);
    else groups.set(key, [unit]);
  }
  const out: EliminatedStack[] = [];
  groups.forEach(group => {
    const dead = group.filter(u => casualtySet.has(u.id));
    if (dead.length !== group.length || dead.length === 0) return;
    const u = group[0];
    out.push({
      unitType: u.unitType,
      unitKey: `${u.unitType}::${u.factionId ?? u.factionColor ?? ''}::${u.health}`,
      name: u.name,
      icon: u.icon,
      health: u.health,
      statValue: getStat(u),
      count: group.length,
      factionColor: u.factionColor,
      factionId: u.factionId,
      hasTerror: u.hasTerror,
      terrainMountain: u.terrainMountain,
      terrainForest: u.terrainForest,
      hasCaptainBonus: u.hasCaptainBonus,
      hasKingBonus: u.hasKingBonus,
      hasAntiCavalry: u.hasAntiCavalry,
      hasSeaRaider: u.hasSeaRaider,
      hasArcher: u.hasArcher,
      hasStealth: u.hasStealth,
      hasBombikazi: u.hasBombikazi,
      hasFearless: u.hasFearless,
      hasHope: u.hasHope,
      hasLight: u.hasLight,
      hasRam: u.hasRam,
    });
  });
  return out;
}
