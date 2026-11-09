import type { CSSProperties } from 'react';
import type { ProgressionTier } from '../../services/api';

/** Color/glow fallback and positioning for the optional frame overlay. */
export function tierRingProps(tier: ProgressionTier | null | undefined): {
  className: string;
  style: CSSProperties;
} {
  const ring = tier?.visual?.ring_color;
  const glow = tier?.visual?.glow;
  const frameClass = tier?.visual?.frame_src ? ' tier-avatar' : '';
  if (!ring || (glow !== 'none' && glow !== 'soft' && glow !== 'pulse')) {
    return { className: frameClass, style: {} };
  }
  return {
    className: ` tier-ring tier-ring--${glow}${frameClass}`,
    style: { '--tier-ring': ring } as CSSProperties,
  };
}
