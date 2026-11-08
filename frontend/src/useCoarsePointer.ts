import { useSyncExternalStore } from 'react';

/**
 * True on touch-first devices (`pointer: coarse`).
 *
 * Used to downgrade the expensive parts of the map (SVG glow filters, blend-mode
 * parchment texture, infinite wave/particle animation) and to poll game state at
 * half rate on phones/tablets, where a poll re-renders the whole map.
 */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (onStoreChange) => {
      const mq = window.matchMedia('(pointer: coarse)');
      mq.addEventListener('change', onStoreChange);
      return () => mq.removeEventListener('change', onStoreChange);
    },
    () => window.matchMedia('(pointer: coarse)').matches,
    () => false,
  );
}
