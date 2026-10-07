import type { CatalogSpecial, FormulaWeights, UnitFormulas } from '../../services/api';

/** Mirrors backend/unit_formulas.py. Keep the feature keys and values in step. */
export const BASE_FEATURES: { key: string; short: string; label: string }[] = [
  { key: 'attack', short: 'A', label: 'Attack' },
  { key: 'defense', short: 'D', label: 'Defense' },
  { key: 'movement', short: 'M', label: 'Moves' },
  { key: 'dice', short: 'R', label: 'Dice rolls' },
  { key: 'health', short: 'HP', label: 'Hit points' },
  { key: 'transport_capacity', short: 'TC', label: 'Transport capacity' },
  { key: 'is_naval', short: 'Naval', label: 'Is naval (0/1)' },
  { key: 'is_hero', short: 'Hero', label: 'Is hero (0/1)' },
];

export const SPECIAL_PREFIX = 'special:';

export const EMPTY_FORMULAS: UnitFormulas = {
  features: ['attack', 'defense', 'movement', 'dice', 'health', 'transport_capacity'],
  training: { setup_ids: [], include_heroes: true, ridge: 1 },
  regression: {
    label: 'Fit',
    show: false,
    intercept: 0,
    weights: {},
    stats: { n: 0, r2: null, rmse: null, mae: null, trained_at: null },
  },
  custom: { label: 'Custom', show: false, intercept: 0, weights: {} },
  diff_label: 'Δ',
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function num(value: unknown, fallback = 0): number {
  if (typeof value === 'boolean' || value == null) return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** Raw JSON edits may drop keys; fill every field the panel reads. */
export function asFormulas(raw: unknown): UnitFormulas {
  const o = asRecord(raw);
  const training = asRecord(o.training);
  const reg = asRecord(o.regression);
  const stats = asRecord(reg.stats);
  const custom = asRecord(o.custom);
  const weights = (v: unknown): FormulaWeights =>
    Object.fromEntries(Object.entries(asRecord(v)).map(([k, w]) => [k, num(w)]).filter(([, w]) => w !== 0));
  const optional = (v: unknown) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));
  return {
    features: Array.isArray(o.features) ? o.features.filter((k): k is string => typeof k === 'string') : EMPTY_FORMULAS.features,
    training: {
      setup_ids: Array.isArray(training.setup_ids)
        ? training.setup_ids.filter((s): s is string => typeof s === 'string')
        : [],
      include_heroes: training.include_heroes !== false,
      ridge: Math.max(0, num(training.ridge, 1)),
    },
    regression: {
      label: typeof reg.label === 'string' ? reg.label : 'Fit',
      show: reg.show === true,
      intercept: num(reg.intercept),
      weights: weights(reg.weights),
      stats: {
        n: num(stats.n),
        r2: optional(stats.r2),
        rmse: optional(stats.rmse),
        mae: optional(stats.mae),
        trained_at: typeof stats.trained_at === 'string' ? stats.trained_at : null,
      },
    },
    custom: {
      label: typeof custom.label === 'string' ? custom.label : 'Custom',
      show: custom.show === true,
      intercept: num(custom.intercept),
      weights: weights(custom.weights),
    },
    diff_label: typeof o.diff_label === 'string' ? o.diff_label : 'Δ',
  };
}

export function unitPowerCost(unit: Record<string, unknown>): number {
  const cost = unit.cost;
  if (cost && typeof cost === 'object') return num(asRecord(cost).power);
  return num(cost);
}

export function unitFeatures(unit: Record<string, unknown>): Record<string, number> {
  const tags = Array.isArray(unit.tags) ? unit.tags : [];
  const out: Record<string, number> = {
    attack: num(unit.attack),
    defense: num(unit.defense),
    movement: num(unit.movement),
    dice: num(unit.dice, 1),
    health: num(unit.health),
    transport_capacity: num(unit.transport_capacity),
    is_naval: unit.archetype === 'naval' || tags.includes('naval') ? 1 : 0,
    is_hero: typeof unit.hero_id === 'string' && unit.hero_id.trim() ? 1 : 0,
  };
  for (const sid of Array.isArray(unit.specials) ? unit.specials : []) {
    if (typeof sid === 'string' && sid) out[SPECIAL_PREFIX + sid] = 1;
  }
  return out;
}

export function predictCost(formula: { intercept: number; weights: FormulaWeights }, features: Record<string, number>): number {
  let total = formula.intercept;
  for (const [key, w] of Object.entries(formula.weights)) total += w * (features[key] ?? 0);
  return total;
}

export function featureShort(key: string, specials: CatalogSpecial[]): string {
  const base = BASE_FEATURES.find((f) => f.key === key);
  if (base) return base.short;
  if (key.startsWith(SPECIAL_PREFIX)) {
    const sid = key.slice(SPECIAL_PREFIX.length);
    const s = specials.find((sp) => sp.id === sid);
    return s?.display_code || s?.name || sid;
  }
  return key;
}

export function formatWeight(n: number): string {
  return String(Math.round(n * 100) / 100);
}

/** "2 + 0.5·A + 1.25·HP − 0.3·Naval" */
export function formulaText(
  formula: { intercept: number; weights: FormulaWeights },
  specials: CatalogSpecial[],
): string {
  const terms = Object.entries(formula.weights)
    .filter(([, w]) => Math.round(w * 100) !== 0)
    .map(([k, w]) => ({ w, name: featureShort(k, specials) }));
  let text = formatWeight(formula.intercept);
  for (const { w, name } of terms) {
    text += ` ${w < 0 ? '−' : '+'} ${formatWeight(Math.abs(w))}·${name}`;
  }
  return text;
}
