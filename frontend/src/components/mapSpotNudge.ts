/**
 * Pixel nudge applied to the unit layer after centroid→screen (SVG geometry vs visible map art).
 * Lives outside GameMap.tsx so the admin Maps editor can reproduce the exact rendered spot
 * (GameMap only exports its component). Skipped at render time when an SVG units hint (or a
 * map-level unit override) anchors the stack.
 */
export const TERRITORY_UNIT_PIXEL_NUDGE: Record<string, { x?: number; y?: number }> = {
  grey_havens: { y: 20 },
  /* sea_zone_2 path bbox is very wide; pole can sit past the illustrated water — nudge east into the gulf (keep y tiny so stacks don’t read as sea zone 3) */
  sea_zone_2: { x: 54, y: 45 },
};
