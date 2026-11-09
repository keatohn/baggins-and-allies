import { test } from 'node:test';
import assert from 'node:assert/strict';
import { jointsInBox, moveJoints, deleteJoints, labelAtPoint, splitBorderAtPoint } from '../frontend/src/pages/admin/mapBuilderSelection.ts';

const drawing = () => ({
  width: 100, height: 100,
  joints: { a: { x: 10, y: 20 }, b: { x: 40, y: 50 }, c: { x: 80, y: 90 }, d: { x: 90, y: 90 } },
  borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' } },
});

test('label dots can be targeted individually after regions merge or their borders disappear', () => {
  const labels = { first: { x: 20, y: 20 }, second: { x: 40, y: 40 } };
  assert.equal(labelAtPoint(labels, { x: 41, y: 40 }, 7), 'second');
  assert.equal(labelAtPoint(labels, { x: 21, y: 20 }, 7), 'first');
  assert.equal(labelAtPoint(labels, { x: 80, y: 80 }, 7), null);
  assert.equal(labelAtPoint({}, { x: 20, y: 20 }, 7), null);
});

test('label hit tolerance stays constant in screen pixels when zoom changes', () => {
  const labels = { region: { x: 40, y: 40 } };
  for (const scale of [0.25, 1, 4]) {
    assert.equal(labelAtPoint(labels, { x: 40 + 6 / scale, y: 40 }, 7 / scale), 'region');
    assert.equal(labelAtPoint(labels, { x: 40 + 8 / scale, y: 40 }, 7 / scale), null);
  }
});

test('node deletion leaves named region dots in place', () => {
  const doc = { ...drawing(), labels: { region: { x: 50, y: 50, display_name: 'Region' } } };
  deleteJoints(doc, ['b', 'c']);
  assert.deepEqual(doc.labels, { region: { x: 50, y: 50, display_name: 'Region' } });
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

test('moving a selected region carries its labels by the same clamped offset', () => {
  const original = { ...drawing(), labels: { region: { x: 25, y: 35, display_name: 'Region' }, neighbor: { x: 90, y: 90 } } };
  const doc = structuredClone(original);
  moveJoints(doc, original, ['a', 'b'], 10, 0, ['region']);
  assert.deepEqual(doc.labels.region, { x: 35, y: 35, display_name: 'Region' });
  assert.deepEqual(doc.labels.neighbor, original.labels.neighbor);
  moveJoints(doc, original, ['a', 'b'], -100, -100, ['region']);
  assert.deepEqual(doc.labels.region, { x: 15, y: 15, display_name: 'Region' });
  assert.deepEqual(doc.joints.a, { x: 0, y: 0 });
});

test('bulk deletion removes incident borders and preserves unrelated geometry', () => {
  const doc = drawing();
  deleteJoints(doc, ['a', 'b']);
  assert.deepEqual(Object.keys(doc.joints), ['c', 'd']);
  assert.deepEqual(doc.borders, { cd: { a: 'c', b: 'd' } });
});

const edges = (doc) => Object.values(doc.borders).map(({ a, b }) => [a, b].sort().join('-')).sort();

test('splitting an edge projects the click onto it and connects the new node to both endpoints', () => {
  const doc = drawing();
  const result = splitBorderAtPoint(doc, 'ab', { x: 26, y: 34 });
  assert.equal(result.created, true);
  assert.deepEqual(doc.joints[result.jointId], { x: 25, y: 35 });
  assert.deepEqual(doc.borders.ab, { a: 'a', b: result.jointId });
  assert.ok(Object.values(doc.borders).some((edge) => edge.a === result.jointId && edge.b === 'b'));
  assert.deepEqual(doc.borders.bc, { a: 'b', b: 'c' });
  deleteJoints(doc, [result.jointId]);
  assert.deepEqual(edges(doc), ['a-b', 'b-c', 'c-d']);
});

test('splitting at or near an endpoint reuses it without duplicate nodes or edges', () => {
  const doc = drawing();
  const original = structuredClone(doc);
  assert.deepEqual(splitBorderAtPoint(doc, 'ab', { x: 10, y: 20 }), { jointId: 'a', created: false });
  assert.deepEqual(splitBorderAtPoint(doc, 'ab', { x: 39, y: 49 }, 7), { jointId: 'b', created: false });
  assert.deepEqual(doc, original);
});

test('edge splitting does not overwrite existing ids or modify missing edges', () => {
  const doc = drawing();
  doc.joints.j1 = { x: 0, y: 0 };
  doc.borders.b1 = { a: 'c', b: 'd' };
  assert.equal(splitBorderAtPoint(doc, 'missing', { x: 25, y: 35 }), null);
  assert.deepEqual(splitBorderAtPoint(doc, 'ab', { x: 25, y: 35 }), { jointId: 'j2', created: true });
  assert.deepEqual(doc.joints.j1, { x: 0, y: 0 });
  assert.deepEqual(doc.borders.b1, { a: 'c', b: 'd' });
});

test('deleting an intermediate node joins its neighbors and preserves other borders', () => {
  const doc = drawing();
  deleteJoints(doc, ['b']);
  assert.deepEqual(Object.keys(doc.joints), ['a', 'c', 'd']);
  assert.deepEqual(edges(doc), ['a-c', 'c-d']);
  assert.deepEqual(doc.borders.cd, { a: 'c', b: 'd' });
});

test('bulk deletion reconnects a selected chain independently of selection order', () => {
  const forward = drawing(), reverse = drawing();
  deleteJoints(forward, ['b', 'c']);
  deleteJoints(reverse, ['c', 'b']);
  assert.deepEqual(edges(forward), ['a-d']);
  assert.deepEqual(forward, reverse);
});

test('existing edges are not duplicated when a node is removed from a triangle', () => {
  const doc = drawing();
  doc.borders.ca = { a: 'c', b: 'a' };
  deleteJoints(doc, ['b']);
  assert.deepEqual(doc.borders, { ca: { a: 'c', b: 'a' }, cd: { a: 'c', b: 'd' } });
});

test('deleting a junction does not invent connections between its branches', () => {
  const doc = drawing();
  doc.borders.bd = { a: 'b', b: 'd' };
  deleteJoints(doc, ['b']);
  assert.deepEqual(doc.borders, { cd: { a: 'c', b: 'd' } });
});

test('deleting a complete closed loop leaves no dangling borders or self edges', () => {
  const doc = drawing();
  doc.borders.da = { a: 'd', b: 'a' };
  deleteJoints(doc, ['a', 'b', 'c', 'd']);
  assert.deepEqual(doc.joints, {});
  assert.deepEqual(doc.borders, {});
});

test('separate selected segments retain their individual surviving neighbors', () => {
  const doc = drawing();
  doc.joints.e = { x: 95, y: 95 };
  doc.borders.de = { a: 'd', b: 'e' };
  deleteJoints(doc, ['b', 'd', 'missing']);
  assert.deepEqual(edges(doc), ['a-c', 'c-e']);
});
