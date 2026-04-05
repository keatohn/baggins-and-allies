/**
 * Normalize sea zone id to sea_zone_n (matches backend / GameMap).
 */
export function canonicalSeaZoneId(tid: string): string {
  if (!tid || typeof tid !== 'string') return tid || '';
  const m = tid.trim().match(/^sea_zone_*(\d+)$/i);
  return m ? `sea_zone_${m[1]}` : tid.trim();
}

function isSeaTerrain(
  tid: string,
  territoryData: Record<string, { terrain?: string; adjacent?: string[] } | undefined>,
): boolean {
  const t = territoryData[tid];
  return (t?.terrain === 'sea') || /^sea_zone_?\d+$/i.test(tid);
}

function resolveDataKey(
  id: string,
  territoryData: Record<string, { terrain?: string; adjacent?: string[] } | undefined>,
): string | null {
  if (territoryData[id]) return id;
  const c = canonicalSeaZoneId(id);
  if (territoryData[c]) return c;
  return null;
}

/**
 * Sea zones reachable by sailing from fromSeaId (BFS over sea only), mirroring backend
 * get_sea_zones_reachable_by_sail: cannot expand through enemy-occupied sea, but may end in one.
 */
export function seaZonesReachableBySailFrom(
  fromSeaId: string,
  maxSteps: number,
  territoryData: Record<string, { terrain?: string; adjacent?: string[] } | undefined>,
  territoryUnits: Record<string, { unit_id: string }[] | undefined>,
  currentFaction: string,
  factionData: Record<string, { alliance?: string } | undefined>,
  unitDefs: Record<string, { faction?: string } | undefined>,
): Set<string> {
  const result = new Set<string>();
  const curAlliance = factionData[currentFaction]?.alliance ?? null;

  const stacksInSea = (seaTid: string) =>
    territoryUnits[seaTid] ||
    territoryUnits[canonicalSeaZoneId(seaTid)] ||
    [];

  const hasEnemyInSea = (seaTid: string): boolean => {
    const stacks = stacksInSea(seaTid);
    for (const s of stacks) {
      const uf = unitDefs[s.unit_id]?.faction;
      if (!uf) continue;
      if (uf === currentFaction) continue;
      const otherAlliance = factionData[uf]?.alliance ?? null;
      if (curAlliance != null && otherAlliance != null && otherAlliance !== curAlliance) {
        return true;
      }
    }
    return false;
  };

  const startKey = resolveDataKey(fromSeaId, territoryData);
  if (!startKey || !isSeaTerrain(startKey, territoryData)) {
    return result;
  }

  result.add(canonicalSeaZoneId(startKey));

  const queue: [string, number][] = [[startKey, 0]];
  const visited = new Set<string>([startKey]);

  while (queue.length > 0) {
    const [tid, steps] = queue.shift()!;
    const tdef = territoryData[tid];
    if (!tdef) continue;
    for (const adjRaw of tdef.adjacent || []) {
      const adjKey = resolveDataKey(adjRaw, territoryData);
      if (!adjKey || !isSeaTerrain(adjKey, territoryData)) continue;
      const newSteps = steps + 1;
      if (newSteps > maxSteps) continue;

      if (hasEnemyInSea(adjKey)) {
        result.add(canonicalSeaZoneId(adjKey));
        continue;
      }
      result.add(canonicalSeaZoneId(adjKey));
      if (!visited.has(adjKey)) {
        visited.add(adjKey);
        queue.push([adjKey, newSteps]);
      }
    }
  }
  return result;
}

/**
 * Sort sea_zone_N ids by numeric N (so sea_zone_2 comes before sea_zone_10).
 * Plain string sort would put sea_zone_10 before sea_zone_2.
 */
export function sortSeaZoneIdsByNumericSuffix(ids: string[]): string[] {
  const num = (id: string): number => {
    const m = String(id).trim().match(/^sea_zone_*(\d+)$/i);
    return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
  };
  return [...ids].sort((a, b) => num(a) - num(b) || a.localeCompare(b));
}
