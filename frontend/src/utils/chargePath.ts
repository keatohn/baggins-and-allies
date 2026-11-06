/**
 * Default charge-through path when the route choice does not matter (issue #113):
 * the shortest route (fewest via territories = fewest steps = cheapest), first path
 * wins ties. Backend charge_routes list BFS order so paths[0] is already shortest;
 * this helper keeps the choice explicit and order-independent.
 */
export function pickDefaultChargePath(paths: string[][] | undefined): string[] | undefined {
  if (!Array.isArray(paths) || paths.length === 0) return undefined;
  let best = paths[0];
  for (const p of paths) {
    if (Array.isArray(p) && p.length < best.length) best = p;
  }
  return best;
}
