/**
 * Combat modal header round indicators, in engine execution order.
 *
 * The engine resolves dedicated rounds as: stealth prefire → siegeworks round →
 * defender archer prefire → standard rounds (reducer: initiate/continue use
 * pending_combat_round). The header used to render "Archers" first, which put
 * the pending archer chip ahead of the siegeworks round that actually runs
 * before it (audit Part 2, L1).
 */

export interface HeaderIndicatorSource {
  /** Resolved round currently displayed (undefined before the first round). */
  currentRound?: {
    isArcherPrefire?: boolean;
    isStealthPrefire?: boolean;
    isSiegeworksRound?: boolean;
  } | null;
  roundNumber: number;
  archerPrefirePending?: boolean;
  siegeworksPending?: boolean;
}

/** Labels for the header chips, ordered the way the engine runs them. */
export function combatHeaderIndicators(src: HeaderIndicatorSource): string[] {
  const { currentRound, roundNumber, archerPrefirePending = false, siegeworksPending = false } = src;
  const labels: string[] = [];
  if (currentRound?.isStealthPrefire) labels.push('Stealth');
  if (currentRound?.isSiegeworksRound || (roundNumber === 0 && siegeworksPending)) labels.push('Siegeworks');
  if (currentRound?.isArcherPrefire || (roundNumber === 0 && archerPrefirePending)) labels.push('Archers');
  const isDedicatedRound =
    currentRound?.isArcherPrefire === true ||
    currentRound?.isStealthPrefire === true ||
    currentRound?.isSiegeworksRound === true;
  if (roundNumber > 0 && !isDedicatedRound) labels.push(`Round ${roundNumber}`);
  return labels;
}
