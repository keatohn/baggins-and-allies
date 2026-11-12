/**
 * Combat modal round-request flow helpers.
 *
 * Continue computes the *prospective* cumulative casualty lists up-front and passes them into
 * the round request, but they are committed to state only after the request succeeds. A failed
 * request must leave the last resolved round (dice, hits, red Xs) on screen so the user can
 * retry — folding or clearing first wiped the displayed round and permanently folded casualties
 * for a round the UI never got a result for.
 */

/** Prior-round casualties plus the currently displayed round's casualties (ids are unique per battle). */
export function prospectiveCumulativeCasualties(
  previous: readonly string[],
  currentRoundCasualties: readonly string[] | undefined,
): string[] {
  return [...previous, ...(currentRoundCasualties ?? [])];
}

/**
 * Phase to restore when a round request fails: a displayed round returns to the decision state
 * (Continue / Retreat); with no round on screen the modal returns to ready (Start).
 */
export function phaseAfterFailedRoll(hasDisplayedRound: boolean): 'ready' | 'awaiting_decision' {
  return hasDisplayedRound ? 'awaiting_decision' : 'ready';
}
