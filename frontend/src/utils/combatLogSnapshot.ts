/**
 * Spectator (readOnly) combat modal reconstruction from the backend combat_log.
 *
 * Live territory state drops dead instance ids, so a spectator who opens a battle
 * mid-fight only receives surviving units. The log's per-round `*_units_at_start`
 * snapshots rebuild what live props cannot:
 * - the battle-open roster (union across rounds), used as the modal's battle-start
 *   snapshot so shelves show every stack that ever participated, and
 * - via that roster, cumulative fully-eliminated stacks (red-X ghosts) that
 *   interactive players accumulate in animateDiceReveals — a path spectators never run.
 */

/** Structural shape of one `*_units_at_start` payload unit (only the id is read here). */
export interface CombatLogRoundUnitLike {
  instance_id?: string;
}

type CombatLogEntryLike = {
  attacker_units_at_start?: unknown;
  defender_units_at_start?: unknown;
};

/**
 * Union of round-start roster payloads across combat_log entries, in first-seen order.
 *
 * The earliest entry listing a unit shows it as of its first participation (round 1 for
 * everyone at battle open), so the union is the battle-open roster plus any unit that
 * joined later (e.g. siegework equipment committed mid-battle). Returns empty arrays when
 * no entry carries payloads (legacy logs) — callers keep their live-derived fallback.
 */
export function unionUnitsAtStartFromCombatLog<T extends CombatLogRoundUnitLike>(
  log: readonly unknown[] | undefined | null,
): { attacker: T[]; defender: T[] } {
  const collect = (side: 'attacker' | 'defender'): T[] => {
    const out: T[] = [];
    if (!Array.isArray(log)) return out;
    const seen = new Set<string>();
    for (const raw of log) {
      if (!raw || typeof raw !== 'object') continue;
      const units = (raw as CombatLogEntryLike)[side === 'attacker' ? 'attacker_units_at_start' : 'defender_units_at_start'];
      if (!Array.isArray(units)) continue;
      for (const unit of units) {
        if (!unit || typeof unit !== 'object') continue;
        const id = (unit as { instance_id?: unknown }).instance_id;
        if (typeof id !== 'string' || !id || seen.has(id)) continue;
        seen.add(id);
        out.push(unit as T);
      }
    }
    return out;
  };
  return { attacker: collect('attacker'), defender: collect('defender') };
}
