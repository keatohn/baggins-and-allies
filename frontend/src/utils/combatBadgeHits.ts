/**
 * Combat modal hit badges.
 *
 * One per-instance damage map drives every badge (flat stat shelves, defender
 * per-shelf badges, ladder segments) so all shelves share the same semantics:
 * cumulative "hits received" by a surviving stack for the whole battle.
 *
 * Sources per displayed unit (round-start payload roster is authoritative):
 *   - cumulative = prior + this round, where
 *       prior     = max HP − remainingHealth at round start (payload, never live state)
 *       this round = live roster delta, counted only when the round payload lists the
 *                    unit as wounded (≥ 1, so ring HP-shield wounds that cost no health
 *                    still badge; a payload with no wounded list — legacy combat-log
 *                    entries — falls back to the live delta)
 *   - casualties never badge; they render a red X instead.
 */

/** Minimal unit shape needed to compute badges (structural subset of CombatUnit). */
export interface BadgeUnit {
  id: string;
  unitType: string;
  /** Max HP. */
  health: number;
  /** Remaining HP in this roster snapshot. */
  remainingHealth: number;
  defense: number;
  effectiveDefense?: number;
}

export interface BadgeHitsContext {
  /** Displayed roster for the round (round-start snapshot, incl. prior eliminated ghosts). */
  units: BadgeUnit[];
  /** Live game-state roster; only used for this-round damage deltas. */
  liveUnits: BadgeUnit[];
  /** Every instance id dead this battle, including casualties from the displayed round. */
  cumulativeCasualties: string[];
  /** Wounded instance ids for the displayed round; null when the payload has no wounded list. */
  wounded: string[] | null;
  /** False before dice reveal: badges then show only damage already suffered. */
  postRound: boolean;
}

export interface BadgeHits {
  /** Per surviving instance: cumulative hits received (ladder segment badges). */
  byInstanceId: Record<string, number>;
  /** Per unit type (flat stat-shelf badges). */
  byUnitType: Record<string, number>;
  /** Defender only: unit type nested by effective-defense shelf. */
  byShelf: Record<number, Record<string, number>>;
}

export function computeBadgeHits(ctx: BadgeHitsContext): BadgeHits {
  const { units, liveUnits, cumulativeCasualties, wounded, postRound } = ctx;
  const byInstanceId: Record<string, number> = {};
  const byUnitType: Record<string, number> = {};
  const byShelf: Record<number, Record<string, number>> = {};

  const casualties = new Set(cumulativeCasualties);
  const woundSet = wounded === null ? null : new Set(wounded);
  const liveById = new Map(liveUnits.map(u => [u.id, u]));

  for (const u of units) {
    if (casualties.has(u.id)) continue;
    const prior = Math.max(0, u.health - u.remainingHealth);
    let damage: number;
    if (!postRound) {
      damage = prior;
    } else {
      const live = liveById.get(u.id);
      const delta =
        live && live.remainingHealth < u.remainingHealth
          ? u.remainingHealth - live.remainingHealth
          : 0;
      if (woundSet === null) damage = prior + delta;
      else if (!woundSet.has(u.id)) damage = prior; // live state ahead of the displayed round
      else damage = prior + Math.max(1, delta); // ring HP-shield wounds cost no health
    }
    if (damage <= 0) continue;

    byInstanceId[u.id] = damage;
    byUnitType[u.unitType] = (byUnitType[u.unitType] ?? 0) + damage;
    const shelf = u.effectiveDefense ?? u.defense;
    if (!byShelf[shelf]) byShelf[shelf] = {};
    byShelf[shelf][u.unitType] = (byShelf[shelf][u.unitType] ?? 0) + damage;
  }

  return { byInstanceId, byUnitType, byShelf };
}
