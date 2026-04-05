/**
 * Ford escort rules (mirrors backend): non-ford-crosser land units may only use ford shortcuts after
 * a ford crosser has a same-phase pending lead. A lead is a move with min ford edges ≥ 1 on some path,
 * or a direct river-ford pair (ford_adjacent only) even when a longer land detour exists.
 */

import { canonicalSeaZoneId } from './seaZoneSort';
import type { PendingMove } from './types/game';

export type TerritoryFordGraph = {
  adjacent?: string[];
  ford_adjacent?: string[];
};

/** Match backend resolve_territory_key / sea canonicalization for graph + unit lookups. */
export function resolveTerritoryGraphKey(
  raw: string,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
): string {
  const t = raw.trim();
  if (!t) return t;
  if (territoryData[t]) return t;
  const byLower = new Map<string, string>();
  for (const k of Object.keys(territoryData)) {
    byLower.set(k.toLowerCase(), k);
  }
  const lowerHit = byLower.get(t.toLowerCase());
  if (lowerHit) return lowerHit;
  const seaCanon = canonicalSeaZoneId(t);
  if (territoryData[seaCanon]) return seaCanon;
  const seaLower = byLower.get(seaCanon.toLowerCase());
  if (seaLower) return seaLower;
  return t;
}

export function isFordCrosser(ud: { specials?: string[]; tags?: string[] } | undefined): boolean {
  if (!ud) return false;
  return Boolean(ud.specials?.includes('ford_crosser') || ud.tags?.includes('ford_crosser'));
}

/**
 * Land units that pay ford escort: transportable only (matches backend is_transportable),
 * not aerial, not naval, not ford crosser.
 */
export function usesFordEscortBudget(ud: { archetype?: string; tags?: string[]; specials?: string[] } | undefined): boolean {
  if (!ud) return false;
  if (ud.archetype === 'aerial' || ud.tags?.includes('aerial')) return false;
  if (ud.archetype === 'naval' || ud.tags?.includes('naval')) return false;
  if (isFordCrosser(ud)) return false;
  if (!ud.tags?.includes('transportable')) return false;
  return true;
}

function hasAdjacentOnlyLandPath(
  origin: string,
  dest: string,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
): boolean {
  if (origin === dest) return true;
  const queue = [origin];
  const visited = new Set<string>([origin]);
  while (queue.length) {
    const tid = queue.shift()!;
    const t = territoryData[tid];
    if (!t) continue;
    for (const nxt of t.adjacent || []) {
      if (nxt === dest) return true;
      if (!visited.has(nxt)) {
        visited.add(nxt);
        queue.push(nxt);
      }
    }
  }
  return false;
}

/** Minimum ford-only edges on any land path; 0 if adjacent-only path exists; null if unreachable. */
export function minFordEdgesForLandMove(
  origin: string,
  dest: string,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
): number | null {
  if (origin === dest) return 0;
  if (hasAdjacentOnlyLandPath(origin, dest, territoryData)) return 0;
  const best = new Map<string, number>();
  const pq: [number, string][] = [[0, origin]];
  best.set(origin, 0);
  const INF = 1e9;
  while (pq.length) {
    pq.sort((a, b) => a[0] - b[0]);
    const [fc, tid] = pq.shift()!;
    if (best.get(tid) !== fc) continue;
    if (tid === dest) return fc;
    const tdef = territoryData[tid];
    if (!tdef) continue;
    const adjSet = new Set(tdef.adjacent || []);
    for (const nxt of tdef.adjacent || []) {
      const nfc = fc;
      if (nfc < (best.get(nxt) ?? INF)) {
        best.set(nxt, nfc);
        pq.push([nfc, nxt]);
      }
    }
    for (const nxt of tdef.ford_adjacent || []) {
      if (adjSet.has(nxt)) continue;
      const nfc = fc + 1;
      if (nfc < (best.get(nxt) ?? INF)) {
        best.set(nxt, nfc);
        pq.push([nfc, nxt]);
      }
    }
  }
  return best.has(dest) ? (best.get(dest) as number) : null;
}

type FordTerritory = TerritoryFordGraph & { terrain?: string };

/** True if O and D are linked by a ford_only edge (mirrors backend direct_ford_only_land_pair). */
export function directFordOnlyLandPair(
  a: string,
  b: string,
  territoryData: Record<string, FordTerritory | undefined>,
): boolean {
  const ak = resolveTerritoryGraphKey(a, territoryData);
  const bk = resolveTerritoryGraphKey(b, territoryData);
  const ta = territoryData[ak];
  const tb = territoryData[bk];
  if (!ta || !tb) return false;
  const isSeaId = (id: string) => {
    const d = territoryData[resolveTerritoryGraphKey(id, territoryData)];
    return Boolean(d?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(id));
  };
  if (isSeaId(ak) || isSeaId(bk)) return false;
  const adjA = new Set(ta.adjacent || []);
  const adjB = new Set(tb.adjacent || []);
  const fa = ta.ford_adjacent || [];
  const fb = tb.ford_adjacent || [];
  if (fa.includes(bk) && !adjA.has(bk)) return true;
  if (fb.includes(ak) && !adjB.has(ak)) return true;
  return false;
}

/** True if O→D is a ford escort declaration (mirrors backend ford_shortcut_requires_escort_lead). */
export function fordShortcutRequiresEscortLead(
  okey: string,
  dkey: string,
  territoryData: Record<string, FordTerritory | undefined>,
): boolean {
  const mf = minFordEdgesForLandMove(okey, dkey, territoryData);
  if (mf !== null && mf >= 1) return true;
  return directFordOnlyLandPair(okey, dkey, territoryData);
}

/**
 * Ford escort billing multiplier O→D (matches remainingFordEscortSlotsClient / backend land_move_ford_escort_cost).
 * Graph keys should already be resolved.
 */
export function fordEscortOdMultiplier(
  fromKey: string,
  destKey: string,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
): number {
  let minF = minFordEdgesForLandMove(fromKey, destKey, territoryData);
  if (minF === 0 && directFordOnlyLandPair(fromKey, destKey, territoryData)) minF = 1;
  if (minF === null || minF < 1) return 0;
  return minF;
}

function unitRowFaction(
  u: { unit_id: string; instance_id: string },
  unitDefs: Record<string, { faction?: string; tags?: string[] } | undefined>,
): string | null {
  const f = unitDefs[u.unit_id]?.faction;
  if (f) return f;
  const p = u.instance_id.indexOf('_');
  return p > 0 ? u.instance_id.slice(0, p) : null;
}

/**
 * Remaining ford escort slots at origin this phase (mirrors backend remaining_ford_escort_slots).
 */
export function remainingFordEscortSlotsClient(
  origin: string,
  phase: string,
  pendingMoves: PendingMove[] | undefined,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
  territoryUnitsFull: Record<string, { instance_id: string; unit_id: string }[] | undefined>,
  unitDefs: Record<
    string,
    | {
        specials?: string[];
        tags?: string[];
        archetype?: string;
        transport_capacity?: number;
        faction?: string;
      }
    | undefined
  >,
  currentFaction: string,
  excludeInstanceIds: Set<string>,
): number {
  const okey = resolveTerritoryGraphKey(origin, territoryData);
  const full = territoryUnitsFull[okey] ?? territoryUnitsFull[origin] ?? [];
  let cap = 0;
  for (const u of full) {
    const ud = unitDefs[u.unit_id];
    if (!isFordCrosser(ud)) continue;
    if (unitRowFaction(u, unitDefs) !== currentFaction) continue;
    cap += Number(ud?.transport_capacity ?? 0) || 0;
  }
  let used = 0;
  for (const pm of pendingMoves ?? []) {
    if (pm.phase !== phase) continue;
    if (resolveTerritoryGraphKey(pm.from, territoryData) !== okey) continue;
    const mt = pm.move_type;
    if (mt === 'load' || mt === 'offload' || mt === 'sail') continue;
    const toKey = resolveTerritoryGraphKey(pm.to, territoryData);
    const ids = (pm.unit_instance_ids ?? []).filter((id) => !excludeInstanceIds.has(id));
    if (!ids.length) continue;
    let minF = minFordEdgesForLandMove(okey, toKey, territoryData);
    if (minF === 0 && directFordOnlyLandPair(okey, toKey, territoryData)) minF = 1;
    if (minF === null || minF === 0) continue;
    const byId = new Map(full.map((x) => [x.instance_id, x]));
    let n = 0;
    for (const iid of ids) {
      const row = byId.get(iid);
      if (!row) continue;
      const uud = unitDefs[row.unit_id];
      if (isFordCrosser(uud)) continue;
      if (!usesFordEscortBudget(uud)) continue;
      n += 1;
    }
    used += n * minF;
  }
  return Math.max(0, cap - used);
}

export function pendingFordCrosserLeadFromOrigin(
  origin: string,
  phase: string,
  pendingMoves: PendingMove[] | undefined,
  territoryData: Record<string, TerritoryFordGraph | undefined>,
  unitDefs: Record<string, { specials?: string[]; tags?: string[]; archetype?: string } | undefined>,
  territoryUnitsFull: Record<string, { instance_id: string; unit_id: string }[] | undefined>,
): boolean {
  if (!pendingMoves?.length) return false;
  const originKey = resolveTerritoryGraphKey(origin, territoryData);
  for (const pm of pendingMoves) {
    if (pm.phase !== phase) continue;
    if (resolveTerritoryGraphKey(pm.from, territoryData) !== originKey) continue;
    const mt = pm.move_type;
    if (mt === 'load' || mt === 'offload' || mt === 'sail') continue;
    const to = pm.to;
    const toKeyForLead = resolveTerritoryGraphKey(to, territoryData);
    if (!fordShortcutRequiresEscortLead(originKey, toKeyForLead, territoryData)) continue;
    const iids = pm.unit_instance_ids ?? [];
    const pu = (pm.primary_unit_id || '').trim();
    if (!iids.length) {
      if (pu && isFordCrosser(unitDefs[pu])) return true;
      continue;
    }
    const units =
      territoryUnitsFull[originKey] ||
      territoryUnitsFull[origin] ||
      [];
    const byId = new Map(units.map(u => [u.instance_id, u]));
    for (const iid of iids) {
      const u = byId.get(iid);
      if (!u) continue;
      if (isFordCrosser(unitDefs[u.unit_id])) return true;
    }
    if (pu && isFordCrosser(unitDefs[pu]) && iids.length === 1) {
      return true;
    }
  }
  return false;
}
