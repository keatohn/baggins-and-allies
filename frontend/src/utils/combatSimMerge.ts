/**
 * Merge per-trial combat-sim outcomes into one SimulateCombatResponse.
 *
 * Used by the simulator panel for chunked progressive results: each 500-trial
 * chunk returns raw outcomes, and this folds every completed chunk into a single
 * aggregate. Prefire and siegeworks hit means are recomputed from *all* merged
 * trials (not just the first chunk) so later chunks cannot be ignored.
 */

import type { Definitions, SimulateCombatResponse } from '../services/api';

export type SimTrialOutcome = {
  winner: string;
  conquered: boolean;
  retreat: boolean;
  rounds: number;
  attacker_casualties: Record<string, number>;
  defender_casualties: Record<string, number>;
  attacker_survived?: boolean;
  defender_survived?: boolean;
  attacker_prefire_hits?: number;
  defender_prefire_hits?: number;
  attacker_prefire_applicable?: boolean;
  defender_prefire_applicable?: boolean;
  attacker_siegework_hits?: number;
  defender_siegework_hits?: number;
  siegework_round_applicable?: boolean;
  siegework_attacker_dice?: number;
  siegework_defender_dice?: number;
};

export function getUnitPowerCost(definitions: Definitions | null, unitId: string): number {
  if (!definitions?.units) return 0;
  const u = definitions.units[unitId] as { cost?: number | { power?: number } } | undefined;
  const c = u?.cost;
  return typeof c === 'object' && c && 'power' in c ? (c as { power: number }).power : typeof c === 'number' ? c : 0;
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

function trialAttackerSurvived(o: SimTrialOutcome): boolean {
  if (typeof o.attacker_survived === 'boolean') return o.attacker_survived;
  return o.winner === 'attacker' || o.retreat;
}

/** True if at least one defending unit is alive at end (mutual destruction => false). */
function trialDefenderSurvived(o: SimTrialOutcome): boolean {
  if (typeof o.defender_survived === 'boolean') return o.defender_survived;
  return o.winner === 'defender' && !o.retreat;
}

/**
 * Mean hits over trials where that prefire actually ran (matches backend
 * `run_simulation`: only trials with `*_prefire_applicable` contribute).
 * Falls back to the first chunk's mean when no merged trial carries the flag
 * (legacy payloads / no prefire in this matchup).
 */
export function computePrefireHitsMeans(
  outcomes: readonly SimTrialOutcome[],
  fallback: { attacker: number | null; defender: number | null }
): { attacker: number | null; defender: number | null } {
  const attTrials = outcomes.filter((o) => o.attacker_prefire_applicable === true);
  const defTrials = outcomes.filter((o) => o.defender_prefire_applicable === true);
  const attacker =
    attTrials.length > 0
      ? attTrials.reduce((s, o) => s + (o.attacker_prefire_hits ?? 0), 0) / attTrials.length
      : fallback.attacker;
  const defender =
    defTrials.length > 0
      ? defTrials.reduce((s, o) => s + (o.defender_prefire_hits ?? 0), 0) / defTrials.length
      : fallback.defender;
  return { attacker, defender };
}

/** Merge per-trial outcomes into a single SimulateCombatResponse (for chunked progressive results). */
export function mergeSimOutcomes(
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
      attacker_prefire_hits_mean: firstChunkResponse?.attacker_prefire_hits_mean ?? null,
      defender_prefire_hits_mean: firstChunkResponse?.defender_prefire_hits_mean ?? null,
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

  const prefire = computePrefireHitsMeans(outcomes, {
    attacker: firstChunkResponse?.attacker_prefire_hits_mean ?? null,
    defender: firstChunkResponse?.defender_prefire_hits_mean ?? null,
  });

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
    attacker_prefire_hits_mean: prefire.attacker,
    defender_prefire_hits_mean: prefire.defender,
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
