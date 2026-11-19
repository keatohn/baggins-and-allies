// node --test test/combat_header_indicators.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { combatHeaderIndicators } from '../frontend/src/utils/combatHeaderIndicators.ts';

test('pending siegeworks renders before pending archers (engine order)', () => {
  // Audit Part 2 L1: the engine runs the siegeworks round before defender archer
  // prefire; the header used to show "Archers" first.
  const labels = combatHeaderIndicators({
    roundNumber: 0,
    siegeworksPending: true,
    archerPrefirePending: true,
  });
  assert.deepEqual(labels, ['Siegeworks', 'Archers']);
});

test('resolved stealth prefire leads the indicator list', () => {
  const labels = combatHeaderIndicators({
    currentRound: { isStealthPrefire: true },
    roundNumber: 0,
  });
  assert.deepEqual(labels, ['Stealth']);
});

test('resolved siegeworks round still precedes pending archers', () => {
  const labels = combatHeaderIndicators({
    currentRound: { isSiegeworksRound: true },
    roundNumber: 0,
    archerPrefirePending: true,
  });
  assert.deepEqual(labels, ['Siegeworks', 'Archers']);
});

test('stealth, siegeworks and archers never combine into one list out of order', () => {
  const labels = combatHeaderIndicators({
    currentRound: { isStealthPrefire: true, isSiegeworksRound: true, isArcherPrefire: true },
    roundNumber: 0,
    siegeworksPending: true,
    archerPrefirePending: true,
  });
  assert.deepEqual(labels, ['Stealth', 'Siegeworks', 'Archers']);
});

test('standard round shows only Round N', () => {
  const labels = combatHeaderIndicators({
    currentRound: {},
    roundNumber: 3,
    archerPrefirePending: true,
  });
  assert.deepEqual(labels, ['Round 3']);
});

test('round 0 with nothing pending renders no indicators', () => {
  assert.deepEqual(combatHeaderIndicators({ roundNumber: 0 }), []);
});

test('resolved archer prefire renders Archers', () => {
  const labels = combatHeaderIndicators({
    currentRound: { isArcherPrefire: true },
    roundNumber: 0,
  });
  assert.deepEqual(labels, ['Archers']);
});
