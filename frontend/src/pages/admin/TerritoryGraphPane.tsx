import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import {
  adjacencyEdges,
  computePathCentroids,
  loadTerritorySvgPaths,
  lookupCentroid,
  resolveTerritoryKey,
  sameTerritoryId,
  toMapBase,
  type TerritoryPathData,
} from './territoryGraph';

type Pt = { x: number; y: number };

function neighborIdsForField(
  territories: Record<string, Record<string, unknown>>,
  selectedKey: string,
  field: 'adjacent' | 'aerial_adjacent',
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
}: {
  mapAsset: string | undefined;
  territories: Record<string, Record<string, unknown>>;
  onClose: () => void;
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

  useEffect(() => {
    setPngFailed(false);
  }, [mapBase]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

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

  const selectedKey = selected ? resolveTerritoryKey(territories, selected) ?? selected : null;

  const landNeighborIds = useMemo(
    () => (selectedKey ? neighborIdsForField(territories, selectedKey, 'adjacent') : []),
    [selectedKey, territories],
  );
  const aerialNeighborIds = useMemo(
    () => (selectedKey ? neighborIdsForField(territories, selectedKey, 'aerial_adjacent') : []),
    [selectedKey, territories],
  );

  const isLandNeighborTid = (tid: string) => landNeighborIds.some((n) => sameTerritoryId(n, tid));
  const isAerialNeighborTid = (tid: string) => aerialNeighborIds.some((n) => sameTerritoryId(n, tid));
  const isSelectedTid = (tid: string) => selected != null && sameTerritoryId(tid, selected);

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

  const landEdges = useMemo(() => adjacencyEdges(territories, 'adjacent'), [territories]);
  const aerialEdges = useMemo(() => adjacencyEdges(territories, 'aerial_adjacent'), [territories]);

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
    setSelected(tid);
  };

  const renderEdge = (
    kind: 'land' | 'aerial',
    a: string,
    b: string,
    pa: Pt,
    pb: Pt,
  ) => {
    const isHot = selected != null && (sameTerritoryId(a, selected) || sameTerritoryId(b, selected));
    const pts = kind === 'aerial' ? offsetEdge(pa, pb, 7) : { a: pa, b: pb };
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
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
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
            Landscape from {mapBase}.png with borders from the SVG. Click a territory to highlight neighbors.
            Scroll to zoom, drag to pan.
          </p>
          <ul className="admin-graph__key" aria-label="Edge colors">
            <li>
              <span className="admin-graph__key-swatch admin-graph__key-swatch--land" aria-hidden />
              Adjacent
            </li>
            <li>
              <span className="admin-graph__key-swatch admin-graph__key-swatch--aerial" aria-hidden />
              Aerial adjacent
            </li>
          </ul>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>
        {loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {err ? <p className="admin-page__error">{err}</p> : null}
        {!loading && !err ? (
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
                let cls = 'admin-graph__path';
                if (isSel) cls += ' admin-graph__path--selected';
                else if (isLandN && isAerialN) cls += ' admin-graph__path--neighbor-both';
                else if (isLandN) cls += ' admin-graph__path--neighbor-land';
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
                const pa = lookupCentroid(centroids, a);
                const pb = lookupCentroid(centroids, b);
                if (!pa || !pb) return null;
                return renderEdge('aerial', a, b, pa, pb);
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
                      : isLandNeighborTid(tid)
                        ? 'admin-graph__node admin-graph__node--land'
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
        ) : null}
        {selected ? (
          <p className="admin-graph__status">
            {selected}
            {landNeighborIds.length ? ` · adjacent: ${landNeighborIds.join(', ')}` : ''}
            {aerialNeighborIds.length ? ` · aerial: ${aerialNeighborIds.join(', ')}` : ''}
            {!landNeighborIds.length && !aerialNeighborIds.length ? ' (no adjacent listed)' : ''}
          </p>
        ) : (
          <p className="admin-graph__status">No territory selected</p>
        )}
      </div>
    </div>
  );
}
