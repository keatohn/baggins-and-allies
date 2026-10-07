import type { AdminSetupBundle, ApiFactionStats, FactionStatEntry } from '../../services/api';
import type { StatsFactionData, StatsRingMark, UnitForStats } from '../../components/StatsModals';

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function powerCost(cost: unknown): number {
  if (typeof cost === 'number' && Number.isFinite(cost)) return cost;
  const rec = asRecord(cost);
  return asNumber(rec.power, 0);
}

function emptyStat(): FactionStatEntry {
  return { territories: 0, strongholds: 0, power: 0, power_per_turn: 0, units: 0, unit_power: 0 };
}

function addStat(into: FactionStatEntry, add: FactionStatEntry): void {
  into.territories += add.territories;
  into.strongholds += add.strongholds;
  into.power += add.power;
  into.power_per_turn += add.power_per_turn;
  into.units = (into.units ?? 0) + (add.units ?? 0);
  into.unit_power = (into.unit_power ?? 0) + (add.unit_power ?? 0);
}

function formatSpecialFallback(s: string): string {
  const normalized = s.replace(/_/g, ' ').replace(/-/g, ' ').toLowerCase().trim();
  if (normalized === 'anti cavalry') return 'Anti-Cavalry';
  return s.replace(/_/g, ' ').replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function unitSpecialLabels(
  specials: unknown,
  catalog: Record<string, unknown>,
  homeTerritoryDisplayNames?: string[],
): string[] {
  const ids = Array.isArray(specials)
    ? [...new Set(specials.filter((id): id is string => typeof id === 'string' && id !== 'order'))]
    : [];
  const names = ids
    .filter((id) => {
      const entry = catalog[id];
      return typeof entry === 'object' && entry !== null && 'name' in (entry as Record<string, unknown>);
    })
    .map((sid) => {
      const n = asString(asRecord(catalog[sid]).name).trim();
      return n || formatSpecialFallback(sid);
    })
    .sort((a, b) => a.localeCompare(b));
  if (ids.includes('home') && homeTerritoryDisplayNames?.length) {
    return names.map((n) => (n === 'Home' ? `Home (${homeTerritoryDisplayNames.join(', ')})` : n));
  }
  return names;
}

export type RingsRuleMode = 'none' | 'always' | 'optional';

export interface AdminStatsPreview {
  factionData: StatsFactionData;
  turnOrder: string[];
  unitsByFaction: Record<string, UnitForStats[]>;
  factionStats: ApiFactionStats;
  ringsByFaction: Record<string, StatsRingMark[]>;
  ringsMode: RingsRuleMode;
}

type RingRow = { id: string; name: string; territory_id: string; power: number; bearer_hero_id: string };

function ringsRuleFromManifest(manifest: Record<string, unknown>): { mode: RingsRuleMode; rings: RingRow[] } {
  const rules = Array.isArray(manifest.special_rules) ? manifest.special_rules : [];
  for (const raw of rules) {
    const rule = asRecord(raw);
    if (asString(rule.type) !== 'rings_of_power') continue;
    const rings: RingRow[] = [];
    for (const rawRing of Array.isArray(rule.rings) ? rule.rings : []) {
      const r = asRecord(rawRing);
      const id = asString(r.id);
      if (!id) continue;
      rings.push({
        id,
        name: asString(r.name) || id,
        territory_id: asString(r.territory_id),
        power: Math.max(0, Math.floor(asNumber(r.power, 0))),
        bearer_hero_id: asString(r.bearer_hero_id).trim(),
      });
    }
    return { mode: rule.is_optional === true ? 'optional' : 'always', rings };
  }
  return { mode: 'none', rings: [] };
}

/** Match-start preview from the current (possibly unsaved) admin setup bundle. */
export function previewStatsFromBundle(
  bundle: AdminSetupBundle | null,
  options: { rings?: boolean } = {},
): AdminStatsPreview | null {
  if (!bundle) return null;

  const units = asRecord(bundle.units);
  const territories = asRecord(bundle.territories);
  const factions = asRecord(bundle.factions);
  const specials = asRecord(bundle.specials);
  const starting = asRecord(bundle.starting_setup);
  const owners = asRecord(starting.territory_owners);
  const startingUnits = asRecord(starting.starting_units);

  const factionData: StatsFactionData = {};
  for (const [id, raw] of Object.entries(factions)) {
    const f = asRecord(raw);
    const iconFile = asString(f.icon) || `${id}.png`;
    const parentIcon = `/assets/factions/${iconFile}`;
    factionData[id] = {
      name: asString(f.display_name) || id,
      icon: parentIcon,
      color: asString(f.color) || '#888888',
      alliance: asString(f.alliance),
    };
    const subs = Array.isArray(f.subfactions) ? f.subfactions : [];
    for (const rawSub of subs) {
      const sub = asRecord(rawSub);
      const sid = asString(sub.id);
      if (!sid) continue;
      const subIconFile = asString(sub.icon);
      factionData[sid] = {
        name: asString(sub.display_name) || sid,
        icon: subIconFile ? `/assets/factions/${subIconFile}` : parentIcon,
        color: asString(sub.color) || '#888888',
        alliance: asString(f.alliance),
        parent: id,
      };
    }
  }

  const turnOrderRaw = Array.isArray(starting.turn_order)
    ? starting.turn_order.filter((fid): fid is string => typeof fid === 'string' && fid in factions)
    : [];
  const turnOrder = turnOrderRaw.length > 0 ? turnOrderRaw : Object.keys(factions);

  const unitsByFaction: Record<string, UnitForStats[]> = {};
  for (const [id, raw] of Object.entries(units)) {
    const u = asRecord(raw);
    const faction = asString(u.faction);
    if (!faction) continue;
    const iconFile = asString(u.icon) || `${id}.png`;
    const homeIds = Array.isArray(u.home_territory_ids)
      ? u.home_territory_ids.filter((tid): tid is string => typeof tid === 'string')
      : [];
    const homeNames = homeIds
      .map((tid) => asString(asRecord(territories[tid]).display_name) || tid)
      .filter(Boolean);
    if (!unitsByFaction[faction]) unitsByFaction[faction] = [];
    unitsByFaction[faction].push({
      id,
      name: asString(u.display_name) || id,
      icon: `/assets/units/${iconFile}`,
      cost: powerCost(u.cost),
      attack: asNumber(u.attack),
      defense: asNumber(u.defense),
      dice: asNumber(u.dice, 1),
      movement: asNumber(u.movement),
      health: asNumber(u.health),
      specials: unitSpecialLabels(u.specials, specials, homeNames),
    });
  }
  for (const fid of Object.keys(unitsByFaction)) {
    unitsByFaction[fid].sort((a, b) => {
      if (a.cost !== b.cost) return a.cost - b.cost;
      if (a.dice !== b.dice) return a.dice - b.dice;
      if (a.attack !== b.attack) return a.attack - b.attack;
      const lenA = a.specials.length;
      const lenB = b.specials.length;
      if (lenA !== lenB) return lenA - lenB;
      const byName = a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
      if (byName !== 0) return byName;
      return a.id.localeCompare(b.id);
    });
  }

  const factionStatsMap: Record<string, FactionStatEntry> = {};
  for (const fid of Object.keys(factions)) {
    factionStatsMap[fid] = emptyStat();
  }

  const manifest = asRecord(bundle.manifest);
  const subfactionRules = asRecord(manifest.subfaction_rules);
  const parentOf: Record<string, string> = {};
  for (const [id, raw] of Object.entries(factions)) {
    for (const rawSub of Array.isArray(asRecord(raw).subfactions) ? (asRecord(raw).subfactions as unknown[]) : []) {
      const sid = asString(asRecord(rawSub).id);
      if (sid) parentOf[sid] = id;
    }
  }
  const controller = (fid: string): string => parentOf[fid] ?? fid;
  const subfactionStatsMap: Record<string, FactionStatEntry> = {};
  for (const sid of Object.keys(parentOf)) {
    const economy = asString(asRecord(subfactionRules[sid]).economy) || 'none';
    if (economy !== 'none') subfactionStatsMap[sid] = emptyStat();
  }
  /** Parent row plus, for subfaction-owned things, the subfaction's own row. */
  const rowsFor = (fid: string): FactionStatEntry[] =>
    [factionStatsMap[controller(fid)], subfactionStatsMap[fid]].filter((s): s is FactionStatEntry => Boolean(s));
  const parentHoldsCapital = (pid: string): boolean => {
    const capital = asString(asRecord(factions[pid]).capital);
    return Boolean(capital) && asString(owners[capital]) === pid;
  };
  /** Production from this owner pays the parent only for a pool subfaction whose parent holds its capital. */
  const paysParent = (fid: string): boolean => {
    const pid = parentOf[fid];
    if (!pid) return true;
    return asString(asRecord(subfactionRules[fid]).economy) === 'pool' && parentHoldsCapital(pid);
  };

  for (const [tid, raw] of Object.entries(territories)) {
    const t = asRecord(raw);
    const owner = asString(owners[tid]);
    if (!owner) continue;
    const pays = paysParent(owner);
    const ppt = powerCost(t.produces);
    for (const st of rowsFor(owner)) {
      st.territories += 1;
      if (t.is_stronghold === true) st.strongholds += 1;
      if (pays) st.power_per_turn += ppt;
    }
    const parentRow = factionStatsMap[controller(owner)];
    if (parentRow && pays) parentRow.power += ppt;
  }

  for (const unitList of Object.values(startingUnits)) {
    if (!Array.isArray(unitList)) continue;
    for (const entry of unitList) {
      const rec = asRecord(entry);
      const unitId = asString(rec.unit_id);
      const count = Math.max(0, Math.floor(asNumber(rec.count, 1)));
      if (!unitId || count <= 0) continue;
      const ud = asRecord(units[unitId]);
      const fid = asString(ud.faction);
      if (!fid) continue;
      for (const st of rowsFor(fid)) {
        st.units = (st.units ?? 0) + count;
        st.unit_power = (st.unit_power ?? 0) + powerCost(ud.cost) * count;
      }
    }
  }

  const ringsRule = ringsRuleFromManifest(manifest);
  const ringsOn = ringsRule.mode === 'always' || (ringsRule.mode === 'optional' && options.rings !== false);
  const ringsByFaction: Record<string, StatsRingMark[]> = {};
  if (ringsOn) {
    for (const ring of ringsRule.rings) {
      let holder = '';
      if (ring.bearer_hero_id) {
        const here = Array.isArray(startingUnits[ring.territory_id]) ? (startingUnits[ring.territory_id] as unknown[]) : [];
        for (const entry of here) {
          const ud = asRecord(units[asString(asRecord(entry).unit_id)]);
          if (asString(ud.hero_id).trim() === ring.bearer_hero_id) {
            holder = asString(ud.faction);
            break;
          }
        }
      }
      if (!holder) holder = asString(owners[ring.territory_id]);
      if (!holder) continue;
      (ringsByFaction[holder] ??= []).push({ id: ring.id, name: ring.name });
      if (!paysParent(holder)) continue;
      for (const st of rowsFor(holder)) st.power_per_turn += ring.power;
      const parentRow = factionStatsMap[controller(holder)];
      if (parentRow) parentRow.power += ring.power;
    }
  }

  const alliances: Record<string, FactionStatEntry> = {};
  for (const [fid, fd] of Object.entries(factionData)) {
    const alliance = fd.alliance || '';
    if (!alliances[alliance]) alliances[alliance] = emptyStat();
    addStat(alliances[alliance], factionStatsMap[fid] ?? emptyStat());
  }

  let neutral_strongholds = 0;
  for (const [tid, raw] of Object.entries(territories)) {
    const t = asRecord(raw);
    if (t.is_stronghold !== true) continue;
    const owner = asString(owners[tid]);
    if (!owner) neutral_strongholds += 1;
  }

  const factionStats: ApiFactionStats = {
    factions: factionStatsMap,
    alliances,
    neutral_strongholds,
  };
  if (Object.keys(subfactionStatsMap).length > 0) factionStats.subfactions = subfactionStatsMap;
  const vc = asRecord(asRecord(bundle.manifest).victory_criteria).strongholds;
  const sh = asRecord(vc);
  const stronghold_victory: { good?: number; evil?: number } = {};
  for (const key of ['good', 'evil'] as const) {
    const n = asNumber(sh[key], 0);
    if (n > 0) stronghold_victory[key] = n;
  }
  if (Object.keys(stronghold_victory).length > 0) {
    factionStats.stronghold_victory = stronghold_victory;
  }

  return { factionData, turnOrder, unitsByFaction, factionStats, ringsByFaction, ringsMode: ringsRule.mode };
}
