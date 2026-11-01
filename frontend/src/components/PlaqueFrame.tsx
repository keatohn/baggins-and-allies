import type { CSSProperties } from 'react';
import './PlaqueFrame.css';

export type PlaqueFrameVariant = 'ring' | 'hobbit' | 'elven' | 'dwarven' | 'gondor' | 'rohan' | 'sauron';

/** Native pixel size, plus the clear interior as a fraction of that box. */
const FRAMES: Record<
  PlaqueFrameVariant,
  { file: string; w: number; h: number; padTop: string; padBottom: string; padX: string }
> = {
  ring: { file: 'ring_frame.png', w: 838, h: 214, padTop: '24%', padBottom: '16%', padX: '9%' },
  hobbit: { file: 'hobbit_frame.png', w: 1240, h: 314, padTop: '34%', padBottom: '21%', padX: '11%' },
  elven: { file: 'elven_frame.png', w: 846, h: 216, padTop: '30%', padBottom: '19%', padX: '11%' },
  dwarven: { file: 'dwarven_frame.png', w: 832, h: 216, padTop: '27%', padBottom: '17%', padX: '12%' },
  gondor: { file: 'gondor_frame.png', w: 857, h: 267, padTop: '33%', padBottom: '16%', padX: '10%' },
  rohan: { file: 'rohan_frame.png', w: 1286, h: 320, padTop: '33%', padBottom: '18%', padX: '10%' },
  sauron: { file: 'sauron_frame.png', w: 900, h: 300, padTop: '32%', padBottom: '16%', padX: '10%' },
};

/** CSS variables for the frame image and the padding that keeps text inside the border. */
export function plaqueFrameVars(variant: PlaqueFrameVariant): CSSProperties {
  const f = FRAMES[variant];
  return {
    '--frame-img': `url('/frames/${f.file}')`,
    '--frame-w': f.w,
    '--frame-h': f.h,
    '--plaque-pad-top': f.padTop,
    '--plaque-pad-bottom': f.padBottom,
    '--plaque-pad-x': f.padX,
  } as CSSProperties;
}

/** Banner drawn at its own aspect ratio. Width follows the button; height does not stretch. */
export default function PlaqueFrame({ variant }: { variant: PlaqueFrameVariant }) {
  return <span className={`plaque-frame plaque-frame--${variant}`} aria-hidden="true" />;
}
