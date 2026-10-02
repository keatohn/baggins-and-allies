type StackLike = { unit_id: string; count: number };

type UnitDefLike = { faction?: string; cost?: number; hero_id?: string } | undefined;
type FactionLike = { name?: string; color?: string } | undefined;

/** Faction id for map/territory stack ordering. */
export function factionKeyForUnitType(
  unit_id: string,
  unitDefs: Record<string, UnitDefLike>,
  factionData: Record<string, FactionLike>,
): string {
  const parts = unit_id.split('_');
  const factionFromId = parts.find((p) => factionData[p]);
  const defFaction = unitDefs[unit_id]?.faction;
  return factionFromId ?? defFaction ?? parts[0] ?? '';
}

function isHeroUnit(unit_id: string, unitDefs: Record<string, UnitDefLike>): boolean {
  const heroId = unitDefs[unit_id]?.hero_id;
  return typeof heroId === 'string' && heroId.trim() !== '';
}

/** Shared stack sort: heroes first, then faction, count (desc), cost/power (desc), unit_id. */
export function compareUnitStacksByMapOrder(
  a: StackLike,
  b: StackLike,
  unitDefs: Record<string, UnitDefLike>,
  factionData: Record<string, FactionLike>,
): number {
  const heroA = isHeroUnit(a.unit_id, unitDefs);
  const heroB = isHeroUnit(b.unit_id, unitDefs);
  if (heroA !== heroB) return heroA ? -1 : 1;
  const fa = factionKeyForUnitType(a.unit_id, unitDefs, factionData);
  const fb = factionKeyForUnitType(b.unit_id, unitDefs, factionData);
  if (fa !== fb) return fa.localeCompare(fb);
  if (b.count !== a.count) return b.count - a.count;
  const costA = unitDefs[a.unit_id]?.cost ?? 0;
  const costB = unitDefs[b.unit_id]?.cost ?? 0;
  if (costB !== costA) return costB - costA;
  return a.unit_id.localeCompare(b.unit_id);
}
