import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react';
import GameMap from '../../components/GameMap';
import type { RingView } from '../../ringsDisplay';
import type { GameState, PendingMove } from '../../types/game';
import {
  adjacencyEdges,
  computePathCentroids,
  hasSiteAt,
  loadTerritorySvgPaths,
  lookupCentroid,
  resolveTerritoryKey,
  sameTerritoryId,
  sitesWithToggle,
  stacksForTerritory,
  startingSetupWithUnitEdits,
  startingUnitsOf,
  territoriesWithEdgeEdits,
  territoryPower,
  toMapBase,
  type StartingStack,
  type TerritoryEdgeEdit,
  type TerritoryEdgeField,
  type TerritoryPathData,
} from './territoryGraph';
import { NumberField } from './NumberField';

type Pt = { x: number; y: number };
type Dict = Record<string, Record<string, unknown>>;

export type MapSetupDraft = {
  territories: Dict;
  starting_setup: Record<string, unknown>;
  camps: Dict;
  ports: Dict;
};

function neighborIdsForField(
  territories: Dict,
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

const NOOP = () => {};
const NO_MOVES: PendingMove[] = [];

function ownNeighborIds(territories: Dict, selectedKey: string, field: TerritoryEdgeField): string[] {
  const raw = territories[selectedKey]?.[field];
  if (!Array.isArray(raw)) return [];
  return raw.filter((id): id is string => typeof id === 'string');
}

function listsNeighbor(territories: Dict, from: string, to: string, field: TerritoryEdgeField): boolean {
  return ownNeighborIds(territories, from, field).some((id) => sameTerritoryId(id, to));
}

function territoryLabel(id: string, def: Record<string, unknown> | undefined): string {
  const name = def && typeof def.display_name === 'string' ? def.display_name.trim() : '';
  return name || id;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((x): x is string => typeof x === 'string') : [];
}

function strongholdHp(def: Record<string, unknown> | undefined): number {
  const n = Number(def?.stronghold_base_health ?? def?.stronghold_health ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function ownersOf(startingSetup: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [tid, owner] of Object.entries(asRecord(startingSetup.territory_owners))) {
    if (typeof owner === 'string' && owner) out[tid] = owner;
  }
  return out;
}

/** Object keys sorted so re-adding a camp or port does not count as a change. */
function stableJson(value: unknown): string {
  return JSON.stringify(value, (_k, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
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

export function MapViewPane({
  mapAsset,
  manifest,
  territories,
  startingSetup,
  camps,
  ports,
  factions,
  units,
  onClose,
  onSave,
}: {
  mapAsset: string | undefined;
  manifest: Record<string, unknown>;
  territories: Dict;
  startingSetup: Record<string, unknown>;
  camps: Dict;
  ports: Dict;
  factions: Dict;
  units: Dict;
  onClose: () => void;
  onSave: (next: MapSetupDraft) => Promise<void>;
}) {
  const mapBase = toMapBase(mapAsset);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [view, setView] = useState<'setup' | 'graph'>('setup');
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paths, setPaths] = useState<Map<string, TerritoryPathData>>(new Map());
  const [viewBox, setViewBox] = useState({ width: 3500, height: 2600 });
  const [centroids, setCentroids] = useState<Record<string, Pt>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const dragRef = useRef<{ x: number; y: number; tx: number; ty: number; id: number } | null>(null);
  const didDragRef = useRef(false);
  const [pngFailed, setPngFailed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [edgeField, setEdgeField] = useState<TerritoryEdgeField>('adjacent');
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState<MapSetupDraft | null>(null);
  const [search, setSearch] = useState('');
  const [discardOpen, setDiscardOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const base = useMemo<MapSetupDraft>(
    () => ({ territories, starting_setup: startingSetup, camps, ports }),
    [territories, startingSetup, camps, ports],
  );
  const cur = editing && draft ? draft : base;
  const shown = cur.territories;
  const dirty = useMemo(() => draft != null && stableJson(draft) !== stableJson(base), [draft, base]);

  const update = (fn: (d: MapSetupDraft) => MapSetupDraft) => {
    setDraft((prev) => fn(prev ?? base));
    setSaveError(null);
  };

  const owners = useMemo(() => ownersOf(cur.starting_setup), [cur.starting_setup]);
  const startingUnits = useMemo(() => startingUnitsOf(cur.starting_setup), [cur.starting_setup]);

  const unitChoices = useMemo(() => {
    const rows = Object.entries(units).map(([id, def]) => ({ id, label: territoryLabel(id, def) }));
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [units]);
  const unitLabel = (id: string) => territoryLabel(id, units[id]);

  const ownerChoices = useMemo(() => {
    const rows: { id: string; label: string }[] = [];
    for (const [fid, f] of Object.entries(factions)) {
      const label = territoryLabel(fid, f);
      rows.push({ id: fid, label });
      for (const sub of Array.isArray(f.subfactions) ? f.subfactions : []) {
        const rec = asRecord(sub);
        if (typeof rec.id !== 'string' || !rec.id) continue;
        rows.push({ id: rec.id, label: `${label} › ${territoryLabel(rec.id, rec)}` });
      }
    }
    return rows;
  }, [factions]);
  const ownerLabel = (id: string) => ownerChoices.find((row) => row.id === id)?.label ?? id;

  const factionData = useMemo(() => {
    const data: Record<string, { name: string; icon: string; color: string; alliance: string; capital?: string; parent?: string }> = {};
    for (const [fid, f] of Object.entries(factions)) {
      const icon = `/assets/factions/${typeof f.icon === 'string' && f.icon ? f.icon : `${fid}.png`}`;
      const alliance = typeof f.alliance === 'string' ? f.alliance : '';
      data[fid] = {
        name: territoryLabel(fid, f),
        icon,
        color: typeof f.color === 'string' ? f.color : '#888888',
        alliance,
        capital: typeof f.capital === 'string' ? f.capital : '',
      };
      for (const sub of Array.isArray(f.subfactions) ? f.subfactions : []) {
        const rec = asRecord(sub);
        if (typeof rec.id !== 'string' || !rec.id) continue;
        data[rec.id] = {
          name: territoryLabel(rec.id, rec),
          icon: typeof rec.icon === 'string' && rec.icon.trim() ? `/assets/factions/${rec.icon.trim()}` : icon,
          color: typeof rec.color === 'string' ? rec.color : data[fid].color,
          alliance,
          capital: '',
          parent: fid,
        };
      }
    }
    return data;
  }, [factions]);

  const capitals = useMemo(
    () => new Set(Object.values(factions).map((f) => (typeof f.capital === 'string' ? f.capital : '')).filter(Boolean)),
    [factions],
  );

  const territoryData = useMemo(() => {
    const out: Record<string, {
      name: string;
      owner?: string;
      terrain: string;
      stronghold: boolean;
      stronghold_base_health: number;
      stronghold_current_health: number;
      produces: number;
      adjacent: string[];
      aerial_adjacent: string[];
      ford_adjacent: string[];
      hasCamp: boolean;
      hasPort: boolean;
      isCapital: boolean;
      ownable: boolean;
      image?: string;
    }> = {};
    for (const [tid, def] of Object.entries(cur.territories)) {
      const hp = strongholdHp(def);
      const image = typeof def.image === 'string' ? def.image.trim() : '';
      out[tid] = {
        name: territoryLabel(tid, def),
        owner: owners[tid],
        terrain: typeof def.terrain_type === 'string' ? def.terrain_type : 'land',
        stronghold: def.is_stronghold === true,
        stronghold_base_health: hp,
        stronghold_current_health: hp,
        produces: territoryPower(def),
        adjacent: stringList(def.adjacent),
        aerial_adjacent: stringList(def.aerial_adjacent),
        ford_adjacent: stringList(def.ford_adjacent),
        hasCamp: hasSiteAt(cur.camps, tid),
        hasPort: hasSiteAt(cur.ports, tid),
        isCapital: capitals.has(tid),
        ownable: def.ownable !== false,
        ...(image ? { image } : {}),
      };
    }
    return out;
  }, [cur.territories, cur.camps, cur.ports, owners, capitals]);

  const unitDefs = useMemo(() => {
    const defs: Record<string, { name: string; icon: string; faction?: string; archetype?: string; tags?: string[]; home_territory_ids?: string[]; cost?: number; transport_capacity?: number; hero_id?: string }> = {};
    for (const [id, u] of Object.entries(units)) {
      const cost = asRecord(u.cost).power;
      const heroId = typeof u.hero_id === 'string' ? u.hero_id.trim() : '';
      const homes = stringList(u.home_territory_ids);
      defs[id] = {
        name: territoryLabel(id, u),
        icon: `/assets/units/${typeof u.icon === 'string' && u.icon ? u.icon : `${id}.png`}`,
        faction: typeof u.faction === 'string' ? u.faction : undefined,
        archetype: typeof u.archetype === 'string' ? u.archetype : undefined,
        tags: stringList(u.tags),
        home_territory_ids: homes.length ? homes : undefined,
        cost: typeof cost === 'number' ? cost : typeof u.cost === 'number' ? u.cost : 0,
        transport_capacity: typeof u.transport_capacity === 'number' ? u.transport_capacity : undefined,
        ...(heroId ? { hero_id: heroId } : {}),
      };
    }
    return defs;
  }, [units]);

  const unitStats = useMemo(() => {
    const stats: Record<string, { movement: number }> = {};
    for (const [id, u] of Object.entries(units)) stats[id] = { movement: Number(u.movement) || 0 };
    return stats;
  }, [units]);

  const [navalUnitIds, riverUnitIds] = useMemo(() => {
    const naval = new Set<string>();
    const river = new Set<string>();
    for (const [id, u] of Object.entries(units)) {
      const tags = stringList(u.tags);
      if (u.archetype === 'naval' || tags.includes('naval')) naval.add(id);
      if (u.archetype === 'river' || tags.includes('river')) river.add(id);
    }
    return [naval, river];
  }, [units]);

  const rings = useMemo(() => {
    const out: RingView[] = [];
    for (const rule of Array.isArray(manifest.special_rules) ? manifest.special_rules : []) {
      const rec = asRecord(rule);
      if (rec.type !== 'rings_of_power' || !Array.isArray(rec.rings)) continue;
      for (const raw of rec.rings) {
        const ring = asRecord(raw);
        if (typeof ring.id !== 'string' || typeof ring.territory_id !== 'string') continue;
        out.push({
          id: ring.id,
          name: typeof ring.name === 'string' ? ring.name : ring.id,
          power: Number(ring.power) || 0,
          territory_id: ring.territory_id,
          bearer_hero_id: typeof ring.bearer_hero_id === 'string' ? ring.bearer_hero_id : null,
          returns_to: typeof ring.returns_to === 'string' ? ring.returns_to : null,
        });
      }
    }
    return out;
  }, [manifest]);

  const gameState = useMemo<GameState>(() => {
    const order = stringList(cur.starting_setup.turn_order);
    return {
      turn_number: 1,
      current_faction: order[0] ?? '',
      phase: 'purchase',
      territories: {},
      faction_resources: {},
      pending_purchases: {},
      pending_moves: [],
      pending_mobilizations: [],
      pending_camp_placements: [],
      declared_battles: [],
      map_asset: mapBase,
      turn_order: order,
    };
  }, [cur.starting_setup, mapBase]);

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
    void loadTerritorySvgPaths(mapBase)
      .then(({ paths: nextPaths, viewBox: vb }) => {
        if (cancelled) return;
        setPaths(nextPaths);
        setViewBox(vb);
        setCentroids(computePathCentroids(nextPaths, vb, mapBase));
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

  const toKey = (tid: string) => resolveTerritoryKey(shown, tid) ?? tid;
  const focusId = editing ? editId : selected;
  const selectedKey = focusId ? toKey(focusId) : null;

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
  const neighborPathClass = (tid: string) => {
    const isLandN = isLandNeighborTid(tid);
    const isAerialN = isAerialNeighborTid(tid);
    const isFordN = isFordNeighborTid(tid);
    if (isFordN && isAerialN) return ' admin-graph__path--neighbor-ford';
    if (isLandN && isAerialN) return ' admin-graph__path--neighbor-both';
    if (isLandN) return ' admin-graph__path--neighbor-land';
    if (isFordN) return ' admin-graph__path--neighbor-ford';
    if (isAerialN) return ' admin-graph__path--neighbor-aerial';
    return '';
  };
  const neighborNodeClass = (tid: string) => {
    if (isFordNeighborTid(tid) && isAerialNeighborTid(tid)) return 'admin-graph__node admin-graph__node--ford';
    if (isLandNeighborTid(tid)) return 'admin-graph__node admin-graph__node--land';
    if (isFordNeighborTid(tid)) return 'admin-graph__node admin-graph__node--ford';
    if (isAerialNeighborTid(tid)) return 'admin-graph__node admin-graph__node--aerial';
    return 'admin-graph__node';
  };

  const territoryChoices = useMemo(() => {
    const rows = Object.entries(territories).map(([id, def]) => ({
      id,
      label: territoryLabel(id, def),
    }));
    rows.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }) || a.id.localeCompare(b.id));
    return rows;
  }, [territories]);

  const filteredChoices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return territoryChoices;
    return territoryChoices.filter((row) => row.label.toLowerCase().includes(q) || row.id.toLowerCase().includes(q));
  }, [territoryChoices, search]);

  const editKey = editing && editId ? resolveTerritoryKey(shown, editId) : null;
  const editDef = editKey ? shown[editKey] : undefined;
  const editStacks = editKey ? stacksForTerritory(startingUnits, editKey) : [];
  const editIsStronghold = editDef?.is_stronghold === true;

  const patchTerritory = (tid: string, patch: Record<string, unknown>) =>
    update((d) => ({ ...d, territories: { ...d.territories, [tid]: { ...d.territories[tid], ...patch } } }));

  const setPower = (tid: string, power: number) =>
    patchTerritory(tid, { produces: { ...asRecord(shown[tid]?.produces), power } });

  const setStacks = (tid: string, stacks: StartingStack[]) =>
    update((d) => ({ ...d, starting_setup: startingSetupWithUnitEdits(d.starting_setup, { [tid]: stacks }) }));

  const setOwner = (tid: string, owner: string) =>
    update((d) => {
      const next = { ...asRecord(d.starting_setup.territory_owners) };
      const existing = resolveTerritoryKey(next, tid);
      if (existing) delete next[existing];
      if (owner) next[existing ?? tid] = owner;
      return { ...d, starting_setup: { ...d.starting_setup, territory_owners: next } };
    });

  const setSite = (tid: string, kind: 'camp' | 'port', on: boolean) =>
    update((d) =>
      kind === 'camp'
        ? { ...d, camps: sitesWithToggle(d.camps, tid, on, 'camp') }
        : { ...d, ports: sitesWithToggle(d.ports, tid, on, 'port') },
    );

  const setStronghold = (tid: string, on: boolean) =>
    patchTerritory(
      tid,
      on
        ? { is_stronghold: true, stronghold_base_health: Math.max(1, strongholdHp(shown[tid])), stronghold_health: undefined }
        : { is_stronghold: false },
    );

  useEffect(() => {
    if (view !== 'graph' || loading || err) return;
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
  }, [view, loading, err, viewBox.width, viewBox.height]);

  const landEdges = useMemo(() => adjacencyEdges(shown, 'adjacent'), [shown]);
  const aerialEdges = useMemo(() => adjacencyEdges(shown, 'aerial_adjacent'), [shown]);
  const fordEdges = useMemo(() => adjacencyEdges(shown, 'ford_adjacent'), [shown]);

  const toggleEdge = (source: string, neighbor: string) => {
    const currently = listsNeighbor(shown, source, neighbor, edgeField);
    const turningOn = !currently;
    const batch: TerritoryEdgeEdit[] = [];
    const add = (field: TerritoryEdgeField, on: boolean) => batch.push({ source, neighbor, field, on, seq: batch.length });
    add(edgeField, turningOn);
    if (turningOn && edgeField === 'adjacent') {
      for (const other of ['aerial_adjacent', 'ford_adjacent'] as const) {
        if (listsNeighbor(shown, source, neighbor, other) || listsNeighbor(shown, neighbor, source, other)) {
          add(other, false);
        }
      }
    } else if (turningOn && (edgeField === 'aerial_adjacent' || edgeField === 'ford_adjacent')) {
      if (listsNeighbor(shown, source, neighbor, 'adjacent') || listsNeighbor(shown, neighbor, source, 'adjacent')) {
        add('adjacent', false);
      }
    }
    update((d) => ({ ...d, territories: territoriesWithEdgeEdits(d.territories, batch) }));
  };

  const saveEdits = async () => {
    if (!dirty || !draft || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(draft);
      setDraft(null);
      setDiscardOpen(false);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const onMapSelect = (tid: string | null) => {
    if (editing) {
      if (tid) setEditId(toKey(tid));
      return;
    }
    setSelected(tid ? toKey(tid) : null);
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
      setSelected(tid ? toKey(tid) : null);
      return;
    }
    if (!tid || !editId) return;
    const source = resolveTerritoryKey(shown, editId);
    const neighbor = resolveTerritoryKey(shown, tid);
    if (!source || !neighbor || sameTerritoryId(source, neighbor)) return;
    toggleEdge(source, neighbor);
  };

  const renderEdge = (kind: 'land' | 'aerial' | 'ford', a: string, b: string, pa: Pt, pb: Pt) => {
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

  const infoText = (key: string) => {
    const def = shown[key];
    const parts = [
      territoryLabel(key, def),
      `Owner: ${owners[key] ? ownerLabel(owners[key]) : 'none'}`,
      `Power: ${territoryPower(def)}`,
    ];
    if (def?.is_stronghold === true) parts.push(`Stronghold ${strongholdHp(def)} HP`);
    if (hasSiteAt(cur.camps, key)) parts.push('Camp');
    if (hasSiteAt(cur.ports, key)) parts.push('Port');
    const stacks = stacksForTerritory(startingUnits, key);
    parts.push(`Starting units: ${stacks.map((s) => `${s.count}× ${unitLabel(s.unit_id)}`).join(', ') || 'none'}`);
    return parts.join(' · ');
  };

  const description =
    view === 'setup'
      ? editing
        ? 'Choose a territory from the list or the map, then set its owner, power, camp, port, stronghold, and starting units.'
        : 'The map as it starts on turn 1. Click a territory for its details. Scroll to zoom, drag to pan.'
      : editing
        ? 'Choose a territory, then click the map to toggle its neighbors and set its power and starting units. Land replaces aerial and ford on that border. Aerial and ford can both stay. Save writes both sides.'
        : `Landscape from ${mapBase}.png with borders from the SVG. Click a territory to highlight neighbors. Scroll to zoom, drag to pan.`;

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
          <div className="admin-graph__title-row">
            <h2 id="admin-graph-title" className="admin-modal__title">
              Map view
            </h2>
            <div className="admin-mapview__toggle" role="group" aria-label="Map view mode">
              {(['setup', 'graph'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={`admin-mapview__toggle-btn${view === mode ? ' admin-mapview__toggle-btn--active' : ''}`}
                  aria-pressed={view === mode}
                  onClick={() => setView(mode)}
                >
                  {mode === 'setup' ? 'Setup' : 'Graph'}
                </button>
              ))}
            </div>
          </div>
          {editing ? (
            <div className="admin-graph__toolbar">
              {view === 'graph' ? (
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
              {editKey ? (
                <div className="admin-graph__props">
                  {view === 'setup' ? (
                    <label className="admin-graph__prop">
                      Owner
                      <select
                        className="admin-page__select admin-graph__owner"
                        value={owners[editKey] ?? ''}
                        onChange={(e) => setOwner(editKey, e.target.value)}
                      >
                        <option value="">Unowned</option>
                        {owners[editKey] && !ownerChoices.some((row) => row.id === owners[editKey]) ? (
                          <option value={owners[editKey]}>{owners[editKey]}</option>
                        ) : null}
                        {ownerChoices.map((row) => (
                          <option key={row.id} value={row.id}>
                            {row.label}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  <label className="admin-graph__prop">
                    Power
                    <NumberField
                      min={0}
                      step={1}
                      className="admin-form__input admin-graph__power"
                      value={String(territoryPower(editDef))}
                      onChange={(e) => {
                        const raw = e.target.value.trim();
                        if (raw === '' || raw === '-') return;
                        const n = Number(raw);
                        if (!Number.isFinite(n)) return;
                        setPower(editKey, Math.max(0, Math.trunc(n)));
                      }}
                    />
                  </label>
                  {view === 'setup' ? (
                    <>
                      <label className="admin-graph__prop admin-graph__check">
                        <input
                          type="checkbox"
                          checked={hasSiteAt(cur.camps, editKey)}
                          onChange={(e) => setSite(editKey, 'camp', e.target.checked)}
                        />
                        Camp
                      </label>
                      <label className="admin-graph__prop admin-graph__check">
                        <input
                          type="checkbox"
                          checked={hasSiteAt(cur.ports, editKey)}
                          onChange={(e) => setSite(editKey, 'port', e.target.checked)}
                        />
                        Port
                      </label>
                      <label className="admin-graph__prop admin-graph__check">
                        <input
                          type="checkbox"
                          checked={editIsStronghold}
                          onChange={(e) => setStronghold(editKey, e.target.checked)}
                        />
                        Stronghold
                      </label>
                      {editIsStronghold ? (
                        <label className="admin-graph__prop">
                          HP
                          <NumberField
                            min={1}
                            step={1}
                            className="admin-form__input admin-graph__power"
                            value={String(strongholdHp(editDef))}
                            onChange={(e) => {
                              const raw = e.target.value.trim();
                              if (raw === '' || raw === '-') return;
                              const n = Number(raw);
                              if (!Number.isFinite(n)) return;
                              patchTerritory(editKey, {
                                stronghold_base_health: Math.max(1, Math.trunc(n)),
                                stronghold_health: undefined,
                              });
                            }}
                            onBlur={(e) => {
                              const n = Number(e.target.value);
                              if (!Number.isFinite(n) || n < 1) {
                                patchTerritory(editKey, {
                                  stronghold_base_health: 1,
                                  stronghold_health: undefined,
                                });
                              }
                            }}
                          />
                        </label>
                      ) : null}
                    </>
                  ) : null}
                  <div className="admin-graph__stacks" role="group" aria-label="Starting units">
                    <span className="admin-graph__prop">Units</span>
                    {editStacks.map((stack, i) => (
                      <span key={i} className="admin-graph__stack">
                        <NumberField
                          min={1}
                          step={1}
                          className="admin-form__input admin-graph__count"
                          aria-label="Unit count"
                          value={String(stack.count)}
                          onChange={(e) => {
                            const raw = e.target.value.trim();
                            if (raw === '' || raw === '-') return;
                            const n = Number(raw);
                            if (!Number.isFinite(n)) return;
                            const count = Math.max(1, Math.trunc(n));
                            setStacks(editKey, editStacks.map((s, j) => (j === i ? { ...s, count } : s)));
                          }}
                          onBlur={(e) => {
                            const n = Number(e.target.value);
                            if (!Number.isFinite(n) || n < 1) {
                              setStacks(editKey, editStacks.map((s, j) => (j === i ? { ...s, count: 1 } : s)));
                            }
                          }}
                        />
                        <select
                          className="admin-page__select admin-graph__unit"
                          aria-label="Unit"
                          value={stack.unit_id}
                          onChange={(e) =>
                            setStacks(editKey, editStacks.map((s, j) => (j === i ? { ...s, unit_id: e.target.value } : s)))
                          }
                        >
                          {units[stack.unit_id] ? null : <option value={stack.unit_id}>{stack.unit_id}</option>}
                          {unitChoices.map((u) => (
                            <option key={u.id} value={u.id}>
                              {u.label}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="admin-graph__stack-remove"
                          aria-label={`Remove ${unitLabel(stack.unit_id)}`}
                          onClick={() => setStacks(editKey, editStacks.filter((_, j) => j !== i))}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    <button
                      type="button"
                      className="admin-page__btn admin-graph__stack-add"
                      disabled={!unitChoices.length}
                      onClick={() => setStacks(editKey, [...editStacks, { unit_id: unitChoices[0]?.id ?? '', count: 1 }])}
                    >
                      + Unit
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="admin-graph__header-actions">
            {editing ? (
              <button
                type="button"
                className="admin-page__btn"
                disabled={saving || dirty}
                title={dirty ? 'Save or close to leave edit mode' : undefined}
                onClick={() => {
                  setEditing(false);
                  setDraft(null);
                  setSelected(editId);
                }}
              >
                Done
              </button>
            ) : null}
            <button
              type="button"
              className={`admin-page__btn${editing ? ' admin-page__btn--primary' : ''}`}
              disabled={saving || (editing && !dirty)}
              onClick={() => {
                if (editing) void saveEdits();
                else {
                  setEditing(true);
                  setDraft(null);
                  setEdgeField('adjacent');
                  setEditId(selected);
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
          <p className="admin-form__micro">{description}</p>
          {view === 'graph' ? (
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
          ) : null}
        </div>
        {view === 'graph' && loading ? <p className="admin-form__micro">Loading map…</p> : null}
        {view === 'graph' && err ? <p className="admin-page__error">{err}</p> : null}
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
        {view === 'setup' || (!loading && !err) ? (
          <div className="admin-graph__body">
            {editing ? (
              <div className="admin-graph__side">
                <input
                  type="search"
                  className="admin-form__input admin-graph__search"
                  placeholder="Search territories"
                  aria-label="Search territories"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <div className="admin-graph__list" role="listbox" aria-label="Territories">
                  {filteredChoices.map((row) => (
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
                  {filteredChoices.length ? null : <p className="admin-graph__list-empty">No matches</p>}
                </div>
              </div>
            ) : null}
            {view === 'setup' ? (
              <div className="admin-mapview__game">
                <GameMap
                  gameState={gameState}
                  selectedTerritory={selectedKey}
                  selectedUnit={null}
                  territoryData={territoryData}
                  territoryUnits={startingUnits}
                  rings={rings}
                  unitDefs={unitDefs}
                  unitStats={unitStats}
                  factionData={factionData}
                  onTerritorySelect={onMapSelect}
                  onUnitSelect={NOOP}
                  onUnitMove={NOOP}
                  canAct={false}
                  isMovementPhase={false}
                  isCombatMove={false}
                  isMobilizePhase={false}
                  hasMobilizationSelected={false}
                  navalUnitIds={navalUnitIds}
                  riverUnitIds={riverUnitIds}
                  pendingMoveConfirm={null}
                  onSetPendingMove={NOOP}
                  pendingMoves={NO_MOVES}
                />
              </div>
            ) : (
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
                      const cls = `admin-graph__path${isSel ? ' admin-graph__path--selected' : neighborPathClass(tid)}`;
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
                            : neighborNodeClass(tid)
                        }
                        pointerEvents="none"
                      />
                    ))}
                  </svg>
                </div>
              </div>
            )}
          </div>
        ) : null}
        {view === 'graph' ? (
          editing ? (
            <p className="admin-graph__status">
              {editKey
                ? `${territoryLabel(editKey, shown[editKey])} · ${EDGE_TYPES.find((type) => type.field === edgeField)?.label ?? 'Land'}: ${(edgeField === 'adjacent' ? landNeighborIds : edgeField === 'aerial_adjacent' ? aerialNeighborIds : fordNeighborIds).join(', ') || 'none'}`
                : 'Choose a territory'}
            </p>
          ) : selectedKey ? (
            <p className="admin-graph__status">
              {selectedKey}
              {landNeighborIds.length ? ` · adjacent: ${landNeighborIds.join(', ')}` : ''}
              {aerialNeighborIds.length ? ` · aerial: ${aerialNeighborIds.join(', ')}` : ''}
              {fordNeighborIds.length ? ` · ford: ${fordNeighborIds.join(', ')}` : ''}
              {!landNeighborIds.length && !aerialNeighborIds.length && !fordNeighborIds.length ? ' (no adjacent listed)' : ''}
            </p>
          ) : (
            <p className="admin-graph__status">No territory selected</p>
          )
        ) : null}
        {selectedKey && shown[selectedKey] ? (
          <p className="admin-graph__status admin-graph__status--info">{infoText(selectedKey)}</p>
        ) : view === 'setup' ? (
          <p className="admin-graph__status">{editing ? 'Choose a territory' : 'No territory selected'}</p>
        ) : null}
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
              <p className="admin-form__micro">Unsaved map edits will be discarded.</p>
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
