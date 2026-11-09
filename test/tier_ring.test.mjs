import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tierRingProps } from '../frontend/src/components/account/tierRing.ts';

test('unframed portraits retain their existing ring color and glow', () => {
  assert.deepEqual(tierRingProps(null), { className: '', style: {} });
  const result = tierRingProps({ visual: { ring_color: '#c9a227', glow: 'soft', frame_src: null } });
  assert.equal(result.className, ' tier-ring tier-ring--soft');
  assert.deepEqual(result.style, { '--tier-ring': '#c9a227' });
});

test('configured artwork adds overlay positioning without losing the ring fallback', () => {
  const result = tierRingProps({ visual: { ring_color: '#c9a227', glow: 'pulse', frame_src: '/frames/gold.png' } });
  assert.equal(result.className, ' tier-ring tier-ring--pulse tier-avatar');
  assert.deepEqual(result.style, { '--tier-ring': '#c9a227' });
});

test('frame positioning does not require a color/glow configuration', () => {
  assert.deepEqual(tierRingProps({ visual: { frame_src: 'https://example.com/frame.png' } }), { className: ' tier-avatar', style: {} });
});
