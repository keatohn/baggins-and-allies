/**
 * Land parchment grain. One shared tile, painted over territory fills.
 * Raise `opacity` for a stronger grain; set `enabled` false to turn it off.
 * The map builder can pass these through later without a new rendering path.
 */
export const MAP_TEXTURE_CONFIG = {
  enabled: true,
  /** Multiply strength of the grain. About a 10–15% darkening on the darkest flecks. */
  opacity: 0.55,
  tileSize: 512,
  applyToLandOnly: true,
  src: '/assets/parchment-texture.png',
};
