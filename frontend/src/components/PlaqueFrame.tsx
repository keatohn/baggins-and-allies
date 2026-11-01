import type { CSSProperties } from 'react';
import './PlaqueFrame.css';

export type PlaqueFrameVariant = 'ring' | 'hobbit' | 'elven' | 'dwarven' | 'gondor' | 'rohan' | 'sauron';

/** Native pixel size of each transparent banner in public/frames. */
const FRAMES: Record<PlaqueFrameVariant, { file: string; w: number; h: number }> = {
  ring: { file: 'ring_frame.png', w: 838, h: 214 },
  hobbit: { file: 'hobbit_frame.png', w: 1240, h: 314 },
  elven: { file: 'elven_frame.png', w: 846, h: 216 },
  dwarven: { file: 'dwarven_frame.png', w: 832, h: 216 },
  gondor: { file: 'gondor_frame.png', w: 857, h: 267 },
  rohan: { file: 'rohan_frame.png', w: 1286, h: 320 },
  sauron: { file: 'sauron_frame.png', w: 900, h: 300 },
};

/**
 * Wide banner frame fitted to a taller box. The banner is scaled to the box
 * width, cut at mid-height, and the top and bottom halves pinned to the box
 * edges. The gap between them is filled by stretching the banner's middle
 * row, so side ornaments continue as straight stems. Banners are transparent,
 * so whatever is behind the box shows through.
 */
export default function PlaqueFrame({ variant }: { variant: PlaqueFrameVariant }) {
  const f = FRAMES[variant];
  const midRow = Math.round(f.h / 2);
  // background-position % maps to (box - image) so row `midRow` lands at the top edge
  // once the image is scaled to (h * 100%) tall.
  const midPos = (midRow / (f.h - 1)) * 100;
  const style = {
    '--frame-img': `url('/frames/${f.file}')`,
    '--frame-w': f.w,
    '--frame-h': f.h,
    '--frame-mid-pos': `${midPos}%`,
  } as CSSProperties;
  return (
    <span className={`plaque-frame plaque-frame--${variant}`} style={style} aria-hidden="true">
      <span className="plaque-frame__top" />
      <span className="plaque-frame__mid" />
      <span className="plaque-frame__bottom" />
    </span>
  );
}
