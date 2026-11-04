/**
 * Classify wheel input for the game map: zoom (mouse wheel / pinch) vs pan (trackpad).
 *
 * Browsers expose a single `WheelEvent` for mouse wheels and trackpad two-finger
 * scroll — there is no device field — so classification uses gesture shape:
 *
 * - Pinch (ctrlKey/metaKey) and LINE/PAGE deltaMode → zoom. Trackpads always
 *   report PIXEL mode; Firefox/Safari mice often report LINE.
 * - Horizontal/diagonal movement → pan (trackpad).
 * - Rapid event stream (< ~40ms apart) → pan (trackpad, including momentum).
 *   Mouse notches almost never arrive that fast. Legacy `wheelDelta` ±120
 *   (Windows Chromium mouse) still zooms inside a stream.
 * - Isolated events → zoom (Windows ±100 notches, macOS small integer notches,
 *   smooth-scroll fractional mice).
 * - After a trackpad gesture, small fractional momentum ticks keep panning;
 *   a confident mouse step (large step or isolated integer notch) switches
 *   back to zoom.
 *
 * Heuristics follow the same signals documented by gravity-ui/graph
 * (`docs/system/wheel-intent.md`), trimmed for this game’s pan/zoom needs.
 */

export type WheelNavIntent = 'zoom' | 'pan';

const RAPID_STREAM_MS = 40;
/** Legacy Chromium/Windows mouse notch reports wheelDelta ≈ ±120 (deltaY ≈ ∓100). */
const LEGACY_WHEEL_DELTA = 120;
/** Isolated integer pixel steps at or above this are a mouse notch, not a trackpad tick. */
const CONFIDENT_MOUSE_STEP_PX = 20;

interface WheelNavState {
  lastTime: number;
  hasLast: boolean;
  lastIntent: WheelNavIntent;
}

function isLegacyWindowsMouseNotch(e: WheelEvent): boolean {
  const wd = (e as WheelEvent & { wheelDelta?: number }).wheelDelta;
  return typeof wd === 'number' && Math.abs(Math.abs(wd) - LEGACY_WHEEL_DELTA) <= 1;
}

/**
 * Stateful resolver — keep one instance per wheel listener (per map mount)
 * so inter-event timing is tracked across a gesture.
 */
export function createWheelNavIntentResolver(): (e: WheelEvent) => WheelNavIntent {
  const state: WheelNavState = { lastTime: 0, hasLast: false, lastIntent: 'zoom' };

  return (e: WheelEvent): WheelNavIntent => {
    const decide = (intent: WheelNavIntent): WheelNavIntent => {
      state.lastIntent = intent;
      return intent;
    };

    // Trackpad pinch and Ctrl/Cmd+wheel always zoom.
    if (e.ctrlKey || e.metaKey) return decide('zoom');

    // LINE/PAGE: classic mouse notch (Firefox etc.). Trackpads never use these.
    if (e.deltaMode !== 0 /* WheelEvent.DOM_DELTA_PIXEL */) return decide('zoom');

    const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const gapMs = state.hasLast ? now - state.lastTime : Number.POSITIVE_INFINITY;
    state.lastTime = now;
    state.hasLast = true;

    const absX = Math.abs(e.deltaX);
    const absY = Math.abs(e.deltaY);
    const integer = Number.isInteger(e.deltaX) && Number.isInteger(e.deltaY);
    const pureVertical = absX === 0;
    const rapid = gapMs < RAPID_STREAM_MS;

    // Horizontal / diagonal two-finger swipe → pan.
    if (absX >= 1 && absX >= absY * 0.5) return decide('pan');

    // Rapid stream → trackpad (fast swipe or inertia), unless legacy Windows mouse notch.
    if (rapid) {
      if (isLegacyWindowsMouseNotch(e)) return decide('zoom');
      return decide('pan');
    }

    // After a trackpad gesture: momentum/inertia tails are small fractional
    // ticks — keep panning so the gesture does not flip to zoom as gaps grow.
    // Confident mouse shapes (large steps, isolated macOS integer notches)
    // fall through and switch back to zoom.
    if (
      state.lastIntent === 'pan' &&
      !integer &&
      absY < CONFIDENT_MOUSE_STEP_PX &&
      absX < absY * 0.5
    ) {
      return decide('pan');
    }

    // Isolated mouse-like step (allow tiny horizontal noise on a vertical notch):
    // large integer notch, small macOS integer notch, or smooth-scroll fractional
    // (high-res / free-spin mice).
    if (absX < absY * 0.5) {
      if (absY >= CONFIDENT_MOUSE_STEP_PX) return decide('zoom');
      if (!integer) return decide('zoom');
      if (pureVertical) return decide('zoom');
    }
    return decide(state.lastIntent);
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
