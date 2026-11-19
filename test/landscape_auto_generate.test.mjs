import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  autoGenerateForTerritory,
  LANDSCAPE_AUTO_MIN_DIST,
  landscapeImageCategory,
} from '../frontend/src/pages/admin/landscape.ts';

/** Mock SVGPathElement: an axis-aligned rectangle fill with a matching bbox. */
function rectPath(x, y, width, height) {
  return {
    ownerSVGElement: { createSVGPoint: () => ({ x: 0, y: 0 }) },
    getBBox: () => ({ x, y, width, height }),
    isPointInFill: (pt) => pt.x >= x && pt.x <= x + width && pt.y >= y && pt.y <= y + height,
  };
}

test('auto-generate packs a territory up to the cap at paint spacing', () => {
  const placements = autoGenerateForTerritory(rectPath(0, 0, 600, 600), 'forest', ['a.png', 'b.png'], 42);
  assert.equal(placements.length, 50);
  for (let i = 0; i < placements.length; i++) {
    for (let j = i + 1; j < placements.length; j++) {
      const d = Math.hypot(placements[i].x - placements[j].x, placements[i].y - placements[j].y);
      assert.ok(d >= LANDSCAPE_AUTO_MIN_DIST - 0.05, `pieces ${i} and ${j} are ${d} apart`);
    }
  }
});

test('auto-generate uses one image for every piece in the territory', () => {
  const placements = autoGenerateForTerritory(
    rectPath(0, 0, 400, 400),
    'mountains',
    ['m1.png', 'm2.png', 'm3.png'],
    7,
  );
  assert.ok(placements.length > 1);
  assert.equal(new Set(placements.map((p) => p.img)).size, 1);
});

test('auto-generate keeps every piece clear of obstacles', () => {
  const obstacles = [
    { x: 300, y: 300, r: 80 },
    { x: 60, y: 60, r: 40 },
  ];
  const placements = autoGenerateForTerritory(rectPath(0, 0, 600, 600), 'hills', ['h.png'], 99, obstacles);
  assert.ok(placements.length > 0);
  for (const p of placements) {
    const s = p.s ?? 1;
    for (const o of obstacles) {
      const d = Math.hypot(p.x - o.x, p.y - o.y);
      assert.ok(
        d >= o.r + 20 * s - 0.05,
        `piece at ${p.x},${p.y} is ${d} from obstacle (need ${o.r + 20 * s})`,
      );
    }
  }
});

test('auto-generate yields the whole territory to a blocking obstacle', () => {
  const obstacles = [{ x: 300, y: 300, r: 5000 }];
  assert.deepEqual(
    autoGenerateForTerritory(rectPath(0, 0, 600, 600), 'forest', ['f.png'], 5, obstacles),
    [],
  );
});

test('auto-generate without images or a usable path places nothing', () => {
  assert.deepEqual(autoGenerateForTerritory(rectPath(0, 0, 200, 200), 'forest', [], 1), []);
  assert.deepEqual(autoGenerateForTerritory(rectPath(0, 0, 0, 0), 'forest', ['f.png'], 1), []);
});

test('landscape images resolve to their manifest category', () => {
  const manifest = { forest: ['tree_oak.png'], mountains: ['mountain_low.png'], landmarks: ['bree.png'] };
  assert.equal(landscapeImageCategory(manifest, 'tree_oak.png'), 'forest');
  assert.equal(landscapeImageCategory(manifest, 'bree.png'), 'landmarks');
  assert.equal(landscapeImageCategory(manifest, 'missing.png'), null);
});
