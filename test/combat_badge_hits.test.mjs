// node --test test/combat_badge_hits.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeBadgeHits } from '../frontend/src/utils/combatBadgeHits.ts';

function unit(id, unitType, health, remainingHealth, extra = {}) {
  return { id, unitType, health, remainingHealth, defense: 1, ...extra };
}

test('round start badges show only prior damage from the round-start payload roster', () => {
  const roster = [
    unit('a_foot_1', 'foot', 3, 1), // 2 prior damage
    unit('a_foot_2', 'foot', 3, 3), // undamaged
    unit('a_dead_1', 'foot', 3, 0), // casualty ghost
  ];
  const result = computeBadgeHits({
    units: roster,
    liveUnits: [unit('a_foot_1', 'foot', 3, 0), unit('a_foot_2', 'foot', 3, 3)], // live already ahead
    cumulativeCasualties: ['a_dead_1'],
    wounded: [],
    postRound: false,
  });
  assert.deepEqual(result.byInstanceId, { a_foot_1: 2 });
  assert.deepEqual(result.byUnitType, { foot: 2 });
});

test('post-round badges are cumulative: payload prior damage plus this round live delta', () => {
  const roster = [unit('d_troll_1', 'cave_troll', 5, 3)]; // 2 damage before this round
  const result = computeBadgeHits({
    units: roster,
    liveUnits: [unit('d_troll_1', 'cave_troll', 5, 1)], // took 2 more this round
    cumulativeCasualties: [],
    wounded: ['d_troll_1'],
    postRound: true,
  });
  assert.deepEqual(result.byInstanceId, { d_troll_1: 4 });
  assert.deepEqual(result.byUnitType, { cave_troll: 4 });
});

test('several hits on one stack in one round count fully (not one per wounded id)', () => {
  const roster = [unit('d_guard_1', 'guard', 4, 4)];
  const result = computeBadgeHits({
    units: roster,
    liveUnits: [unit('d_guard_1', 'guard', 4, 2)], // wounded once, hit twice
    cumulativeCasualties: [],
    wounded: ['d_guard_1'],
    postRound: true,
  });
  assert.deepEqual(result.byInstanceId, { d_guard_1: 2 });
});

test('ring HP-shield wound (no health lost) still badges one hit', () => {
  const roster = [unit('a_ring_1', 'captain', 2, 2)];
  const result = computeBadgeHits({
    units: roster,
    liveUnits: [unit('a_ring_1', 'captain', 2, 2)],
    cumulativeCasualties: [],
    wounded: ['a_ring_1'],
    postRound: true,
  });
  assert.deepEqual(result.byInstanceId, { a_ring_1: 1 });
});

test('live state ahead of the displayed round is ignored for units the payload says were untouched', () => {
  const roster = [unit('d_foot_1', 'foot', 1, 1)];
  const result = computeBadgeHits({
    units: roster,
    liveUnits: [unit('d_foot_1', 'foot', 1, 0)], // damaged by a LATER round than the one shown
    cumulativeCasualties: ['d_foot_1'],
    wounded: [],
    postRound: true,
  });
  // casualty of a later round is still in the cumulative casualty list → never badges
  assert.deepEqual(result.byInstanceId, {});

  const survivor = computeBadgeHits({
    units: [unit('d_foot_2', 'foot', 3, 3)],
    liveUnits: [unit('d_foot_2', 'foot', 3, 2)],
    cumulativeCasualties: [],
    wounded: [], // not wounded in the displayed round
    postRound: true,
  });
  assert.deepEqual(survivor.byInstanceId, {});
});

test('casualties never badge, even when they carried prior damage', () => {
  const result = computeBadgeHits({
    units: [unit('a_dead_1', 'foot', 3, 1), unit('a_alive_1', 'foot', 3, 3)],
    liveUnits: [unit('a_alive_1', 'foot', 3, 2)],
    cumulativeCasualties: ['a_dead_1'],
    wounded: ['a_alive_1'],
    postRound: true,
  });
  assert.deepEqual(result.byInstanceId, { a_alive_1: 1 });
  assert.deepEqual(result.byUnitType, { foot: 1 });
});

test('legacy combat-log payload without a wounded list falls back to the live delta', () => {
  const result = computeBadgeHits({
    units: [unit('d_foot_1', 'foot', 2, 2), unit('d_foot_2', 'foot', 2, 2)],
    liveUnits: [unit('d_foot_1', 'foot', 2, 1), unit('d_foot_2', 'foot', 2, 2)],
    cumulativeCasualties: [],
    wounded: null,
    postRound: true,
  });
  assert.deepEqual(result.byInstanceId, { d_foot_1: 1 });
});

test('defender badges group by effective defense shelf while type totals stay flat', () => {
  const result = computeBadgeHits({
    units: [
      unit('d_a_1', 'archer', 1, 1, { defense: 2, effectiveDefense: 4 }),
      unit('d_b_1', 'guard', 2, 2, { defense: 3 }),
    ],
    liveUnits: [
      unit('d_a_1', 'archer', 1, 1, { defense: 2, effectiveDefense: 4 }),
      unit('d_b_1', 'guard', 2, 1, { defense: 3 }),
    ],
    cumulativeCasualties: [],
    wounded: ['d_a_1', 'd_b_1'],
    postRound: true,
  });
  assert.deepEqual(result.byUnitType, { archer: 1, guard: 1 });
  assert.deepEqual(result.byShelf, {
    4: { archer: 1 },
    3: { guard: 1 },
  });
});
