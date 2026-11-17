// node --test test/combat_battle_roster.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  applyRoundStartUnitsAtStart,
  buildBattleOpenRoster,
} from '../frontend/src/utils/combatBattleRoster.ts';
import { unionUnitsAtStartFromCombatLog } from '../frontend/src/utils/combatLogSnapshot.ts';

function payloadUnit(instance_id, extra = {}) {
  return {
    instance_id,
    unit_id: 'footman',
    display_name: 'Footman',
    attack: 2,
    defense: 2,
    health: 2,
    remaining_health: 2,
    ...extra,
  };
}

function liveUnit(id, extra = {}) {
  return {
    id,
    unitType: 'footman',
    name: 'Footman',
    icon: '/assets/units/footman.png',
    attack: 2,
    defense: 2,
    health: 2,
    remainingHealth: 2,
    ...extra,
  };
}

test('roster revives units that died before the reload from the log union', () => {
  const log = [
    {
      attacker_units_at_start: [payloadUnit('a1'), payloadUnit('a2')],
      defender_units_at_start: [payloadUnit('d1')],
    },
    {
      // a1/d1 died in round 1: absent from the later snapshot and from live props after reload.
      attacker_units_at_start: [payloadUnit('a2', { remaining_health: 1 })],
      defender_units_at_start: [payloadUnit('d1', { remaining_health: 1 })],
    },
  ];
  const union = unionUnitsAtStartFromCombatLog(log);
  const live = [liveUnit('a2', { remainingHealth: 1 })];
  const roster = buildBattleOpenRoster(union.attacker, live);
  assert.deepEqual(roster.map(u => u.id), ['a1', 'a2']);
  assert.equal(roster[0].remainingHealth, 2); // battle-open snapshot values, not dead-in-live
  assert.equal(roster[0].icon, '/assets/units/footman.png');
  assert.equal(roster[1].remainingHealth, 1); // live state wins for survivors
});

test('effective stats and badge flags come from the payload even when the unit is alive in live props', () => {
  const payload = [
    payloadUnit('a1', {
      effective_attack: 4,
      effective_defense: 1,
      is_archer: true,
      terror: true,
      captain_bonus: true,
      passenger_count: 0,
    }),
  ];
  const live = [liveUnit('a1', { icon: '/custom/icon.png', factionColor: '#123456' })];
  const roster = buildBattleOpenRoster(payload, live);
  assert.equal(roster.length, 1);
  assert.equal(roster[0].icon, '/custom/icon.png'); // display fields from live
  assert.equal(roster[0].factionColor, '#123456');
  assert.equal(roster[0].effectiveAttack, 4); // dice-bucket stats from the snapshot
  assert.equal(roster[0].effectiveDefense, 1);
  assert.equal(roster[0].isArcher, true);
  assert.equal(roster[0].hasTerror, true);
  assert.equal(roster[0].hasCaptainBonus, true);
  assert.equal('passengerCount' in roster[0], false);
});

test('live units the log never listed are appended as extras', () => {
  const union = unionUnitsAtStartFromCombatLog([{ attacker_units_at_start: [payloadUnit('a1')] }]);
  const live = [liveUnit('a1'), liveUnit('a2', { unitType: 'scout', name: 'Scout' })];
  const roster = buildBattleOpenRoster(union.attacker, live);
  assert.deepEqual(roster.map(u => u.id), ['a1', 'a2']);
  assert.equal(roster[1].name, 'Scout');
});

test('payload-less side yields null so callers keep their live-derived fallback', () => {
  assert.equal(buildBattleOpenRoster([], [liveUnit('a1')]), null);
});

test('applyRoundStartUnitsAtStart falls back to the battle-start snapshot for units missing from live props', () => {
  const payload = [payloadUnit('a1', { effective_attack: 5 })];
  const snapshot = [liveUnit('a1', { effectiveAttack: 2, factionColor: '#abc' })];
  const out = applyRoundStartUnitsAtStart(payload, [], snapshot);
  assert.equal(out.length, 1);
  assert.equal(out[0].factionColor, '#abc'); // display fields from the snapshot
  assert.equal(out[0].effectiveAttack, 5); // payload stats still win
  assert.equal(applyRoundStartUnitsAtStart(undefined, [], null), null);
  assert.equal(applyRoundStartUnitsAtStart([], [], null), null);
});
