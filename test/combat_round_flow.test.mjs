// node --test test/combat_round_flow.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  prospectiveCumulativeCasualties,
  phaseAfterFailedRoll,
  fullyEliminatedStacks,
  attackerStackStat,
  defenderStackStat,
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

function stackUnit(id, extra = {}) {
  return {
    id,
    unitType: 'footman',
    name: 'Footman',
    icon: '/assets/units/footman.png',
    health: 2,
    attack: 3,
    defense: 4,
    factionId: 'gondor',
    ...extra,
  };
}

test('fully dead group becomes one eliminated stack keyed like the shelf group key', () => {
  // Regression: spectators never run animateDiceReveals, so these stacks are rebuilt from
  // the battle-open roster (audit M3 — cumulative eliminated ghosts were interactive-only).
  const out = fullyEliminatedStacks(
    [stackUnit('a1'), stackUnit('a2'), stackUnit('a3')],
    ['a1', 'a2', 'a3'],
    attackerStackStat,
  );
  assert.equal(out.length, 1);
  const stack = out[0];
  assert.equal(stack.count, 3);
  assert.equal(stack.unitKey, 'footman::gondor::2');
  assert.equal(stack.unitType, 'footman');
  assert.equal(stack.name, 'Footman');
  assert.equal(stack.icon, '/assets/units/footman.png');
  assert.equal(stack.health, 2);
  assert.equal(stack.statValue, 3);
  assert.equal(stack.factionId, 'gondor');
});

test('partially dead group yields no stack (survivors stay on the shelf)', () => {
  const out = fullyEliminatedStacks(
    [stackUnit('a1'), stackUnit('a2')],
    ['a1'],
    attackerStackStat,
  );
  assert.deepEqual(out, []);
});

test('no casualties or empty roster yields no stacks', () => {
  assert.deepEqual(fullyEliminatedStacks([stackUnit('a1')], [], attackerStackStat), []);
  assert.deepEqual(fullyEliminatedStacks([], ['a1'], attackerStackStat), []);
});

test('groups split by faction and by effective stat, not merged across them', () => {
  const out = fullyEliminatedStacks(
    [
      stackUnit('a1'),
      stackUnit('a2', { factionId: 'rohan' }),
      stackUnit('a3', { effectiveAttack: 5 }),
    ],
    ['a1', 'a2', 'a3'],
    attackerStackStat,
  );
  assert.equal(out.length, 3);
  assert.deepEqual(out.map(s => s.statValue).sort(), [3, 3, 5]);
  assert.deepEqual(out.map(s => s.factionId).sort(), ['gondor', 'gondor', 'rohan']);
});

test('stat getters prefer the effective stat and fall back to the base stat', () => {
  assert.equal(attackerStackStat(stackUnit('a1', { effectiveAttack: 7 })), 7);
  assert.equal(attackerStackStat(stackUnit('a1')), 3);
  assert.equal(defenderStackStat(stackUnit('d1', { effectiveDefense: 6 })), 6);
  assert.equal(defenderStackStat(stackUnit('d1')), 4);
  // Defender effective stat drives the defender stack placement.
  const out = fullyEliminatedStacks(
    [stackUnit('d1', { effectiveDefense: 6 })],
    ['d1'],
    defenderStackStat,
  );
  assert.equal(out[0].statValue, 6);
});

test('specials badges are carried onto the ghost stack', () => {
  const out = fullyEliminatedStacks(
    [stackUnit('a1', { hasTerror: true, hasSeaRaider: true })],
    ['a1'],
    attackerStackStat,
  );
  assert.equal(out[0].hasTerror, true);
  assert.equal(out[0].hasSeaRaider, true);
  assert.equal(out[0].hasRam, undefined);
});

test('casualty ids that match no roster unit are ignored', () => {
  const out = fullyEliminatedStacks([stackUnit('a1')], ['ghost'], attackerStackStat);
  assert.deepEqual(out, []);
});
