import type { UnitForStats } from './components/StatsModals';

/** Heroes sit together after the rest. Unbuyable units lead their group, then cost, dice, attack, specials, name. */
export function compareUnitsForStats(a: UnitForStats, b: UnitForStats): number {
  if (a.hero !== b.hero) return a.hero ? 1 : -1;
  if (a.purchasable !== b.purchasable) return a.purchasable ? 1 : -1;
  if (a.cost !== b.cost) return a.cost - b.cost;
  if (a.dice !== b.dice) return a.dice - b.dice;
  if (a.attack !== b.attack) return a.attack - b.attack;
  if (a.specials.length !== b.specials.length) return a.specials.length - b.specials.length;
  const byName = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
  if (byName !== 0) return byName;
  return a.id.localeCompare(b.id);
}
