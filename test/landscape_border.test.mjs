import { test } from 'node:test';
import assert from 'node:assert/strict';
import { landscapeCategoriesForTerrain } from '../frontend/src/pages/admin/landscape.ts';
import { sharedBorderSegments, sharedBorderAtPoint, nearestPointOnBorder, pointsAlongBorder } from '../frontend/src/pages/admin/sharedBorder.ts';

function outline(points) {
  const segments = points.slice(1).map((point, index) => ({ a: points[index], b: point, length: Math.hypot(point.x - points[index].x, point.y - points[index].y) }));
  const length = segments.reduce((sum, s) => sum + s.length, 0);
  return {
    getTotalLength: () => length,
    getPointAtLength: (distance) => {
      for (const s of segments) {
        if (distance <= s.length) return { x: s.a.x + (s.b.x - s.a.x) * distance / s.length, y: s.a.y + (s.b.y - s.a.y) * distance / s.length };
        distance -= s.length;
      }
      return points.at(-1);
    },
  };
}
const rectangle = (x, y, width = 100, height = 100) => outline([{ x, y }, { x: x + width, y }, { x: x + width, y: y + height }, { x, y: y + height }, { x, y }]);

test('only the shared side of two territories receives mountain positions', () => {
  const segments = sharedBorderSegments(rectangle(0, 0), rectangle(100, 0));
  const points = pointsAlongBorder(segments, 36);
  assert.equal(points.length, 3);
  assert.ok(points.every((p) => p.x === 100 && p.y >= 0 && p.y <= 100));
  assert.ok(segments.every(({ a, b }) => a.x === 100 && b.x === 100));
});

test('touching at a corner or being separated does not create a shared border', () => {
  assert.deepEqual(sharedBorderSegments(rectangle(0, 0), rectangle(100, 100)), []);
  assert.deepEqual(sharedBorderSegments(rectangle(0, 0), rectangle(110, 0)), []);
});

test('border mode identifies the clicked neighbor without a separate selector', () => {
  const outlines = [{ id: 'middle', path: rectangle(0, 0) }, { id: 'left', path: rectangle(-100, 0) }, { id: 'right', path: rectangle(100, 0) }];
  assert.equal(sharedBorderAtPoint('middle', outlines, { x: 99, y: 50 }).neighbor, 'right');
  assert.equal(sharedBorderAtPoint('middle', outlines, { x: 1, y: 50 }).neighbor, 'left');
  assert.equal(sharedBorderAtPoint('middle', outlines, { x: 50, y: 1 }), null, 'coastline does not snap to a distant neighbor');
});

test('brush points stay on the originally selected shared border near junctions', () => {
  const segments = sharedBorderSegments(rectangle(0, 0), rectangle(100, 0));
  for (const point of [{ x: 80, y: -20 }, { x: 140, y: 130 }, { x: 20, y: 40 }]) {
    const snap = nearestPointOnBorder(segments, point);
    assert.equal(snap.x, 100);
    assert.ok(snap.y >= 0 && snap.y <= 100);
  }
});

test('spacing resets at disconnected border runs without filling gaps', () => {
  const segments = [{ a: { x: 0, y: 0 }, b: { x: 0, y: 100 } }, { a: { x: 0, y: 300 }, b: { x: 0, y: 400 } }];
  const points = pointsAlongBorder(segments, 36);
  assert.equal(points.length, 6);
  assert.ok(points.every((p) => p.y < 100 || p.y > 300));
  assert.deepEqual(pointsAlongBorder(segments, 0), []);
  assert.deepEqual(pointsAlongBorder([{ a: { x: 0, y: 0 }, b: { x: 0, y: 10 } }], 36), [{ x: 0, y: 5 }]);
});

test('shared curved/polyline borders tolerate differing vertex placement', () => {
  const left = outline([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 120, y: 50 }, { x: 100, y: 100 }, { x: 0, y: 100 }, { x: 0, y: 0 }]);
  const right = outline([{ x: 100, y: 0 }, { x: 200, y: 0 }, { x: 200, y: 100 }, { x: 100, y: 100 }, { x: 110, y: 75 }, { x: 120, y: 50 }, { x: 110, y: 25 }, { x: 100, y: 0 }]);
  const points = pointsAlongBorder(sharedBorderSegments(left, right), 20);
  assert.ok(points.length >= 4);
  assert.ok(points.every((p) => Math.abs(p.x - (100 + Math.min(p.y, 100 - p.y) * 0.4)) < 1));
});


test('free placement exposes all available terrain art without requiring a matching territory', () => {
  const manifest = { mountains: ['mountain.png'], forest: ['tree.png'], hills: ['hill.png'], anywhere: ['bridge.png'], landmarks: ['city.png'], empty: [] };
  assert.deepEqual(landscapeCategoriesForTerrain('plains', manifest, { unrestricted: true }), ['anywhere', 'forest', 'hills', 'landmarks', 'mountains']);
  assert.deepEqual(landscapeCategoriesForTerrain(undefined, manifest, { unrestricted: true }), ['anywhere', 'forest', 'hills', 'landmarks', 'mountains']);
  assert.deepEqual(landscapeCategoriesForTerrain('forest', manifest), ['forest', 'anywhere', 'landmarks']);
  assert.deepEqual(landscapeCategoriesForTerrain('forest', manifest, { includeFree: false }), ['forest']);
  assert.deepEqual(landscapeCategoriesForTerrain('plains', manifest), ['anywhere', 'landmarks']);
});
