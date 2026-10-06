import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import {
  adjacencyEdges,
  computePathCentroids,
  loadTerritorySvgPaths,
  lookupCentroid,
  resolveTerritoryKey,
  sameTerritoryId,
  territoriesWithEdgeEdits,
  toMapBase,
  type TerritoryEdgeEdit,
  type TerritoryEdgeField,
  type TerritoryPathData,
} from './territoryGraph';

type Pt = { x: number; y: number };

function neighborIdsForField(
  territories: Record<string, Record<string, unknown>>,
  selectedKey: string,
  field: 'adjacent' | 'aerial_adjacent' | 'ford_adjacent',
): string[] {
  const names = new Set<string>();
  const t = territories[selectedKey];
  const adj = Array.isArray(t?.[field]) ? t[field] : [];
  for (const raw of adj) {
    if (typeof raw === 'string' && raw.trim()) names.add(raw.trim());
  }
  for (const [id, def] of Object.entries(territories)) {
    if (sameTerritoryId(id, selectedKey)) continue;
    const list = Array.isArray(def[field]) ? def[field] : [];
    if (list.some((x) => typeof x === 'string' && sameTerritoryId(x.trim(), selectedKey))) names.add(id);
  }
  return [...names].sort();
}

function offsetEdge(pa: Pt, pb: Pt, dist: number): { a: Pt; b: Pt } {
  const dx = pb.x - pa.x;
  const dy = pb.y - pa.y;
  const len = Math.hypot(dx, dy) || 1;
  const ox = (-dy / len) * dist;
  const oy = (dx / len) * dist;
  return {
    a: { x: pa.x + ox, y: pa.y + oy },
    b: { x: pb.x + ox, y: pb.y + oy },
  };
}

const EDGE_TYPES: { field: TerritoryEdgeField; label: string; kind: 'land' | 'aerial' | 'ford' }[] = [
  { field: 'adjacent', label: 'Land', kind: 'land' },
  { field: 'aerial_adjacent', label: 'Aerial', kind: 'aerial' },
  { field: 'ford_adjacent', label: 'Ford', kind: 'ford' },
];

function ownNeighborIds(
  territories: Record<string, Record<string, unknown>>,
  selectedKey: string,
  field: TerritoryEdgeField,
): string[] {
  const raw = territories[selectedKey]?.[field];
  if (!Array.isArray(raw)) return [];
  return raw.filter((id): id is string => typeof id === 'string');
}

function listsNeighbor(
  territories: Record<string, Record<string, unknown>>,
  from: string,
  to: string,
  field: TerritoryEdgeField,
): boolean {
  return ownNeighborIds(territories, from, field).some((id) => sameTerritoryId(id, to));
}

function territoryLabel(id: string, def: Record<string, unknown> | undefined): string {
  const name = def && typeof def.display_name === 'string' ? def.display_name.trim() : '';
  return name || id;
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

export function TerritoryGraphPane({
  mapAsset,
  territories,
  onClose,
  onSave,
}: {
  mapAsset: string | undefined;
  territories: Record<string, Record<string, unknown>>;
  onClose: () => void;
  onSave: (next: Record<string, Record<string, unknown>>) => Promise<void>;
}) {
  const mapBase = toMapBase(mapAsset);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paths, setPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [viewBox, setViewBox] = useState({ width: 3500, height: 2600 });
  const [centroids, setCentroids] = useState<Record<string, Pt>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number; id: number } | null>(null);
  const didDragRef = useRef(false);
  const fittedRef = useRef(false);
  const [pngFailed, setPngFailed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [edgeField, setEdgeField] = useState<TerritoryEdgeField>('adjacent');
  const [editId, setEditId] = useState<string | null>(null);
  const [edits, setEdits] = useState<TerritoryEdgeEdit[]>([]);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const draft = useMemo(() => territoriesWithEdgeEdits(territories, edits), [territories, edits]);
  const shown = editing ? draft : territories;
  const dirty = edits.length > 0;

  const requestClose = () => {
    if (saving) return;
    if (editing && dirty) {
      setDiscardOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    setPngFailed(false);
  }, [mapBase]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (discardOpen) {
        setDiscardOpen(false);
        return;
      }
      if (saving) return;
      if (editing && dirty) {
        setDiscardOpen(true);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [discardOpen, saving, editing, dirty, onClose]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    setSelected(null);
    void loadTerritorySvgPaths(mapBase)
      .then(({ paths: nextPaths, viewBox: vb }) => {
        if (cancelled) return;
        setPaths(nextPaths);
        setViewBox(vb);
        setCentroids(computePathCentroids(nextPaths, vb, mapBase));
        fittedRef.current = false;
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : 'Failed to load map SVG');
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mapBase]);

  const focusId = editing ? editId : selected;
  const selectedKey = focusId ? resolveTerritoryKey(shown, focusId) ?? focusId : null;

  const landNeighborIds = useMemo(
    () =>
      selectedKey
        ? editing
          ? ownNeighborIds(shown, selectedKey, 'adjacent')
          : neighborIdsForField(shown, selectedKey, 'adjacent')
        : [],
    [selectedKey, shown, editing],
  );
  const aerialNeighborIds = useMemo(
    () =>
      selectedKey
        ? editing
          ? ownNeighborIds(shown, selectedKey, 'aerial_adjacent')
          : neighborIdsForField(shown, selectedKey, 'aerial_adjacent')
        : [],
    [selectedKey, shown, editing],
  );
  const fordNeighborIds = useMemo(
    () =>
      selectedKey
        ? editing
          ? ownNeighborIds(shown, selectedKey, 'ford_adjacent')
          : neighborIdsForField(shown, selectedKey, 'ford_adjacent')
        : [],
    [selectedKey, shown, editing],
  );

  const isLandNeighborTid = (tid: string) => landNeighborIds.some((n) => sameTerritoryId(n, tid));
  const isAerialNeighborTid = (tid: string) => aerialNeighborIds.some((n) => sameTerritoryId(n, tid));
  const isFordNeighborTid = (tid: string) => fordNeighborIds.some((n) => sameTerritoryId(n, tid));
  const isSelectedTid = (tid: string) => focusId != null && sameTerritoryId(tid, focusId);
  const activeKind = EDGE_TYPES.find((type) => type.field === edgeField)?.kind ?? 'land';
  const activeNeighborTid = (tid: string) => {
    if (edgeField === 'adjacent') return isLandNeighborTid(tid);
    if (edgeField === 'aerial_adjacent') return isAerialNeighborTid(tid);
    return isFordNeighborTid(tid);
  };

  const territoryChoices = useMemo(() => {
    const rows = Object.entries(territories).map(([id, def]) => ({
      id,
      label: territoryLabel(id, def),
    }));
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [territories]);

  useEffect(() => {
    if (loading || err || fittedRef.current) return;
    const el = wrapRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const s = Math.min(r.width / viewBox.width, r.height / viewBox.height);
    fittedRef.current = true;
    setTransform({
      scale: s,
      x: (r.width - viewBox.width * s) / 2,
      y: (r.height - viewBox.height * s) / 2,
    });
  }, [loading, err, viewBox.width, viewBox.height, paths]);

  const landEdges = useMemo(() => adjacencyEdges(shown, 'adjacent'), [shown]);
  const aerialEdges = useMemo(() => adjacencyEdges(shown, 'aerial_adjacent'), [shown]);
  const fordEdges = useMemo(() => adjacencyEdges(shown, 'ford_adjacent'), [shown]);

  const toggleEdge = (source: string, neighbor: string) => {
    const currently = listsNeighbor(shown, source, neighbor, edgeField);
    const turningOn = !currently;
    const batch: TerritoryEdgeEdit[] = [];
    const nextSeq = () => {
      seqRef.current += 1;
      return seqRef.current;
    };
    batch.push({ source, neighbor, field: edgeField, on: turningOn, seq: nextSeq() });
    if (turningOn && edgeField === 'adjacent') {
      for (const other of ['aerial_adjacent', 'ford_adjacent'] as const) {
        if (listsNeighbor(shown, source, neighbor, other) || listsNeighbor(shown, neighbor, source, other)) {
          batch.push({ source, neighbor, field: other, on: false, seq: nextSeq() });
        }
      }
    } else if (turningOn && (edgeField === 'aerial_adjacent' || edgeField === 'ford_adjacent')) {
      if (listsNeighbor(shown, source, neighbor, 'adjacent') || listsNeighbor(shown, neighbor, source, 'adjacent')) {
        batch.push({ source, neighbor, field: 'adjacent', on: false, seq: nextSeq() });
      }
    }
    setEdits((prev) => [...prev, ...batch]);
    setSaveError(null);
  };

  const saveEdits = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(draft);
      seqRef.current = 0;
      setEdits([]);
      setEditing(false);
      setDiscardOpen(false);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
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
    if (!editing) {
      setSelected(tid);
      return;
    }
    if (!tid || !editId) return;
    const source = resolveTerritoryKey(shown, editId);
    const neighbor = resolveTerritoryKey(shown, tid);
    if (!source || !neighbor || sameTerritoryId(source, neighbor)) return;
    toggleEdge(source, neighbor);
  };

  const renderEdge = (
    kind: 'land' | 'aerial' | 'ford',
    a: string,
    b: string,
    pa: Pt,
    pb: Pt,
  ) => {
    const isHot = focusId != null && (sameTerritoryId(a, focusId) || sameTerritoryId(b, focusId));
    const pts =
      kind === 'aerial' ? offsetEdge(pa, pb, 7) : kind === 'ford' ? offsetEdge(pa, pb, -7) : { a: pa, b: pb };
    return (
      <line
        key={`${kind}|${a}|${b}`}
        x1={pts.a.x}
        y1={pts.a.y}
        x2={pts.b.x}
        y2={pts.b.y}
        className={`admin-graph__edge admin-graph__edge--${kind}${isHot ? ' admin-graph__edge--hot' : ''}`}
        pointerEvents="none"
      />
    );
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={requestClose}>
      <div
        className="admin-modal admin-modal--graph"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-graph-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="admin-graph__header">
          <h2 id="admin-graph-title" className="admin-modal__title">
            Territory graph
          </h2>
          <p className="admin-form__micro">
            {editing
              ? 'Choose a territory, then click the map to toggle its neighbors. Land replaces aerial and ford on that border. Aerial and ford can both stay. Save writes both sides.'
              : `Landscape from ${mapBase}.png with borders from the SVG. Click a territory to highlight neighbors. Scroll to zoom, drag to pan.`}
          </p>
          {editing ? (
            <div className="admin-graph__types" role="group" aria-label="Adjacency type">
              {EDGE_TYPES.map((type) => (
                <button
                  key={type.field}
                  type="button"
                  className={`admin-graph__type admin-graph__type--${type.kind}${edgeField === type.field ? ' admin-graph__type--active' : ''}`}
                  aria-pressed={edgeField === type.field}
                  onClick={() => setEdgeField(type.field)}
                >
                  {type.label}
                </button>
              ))}
            </div>
          ) : null}
          <div className="admin-graph__header-actions">
            <button
              type="button"
              className={`admin-page__btn${editing ? ' admin-page__btn--primary' : ''}`}
              disabled={saving || (editing && !dirty)}
              onClick={() => {
                if (editing) void saveEdits();
                else {
                  setEditing(true);
                  setEdgeField('adjacent');
                  setEditId(null);
                  setSaveError(null);
                }
              }}
            >
              {saving ? 'Saving…' : editing ? 'Save' : 'Edit'}
            </button>
            <button type="button" className="admin-page__btn" onClick={requestClose} disabled={saving}>
              Close
            </button>
          </div>
          <ul className="admin-graph__key" aria-label="Edge colors">
            <li>
              <span className="admin-graph__key-swatch admin-graph__key-swatch--land" aria-hidden />
              Adjacent
            </li>
            <li>
              <span className="admin-graph__key-swatch admin-graph__key-swatch--aerial" aria-hidden />
              Aerial adjacent
            </li>
            <li>
              <span className="admin-graph__key-swatch admin-graph__key-swatch--ford" aria-hidden />
              Ford adjacent
            </li>
          </ul>
        </div>
        {loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {err ? <p className="admin-page__error">{err}</p> : null}
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
        {!loading && !err ? (
          <div className="admin-graph__body">
            {editing ? (
              <div className="admin-graph__list" role="listbox" aria-label="Territories">
                {territoryChoices.map((row) => (
                  <button
                    key={row.id}
                    type="button"
                    role="option"
                    aria-selected={editId === row.id}
                    className={`admin-graph__list-item${editId === row.id ? ' admin-graph__list-item--selected' : ''}`}
                    onClick={() => setEditId(row.id)}
                  >
                    {row.label}
                  </button>
                ))}
              </div>
            ) : null}
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
              {Array.from(paths.entries()).map(([tid, pathData]) => {
                const isSel = isSelectedTid(tid);
                const isLandN = !isSel && isLandNeighborTid(tid);
                const isAerialN = !isSel && isAerialNeighborTid(tid);
                const isFordN = !isSel && isFordNeighborTid(tid);
                let cls = 'admin-graph__path';
                if (isSel) cls += ' admin-graph__path--selected';
                else if (editing) {
                  if (activeNeighborTid(tid)) cls += ` admin-graph__path--neighbor-${activeKind}`;
                } else if (isFordN && isAerialN) cls += ' admin-graph__path--neighbor-ford';
                else if (isLandN && isAerialN) cls += ' admin-graph__path--neighbor-both';
                else if (isLandN) cls += ' admin-graph__path--neighbor-land';
                else if (isFordN) cls += ' admin-graph__path--neighbor-ford';
                else if (isAerialN) cls += ' admin-graph__path--neighbor-aerial';
                return (
                  <path
                    key={tid}
                    data-tid={tid}
                    d={pathData.d}
                    transform={pathData.transform}
                    className={cls}
                  />
                );
              })}
              {landEdges.map(({ a, b }) => {
                const pa = lookupCentroid(centroids, a);
                const pb = lookupCentroid(centroids, b);
                if (!pa || !pb) return null;
                return renderEdge('land', a, b, pa, pb);
              })}
              {aerialEdges.map(({ a, b }) => {
                if (
                  fordEdges.some(
                    (e) =>
                      (sameTerritoryId(e.a, a) && sameTerritoryId(e.b, b)) ||
                      (sameTerritoryId(e.a, b) && sameTerritoryId(e.b, a)),
                  )
                ) {
                  return null;
                }
                const pa = lookupCentroid(centroids, a);
                const pb = lookupCentroid(centroids, b);
                if (!pa || !pb) return null;
                return renderEdge('aerial', a, b, pa, pb);
              })}
              {fordEdges.map(({ a, b }) => {
                const pa = lookupCentroid(centroids, a);
                const pb = lookupCentroid(centroids, b);
                if (!pa || !pb) return null;
                return renderEdge('ford', a, b, pa, pb);
              })}
              {Object.entries(centroids).map(([tid, c]) => (
                <circle
                  key={`n-${tid}`}
                  cx={c.x}
                  cy={c.y}
                  r={isSelectedTid(tid) ? 8 : 5}
                  className={
                    isSelectedTid(tid)
                      ? 'admin-graph__node admin-graph__node--selected'
                      : editing
                        ? activeNeighborTid(tid)
                          ? `admin-graph__node admin-graph__node--${activeKind}`
                          : 'admin-graph__node'
                        : isFordNeighborTid(tid) && isAerialNeighborTid(tid)
                          ? 'admin-graph__node admin-graph__node--ford'
                          : isLandNeighborTid(tid)
                            ? 'admin-graph__node admin-graph__node--land'
                            : isFordNeighborTid(tid)
                              ? 'admin-graph__node admin-graph__node--ford'
                              : isAerialNeighborTid(tid)
                                ? 'admin-graph__node admin-graph__node--aerial'
                                : 'admin-graph__node'
                  }
                  pointerEvents="none"
                />
              ))}
              </svg>
            </div>
          </div>
          </div>
        ) : null}
        {editing ? (
          <p className="admin-graph__status">
            {editId
              ? `${territoryLabel(editId, territories[editId])} · ${EDGE_TYPES.find((type) => type.field === edgeField)?.label ?? 'Land'}: ${(edgeField === 'adjacent' ? landNeighborIds : edgeField === 'aerial_adjacent' ? aerialNeighborIds : fordNeighborIds).join(', ') || 'none'}`
              : 'Choose a territory'}
          </p>
        ) : selected ? (
          <p className="admin-graph__status">
            {selected}
            {landNeighborIds.length ? ` · adjacent: ${landNeighborIds.join(', ')}` : ''}
            {aerialNeighborIds.length ? ` · aerial: ${aerialNeighborIds.join(', ')}` : ''}
            {fordNeighborIds.length ? ` · ford: ${fordNeighborIds.join(', ')}` : ''}
            {!landNeighborIds.length && !aerialNeighborIds.length && !fordNeighborIds.length ? ' (no adjacent listed)' : ''}
          </p>
        ) : (
          <p className="admin-graph__status">No territory selected</p>
        )}
        {discardOpen ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation" onClick={() => setDiscardOpen(false)}>
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-graph-discard-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-graph-discard-title" className="admin-modal__title">
                Close without saving?
              </h2>
              <p className="admin-form__micro">Adjacency edits will be discarded.</p>
              <div className="admin-modal__actions">
                <button type="button" className="admin-page__btn" onClick={() => setDiscardOpen(false)}>
                  Keep editing
                </button>
                <button type="button" className="admin-page__btn admin-page__btn--danger" onClick={onClose}>
                  Discard and close
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
