/** Minimal tray boat shape for allocation math (avoid importing NavalTray.tsx here → circular deps). */
export type TrayBoatForAllocation = {
  boatInstanceId: string;
  transportCapacity: number;
  passengers: { instanceId?: string }[];
};

/** Used by GameMap + NavalTray; tray passenger drag runs on the map DndContext (nested contexts break dnd-kit). */
export function applyPassengerReassignToAllocation(
  loadAllocation: Record<string, string[]>,
  boats: TrayBoatForAllocation[],
  instanceId: string,
  targetBoatId: string,
): Record<string, string[]> | null {
  const currentBoatId = Object.keys(loadAllocation).find((bid) =>
    (loadAllocation[bid] ?? []).includes(instanceId),
  );
  if (currentBoatId === targetBoatId) return null;

  const next: Record<string, string[]> = {};
  for (const [bid, ids] of Object.entries(loadAllocation)) {
    if (bid === currentBoatId) {
      const filtered = (ids ?? []).filter((id) => id !== instanceId);
      if (filtered.length > 0) next[bid] = filtered;
    } else if (bid === targetBoatId) {
      next[bid] = [...(ids ?? []), instanceId];
    } else {
      if ((ids ?? []).length > 0) next[bid] = ids;
    }
  }
  if (!next[targetBoatId]) next[targetBoatId] = [instanceId];
  for (const boat of boats) {
    const cap = boat.transportCapacity ?? 0;
    const confirmed = boat.passengers.filter((p): p is typeof p & { instanceId: string } =>
      typeof p.instanceId === 'string' && p.instanceId !== '',
    ).length;
    const alloc = (next[boat.boatInstanceId] ?? []).length;
    if (confirmed + alloc > cap) return null;
  }
  return next;
}
