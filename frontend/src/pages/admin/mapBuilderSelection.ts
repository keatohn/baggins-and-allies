type Point = { x: number; y: number };
type Drawing = {
  width: number;
  height: number;
  joints: Record<string, Point>;
  borders: Record<string, { a: string; b: string }>;
};

export function jointsInBox(joints: Drawing['joints'], start: Point, end: Point, previous: string[] = []): string[] {
  const selected = new Set(previous.filter((id) => joints[id]));
  for (const [id, point] of Object.entries(joints)) {
    if (point.x >= Math.min(start.x, end.x) && point.x <= Math.max(start.x, end.x)
      && point.y >= Math.min(start.y, end.y) && point.y <= Math.max(start.y, end.y)) selected.add(id);
  }
  return [...selected];
}

/** Clamp the group as a whole, preserving spacing between nodes at the map edges. */
export function moveJoints(drawing: Drawing, original: Drawing, ids: string[], dx: number, dy: number): void {
  const points = ids.map((id) => original.joints[id]).filter(Boolean);
  if (!points.length) return;
  const offsetX = Math.max(-Math.min(...points.map((p) => p.x)), Math.min(dx, original.width - Math.max(...points.map((p) => p.x))));
  const offsetY = Math.max(-Math.min(...points.map((p) => p.y)), Math.min(dy, original.height - Math.max(...points.map((p) => p.y))));
  for (const id of ids) {
    const point = original.joints[id];
    if (point && drawing.joints[id]) drawing.joints[id] = { x: point.x + offsetX, y: point.y + offsetY };
  }
}

export function deleteJoints(drawing: Drawing, ids: string[]): void {
  const selected = new Set(ids);
  for (const id of selected) delete drawing.joints[id];
  for (const [id, border] of Object.entries(drawing.borders)) {
    if (selected.has(border.a) || selected.has(border.b)) delete drawing.borders[id];
  }
}
