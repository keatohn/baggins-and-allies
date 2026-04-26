type StackLike = { unit_id: string; count: number };

type UnitDefLike = { faction?: string; cost?: number } | undefined;
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

/** Shared stack sort: faction, then count (desc), then cost/power (desc), then unit_id. */
export function compareUnitStacksByMapOrder(
  a: StackLike,
  b: StackLike,
  unitDefs: Record<string, UnitDefLike>,
  factionData: Record<string, FactionLike>,
): number {
  const fa = factionKeyForUnitType(a.unit_id, unitDefs, factionData);
  const fb = factionKeyForUnitType(b.unit_id, unitDefs, factionData);
  if (fa !== fb) return fa.localeCompare(fb);
  if (b.count !== a.count) return b.count - a.count;
  const costA = unitDefs[a.unit_id]?.cost ?? 0;
  const costB = unitDefs[b.unit_id]?.cost ?? 0;
  if (costB !== costA) return costB - costA;
  return a.unit_id.localeCompare(b.unit_id);
}
