// node --test test/combat_sim_merge.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  computePrefireHitsMeans,
  mergeSimOutcomes,
} from '../frontend/src/utils/combatSimMerge.ts';

function trial(overrides = {}) {
  return {
    winner: 'defender',
    conquered: false,
    retreat: false,
    rounds: 3,
    attacker_casualties: { gondor_soldier: 1 },
    defender_casualties: {},
    attacker_survived: true,
    defender_survived: true,
    ...overrides,
  };
}

test('prefire means average only trials where that prefire ran (backend parity)', () => {
  const outcomes = [
    trial({ defender_prefire_applicable: true, defender_prefire_hits: 2 }),
    trial({ defender_prefire_applicable: true, defender_prefire_hits: 4 }),
    trial({ defender_prefire_applicable: false, defender_prefire_hits: 0 }),
    trial({ attacker_prefire_applicable: true, attacker_prefire_hits: 1 }),
    trial({ attacker_prefire_applicable: false, attacker_prefire_hits: 9 }),
  ];
  const means = computePrefireHitsMeans(outcomes, { attacker: null, defender: null });
  assert.equal(means.defender, 3); // (2+4)/2 — non-applicable trial ignored
  assert.equal(means.attacker, 1); // only the applicable trial
});

test('prefire means fall back to first-chunk values when no trial carries the flag', () => {
  const outcomes = [trial(), trial()];
  const means = computePrefireHitsMeans(outcomes, { attacker: 1.5, defender: 2.5 });
  assert.equal(means.attacker, 1.5);
  assert.equal(means.defender, 2.5);
});

test('merge recomputes prefire means from all chunks, not the first chunk alone', () => {
  // First chunk reported mean 0 (e.g. unlucky / few trials); later trials have real hits.
  const firstChunk = {
    n_trials: 500,
    attacker_prefire_hits_mean: 0,
    defender_prefire_hits_mean: 0,
    attacker_siegework_hits_mean: null,
    defender_siegework_hits_mean: null,
    battle_context: null,
    prefire_penalty: true,
  };
  const outcomes = [
    // chunk 1: no applicable (mean would be 0/null from first response alone)
    trial({ defender_prefire_applicable: false }),
    // chunk 2+: applicable with hits — must drive the merged mean
    trial({ defender_prefire_applicable: true, defender_prefire_hits: 3 }),
    trial({ defender_prefire_applicable: true, defender_prefire_hits: 5 }),
    trial({ attacker_prefire_applicable: true, attacker_prefire_hits: 2 }),
    trial({ attacker_prefire_applicable: true, attacker_prefire_hits: 4 }),
  ];
  const merged = mergeSimOutcomes(outcomes, firstChunk, null);
  assert.equal(merged.n_trials, 5);
  assert.equal(merged.defender_prefire_hits_mean, 4); // (3+5)/2 — not firstChunk's 0
  assert.equal(merged.attacker_prefire_hits_mean, 3); // (2+4)/2 — not firstChunk's 0
});

test('merge keeps first-chunk prefire mean when merged trials have no applicable flags', () => {
  const firstChunk = {
    n_trials: 500,
    attacker_prefire_hits_mean: null,
    defender_prefire_hits_mean: 1.25,
    attacker_siegework_hits_mean: null,
    defender_siegework_hits_mean: null,
    battle_context: null,
    prefire_penalty: true,
  };
  const merged = mergeSimOutcomes([trial(), trial()], firstChunk, null);
  assert.equal(merged.defender_prefire_hits_mean, 1.25);
  assert.equal(merged.attacker_prefire_hits_mean, null);
});

test('merge of zero outcomes returns the first chunk unchanged', () => {
  const firstChunk = {
    n_trials: 500,
    attacker_prefire_hits_mean: 2,
    defender_prefire_hits_mean: 3,
    attacker_siegework_hits_mean: null,
    defender_siegework_hits_mean: null,
    battle_context: null,
    prefire_penalty: false,
  };
  const merged = mergeSimOutcomes([], firstChunk, null);
  assert.equal(merged, firstChunk);
  assert.equal(merged.attacker_prefire_hits_mean, 2);
  assert.equal(merged.prefire_penalty, false);
});
