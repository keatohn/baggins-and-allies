type Point = { x: number; y: number };
export type BorderSegment = { a: Point; b: Point };
type Outline = { getTotalLength(): number; getPointAtLength(distance: number): Point; getBBox?(): { x: number; y: number; width: number; height: number } };
export type SharedBorderTarget = { territory: string; neighbor: string; segments: BorderSegment[] };

function projection(point: Point, segment: BorderSegment): Point {
  const dx = segment.b.x - segment.a.x, dy = segment.b.y - segment.a.y;
  const length2 = dx * dx + dy * dy;
  const t = length2 ? Math.max(0, Math.min(1, ((point.x - segment.a.x) * dx + (point.y - segment.a.y) * dy) / length2)) : 0;
  return { x: segment.a.x + t * dx, y: segment.a.y + t * dy };
}
const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

function sample(path: Outline): BorderSegment[] {
  const length = path.getTotalLength();
  if (!Number.isFinite(length) || length <= 0) return [];
  const count = Math.min(20000, Math.max(1, Math.ceil(length / 4)));
  const step = length / count;
  const segments: BorderSegment[] = [];
  let a = path.getPointAtLength(0);
  for (let i = 1; i <= count; i++) {
    const b = path.getPointAtLength(i * step);
    // SVG moveto jumps must never become artificial shared edges between islands.
    if (distance(a, b) <= step * 1.5) segments.push({ a: { x: a.x, y: a.y }, b: { x: b.x, y: b.y } });
    a = b;
  }
  return segments;
}

/** Match actual outlines, not movement adjacency (mountain borders may be impassable). */
export function sharedBorderSegments(first: Outline, second: Outline, tolerance = 2): BorderSegment[] {
  const cell = 32;
  const buckets = new Map<string, BorderSegment[]>();
  for (const segment of sample(second)) {
    for (let x = Math.floor((Math.min(segment.a.x, segment.b.x) - tolerance) / cell); x <= Math.floor((Math.max(segment.a.x, segment.b.x) + tolerance) / cell); x++) {
      for (let y = Math.floor((Math.min(segment.a.y, segment.b.y) - tolerance) / cell); y <= Math.floor((Math.max(segment.a.y, segment.b.y) + tolerance) / cell); y++) {
        const key = `${x},${y}`;
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key)!.push(segment);
      }
    }
  }
  const result: BorderSegment[] = [];
  let run: BorderSegment[] = [];
  const flush = () => {
    // Corner contact alone is not a border. Keep only meaningful shared runs.
    if (run.reduce((sum, s) => sum + distance(s.a, s.b), 0) >= 8) result.push(...run);
    run = [];
  };
  for (const segment of sample(first)) {
    const mid = { x: (segment.a.x + segment.b.x) / 2, y: (segment.a.y + segment.b.y) / 2 };
    const candidates = buckets.get(`${Math.floor(mid.x / cell)},${Math.floor(mid.y / cell)}`) ?? [];
    const close = (p: Point) => candidates.some((other) => distance(p, projection(p, other)) <= tolerance);
    if (close(segment.a) && close(mid) && close(segment.b)) {
      if (run.length && distance(run[run.length - 1].b, segment.a) > 0.1) flush();
      run.push(segment);
    } else flush();
  }
  flush();
  return result;
}

export function nearestPointOnBorder(segments: BorderSegment[], point: Point): Point | null {
  let best: Point | null = null;
  let bestDistance = Infinity;
  for (const segment of segments) {
    const candidate = projection(point, segment);
    const d = distance(point, candidate);
    if (d < bestDistance) { best = candidate; bestDistance = d; }
  }
  return best;
}

/** Identify the shared edge nearest the clicked outline, never a remote border/coast. */
export function sharedBorderAtPoint(territory: string, outlines: { id: string; path: Outline }[], point: Point): SharedBorderTarget | null {
  const first = outlines.find(({ id }) => id.toLowerCase() === territory.toLowerCase());
  if (!first) return null;
  const outlinePoint = nearestPointOnBorder(sample(first.path), point);
  if (!outlinePoint) return null;
  let best: SharedBorderTarget | null = null;
  let bestDistance = Infinity;
  for (const other of outlines) {
    if (other.id.toLowerCase() === territory.toLowerCase()) continue;
    const box = other.path.getBBox?.();
    if (box && (outlinePoint.x < box.x - 8 || outlinePoint.x > box.x + box.width + 8 || outlinePoint.y < box.y - 8 || outlinePoint.y > box.y + box.height + 8)) continue;
    const segments = sharedBorderSegments(first.path, other.path);
    const nearest = nearestPointOnBorder(segments, point);
    if (!nearest || distance(nearest, outlinePoint) > 8) continue;
    const d = distance(nearest, point);
    if (d < bestDistance) { bestDistance = d; best = { territory: first.id, neighbor: other.id, segments }; }
  }
  return best;
}

/** Spacing restarts at disconnected runs; never draws across gaps between islands. */
export function pointsAlongBorder(segments: BorderSegment[], spacing: number): Point[] {
  if (!Number.isFinite(spacing) || spacing <= 0) return [];
  const points: Point[] = [];
  let remaining = spacing / 2;
  let previous: Point | null = null;
  let run: BorderSegment[] = [];
  let runStart = 0;
  const finishRun = () => {
    if (run.length && points.length === runStart) {
      let half = run.reduce((sum, s) => sum + distance(s.a, s.b), 0) / 2;
      for (const { a, b } of run) {
        const length = distance(a, b);
        if (half <= length) { points.push({ x: a.x + (b.x - a.x) * half / length, y: a.y + (b.y - a.y) * half / length }); break; }
        half -= length;
      }
    }
    run = [];
    runStart = points.length;
  };
  for (const { a, b } of segments) {
    if (previous && distance(previous, a) > 0.1) { finishRun(); remaining = spacing / 2; }
    const length = distance(a, b);
    if (!length) continue;
    run.push({ a, b });
    while (remaining <= length) {
      points.push({ x: a.x + (b.x - a.x) * remaining / length, y: a.y + (b.y - a.y) * remaining / length });
      remaining += spacing;
    }
    remaining -= length;
    previous = b;
  }
  finishRun();
  return points;
}
