import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import GameMap, { DEFAULT_MAP_BASE } from '../../components/GameMap';
import { TERRITORY_UNIT_PIXEL_NUDGE } from '../../components/mapSpotNudge';
import { api } from '../../services/api';
import type { GameState, MapOverridePoint, MapOverridesData, MapSpotKind } from '../../types/game';
import { normalizeMapOverrides } from '../../types/game';
import { loadTerritorySvgPaths, sameTerritoryId, type TerritoryPathData } from './territoryGraph';

type Pt = { x: number; y: number };
type Dict = Record<string, Record<string, unknown>>;

type HintKind = 'units' | 'number' | 'icons' | 'logo';

type CalculatedSpots = {
  centroids: Record<string, Pt>;
  positions: Record<string, { marker: Pt; unit: Pt; center?: Pt }>;
  hints: Record<string, Partial<Record<HintKind, Pt>>>;
};

/**
 * Dot colors per spot kind (issue scheme: blue units, red production / sea zone number,
 * yellow faction logo, green territory type icon + signals/rings/other). Production and sea
 * zone number share red because sea zones can never be owned or produce — they never coexist.
 */
const SPOT_KINDS: { kind: MapSpotKind; label: string; color: string }[] = [
  { kind: 'unit', label: 'Units', color: '#2563eb' },
  { kind: 'marker', label: 'Production / markers', color: '#dc2626' },
  { kind: 'number', label: 'Sea zone number', color: '#dc2626' },
  { kind: 'logo', label: 'Faction logo', color: '#eab308' },
  { kind: 'icons', label: 'Territory type icon', color: '#16a34a' },
  { kind: 'center', label: 'Signals / rings', color: '#16a34a' },
];
const KIND_COLOR: Record<MapSpotKind, string> = Object.fromEntries(
  SPOT_KINDS.map((row) => [row.kind, row.color]),
) as Record<MapSpotKind, string>;
const KIND_LABEL: Record<MapSpotKind, string> = Object.fromEntries(
  SPOT_KINDS.map((row) => [row.kind, row.label]),
) as Record<MapSpotKind, string>;
/**
 * Kinds every territory shows by default (blue + red). The rest (yellow / green) only appear
 * when an Inkscape hint or special-case calculation places them, or when manually overridden.
 */
const ALWAYS_KINDS: MapSpotKind[] = ['unit', 'marker'];
const EXTRA_KINDS: MapSpotKind[] = ['number', 'icons', 'logo', 'center'];

const NOOP = () => {};

/** Object keys sorted so re-adding/removing a territory does not count as a change by itself. */
function stableJson(value: unknown): string {
  return JSON.stringify(value, (_k, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
}

function prettifyId(id: string): string {
  return id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function territoryLabel(id: string, territories: Dict): string {
  const name = territories[id] && typeof territories[id].display_name === 'string' ? (territories[id].display_name as string).trim() : '';
  return name || prettifyId(id);
}

function resolveTid<T>(dict: Record<string, T> | undefined, tid: string): string | null {
  if (!dict) return null;
  if (tid in dict) return tid;
  const lower = tid.toLowerCase();
  if (lower in dict) return lower;
  for (const key of Object.keys(dict)) {
    if (key.toLowerCase() === lower) return key;
  }
  return null;
}

function territoryIdFromPoint(clientX: number, clientY: number): string | null {
  try {
    for (const node of document.elementsFromPoint(clientX, clientY)) {
      if (!(node instanceof Element)) continue;
      const path = node.closest('path.admin-graph__path');
      const tid = path instanceof SVGPathElement ? path.dataset.tid : undefined;
      if (tid) return tid;
    }
  } catch {
    /* ignore */
  }
  return null;
}

function entryFor(data: MapOverridesData | null, tid: string): MapOverridesData['territories'][string] | undefined {
  if (!data) return undefined;
  const key = resolveTid(data.territories, tid);
  return key ? data.territories[key] : undefined;
}

function fmt(pt: MapOverridePoint): string {
  return `${Math.round(pt.x)}, ${Math.round(pt.y)}`;
}

/**
 * Map-level icon/location override editor: pick a map, see every calculated spot as a color-coded
 * dot, flip a territory from "calculated" to "override", then drag its dots within the territory.
 * Saves the whole overrides blob for the map (shared by every setup using it).
 */
export function MapOverridesPane({ territories = {}, onClose }: { territories?: Dict; onClose: () => void }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [maps, setMaps] = useState<string[]>([]);
  const [mapBase, setMapBase] = useState('');
  const [pendingMap, setPendingMap] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paths, setPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [viewBox, setViewBox] = useState({ width: 3500, height: 2600 });
  const [pngFailed, setPngFailed] = useState(false);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [selected, setSelected] = useState<string | null>(null);
  const [activeKind, setActiveKind] = useState<MapSpotKind>('unit');
  const [search, setSearch] = useState('');
  const [stored, setStored] = useState<MapOverridesData | null>(null);
  const [draft, setDraft] = useState<MapOverridesData | null>(null);
  const [spots, setSpots] = useState<CalculatedSpots | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);

  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number; id: number } | null>(null);
  const didDragRef = useRef(false);
  const dotDragRef = useRef<{
    tid: string;
    kind: MapSpotKind;
    id: number;
    sx: number;
    sy: number;
    scale: number;
    startPt: Pt;
  } | null>(null);

  const current = draft ?? stored;
  const dirty = draft != null && stored != null && stableJson(draft) !== stableJson(stored);

  // --- data loading -------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    api
      .adminListMaps()
      .then((r) => {
        if (cancelled) return;
        const list = Array.isArray(r?.maps) ? r.maps.filter((m) => typeof m === 'string' && m) : [];
        setMaps(list);
        setMapBase((prev) => prev || list[0] || '');
        if (!list.length) {
          setErr('No maps found.');
          setLoading(false);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : 'Failed to list maps');
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!mapBase) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErr(null);
    setPaths(new Map());
    setSpots(null);
    setStored(null);
    setDraft(null);
    setSelected(null);
    setPngFailed(false);
    setSearch('');
    api
      .getMapOverrides(mapBase)
      .then((r) => {
        if (!cancelled) setStored(normalizeMapOverrides(r?.overrides));
      })
      .catch(() => {
        if (!cancelled) setStored({ territories: {} });
      });
    loadTerritorySvgPaths(mapBase)
      .then(({ paths: nextPaths, viewBox: vb }) => {
        if (cancelled) return;
        setPaths(nextPaths);
        setViewBox(vb);
        setLoading(false);
      })
      .catch((e) => {
        if (!cancelled) {
          setErr(e instanceof Error ? e.message : 'Failed to load map SVG');
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [mapBase]);

  // --- calculated spot oracle (hidden GameMap) ----------------------------

  const onSpotsCalculated = useCallback((next: CalculatedSpots) => {
    setSpots((prev) => (prev && stableJson(prev) === stableJson(next) ? prev : next));
  }, []);

  const oracleGameState = useMemo<GameState>(
    () => ({
      turn_number: 1,
      current_faction: '',
      phase: 'purchase',
      territories: {},
      faction_resources: {},
      pending_purchases: {},
      pending_moves: [],
      pending_mobilizations: [],
      pending_camp_placements: [],
      declared_battles: [],
      map_asset: mapBase || DEFAULT_MAP_BASE,
    }),
    [mapBase],
  );

  const oracleTerritoryData = useMemo(() => {
    const out: Record<string, { name: string; owner?: string; terrain: string; stronghold: boolean; produces: number; adjacent: string[] }> = {};
    for (const [tid, def] of Object.entries(territories)) {
      const terrain =
        typeof def.terrain_type === 'string' ? def.terrain_type : typeof def.terrain === 'string' ? def.terrain : 'land';
      out[tid] = {
        name: territoryLabel(tid, territories),
        terrain,
        stronghold: false,
        produces: 0,
        adjacent: [],
      };
    }
    return out;
  }, [territories]);

  // --- viewport fit / pan / zoom -----------------------------------------

  useEffect(() => {
    if (loading || err) return;
    const el = wrapRef.current;
    if (!el) return;
    let lastW = -1;
    let lastH = -1;
    const fit = () => {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      if (Math.abs(r.width - lastW) < 1 && Math.abs(r.height - lastH) < 1) return;
      lastW = r.width;
      lastH = r.height;
      const s = Math.min(r.width / viewBox.width, r.height / viewBox.height);
      setTransform({
        scale: s,
        x: (r.width - viewBox.width * s) / 2,
        y: (r.height - viewBox.height * s) / 2,
      });
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [loading, err, viewBox.width, viewBox.height]);

  const requestClose = () => {
    if (saving) return;
    if (dirty) {
      setPendingMap(null);
      setDiscardOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (discardOpen) {
        setDiscardOpen(false);
        setPendingMap(null);
        return;
      }
      if (saving) return;
      if (dirty) {
        setPendingMap(null);
        setDiscardOpen(true);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [discardOpen, saving, dirty, onClose]);

  const changeMap = (next: string) => {
    if (!next || next === mapBase) return;
    if (dirty) {
      setPendingMap(next);
      setDiscardOpen(true);
      return;
    }
    setMapBase(next);
  };

  const confirmDiscard = () => {
    setDraft(null);
    setDiscardOpen(false);
    if (pendingMap) {
      setMapBase(pendingMap);
      setPendingMap(null);
    } else {
      onClose();
    }
  };

  const onWheel = (e: ReactWheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = wrapRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.08 : 1 / 1.08;
    setTransform((prev) => {
      const nextScale = Math.min(8, Math.max(0.25, prev.scale * factor));
      const k = nextScale / prev.scale;
      return {
        scale: nextScale,
        x: mx - (mx - prev.x) * k,
        y: my - (my - prev.y) * k,
      };
    });
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    didDragRef.current = false;
    dragRef.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y, id: e.pointerId };
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d || e.pointerId !== d.id) return;
    const dist = Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y);
    if (!didDragRef.current) {
      if (dist < 10) return;
      didDragRef.current = true;
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    }
    setTransform((prev) => ({
      ...prev,
      x: d.tx + (e.clientX - d.x),
      y: d.ty + (e.clientY - d.y),
    }));
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (d && e.pointerId === d.id) {
      try {
        if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    }
    dragRef.current = null;
    if (didDragRef.current) return;
    const tid = territoryIdFromPoint(e.clientX, e.clientY);
    if (tid) {
      setSelected(tid);
      setActiveKind('unit');
    }
  };

  // --- spots / overrides --------------------------------------------------

  const calculatedFor = useCallback(
    (tid: string): Partial<Record<MapSpotKind, Pt>> => {
      if (!spots) return {};
      const pKey = resolveTid(spots.positions, tid);
      const hKey = resolveTid(spots.hints, tid);
      const p = pKey ? spots.positions[pKey] : undefined;
      const h = hKey ? spots.hints[hKey] : undefined;
      const out: Partial<Record<MapSpotKind, Pt>> = {};
      if (p?.unit) out.unit = p.unit;
      if (p?.marker) out.marker = p.marker;
      // Center (signals/rings) only counts as calculated when it differs from the unit spot —
      // the oracle defaults center = unit for every territory, which isn't a real anchor.
      const center = p?.center ?? p?.unit;
      if (center && (!p?.unit || center.x !== p.unit.x || center.y !== p.unit.y)) {
        out.center = center;
      }
      if (h?.number) out.number = h.number;
      if (h?.icons) out.icons = h.icons;
      if (h?.logo) out.logo = h.logo;
      // Reproduce the render-time unit pixel nudge (viewBox ≈ screen px, 1:1 per MAP_CONFIG).
      // Skipped when an SVG units hint anchors the stack — those bypass the nudge in GameMap.
      if (out.unit && !h?.units) {
        const nudge = TERRITORY_UNIT_PIXEL_NUDGE[tid] ?? TERRITORY_UNIT_PIXEL_NUDGE[resolveTid(TERRITORY_UNIT_PIXEL_NUDGE, tid) ?? ''];
        if (nudge) out.unit = { x: out.unit.x + (nudge.x ?? 0), y: out.unit.y + (nudge.y ?? 0) };
      }
      return out;
    },
    [spots],
  );

  const overridePositionsFor = useCallback(
    (tid: string): Partial<Record<MapSpotKind, MapOverridePoint>> | undefined => {
      const entry = entryFor(current, tid);
      return entry?.focus === 'override' ? entry.positions : undefined;
    },
    [current],
  );

  const effectiveFor = useCallback(
    (tid: string, kind: MapSpotKind): { pt: MapOverridePoint; isOverride: boolean } | null => {
      const ovp = overridePositionsFor(tid)?.[kind];
      if (ovp) return { pt: ovp, isOverride: true };
      const calc = calculatedFor(tid)[kind];
      return calc ? { pt: calc, isOverride: false } : null;
    },
    [calculatedFor, overridePositionsFor],
  );

  const updateEntry = useCallback((tid: string, entry: MapOverridesData['territories'][string] | null) => {
    setDraft((prevDraft) => {
      const base = prevDraft ?? stored ?? { territories: {} };
      const key = resolveTid(base.territories, tid) ?? tid;
      const next = { ...base.territories };
      if (entry === null) delete next[key];
      else next[key] = entry;
      const data: MapOverridesData = { territories: next };
      return stableJson(data) === stableJson(base) ? prevDraft ?? base : data;
    });
    setSaveError(null);
  }, [stored]);

  const setFocus = (tid: string, focus: 'calculated' | 'override') => {
    if (focus === 'calculated') {
      updateEntry(tid, null);
      return;
    }
    const existing = entryFor(current, tid);
    if (existing?.focus === 'override') return;
    const calc = calculatedFor(tid);
    const positions: Partial<Record<MapSpotKind, MapOverridePoint>> = {};
    for (const kind of [...ALWAYS_KINDS, ...EXTRA_KINDS]) {
      const pt = calc[kind];
      if (pt) positions[kind] = pt;
    }
    updateEntry(tid, { focus: 'override', positions });
  };

  const setKindPosition = (tid: string, kind: MapSpotKind, pt: MapOverridePoint) => {
    const existing = entryFor(current, tid);
    const positions = { ...(existing?.focus === 'override' ? existing.positions : {}), [kind]: pt };
    updateEntry(tid, { focus: 'override', positions });
  };

  const resetKind = (tid: string, kind: MapSpotKind) => {
    const existing = entryFor(current, tid);
    if (existing?.focus !== 'override' || !existing.positions) return;
    const positions = { ...existing.positions };
    delete positions[kind];
    updateEntry(tid, { focus: 'override', positions });
  };

  const addKind = (tid: string, kind: MapSpotKind) => {
    const calc = calculatedFor(tid);
    const seed = calc[kind] ?? calc.marker ?? calc.unit ?? calc.center;
    if (!seed) return;
    setKindPosition(tid, kind, seed);
    setActiveKind(kind);
  };

  // --- dot dragging (constrained to the territory) ------------------------

  const onDotPointerDown = (e: ReactPointerEvent<HTMLDivElement>, tid: string, kind: MapSpotKind) => {
    e.stopPropagation();
    e.preventDefault();
    if (e.button !== 0) return;
    setSelected(tid);
    setActiveKind(kind);
    const entry = entryFor(current, tid);
    if (entry?.focus !== 'override') return; // calculated dots are not draggable until focus flips
    const spot = effectiveFor(tid, kind);
    if (!spot) return;
    try {
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dotDragRef.current = {
      tid,
      kind,
      id: e.pointerId,
      sx: e.clientX,
      sy: e.clientY,
      scale: transform.scale,
      startPt: { x: spot.pt.x, y: spot.pt.y },
    };
  };

  const onDotPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = dotDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    // Constrain: the cursor must be over this territory's path (same hit test as selection).
    const overTid = territoryIdFromPoint(e.clientX, e.clientY);
    if (!overTid || !sameTerritoryId(overTid, d.tid)) return;
    const dx = (e.clientX - d.sx) / d.scale;
    const dy = (e.clientY - d.sy) / d.scale;
    const nx = Math.max(0, Math.min(viewBox.width, d.startPt.x + dx));
    const ny = Math.max(0, Math.min(viewBox.height, d.startPt.y + dy));
    setKindPosition(d.tid, d.kind, { x: nx, y: ny });
  };

  const onDotPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    const d = dotDragRef.current;
    if (!d || e.pointerId !== d.id) return;
    try {
      if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    dotDragRef.current = null;
  };

  // --- save ---------------------------------------------------------------

  const save = async () => {
    if (!dirty || !draft || !mapBase || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res = await api.adminPutMapOverrides(mapBase, draft);
      setStored(normalizeMapOverrides(res?.overrides ?? draft));
      setDraft(null);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  // --- derived lists ------------------------------------------------------

  const territoryChoices = useMemo(() => {
    const seen = new Set<string>();
    const rows: { id: string; label: string }[] = [];
    for (const id of paths.keys()) {
      if (seen.has(id)) continue;
      seen.add(id);
      rows.push({ id, label: territoryLabel(id, territories) });
    }
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [paths, territories]);

  const filteredChoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return territoryChoices;
    return territoryChoices.filter(
      (row) => row.label.toLowerCase().includes(q) || row.id.toLowerCase().includes(q),
    );
  }, [territoryChoices, search]);

  const dots = useMemo(() => {
    if (!spots) return [];
    const rows: { tid: string; kind: MapSpotKind; pt: MapOverridePoint; isOverride: boolean }[] = [];
    for (const tid of paths.keys()) {
      for (const { kind } of SPOT_KINDS) {
        const spot = effectiveFor(tid, kind);
        if (!spot) continue;
        rows.push({ tid, kind, pt: spot.pt, isOverride: spot.isOverride });
      }
    }
    // Selected territory's dots (and the active kind) render last so they stay clickable when overlapping.
    rows.sort((a, b) => {
      const aSel = a.tid === selected ? 1 : 0;
      const bSel = b.tid === selected ? 1 : 0;
      if (aSel !== bSel) return aSel - bSel;
      const aAct = a.tid === selected && a.kind === activeKind ? 1 : 0;
      const bAct = b.tid === selected && b.kind === activeKind ? 1 : 0;
      if (aAct !== bAct) return aAct - bAct;
      return 0;
    });
    return rows;
  }, [spots, paths, effectiveFor, selected, activeKind]);

  const overrideCount = useMemo(
    () => Object.values(current?.territories ?? {}).filter((e) => e.focus === 'override').length,
    [current],
  );

  const selectedEntry = selected ? entryFor(current, selected) : undefined;
  const selectedFocus: 'calculated' | 'override' = selectedEntry?.focus === 'override' ? 'override' : 'calculated';
  const selectedCalc = useMemo(
    () => (selected ? calculatedFor(selected) : {}),
    [selected, calculatedFor],
  );
  const selectedOverride = selectedEntry?.focus === 'override' ? (selectedEntry.positions ?? {}) : {};

  const description = spots
    ? 'Every territory starts with a blue (units) and red (production / sea number) dot. Yellow and green dots appear only when calculated or placed. Pick a territory, switch to Override, then drag its dots inside the territory. Save applies to every setup using this map.'
    : 'Loading calculated spots…';

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={requestClose}>
      <div
        className="admin-modal admin-modal--graph"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-mapov-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="admin-graph__header">
          <div className="admin-graph__title-row">
            <h2 id="admin-mapov-title" className="admin-modal__title">
              Map icons
            </h2>
            <label className="admin-graph__prop">
              Map
              <select
                className="admin-page__select"
                value={mapBase}
                onChange={(e) => changeMap(e.target.value)}
                disabled={maps.length === 0}
              >
                {maps.length === 0 ? <option value="">No maps</option> : null}
                {maps.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="admin-graph__header-actions">
            <button
              type="button"
              className={`admin-page__btn${dirty ? ' admin-page__btn--primary' : ''}`}
              disabled={saving || !dirty}
              onClick={() => void save()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="admin-page__btn" onClick={requestClose} disabled={saving}>
              Close
            </button>
          </div>
          <p className="admin-form__micro">{description}</p>
          <ul className="admin-graph__key mapov-legend" aria-label="Spot colors">
            {SPOT_KINDS.map((row) => (
              <li key={row.kind}>
                <span
                  className="mapov-legend__swatch"
                  style={{ background: row.color }}
                  aria-hidden
                />
                {row.label}
              </li>
            ))}
          </ul>
        </div>
        {loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {err ? <p className="admin-page__error">{err}</p> : null}
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
        {!loading && !err ? (
          <div className="admin-graph__body">
            <div className="admin-graph__side mapov-side">
              <input
                type="search"
                className="admin-form__input admin-graph__search"
                placeholder="Search territories"
                aria-label="Search territories"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <div className="admin-graph__list" role="listbox" aria-label="Territories">
                {filteredChoices.map((row) => {
                  const overridden = entryFor(current, row.id)?.focus === 'override';
                  return (
                    <button
                      key={row.id}
                      type="button"
                      role="option"
                      aria-selected={selected === row.id}
                      className={`admin-graph__list-item${selected === row.id ? ' admin-graph__list-item--selected' : ''}`}
                      onClick={() => {
                        setSelected(row.id);
                        setActiveKind('unit');
                      }}
                    >
                      {row.label}
                      {overridden ? <span className="mapov-list-badge" title="Override">●</span> : null}
                    </button>
                  );
                })}
                {filteredChoices.length ? null : <p className="admin-graph__list-empty">No matches</p>}
              </div>
              {selected ? (
                <div className="mapov-inspector">
                  <div className="mapov-inspector__head">
                    <strong title={selected}>{territoryLabel(selected, territories)}</strong>
                    <div className="admin-mapview__toggle" role="group" aria-label="Spot focus">
                      <button
                        type="button"
                        className={`admin-mapview__toggle-btn${selectedFocus === 'calculated' ? ' admin-mapview__toggle-btn--active' : ''}`}
                        aria-pressed={selectedFocus === 'calculated'}
                        onClick={() => setFocus(selected, 'calculated')}
                      >
                        Calculated
                      </button>
                      <button
                        type="button"
                        className={`admin-mapview__toggle-btn${selectedFocus === 'override' ? ' admin-mapview__toggle-btn--active' : ''}`}
                        aria-pressed={selectedFocus === 'override'}
                        onClick={() => setFocus(selected, 'override')}
                      >
                        Override
                      </button>
                    </div>
                  </div>
                  {selectedFocus === 'override' ? (
                    <ul className="mapov-kinds">
                      {SPOT_KINDS.map(({ kind }) => {
                        const ovp = selectedOverride[kind];
                        const calc = selectedCalc[kind];
                        const shown = ovp ?? calc;
                        if (!shown && !ALWAYS_KINDS.includes(kind)) {
                          return (
                            <li key={kind} className="mapov-kind mapov-kind--absent">
                              <span className="mapov-kind__swatch" style={{ background: KIND_COLOR[kind] }} aria-hidden />
                              <span className="mapov-kind__label">{KIND_LABEL[kind]}</span>
                              <button type="button" className="admin-page__btn mapov-kind__btn" onClick={() => addKind(selected, kind)}>
                                + Place
                              </button>
                            </li>
                          );
                        }
                        if (!shown) return null;
                        return (
                          <li
                            key={kind}
                            className={`mapov-kind${activeKind === kind ? ' mapov-kind--active' : ''}${ovp ? ' mapov-kind--override' : ''}`}
                            onClick={() => setActiveKind(kind)}
                          >
                            <span className="mapov-kind__swatch" style={{ background: KIND_COLOR[kind] }} aria-hidden />
                            <span className="mapov-kind__label">{KIND_LABEL[kind]}</span>
                            <span className="mapov-kind__pos" title={ovp ? 'Override' : 'Calculated'}>
                              {fmt(shown)}
                              {ovp ? '' : ' · calc'}
                            </span>
                            {ovp ? (
                              <button
                                type="button"
                                className="admin-page__btn mapov-kind__btn"
                                title="Reset this spot to calculated"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  resetKind(selected, kind);
                                }}
                              >
                                Reset
                              </button>
                            ) : null}
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    <p className="admin-form__micro">
                      Spots on the map are calculated. Switch to Override to move them.
                    </p>
                  )}
                </div>
              ) : (
                <p className="admin-form__micro">Choose a territory from the list or the map.</p>
              )}
            </div>
            <div
              ref={wrapRef}
              className="admin-graph__viewport"
              onWheel={onWheel}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
            >
              <div
                className="admin-graph__world"
                style={{
                  width: viewBox.width,
                  height: viewBox.height,
                  transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
                }}
              >
                {pngFailed ? null : (
                  <img
                    className="admin-graph__landscape"
                    src={`/${mapBase}.png`}
                    alt=""
                    draggable={false}
                    onError={() => setPngFailed(true)}
                  />
                )}
                <svg
                  className="admin-graph__svg"
                  viewBox={`0 0 ${viewBox.width} ${viewBox.height}`}
                  width={viewBox.width}
                  height={viewBox.height}
                >
                  {Array.from(paths.entries()).map(([tid, pathData]) => (
                    <path
                      key={tid}
                      data-tid={tid}
                      d={pathData.d}
                      transform={pathData.transform}
                      className={`admin-graph__path${selected && sameTerritoryId(tid, selected) ? ' admin-graph__path--selected' : ''}`}
                    />
                  ))}
                </svg>
              </div>
              <div className="mapov-dots">
                {dots.map((d) => {
                  const isActive = d.tid === selected && d.kind === activeKind;
                  const x = transform.x + d.pt.x * transform.scale;
                  const y = transform.y + d.pt.y * transform.scale;
                  return (
                    <div
                      key={`${d.tid}|${d.kind}`}
                      role="button"
                      tabIndex={0}
                      aria-label={`${territoryLabel(d.tid, territories)} ${KIND_LABEL[d.kind]}`}
                      className={`mapov-dot mapov-dot--${d.kind}${d.isOverride ? ' mapov-dot--override' : ''}${isActive ? ' mapov-dot--active' : ''}`}
                      style={{ left: x, top: y, background: KIND_COLOR[d.kind] }}
                      title={`${territoryLabel(d.tid, territories)} · ${KIND_LABEL[d.kind]} · ${d.isOverride ? 'override' : 'calculated'}${selectedFocus === 'override' && d.tid === selected ? ' — drag to move' : ''}`}
                      onPointerDown={(e) => onDotPointerDown(e, d.tid, d.kind)}
                      onPointerMove={onDotPointerMove}
                      onPointerUp={onDotPointerUp}
                      onPointerCancel={onDotPointerUp}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          setSelected(d.tid);
                          setActiveKind(d.kind);
                        }
                      }}
                    />
                  );
                })}
              </div>
            </div>
          </div>
        ) : null}
        <p className="admin-graph__status">
          {selected
            ? `${territoryLabel(selected, territories)} · ${KIND_LABEL[activeKind]}${
                selectedFocus === 'override' ? ' · override' : ' · calculated'
              }${overrideCount ? ` · ${overrideCount} overridden` : ''}`
            : 'No territory selected'}
        </p>
        {/* Hidden spot oracle: a mounted-but-invisible GameMap reports the exact calculated anchors. */}
        <div className="mapov-oracle" aria-hidden="true">
          <GameMap
            key={mapBase || DEFAULT_MAP_BASE}
            gameState={oracleGameState}
            selectedTerritory={null}
            selectedUnit={null}
            territoryData={oracleTerritoryData}
            territoryUnits={{}}
            rings={[]}
            unitDefs={{}}
            unitStats={{}}
            factionData={{}}
            onTerritorySelect={NOOP}
            onUnitSelect={NOOP}
            onUnitMove={NOOP}
            canAct={false}
            isMovementPhase={false}
            isCombatMove={false}
            isMobilizePhase={false}
            hasMobilizationSelected={false}
            pendingMoveConfirm={null}
            onSetPendingMove={NOOP}
            pendingMoves={[]}
            onSpotsCalculated={onSpotsCalculated}
          />
        </div>
        {discardOpen ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation" onClick={() => { setDiscardOpen(false); setPendingMap(null); }}>
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-mapov-discard-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-mapov-discard-title" className="admin-modal__title">
                {pendingMap ? 'Discard changes and switch maps?' : 'Close without saving?'}
              </h2>
              <p className="admin-form__micro">Unsaved icon overrides will be discarded.</p>
              <div className="admin-modal__actions">
                <button type="button" className="admin-page__btn" onClick={() => { setDiscardOpen(false); setPendingMap(null); }}>
                  Keep editing
                </button>
                <button type="button" className="admin-page__btn admin-page__btn--danger" onClick={confirmDiscard}>
                  {pendingMap ? 'Discard and switch' : 'Discard and close'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default MapOverridesPane;
