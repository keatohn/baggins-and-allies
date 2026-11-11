import { test } from 'node:test';
import assert from 'node:assert/strict';
import { jointsInBox, moveJoints, deleteJoints, labelAtPoint, splitBorderAtPoint, copyGraphFragment, pasteGraphFragment, selectionBounds, resizeJoints } from '../frontend/src/pages/admin/mapBuilderSelection.ts';

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

test('copy includes selected nodes and their internal edges without external neighbors', () => {
  const original = drawing();
  const fragment = copyGraphFragment(original, ['a', 'b', 'b', 'missing']);
  assert.deepEqual(Object.keys(fragment.joints), ['a', 'b']);
  assert.deepEqual(fragment.borders, { ab: { a: 'a', b: 'b' } });
  original.joints.a.x = 99;
  assert.equal(fragment.joints.a.x, 10);
  assert.equal(copyGraphFragment(original, ['missing']), null);
});

test('paste remaps all edges to fresh nodes and creates independent copies on repeated pastes', () => {
  const doc = drawing();
  const original = structuredClone(doc);
  const fragment = copyGraphFragment(doc, ['a', 'b']);
  const first = pasteGraphFragment(doc, fragment, 10, 15);
  const second = pasteGraphFragment(doc, fragment, 20, 25);
  assert.deepEqual(first.ids.map(id => doc.joints[id]), [{ x: 20, y: 35 }, { x: 50, y: 65 }]);
  assert.deepEqual(second.ids.map(id => doc.joints[id]), [{ x: 30, y: 45 }, { x: 60, y: 75 }]);
  assert.ok(first.ids.every(id => !second.ids.includes(id) && !Object.hasOwn(original.joints, id)));
  const newEdges = Object.entries(doc.borders).filter(([id]) => !Object.hasOwn(original.borders, id)).map(([, edge]) => edge);
  assert.deepEqual(newEdges, [{ a: first.ids[0], b: first.ids[1] }, { a: second.ids[0], b: second.ids[1] }]);
  assert.deepEqual(doc.borders.ab, original.borders.ab);
  assert.deepEqual(doc.joints.a, original.joints.a);
  assert.deepEqual(fragment, copyGraphFragment(original, ['a', 'b']));
});

test('paste clamps the whole group into a different map and never distorts its spacing', () => {
  const fragment = copyGraphFragment(drawing(), ['a', 'b']);
  const target = { width: 50, height: 40, joints: {}, borders: {} };
  const pasted = pasteGraphFragment(target, fragment, 100, -100);
  assert.deepEqual(pasted.ids.map(id => target.joints[id]), [{ x: 20, y: 0 }, { x: 50, y: 30 }]);
  const small = { width: 10, height: 10, joints: {}, borders: {} };
  assert.equal(pasteGraphFragment(small, fragment, 0, 0), null);
  assert.deepEqual(small.joints, {});
  assert.deepEqual(small.borders, {});
});

test('region copies retain seed references across maps and leave same-map duplicates unassigned', () => {
  const doc = { ...drawing(), labels: { shire: { x: 20, y: 30 } } };
  const fragment = copyGraphFragment(doc, ['a', 'b'], ['shire']);
  const sameMap = pasteGraphFragment(doc, fragment, 10, 10);
  assert.deepEqual(sameMap.labelIds, []);
  assert.deepEqual(doc.labels, { shire: { x: 20, y: 30 } });
  const other = { width: 100, height: 100, joints: {}, borders: {}, labels: {} };
  const pasted = pasteGraphFragment(other, fragment, 10, 10);
  assert.deepEqual(pasted.labelIds, ['shire']);
  assert.deepEqual(other.labels.shire, { x: 30, y: 40 });
  assert.deepEqual(copyGraphFragment(doc, ['a', 'b']).labels, {});
});

const rectangle = () => ({
  width: 500, height: 400,
  joints: { a: { x: 100, y: 100 }, b: { x: 200, y: 100 }, c: { x: 200, y: 150 }, d: { x: 100, y: 150 }, outside: { x: 400, y: 300 } },
  borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' }, da: { a: 'd', b: 'a' }, neighbor: { a: 'b', b: 'outside' } },
  labels: { region: { x: 150, y: 125, display_name: 'Region' }, outside: { x: 400, y: 300 } },
});
const rectIds = ['a', 'b', 'c', 'd'];

test('side grips scale only the dragged axis and anchor the opposite side', () => {
  for (const [handle, dx, dy, expected] of [
    ['e', 100, 99, { minX: 100, maxX: 300, minY: 100, maxY: 150 }],
    ['w', -50, 99, { minX: 50, maxX: 200, minY: 100, maxY: 150 }],
    ['n', 99, -50, { minX: 100, maxX: 200, minY: 50, maxY: 150 }],
    ['s', 99, 50, { minX: 100, maxX: 200, minY: 100, maxY: 200 }],
  ]) {
    const original = rectangle(), doc = structuredClone(original);
    assert.equal(resizeJoints(doc, original, rectIds, handle, dx, dy, ['region']), true);
    assert.deepEqual(selectionBounds(doc, rectIds), expected, handle);
    assert.deepEqual(doc.joints.outside, original.joints.outside);
    assert.deepEqual(doc.labels.outside, original.labels.outside);
    assert.deepEqual(doc.borders, original.borders);
  }
});

test('every corner grip preserves aspect ratio and moves the selected region label with its geometry', () => {
  for (const [handle, dx, dy, expected] of [
    ['se', 100, 50, { minX: 100, minY: 100, maxX: 300, maxY: 200 }],
    ['nw', -100, -50, { minX: 0, minY: 50, maxX: 200, maxY: 150 }],
    ['ne', 100, -50, { minX: 100, minY: 50, maxX: 300, maxY: 150 }],
    ['sw', -100, 50, { minX: 0, minY: 100, maxX: 200, maxY: 200 }],
  ]) {
    const original = rectangle(), doc = structuredClone(original);
    resizeJoints(doc, original, rectIds, handle, dx, dy, ['region']);
    const box = selectionBounds(doc, rectIds);
    assert.deepEqual(box, expected, handle);
    assert.equal((box.maxX - box.minX) / (box.maxY - box.minY), 2);
    assert.deepEqual(doc.labels.region, { x: (box.minX + box.maxX) / 2, y: (box.minY + box.maxY) / 2, display_name: 'Region' });
  }
});

test('resize uses one gesture snapshot and clamps scale at map bounds rather than individual nodes', () => {
  const original = rectangle(), doc = structuredClone(original);
  resizeJoints(doc, original, rectIds, 'se', 100, 50);
  resizeJoints(doc, original, rectIds, 'se', 1000, 1000);
  assert.deepEqual(selectionBounds(doc, rectIds), { minX: 100, minY: 100, maxX: 500, maxY: 300 });
  resizeJoints(doc, original, rectIds, 'se', 50, 25);
  assert.deepEqual(selectionBounds(doc, rectIds), { minX: 100, minY: 100, maxX: 250, maxY: 175 });
  resizeJoints(doc, original, rectIds, 'se', -999, -999);
  const box = selectionBounds(doc, rectIds);
  assert.ok(box.maxX > box.minX && box.maxY > box.minY, 'crossing the anchor cannot collapse or invert the shape');
  assert.equal(resizeJoints(doc, original, rectIds, 'se', 0, 0), false);
  assert.deepEqual(doc.joints, original.joints);
});

test('zero-width/height selections resize without NaNs and invalid drags leave geometry untouched', () => {
  const original = rectangle();
  original.joints.b = { ...original.joints.a, x: 200 };
  const doc = structuredClone(original);
  resizeJoints(doc, original, ['a', 'b'], 'se', 100, 200);
  assert.deepEqual(doc.joints.b, { x: 300, y: 100 });
  resizeJoints(doc, original, ['a', 'd'], 'se', 200, 50);
  assert.deepEqual(doc.joints.d, { x: 100, y: 200 });
  assert.equal(resizeJoints(doc, original, ['a'], 'se', 30, 30), false);
  const before = structuredClone(doc);
  assert.equal(resizeJoints(doc, original, rectIds, 'e', Infinity, 1), false);
  assert.deepEqual(doc, before);
});
