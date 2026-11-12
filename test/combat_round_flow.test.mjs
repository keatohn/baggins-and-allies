// node --test test/combat_round_flow.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  prospectiveCumulativeCasualties,
  phaseAfterFailedRoll,
} from '../frontend/src/utils/combatRoundFlow.ts';

test('prospective fold appends the displayed round casualties to the prior list', () => {
  assert.deepEqual(
    prospectiveCumulativeCasualties(['a1', 'd1'], ['a2', 'd2']),
    ['a1', 'd1', 'a2', 'd2'],
  );
});

test('prospective fold with no displayed round copies prior (no aliasing of state array)', () => {
  const prior = ['a1'];
  const out = prospectiveCumulativeCasualties(prior, undefined);
  assert.deepEqual(out, ['a1']);
  assert.notEqual(out, prior);
});

test('prospective fold treats empty casualty list as no-op copy', () => {
  assert.deepEqual(prospectiveCumulativeCasualties(['a1'], []), ['a1']);
});

test('failed round request with a displayed round returns to the decision phase', () => {
  // Regression: a failed Continue must keep Continue/Retreat available with the last
  // resolved round still on screen — folding/clearing up-front wiped it (audit L2).
  assert.equal(phaseAfterFailedRoll(true), 'awaiting_decision');
});

test('failed round request with no displayed round returns to ready (Start)', () => {
  assert.equal(phaseAfterFailedRoll(false), 'ready');
});
