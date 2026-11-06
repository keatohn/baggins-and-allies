import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { api } from '../../services/api';
import { fetchMapSvgText } from './territoryGraph';

/**
 * Admin Map Builder (Maps editor → Builder pill).
 *
 * Draws territory borders as a planar graph of shared line segments (each border
 * stored once and reused by both neighbours). Closed-path SVGs (including the
 * built-in maps) import into that graph. Saving stores the drawing and the
 * generated territory SVG with the other map data; that SVG is what setups and
 * the icon/landscape editor load, ahead of a built-in file of the same id.
 */

type Pt = { x: number; y: number };
type Joint = { x: number; y: number };
type Border = { a: string; b: string };
type TerritoryLabel = { display_name: string; x: number; y: number };

type BuilderDoc = {
  width: number;
  height: number;
  joints: Record<string, Joint>;
  borders: Record<string, Border>;
  labels: Record<string, TerritoryLabel>;
};

type Tool = 'select' | 'pen' | 'pencil' | 'label';
type Selection = { kind: 'joint' | 'border' | 'label'; id: string } | null;

const LEGACY_STORAGE_KEY = 'admin_map_builder_doc_v1';
const DRAFTS_KEY = 'admin_map_builder_drafts_v1';
const SNAP_PX = 10;
const HIT_PX = 7;
const JOINT_R_PX = 5;
const MAX_HISTORY = 100;
const PENCIL_MIN_PX = 4;
const RDP_EPS_PX = 1.5;
const CLICK_PX = 8;
const AREA_EPS = 1e-6;
const TID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const MAP_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const MIN_DIM = 100;
const MAX_DIM = 50000;
const DEFAULT_W = 3500;
const DEFAULT_H = 2600;

const TOOL_HINTS: Record<Tool, string> = {
  select: 'Drag joints to move them · click a border to select · Delete removes · drag the background to pan',
  pen: 'Click to place points (endpoints snap onto joints or split borders they land on) · drag to pan · Enter or click the first point finishes · Esc cancels',
  pencil: 'Drag to draw a freehand border · ends snap and join the shared graph',
  label: 'Click inside a closed region to name it · click an existing label to edit · drag to pan',
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function cloneDoc(doc: BuilderDoc): BuilderDoc {
  return structuredClone(doc);
}

function defaultFrame(width: number, height: number): BuilderDoc {
  return {
    width,
    height,
    joints: {
      f_tl: { x: 0, y: 0 },
      f_tr: { x: width, y: 0 },
      f_br: { x: width, y: height },
      f_bl: { x: 0, y: height },
    },
    borders: {
      fb_t: { a: 'f_tl', b: 'f_tr' },
      fb_r: { a: 'f_tr', b: 'f_br' },
      fb_b: { a: 'f_br', b: 'f_bl' },
      fb_l: { a: 'f_bl', b: 'f_tl' },
    },
    labels: {},
  };
}

function isBuilderDoc(d: unknown): d is BuilderDoc {
  const doc = d as BuilderDoc | null | undefined;
  return !!(
    doc &&
    typeof doc.width === 'number' &&
    typeof doc.height === 'number' &&
    doc.joints &&
    typeof doc.joints === 'object' &&
    doc.borders &&
    typeof doc.borders === 'object' &&
    doc.labels &&
    typeof doc.labels === 'object'
  );
}

type Draft = { id: string; name: string; updated: number; doc: BuilderDoc; mapBase?: string | null };
type SavedMap = {
  map_base: string;
  display_name: string;
  width: number;
  height: number;
  territories: number;
  updated_at: string | null;
};
type DraftStore = { currentId: string; drafts: Draft[] };

function makeDraft(name: string, doc: BuilderDoc): Draft {
  return { id: `d_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`, name, updated: Date.now(), doc };
}

function defaultStore(): DraftStore {
  const draft = makeDraft('Draft 1', defaultFrame(DEFAULT_W, DEFAULT_H));
  return { currentId: draft.id, drafts: [draft] };
}

function loadStore(): DraftStore {
  try {
    const raw = localStorage.getItem(DRAFTS_KEY);
    if (raw) {
      const s = JSON.parse(raw) as DraftStore;
      if (
        s &&
        typeof s.currentId === 'string' &&
        Array.isArray(s.drafts) &&
        s.drafts.length > 0 &&
        s.drafts.every(
          (d) => d && typeof d.id === 'string' && typeof d.name === 'string' && isBuilderDoc(d.doc),
        )
      ) {
        if (!s.drafts.some((d) => d.id === s.currentId)) s.currentId = s.drafts[0].id;
        return s;
      }
    }
  } catch {
    /* fall through */
  }
  // Migrate the legacy single-document key.
  try {
    const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (raw) {
      const legacy = JSON.parse(raw) as BuilderDoc;
      if (isBuilderDoc(legacy)) {
        const draft = makeDraft('Draft 1', legacy);
        const store: DraftStore = { currentId: draft.id, drafts: [draft] };
        saveStore(store);
        return store;
      }
    }
  } catch {
    /* fall through */
  }
  return defaultStore();
}

function saveStore(s: DraftStore): void {
  try {
    localStorage.setItem(DRAFTS_KEY, JSON.stringify(s));
  } catch {
    /* storage full/blocked — ignore */
  }
}

function downloadText(filename: string, text: string, mime: string): void {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function normalizeTid(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/'/g, '')
    .replace(/[\s-]+/g, '_')
    .replace(/[^a-z0-9_.]/g, '');
}

function newId(dict: Record<string, unknown>, prefix: string): string {
  let i = 1;
  while (`${prefix}${i}` in dict) i += 1;
  return `${prefix}${i}`;
}

function closestOnSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2;
  t = clamp(t, 0, 1);
  return { x: ax + t * dx, y: ay + t * dy, t };
}

function distToSegment(p: Pt, a: Pt, b: Pt): number {
  const c = closestOnSegment(p.x, p.y, a.x, a.y, b.x, b.y);
  return Math.hypot(p.x - c.x, p.y - c.y);
}

function rdp(points: Pt[], eps: number): Pt[] {
  if (points.length < 3) return points;
  const keep = new Array<boolean>(points.length).fill(false);
  keep[0] = true;
  keep[points.length - 1] = true;
  const stack: [number, number][] = [[0, points.length - 1]];
  while (stack.length) {
    const [s, e] = stack.pop() as [number, number];
    if (e - s < 2) continue;
    let maxD = -1;
    let idx = -1;
    for (let i = s + 1; i < e; i += 1) {
      const d = distToSegment(points[i], points[s], points[e]);
      if (d > maxD) {
        maxD = d;
        idx = i;
      }
    }
    if (maxD > eps && idx > 0) {
      keep[idx] = true;
      stack.push([s, idx], [idx, e]);
    }
  }
  return points.filter((_, i) => keep[i]);
}

// --- face tracing (mirrors backend/map_builder.py) ----------------------------

type Ring = { area: number; ring: string[] };

function signedArea(ring: string[], joints: Record<string, Joint>): number {
  let total = 0;
  const n = ring.length;
  for (let i = 0; i < n; i += 1) {
    const a = joints[ring[i]];
    const b = joints[ring[(i + 1) % n]];
    if (!a || !b) continue;
    total += a.x * b.y - b.x * a.y;
  }
  return total / 2;
}

function traceRings(doc: BuilderDoc): Ring[] {
  const { joints, borders } = doc;
  const outgoing: Record<string, { ang: number; to: string }[]> = {};
  for (const jid of Object.keys(joints)) outgoing[jid] = [];
  for (const b of Object.values(borders)) {
    const A = joints[b.a];
    const B = joints[b.b];
    if (!A || !B) continue;
    outgoing[b.a].push({ ang: Math.atan2(B.y - A.y, B.x - A.x) % (2 * Math.PI), to: b.b });
    outgoing[b.b].push({ ang: Math.atan2(A.y - B.y, A.x - B.x) % (2 * Math.PI), to: b.a });
  }
  for (const lst of Object.values(outgoing)) lst.sort((p, q) => p.ang - q.ang);

  const pos = new Map<string, number>();
  for (const [frm, lst] of Object.entries(outgoing)) {
    lst.forEach((e, i) => pos.set(`${frm}>${e.to}`, i));
  }

  const used = new Set<string>();
  const rings: Ring[] = [];
  for (const frm of Object.keys(outgoing).sort()) {
    const lst = outgoing[frm];
    for (let i = 0; i < lst.length; i += 1) {
      const startKey = `${frm}>${lst[i].to}`;
      if (used.has(startKey)) continue;
      const ring: string[] = [];
      let curFrm = frm;
      let curTo = lst[i].to;
      let curKey = startKey;
      while (!used.has(curKey)) {
        used.add(curKey);
        ring.push(curFrm);
        const vl = outgoing[curTo];
        const j = pos.get(`${curTo}>${curFrm}`) as number;
        const next = vl[(j - 1 + vl.length) % vl.length];
        curFrm = curTo;
        curTo = next.to;
        curKey = `${curFrm}>${curTo}`;
      }
      if (ring.length < 3) continue;
      rings.push({ area: signedArea(ring, joints), ring });
    }
  }
  return rings;
}

function interiorRings(doc: BuilderDoc): Ring[] {
  return traceRings(doc)
    .filter((r) => r.area > AREA_EPS)
    .sort((a, b) => b.area - a.area);
}

function pointInRing(x: number, y: number, ring: string[], joints: Record<string, Joint>): boolean {
  let inside = false;
  const n = ring.length;
  let j = n - 1;
  for (let i = 0; i < n; i += 1) {
    const a = joints[ring[i]];
    const b = joints[ring[j]];
    if (!a || !b) {
      j = i;
      continue;
    }
    if (a.y > y !== b.y > y) {
      if (x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
    }
    j = i;
  }
  return inside;
}

function findRingIndex(rings: Ring[], joints: Record<string, Joint>, x: number, y: number): number | null {
  let best: number | null = null;
  let bestArea = 0;
  for (let i = 0; i < rings.length; i += 1) {
    const { area, ring } = rings[i];
    if (best !== null && area >= bestArea) continue;
    if (pointInRing(x, y, ring, joints)) {
      best = i;
      bestArea = area;
    }
  }
  return best;
}

// --- point resolution: snap to joint, split a border, or create ---------------

type ResolveResult = { jointId: string };

function resolvePoint(doc: BuilderDoc, x: number, y: number, snapWorld: number, hitWorld: number): ResolveResult {
  let best: string | null = null;
  let bestD = snapWorld;
  for (const [jid, j] of Object.entries(doc.joints)) {
    const d = Math.hypot(j.x - x, j.y - y);
    if (d < bestD) {
      bestD = d;
      best = jid;
    }
  }
  if (best) return { jointId: best };

  let hitBorder: string | null = null;
  let hitDist = hitWorld;
  let hitPt: Pt | null = null;
  let hitT = 0;
  for (const [bid, b] of Object.entries(doc.borders)) {
    const A = doc.joints[b.a];
    const B = doc.joints[b.b];
    if (!A || !B) continue;
    const c = closestOnSegment(x, y, A.x, A.y, B.x, B.y);
    const d = Math.hypot(x - c.x, y - c.y);
    if (d < hitDist) {
      hitDist = d;
      hitBorder = bid;
      hitPt = { x: c.x, y: c.y };
      hitT = c.t;
    }
  }
  if (hitBorder && hitPt) {
    const b = doc.borders[hitBorder];
    // Near an endpoint: reuse that joint instead of splitting.
    if (hitT < 0.02) return { jointId: b.a };
    if (hitT > 0.98) return { jointId: b.b };
    const jid = newId(doc.joints, 'j');
    doc.joints[jid] = { x: hitPt.x, y: hitPt.y };
    doc.borders[hitBorder] = { a: b.a, b: jid };
    const tailId = newId(doc.borders, 'b');
    doc.borders[tailId] = { a: jid, b: b.b };
    return { jointId: jid };
  }

  const jid = newId(doc.joints, 'j');
  doc.joints[jid] = { x, y };
  return { jointId: jid };
}

function addBorder(doc: BuilderDoc, a: string, b: string): void {
  if (a === b) return;
  const bid = newId(doc.borders, 'b');
  doc.borders[bid] = { a, b };
}

// --- component ----------------------------------------------------------------

export function MapBuilder() {
  const [boot] = useState(loadStore);
  const [doc, setDoc] = useState<BuilderDoc>(
    () => boot.drafts.find((d) => d.id === boot.currentId)?.doc ?? defaultFrame(DEFAULT_W, DEFAULT_H),
  );
  const [currentId, setCurrentId] = useState<string>(boot.currentId);
  const [draftMeta, setDraftMeta] = useState(() =>
    boot.drafts.map((d) => ({ id: d.id, name: d.name, updated: d.updated, mapBase: d.mapBase ?? null })),
  );
  const [past, setPast] = useState<BuilderDoc[]>([]);
  const [future, setFuture] = useState<BuilderDoc[]>([]);
  const [tool, setTool] = useState<Tool>('select');
  const [selection, setSelection] = useState<Selection>(null);
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 });
  const [sizeDraft, setSizeDraft] = useState({ w: String(doc.width), h: String(doc.height) });
  const [status, setStatus] = useState<string | null>(null);
  const [cursor, setCursor] = useState<Pt | null>(null);
  const [pencilPts, setPencilPts] = useState<Pt[] | null>(null);
  const [svgModal, setSvgModal] = useState<{ svg: string; warnings: string[] } | null>(null);
  const [jsonModal, setJsonModal] = useState<{ mode: 'export' | 'import'; text: string; error: string | null } | null>(null);
  const [confirmBox, setConfirmBox] = useState<{ title: string; message: string; onConfirm: () => void } | null>(
    null,
  );
  const [labelDialog, setLabelDialog] = useState<{
    x: number;
    y: number;
    tid: string;
    name: string;
    tidTouched: boolean;
    existing: string | null;
    error: string | null;
  } | null>(null);
  const [converting, setConverting] = useState(false);
  const [savedMaps, setSavedMaps] = useState<SavedMap[]>([]);
  const [knownMaps, setKnownMaps] = useState<string[]>([]);
  const [bringIn, setBringIn] = useState('');
  const [tolerance, setTolerance] = useState('4');
  const [saveBox, setSaveBox] = useState<{
    mode: 'save' | 'copy';
    sourceId: string | null;
    id: string;
    name: string;
    error: string | null;
  } | null>(null);
  const [noteBox, setNoteBox] = useState<{ title: string; lines: string[] } | null>(null);
  const svgFileRef = useRef<HTMLInputElement | null>(null);
  // Id to offer in the Save dialog after converting a built-in map that is not saved yet.
  const importHintRef = useRef<string | null>(null);

  const storeRef = useRef<DraftStore>(boot);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const docRef = useRef(doc);
  const pastRef = useRef(past);
  const futureRef = useRef(future);
  const viewRef = useRef(view);
  const toolRef = useRef(tool);
  const penRef = useRef<{ joints: string[]; snapshot: BuilderDoc | null }>({ joints: [], snapshot: null });
  const pencilRef = useRef<{ snapshot: BuilderDoc | null } | null>(null);
  const panRef = useRef<{ x: number; y: number; vx: number; vy: number; id: number } | null>(null);
  const jointDragRef = useRef<{ id: string; snapshot: BuilderDoc; moved: boolean } | null>(null);
  const downRef = useRef<{ x: number; y: number; id: number } | null>(null);

  useEffect(() => {
    docRef.current = doc;
  }, [doc]);
  useEffect(() => {
    pastRef.current = past;
  }, [past]);
  useEffect(() => {
    futureRef.current = future;
  }, [future]);
  useEffect(() => {
    viewRef.current = view;
  }, [view]);
  useEffect(() => {
    toolRef.current = tool;
  }, [tool]);

  // Autosave the open draft (debounced), then refresh sidebar timestamps.
  useEffect(() => {
    const t = window.setTimeout(() => {
      const s = storeRef.current;
      const cur = s.drafts.find((d) => d.id === currentId);
      let changed = false;
      if (cur && cur.doc !== doc) {
        cur.doc = doc;
        cur.updated = Date.now();
        changed = true;
      }
      saveStore(s);
      if (changed) {
        setDraftMeta(s.drafts.map((d) => ({ id: d.id, name: d.name, updated: d.updated, mapBase: d.mapBase ?? null })));
      }
    }, 400);
    return () => window.clearTimeout(t);
  }, [currentId, doc]);

  // Keep the size inputs in sync when the doc changes from import/clear/undo/draft switch.
  useEffect(() => {
    setSizeDraft({ w: String(doc.width), h: String(doc.height) });
  }, [doc.width, doc.height, currentId]);

  const fitView = useCallback((which?: BuilderDoc) => {
    const el = wrapRef.current;
    if (!el) return;
    const d = which ?? docRef.current;
    const rect = el.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const scale = Math.min(rect.width / d.width, rect.height / d.height) * 0.95;
    const next = {
      x: (rect.width - d.width * scale) / 2,
      y: (rect.height - d.height * scale) / 2,
      scale,
    };
    viewRef.current = next;
    setView(next);
  }, []);

  // Fit on open.
  useEffect(() => {
    const t = window.setTimeout(() => fitView(), 0);
    return () => window.clearTimeout(t);
  }, [fitView]);

  // Non-passive wheel: plain wheel pans, ctrl+wheel zooms toward the cursor.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const v = viewRef.current;
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
        const next = { ...v, x: v.x - e.deltaX, y: v.y - e.deltaY };
        viewRef.current = next;
        setView(next);
        return;
      }
      // Delta-based zoom: one mouse notch (~100 units) ≈ ×1.1, but trackpad
      // pinch storms (many tiny deltas) no longer compound into hypersensitivity.
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
      const factor = clamp(Math.exp(-dy * 0.001), 0.4, 2.5);
      const ns = clamp(v.scale * factor, 0.02, 8);
      const wx = (mx - v.x) / v.scale;
      const wy = (my - v.y) / v.scale;
      const next = { x: mx - wx * ns, y: my - wy * ns, scale: ns };
      viewRef.current = next;
      setView(next);
    };
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
  }, []);

  const mutate = useCallback((fn: (d: BuilderDoc) => void) => {
    const next = cloneDoc(docRef.current);
    fn(next);
    docRef.current = next;
    setDoc(next);
    return next;
  }, []);

  const pushHistory = useCallback((snapshot: BuilderDoc) => {
    setPast((p) => [...p.slice(-(MAX_HISTORY - 1)), snapshot]);
    setFuture([]);
  }, []);

  const undo = useCallback(() => {
    if (penRef.current.snapshot) {
      // First undo during a pen stroke just cancels the stroke.
      setDoc(penRef.current.snapshot);
      docRef.current = penRef.current.snapshot;
      penRef.current = { joints: [], snapshot: null };
      return;
    }
    const p = pastRef.current;
    if (!p.length) return;
    const prev = p[p.length - 1];
    const cur = docRef.current;
    setFuture((f) => [cur, ...f].slice(0, MAX_HISTORY));
    setPast((pp) => pp.slice(0, -1));
    docRef.current = prev;
    setDoc(prev);
    setSelection(null);
    setStatus(null);
  }, []);

  const redo = useCallback(() => {
    if (penRef.current.snapshot) {
      // Cancel an in-progress stroke first so partial geometry never lands in history.
      const snap = penRef.current.snapshot;
      docRef.current = snap;
      setDoc(snap);
      penRef.current = { joints: [], snapshot: null };
    }
    const f = futureRef.current;
    if (!f.length) return;
    const next = f[0];
    const cur = docRef.current;
    setPast((p) => [...p, cur].slice(-MAX_HISTORY));
    setFuture((ff) => ff.slice(1));
    docRef.current = next;
    setDoc(next);
    setSelection(null);
    setStatus(null);
  }, []);

  const regions = useMemo(() => interiorRings(doc).length, [doc]);
  const labelled = Object.keys(doc.labels).length;

  // --- pointer helpers --------------------------------------------------------

  const screenToWorld = useCallback((clientX: number, clientY: number): Pt | null => {
    const el = wrapRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const v = viewRef.current;
    return { x: (clientX - rect.left - v.x) / v.scale, y: (clientY - rect.top - v.y) / v.scale };
  }, []);

  const hitJoint = useCallback((w: Pt): string | null => {
    const v = viewRef.current;
    const r = (JOINT_R_PX + 3) / v.scale;
    let best: string | null = null;
    let bestD = r;
    for (const [jid, j] of Object.entries(docRef.current.joints)) {
      const d = Math.hypot(j.x - w.x, j.y - w.y);
      if (d < bestD) {
        bestD = d;
        best = jid;
      }
    }
    return best;
  }, []);

  const hitBorder = useCallback((w: Pt): string | null => {
    const v = viewRef.current;
    const r = HIT_PX / v.scale;
    let best: string | null = null;
    let bestD = r;
    for (const [bid, b] of Object.entries(docRef.current.borders)) {
      const A = docRef.current.joints[b.a];
      const B = docRef.current.joints[b.b];
      if (!A || !B) continue;
      const d = distToSegment(w, A, B);
      if (d < bestD) {
        bestD = d;
        best = bid;
      }
    }
    return best;
  }, []);

  const clearStroke = useCallback(() => {
    penRef.current = { joints: [], snapshot: null };
    pencilRef.current = null;
    setPencilPts(null);
  }, []);

  const cancelStroke = useCallback(() => {
    const snap = penRef.current.snapshot;
    if (snap) {
      docRef.current = snap;
      setDoc(snap);
    }
    clearStroke();
    setStatus(null);
  }, [clearStroke]);

  const finishPen = useCallback(() => {
    const stroke = penRef.current;
    if (!stroke.snapshot) return;
    if (stroke.joints.length >= 2 && stroke.snapshot !== docRef.current) {
      pushHistory(stroke.snapshot);
      setStatus(`Stroke finished (${stroke.joints.length} points)`);
    } else {
      docRef.current = stroke.snapshot;
      setDoc(stroke.snapshot);
      setStatus(null);
    }
    penRef.current = { joints: [], snapshot: null };
  }, [pushHistory]);

  // --- tool actions -----------------------------------------------------------

  const penClick = useCallback(
    (w: Pt) => {
      const v = viewRef.current;
      const stroke = penRef.current;
      if (!stroke.snapshot) {
        const snapshot = docRef.current;
        const d = cloneDoc(snapshot);
        const res = resolvePoint(d, w.x, w.y, SNAP_PX / v.scale, HIT_PX / v.scale);
        penRef.current = { joints: [res.jointId], snapshot };
        docRef.current = d;
        setDoc(d);
        setStatus('Pen: click to extend the stroke · Enter or click the first point to finish · Esc cancels');
        return;
      }
      const d = cloneDoc(docRef.current);
      const res = resolvePoint(d, w.x, w.y, SNAP_PX / v.scale, HIT_PX / v.scale);
      const first = stroke.joints[0];
      if (res.jointId === first && stroke.joints.length >= 2) {
        docRef.current = d; // keep any split that happened under the click
        penRef.current = { ...stroke, joints: stroke.joints };
        setDoc(d);
        finishPen();
        return;
      }
      if (res.jointId !== stroke.joints[stroke.joints.length - 1]) {
        addBorder(d, stroke.joints[stroke.joints.length - 1], res.jointId);
        stroke.joints.push(res.jointId);
        docRef.current = d;
        setDoc(d);
        setStatus(
          `Pen: ${stroke.joints.length} points · Enter or click the first point to finish · Esc cancels`,
        );
        return;
      }
      // Snapped onto the live endpoint: resolvePoint made no change, so skip setDoc.
      setStatus('Pen: that is the point you are already on — click a new spot (or the first point to finish)');
    },
    [finishPen],
  );

  const labelClick = useCallback((w: Pt) => {
    const d = docRef.current;
    const rings = interiorRings(d);
    const idx = findRingIndex(rings, d.joints, w.x, w.y);
    if (idx === null) {
      setStatus('No closed region here — borders must connect into a loop to enclose an area.');
      return;
    }
    let existing: string | null = null;
    for (const [tid, lab] of Object.entries(d.labels)) {
      if (findRingIndex(rings, d.joints, lab.x, lab.y) === idx) {
        existing = tid;
        break;
      }
    }
    if (existing) {
      const lab = d.labels[existing];
      setLabelDialog({
        x: lab.x,
        y: lab.y,
        tid: existing,
        name: lab.display_name,
        tidTouched: true,
        existing,
        error: null,
      });
    } else {
      setLabelDialog({ x: w.x, y: w.y, tid: '', name: '', tidTouched: false, existing: null, error: null });
    }
  }, []);

  const deleteSelection = useCallback(() => {
    const sel = selection;
    if (!sel) return;
    const snapshot = docRef.current;
    mutate((d) => {
      if (sel.kind === 'joint') {
        delete d.joints[sel.id];
        for (const [bid, b] of Object.entries(d.borders)) {
          if (b.a === sel.id || b.b === sel.id) delete d.borders[bid];
        }
      } else if (sel.kind === 'border') {
        delete d.borders[sel.id];
      } else {
        delete d.labels[sel.id];
      }
    });
    pushHistory(snapshot);
    setSelection(null);
    setStatus(sel.kind === 'label' ? 'Label removed' : 'Deleted');
  }, [mutate, pushHistory, selection]);

  // --- pointer events ---------------------------------------------------------

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button === 1) {
      panRef.current = { x: e.clientX, y: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y, id: e.pointerId };
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }
    if (e.button !== 0) return;
    const w = screenToWorld(e.clientX, e.clientY);
    if (!w) return;
    downRef.current = { x: e.clientX, y: e.clientY, id: e.pointerId };
    const t = toolRef.current;

    if (t === 'pencil') {
      const snapshot = docRef.current;
      pencilRef.current = { snapshot };
      setPencilPts([w]);
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }

    if (t === 'select') {
      const jid = hitJoint(w);
      if (jid) {
        setSelection({ kind: 'joint', id: jid });
        jointDragRef.current = { id: jid, snapshot: docRef.current, moved: false };
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch {
          /* ignore */
        }
        return;
      }
      const bid = hitBorder(w);
      if (bid) {
        setSelection({ kind: 'border', id: bid });
        return;
      }
      setSelection(null);
      panRef.current = { x: e.clientX, y: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y, id: e.pointerId };
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }

    // pen / label act on pointer-up (click detection) — capture so the
    // click still lands when the pointer is released slightly outside.
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    // pen / label: a drag past the click threshold becomes a pan instead of a silent no-op.
    const downForPan = downRef.current;
    if (
      !panRef.current &&
      downForPan &&
      e.pointerId === downForPan.id &&
      (toolRef.current === 'pen' || toolRef.current === 'label') &&
      Math.hypot(e.clientX - downForPan.x, e.clientY - downForPan.y) > CLICK_PX
    ) {
      panRef.current = {
        x: e.clientX,
        y: e.clientY,
        vx: viewRef.current.x,
        vy: viewRef.current.y,
        id: e.pointerId,
      };
    }

    const pan = panRef.current;
    if (pan && e.pointerId === pan.id) {
      const next = {
        ...viewRef.current,
        x: pan.vx + (e.clientX - pan.x),
        y: pan.vy + (e.clientY - pan.y),
      };
      viewRef.current = next;
      setView(next);
      return;
    }

    const jd = jointDragRef.current;
    if (jd && e.pointerId === (downRef.current?.id ?? -1)) {
      const w = screenToWorld(e.clientX, e.clientY);
      if (!w) return;
      if (!jd.moved) {
        const d0 = docRef.current.joints[jd.id];
        if (d0 && Math.hypot(d0.x - w.x, d0.y - w.y) < 1 / viewRef.current.scale) return;
        jd.moved = true;
      }
      mutate((d) => {
        const j = d.joints[jd.id];
        if (!j) return;
        j.x = clamp(w.x, 0, d.width);
        j.y = clamp(w.y, 0, d.height);
      });
      return;
    }

    const w = screenToWorld(e.clientX, e.clientY);
    setCursor(w);

    if (pencilRef.current && pencilPts) {
      if (!w) return;
      const last = pencilPts[pencilPts.length - 1];
      const minWorld = PENCIL_MIN_PX / viewRef.current.scale;
      if (!last || Math.hypot(w.x - last.x, w.y - last.y) >= minWorld) {
        setPencilPts([...pencilPts, w]);
      }
    }
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (pan && e.pointerId === pan.id) {
      panRef.current = null;
      downRef.current = null;
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      return;
    }

    const jd = jointDragRef.current;
    if (jd) {
      jointDragRef.current = null;
      if (jd.moved) pushHistory(jd.snapshot);
      downRef.current = null;
      return;
    }

    if (pencilRef.current) {
      const snapshot = pencilRef.current.snapshot;
      pencilRef.current = null;
      const pts = pencilPts ?? [];
      setPencilPts(null);
      downRef.current = null;
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      if (!snapshot || pts.length < 2) return;
      const v = viewRef.current;
      const simple = rdp(pts, RDP_EPS_PX / v.scale);
      if (simple.length < 2) return;
      const d = cloneDoc(docRef.current);
      const ids: string[] = [];
      for (const p of simple) {
        const res = resolvePoint(d, p.x, p.y, SNAP_PX / v.scale, HIT_PX / v.scale);
        if (!ids.length || ids[ids.length - 1] !== res.jointId) ids.push(res.jointId);
      }
      if (ids.length < 2) return;
      for (let i = 1; i < ids.length; i += 1) addBorder(d, ids[i - 1], ids[i]);
      docRef.current = d;
      setDoc(d);
      pushHistory(snapshot);
      setStatus(`Pencil stroke: ${ids.length} points, ${ids.length - 1} borders`);
      return;
    }

    // Click actions (pen / label): require a left-click, not a drag.
    const down = downRef.current;
    downRef.current = null;
    if (!down || e.pointerId !== down.id || e.button !== 0) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > CLICK_PX) return;
    const w = screenToWorld(e.clientX, e.clientY);
    if (!w) return;
    const t = toolRef.current;
    if (t === 'pen') penClick(w);
    else if (t === 'label') labelClick(w);
  };

  const onPointerCancel = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (panRef.current?.id === e.pointerId) panRef.current = null;
    if (jointDragRef.current) {
      const jd = jointDragRef.current;
      jointDragRef.current = null;
      if (jd.moved) pushHistory(jd.snapshot);
    }
    // An interrupted pencil drag is discarded (nothing is committed until pointer-up).
    if (pencilRef.current) {
      pencilRef.current = null;
      setPencilPts(null);
    }
    downRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };

  // --- keyboard ---------------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
      ) {
        return;
      }
      if (labelDialog || jsonModal || svgModal || confirmBox) {
        if (e.key === 'Escape') {
          setLabelDialog(null);
          setJsonModal(null);
          setSvgModal(null);
          setConfirmBox(null);
        }
        return;
      }
      const key = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && key === 'z') {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && key === 'y') {
        e.preventDefault();
        redo();
        return;
      }
      if (e.key === 'Escape') {
        cancelStroke();
        setSelection(null);
        return;
      }
      if (e.key === 'Enter') {
        if (penRef.current.snapshot) {
          e.preventDefault();
          finishPen();
        }
        return;
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (selection) {
          e.preventDefault();
          deleteSelection();
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cancelStroke, confirmBox, deleteSelection, finishPen, jsonModal, labelDialog, redo, selection, svgModal, undo]);

  // --- header actions ---------------------------------------------------------

  const applySize = () => {
    const w = Number(sizeDraft.w);
    const h = Number(sizeDraft.h);
    if (!Number.isFinite(w) || !Number.isFinite(h) || w < MIN_DIM || h < MIN_DIM || w > MAX_DIM || h > MAX_DIM) {
      setStatus(`Size must be ${MIN_DIM}–${MAX_DIM} px`);
      return;
    }
    if (w === docRef.current.width && h === docRef.current.height) return;
    const snapshot = docRef.current;
    mutate((d) => {
      d.width = w;
      d.height = h;
      const corners: [string, number, number][] = [
        ['f_tl', 0, 0],
        ['f_tr', w, 0],
        ['f_br', w, h],
        ['f_bl', 0, h],
      ];
      for (const [jid, x, y] of corners) d.joints[jid] = { x, y };
      const frame: [string, string, string][] = [
        ['fb_t', 'f_tl', 'f_tr'],
        ['fb_r', 'f_tr', 'f_br'],
        ['fb_b', 'f_br', 'f_bl'],
        ['fb_l', 'f_bl', 'f_tl'],
      ];
      for (const [bid, a, b] of frame) if (!d.borders[bid]) d.borders[bid] = { a, b };
      for (const j of Object.values(d.joints)) {
        j.x = clamp(j.x, 0, w);
        j.y = clamp(j.y, 0, h);
      }
      for (const l of Object.values(d.labels)) {
        l.x = clamp(l.x, 0, w);
        l.y = clamp(l.y, 0, h);
      }
    });
    pushHistory(snapshot);
    setStatus(`Map size set to ${w}×${h} (outer frame ensured)`);
    window.setTimeout(() => fitView(), 0);
  };

  const clearAll = () => {
    const snapshot = docRef.current;
    const fresh = defaultFrame(snapshot.width, snapshot.height);
    docRef.current = fresh;
    setDoc(fresh);
    pushHistory(snapshot);
    setSelection(null);
    clearStroke();
    setStatus('Cleared — fresh outer frame only');
    window.setTimeout(() => fitView(fresh), 0);
  };

  const openExport = () => {
    setJsonModal({ mode: 'export', text: JSON.stringify(docRef.current, null, 2), error: null });
  };

  const applyImport = (text: string) => {
    try {
      const raw = JSON.parse(text) as BuilderDoc;
      if (typeof raw?.width !== 'number' || typeof raw?.height !== 'number') throw new Error('width/height missing');
      if (raw.width < MIN_DIM || raw.width > MAX_DIM || raw.height < MIN_DIM || raw.height > MAX_DIM) {
        throw new Error(`size must be ${MIN_DIM}–${MAX_DIM} px`);
      }
      if (!raw.joints || typeof raw.joints !== 'object') throw new Error('joints missing');
      const next: BuilderDoc = {
        width: raw.width,
        height: raw.height,
        joints: {},
        borders: (raw.borders ?? {}) as Record<string, Border>,
        labels: (raw.labels ?? {}) as Record<string, TerritoryLabel>,
      };
      for (const [jid, j] of Object.entries(raw.joints as Record<string, Joint>)) {
        if (typeof j?.x !== 'number' || typeof j?.y !== 'number' || !Number.isFinite(j.x) || !Number.isFinite(j.y)) {
          throw new Error(`joint ${jid} is invalid`);
        }
        next.joints[jid] = { x: j.x, y: j.y };
      }
      const snapshot = docRef.current;
      docRef.current = next;
      setDoc(next);
      pushHistory(snapshot);
      setJsonModal(null);
      setSelection(null);
      clearStroke();
      setStatus('JSON imported');
      window.setTimeout(() => fitView(next), 0);
    } catch (err) {
      setJsonModal((m) => (m ? { ...m, error: err instanceof Error ? err.message : String(err) } : m));
    }
  };

  const buildSvg = async (source?: { doc: BuilderDoc; name?: string }) => {
    setConverting(true);
    try {
      const r = await api.adminConvertMapBuilder(source?.doc ?? docRef.current);
      setSvgModal({ svg: r.svg, warnings: r.warnings ?? [] });
      setStatus(
        `SVG built${source?.name ? ` from “${source.name}”` : ''}: ` +
          `${r.stats.labelled}/${r.stats.regions} regions labelled · ${r.stats.borders} borders (${r.stats.pruned_borders} dangling pruned)`,
      );
    } catch (err) {
      setStatus(`Convert failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setConverting(false);
    }
  };

  const saveLabel = () => {
    if (!labelDialog) return;
    const name = labelDialog.name.trim();
    const tid = labelDialog.tid.trim();
    if (!name) {
      setLabelDialog({ ...labelDialog, error: 'Display name is required' });
      return;
    }
    if (!TID_RE.test(tid)) {
      setLabelDialog({ ...labelDialog, error: 'Id must be 1–64 chars: letters, digits, _ . -' });
      return;
    }
    if (tid !== labelDialog.existing && tid in docRef.current.labels) {
      setLabelDialog({ ...labelDialog, error: `Id “${tid}” is already used` });
      return;
    }
    const snapshot = docRef.current;
    mutate((d) => {
      if (labelDialog.existing && labelDialog.existing !== tid) delete d.labels[labelDialog.existing];
      d.labels[tid] = { display_name: name, x: labelDialog.x, y: labelDialog.y };
    });
    pushHistory(snapshot);
    setSelection({ kind: 'label', id: tid });
    setLabelDialog(null);
    setStatus(labelDialog.existing ? `Label updated: ${tid}` : `Region labelled: ${tid}`);
  };

  const setToolAndCancel = (t: Tool) => {
    if (penRef.current.snapshot) cancelStroke();
    if (pencilRef.current) {
      pencilRef.current = null;
      setPencilPts(null);
    }
    setTool(t);
    setStatus(null);
  };

  // --- drafts -----------------------------------------------------------------

  const refreshMeta = () =>
    setDraftMeta(
      storeRef.current.drafts.map((d) => ({ id: d.id, name: d.name, updated: d.updated, mapBase: d.mapBase ?? null })),
    );

  const reloadLibrary = useCallback(() => {
    api
      .adminListMapBuilder()
      .then((r) => setSavedMaps(Array.isArray(r.maps) ? r.maps : []))
      .catch(() => setSavedMaps([]));
    api
      .adminListMaps()
      .then((r) => {
        const list = Array.isArray(r.maps) ? r.maps.filter((m) => typeof m === 'string' && m) : [];
        setKnownMaps(list);
        setBringIn((prev) => prev || list[0] || '');
      })
      .catch(() => setKnownMaps([]));
  }, []);

  useEffect(() => {
    reloadLibrary();
  }, [reloadLibrary]);

  const notifyMapsChanged = () => {
    reloadLibrary();
    window.dispatchEvent(new Event('admin-maps-changed'));
  };

  const loadDoc = (next: BuilderDoc, name: string, mapBase: string | null, message: string) => {
    const snapshot = docRef.current;
    docRef.current = next;
    setDoc(next);
    pushHistory(snapshot);
    const s = storeRef.current;
    const cur = s.drafts.find((d) => d.id === s.currentId);
    if (cur) {
      cur.doc = next;
      cur.name = name;
      cur.mapBase = mapBase;
      cur.updated = Date.now();
      saveStore(s);
      refreshMeta();
    }
    importHintRef.current = mapBase;
    setSelection(null);
    clearStroke();
    setStatus(message);
    window.setTimeout(() => fitView(next), 0);
  };

  const importSvgText = async (svg: string, label: string) => {
    const tol = Number(tolerance);
    if (!Number.isFinite(tol) || tol < 0 || tol > 40) {
      setStatus('Snap tolerance must be between 0 and 40 px');
      return;
    }
    setConverting(true);
    try {
      const r = await api.adminImportMapSvg({ svg, tolerance: tol });
      loadDoc(
        r.doc,
        label,
        null,
        `Imported ${r.stats.territories} territories · ${r.stats.shared_borders} shared borders. Save it to use the map.`,
      );
      if (MAP_ID_RE.test(label)) importHintRef.current = label;
      if (r.warnings?.length) setNoteBox({ title: 'Import notes', lines: r.warnings });
    } catch (err) {
      setStatus(`Import failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setConverting(false);
    }
  };

  const onSvgFile = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : '';
      void importSvgText(text, file.name.replace(/\.svg$/i, '') || 'Imported map');
    };
    reader.onerror = () => setStatus('Could not read that SVG file');
    reader.readAsText(file);
  };

  const openKnownMap = async (mapBase: string) => {
    if (!mapBase) return;
    setConverting(true);
    try {
      try {
        const saved = await api.adminGetMapBuilder(mapBase);
        loadDoc(saved.doc, saved.display_name || mapBase, saved.map_base, `Opened saved map “${saved.display_name}”`);
        return;
      } catch (err) {
        const message = err instanceof Error ? err.message : '';
        if (!/no saved map/i.test(message)) throw err;
      }
      const svg = await fetchMapSvgText(mapBase);
      const tol = Number(tolerance);
      const r = await api.adminImportMapSvg({ svg, tolerance: Number.isFinite(tol) ? tol : 4 });
      loadDoc(
        r.doc,
        mapBase,
        null,
        `Converted built-in “${mapBase}” into shared borders (${r.stats.shared_borders} shared). Save as a new id, or save over “${mapBase}” to replace the built-in file.`,
      );
      importHintRef.current = mapBase;
      if (r.warnings?.length) setNoteBox({ title: `Imported ${mapBase}`, lines: r.warnings });
    } catch (err) {
      setStatus(`Could not open ${mapBase}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setConverting(false);
    }
  };

  const currentMapBase = () => storeRef.current.drafts.find((d) => d.id === storeRef.current.currentId)?.mapBase ?? null;

  const openSaveBox = (mode: 'save' | 'copy', sourceId: string | null) => {
    const cur = storeRef.current.drafts.find((d) => d.id === storeRef.current.currentId);
    const id = mode === 'copy' ? '' : sourceId || cur?.mapBase || importHintRef.current || '';
    const name = mode === 'copy' ? '' : cur?.name || '';
    setSaveBox({ mode, sourceId, id, name, error: null });
  };

  const commitSave = async () => {
    if (!saveBox) return;
    const id = saveBox.id.trim();
    const name = saveBox.name.trim();
    if (!MAP_ID_RE.test(id)) {
      setSaveBox({ ...saveBox, error: 'Id must be 1–64 chars: letters, digits, _ . -' });
      return;
    }
    if (!name) {
      setSaveBox({ ...saveBox, error: 'Display name is required' });
      return;
    }
    const exists = savedMaps.some((m) => m.map_base === id);
    if (exists && saveBox.mode === 'copy') {
      setSaveBox({ ...saveBox, error: `“${id}” already exists` });
      return;
    }
    setConverting(true);
    try {
      if (saveBox.mode === 'copy' && saveBox.sourceId) {
        await api.adminCopyMapBuilder(saveBox.sourceId, { map_base: id, display_name: name });
        setStatus(`Copied to “${name}” (${id})`);
      } else {
        flushDraft();
        await api.adminSaveMapBuilder(id, { display_name: name, doc: docRef.current });
        const s = storeRef.current;
        const cur = s.drafts.find((d) => d.id === s.currentId);
        if (cur) {
          cur.mapBase = id;
          cur.name = name;
          cur.updated = Date.now();
          saveStore(s);
          refreshMeta();
        }
        setStatus(
          knownMaps.includes(id) && !savedMaps.some((m) => m.map_base === id)
            ? `Saved “${name}”. It now replaces the built-in ${id} SVG.`
            : `Saved “${name}” (${id})`,
        );
      }
      setSaveBox(null);
      notifyMapsChanged();
    } catch (err) {
      setSaveBox((box) => (box ? { ...box, error: err instanceof Error ? err.message : String(err) } : box));
    } finally {
      setConverting(false);
    }
  };

  const deleteSaved = (mapBase: string, name: string) => {
    setConfirmBox({
      title: 'Delete saved map',
      message: knownMaps.includes(mapBase)
        ? `Delete the saved drawing “${name}” (${mapBase})? The built-in file of that id, if there is one, is what the game will use again.`
        : `Delete “${name}” (${mapBase})? Setups that name this map will no longer have an SVG.`,
      onConfirm: () => {
        void api
          .adminDeleteMapBuilder(mapBase)
          .then(() => {
            const s = storeRef.current;
            for (const d of s.drafts) {
              if (d.mapBase === mapBase) d.mapBase = null;
            }
            saveStore(s);
            refreshMeta();
            setStatus(`Deleted saved map ${mapBase}`);
            notifyMapsChanged();
          })
          .catch((err) => setStatus(`Delete failed: ${err instanceof Error ? err.message : String(err)}`));
      },
    });
  };

  // Write the live doc into its draft immediately (bypasses the debounce).
  const flushDraft = () => {
    const s = storeRef.current;
    const cur = s.drafts.find((d) => d.id === s.currentId);
    if (cur && cur.doc !== docRef.current) {
      cur.doc = docRef.current;
      cur.updated = Date.now();
    }
    saveStore(s);
  };

  const switchDraft = (id: string) => {
    if (id === currentId) return;
    cancelStroke(); // revert any in-progress pen/pencil stroke before saving
    flushDraft();
    const s = storeRef.current;
    const target = s.drafts.find((d) => d.id === id);
    if (!target) return;
    s.currentId = id;
    saveStore(s);
    setCurrentId(id);
    docRef.current = target.doc;
    setDoc(target.doc);
    setPast([]);
    setFuture([]);
    setSelection(null);
    setStatus(`Switched to “${target.name}”`);
    refreshMeta();
    window.setTimeout(() => fitView(target.doc), 0);
  };

  const newDraft = () => {
    cancelStroke(); // revert any in-progress pen/pencil stroke before saving
    flushDraft();
    const s = storeRef.current;
    let n = s.drafts.length + 1;
    const names = new Set(s.drafts.map((d) => d.name));
    while (names.has(`Draft ${n}`)) n += 1;
    const draft = makeDraft(`Draft ${n}`, defaultFrame(DEFAULT_W, DEFAULT_H));
    s.drafts.push(draft);
    s.currentId = draft.id;
    saveStore(s);
    setCurrentId(draft.id);
    docRef.current = draft.doc;
    setDoc(draft.doc);
    setPast([]);
    setFuture([]);
    setSelection(null);
    setStatus(`Created “${draft.name}”`);
    refreshMeta();
    window.setTimeout(() => fitView(draft.doc), 0);
  };

  const renameDraft = (id: string) => {
    flushDraft();
    const s = storeRef.current;
    const d = s.drafts.find((x) => x.id === id);
    if (!d) return;
    const raw = window.prompt('Draft name:', d.name);
    if (raw === null) return;
    const name = raw.trim();
    if (!name) return;
    d.name = name;
    d.updated = Date.now();
    saveStore(s);
    refreshMeta();
    setStatus(`Renamed to “${name}”`);
  };

  const deleteDraft = (id: string) => {
    const s = storeRef.current;
    if (s.drafts.length <= 1) {
      setStatus('Cannot delete the only draft — use Clear instead');
      return;
    }
    const d = s.drafts.find((x) => x.id === id);
    if (!d) return;
    setConfirmBox({
      title: 'Delete draft',
      message: `Delete “${d.name}”? This cannot be undone.`,
      onConfirm: () => {
        cancelStroke(); // revert any in-progress pen/pencil stroke before saving
        flushDraft();
        const st = storeRef.current;
        const idx = st.drafts.findIndex((x) => x.id === id);
        if (idx < 0) return;
        const wasCurrent = st.currentId === id;
        st.drafts.splice(idx, 1);
        if (wasCurrent) {
          const next = st.drafts[Math.min(idx, st.drafts.length - 1)];
          st.currentId = next.id;
          docRef.current = next.doc;
          setDoc(next.doc);
          setCurrentId(next.id);
          setPast([]);
          setFuture([]);
          setSelection(null);
          window.setTimeout(() => fitView(next.doc), 0);
        }
        saveStore(st);
        refreshMeta();
        setStatus(`Deleted “${d.name}”`);
      },
    });
  };

  const buildSvgForDraft = (id: string) => {
    const s = storeRef.current;
    const d = s.drafts.find((x) => x.id === id);
    if (!d) return;
    // Build from the live doc when it is the open draft (unsaved edits included).
    void buildSvg(id === currentId ? { doc: docRef.current, name: d.name } : { doc: d.doc, name: d.name });
  };

  // --- render -----------------------------------------------------------------

  const k = 1 / view.scale;
  const borderList = Object.entries(doc.borders);
  const jointList = Object.entries(doc.joints);
  const penJoints = penRef.current.joints;

  return (
    <div className="mapbuilder">
      <aside className="mapbuilder__drafts" aria-label="Maps and drafts">
        <div className="mapbuilder__drafts-head">
          <span className="mapbuilder__drafts-title">Bring in</span>
        </div>
        <label className="mapbuilder__field admin-form__label">
          Map
          <select
            className="admin-page__select"
            aria-label="Existing map to open"
            value={bringIn}
            onChange={(e) => setBringIn(e.target.value)}
          >
            {knownMaps.length === 0 ? <option value="">No maps</option> : null}
            {knownMaps.map((id) => (
              <option key={id} value={id}>
                {id}
                {savedMaps.some((m) => m.map_base === id) ? ' (saved)' : ''}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="admin-page__btn"
          disabled={!bringIn || converting}
          onClick={() => void openKnownMap(bringIn)}
        >
          Open
        </button>
        <p className="admin-form__micro mapbuilder__drafts-note">
          A saved drawing opens as shared borders. A built-in map is converted from its SVG. Snap tolerance is on the toolbar.
        </p>

        <div className="mapbuilder__drafts-head">
          <span className="mapbuilder__drafts-title">Saved maps</span>
        </div>
        {savedMaps.length === 0 ? (
          <p className="admin-form__micro mapbuilder__drafts-note">None yet. Import or draw, then Save.</p>
        ) : (
          <ul className="mapbuilder__draft-list">
            {savedMaps.map((m) => (
              <li key={m.map_base} className="mapbuilder__draft">
                <button
                  type="button"
                  className="mapbuilder__draft-open"
                  onClick={() => void openKnownMap(m.map_base)}
                  title={`Open ${m.display_name}`}
                >
                  <span className="mapbuilder__draft-name">{m.display_name}</span>
                  <span className="mapbuilder__draft-time">
                    {m.map_base} · {m.territories} territories
                  </span>
                </button>
                <span className="mapbuilder__draft-tools">
                  <button
                    type="button"
                    className="admin-page__btn"
                    onClick={() => openSaveBox('copy', m.map_base)}
                    title={`Copy ${m.display_name} to a new id`}
                  >
                    Copy
                  </button>
                  <button
                    type="button"
                    className="admin-page__btn admin-page__btn--danger"
                    onClick={() => deleteSaved(m.map_base, m.display_name)}
                    title={`Delete ${m.display_name}`}
                  >
                    Del
                  </button>
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="mapbuilder__drafts-head">
          <span className="mapbuilder__drafts-title">Drafts</span>
          <button type="button" className="admin-page__btn" onClick={newDraft} title="Start a new blank draft">
            New
          </button>
        </div>
        <ul className="mapbuilder__draft-list">
          {draftMeta.map((d) => (
            <li
              key={d.id}
              className={`mapbuilder__draft${d.id === currentId ? ' mapbuilder__draft--active' : ''}`}
            >
              <button
                type="button"
                className="mapbuilder__draft-open"
                aria-current={d.id === currentId ? 'true' : undefined}
                onClick={() => switchDraft(d.id)}
                title={`Open ${d.name}`}
              >
                <span className="mapbuilder__draft-name">{d.name}</span>
                <span className="mapbuilder__draft-time">
                  {new Date(d.updated).toLocaleString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              </button>
              <span className="mapbuilder__draft-tools">
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={() => buildSvgForDraft(d.id)}
                  disabled={converting}
                  title={`Build SVG from ${d.name} without opening it`}
                >
                  SVG
                </button>
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={() => renameDraft(d.id)}
                  title={`Rename ${d.name}`}
                >
                  Rename
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  onClick={() => deleteDraft(d.id)}
                  title={`Delete ${d.name}`}
                >
                  Del
                </button>
              </span>
            </li>
          ))}
        </ul>
        <p className="admin-form__micro mapbuilder__drafts-note">
          Drafts stay in this browser. Save writes the map on the server: the database in production, or
          backend/data/maps/&lt;id&gt;/ when the server is running from files. Setups and the icon and landscape
          editors use that SVG ahead of a built-in file with the same id.
        </p>
      </aside>

      <div className="mapbuilder__main">
        <div className="mapbuilder__toolbar" role="toolbar" aria-label="Map builder tools">
          <label className="mapbuilder__size">
            Size
            <input
              className="admin-form__input admin-form__input--narrow"
              type="number"
              min={MIN_DIM}
              max={MAX_DIM}
              value={sizeDraft.w}
              aria-label="Map width in px"
              onChange={(e) => setSizeDraft((s) => ({ ...s, w: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === 'Enter') applySize();
              }}
            />
            ×
            <input
              className="admin-form__input admin-form__input--narrow"
              type="number"
              min={MIN_DIM}
              max={MAX_DIM}
              value={sizeDraft.h}
              aria-label="Map height in px"
              onChange={(e) => setSizeDraft((s) => ({ ...s, h: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === 'Enter') applySize();
              }}
            />
            <button type="button" className="admin-page__btn" onClick={applySize}>
              Apply
            </button>
          </label>

          <div className="admin-mapview__toggle" role="group" aria-label="Drawing tool">
            {(
              [
                ['select', 'Select'],
                ['pen', 'Pen'],
                ['pencil', 'Pencil'],
                ['label', 'Label'],
              ] as const
            ).map(([t, label]) => (
              <button
                key={t}
                type="button"
                className={`admin-mapview__toggle-btn${tool === t ? ' admin-mapview__toggle-btn--active' : ''}`}
                aria-pressed={tool === t}
                onClick={() => setToolAndCancel(t)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="mapbuilder__actions">
            <button type="button" className="admin-page__btn" onClick={undo} disabled={!past.length} title="Ctrl+Z">
              Undo
            </button>
            <button
              type="button"
              className="admin-page__btn"
              onClick={redo}
              disabled={!future.length}
              title="Ctrl+Shift+Z"
            >
              Redo
            </button>
            <button type="button" className="admin-page__btn" onClick={() => fitView()}>
              Fit
            </button>
            <button
              type="button"
              className="admin-page__btn admin-page__btn--danger"
              onClick={() =>
                setConfirmBox({
                  title: 'Clear everything?',
                  message:
                    'This resets the open draft to a bare outer frame — all borders, strokes and labels are removed. Undo (Ctrl+Z) still works afterwards.',
                  onConfirm: clearAll,
                })
              }
            >
              Clear
            </button>
            <label className="mapbuilder__size">
              Snap
              <input
                className="admin-form__input admin-form__input--narrow"
                type="number"
                min={0}
                max={40}
                step={0.5}
                value={tolerance}
                aria-label="Snap tolerance in px"
                title="How far apart a double-drawn border can be and still become one shared border"
                onChange={(e) => setTolerance(e.target.value)}
              />
              px
            </label>
            <input
              ref={svgFileRef}
              type="file"
              accept=".svg,image/svg+xml"
              hidden
              onChange={(e) => {
                onSvgFile(e.target.files?.[0]);
                e.target.value = '';
              }}
            />
            <button
              type="button"
              className="admin-page__btn"
              disabled={converting}
              onClick={() => svgFileRef.current?.click()}
              title="Upload an SVG whose paths are one closed shape per territory"
            >
              Import SVG
            </button>
            <button
              type="button"
              className="admin-page__btn admin-page__btn--primary"
              disabled={converting}
              onClick={() => openSaveBox('save', currentMapBase())}
              title="Save this drawing and its territory SVG under a map id"
            >
              Save
            </button>
            <button
              type="button"
              className="admin-page__btn"
              onClick={openExport}
              title="Download or copy the whole drawing as JSON — back it up or move it to another browser"
            >
              Export JSON
            </button>
            <button
              type="button"
              className="admin-page__btn"
              onClick={() => setJsonModal({ mode: 'import', text: '', error: null })}
              title="Paste exported JSON to replace the current drawing"
            >
              Import JSON
            </button>
            <button
              type="button"
              className="admin-page__btn admin-page__btn--primary"
              onClick={() => void buildSvg()}
              disabled={converting}
              title="Send the drawing to the converter and show the generated SVG source"
            >
              {converting ? 'Building…' : 'Build SVG'}
            </button>
          </div>

          <span className="mapbuilder__stats" aria-live="polite">
            {jointList.length} joints · {borderList.length} borders · {regions} regions · {labelled} labelled
          </span>
        </div>

        <div
          ref={wrapRef}
          className={`mapbuilder__viewport mapbuilder__viewport--${tool}`}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
        >
          <div
            className="mapbuilder__world"
            style={{
              width: doc.width,
              height: doc.height,
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            }}
          >
            <svg
              className="mapbuilder__svg"
              viewBox={`0 0 ${doc.width} ${doc.height}`}
              width={doc.width}
              height={doc.height}
            >
              {/* Borders — each stored once, shared by both neighbouring regions. */}
              {borderList.map(([bid, b]) => {
                const A = doc.joints[b.a];
                const B = doc.joints[b.b];
                if (!A || !B) return null;
                const selectedBorder = selection?.kind === 'border' && selection.id === bid;
                const inPen = penJoints.includes(b.a) && penJoints.includes(b.b);
                return (
                  <line
                    key={bid}
                    x1={A.x}
                    y1={A.y}
                    x2={B.x}
                    y2={B.y}
                    className={`mapbuilder__border${selectedBorder ? ' mapbuilder__border--selected' : ''}${
                      inPen ? ' mapbuilder__border--pen' : ''
                    }`}
                    strokeWidth={selectedBorder || inPen ? 3 * k : 1.5 * k}
                  />
                );
              })}

              {/* Pen preview: last point → cursor. */}
              {tool === 'pen' && penJoints.length > 0 && cursor ? (
                (() => {
                  const last = doc.joints[penJoints[penJoints.length - 1]];
                  if (!last) return null;
                  return (
                    <line
                      className="mapbuilder__preview"
                      x1={last.x}
                      y1={last.y}
                      x2={cursor.x}
                      y2={cursor.y}
                      strokeWidth={1.5 * k}
                    />
                  );
                })()
              ) : null}

              {/* Pencil preview polyline. */}
              {pencilPts && pencilPts.length > 1 ? (
                <polyline
                  className="mapbuilder__preview"
                  points={pencilPts.map((p) => `${p.x},${p.y}`).join(' ')}
                  fill="none"
                  strokeWidth={2 * k}
                />
              ) : null}

              {/* Territory labels (anchor = click point inside the region). */}
              {Object.entries(doc.labels).map(([tid, lab]) => {
                const sel = selection?.kind === 'label' && selection.id === tid;
                return (
                  <g key={tid} className={`mapbuilder__label${sel ? ' mapbuilder__label--selected' : ''}`}>
                    <circle cx={lab.x} cy={lab.y} r={3 * k} />
                    <text x={lab.x + 8 * k} y={lab.y + 4 * k} fontSize={14 * k}>
                      {lab.display_name}
                    </text>
                  </g>
                );
              })}

              {/* Joints */}
              {jointList.map(([jid, j]) => {
                const sel = selection?.kind === 'joint' && selection.id === jid;
                const inPen = penJoints.includes(jid);
                return (
                  <circle
                    key={jid}
                    cx={j.x}
                    cy={j.y}
                    r={(sel || inPen ? JOINT_R_PX + 2 : JOINT_R_PX) * k}
                    className={`mapbuilder__joint${sel ? ' mapbuilder__joint--selected' : ''}${
                      inPen ? ' mapbuilder__joint--pen' : ''
                    }`}
                  />
                );
              })}
            </svg>
          </div>
        </div>

        <div className="mapbuilder__status" role="status">
          <span className="mapbuilder__hint">{status ?? TOOL_HINTS[tool]}</span>
          {selection ? (
            <span className="mapbuilder__sel">
              {selection.kind}: {selection.id} · Delete to remove
            </span>
          ) : null}
        </div>
      </div>

      {confirmBox ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setConfirmBox(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label={confirmBox.title}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">{confirmBox.title}</h3>
            <p className="admin-form__micro">{confirmBox.message}</p>
            <div className="admin-modal__actions">
              <button
                type="button"
                className="admin-page__btn admin-page__btn--danger"
                onClick={() => {
                  const fn = confirmBox.onConfirm;
                  setConfirmBox(null);
                  fn();
                }}
              >
                Yes
              </button>
              <button type="button" className="admin-page__btn" onClick={() => setConfirmBox(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {labelDialog ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setLabelDialog(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label={labelDialog.existing ? 'Edit territory label' : 'Label region'}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">
              {labelDialog.existing ? 'Edit territory' : 'Label region'}
            </h3>
            <label className="mapbuilder__field admin-form__label">
              Display name
              <input
                className="admin-form__input"
                value={labelDialog.name}
                autoFocus
                onChange={(e) => {
                  const name = e.target.value;
                  setLabelDialog((d) =>
                    d
                      ? {
                          ...d,
                          name,
                          tid: d.tidTouched ? d.tid : normalizeTid(name),
                          error: null,
                        }
                      : d,
                  );
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') saveLabel();
                }}
              />
            </label>
            <label className="mapbuilder__field admin-form__label">
              Territory id
              <input
                className="admin-form__input"
                value={labelDialog.tid}
                onChange={(e) =>
                  setLabelDialog((d) => (d ? { ...d, tid: e.target.value, tidTouched: true, error: null } : d))
                }
                onKeyDown={(e) => {
                  if (e.key === 'Enter') saveLabel();
                }}
              />
            </label>
            <p className="admin-form__micro">
              The id is derived from the name (lowercase, spaces → _ , apostrophes stripped) until you edit it.
            </p>
            {labelDialog.error ? <p className="admin-page__error">{labelDialog.error}</p> : null}
            <div className="admin-modal__actions">
              <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={saveLabel}>
                Save
              </button>
              <button type="button" className="admin-page__btn" onClick={() => setLabelDialog(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {saveBox ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setSaveBox(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label={saveBox.mode === 'copy' ? 'Copy map' : 'Save map'}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">{saveBox.mode === 'copy' ? 'Copy map' : 'Save map'}</h3>
            <p className="admin-form__micro">
              {saveBox.mode === 'copy'
                ? 'Writes a new map id with its own name. The drawing is the saved source, not unsaved edits in the open draft.'
                : 'Stores the shared-border drawing and the territory SVG. Using an existing built-in id replaces that map for setups and the icon and landscape editors.'}
            </p>
            <label className="mapbuilder__field admin-form__label">
              Map id
              <input
                className="admin-form__input"
                value={saveBox.id}
                autoFocus
                placeholder="e.g. wotr_map_1.3"
                onChange={(e) => setSaveBox((b) => (b ? { ...b, id: e.target.value, error: null } : b))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void commitSave();
                }}
              />
            </label>
            <label className="mapbuilder__field admin-form__label">
              Display name
              <input
                className="admin-form__input"
                value={saveBox.name}
                onChange={(e) => setSaveBox((b) => (b ? { ...b, name: e.target.value, error: null } : b))}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void commitSave();
                }}
              />
            </label>
            {saveBox.error ? <p className="admin-page__error">{saveBox.error}</p> : null}
            <div className="admin-modal__actions">
              <button
                type="button"
                className="admin-page__btn admin-page__btn--primary"
                disabled={converting}
                onClick={() => void commitSave()}
              >
                {saveBox.mode === 'copy' ? 'Copy' : 'Save'}
              </button>
              <button type="button" className="admin-page__btn" onClick={() => setSaveBox(null)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {noteBox ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setNoteBox(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label={noteBox.title}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">{noteBox.title}</h3>
            <ul className="mapbuilder__warnings">
              {noteBox.lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <div className="admin-modal__actions">
              <button type="button" className="admin-page__btn" onClick={() => setNoteBox(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {jsonModal ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setJsonModal(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label={jsonModal.mode === 'export' ? 'Export builder JSON' : 'Import builder JSON'}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">
              {jsonModal.mode === 'export' ? 'Export JSON' : 'Import JSON'}
            </h3>
            <p className="admin-form__micro">
              {jsonModal.mode === 'export'
                ? 'Saves the whole drawing (size, joints, borders, labels) as text. Saved maps on the server are separate — use Save for those.'
                : 'Replaces the open draft with a previously exported JSON drawing. The current drawing stays in undo history (Ctrl+Z) if you change your mind.'}
            </p>
            <textarea
              className="mapbuilder__textarea admin-form__textarea admin-form__textarea--json"
              value={jsonModal.text}
              readOnly={jsonModal.mode === 'export'}
              spellCheck={false}
              onChange={(e) => setJsonModal((m) => (m ? { ...m, text: e.target.value, error: null } : m))}
            />
            {jsonModal.error ? <p className="admin-page__error">{jsonModal.error}</p> : null}
            <div className="admin-modal__actions">
              {jsonModal.mode === 'export' ? (
                <>
                  <button
                    type="button"
                    className="admin-page__btn admin-page__btn--primary"
                    onClick={() => {
                      void navigator.clipboard?.writeText(jsonModal.text);
                      setStatus('JSON copied to clipboard');
                    }}
                  >
                    Copy
                  </button>
                  <button
                    type="button"
                    className="admin-page__btn"
                    onClick={() => downloadText('map_builder.json', jsonModal.text, 'application/json')}
                  >
                    Download
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--primary"
                  onClick={() => applyImport(jsonModal.text)}
                >
                  Apply
                </button>
              )}
              <button type="button" className="admin-page__btn" onClick={() => setJsonModal(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {svgModal ? (
        <div className="admin-modal-overlay" role="presentation" onClick={() => setSvgModal(null)}>
          <div
            className="admin-modal admin-modal--small"
            role="dialog"
            aria-modal="true"
            aria-label="Generated SVG"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="admin-modal__title">Generated SVG</h3>
            {svgModal.warnings.length ? (
              <ul className="mapbuilder__warnings">
                {svgModal.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : (
              <p className="admin-form__micro">All regions converted cleanly.</p>
            )}
            <textarea className="mapbuilder__textarea admin-form__textarea admin-form__textarea--json" readOnly spellCheck={false} value={svgModal.svg} />
            <div className="admin-modal__actions">
              <button
                type="button"
                className="admin-page__btn admin-page__btn--primary"
                onClick={() => {
                  void navigator.clipboard?.writeText(svgModal.svg);
                  setStatus('SVG copied to clipboard');
                }}
              >
                Copy
              </button>
              <button
                type="button"
                className="admin-page__btn"
                onClick={() => downloadText('map_builder.svg', svgModal.svg, 'image/svg+xml')}
              >
                Download
              </button>
              <button type="button" className="admin-page__btn" onClick={() => setSvgModal(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default MapBuilder;
