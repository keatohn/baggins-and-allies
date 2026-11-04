/** Terrains that use the river rules. The stored name is what the map shows. */
const RIVER_TERRAINS = new Set(['river', 'lake']);

const SEA_ZONE_ID = /^sea_zone_?\d+$/i;

/** 'sea' or 'river' for water terrain. Lake uses the river domain. */
export function waterDomain(terrain: string | null | undefined): 'sea' | 'river' | null {
  const t = (terrain ?? '').trim().toLowerCase();
  if (t === 'sea') return 'sea';
  if (RIVER_TERRAINS.has(t)) return 'river';
  return null;
}

export function isRiverTerrain(terrain: string | null | undefined): boolean {
  return waterDomain(terrain) === 'river';
}

/** Water terrain, or a sea_zone_N id whose terrain has not loaded yet. */
export function isWaterTerrain(terrain: string | null | undefined, territoryId?: string): boolean {
  return waterDomain(terrain) != null || (territoryId != null && SEA_ZONE_ID.test(territoryId));
}
