/**
 * Classify wheel input for the game map: zoom (mouse wheel / pinch) vs pan (trackpad).
 *
 * Browsers expose a single `WheelEvent` for mouse wheels and trackpad two-finger
 * scroll — there is no device field — so classification uses gesture shape,
 * biased toward pan: a slow, deliberate two-finger pan must never zoom.
 *
 * Rules, in priority order:
 *
 * - LINE/PAGE deltaMode → zoom (trackpads always report PIXEL; Firefox/Safari
 *   mice report LINE).
 * - Pinch (ctrlKey/metaKey) → zoom only once the gesture accumulates real
 *   magnitude (|deltaY| sums to ≥ 1 across consecutive pinch events). A barely
 *   perceptible pinch blip while panning keeps panning instead of zooming.
 * - Horizontal/diagonal movement → pan (trackpad swipe; also Shift+wheel).
 * - Legacy mouse notch (`wheelDelta` a multiple of ±120) → zoom, unless it has
 *   the Mac trackpad shape `wheelDelta ≈ 3 × deltaY`.
 * - Rapid event stream (< ~40ms apart) → pan (trackpad swipe or momentum).
 * - Small integer ticks (< 20 px on both axes) → pan regardless of timing —
 *   this is the slow, careful Windows/Chrome integer trackpad pan.
 * - Fractional macOS mouse notch band (~3–6 px, e.g. Δy ≈ -4.000244) → zoom.
 * - Shortly after a confident pan, steps under 50 px keep panning (momentum
 *   tails, coalesced mid-pan ticks) unless a rule above claimed them.
 * - Isolated vertical steps ≥ 20 px → zoom (Chromium notches ±33/50/100,
 *   Safari large pixel steps).
 * - Anything else → pan.
 *
 * Heuristics follow the same signals documented by gravity-ui/graph
 * (`docs/system/wheel-intent.md`), trimmed for this game’s pan/zoom needs.
 */

export type WheelNavIntent = 'zoom' | 'pan';

const RAPID_STREAM_MS = 40;
/** Legacy Chromium mouse notch reports wheelDelta in multiples of ±120. */
const LEGACY_WHEEL_DELTA = 120;
/** Mac Chrome trackpads report wheelDelta ≈ 3 × deltaY — never a mouse notch. */
const TRACKPAD_WHEEL_DELTA_RATIO = 3;
/** Ticks with both axes below this are trackpad-shaped, not mouse notches. */
const SMALL_STEP_PX = 20;
/** Isolated vertical steps at/above this are mouse notches (±33/50/100 px). */
const LARGE_STEP_PX = 20;
/** After a confident pan, sub-50 px steps stay pan (coalescing, momentum). */
const PAN_STICKY_MS = 400;
const PAN_STICKY_MAX_PX = 50;
/** Accumulated |deltaY| across consecutive pinch events before pinch is trusted. */
const PINCH_MIN_ENERGY = 1;
/** A pinch blip whose next event comes later than this starts fresh energy. */
const PINCH_RESET_MS = 250;
/** macOS mouse notch: fractional vertical steps in this band (e.g. -4.000244). */
const NOTCH_BAND_MIN = 3;
const NOTCH_BAND_MAX = 6;
const NOTCH_BAND_EPSILON = 0.05;

interface WheelNavState {
  lastTime: number;
  hasLast: boolean;
  lastIntent: WheelNavIntent;
  pinchEnergy: number;
  hasLastPinch: boolean;
  lastPinchTime: number;
}

function wheelDeltaOf(e: WheelEvent): number | undefined {
  const wd = (e as WheelEvent & { wheelDelta?: number }).wheelDelta;
  return typeof wd === 'number' && wd !== 0 ? wd : undefined;
}

/** Chromium/Windows mouse notch: wheelDelta in whole multiples of ±120. */
function isMouseNotchShaped(e: WheelEvent): boolean {
  const wd = wheelDeltaOf(e);
  if (wd === undefined) return false;
  const absWd = Math.abs(wd);
  if (absWd < LEGACY_WHEEL_DELTA || absWd % LEGACY_WHEEL_DELTA !== 0) return false;
  // Exclude the Mac trackpad shape wheelDelta ≈ 3 × deltaY, which can land on
  // a 120 multiple when deltaY is a multiple of 40.
  return Math.abs(absWd - Math.abs(e.deltaY) * TRACKPAD_WHEEL_DELTA_RATIO) > 1.5;
}

function isNotchBand(absY: number): boolean {
  if (absY < NOTCH_BAND_MIN || absY > NOTCH_BAND_MAX) return false;
  return Math.abs(absY - Math.round(absY)) <= NOTCH_BAND_EPSILON;
}

/**
 * Stateful resolver — keep one instance per wheel listener (per map mount)
 * so inter-event timing and pinch energy are tracked across a gesture.
 */
export function createWheelNavIntentResolver(): (e: WheelEvent) => WheelNavIntent {
  const state: WheelNavState = {
    lastTime: 0,
    hasLast: false,
    lastIntent: 'zoom',
    pinchEnergy: 0,
    hasLastPinch: false,
    lastPinchTime: 0,
  };

  return (e: WheelEvent): WheelNavIntent => {
    const decide = (intent: WheelNavIntent): WheelNavIntent => {
      state.lastIntent = intent;
      return intent;
    };

    const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const gapMs = state.hasLast ? now - state.lastTime : Number.POSITIVE_INFINITY;
    state.lastTime = now;
    state.hasLast = true;

    const absX = Math.abs(e.deltaX);
    const absY = Math.abs(e.deltaY);
    const integer = Number.isInteger(e.deltaX) && Number.isInteger(e.deltaY);
    const vertical = absX < absY * 0.5;
    const rapid = gapMs < RAPID_STREAM_MS;

    // LINE/PAGE: classic mouse notch (Firefox/Safari). Trackpads never use these.
    if (e.deltaMode !== 0 /* WheelEvent.DOM_DELTA_PIXEL */) return decide('zoom');

    // Trackpad pinch / Ctrl/Cmd+wheel. Trust zoom only once the pinch carries
    // real magnitude; micro-jitter (a barely-perceptible finger squeeze during
    // a pan) stays under the threshold and keeps panning.
    if (e.ctrlKey || e.metaKey) {
      if (state.hasLastPinch && now - state.lastPinchTime > PINCH_RESET_MS) {
        state.pinchEnergy = 0;
      }
      state.pinchEnergy += absY;
      state.lastPinchTime = now;
      state.hasLastPinch = true;
      return decide(state.pinchEnergy >= PINCH_MIN_ENERGY ? 'zoom' : 'pan');
    }
    state.pinchEnergy = 0;
    state.hasLastPinch = false;

    // Horizontal / diagonal two-finger swipe → pan.
    if (absX >= 1 && absX >= absY * 0.5) return decide('pan');

    // Legacy mouse notch (wheelDelta multiple of ±120) → zoom at any speed.
    if (isMouseNotchShaped(e)) return decide('zoom');

    // Rapid stream → trackpad (fast swipe or inertia).
    if (rapid) return decide('pan');

    // Small integer ticks are a deliberate slow trackpad pan (Windows/Chrome
    // integer deltas), not a mouse notch — even when events arrive far apart.
    if (integer && absX < SMALL_STEP_PX && absY < SMALL_STEP_PX) return decide('pan');

    // Fractional macOS mouse notch (~3–6 px) → zoom.
    if (!integer && vertical && isNotchBand(absY)) return decide('zoom');

    // Shortly after a confident pan, keep panning through momentum tails and
    // coalesced mid-pan ticks; confident shapes were claimed by rules above.
    if (
      state.lastIntent === 'pan' &&
      gapMs < PAN_STICKY_MS &&
      absY < PAN_STICKY_MAX_PX &&
      vertical
    ) {
      return decide('pan');
    }

    // Isolated large vertical step → mouse notch (Chromium ±33/50/100,
    // Safari's large pixel steps, coalesced smooth-scroll ticks).
    if (vertical && absY >= LARGE_STEP_PX) return decide('zoom');

    // Anything else (slow small fractional vertical steps) → trackpad pan.
    return decide('pan');
  };
}

/**
 * Scale multiplier for a zoom-intent wheel event.
 *
 * Pinch / Ctrl+wheel keeps a gentle continuous feel (tiny browser deltas are boosted).
 * Plain mouse wheel always applies a clearly visible step — macOS notches report
 * deltaY ≈ ±3, which the old exp mapping turned into a ~0.1% no-op.
 */
export function wheelZoomScaleMultiplier(e: WheelEvent): number {
  let dy = e.deltaY;
  if (e.deltaMode === 1 /* DOM_DELTA_LINE */) {
    dy *= 24;
  } else if (e.deltaMode === 2 /* DOM_DELTA_PAGE */) {
    dy *= 320;
  }

  if (e.ctrlKey || e.metaKey) {
    // Browser pinch deltas are tiny — boost so pinch feels responsive.
    dy *= 75;
    const raw = Math.exp(-dy * 0.00028);
    return Math.max(0.965, Math.min(1.035, raw));
  }

  // Plain wheel (mouse notch / smooth-scroll): ~7–15% per event, with a floor
  // so small macOS integer notches still zoom visibly (buttons use 20%).
  const raw = Math.exp(-dy * 0.001);
  const maxZoomIn = 1.15;
  const maxZoomOut = 1 / maxZoomIn;
  const minStep = 1.07;
  let mult = Math.max(maxZoomOut, Math.min(maxZoomIn, raw));
  if (mult > 1 && mult < minStep) mult = minStep;
  if (mult < 1 && mult > 1 / minStep) mult = 1 / minStep;
  return mult;
}
