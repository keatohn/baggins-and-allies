import { useState, useMemo, useEffect, useRef } from 'react';
import type { Definitions } from '../services/api';
import api, { type SimulateCombatResponse } from '../services/api';
import './CombatSimulatorPanel.css';

/** Per-territory list of stacks (unit_id + count). */
type TerritoryUnitsMap = Record<string, { unit_id: string; count: number }[]>;

interface UnitDefForSim {
  name: string;
  icon: string;
  faction?: string;
}

interface CombatSimulatorPanelProps {
  definitions: Definitions | null;
  territoryUnits?: TerritoryUnitsMap;
  unitDefs?: Record<string, UnitDefForSim>;
  /** Territory id -> owner (faction id); for filtering non-ally territories. */
  territoryData?: Record<string, { owner?: string; terrain?: string }>;
  /** Faction id -> alliance, icon, color (for logos and unit borders). */
  factionData?: Record<string, { alliance?: string; icon?: string; color?: string }>;
  /** Game id when in a game. Sent to backend so sim uses this game's definitions (same as actual combat); ensures archer/stealth prefire is recognized. */
  gameId?: string | null;
  /** Setup id when not in a game. Fallback so backend can load definitions by setup. */
  setupId?: string | null;
  /** Per-territory defender casualty order from live game (`best_unit` | `best_defense`). Drives default defender sim pill when a real territory is selected (attacker order is never derived from territory). */
  territoryDefenderCasualtyOrder?: Record<string, string>;
  onClose?: () => void;
  embedded?: boolean;
}

type FactionId = string;
type UnitId = string;

/** Specials that can affect battle rolls and have display_code. Excludes charging, aerial, home. Stealth on defender never shown. */
function isNavalUnit(definitions: Definitions | null, unitId: string): boolean {
  if (!definitions?.units) return false;
  const u = definitions.units[unitId] as { archetype?: string; tags?: string[] } | undefined;
  if (!u) return false;
  return u.archetype === 'naval' || (u.tags || []).includes('naval');
}

function isAerialUnit(definitions: Definitions | null, unitId: string): boolean {
  if (!definitions?.units) return false;
  const u = definitions.units[unitId] as { archetype?: string; tags?: string[] } | undefined;
  if (!u) return false;
  return u.archetype === 'aerial' || (u.tags || []).includes('aerial');
}

/** Land combat: land + aerial. Sea combat: naval + aerial. */
function unitAllowedForCombatType(
  definitions: Definitions | null,
  unitId: string,
  isLand: boolean
): boolean {
  if (isAerialUnit(definitions, unitId)) return true;
  if (isLand) return !isNavalUnit(definitions, unitId);
  return isNavalUnit(definitions, unitId);
}

/** For adding defenders: land = land + aerial; sea = naval only (no aerial on sea defense). */
function unitAllowedForDefenseInCombatType(
  definitions: Definitions | null,
  unitId: string,
  isLandCombat: boolean
): boolean {
  if (isLandCombat) return unitAllowedForCombatType(definitions, unitId, true);
  return isNavalUnit(definitions, unitId) && !isAerialUnit(definitions, unitId);
}

function getUnitMovement(definitions: Definitions | null, unitId: string): number {
  if (!definitions?.units) return 0;
  const u = definitions.units[unitId] as { movement?: number } | undefined;
  return typeof u?.movement === 'number' ? u.movement : 0;
}

/**
 * Per-word overrides for combat sim casualty/destroyed lists (shorter than default 3-letter abbrev).
 * Keys: letters-only lowercase match of the word (accents stripped).
 */
const UNIT_NAME_ABBREV_OVERRIDES: Record<string, string> = {
  morannon: 'Moran',
  morgul: 'Morg',
};

function abbreviateUnitWord(word: string): string {
  const lettersOnly = word
    .normalize('NFD')
    .replace(/\p{M}/gu, '')
    .replace(/[^a-zA-Z]/g, '')
    .toLowerCase();
  if (lettersOnly && UNIT_NAME_ABBREV_OVERRIDES[lettersOnly]) {
    return UNIT_NAME_ABBREV_OVERRIDES[lettersOnly];
  }
  if (word.length === 4) return word;
  return word.slice(0, 3);
}

/** Abbreviate unit name: first 3 letters of each word, or all 4 if word length is 4; see overrides above. */
function abbreviateUnitName(name: string): string {
  return name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map(abbreviateUnitWord)
    .join(' ');
}

const TERRAIN_PREFIX = 'terrain:';

function normalizeDefenderCasualtyOrder(raw: string | undefined): 'best_unit' | 'best_defense' {
  return raw === 'best_defense' ? 'best_defense' : 'best_unit';
}

/** Resolve terrain type from territory id: real territory from definitions, or "terrain:forest" -> "forest". */
function getTerrainTypeFromTerritoryId(definitions: Definitions | null, territoryId: string | null): string {
  if (!territoryId) return '';
  if (territoryId.startsWith(TERRAIN_PREFIX)) return territoryId.slice(TERRAIN_PREFIX.length).toLowerCase();
  const territory = definitions?.territories?.[territoryId] as { terrain_type?: string } | undefined;
  return (territory?.terrain_type ?? '').toLowerCase();
}

const SIM_COUNT_INPUT_MAX = 999;

/** Touch-friendly count control: − / number / + */
function SimCountStepper({
  value,
  onChange,
  unitLabel,
}: {
  value: number;
  onChange: (n: number) => void;
  /** For aria-label, e.g. unit display name */
  unitLabel: string;
}) {
  const v = Math.max(0, Math.min(SIM_COUNT_INPUT_MAX, value));
  const aria = `Count for ${unitLabel}`;
  return (
    <div className="combat-sim-count-stepper">
      <button
        type="button"
        className="combat-sim-count-btn combat-sim-count-btn--dec"
        aria-label={`${aria}: decrease`}
        disabled={v <= 0}
        onClick={() => onChange(Math.max(0, v - 1))}
      >
        −
      </button>
      <input
        type="number"
        min={0}
        max={SIM_COUNT_INPUT_MAX}
        inputMode="numeric"
        className="combat-sim-input combat-sim-input--stepper"
        value={v}
        onChange={(e) => {
          const raw = e.target.value.trim();
          if (raw === '') {
            onChange(0);
            return;
          }
          const n = parseInt(raw, 10);
          if (Number.isNaN(n)) return;
          onChange(Math.max(0, Math.min(SIM_COUNT_INPUT_MAX, n)));
        }}
        aria-label={aria}
      />
      <button
        type="button"
        className="combat-sim-count-btn combat-sim-count-btn--inc"
        aria-label={`${aria}: increase`}
        disabled={v >= SIM_COUNT_INPUT_MAX}
        onClick={() => onChange(Math.min(SIM_COUNT_INPUT_MAX, v + 1))}
      >
        +
      </button>
    </div>
  );
}

function getUnitStatFromDefs(definitions: Definitions | null, unitId: string, kind: 'attack' | 'defense'): number {
  if (!definitions?.units) return 0;
  const u = definitions.units[unitId] as { attack?: number; defense?: number } | undefined;
  const n = kind === 'attack' ? u?.attack : u?.defense;
  return typeof n === 'number' ? n : 0;
}

function getUnitSpecialsCount(definitions: Definitions | null, unitId: string): number {
  if (!definitions?.units) return 0;
  const u = definitions.units[unitId] as { specials?: string[] } | undefined;
  return Array.isArray(u?.specials) ? u!.specials.length : 0;
}

/** Power cost for unit (from cost.power). */
function getUnitPowerCost(definitions: Definitions | null, unitId: string): number {
  if (!definitions?.units) return 0;
  const u = definitions.units[unitId] as { cost?: number | { power?: number } } | undefined;
  const c = u?.cost;
  return typeof c === 'object' && c && 'power' in c ? (c as { power: number }).power : typeof c === 'number' ? c : 0;
}

function compareUnitIdsByBattleOrder(
  definitions: Definitions | null,
  unitDefs: Record<string, UnitDefForSim>,
  aUnitId: string,
  bUnitId: string
): number {
  const aCost = getUnitPowerCost(definitions, aUnitId);
  const bCost = getUnitPowerCost(definitions, bUnitId);
  const aRolls = getUnitDiceAndHealth(definitions, aUnitId).dice;
  const bRolls = getUnitDiceAndHealth(definitions, bUnitId).dice;
  const aAttack = getUnitStatFromDefs(definitions, aUnitId, 'attack');
  const bAttack = getUnitStatFromDefs(definitions, bUnitId, 'attack');
  const aSpecials = getUnitSpecialsCount(definitions, aUnitId);
  const bSpecials = getUnitSpecialsCount(definitions, bUnitId);
  const aName = unitDefs[aUnitId]?.name ?? aUnitId;
  const bName = unitDefs[bUnitId]?.name ?? bUnitId;
  return (
    aCost - bCost
    || aRolls - bRolls
    || aAttack - bAttack
    || aSpecials - bSpecials
    || aName.localeCompare(bName)
  );
}

/** Defender lists / add-defender dropdown: faction (display name), then unit name. Optional primary faction sorts first (e.g. territory owner). */
function compareDefenderUnitIdsByFactionThenName(
  definitions: Definitions | null,
  unitDefs: Record<string, UnitDefForSim>,
  aUnitId: string,
  bUnitId: string,
  primaryFactionId?: string | null,
): number {
  const factionOf = (id: string) =>
    (definitions?.units?.[id] as { faction?: string } | undefined)?.faction?.trim() ||
    unitDefs[id]?.faction ||
    'neutral';
  const factionDisplay = (fid: string) => {
    const f = definitions?.factions?.[fid] as { display_name?: string } | undefined;
    return f?.display_name ?? fid;
  };
  const displayName = (id: string) =>
    unitDefs[id]?.name ??
    (definitions?.units?.[id] as { display_name?: string } | undefined)?.display_name ??
    id;

  const fa = factionOf(aUnitId);
  const fb = factionOf(bUnitId);
  if (primaryFactionId) {
    const pa = fa === primaryFactionId ? 0 : 1;
    const pb = fb === primaryFactionId ? 0 : 1;
    if (pa !== pb) return pa - pb;
  }
  const cFac = factionDisplay(fa).localeCompare(factionDisplay(fb), undefined, { sensitivity: 'base' });
  if (cFac !== 0) return cFac;
  return displayName(aUnitId).localeCompare(displayName(bUnitId), undefined, { sensitivity: 'base' });
}

/**
 * Add-defender dropdown: sort factions with a real alliance (e.g. good/evil) before neutral
 * or factions with no/missing alliance so neutral sits at the bottom.
 */
function addDefenderFactionAllianceSortTier(
  factionId: string,
  definitions: Definitions,
  factionData: Record<string, { alliance?: string } | undefined>,
): number {
  const raw =
    factionData[factionId]?.alliance ??
    (definitions.factions?.[factionId] as { alliance?: string } | undefined)?.alliance ??
    '';
  const a = String(raw).trim().toLowerCase();
  if (!a || a === 'neutral') return 1;
  return 0;
}

/** Dice rolls and HP for unit (for top unit rows in sim modal). */
function getUnitDiceAndHealth(definitions: Definitions | null, unitId: string): { dice: number; health: number } {
  if (!definitions?.units) return { dice: 0, health: 0 };
  const u = definitions.units[unitId] as { dice?: number; health?: number } | undefined;
  return {
    dice: typeof u?.dice === 'number' ? u.dice : 0,
    health: typeof u?.health === 'number' ? u.health : 0,
  };
}

/** `purchasable: false` excludes from purchase roster; missing defaults to true (matches backend UnitDefinition). */
function isUnitPurchasableInDefs(unit: unknown): boolean {
  if (unit == null || typeof unit !== 'object') return true;
  return (unit as { purchasable?: boolean }).purchasable !== false;
}

/**
 * Sea combat — attacking faction dropdown: any faction with at least one purchasable naval or aerial
 * unit type in definitions (roster / purchase list), not units on the board.
 */
function factionHasPurchasableNavalOrAerialForSeaAttack(definitions: Definitions | null, factionId: string): boolean {
  if (!definitions?.units) return false;
  for (const uid of Object.keys(definitions.units)) {
    const u = definitions.units[uid];
    const faction = (u as { faction?: string }).faction ?? 'neutral';
    if (faction !== factionId) continue;
    if (!isUnitPurchasableInDefs(u)) continue;
    if (isNavalUnit(definitions, uid) || isAerialUnit(definitions, uid)) return true;
  }
  return false;
}

/**
 * Sea combat — defender can only be naval (non-aerial). Used to narrow which factions appear when
 * picking defenders on generic sea terrain (purchase roster).
 */
function factionHasPurchasableNavalForSeaDefense(definitions: Definitions | null, factionId: string): boolean {
  if (!definitions?.units) return false;
  for (const uid of Object.keys(definitions.units)) {
    const u = definitions.units[uid];
    const faction = (u as { faction?: string }).faction ?? 'neutral';
    if (faction !== factionId) continue;
    if (!isUnitPurchasableInDefs(u)) continue;
    if (isNavalUnit(definitions, uid) && !isAerialUnit(definitions, uid)) return true;
  }
  return false;
}

const CV_PREDICTABLE = 0.4;
const CV_UNPREDICTABLE = 0.8;
const MEAN_EPSILON = 0.5;

function casualtyCostVarianceCategory(
  costs: number[]
): 'Predictable' | 'Moderate' | 'Unpredictable' {
  if (!costs.length) return 'Predictable';
  const n = costs.length;
  const mean = costs.reduce((a, b) => a + b, 0) / n;
  if (n < 2) return 'Predictable';
  const variance = costs.reduce((s, x) => s + (x - mean) ** 2, 0) / (n - 1);
  const stdev = Math.sqrt(variance);
  if (mean < MEAN_EPSILON) return stdev > 0 ? 'Unpredictable' : 'Predictable';
  const cv = stdev / mean;
  if (cv < CV_PREDICTABLE) return 'Predictable';
  if (cv <= CV_UNPREDICTABLE) return 'Moderate';
  return 'Unpredictable';
}

/** Merge per-trial outcomes into a single SimulateCombatResponse (for chunked progressive results). */
type SimTrialOutcome = {
  winner: string;
  conquered: boolean;
  retreat: boolean;
  rounds: number;
  attacker_casualties: Record<string, number>;
  defender_casualties: Record<string, number>;
  attacker_survived?: boolean;
  defender_survived?: boolean;
  attacker_siegework_hits?: number;
  defender_siegework_hits?: number;
  siegework_round_applicable?: boolean;
  siegework_attacker_dice?: number;
  siegework_defender_dice?: number;
};

function trialAttackerSurvived(o: SimTrialOutcome): boolean {
  if (typeof o.attacker_survived === 'boolean') return o.attacker_survived;
  return o.winner === 'attacker' || o.retreat;
}

/** True if at least one defending unit is alive at end (mutual destruction => false). */
function trialDefenderSurvived(o: SimTrialOutcome): boolean {
  if (typeof o.defender_survived === 'boolean') return o.defender_survived;
  return o.winner === 'defender' && !o.retreat;
}

function mergeSimOutcomes(
  outcomes: SimTrialOutcome[],
  firstChunkResponse: SimulateCombatResponse | null,
  definitions: Definitions | null
): SimulateCombatResponse {
  const n = outcomes.length;
  if (n === 0 && firstChunkResponse) return firstChunkResponse;
  if (n === 0) {
    return {
      n_trials: 0,
      attacker_wins: 0,
      defender_wins: 0,
      attacker_survives: 0,
      defender_survives: 0,
      retreats: 0,
      conquers: 0,
      p_attacker_win: 0,
      p_defender_win: 0,
      p_attacker_survives: 0,
      p_defender_survives: 0,
      p_retreat: 0,
      p_conquer: 0,
      rounds_mean: 0,
      rounds_p50: 0,
      rounds_p90: 0,
      attacker_casualties_mean: {},
      defender_casualties_mean: {},
      attacker_casualties_total_mean: 0,
      defender_casualties_total_mean: 0,
      attacker_casualties_p90: {},
      defender_casualties_p90: {},
      attacker_casualty_cost_mean: 0,
      defender_casualty_cost_mean: 0,
      attacker_casualty_cost_variance_category: 'Predictable',
      defender_casualty_cost_variance_category: 'Predictable',
      percentile_outcomes: [],
      battle_context: firstChunkResponse?.battle_context ?? null,
      prefire_penalty: firstChunkResponse?.prefire_penalty ?? true,
      attacker_siegework_hits_mean: firstChunkResponse?.attacker_siegework_hits_mean ?? null,
      defender_siegework_hits_mean: firstChunkResponse?.defender_siegework_hits_mean ?? null,
    };
  }
  const attacker_wins = outcomes.filter((o) => o.winner === 'attacker').length;
  const defender_wins = n - attacker_wins;
  const retreats = outcomes.filter((o) => o.retreat).length;
  const conquers = outcomes.filter((o) => o.conquered).length;
  const attacker_survives = outcomes.filter(trialAttackerSurvived).length;
  const defender_survives = outcomes.filter(trialDefenderSurvived).length;
  const roundsList = outcomes.map((o) => o.rounds).sort((a, b) => a - b);
  const rounds_mean = roundsList.reduce((s, r) => s + r, 0) / n;
  const rounds_p50 = roundsList[Math.floor(n * 0.5)] ?? 0;
  const rounds_p90 = roundsList[Math.floor(n * 0.9)] ?? 0;
  const allAttIds = new Set<string>();
  const allDefIds = new Set<string>();
  outcomes.forEach((o) => {
    Object.keys(o.attacker_casualties).forEach((id) => allAttIds.add(id));
    Object.keys(o.defender_casualties).forEach((id) => allDefIds.add(id));
  });
  const attacker_casualties_mean: Record<string, number> = {};
  const defender_casualties_mean: Record<string, number> = {};
  allAttIds.forEach((uid) => {
    attacker_casualties_mean[uid] = outcomes.reduce((s, o) => s + (o.attacker_casualties[uid] ?? 0), 0) / n;
  });
  allDefIds.forEach((uid) => {
    defender_casualties_mean[uid] = outcomes.reduce((s, o) => s + (o.defender_casualties[uid] ?? 0), 0) / n;
  });
  const attacker_casualties_total_mean = Object.values(attacker_casualties_mean).reduce((a, b) => a + b, 0);
  const defender_casualties_total_mean = Object.values(defender_casualties_mean).reduce((a, b) => a + b, 0);
  const attP90: Record<string, number> = {};
  allAttIds.forEach((uid) => {
    const vals = outcomes.map((o) => o.attacker_casualties[uid] ?? 0).sort((a, b) => a - b);
    attP90[uid] = vals[Math.floor(n * 0.9)] ?? 0;
  });
  const defP90: Record<string, number> = {};
  allDefIds.forEach((uid) => {
    const vals = outcomes.map((o) => o.defender_casualties[uid] ?? 0).sort((a, b) => a - b);
    defP90[uid] = vals[Math.floor(n * 0.9)] ?? 0;
  });
  const outcomeValue = (i: number) => {
    const o = outcomes[i];
    if (o.conquered) return 3;
    if (o.retreat) return 1;
    if (o.winner === 'attacker') return 2;
    return 0;
  };
  const attCasSum = (i: number) =>
    Object.values(outcomes[i].attacker_casualties).reduce((a, b) => a + b, 0);
  const defCasSum = (i: number) =>
    Object.values(outcomes[i].defender_casualties).reduce((a, b) => a + b, 0);
  const sortedIndices = [...Array(n).keys()].sort(
    (a, b) =>
      outcomeValue(b) - outcomeValue(a) ||
      attCasSum(a) - attCasSum(b) ||
      defCasSum(b) - defCasSum(a)
  );
  const PERCENTILES = [5, 25, 50, 75, 95];
  const percentile_outcomes = PERCENTILES.map((p) => {
    const idx = Math.min(Math.floor((p / 100) * n), n - 1);
    const i = sortedIndices[idx];
    const o = outcomes[i];
    return {
      percentile: p,
      winner: o.winner,
      conquered: o.conquered,
      retreat: o.retreat,
      attacker_casualties: { ...o.attacker_casualties },
      defender_casualties: { ...o.defender_casualties },
    };
  });

  const attCosts = outcomes.map((o) =>
    Object.entries(o.attacker_casualties).reduce(
      (s, [uid, c]) => s + c * getUnitPowerCost(definitions, uid),
      0
    )
  );
  const defCosts = outcomes.map((o) =>
    Object.entries(o.defender_casualties).reduce(
      (s, [uid, c]) => s + c * getUnitPowerCost(definitions, uid),
      0
    )
  );
  const attacker_casualty_cost_mean = attCosts.reduce((a, b) => a + b, 0) / n;
  const defender_casualty_cost_mean = defCosts.reduce((a, b) => a + b, 0) / n;
  const attacker_casualty_cost_variance_category = casualtyCostVarianceCategory(attCosts);
  const defender_casualty_cost_variance_category = casualtyCostVarianceCategory(defCosts);

  const hasSiegeworkDiceMeta = outcomes.some(
    (o) => o.siegework_attacker_dice !== undefined || o.siegework_defender_dice !== undefined
  );
  const attSwTrials = outcomes.filter((o) =>
    hasSiegeworkDiceMeta
      ? (o.siegework_attacker_dice ?? 0) > 0
      : o.siegework_round_applicable === true
  );
  const defSwTrials = outcomes.filter((o) =>
    hasSiegeworkDiceMeta
      ? (o.siegework_defender_dice ?? 0) > 0
      : o.siegework_round_applicable === true
  );
  const attacker_siegework_hits_mean =
    attSwTrials.length > 0
      ? attSwTrials.reduce((s, o) => s + (o.attacker_siegework_hits ?? 0), 0) / attSwTrials.length
      : (firstChunkResponse?.attacker_siegework_hits_mean ?? null);
  const defender_siegework_hits_mean =
    defSwTrials.length > 0
      ? defSwTrials.reduce((s, o) => s + (o.defender_siegework_hits ?? 0), 0) / defSwTrials.length
      : (firstChunkResponse?.defender_siegework_hits_mean ?? null);

  return {
    n_trials: n,
    attacker_wins,
    defender_wins,
    attacker_survives,
    defender_survives,
    retreats,
    conquers,
    p_attacker_win: attacker_wins / n,
    p_defender_win: defender_wins / n,
    p_attacker_survives: attacker_survives / n,
    p_defender_survives: defender_survives / n,
    p_retreat: retreats / n,
    p_conquer: conquers / n,
    rounds_mean,
    rounds_p50,
    rounds_p90,
    attacker_casualties_mean,
    defender_casualties_mean,
    attacker_casualties_total_mean,
    defender_casualties_total_mean,
    attacker_casualties_p90: attP90,
    defender_casualties_p90: defP90,
    attacker_prefire_hits_mean: firstChunkResponse?.attacker_prefire_hits_mean ?? null,
    defender_prefire_hits_mean: firstChunkResponse?.defender_prefire_hits_mean ?? null,
    attacker_siegework_hits_mean,
    defender_siegework_hits_mean,
    attacker_casualty_cost_mean,
    defender_casualty_cost_mean,
    attacker_casualty_cost_variance_category,
    defender_casualty_cost_variance_category,
    percentile_outcomes,
    battle_context: firstChunkResponse?.battle_context ?? null,
    prefire_penalty: firstChunkResponse?.prefire_penalty ?? true,
  };
}

export default function CombatSimulatorPanel({
  definitions,
  territoryUnits = {},
  unitDefs = {},
  territoryData = {},
  factionData = {},
  gameId = null,
  setupId = null,
  territoryDefenderCasualtyOrder = {},
  onClose,
  embedded,
}: CombatSimulatorPanelProps) {
  /** Fresh props for effects that must not re-run on every multiplayer poll. */
  const territoryUnitsRef = useRef(territoryUnits);
  territoryUnitsRef.current = territoryUnits;
  const territoryDataRef = useRef(territoryData);
  territoryDataRef.current = territoryData;

  const allFactions = useMemo(() => {
    if (!definitions?.factions) return [];
    const list = Object.entries(definitions.factions).map(([id, f]) => ({
      id,
      name: (f as { display_name?: string }).display_name ?? id,
    }));
    return list.sort((a, b) => a.name.localeCompare(b.name));
  }, [definitions?.factions]);

  const allTerritoryList = useMemo(() => {
    if (!definitions?.territories) return [];
    const list = Object.entries(definitions.territories).map(([id, t]) => ({
      id,
      name: (t as { display_name?: string }).display_name ?? id,
      isSea: (t as { terrain_type?: string }).terrain_type?.toLowerCase() === 'sea',
    }));
    return list.sort((a, b) => a.name.localeCompare(b.name));
  }, [definitions?.territories]);

  const unitsByFaction = useMemo(() => {
    if (!definitions?.units) return {} as Record<FactionId, { id: UnitId; name: string }[]>;
    const map: Record<FactionId, { id: UnitId; name: string }[]> = {};
    for (const [uid, u] of Object.entries(definitions.units)) {
      const faction = (u as { faction?: string }).faction ?? 'neutral';
      if (!map[faction]) map[faction] = [];
      map[faction].push({
        id: uid,
        name: (u as { display_name?: string }).display_name ?? uid,
      });
    }
    return map;
  }, [definitions?.units]);

  const [isLandCombat, setIsLandCombat] = useState(true);
  const [attackerFaction, setAttackerFaction] = useState<FactionId>('');
  const [attackingTerritoryId, setAttackingTerritoryId] = useState<string>('');
  const [territoryId, setTerritoryId] = useState<string>('');

  /** Terrain types as generic options (e.g. Forest, Mountain) for battles without a specific territory. */
  const terrainTypeOptions = useMemo(() => {
    if (!definitions?.territories) return [];
    const types = new Set<string>();
    Object.values(definitions.territories).forEach((t) => {
      const tt = (t as { terrain_type?: string }).terrain_type?.toLowerCase() ?? '';
      if (tt) types.add(tt);
    });
    return Array.from(types)
      .filter((t) => (isLandCombat ? t !== 'sea' : t === 'sea'))
      .sort((a, b) => a.localeCompare(b))
      .map((t) => ({
        id: `${TERRAIN_PREFIX}${t}`,
        name: t.charAt(0).toUpperCase() + t.slice(1),
      }));
  }, [definitions?.territories, isLandCombat]);
  const [attackerCounts, setAttackerCounts] = useState<Record<UnitId, number>>({});
  const [casualtyOrderAttacker, setCasualtyOrderAttacker] = useState<'best_unit' | 'best_attack'>('best_unit');
  const [casualtyOrderDefender, setCasualtyOrderDefender] = useState<'best_unit' | 'best_defense'>('best_unit');
  const [mustConquer, setMustConquer] = useState(false);
  /** Land combat only: attackers came ashore from ships. Enables +1 attack for units with the Sea Raider special only (not naval combat — no boats fighting). */
  const [isSeaRaid, setIsSeaRaid] = useState(false);
  /** When true, retreat option is enabled and threshold is used. */
  const [retreatEnabled, setRetreatEnabled] = useState(false);
  /** Retreat when attacker has this many or fewer units left (used only when retreatEnabled). */
  const [retreatWhenUnitsLe, setRetreatWhenUnitsLe] = useState<number | null>(null);
  /** When true, stronghold HP is sent to sim (starts at strongholdHpAmount). Locked when territory selected; unlocked for terrain. */
  const [strongholdHpEnabled, setStrongholdHpEnabled] = useState(false);
  /** Stronghold current HP for sim (min 0, max base or 10). Editable when territory stronghold; when terrain/none max 10. */
  const [strongholdHpAmount, setStrongholdHpAmount] = useState<number>(0);
  const [loading, setLoading] = useState(false);
  /** For chunked sim: completed trials so far (progress bar). Zero when not loading. */
  const [simProgressCompleted, setSimProgressCompleted] = useState(0);
  const [simProgressTotal, setSimProgressTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SimulateCombatResponse | null>(null);
  const [addedDefenderStacks, setAddedDefenderStacks] = useState<{ unit_id: string; count: number }[]>([]);
  const [addDefenderDropdownOpen, setAddDefenderDropdownOpen] = useState(false);
  /** Editable counts for defenders that come from the territory; keyed by unit_id, defaults to actual territory count. */
  const [defenderTerritoryCounts, setDefenderTerritoryCounts] = useState<Record<string, number>>({});

  const factionsNoNeutral = useMemo(() => allFactions.filter((f) => f.id !== 'neutral'), [allFactions]);
  const factionsSeaOnly = useMemo(
    () =>
      factionsNoNeutral.filter((f) => factionHasPurchasableNavalOrAerialForSeaAttack(definitions, f.id)),
    [factionsNoNeutral, definitions]
  );
  const factions = isLandCombat ? factionsNoNeutral : factionsSeaOnly;

  /** Territories: non-empty, not owned by ally of attacker (or neutral/unowned), and land/sea filter. Include neutral territories that have units. */
  const territoryOptions = useMemo(() => {
    if (!attackerFaction) return [];
    const attackerAlliance = factionData[attackerFaction]?.alliance ?? '';
    const filtered = allTerritoryList.filter((t) => {
      const stacks = territoryUnits[t.id];
      const hasUnits = stacks && stacks.length > 0 && stacks.some((s) => s.count > 0);
      if (!hasUnits) return false;
      const owner = territoryData[t.id]?.owner;
      if (owner && owner !== 'neutral' && factionData[owner]?.alliance === attackerAlliance && attackerAlliance !== '') return false;
      if (isLandCombat) return !t.isSea;
      return t.isSea;
    });
    return filtered.sort((a, b) => a.name.localeCompare(b.name));
  }, [attackerFaction, allTerritoryList, territoryUnits, territoryData, factionData, isLandCombat]);

  /** Dropdown: terrain types (alphabetically) first, then specific territories. */
  const territoryDropdownOptions = useMemo(
    () => (attackerFaction ? [...terrainTypeOptions, ...territoryOptions] : []),
    [attackerFaction, terrainTypeOptions, territoryOptions]
  );

  const attackerUnitsAll = attackerFaction ? unitsByFaction[attackerFaction] ?? [] : [];
  const attackerUnits = useMemo(
    () =>
      attackerUnitsAll.filter((u) => {
        if (!unitAllowedForCombatType(definitions, u.id, isLandCombat)) return false;
        if (getUnitMovement(definitions, u.id) <= 0) return false;
        if (!isLandCombat) {
          const raw = definitions?.units?.[u.id];
          if (!isUnitPurchasableInDefs(raw)) return false;
        }
        return true;
      }).slice().sort((a, b) => compareUnitIdsByBattleOrder(definitions, unitDefs, a.id, b.id)),
    [attackerUnitsAll, definitions, isLandCombat, unitDefs]
  );

  const attackingTerritoryOptions = useMemo(() => {
    if (!attackerFaction) return [];
    const entries = Object.entries(territoryUnits ?? {});
    const out = entries
      .filter(([tid, stacks]) => {
        if (!stacks?.length) return false;
        const t = territoryData[tid];
        if (!t || t.owner !== attackerFaction) return false;
        if (isLandCombat) {
          if (t.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid)) return false;
        } else {
          if (!(t.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid))) return false;
        }
        return stacks.some((s) => {
          const f = (definitions?.units?.[s.unit_id] as { faction?: string } | undefined)?.faction ?? unitDefs[s.unit_id]?.faction;
          return s.count > 0 && f === attackerFaction;
        });
      })
      .map(([tid]) => ({ id: tid, name: (definitions?.territories?.[tid] as { display_name?: string } | undefined)?.display_name ?? tid }));
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }, [attackerFaction, territoryUnits, territoryData, isLandCombat, definitions?.units, definitions?.territories, unitDefs]);

  const defenderStacks = territoryId ? (territoryUnits[territoryId] ?? []).filter((s) => s.count > 0) : [];
  const defenderStacksFiltered = useMemo(
    () => defenderStacks.filter((s) => unitAllowedForCombatType(definitions, s.unit_id, isLandCombat)),
    [defenderStacks, definitions, isLandCombat]
  );

  const defenderStacksFilteredRef = useRef(defenderStacksFiltered);
  defenderStacksFilteredRef.current = defenderStacksFiltered;

  /**
   * Seed editable defender counts from the board only when the user changes the defender territory.
   * Depends only on `territoryId` so `defenderStacksFiltered` reference churn from polling does not re-run this.
   */
  const seededDefenderTerritoryIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!territoryId || territoryId.startsWith(TERRAIN_PREFIX)) {
      seededDefenderTerritoryIdRef.current = null;
      setDefenderTerritoryCounts({});
      return;
    }
    const filtered = defenderStacksFilteredRef.current;
    if (filtered.length === 0) {
      if (seededDefenderTerritoryIdRef.current !== territoryId) {
        seededDefenderTerritoryIdRef.current = territoryId;
        setDefenderTerritoryCounts({});
      }
      return;
    }
    if (seededDefenderTerritoryIdRef.current === territoryId) return;
    seededDefenderTerritoryIdRef.current = territoryId;
    setDefenderTerritoryCounts(
      filtered.reduce<Record<string, number>>((acc, { unit_id, count }) => ({ ...acc, [unit_id]: count }), {})
    );
  }, [territoryId]);

  /** When switching Land/Sea, clear generic terrain selection if it's no longer valid (e.g. Forest when switching to Sea). */
  useEffect(() => {
    if (territoryId.startsWith(TERRAIN_PREFIX) && !terrainTypeOptions.some((o) => o.id === territoryId)) {
      setTerritoryId('');
      setAddedDefenderStacks([]);
    }
  }, [isLandCombat]); // eslint-disable-line react-hooks/exhaustive-deps -- only run when combat type toggles

  useEffect(() => {
    if (!isLandCombat) setIsSeaRaid(false);
  }, [isLandCombat]);

  useEffect(() => {
    if (!attackingTerritoryId) return;
    if (!attackingTerritoryOptions.some((t) => t.id === attackingTerritoryId)) {
      setAttackingTerritoryId('');
    }
  }, [attackingTerritoryId, attackingTerritoryOptions]);

  /**
   * Seed attacker counts when faction / attacking territory / land-vs-sea changes, or when definitions arrive
   * and counts are still zero. Uses `territoryUnitsRef` so multiplayer polls do not retrigger a full re-seed.
   */
  const attackerSeedKeyRef = useRef<string>('');
  useEffect(() => {
    if (!attackerFaction || !attackingTerritoryId) {
      attackerSeedKeyRef.current = '';
      return;
    }
    const seedKey = `${attackerFaction}\0${attackingTerritoryId}\0${isLandCombat}`;
    const stacks = territoryUnitsRef.current?.[attackingTerritoryId] ?? [];
    const computeNext = (): Record<string, number> => {
      const next: Record<string, number> = {};
      stacks.forEach((s) => {
        const raw = definitions?.units?.[s.unit_id];
        const f = (raw as { faction?: string } | undefined)?.faction ?? unitDefs[s.unit_id]?.faction;
        if (f !== attackerFaction) return;
        if (!unitAllowedForCombatType(definitions, s.unit_id, isLandCombat)) return;
        if (getUnitMovement(definitions, s.unit_id) <= 0) return;
        if (!isLandCombat && !isUnitPurchasableInDefs(raw)) return;
        next[s.unit_id] = (next[s.unit_id] ?? 0) + s.count;
      });
      return next;
    };

    const keyChanged = attackerSeedKeyRef.current !== seedKey;
    if (keyChanged) {
      attackerSeedKeyRef.current = seedKey;
      setAttackerCounts(computeNext());
      return;
    }

    setAttackerCounts((prev) => {
      const prevTotal = Object.values(prev).reduce((a, b) => a + b, 0);
      if (prevTotal > 0) return prev;
      const next = computeNext();
      return Object.keys(next).length > 0 ? next : prev;
    });
  }, [attackerFaction, attackingTerritoryId, isLandCombat, definitions, unitDefs]);

  const isTerrainSelection = territoryId.startsWith(TERRAIN_PREFIX);
  const defenderOrderForEffect =
    !isTerrainSelection && territoryId
      ? normalizeDefenderCasualtyOrder(territoryDefenderCasualtyOrder?.[territoryId])
      : 'best_unit';

  /** Default defender pill from territory config; terrain or unset → best_unit. No attacking-side territory order — attacker stays best_unit until the user picks Best attack. */
  useEffect(() => {
    setCasualtyOrderDefender(defenderOrderForEffect);
  }, [defenderOrderForEffect]);

  const selectedTerritoryDef: { is_stronghold?: boolean; stronghold_base_health?: number } | undefined =
    !isTerrainSelection && territoryId ? (definitions?.territories?.[territoryId] as { is_stronghold?: boolean; stronghold_base_health?: number } | undefined) : undefined;
  const isSelectedStronghold = !!selectedTerritoryDef?.is_stronghold;
  const strongholdBaseHp = selectedTerritoryDef?.stronghold_base_health ?? 10;
  const strongholdLocked = !isTerrainSelection && territoryId !== '';
  const strongholdHpMax = strongholdLocked && isSelectedStronghold ? strongholdBaseHp : 10;

  /** When defender territory changes, seed stronghold HP once per selection (not on every `territoryData` poll). */
  const seededStrongholdTerritoryIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (isTerrainSelection || !territoryId) {
      seededStrongholdTerritoryIdRef.current = null;
      return;
    }
    if (seededStrongholdTerritoryIdRef.current === territoryId) return;
    seededStrongholdTerritoryIdRef.current = territoryId;
    const def = definitions?.territories?.[territoryId] as { is_stronghold?: boolean; stronghold_base_health?: number } | undefined;
    const baseHp = def?.stronghold_base_health ?? 10;
    const tData = territoryDataRef.current[territoryId] as { stronghold_current_health?: number } | undefined;
    const currentHp = tData?.stronghold_current_health ?? baseHp;
    setStrongholdHpEnabled(!!def?.is_stronghold);
    setStrongholdHpAmount(Math.max(0, Math.min(currentHp, baseHp)));
  }, [territoryId, isTerrainSelection, definitions?.territories]);

  /** Effective territory defender stacks (editable counts). */
  const territoryDefenderStacksWithCounts = useMemo(
    () =>
      defenderStacksFiltered.map(({ unit_id, count: orig }) => ({
        unit_id,
        count: defenderTerritoryCounts[unit_id] ?? orig,
      })),
    [defenderStacksFiltered, defenderTerritoryCounts]
  );

  /** Faction for defender header / add-defender filtering (owner on land, or dominant stack faction on sea / from added units). */
  const defenderLogoFaction = useMemo(() => {
    if (!territoryId) return null;
    if (defenderStacksFiltered.length > 0) {
      if (isLandCombat) {
        const owner = territoryData[territoryId]?.owner;
        return owner ?? null;
      }
      const biggest = defenderStacksFiltered.slice().sort((a, b) => b.count - a.count)[0];
      return biggest ? unitDefs[biggest.unit_id]?.faction ?? null : null;
    }
    if (addedDefenderStacks.length > 0) {
      const first = addedDefenderStacks.find((s) => s.count > 0);
      return first ? unitDefs[first.unit_id]?.faction ?? null : null;
    }
    if (isLandCombat) return territoryData[territoryId]?.owner ?? null;
    return null;
  }, [territoryId, defenderStacksFiltered, addedDefenderStacks, isLandCombat, territoryData, unitDefs]);

  /** All defender stacks for API: territory defenders (editable counts) + user-added, merged by unit_id. */
  const defenderStacksMerged = useMemo(() => {
    const byUnit: Record<string, number> = {};
    territoryDefenderStacksWithCounts.forEach(({ unit_id, count }) => { if (count > 0) byUnit[unit_id] = (byUnit[unit_id] ?? 0) + count; });
    addedDefenderStacks.forEach(({ unit_id, count }) => { if (count > 0) byUnit[unit_id] = (byUnit[unit_id] ?? 0) + count; });
    const rows = Object.entries(byUnit).map(([unit_id, count]) => ({ unit_id, count })).filter((s) => s.count > 0);
    rows.sort((a, b) =>
      compareDefenderUnitIdsByFactionThenName(definitions, unitDefs, a.unit_id, b.unit_id, defenderLogoFaction),
    );
    return rows;
  }, [territoryDefenderStacksWithCounts, addedDefenderStacks, definitions, unitDefs, defenderLogoFaction]);

  /** Total attacker units (for retreat-when max validation). */
  const totalAttackerUnits = useMemo(
    () => attackerUnits.reduce((s, u) => s + (attackerCounts[u.id] ?? 0), 0),
    [attackerUnits, attackerCounts]
  );

  const handleClear = () => {
    setAttackerFaction('');
    setTerritoryId('');
    setAttackerCounts({});
    setAddedDefenderStacks([]);
    setDefenderTerritoryCounts({});
    setResult(null);
    setError(null);
    setCasualtyOrderAttacker('best_unit');
    setCasualtyOrderDefender('best_unit');
    setMustConquer(false);
    setIsSeaRaid(false);
    setRetreatEnabled(false);
    setRetreatWhenUnitsLe(null);
  };

  const handleAttackerCount = (unitId: UnitId, value: number) => {
    setAttackerCounts((prev) => ({ ...prev, [unitId]: Math.max(0, value) }));
  };

  const CHUNK_SIZE = 500;
  const TOTAL_TRIALS = 10000;

  const handleCalculate = async () => {
    if (!territoryId?.trim()) {
      setError('Select a territory');
      return;
    }
    const attStacks = attackerUnits
      .map((u) => ({ unit_id: u.id, count: attackerCounts[u.id] ?? 0 }))
      .filter((s) => s.count > 0);
    const defStacks = defenderStacksMerged;
    if (attStacks.length === 0 || defStacks.length === 0) {
      setError('Add at least one attacker unit and ensure the territory has defenders (or add defending units).');
      return;
    }
    setError(null);
    setLoading(true);
    setResult(null);
    setSimProgressTotal(TOTAL_TRIALS);
    setSimProgressCompleted(0);
    const options = {
      casualty_order_attacker: casualtyOrderAttacker,
      casualty_order_defender: casualtyOrderDefender,
      must_conquer: mustConquer,
      is_sea_raid: isLandCombat && isSeaRaid ? true : undefined,
      retreat_when_attacker_units_le: retreatEnabled && retreatWhenUnitsLe !== null ? retreatWhenUnitsLe : undefined,
      stronghold_initial_hp: strongholdHpEnabled ? Math.max(0, Math.min(strongholdHpAmount, strongholdHpMax)) : undefined,
    };
    const baseParams = {
      attacker_stacks: attStacks,
      defender_stacks: defStacks,
      territory_id: territoryId.trim(),
      game_id: gameId ?? undefined,
      setup_id: gameId ? undefined : (setupId ?? undefined),
      options,
    };
    const allOutcomes: Array<{
      winner: string;
      conquered: boolean;
      retreat: boolean;
      rounds: number;
      attacker_casualties: Record<string, number>;
      defender_casualties: Record<string, number>;
    }> = [];
    let firstChunkResponse: SimulateCombatResponse | null = null;
    try {
      for (let chunk = 0; chunk < TOTAL_TRIALS / CHUNK_SIZE; chunk++) {
        const res = await api.simulateCombat({
          ...baseParams,
          n_trials: CHUNK_SIZE,
          seed: 8 + chunk * CHUNK_SIZE,
          include_outcomes: true,
        });
        if (res.outcomes?.length) {
          allOutcomes.push(...res.outcomes);
        }
        if (chunk === 0) firstChunkResponse = res;
        setSimProgressCompleted((chunk + 1) * CHUNK_SIZE);
        const merged = mergeSimOutcomes(allOutcomes, firstChunkResponse, definitions);
        setResult(merged);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Simulation failed');
    } finally {
      setLoading(false);
      setSimProgressCompleted(0);
      setSimProgressTotal(0);
    }
  };

  /** Specials and shelves from backend battle_context only (after CALCULATE). No live frontend computation. */
  const battleContextSpecials = useMemo(() => {
    const bc = result?.battle_context;
    if (!bc?.specials_in_battle) return {};
    const bySpecial: Record<string, { side: 'attacker' | 'defender'; unitId: string; unitName: string; count: number }[]> = {};
    for (const [sid, entries] of Object.entries(bc.specials_in_battle)) {
      if (!entries?.length) continue;
      bySpecial[sid] = entries.map((e) => ({
        side: e.side as 'attacker' | 'defender',
        unitId: e.unit_id,
        unitName: e.unit_name,
        count: e.count,
      })).sort((a, b) => (a.side !== b.side ? (a.side === 'attacker' ? -1 : 1) : a.unitName.localeCompare(b.unitName)));
    }
    return bySpecial;
  }, [result?.battle_context]);

  const battleContextTerrainLabel = result?.battle_context?.terrain_label ?? '';

  /** Effective shelves from backend battle_context only. Backend sends icon as filename; use full path so images load. */
  const battleContextAttackerShelves = useMemo(() => {
    const bc = result?.battle_context?.effective_attacker_shelves;
    if (!bc) return [];
    return bc.map((s) => ({
      statValue: s.stat_value,
      stacks: s.stacks.map((t) => ({
        unitId: t.unit_id,
        name: t.name,
        icon: unitDefs[t.unit_id]?.icon ?? (t.icon ? `/assets/units/${t.icon}` : ''),
        count: t.count,
        specialCodes: t.special_codes,
        factionId: t.faction_id,
      })),
    }));
  }, [result?.battle_context, unitDefs]);

  const battleContextDefenderShelves = useMemo(() => {
    const bc = result?.battle_context?.effective_defender_shelves;
    if (!bc) return [];
    return bc.map((s) => ({
      statValue: s.stat_value,
      stacks: s.stacks.map((t) => ({
        unitId: t.unit_id,
        name: t.name,
        icon: unitDefs[t.unit_id]?.icon ?? (t.icon ? `/assets/units/${t.icon}` : ''),
        count: t.count,
        specialCodes: t.special_codes,
        factionId: t.faction_id,
      })),
    }));
  }, [result?.battle_context, unitDefs]);

  const battleContextMergedShelves = useMemo(() => {
    const statSet = new Set<number>();
    battleContextAttackerShelves.forEach((s) => statSet.add(s.statValue));
    battleContextDefenderShelves.forEach((s) => statSet.add(s.statValue));
    const stats = Array.from(statSet).sort((a, b) => a - b);
    const attByStat = new Map(battleContextAttackerShelves.map((s) => [s.statValue, s.stacks]));
    const defByStat = new Map(battleContextDefenderShelves.map((s) => [s.statValue, s.stacks]));
    return stats.map((statValue) => ({
      statValue,
      attackerStacks: attByStat.get(statValue) ?? [],
      defenderStacks: defByStat.get(statValue) ?? [],
    }));
  }, [battleContextAttackerShelves, battleContextDefenderShelves]);

  /** Defender unit_id -> effective defense from combat shelves (terrain etc.); used for archer prefire label below. */
  const defenderUnitToEffectiveDef = useMemo(() => {
    const m: Record<string, number> = {};
    battleContextDefenderShelves.forEach((shelf) => {
      shelf.stacks.forEach((stack) => {
        m[stack.unitId] = shelf.statValue;
      });
    });
    return m;
  }, [battleContextDefenderShelves]);

  /**
   * Shown archer prefire threshold = shelf effective defense minus this delta (see "(ND Prefire)" in Specials).
   * Manifest `prefire_penalty: false`: delta 0 — the shelf value is the real hit target; "(2D Prefire)" is accurate.
   * Penalty on (true/omitted in API): delta 1 — matches engine −1 to defense for prefire rolls.
   */
  const archerPrefireLabelThresholdDelta = useMemo(
    () => (result?.prefire_penalty === false ? 0 : 1),
    [result?.prefire_penalty],
  );

  const hasBattleContext = Boolean(result?.battle_context);

  if (!definitions) {
    return (
      <div className="combat-sim-panel">
        <p className="combat-sim-muted">Load a game or definitions to use the simulator.</p>
      </div>
    );
  }

  const specialsDefs = definitions.specials ?? {};
  const specialsOrder = definitions.specials_order?.length ? definitions.specials_order : Object.keys(specialsDefs).sort();
  const getUnitStat = (unitId: string, kind: 'attack' | 'defense') => {
    const u = definitions.units[unitId] as { attack?: number; defense?: number } | undefined;
    const n = kind === 'attack' ? u?.attack : u?.defense;
    return typeof n === 'number' ? n : 0;
  };

  const totalDefenderUnits = useMemo(
    () => defenderStacksMerged.reduce((s, { count }) => s + count, 0),
    [defenderStacksMerged]
  );

  const canSwap =
    !!attackerFaction &&
    !!territoryId &&
    !!defenderLogoFaction &&
    defenderLogoFaction !== 'neutral' &&
    totalAttackerUnits >= 1 &&
    totalDefenderUnits >= 1;

  const handleSwap = () => {
    if (!canSwap || !defenderLogoFaction || !definitions) return;
    const prevDefenderTerritoryId = territoryId;
    const prevAttackingTerritoryId = attackingTerritoryId;

    const defenderCountByUnit: Record<string, number> = {};
    territoryDefenderStacksWithCounts.forEach(({ unit_id, count }) => {
      const c = defenderTerritoryCounts[unit_id] ?? count;
      if (c > 0) defenderCountByUnit[unit_id] = (defenderCountByUnit[unit_id] ?? 0) + c;
    });
    addedDefenderStacks.forEach(({ unit_id, count }) => {
      if (count > 0) defenderCountByUnit[unit_id] = (defenderCountByUnit[unit_id] ?? 0) + count;
    });

    const newAttackerCounts: Record<string, number> = {};
    Object.entries(defenderCountByUnit).forEach(([unit_id, c]) => {
      const unitFaction = (definitions.units[unit_id] as { faction?: string } | undefined)?.faction ?? unitDefs[unit_id]?.faction ?? '';
      if (unitFaction === defenderLogoFaction) newAttackerCounts[unit_id] = c;
    });

    const newDefenderStacks: { unit_id: string; count: number }[] = attackerUnits
      .filter((u) => (attackerCounts[u.id] ?? 0) > 0)
      .map((u) => ({ unit_id: u.id, count: attackerCounts[u.id] ?? 0 }));

    let newDefenderTerritoryId = '';
    if (prevAttackingTerritoryId.trim()) {
      newDefenderTerritoryId = prevAttackingTerritoryId;
    } else if (prevDefenderTerritoryId.startsWith(TERRAIN_PREFIX)) {
      newDefenderTerritoryId = prevDefenderTerritoryId;
    } else {
      const tt = getTerrainTypeFromTerritoryId(definitions, prevDefenderTerritoryId);
      newDefenderTerritoryId = tt ? `${TERRAIN_PREFIX}${tt}` : prevDefenderTerritoryId;
    }

    const swapById: Record<string, number> = {};
    for (const { unit_id, count } of newDefenderStacks) {
      if (count <= 0) continue;
      swapById[unit_id] = (swapById[unit_id] ?? 0) + count;
    }

    let nextDefenderTerritoryCounts: Record<string, number> = {};
    let nextAddedDefenderStacks: { unit_id: string; count: number }[] = [];

    if (newDefenderTerritoryId.startsWith(TERRAIN_PREFIX) || !newDefenderTerritoryId.trim()) {
      seededDefenderTerritoryIdRef.current = null;
      nextAddedDefenderStacks = newDefenderStacks.filter((s) => s.count > 0);
    } else {
      const stacksRaw = (territoryUnits?.[newDefenderTerritoryId] ?? []).filter((s) => s.count > 0);
      const filteredForNew = stacksRaw.filter((s) => unitAllowedForCombatType(definitions, s.unit_id, isLandCombat));
      const onHexIds = new Set(filteredForNew.map((s) => s.unit_id));

      seededDefenderTerritoryIdRef.current = newDefenderTerritoryId;
      for (const { unit_id } of filteredForNew) {
        nextDefenderTerritoryCounts[unit_id] = swapById[unit_id] ?? 0;
      }
      for (const [unit_id, count] of Object.entries(swapById)) {
        if (!onHexIds.has(unit_id)) {
          nextAddedDefenderStacks.push({ unit_id, count });
        }
      }
    }

    setAttackerFaction(defenderLogoFaction);
    setTerritoryId(newDefenderTerritoryId);
    setAttackerCounts(newAttackerCounts);
    setDefenderTerritoryCounts(nextDefenderTerritoryCounts);
    setAddedDefenderStacks(nextAddedDefenderStacks);
    // New attacker stages from the hex they previously defended (when it was a real territory).
    const defendingWasTerrainOnly = prevDefenderTerritoryId.startsWith(TERRAIN_PREFIX);
    setAttackingTerritoryId(defendingWasTerrainOnly ? '' : prevDefenderTerritoryId);
    setCasualtyOrderAttacker('best_unit');
    setResult(null);
    setError(null);
  };

  /** Units available for "Add Defending Units": same alliance as defender (or non-attacker + neutral when generic terrain and empty). When terrain is picked and no defenders yet, show other-alliance and neutral; once one is picked, only that alliance/neutral. Order: primary defender faction first when known, then allied factions (good/evil/…), then neutral/no-alliance last, then faction name, then unit name; land/sea filter. */
  const addDefenderUnitOptions = useMemo(() => {
    if (!definitions?.units) return [];
    const isTerrainOnly = territoryId.startsWith(TERRAIN_PREFIX);
    if (!isTerrainOnly && !defenderLogoFaction) return [];
    let factionIds: string[];
    if (isTerrainOnly) {
      // Include every faction that has units (so neutral is included even if not in definitions.factions)
      const fromFactions = definitions.factions ? Object.keys(definitions.factions) : Object.keys(factionData);
      const fromUnits = [
        ...new Set(
          Object.values(definitions.units).map((u) => ((u as { faction?: string }).faction ?? 'neutral').trim() || 'neutral')
        ),
      ];
      const allIds = [...new Set([...fromFactions, ...fromUnits])];
      const attackerAlliance = attackerFaction
        ? (factionData[attackerFaction]?.alliance ?? (definitions.factions?.[attackerFaction] as { alliance?: string } | undefined)?.alliance)
        : '';
      if (defenderLogoFaction) {
        // Once at least one defender unit is chosen (terrain + added), restrict to that faction's alliance (or just that faction for neutral)
        const alliance = factionData[defenderLogoFaction]?.alliance ?? (definitions.factions?.[defenderLogoFaction] as { alliance?: string } | undefined)?.alliance;
        if (alliance === 'neutral' || !alliance) {
          factionIds = [defenderLogoFaction];
        } else {
          factionIds = definitions.factions
            ? Object.entries(definitions.factions).filter(([, f]) => (f as { alliance?: string }).alliance === alliance).map(([id]) => id)
            : Object.entries(factionData).filter(([, d]) => d?.alliance === alliance).map(([id]) => id);
        }
      } else {
        // Empty defender shelf on terrain: show all non-attacker-alliance factions (enemy + neutral)
        factionIds = attackerAlliance
          ? allIds.filter(
            (id) =>
              (factionData[id]?.alliance ?? (definitions.factions?.[id] as { alliance?: string } | undefined)?.alliance) !== attackerAlliance
          )
          : allIds;
        // Sea: only factions that have purchasable naval types (defense is ships only)
        if (!isLandCombat) {
          factionIds = factionIds.filter((id) => factionHasPurchasableNavalForSeaDefense(definitions, id));
        }
      }
    } else {
      const alliance = factionData[defenderLogoFaction!]?.alliance ?? (definitions.factions?.[defenderLogoFaction!] as { alliance?: string } | undefined)?.alliance;
      if (!alliance) return [];
      factionIds = definitions.factions
        ? Object.entries(definitions.factions).filter(([, f]) => (f as { alliance?: string }).alliance === alliance).map(([id]) => id)
        : Object.entries(factionData).filter(([, d]) => d?.alliance === alliance).map(([id]) => id);
    }
    const units: { id: string; name: string; factionId: string; factionName: string }[] = [];
    for (const [uid, u] of Object.entries(definitions.units)) {
      const faction = (u as { faction?: string }).faction ?? 'neutral';
      if (!factionIds.includes(faction)) continue;
      if (!isLandCombat && !isUnitPurchasableInDefs(u)) continue;
      if (!unitAllowedForDefenseInCombatType(definitions, uid, isLandCombat)) continue;
      const f = definitions.factions?.[faction] as { display_name?: string } | undefined;
      units.push({
        id: uid,
        name: (u as { display_name?: string }).display_name ?? uid,
        factionId: faction,
        factionName: f?.display_name ?? faction,
      });
    }
    units.sort((a, b) => {
      if (defenderLogoFaction) {
        const pa = a.factionId === defenderLogoFaction ? 0 : 1;
        const pb = b.factionId === defenderLogoFaction ? 0 : 1;
        if (pa !== pb) return pa - pb;
      }
      const ta = addDefenderFactionAllianceSortTier(a.factionId, definitions, factionData);
      const tb = addDefenderFactionAllianceSortTier(b.factionId, definitions, factionData);
      if (ta !== tb) return ta - tb;
      const fc = a.factionName.localeCompare(b.factionName, undefined, { sensitivity: 'base' });
      if (fc !== 0) return fc;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    });
    return units;
  }, [definitions, factionData, defenderLogoFaction, isLandCombat, territoryId, attackerFaction, unitDefs]);

  /** Exclude unit types already on the defender shelf (territory rows with count > 0, or added) so we only offer units not yet listed. */
  const addDefenderUnitOptionsFiltered = useMemo(() => {
    const alreadyOnShelf = new Set<string>(addedDefenderStacks.map((s) => s.unit_id));
    defenderStacksFiltered.forEach(({ unit_id, count: orig }) => {
      if ((defenderTerritoryCounts[unit_id] ?? orig) > 0) alreadyOnShelf.add(unit_id);
    });
    return addDefenderUnitOptions.filter((u) => !alreadyOnShelf.has(u.id));
  }, [addDefenderUnitOptions, defenderStacksFiltered, addedDefenderStacks, defenderTerritoryCounts]);

  function percentToColor(p: number): string {
    if (p <= 0) return 'hsl(0, 75%, 45%)';
    if (p >= 1) return 'hsl(120, 75%, 35%)';
    const hue = p * 120;
    return `hsl(${hue}, 75%, 40%)`;
  }

  return (
    <div className="combat-sim-panel">
      {!embedded && (
        <div className="combat-sim-header">
          <h3 className="combat-sim-title">Battle Simulator</h3>
          {onClose && (
            <button type="button" className="combat-sim-close" onClick={onClose} title="Hide Combat Simulator" aria-label="Hide Combat Simulator">
              ▶
            </button>
          )}
        </div>
      )}

      <div className="combat-sim-pill-row">
        <button
          type="button"
          className={`combat-sim-pill ${isLandCombat ? 'combat-sim-pill--active' : ''}`}
          onClick={() => setIsLandCombat(true)}
        >
          Land
        </button>
        <button
          type="button"
          className={`combat-sim-pill ${!isLandCombat ? 'combat-sim-pill--active' : ''}`}
          onClick={() => setIsLandCombat(false)}
        >
          Sea
        </button>
      </div>

      <div className="combat-sim-dropdowns-row">
        <div className="combat-sim-attacker-stack">
          <div className="combat-sim-field">
            <label className="combat-sim-label">Attacking Faction</label>
            <select
              className="combat-sim-select"
              value={attackerFaction}
              onChange={(e) => {
                setAttackerFaction(e.target.value);
                setAttackingTerritoryId('');
                setAttackerCounts({});
                setTerritoryId('');
              }}
            >
              <option value="">— Select —</option>
              {factions.map((f) => (
                <option key={f.id} value={f.id}>{f.name}</option>
              ))}
            </select>
          </div>
          <div className="combat-sim-field combat-sim-field--attacking-territory">
            <label className="combat-sim-label">Attacking Territory (optional)</label>
            <select
              className="combat-sim-select"
              value={attackingTerritoryId}
              onChange={(e) => setAttackingTerritoryId(e.target.value)}
              disabled={!attackerFaction}
              title={!attackerFaction ? 'Select attacking faction first' : undefined}
            >
              <option value="">— Select —</option>
              {attackingTerritoryOptions.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="combat-sim-swap-wrap">
          <button
            type="button"
            className="combat-sim-swap-btn"
            onClick={handleSwap}
            disabled={!canSwap}
            title={canSwap ? 'Swap attacker and defender' : 'Select attacker, territory, and at least one unit on each side (defender must not be neutral)'}
            aria-label="Swap attacker and defender"
          >
            ↔
          </button>
        </div>
        <div className="combat-sim-field">
          <label className="combat-sim-label">Defending Terrain or Territory</label>
          <select
            className="combat-sim-select"
            value={territoryId}
            onChange={(e) => {
              setTerritoryId(e.target.value);
              setAddedDefenderStacks([]);
            }}
            disabled={!attackerFaction}
            title={!attackerFaction ? 'Select attacking faction first' : undefined}
          >
            <option value="">— Select —</option>
            {territoryDropdownOptions.map((t) => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="combat-sim-logos-section">
        <div className="combat-sim-logos-row">
          <div className="combat-sim-logos-col">
            {attackerFaction && factionData[attackerFaction]?.icon && (
              <img src={factionData[attackerFaction].icon} alt="" className="combat-sim-faction-logo" />
            )}
          </div>
          <div className="combat-sim-logos-vs">
            <span className="combat-sim-vs-text">VS</span>
          </div>
          <div className="combat-sim-logos-col">
            {defenderLogoFaction && defenderLogoFaction !== 'neutral' && factionData[defenderLogoFaction]?.icon && (
              <img src={factionData[defenderLogoFaction].icon} alt="" className="combat-sim-faction-logo" />
            )}
          </div>
        </div>
        <hr className="combat-sim-divider" />
      </div>

      <div className="combat-sim-attack-defense-section">
        <div className="combat-sim-units-row">
          <div className="combat-sim-units-col">
            {attackerUnits.length > 0 && (
              <>
                <label className="combat-sim-label combat-sim-units-header">Attacking Units</label>
                <div className="combat-sim-units">
                  {attackerUnits.map((u) => {
                    const baseAttack = getUnitStat(u.id, 'attack');
                    const powerCost = getUnitPowerCost(definitions, u.id);
                    const { dice, health } = getUnitDiceAndHealth(definitions, u.id);
                    const baseStatText = ` (${powerCost}P | ${baseAttack}A | ${dice}R | ${health}HP)`;
                    const displayName = unitDefs[u.id]?.name ?? u.name;
                    return (
                      <div key={u.id} className="combat-sim-unit-row">
                        {unitDefs[u.id]?.icon && (
                          <span className="combat-sim-unit-icon-wrap">
                            <img src={unitDefs[u.id].icon} alt="" className="combat-sim-unit-icon" />
                          </span>
                        )}
                        <div className="combat-sim-unit-main">
                          <span className="combat-sim-unit-name-text">{displayName}</span>
                          <span className="combat-sim-unit-stat">{baseStatText}</span>
                        </div>
                        <div className="combat-sim-unit-row-controls">
                          <SimCountStepper
                            value={attackerCounts[u.id] ?? 0}
                            onChange={(n) => handleAttackerCount(u.id, n)}
                            unitLabel={displayName}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
          <div className="combat-sim-units-col">
            {territoryId && (
              <>
                <label className="combat-sim-label combat-sim-units-header">Defending Units</label>
                <div className="combat-sim-units">
                  {defenderStacksFiltered.length === 0 && addedDefenderStacks.length === 0 ? (
                    territoryId.startsWith(TERRAIN_PREFIX) ? null : (
                      <p className="combat-sim-muted">No units in this territory.</p>
                    )
                  ) : (
                    <>
                      {defenderStacksFiltered
                        .filter(({ unit_id, count: origCount }) => (defenderTerritoryCounts[unit_id] ?? origCount) > 0)
                        .slice()
                        .sort((a, b) =>
                          compareDefenderUnitIdsByFactionThenName(
                            definitions,
                            unitDefs,
                            a.unit_id,
                            b.unit_id,
                            defenderLogoFaction,
                          ),
                        )
                        .map(({ unit_id, count: origCount }) => {
                          const count = defenderTerritoryCounts[unit_id] ?? origCount;
                          const baseDef = getUnitStat(unit_id, 'defense');
                          const powerCost = getUnitPowerCost(definitions, unit_id);
                          const { dice, health } = getUnitDiceAndHealth(definitions, unit_id);
                          const baseStatText = ` (${powerCost}P | ${baseDef}D | ${dice}R | ${health}HP)`;
                          const dName = unitDefs[unit_id]?.name ?? unit_id;
                          return (
                            <div key={`t-${unit_id}`} className="combat-sim-unit-row combat-sim-unit-row--defender">
                              {unitDefs[unit_id]?.icon && (
                                <span className="combat-sim-unit-icon-wrap">
                                  <img src={unitDefs[unit_id].icon} alt="" className="combat-sim-unit-icon" />
                                </span>
                              )}
                              <div className="combat-sim-unit-main">
                                <span className="combat-sim-unit-name-text">{dName}</span>
                                <span className="combat-sim-unit-stat">{baseStatText}</span>
                              </div>
                              <div className="combat-sim-unit-row-controls">
                                <SimCountStepper
                                  value={count}
                                  onChange={(n) =>
                                    setDefenderTerritoryCounts((prev) => ({ ...prev, [unit_id]: n }))
                                  }
                                  unitLabel={dName}
                                />
                                <button
                                  type="button"
                                  className="combat-sim-defender-remove"
                                  onClick={() => setDefenderTerritoryCounts((prev) => ({ ...prev, [unit_id]: 0 }))}
                                  title="Remove from defenders"
                                  aria-label={`Remove ${dName} from defenders`}
                                >
                                  ×
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      {addedDefenderStacks.length > 0 &&
                        defenderStacksFiltered.some(
                          ({ unit_id, count: origCount }) => (defenderTerritoryCounts[unit_id] ?? origCount) > 0
                        ) && <hr className="combat-sim-divider combat-sim-divider--thin" />}
                      {addedDefenderStacks
                        .map((row, idx) => ({ ...row, idx }))
                        .slice()
                        .sort((a, b) =>
                          compareDefenderUnitIdsByFactionThenName(
                            definitions,
                            unitDefs,
                            a.unit_id,
                            b.unit_id,
                            defenderLogoFaction,
                          ),
                        )
                        .map(({ unit_id, count, idx }) => {
                          const baseDef = getUnitStat(unit_id, 'defense');
                          const powerCost = getUnitPowerCost(definitions, unit_id);
                          const { dice, health } = getUnitDiceAndHealth(definitions, unit_id);
                          const baseStatText = ` (${powerCost}P | ${baseDef}D | ${dice}R | ${health}HP)`;
                          const aName = unitDefs[unit_id]?.name ?? unit_id;
                          return (
                            <div key={`a-${idx}-${unit_id}`} className="combat-sim-unit-row combat-sim-unit-row--defender">
                              {unitDefs[unit_id]?.icon && (
                                <span className="combat-sim-unit-icon-wrap">
                                  <img src={unitDefs[unit_id].icon} alt="" className="combat-sim-unit-icon" />
                                </span>
                              )}
                              <div className="combat-sim-unit-main">
                                <span className="combat-sim-unit-name-text">{aName}</span>
                                <span className="combat-sim-unit-stat">{baseStatText}</span>
                              </div>
                              <div className="combat-sim-unit-row-controls">
                                <SimCountStepper
                                  value={count}
                                  onChange={(n) =>
                                    setAddedDefenderStacks((prev) =>
                                      prev.map((s, i) => (i === idx ? { ...s, count: n } : s)),
                                    )
                                  }
                                  unitLabel={aName}
                                />
                                <button
                                  type="button"
                                  className="combat-sim-defender-remove"
                                  onClick={() => setAddedDefenderStacks((prev) => prev.filter((_, i) => i !== idx))}
                                  title="Remove from defenders"
                                  aria-label={`Remove ${aName} from defenders`}
                                >
                                  ×
                                </button>
                              </div>
                            </div>
                          );
                        })}
                    </>
                  )}
                  {(defenderLogoFaction || territoryId.startsWith(TERRAIN_PREFIX)) && addDefenderUnitOptionsFiltered.length > 0 && (
                    <div className="combat-sim-add-defender-wrap">
                      <button
                        type="button"
                        className="combat-sim-add-defender-btn"
                        onClick={() => setAddDefenderDropdownOpen((o) => !o)}
                        aria-expanded={addDefenderDropdownOpen}
                        aria-haspopup="listbox"
                      >
                        <span className="combat-sim-add-defender-plus">+</span>
                        <span>Add Defending Units</span>
                      </button>
                      {addDefenderDropdownOpen && (
                        <ul className="combat-sim-add-defender-list" role="listbox">
                          {addDefenderUnitOptionsFiltered.map((u) => (
                            <li
                              key={u.id}
                              role="option"
                              className="combat-sim-add-defender-option"
                              onClick={() => {
                                setAddedDefenderStacks((prev) => [...prev, { unit_id: u.id, count: 1 }]);
                                setAddDefenderDropdownOpen(false);
                              }}
                            >
                              {u.name} <span className="combat-sim-add-defender-faction">({u.factionName})</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>

        <hr className="combat-sim-divider combat-sim-divider--thin" />

        <div className="combat-sim-options-row">
          <div className="combat-sim-options-col">
            <div className="combat-sim-option-line">
              <span className="combat-sim-option-name">Casualty Priority</span>
              <div className="combat-sim-pill-row combat-sim-pill-row--inline">
                <button type="button" className={`combat-sim-pill combat-sim-pill--small ${casualtyOrderAttacker === 'best_unit' ? 'combat-sim-pill--active' : ''}`} onClick={() => setCasualtyOrderAttacker('best_unit')}>Best unit</button>
                <button type="button" className={`combat-sim-pill combat-sim-pill--small ${casualtyOrderAttacker === 'best_attack' ? 'combat-sim-pill--active' : ''}`} onClick={() => setCasualtyOrderAttacker('best_attack')}>Best attack</button>
              </div>
            </div>
            <div className="combat-sim-option-line">
              <label className="combat-sim-checkbox-wrap">
                <input type="checkbox" className="combat-sim-checkbox" checked={mustConquer} onChange={(e) => setMustConquer(e.target.checked)} />
              </label>
              <span className="combat-sim-option-name">Must Conquer</span>
            </div>
            {isLandCombat && (
              <div className="combat-sim-option-line">
                <label className="combat-sim-checkbox-wrap">
                  <input type="checkbox" className="combat-sim-checkbox" checked={isSeaRaid} onChange={(e) => setIsSeaRaid(e.target.checked)} />
                </label>
                <span className="combat-sim-option-name" title="Land combat: Sea Raider units get +1 attack (passengers from sea). Boats do not fight in this battle.">
                  Sea raid
                </span>
              </div>
            )}
            <div className="combat-sim-option-line combat-sim-retreat-row">
              <label className="combat-sim-checkbox-wrap">
                <input type="checkbox" className="combat-sim-checkbox" checked={retreatEnabled} onChange={(e) => setRetreatEnabled(e.target.checked)} />
              </label>
              <span className="combat-sim-retreat-label">Retreat when</span>
              <input
                type="number"
                min={0}
                max={totalAttackerUnits}
                className="combat-sim-retreat-input"
                value={retreatWhenUnitsLe ?? ''}
                disabled={!retreatEnabled}
                onChange={(e) => {
                  const v = e.target.value.trim();
                  if (v === '') {
                    setRetreatWhenUnitsLe(null);
                    return;
                  }
                  const n = parseInt(v, 10);
                  if (Number.isNaN(n) || n < 0) {
                    setRetreatWhenUnitsLe(null);
                    return;
                  }
                  setRetreatWhenUnitsLe(Math.min(n, totalAttackerUnits));
                }}
                placeholder="—"
                title={`Retreat when attacker has this many or fewer units remaining (max ${totalAttackerUnits})`}
              />
              <span className="combat-sim-retreat-suffix">unit(s) remain</span>
            </div>
            <div className="combat-sim-option-line combat-sim-retreat-row">
              <label className="combat-sim-checkbox-wrap">
                <input
                  type="checkbox"
                  className="combat-sim-checkbox"
                  checked={strongholdLocked ? isSelectedStronghold : strongholdHpEnabled}
                  disabled={strongholdLocked}
                  onChange={(e) => !strongholdLocked && setStrongholdHpEnabled(e.target.checked)}
                />
              </label>
              <span className="combat-sim-retreat-label">Stronghold HP</span>
              <input
                type="number"
                min={0}
                max={strongholdHpMax}
                className="combat-sim-retreat-input"
                value={strongholdHpAmount}
                disabled={strongholdLocked ? !isSelectedStronghold : !strongholdHpEnabled}
                onChange={(e) => {
                  const v = e.target.value.trim();
                  if (v === '') {
                    setStrongholdHpAmount(0);
                    return;
                  }
                  const n = parseInt(v, 10);
                  if (!Number.isNaN(n) && n >= 0) {
                    setStrongholdHpAmount(Math.min(n, strongholdHpMax));
                  }
                }}
                title={strongholdLocked ? (isSelectedStronghold ? `Defender stronghold current HP (0–${strongholdHpMax})` : 'Not a stronghold') : `Defender stronghold starting HP for sim (0–${strongholdHpMax})`}
              />
            </div>
          </div>
          <div className="combat-sim-options-col">
            <div className="combat-sim-option-line">
              <span className="combat-sim-option-name">Casualty Priority</span>
              <div className="combat-sim-pill-row combat-sim-pill-row--inline">
                <button type="button" className={`combat-sim-pill combat-sim-pill--small ${casualtyOrderDefender === 'best_unit' ? 'combat-sim-pill--active' : ''}`} onClick={() => setCasualtyOrderDefender('best_unit')}>Best unit</button>
                <button type="button" className={`combat-sim-pill combat-sim-pill--small ${casualtyOrderDefender === 'best_defense' ? 'combat-sim-pill--active' : ''}`} onClick={() => setCasualtyOrderDefender('best_defense')}>Best defense</button>
              </div>
            </div>
          </div>
        </div>

        <hr className="combat-sim-divider" />
      </div>

      {error && <p className="combat-sim-error">{error}</p>}

      <div className="combat-sim-actions">
        <button type="button" className="combat-sim-clear" onClick={handleClear}>Clear</button>
        <div className="combat-sim-calculate-wrap">
          <button type="button" className="combat-sim-calculate" onClick={handleCalculate} disabled={loading}>
            {loading && simProgressTotal > 0
              ? `CALCULATE (${Math.round((simProgressCompleted / simProgressTotal) * 100)}%)`
              : 'CALCULATE'}
          </button>
          {loading && simProgressTotal > 0 && (
            <div className="combat-sim-progress-track">
              <div
                className="combat-sim-progress-fill"
                style={{ width: `${(simProgressCompleted / simProgressTotal) * 100}%` }}
              />
            </div>
          )}
        </div>
      </div>

      <div className="combat-sim-lower">
        <div className="combat-sim-lower-left">
          {hasBattleContext && (
            <>
              {(battleContextTerrainLabel || Object.keys(battleContextSpecials).length > 0) && (
                <div className="combat-sim-terrain-specials-box">
                  <div className="combat-sim-terrain-specials-header">
                    {Object.keys(battleContextSpecials).length > 0 && <h4 className="combat-sim-specials-title">Specials</h4>}
                    {battleContextTerrainLabel && <p className="combat-sim-terrain">Terrain: {battleContextTerrainLabel}</p>}
                  </div>
                  {Object.keys(battleContextSpecials).length > 0 && (
                    <div className="combat-sim-specials">
                      <div className="combat-sim-specials-list">
                        {specialsOrder
                          .filter((sid) => battleContextSpecials[sid]?.length)
                          .map((specialId) => {
                            const def = specialsDefs[specialId] as { name?: string; display_code?: string } | undefined;
                            const label = def?.name ?? specialId;
                            const rawEntries = battleContextSpecials[specialId];
                            const entries =
                              specialId === 'terror'
                                ? (() => {
                                  let remaining = 3;
                                  return rawEntries
                                    .map((e) => {
                                      const effective = Math.min(e.count, remaining);
                                      remaining -= effective;
                                      return { ...e, count: effective };
                                    })
                                    .filter((e) => e.count > 0);
                                })()
                                : rawEntries;
                            return (
                              <div key={specialId} className="combat-sim-special-group">
                                <span className="combat-sim-special-title">{label}</span>
                                <div className="combat-sim-special-units">
                                  {entries.map((e) => {
                                    const prefireDice =
                                      specialId === 'archer' && e.side === 'defender'
                                        ? Math.max(0, (defenderUnitToEffectiveDef[e.unitId] ?? 0) - archerPrefireLabelThresholdDelta)
                                        : 0;
                                    return (
                                      <span key={`${e.side}-${e.unitId}`} className="combat-sim-special-unit">
                                        {e.unitName} ×{e.count}
                                        {specialId === 'archer' && ` (${prefireDice}D Prefire)`}
                                      </span>
                                    );
                                  })}
                                </div>
                              </div>
                            );
                          })}
                      </div>
                    </div>
                  )}
                </div>
              )}
              {battleContextMergedShelves.length > 0 && (
                <div className="combat-sim-units-in-battle-box">
                  <div className="combat-sim-units-in-battle-header-row">
                    <div className="combat-sim-units-in-battle-side-label">Attacker</div>
                    <div className="combat-sim-units-in-battle-side-label">Defender</div>
                  </div>
                  {battleContextMergedShelves.map(({ statValue, attackerStacks, defenderStacks }) => {
                    const toDisplayCode = (sid: string) => (specialsDefs[sid] as { display_code?: string } | undefined)?.display_code ?? sid;
                    return (
                      <div key={statValue} className="combat-sim-preview-shelf-row">
                        <div
                          className={
                            attackerStacks.length === 0
                              ? 'combat-sim-preview-shelf combat-sim-preview-shelf--empty'
                              : 'combat-sim-preview-shelf'
                          }
                        >
                          {attackerStacks.length > 0 && <div className="combat-sim-preview-stat-label">{statValue}</div>}
                          <div className="combat-sim-preview-unit-stack">
                            {attackerStacks.map((group, idx) => (
                              <div
                                key={`att-${statValue}-${idx}-${group.unitId}`}
                                className="combat-sim-preview-unit-group"
                                title={group.name}
                                style={factionData[group.factionId]?.color ? { borderColor: factionData[group.factionId].color } : undefined}
                              >
                                {group.specialCodes.length > 0 && (
                                  <span className="combat-sim-preview-specials-badge">{group.specialCodes.map(toDisplayCode).filter(Boolean).join('')}</span>
                                )}
                                {group.icon ? <img src={group.icon} alt={group.name} /> : null}
                                <span className="combat-sim-preview-unit-count">{group.count}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                        <div
                          className={
                            defenderStacks.length === 0
                              ? 'combat-sim-preview-shelf combat-sim-preview-shelf--empty'
                              : 'combat-sim-preview-shelf'
                          }
                        >
                          {defenderStacks.length > 0 && <div className="combat-sim-preview-stat-label">{statValue}</div>}
                          <div className="combat-sim-preview-unit-stack">
                            {defenderStacks.map((group, idx) => (
                              <div
                                key={`${group.unitId}-${idx}`}
                                className="combat-sim-preview-unit-group"
                                title={group.name}
                                style={factionData[group.factionId]?.color ? { borderColor: factionData[group.factionId].color } : undefined}
                              >
                                {group.specialCodes.length > 0 && (
                                  <span className="combat-sim-preview-specials-badge">{group.specialCodes.map(toDisplayCode).filter(Boolean).join('')}</span>
                                )}
                                {group.icon ? <img src={group.icon} alt={group.name} /> : null}
                                <span className="combat-sim-preview-unit-count">{group.count}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
        <div className="combat-sim-lower-right">
          {result && (
            <>
              <div className="combat-sim-results">
                <table className="combat-sim-results-table">
                  <thead>
                    <tr className="combat-sim-results-top-row">
                      <th className="combat-sim-results-rounds-th">Rounds: {result.rounds_mean.toFixed(1)}</th>
                      <th className="combat-sim-results-th">Attack</th>
                      <th className="combat-sim-results-th">Defense</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="combat-sim-results-td-label">Casualties</td>
                      <td className="combat-sim-results-td">{result.attacker_casualties_total_mean.toFixed(1)}</td>
                      <td className="combat-sim-results-td">{result.defender_casualties_total_mean.toFixed(1)}</td>
                    </tr>
                    <tr>
                      <td className="combat-sim-results-td-label">Casualty Cost</td>
                      <td className="combat-sim-results-td">{typeof result.attacker_casualty_cost_mean === 'number' ? result.attacker_casualty_cost_mean.toFixed(1) : '—'}</td>
                      <td className="combat-sim-results-td">{typeof result.defender_casualty_cost_mean === 'number' ? result.defender_casualty_cost_mean.toFixed(1) : '—'}</td>
                    </tr>
                    <tr>
                      <td className="combat-sim-results-td-label">Casualty Cost Variance</td>
                      <td className="combat-sim-results-td combat-sim-results-td-variance">{result.attacker_casualty_cost_variance_category ?? '—'}</td>
                      <td className="combat-sim-results-td combat-sim-results-td-variance">{result.defender_casualty_cost_variance_category ?? '—'}</td>
                    </tr>
                    <tr>
                      <td
                        className="combat-sim-results-td-label"
                        title="% of trials with at least one unit of that side remaining when combat ends (mutual destruction counts as neither)."
                      >
                        Survives
                      </td>
                      <td className="combat-sim-results-td">
                        <span
                          className="combat-sim-pct-pill"
                          style={{ backgroundColor: percentToColor(result.p_attacker_survives) }}
                          title="Share of trials with &gt;0 attacking units left at end."
                        >
                          {(result.p_attacker_survives * 100).toFixed(1)}%
                        </span>
                      </td>
                      <td className="combat-sim-results-td">
                        <span
                          className="combat-sim-pct-pill"
                          style={{ backgroundColor: percentToColor(result.p_defender_survives) }}
                          title="Share of trials with &gt;0 defending units left at end."
                        >
                          {(result.p_defender_survives * 100).toFixed(1)}%
                        </span>
                      </td>
                    </tr>
                    <tr>
                      <td className="combat-sim-results-td-label">Conquers</td>
                      <td className="combat-sim-results-td">
                        {isLandCombat
                          ? <span className="combat-sim-pct-pill" style={{ backgroundColor: percentToColor(result.p_conquer) }}>{(result.p_conquer * 100).toFixed(1)}%</span>
                          : '—'}
                      </td>
                      <td className="combat-sim-results-td">—</td>
                    </tr>
                    {(typeof result.attacker_siegework_hits_mean === 'number' ||
                      typeof result.defender_siegework_hits_mean === 'number') && (
                      <tr>
                        <td className="combat-sim-results-td-label" title="Dedicated siegeworks round (before round 1).">
                          Siegework Hits
                        </td>
                        <td className="combat-sim-results-td">
                          {typeof result.attacker_siegework_hits_mean === 'number'
                            ? result.attacker_siegework_hits_mean.toFixed(1)
                            : '—'}
                        </td>
                        <td className="combat-sim-results-td">
                          {typeof result.defender_siegework_hits_mean === 'number'
                            ? result.defender_siegework_hits_mean.toFixed(1)
                            : '—'}
                        </td>
                      </tr>
                    )}
                    <tr>
                      <td className="combat-sim-results-td-label">Prefire Hits</td>
                      <td className="combat-sim-results-td">{typeof result.attacker_prefire_hits_mean === 'number' ? result.attacker_prefire_hits_mean.toFixed(1) : '—'}</td>
                      <td className="combat-sim-results-td">{typeof result.defender_prefire_hits_mean === 'number' ? result.defender_prefire_hits_mean.toFixed(1) : '—'}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              {(result.percentile_outcomes?.length ?? 0) > 0 && (
                <div className="combat-sim-percentiles">
                  <table className="combat-sim-percentiles-table">
                    <thead>
                      <tr>
                        <th className="combat-sim-percentiles-th-label combat-sim-percentiles-th">Attacker Luck</th>
                        <th className="combat-sim-percentiles-th">Result</th>
                        <th className="combat-sim-percentiles-th">Casualties</th>
                        <th className="combat-sim-percentiles-th">Destroyed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        const totalDefenderUnits = defenderStacksMerged.reduce((s, { count }) => s + count, 0);
                        const formatCasualties = (casualties: Record<string, number>, total: number) => {
                          const sum = Object.values(casualties).reduce((a, c) => a + c, 0);
                          if (sum === 0) return { list: 'None', totalP: 0 };
                          if (total > 0 && sum >= total) {
                            const entries = Object.entries(casualties).filter(([, c]) => c > 0);
                            const totalP = entries.reduce((s, [uid, c]) => s + c * getUnitPowerCost(definitions, uid), 0);
                            return { list: 'All', totalP };
                          }
                          const entries = Object.entries(casualties)
                            .filter(([, c]) => c > 0)
                            .sort(([a], [b]) => (unitDefs[a]?.name ?? a).localeCompare(unitDefs[b]?.name ?? b));
                          const totalP = entries.reduce((s, [uid, c]) => s + c * getUnitPowerCost(definitions, uid), 0);
                          const abbrevs = entries.map(([uid]) => abbreviateUnitName(unitDefs[uid]?.name ?? uid));
                          const hasDuplicateAbbrev = new Set(abbrevs).size !== abbrevs.length;
                          const list = entries
                            .map(([uid, c]) => {
                              const fullName = unitDefs[uid]?.name ?? uid;
                              const displayName = hasDuplicateAbbrev ? fullName : abbreviateUnitName(fullName);
                              return `${c} ${displayName}`;
                            })
                            .join(', ');
                          return { list, totalP };
                        };
                        return [
                          { p: 5, label: 'Very Lucky', luckValue: 1 },
                          { p: 25, label: 'Kinda Lucky', luckValue: 0.75 },
                          { p: 50, label: 'Balanced', luckValue: 0.5 },
                          { p: 75, label: 'Kinda Unlucky', luckValue: 0.25 },
                          { p: 95, label: 'Very Unlucky', luckValue: 0 },
                        ].map(({ p, label, luckValue }) => {
                          const po = result!.percentile_outcomes!.find((o) => o.percentile === p);
                          if (!po) return null;
                          const casualtyFmt = formatCasualties(po.attacker_casualties, totalAttackerUnits);
                          const destroyedFmt = formatCasualties(po.defender_casualties, totalDefenderUnits);
                          const casualtyStr = casualtyFmt.totalP > 0 ? `${casualtyFmt.totalP}P: ${casualtyFmt.list}` : casualtyFmt.list;
                          const destroyedStr = destroyedFmt.totalP > 0 ? `${destroyedFmt.totalP}P: ${destroyedFmt.list}` : destroyedFmt.list;
                          let resultLabel: string;
                          if (po.conquered) resultLabel = 'Conquer';
                          else if (po.retreat) resultLabel = 'Retreat';
                          else if (po.winner === 'defender') resultLabel = 'Defeat';
                          else resultLabel = 'Survive';
                          return (
                            <tr key={p} className="combat-sim-percentile-row">
                              <td className="combat-sim-percentiles-td-label">
                                <span className="combat-sim-percentile-pill" style={{ backgroundColor: percentToColor(luckValue) }}>
                                  {label}
                                </span>
                              </td>
                              <td className="combat-sim-percentiles-td">{resultLabel}</td>
                              <td className="combat-sim-percentiles-td">{casualtyStr}</td>
                              <td className="combat-sim-percentiles-td">{destroyedStr}</td>
                            </tr>
                          );
                        });
                      })()}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
