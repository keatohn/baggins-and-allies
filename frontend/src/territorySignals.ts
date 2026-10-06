export type SignalAppliesTo = 'enemy' | 'allied' | 'neutral' | 'any';

export interface SignalPreset {
  id: string;
  label: string;
  icon: string;
  applies_to: SignalAppliesTo;
  /** Flag color on the map, chosen in the admin Signals tab. */
  color: string;
}

export interface TerritorySignal {
  preset_id: string;
  label: string;
  icon: string;
  color?: string;
  faction_id: string;
  alliance: string;
}

export type TerritorySignalMap = Record<string, TerritorySignal>;

const SEA_ZONE_ID = /^sea_zone_?\d+$/i;

export function territorySignalRelation(
  territoryId: string,
  territory: { owner?: string | null; ownable?: boolean; terrain?: string } | null | undefined,
  myAlliance: string | null | undefined,
  factionAlliance: (factionId: string) => string | undefined,
): Exclude<SignalAppliesTo, 'any'> | null {
  if (!myAlliance || !territory) return null;
  const terrain = (territory.terrain || '').toLowerCase();
  const isSea = terrain === 'sea' || SEA_ZONE_ID.test(territoryId);
  if (territory.ownable === false || isSea) return null;
  const owner = (territory.owner || '').trim();
  if (!owner || owner.toLowerCase() === 'neutral') return 'neutral';
  const ownerAlliance = factionAlliance(owner);
  if (!ownerAlliance || ownerAlliance === 'neutral') return 'neutral';
  if (ownerAlliance === myAlliance) return 'allied';
  return 'enemy';
}

export function presetMatchesRelation(appliesTo: string, relation: string | null): boolean {
  if (appliesTo === 'any') return true;
  return relation != null && appliesTo === relation;
}
