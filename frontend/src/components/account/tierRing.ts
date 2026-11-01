import type { CSSProperties } from 'react';
import type { ProgressionTier } from '../../services/api';

/** Ring classes and the `--tier-ring` color for the profile circle. */
export function tierRingProps(tier: ProgressionTier | null | undefined): {
  className: string;
  style: CSSProperties;
} {
  const ring = tier?.visual?.ring_color;
  const glow = tier?.visual?.glow;
  if (!ring || (glow !== 'none' && glow !== 'soft' && glow !== 'pulse')) {
    return { className: '', style: {} };
  }
  return {
    className: ` tier-ring tier-ring--${glow}`,
    style: { '--tier-ring': ring } as CSSProperties,
  };
}
