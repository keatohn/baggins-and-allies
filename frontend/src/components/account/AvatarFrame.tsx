import { useState } from 'react';
import type { ProgressionTier } from '../../services/api';
import './AvatarFrame.css';

/** Decorative tier artwork. Failed images leave the normal color/glow ring visible. */
export default function AvatarFrame({ tier }: { tier?: ProgressionTier | null }) {
  const src = tier?.visual?.frame_src;
  return src ? <FrameImage key={src} src={src} /> : null;
}

function FrameImage({ src }: { src: string }) {
  const [state, setState] = useState<'loading' | 'loaded' | 'failed'>('loading');
  if (state === 'failed') return null;
  return (
    <img
      src={src}
      className={`tier-avatar__frame${state === 'loaded' ? ' tier-avatar__frame--loaded' : ''}`}
      alt=""
      aria-hidden="true"
      draggable={false}
      onLoad={() => setState('loaded')}
      onError={() => setState('failed')}
    />
  );
}
