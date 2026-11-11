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

export type SelectionBounds = { minX: number; minY: number; maxX: number; maxY: number };
export type ResizeHandle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w';
export type GraphFragment = {
  joints: Drawing['joints'];
  borders: Drawing['borders'];
  labels: Record<string, Point & { display_name?: string }>;
};

export function selectionBounds(drawing: Drawing, ids: string[], labelIds: string[] = []): SelectionBounds | null {
  const points = [...ids.map(id => drawing.joints[id]), ...labelIds.map(id => drawing.labels?.[id])].filter((point): point is Point => !!point);
  if (!points.length) return null;
  return points.reduce((box, point) => ({ minX: Math.min(box.minX, point.x), minY: Math.min(box.minY, point.y),
    maxX: Math.max(box.maxX, point.x), maxY: Math.max(box.maxY, point.y) }),
  { minX: points[0].x, minY: points[0].y, maxX: points[0].x, maxY: points[0].y });
}

/** Copy only internal edges; external neighbors never become part of a paste. */
export function copyGraphFragment(drawing: Drawing, ids: string[], labelIds: string[] = []): GraphFragment | null {
  const selected = new Set(ids.filter(id => drawing.joints[id]));
  if (!selected.size) return null;
  return structuredClone({
    joints: Object.fromEntries([...selected].map(id => [id, drawing.joints[id]])),
    borders: Object.fromEntries(Object.entries(drawing.borders).filter(([, edge]) => selected.has(edge.a) && selected.has(edge.b) && edge.a !== edge.b)),
    labels: Object.fromEntries(labelIds.filter(id => drawing.labels?.[id]).map(id => [id, drawing.labels![id]])),
  });
}

/** Clone a fragment with fresh IDs and one clamped translation for the whole group. */
export function pasteGraphFragment(drawing: Drawing, fragment: GraphFragment, dx: number, dy: number): { ids: string[]; labelIds: string[] } | null {
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return null;
  const bounds = selectionBounds({ ...fragment, width: drawing.width, height: drawing.height }, Object.keys(fragment.joints), Object.keys(fragment.labels));
  if (!bounds || bounds.maxX - bounds.minX > drawing.width || bounds.maxY - bounds.minY > drawing.height) return null;
  const offsetX = Math.max(-bounds.minX, Math.min(dx, drawing.width - bounds.maxX));
  const offsetY = Math.max(-bounds.minY, Math.min(dy, drawing.height - bounds.maxY));
  const counters = new Map<string, number>();
  const fresh = (records: object, prefix: string) => {
    let n = counters.get(prefix) ?? 1;
    while (`${prefix}${n}` in records) n++;
    counters.set(prefix, n + 1);
    return `${prefix}${n}`;
  };
  const remap = new Map<string, string>();
  for (const [id, point] of Object.entries(fragment.joints)) {
    const nextId = fresh(drawing.joints, 'j');
    drawing.joints[nextId] = { x: point.x + offsetX, y: point.y + offsetY };
    remap.set(id, nextId);
  }
  for (const edge of Object.values(fragment.borders)) {
    const a = remap.get(edge.a), b = remap.get(edge.b);
    if (a && b && a !== b) drawing.borders[fresh(drawing.borders, 'b')] = { a, b };
  }
  const labelIds = [];
  if (Object.keys(fragment.labels).length) drawing.labels ??= {};
  for (const [id, label] of Object.entries(fragment.labels)) {
    // Region IDs reference shared seeds. Reuse that reference across maps;
    // within the same map leave the duplicate unassigned rather than inventing a seed.
    if (id in drawing.labels!) continue;
    drawing.labels![id] = { ...label, x: label.x + offsetX, y: label.y + offsetY };
    labelIds.push(id);
  }
  return { ids: [...remap.values()], labelIds };
}

/** Resize from the original snapshot, anchored opposite the dragged handle.
 * Side handles scale one axis; corners preserve the aspect ratio. Clamp the
 * scale, rather than individual points, to preserve shape at the map bounds.
 */
export function resizeJoints(drawing: Drawing, original: Drawing, ids: string[], handle: ResizeHandle, dx: number, dy: number, labelIds: string[] = []): boolean {
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return false;
  const box = selectionBounds(original, ids, labelIds);
  if (!box) return false;
  const width = box.maxX - box.minX, height = box.maxY - box.minY;
  const horizontal = handle.includes('w') ? -1 : handle.includes('e') ? 1 : 0;
  const vertical = handle.includes('n') ? -1 : handle.includes('s') ? 1 : 0;
  const anchorX = horizontal === -1 ? box.maxX : box.minX;
  const anchorY = vertical === -1 ? box.maxY : box.minY;
  const limit = (min: number, max: number, anchor: number, extent: number) => Math.min(
    min < anchor ? anchor / (anchor - min) : Infinity,
    max > anchor ? (extent - anchor) / (max - anchor) : Infinity,
  );
  const maxX = limit(box.minX, box.maxX, anchorX, original.width);
  const maxY = limit(box.minY, box.maxY, anchorY, original.height);
  const clampScale = (scale: number, span: number, max: number) => Math.max(Math.min(1, 1 / Math.max(span, 1)), Math.min(scale, max));
  let sx = 1, sy = 1;
  if (horizontal && vertical) {
    const vx = horizontal * width, vy = vertical * height;
    const length2 = vx * vx + vy * vy;
    if (!length2) return false;
    const scale = 1 + (dx * vx + dy * vy) / length2;
    sx = sy = clampScale(scale, Math.max(width, height), Math.min(maxX, maxY));
  } else if (horizontal && width) {
    sx = clampScale(1 + horizontal * dx / width, width, maxX);
  } else if (vertical && height) {
    sy = clampScale(1 + vertical * dy / height, height, maxY);
  }
  let changed = false;
  const transform = (point: Point) => ({ x: anchorX + (point.x - anchorX) * sx, y: anchorY + (point.y - anchorY) * sy });
  for (const id of ids) {
    const point = original.joints[id];
    if (!point || !drawing.joints[id]) continue;
    const next = transform(point);
    drawing.joints[id] = next;
    changed ||= Math.abs(next.x - point.x) > 1e-9 || Math.abs(next.y - point.y) > 1e-9;
  }
  for (const id of labelIds) {
    const label = original.labels?.[id];
    if (!label || !drawing.labels?.[id]) continue;
    drawing.labels[id] = { ...label, ...transform(label) };
  }
  return changed;
}
