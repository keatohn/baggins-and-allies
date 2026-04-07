import type { GameEvent } from '../types/game';

/** Same as map canonicalization — keeps sea_zone_9 vs sea_zone9 in one merge bucket. */
function canonicalTerritoryIdForLog(id: string): string {
  if (!id || typeof id !== 'string') return '';
  const t = id.trim();
  const m = t.match(/^sea_zone_*(\d+)$/i);
  return m ? `sea_zone_${m[1]}` : t;
}

function territoryDisplay(id: string, territoryData: Record<string, { name?: string } | undefined>): string {
  const n = territoryData[id]?.name;
  return n && n.trim() ? n : id.replace(/_/g, ' ');
}

function unitDisplay(unitId: string, unitDefs: Record<string, { name?: string } | undefined>): string {
  const n = unitDefs[unitId]?.name;
  return n && n.trim() ? n : unitId.replace(/_/g, ' ');
}

function formatUnitStack(count: number, unitId: string, unitDefs: Record<string, { name?: string } | undefined>): string {
  if (count <= 0) return '';
  const name = unitDisplay(unitId, unitDefs);
  return count === 1 ? name : `${count} ${name}`;
}

/** Instance id → unit_id (same heuristics as backend event_messages). */
function instanceIdsToUnitCounts(
  unitIds: string[],
  unitDefs: Record<string, { name?: string } | undefined>,
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const iid of unitIds) {
    const parts = iid.split('_');
    let uid = parts.length > 2 ? parts.slice(1, -1).join('_') : parts.length > 1 ? parts[1] : 'unknown';
    if (!unitDefs[uid] && parts.length >= 2) uid = parts[1];
    counts[uid] = (counts[uid] ?? 0) + 1;
  }
  return counts;
}

function formatUnitsMovedMessage(
  payload: Record<string, unknown>,
  unitDefs: Record<string, { name?: string } | undefined>,
  territoryData: Record<string, { name?: string } | undefined>,
): string {
  const fromT = territoryDisplay(String(payload.from_territory ?? ''), territoryData);
  const toT = territoryDisplay(String(payload.to_territory ?? ''), territoryData);
  const unitIds = (payload.unit_ids as string[]) ?? [];
  const phase = String(payload.phase ?? '');
  const counts = instanceIdsToUnitCounts(unitIds, unitDefs);
  const stackStr = Object.entries(counts)
    .filter(([, c]) => c > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([uid, c]) => formatUnitStack(c, uid, unitDefs))
    .join(', ');
  const mt = String(payload.move_type ?? '');
  if (mt === 'load') {
    const nBoats = Math.max(1, Number(payload.load_boat_count) || 1);
    const shipWord = nBoats > 1 ? 'ships' : 'ship';
    return `Loaded ${stackStr} from ${fromT} onto ${shipWord} in ${toT}`;
  }
  if (mt === 'sail') return `Moved ${stackStr} from ${fromT} to ${toT}`;
  if (mt === 'offload') {
    const base = `Offloaded ${stackStr} from ${fromT} into ${toT}`;
    return phase === 'combat_move' ? `${base} for sea raid` : base;
  }
  if (phase === 'combat_move') return `Moved ${stackStr} to attack ${toT}`;
  return `Moved ${stackStr} from ${fromT} to ${toT}`;
}

function formatUnitsPurchasedMessage(
  payload: Record<string, unknown>,
  unitDefs: Record<string, { name?: string } | undefined>,
): string {
  const purchases = (payload.purchases as Record<string, number>) ?? {};
  const parts = Object.entries(purchases)
    .filter(([, c]) => (typeof c === 'number' ? c : 0) > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([uid, c]) => formatUnitStack(typeof c === 'number' ? c : 0, uid, unitDefs));
  if (parts.length === 0) return 'Purchased nothing';
  return `Purchased ${parts.join(', ')}`;
}

function formatUnitsMobilizedMessage(
  payload: Record<string, unknown>,
  unitDefs: Record<string, { name?: string } | undefined>,
  territoryData: Record<string, { name?: string } | undefined>,
): string {
  const territory = territoryDisplay(String(payload.territory ?? ''), territoryData);
  const units = (payload.units as { unit_id?: string }[]) ?? [];
  const counts: Record<string, number> = {};
  for (const u of units) {
    const uid = u?.unit_id && typeof u.unit_id === 'string' ? u.unit_id : 'unknown';
    counts[uid] = (counts[uid] ?? 0) + 1;
  }
  const stackStr = Object.entries(counts)
    .filter(([, c]) => c > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([uid, c]) => formatUnitStack(c, uid, unitDefs))
    .join(', ');
  return `Mobilized ${stackStr} to ${territory}`;
}

export function eventLogMergeGroupKey(e: GameEvent): string | null {
  if (e.type === 'units_purchased') {
    const p = e.payload ?? {};
    const fac = String(p.faction ?? '');
    const phase = String(p.phase ?? '');
    const turn = typeof p.turn_number === 'number' ? p.turn_number : -1;
    return `units_purchased|${turn}|${fac}|${phase}`;
  }
  if (e.type === 'combat_ended') {
    const p = e.payload ?? {};
    const terr = canonicalTerritoryIdForLog(String(p.territory ?? ''));
    const turn = typeof p.turn_number === 'number' ? p.turn_number : -1;
    const phase = String(p.phase ?? '');
    return `combat_ended|${turn}|${terr}|${phase}`;
  }
  if (e.type === 'units_moved') {
    const p = e.payload ?? {};
    const to = canonicalTerritoryIdForLog(String(p.to_territory ?? ''));
    const fac = String(p.faction ?? '');
    const phase = String(p.phase ?? '');
    const turn = typeof p.turn_number === 'number' ? p.turn_number : -1;
    // Combat move: one summary line per destination (same as non-combat intent); omit move_type so
    // multiple declarations into the same hex merge. Non–combat move keeps move_type in the key.
    if (phase === 'combat_move') {
      return `units_moved|${turn}|${fac}|combat_move|${to}`;
    }
    const mt = String(p.move_type ?? '');
    return `units_moved|${turn}|${fac}|${phase}|${to}|${mt}`;
  }
  if (e.type === 'units_mobilized') {
    const p = e.payload ?? {};
    const terr = canonicalTerritoryIdForLog(String(p.territory ?? ''));
    const fac = String(p.faction ?? '');
    const phase = String(p.phase ?? '');
    const turn = typeof p.turn_number === 'number' ? p.turn_number : -1;
    return `units_mobilized|${turn}|${fac}|${phase}|${terr}`;
  }
  return null;
}

/**
 * Collapse duplicate summary lines that share faction + turn + phase (+ destination for moves):
 * - units_purchased → one line per purchase phase (merged counts)
 * - units_moved → per destination; combat_move merges all declarations into the same hex
 * - units_mobilized → per destination
 * - combat_ended → one line per battle territory (same turn + combat phase)
 * Newest-first log order: merged row appears at the first (newest) slot for that group.
 */
export function mergeGroupedEventLogForDisplay(
  log: GameEvent[],
  unitDefs: Record<string, { name?: string } | undefined>,
  territoryData: Record<string, { name?: string } | undefined>,
): GameEvent[] {
  const groups = new Map<string, GameEvent[]>();
  for (const e of log) {
    const k = eventLogMergeGroupKey(e);
    if (!k) continue;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(e);
  }

  const seen = new Set<string>();
  const out: GameEvent[] = [];
  for (const e of log) {
    const k = eventLogMergeGroupKey(e);
    if (!k) {
      out.push(e);
      continue;
    }
    if (seen.has(k)) continue;
    seen.add(k);
    const g = groups.get(k)!;
    if (g.length === 1) {
      out.push(e);
      continue;
    }
    out.push(mergeGroup(g, unitDefs, territoryData));
  }
  return out;
}

function mergeGroup(
  group: GameEvent[],
  unitDefs: Record<string, { name?: string } | undefined>,
  territoryData: Record<string, { name?: string } | undefined>,
): GameEvent {
  const first = group[0];
  if (first.type === 'combat_ended') {
    const msgs = [...new Set(group.map((ev) => String(ev.message ?? '').trim()).filter(Boolean))];
    const msg = msgs.join(' · ');
    const latest = group[0];
    const p = { ...(latest.payload as Record<string, unknown>), message: msg };
    return {
      ...latest,
      id: `${latest.id}-merged`,
      message: msg,
      payload: p,
    };
  }
  if (first.type === 'units_moved') {
    const allIds = group.flatMap((ev) => (ev.payload?.unit_ids as string[]) ?? []);
    const base = first.payload as Record<string, unknown>;
    const phase = String(base.phase ?? '');
    let p: Record<string, unknown>;
    if (phase === 'combat_move') {
      const mts = [...new Set(group.map((ev) => String((ev.payload as Record<string, unknown>)?.move_type ?? '')))];
      p = { ...base, unit_ids: allIds, phase: 'combat_move' };
      if (mts.length === 1 && mts[0]) {
        p.move_type = mts[0];
      } else {
        delete p.move_type;
      }
    } else {
      p = { ...base, unit_ids: allIds };
    }
    const msg = formatUnitsMovedMessage(p, unitDefs, territoryData);
    return {
      ...first,
      id: `${first.id}-merged`,
      message: msg,
      payload: { ...p, message: msg },
    };
  }
  if (first.type === 'units_mobilized') {
    const allUnits = group.flatMap((ev) => (ev.payload?.units as { unit_id?: string }[]) ?? []);
    const p = { ...(first.payload as Record<string, unknown>), units: allUnits };
    const msg = formatUnitsMobilizedMessage(p, unitDefs, territoryData);
    return {
      ...first,
      id: `${first.id}-merged`,
      message: msg,
      payload: { ...p, message: msg },
    };
  }
  if (first.type === 'units_purchased') {
    const mergedPurchases: Record<string, number> = {};
    const mergedCost: Record<string, number> = {};
    for (const ev of group) {
      const pr = (ev.payload?.purchases as Record<string, number>) ?? {};
      for (const [k, v] of Object.entries(pr)) {
        const n = typeof v === 'number' ? v : 0;
        mergedPurchases[k] = (mergedPurchases[k] ?? 0) + n;
      }
      const tc = (ev.payload?.total_cost as Record<string, number>) ?? {};
      for (const [k, v] of Object.entries(tc)) {
        const n = typeof v === 'number' ? v : 0;
        mergedCost[k] = (mergedCost[k] ?? 0) + n;
      }
    }
    const p = {
      ...(first.payload as Record<string, unknown>),
      purchases: mergedPurchases,
      total_cost: mergedCost,
    };
    const msg = formatUnitsPurchasedMessage(p, unitDefs);
    return {
      ...first,
      id: `${first.id}-merged`,
      message: msg,
      payload: { ...p, message: msg },
    };
  }
  return first;
}
