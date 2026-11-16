// node --test test/combat_log_snapshot.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { unionUnitsAtStartFromCombatLog } from '../frontend/src/utils/combatLogSnapshot.ts';

function unit(instance_id, extra = {}) {
  return { instance_id, unit_id: 'footman', remaining_health: 2, ...extra };
}

test('unions round-start rosters across entries in first-seen order, deduped per side', () => {
  const log = [
    {
      attacker_units_at_start: [unit('a1', { remaining_health: 2 }), unit('a2', { remaining_health: 2 })],
      defender_units_at_start: [unit('d1', { remaining_health: 3 })],
    },
    {
      // a1 died in round 1: it is absent here; a2 appears wounded and must not duplicate.
      attacker_units_at_start: [unit('a2', { remaining_health: 1 })],
      defender_units_at_start: [unit('d1', { remaining_health: 1 }), unit('d2', { remaining_health: 3 })],
    },
  ];
  const { attacker, defender } = unionUnitsAtStartFromCombatLog(log);
  assert.deepEqual(attacker.map(u => u.instance_id), ['a1', 'a2']);
  assert.deepEqual(defender.map(u => u.instance_id), ['d1', 'd2']);
  // First-seen wins: a2/d1 keep their battle-open snapshot, not the later wounded one.
  assert.equal(attacker[1].remaining_health, 2);
  assert.equal(defender[0].remaining_health, 3);
});

test('units that only appear in a later round (joined mid-battle) are included', () => {
  const log = [
    { attacker_units_at_start: [unit('a1')], defender_units_at_start: [unit('d1')] },
    { attacker_units_at_start: [unit('a1'), unit('siege_1')], defender_units_at_start: [unit('d1')] },
  ];
  const { attacker } = unionUnitsAtStartFromCombatLog(log);
  assert.deepEqual(attacker.map(u => u.instance_id), ['a1', 'siege_1']);
});

test('entries without payloads are skipped; a payload-less log yields empty sides', () => {
  const legacy = [
    { attacker_casualties: ['a1'], defender_casualties: [] },
    { attacker_units_at_start: null, defender_units_at_start: undefined },
  ];
  const { attacker, defender } = unionUnitsAtStartFromCombatLog(legacy);
  assert.deepEqual(attacker, []);
  assert.deepEqual(defender, []);

  const mixed = [
    { attacker_casualties: ['a1'] },
    { attacker_units_at_start: [unit('a1')], defender_units_at_start: [unit('d1')] },
  ];
  const mixedOut = unionUnitsAtStartFromCombatLog(mixed);
  assert.deepEqual(mixedOut.attacker.map(u => u.instance_id), ['a1']);
  assert.deepEqual(mixedOut.defender.map(u => u.instance_id), ['d1']);
});

test('missing, empty, and malformed logs are tolerated', () => {
  for (const log of [undefined, null, [], [null, 42, 'x'], [{ attacker_units_at_start: 'nope' }]]) {
    const { attacker, defender } = unionUnitsAtStartFromCombatLog(log);
    assert.deepEqual(attacker, []);
    assert.deepEqual(defender, []);
  }
  const badUnit = unionUnitsAtStartFromCombatLog([
    { attacker_units_at_start: [{}, { instance_id: '' }, { instance_id: 7 }, unit('a1')] },
  ]);
  assert.deepEqual(badUnit.attacker.map(u => u.instance_id), ['a1']);
});
