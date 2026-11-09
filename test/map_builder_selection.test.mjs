import { test } from 'node:test';
import assert from 'node:assert/strict';
import { jointsInBox, moveJoints, deleteJoints } from '../frontend/src/pages/admin/mapBuilderSelection.ts';

const drawing = () => ({
  width: 100, height: 100,
  joints: { a: { x: 10, y: 20 }, b: { x: 40, y: 50 }, c: { x: 80, y: 90 }, d: { x: 90, y: 90 } },
  borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' } },
});

test('box selection works in either direction and includes edge nodes', () => {
  const doc = drawing();
  assert.deepEqual(jointsInBox(doc.joints, { x: 10, y: 20 }, { x: 40, y: 50 }), ['a', 'b']);
  assert.deepEqual(jointsInBox(doc.joints, { x: 40, y: 50 }, { x: 10, y: 20 }), ['a', 'b']);
  assert.deepEqual(jointsInBox(doc.joints, { x: 0, y: 0 }, { x: 5, y: 5 }), []);
});

test('additive selection preserves existing nodes without duplicates or stale IDs', () => {
  assert.deepEqual(jointsInBox(drawing().joints, { x: 0, y: 0 }, { x: 40, y: 50 }, ['b', 'c', 'missing']), ['b', 'c', 'a']);
});

test('group movement uses the original snapshot without accumulating drag deltas', () => {
  const original = drawing();
  const doc = structuredClone(original);
  moveJoints(doc, original, ['a', 'b'], 10, 15);
  moveJoints(doc, original, ['a', 'b'], 15, 20);
  assert.deepEqual(doc.joints.a, { x: 25, y: 40 });
  assert.deepEqual(doc.joints.b, { x: 55, y: 70 });
  assert.deepEqual(doc.joints.c, original.joints.c);
  assert.deepEqual(original, drawing());
});

test('group movement clamps all edges while retaining relative spacing', () => {
  const original = drawing();
  const doc = structuredClone(original);
  moveJoints(doc, original, ['a', 'b'], 500, 500);
  assert.deepEqual(doc.joints.a, { x: 70, y: 70 });
  assert.deepEqual(doc.joints.b, { x: 100, y: 100 });
  moveJoints(doc, original, ['a', 'b'], -500, -500);
  assert.deepEqual(doc.joints.a, { x: 0, y: 0 });
  assert.deepEqual(doc.joints.b, { x: 30, y: 30 });
});

test('bulk deletion removes incident borders and preserves unrelated geometry', () => {
  const doc = drawing();
  deleteJoints(doc, ['a', 'b']);
  assert.deepEqual(Object.keys(doc.joints), ['c', 'd']);
  assert.deepEqual(doc.borders, { cd: { a: 'c', b: 'd' } });
});
