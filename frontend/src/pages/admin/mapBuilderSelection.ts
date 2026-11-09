type Point = { x: number; y: number };
type Drawing = {
  width: number;
  height: number;
  joints: Record<string, Point>;
  borders: Record<string, { a: string; b: string }>;
  labels?: Record<string, Point>;
};

/** Insert a node on an edge and preserve its two connections. */
export function splitBorderAtPoint(drawing: Drawing, borderId: string, point: Point, endpointTolerance = 0): { jointId: string; created: boolean } | null {
  const border = drawing.borders[borderId];
  if (!border || !Number.isFinite(point.x) || !Number.isFinite(point.y)) return null;
  const a = drawing.joints[border.a], b = drawing.joints[border.b];
  if (!a || !b) return null;
  const dx = b.x - a.x, dy = b.y - a.y;
  const length2 = dx * dx + dy * dy;
  const t = length2 ? Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length2)) : 0;
  const position = { x: a.x + t * dx, y: a.y + t * dy };
  if (Math.hypot(position.x - a.x, position.y - a.y) <= endpointTolerance) return { jointId: border.a, created: false };
  if (Math.hypot(position.x - b.x, position.y - b.y) <= endpointTolerance) return { jointId: border.b, created: false };
  let jointIndex = 1, borderIndex = 1;
  while (`j${jointIndex}` in drawing.joints) jointIndex++;
  while (`b${borderIndex}` in drawing.borders) borderIndex++;
  const jointId = `j${jointIndex}`;
  drawing.joints[jointId] = position;
  drawing.borders[borderId] = { a: border.a, b: jointId };
  drawing.borders[`b${borderIndex}`] = { a: jointId, b: border.b };
  return { jointId, created: true };
}

/** Hit label anchors directly, including labels outside a closed region. */
export function labelAtPoint(labels: Record<string, Point>, point: Point, radius: number): string | null {
  let closest: string | null = null;
  let distance = radius;
  for (const [id, label] of Object.entries(labels)) {
    const candidate = Math.hypot(label.x - point.x, label.y - point.y);
    if (candidate <= distance) {
      closest = id;
      distance = candidate;
    }
  }
  return closest;
}

export function jointsInBox(joints: Drawing['joints'], start: Point, end: Point, previous: string[] = []): string[] {
  const selected = new Set(previous.filter((id) => joints[id]));
  for (const [id, point] of Object.entries(joints)) {
    if (point.x >= Math.min(start.x, end.x) && point.x <= Math.max(start.x, end.x)
      && point.y >= Math.min(start.y, end.y) && point.y <= Math.max(start.y, end.y)) selected.add(id);
  }
  return [...selected];
}

/** Clamp the group as a whole, preserving spacing between nodes at the map edges. */
export function moveJoints(drawing: Drawing, original: Drawing, ids: string[], dx: number, dy: number, labelIds: string[] = []): void {
  const points = ids.map((id) => original.joints[id]).filter(Boolean);
  if (!points.length) return;
  const offsetX = Math.max(-Math.min(...points.map((p) => p.x)), Math.min(dx, original.width - Math.max(...points.map((p) => p.x))));
  const offsetY = Math.max(-Math.min(...points.map((p) => p.y)), Math.min(dy, original.height - Math.max(...points.map((p) => p.y))));
  for (const id of ids) {
    const point = original.joints[id];
    if (point && drawing.joints[id]) drawing.joints[id] = { x: point.x + offsetX, y: point.y + offsetY };
  }
  for (const id of labelIds) {
    const label = original.labels?.[id];
    if (label && drawing.labels?.[id]) drawing.labels[id] = { ...label, x: label.x + offsetX, y: label.y + offsetY };
  }
}

export function deleteJoints(drawing: Drawing, ids: string[]): void {
  const selected = new Set(ids);
  const neighbors = new Map<string, Set<string>>();
  const removed = Object.entries(drawing.borders).filter(([, border]) => selected.has(border.a) || selected.has(border.b));
  for (const border of Object.values(drawing.borders)) {
    if (!drawing.joints[border.a] || !drawing.joints[border.b] || border.a === border.b) continue;
    if (!neighbors.has(border.a)) neighbors.set(border.a, new Set());
    if (!neighbors.has(border.b)) neighbors.set(border.b, new Set());
    neighbors.get(border.a)!.add(border.b);
    neighbors.get(border.b)!.add(border.a);
  }
  // Only intermediate nodes have an unambiguous pair of neighbors. Traverse whole
  // selected chains before mutating so bulk deletion is independent of selection order.
  const intermediate = new Set(ids.filter((id) => neighbors.get(id)?.size === 2));
  const visited = new Set<string>();
  const bridges: { id: string; a: string; b: string }[] = [];
  for (const start of intermediate) {
    if (visited.has(start)) continue;
    const component = new Set<string>();
    const endpoints = new Set<string>();
    const pending = [start];
    while (pending.length) {
      const id = pending.pop()!;
      if (visited.has(id)) continue;
      visited.add(id);
      component.add(id);
      for (const neighbor of neighbors.get(id)!) {
        if (intermediate.has(neighbor)) pending.push(neighbor);
        else if (!selected.has(neighbor)) endpoints.add(neighbor);
      }
    }
    if (endpoints.size === 2) {
      const [a, b] = [...endpoints].sort();
      const borderId = removed.find(([, border]) => component.has(border.a) || component.has(border.b))![0];
      bridges.push({ id: borderId, a, b });
    }
  }
  for (const id of selected) delete drawing.joints[id];
  for (const [id] of removed) delete drawing.borders[id];
  const edgeKey = (a: string, b: string) => JSON.stringify([a, b].sort());
  const connected = new Set(Object.values(drawing.borders).map(({ a, b }) => edgeKey(a, b)));
  for (const { id, a, b } of bridges) {
    if (connected.has(edgeKey(a, b))) continue;
    drawing.borders[id] = { a, b };
    connected.add(edgeKey(a, b));
  }
}
