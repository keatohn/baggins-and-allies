export type RingView = {
  id: string;
  name: string;
  power: number;
  territory_id: string;
  bearer_hero_id?: string | null;
  carried_in_by?: string | null;
  returns_to?: string | null;
  attack_boost?: number;
  defense_boost?: number;
  rolls_boost?: number;
  hp_boost?: number;
  moves_boost?: number;
};

type UnitDefLike = {
  hero_id?: string | null;
  cost?: unknown;
};

export function ringIconSrc(id: string): string {
  return `/assets/rings/${id}.png`;
}

export function ringHasBoost(ring: RingView): boolean {
  return (
    (ring.attack_boost ?? 0) > 0
    || (ring.defense_boost ?? 0) > 0
    || (ring.rolls_boost ?? 0) > 0
    || (ring.hp_boost ?? 0) > 0
  );
}

function heroIdOf(def: UnitDefLike | undefined): string | null {
  const raw = def?.hero_id;
  return typeof raw === 'string' && raw.trim() ? raw.trim() : null;
}

function heroPower(def: UnitDefLike | undefined): number {
  const cost = def?.cost;
  if (typeof cost === 'number') return cost;
  if (cost && typeof cost === 'object' && 'power' in cost && typeof cost.power === 'number') return cost.power;
  return 0;
}

/** Highest unit-power hero among these unit ids. */
export function highestPowerHeroUnitId(
  unitIds: string[],
  unitDefs: Record<string, UnitDefLike | undefined>,
): string | null {
  const heroes = unitIds.filter((id) => heroIdOf(unitDefs[id]));
  if (heroes.length === 0) return null;
  return heroes.reduce((best, id) => (heroPower(unitDefs[id]) > heroPower(unitDefs[best]) ? id : best));
}

/**
 * Unit type this ring sits on, or null when it stands alone.
 * With `attackerIds` (a battle), attackers hold only the ring they carried in.
 */
export function ringHostUnitId(
  ring: RingView,
  units: { unit_id: string; instance_id?: string }[],
  unitDefs: Record<string, UnitDefLike | undefined>,
  attackerIds?: ReadonlySet<string>,
): string | null {
  let pool = units;
  if (attackerIds && attackerIds.size > 0) {
    const carrier = (ring.carried_in_by || '').trim();
    if (carrier && attackerIds.has(carrier)) {
      const unit = units.find((u) => u.instance_id === carrier && heroIdOf(unitDefs[u.unit_id]));
      return unit?.unit_id ?? null;
    }
    pool = units.filter((u) => !u.instance_id || !attackerIds.has(u.instance_id));
  }
  const present = pool.filter((unit) => heroIdOf(unitDefs[unit.unit_id]));
  const required = (ring.bearer_hero_id || '').trim();
  if (required) {
    const match = present.find((unit) => heroIdOf(unitDefs[unit.unit_id]) === required);
    return match?.unit_id ?? null;
  }
  if (present.length === 0) return null;
  const best = present.reduce((top, unit) => {
    const power = heroPower(unitDefs[unit.unit_id]);
    const topPower = heroPower(unitDefs[top.unit_id]);
    return power > topPower ? unit : top;
  });
  return best.unit_id;
}

export function ringsOnUnit(
  rings: RingView[],
  territoryId: string,
  unitId: string,
  units: { unit_id: string; instance_id?: string }[],
  unitDefs: Record<string, UnitDefLike | undefined>,
  attackerIds?: ReadonlySet<string>,
): RingView[] {
  return rings.filter(
    (ring) => ring.territory_id === territoryId && ringHostUnitId(ring, units, unitDefs, attackerIds) === unitId,
  );
}

export function standaloneRings(
  rings: RingView[],
  territoryId: string,
  units: { unit_id: string }[],
  unitDefs: Record<string, UnitDefLike | undefined>,
): RingView[] {
  return rings.filter(
    (ring) => ring.territory_id === territoryId && ringHostUnitId(ring, units, unitDefs) == null,
  );
}

/** Faction (or subfaction) that holds this ring: the named bearer's faction, otherwise the territory owner. */
export function ringHoldingFactionId(
  ring: Pick<RingView, 'bearer_hero_id' | 'territory_id'>,
  territoryOwner: string | null | undefined,
  unitDefs: Record<string, { hero_id?: string | null; faction?: string } | undefined>,
): string | null {
  const bearer = (ring.bearer_hero_id || '').trim();
  let faction = '';
  if (bearer) {
    for (const def of Object.values(unitDefs)) {
      const hero = typeof def?.hero_id === 'string' ? def.hero_id.trim() : '';
      const fac = typeof def?.faction === 'string' ? def.faction.trim() : '';
      if (hero === bearer && fac) {
        faction = fac;
        break;
      }
    }
  }
  if (!faction) {
    faction = typeof territoryOwner === 'string' ? territoryOwner.trim() : '';
  }
  return faction || null;
}

export function boostingRingsFromRules(specialRules: unknown): RingView[] {
  if (!Array.isArray(specialRules)) return [];
  const out: RingView[] = [];
  for (const rule of specialRules) {
    if (!rule || typeof rule !== 'object') continue;
    const typed = rule as { type?: string; rings?: unknown };
    if (typed.type !== 'rings_of_power' || !Array.isArray(typed.rings)) continue;
    for (const row of typed.rings) {
      if (!row || typeof row !== 'object') continue;
      const ring = row as RingView;
      if (typeof ring.id !== 'string' || !ringHasBoost(ring)) continue;
      out.push(ring);
    }
  }
  return out;
}

