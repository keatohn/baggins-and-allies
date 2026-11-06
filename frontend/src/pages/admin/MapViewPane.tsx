import { useEffect, useMemo, useState } from 'react';
import GameMap from '../../components/GameMap';
import { api } from '../../services/api';
import type { RingView } from '../../ringsDisplay';
import type { GamePhase, GameState, MapOverridesData, PendingMove } from '../../types/game';
import { normalizeMapOverrides } from '../../types/game';
import {
  hasSiteAt,
  resolveTerritoryKey,
  sitesWithToggle,
  stacksForTerritory,
  startingSetupWithUnitEdits,
  startingUnitsOf,
  territoryPower,
  toMapBase,
  type StartingStack,
} from './territoryGraph';
import { NumberField } from './NumberField';

type Dict = Record<string, Record<string, unknown>>;

export type MapSetupDraft = {
  territories: Dict;
  starting_setup: Record<string, unknown>;
  camps: Dict;
  ports: Dict;
};

function factionAssetUrl(filename: unknown): string | undefined {
  if (typeof filename !== 'string') return undefined;
  const trimmed = filename.trim();
  return trimmed ? `/assets/factions/${trimmed}` : undefined;
}

const NOOP = () => {};
const NO_MOVES: PendingMove[] = [];

type OpeningView = {
  key: string;
  factions: {
    factionId: string;
    factionName: string;
    arrows: { from: string; to: string; pAttackerWin: number; title: string; label?: string }[];
    assumed: { from: string; to: string; pConquer: number }[];
    board: {
      owners: Record<string, string | null>;
      units: Record<string, { unit_id: string; count: number }[]>;
      strongholdHp: Record<string, number>;
    };
  }[];
};

/** Survives closing the map view, so the same setup does not simulate again. */
const openingCache = new Map<string, OpeningView>();
const OPENING_CACHE_MAX = 6;

function rememberOpenings(result: OpeningView) {
  openingCache.delete(result.key);
  openingCache.set(result.key, result);
  while (openingCache.size > OPENING_CACHE_MAX) {
    const oldest = openingCache.keys().next().value;
    if (oldest === undefined) break;
    openingCache.delete(oldest);
  }
}

function territoryLabel(id: string, def: Record<string, unknown> | undefined): string {
  const name = def && typeof def.display_name === 'string' ? def.display_name.trim() : '';
  return name || id;
}

function openingStatus(
  selected: { factionId: string; factionName: string; arrows: unknown[] },
  factions: { factionId: string; factionName: string; assumed: { to: string }[] }[],
  territories: Dict,
): string {
  const count = selected.arrows.length;
  const head = count
    ? `${selected.factionName}: ${count} opening ${count === 1 ? 'move' : 'moves'}. Hover a pill for the stacks.`
    : `${selected.factionName} has no opening move worth taking.`;
  const index = factions.findIndex((faction) => faction.factionId === selected.factionId);
  const prior = factions.slice(0, Math.max(index, 0)).flatMap((faction) =>
    faction.assumed.map((attack) => `${faction.factionName} took ${territoryLabel(attack.to, territories[attack.to])}`),
  );
  return prior.length ? `${head} Assumes ${prior.join(', ')}.` : head;
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

/** Setup edits the stronghold's base HP. A live game edits the HP it has right now. */
const GAME_PHASES: readonly GamePhase[] = ['purchase', 'combat_move', 'combat', 'non_combat_move', 'mobilize'];

function gamePhase(phase: string | undefined): GamePhase {
  if (phase === 'mobilization') return 'mobilize';
  return GAME_PHASES.find((item) => item === phase) ?? 'purchase';
}

function displayedStrongholdHp(def: Record<string, unknown> | undefined, mode: 'setup' | 'game'): number {
  if (mode === 'game' && def?.stronghold_current_health != null) {
    const n = Number(def.stronghold_current_health);
    if (Number.isFinite(n)) return n;
  }
  return strongholdHp(def);
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
  mode = 'setup',
  title = 'Map view',
  contextLine,
  saveConfirmPhrase,
  startEditing = false,
  ringsView,
  turnNumber,
  phase,
  currentFaction,
  onSaveUnits,
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
  /** setup edits the scenario. game edits one live board and confirms before saving. */
  mode?: 'setup' | 'game';
  title?: string;
  contextLine?: string;
  /** When set, Save asks the admin to type this phrase before onSave runs. */
  saveConfirmPhrase?: string;
  startEditing?: boolean;
  /** Live ring positions. When set, these replace the rings read from the manifest. */
  ringsView?: RingView[];
  turnNumber?: number;
  phase?: string;
  currentFaction?: string;
  /** Game editor only: replace this game's unit definitions. */
  onSaveUnits?: (units: Dict) => Promise<void>;
}) {
  const mapBase = toMapBase(mapAsset);
  const [view, setView] = useState<'setup' | 'openings'>('setup');
  const [selected, setSelected] = useState<string | null>(null);
  const [mapOverrides, setMapOverrides] = useState<MapOverridesData | null>(null);
  const [editing, setEditing] = useState(startEditing);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState<MapSetupDraft | null>(null);
  const [search, setSearch] = useState('');
  const [discardOpen, setDiscardOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [openings, setOpenings] = useState<OpeningView | null>(null);
  const [openingFactionId, setOpeningFactionId] = useState<string | null>(null);
  const [openingLoading, setOpeningLoading] = useState(false);
  const [openingError, setOpeningError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [unitsOpen, setUnitsOpen] = useState(false);
  const [unitsText, setUnitsText] = useState('');
  const [unitsErr, setUnitsErr] = useState<string | null>(null);
  const [unitsConfirm, setUnitsConfirm] = useState('');
  const [unitsSaving, setUnitsSaving] = useState(false);

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
    const data: Record<string, { name: string; icon: string; banner?: string; color: string; alliance: string; capital?: string; parent?: string }> = {};
    for (const [fid, f] of Object.entries(factions)) {
      const icon = `/assets/factions/${typeof f.icon === 'string' && f.icon ? f.icon : `${fid}.png`}`;
      const banner = factionAssetUrl(f.banner);
      const alliance = typeof f.alliance === 'string' ? f.alliance : '';
      data[fid] = {
        name: territoryLabel(fid, f),
        icon,
        ...(banner ? { banner } : {}),
        color: typeof f.color === 'string' ? f.color : '#888888',
        alliance,
        capital: typeof f.capital === 'string' ? f.capital : '',
      };
      for (const sub of Array.isArray(f.subfactions) ? f.subfactions : []) {
        const rec = asRecord(sub);
        if (typeof rec.id !== 'string' || !rec.id) continue;
        const subBanner = factionAssetUrl(rec.banner);
        data[rec.id] = {
          name: territoryLabel(rec.id, rec),
          icon: typeof rec.icon === 'string' && rec.icon.trim() ? `/assets/factions/${rec.icon.trim()}` : icon,
          ...(subBanner ? { banner: subBanner } : {}),
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
      const baseHp = strongholdHp(def);
      const currentHp = displayedStrongholdHp(def, mode);
      const image = typeof def.image === 'string' ? def.image.trim() : '';
      out[tid] = {
        name: territoryLabel(tid, def),
        owner: owners[tid],
        terrain: typeof def.terrain_type === 'string' ? def.terrain_type : 'land',
        stronghold: def.is_stronghold === true,
        stronghold_base_health: baseHp,
        stronghold_current_health: currentHp,
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
  }, [cur.territories, cur.camps, cur.ports, owners, capitals, mode]);

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

  const manifestRings = useMemo(() => {
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
  const rings = ringsView ?? manifestRings;

  const gameState = useMemo<GameState>(() => {
    const order = stringList(cur.starting_setup.turn_order);
    return {
      turn_number: turnNumber ?? 1,
      current_faction: currentFaction || order[0] || '',
      phase: gamePhase(phase),
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
  }, [cur.starting_setup, mapBase, turnNumber, phase, currentFaction]);

  const openingRequest = useMemo(
    () => ({
      key: stableJson({
        manifest,
        units,
        factions,
        territories: cur.territories,
        camps: cur.camps,
        ports: cur.ports,
        starting_setup: cur.starting_setup,
      }),
      body: {
        manifest,
        units,
        factions,
        territories: cur.territories,
        camps: cur.camps,
        ports: cur.ports,
        starting_setup: cur.starting_setup,
      },
    }),
    [manifest, units, factions, cur.territories, cur.camps, cur.ports, cur.starting_setup],
  );

  useEffect(() => {
    if (view !== 'openings') return;
    if (openings?.key === openingRequest.key) return;
    const cached = openingCache.get(openingRequest.key);
    if (cached) {
      setOpenings(cached);
      setOpeningLoading(false);
      setOpeningError(null);
      return;
    }
    let cancelled = false;
    setOpeningLoading(true);
    setOpeningError(null);
    const timer = window.setTimeout(() => {
      const body = openingRequest.body;
      void api
        .openingAttacks({ ...body, n_trials: 2000 })
        .then((res) => {
          if (cancelled) return;
          const nameOf = (table: Dict, id: string) => territoryLabel(id, table[id]);
          const stacksOf = (stacks: { unit_id: string; count: number }[]) =>
            stacks.map((stack) => `${stack.count}× ${nameOf(units, stack.unit_id)}`).join(', ');
          const next: OpeningView = {
            key: openingRequest.key,
            factions: res.factions.map((faction) => ({
              factionId: faction.faction_id,
              factionName: faction.faction_name || faction.faction_id,
              assumed: faction.assumed.map((attack) => ({
                from: attack.from_territory,
                to: attack.to_territory,
                pConquer: attack.p_conquer,
              })),
              board: {
                owners: faction.board.owners,
                units: faction.board.units,
                strongholdHp: faction.board.stronghold_hp ?? {},
              },
              arrows: faction.attacks.map((attack) => {
                const from = nameOf(cur.territories, attack.from_territory);
                const to = nameOf(cur.territories, attack.to_territory);
                const dest = cur.territories[attack.to_territory];
                const worth = [
                  dest?.is_stronghold === true ? 'stronghold' : '',
                  `${territoryPower(dest)} power`,
                ]
                  .filter(Boolean)
                  .join(', ');
                const detail = attack.empty
                  ? `Takes the empty territory (${worth}). ${stacksOf(attack.attacker_stacks)}`
                  : `Attack wins ${Math.round(attack.p_attacker_win * 100)}%. Conquers ${Math.round(attack.p_conquer * 100)}%. ${worth}. ${stacksOf(attack.attacker_stacks)} vs ${stacksOf(attack.defender_stacks)}`;
                return {
                  from: attack.from_territory,
                  to: attack.to_territory,
                  pAttackerWin: attack.p_attacker_win,
                  label: attack.empty ? 'Take' : undefined,
                  title: `${from} → ${to}. ${detail}${attack.is_sea_raid ? '. Sea raid.' : ''}`,
                };
              }),
            })),
          };
          rememberOpenings(next);
          setOpenings(next);
          setOpeningLoading(false);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setOpeningError(e instanceof Error ? e.message : 'Could not simulate openings');
          setOpeningLoading(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [view, openingRequest, openings, units, cur.territories]);

  const requestClose = () => {
    if (saving) return;
    if (editing && dirty) {
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

  // Map-level icon overrides: the preview must honor them so admins see what players see.
  useEffect(() => {
    let cancelled = false;
    api.getMapOverrides(mapBase)
      .then((r) => {
        if (!cancelled) setMapOverrides(normalizeMapOverrides(r?.overrides));
      })
      .catch(() => {
        if (!cancelled) setMapOverrides(null);
      });
    return () => {
      cancelled = true;
    };
  }, [mapBase]);

  const toKey = (tid: string) => resolveTerritoryKey(shown, tid) ?? tid;
  const focusId = editing ? editId : selected;
  const selectedKey = focusId ? toKey(focusId) : null;

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

  const setStronghold = (tid: string, on: boolean) => {
    if (!on) {
      patchTerritory(tid, { is_stronghold: false });
      return;
    }
    const base = Math.max(1, strongholdHp(shown[tid]));
    patchTerritory(
      tid,
      mode === 'game'
        ? {
            is_stronghold: true,
            stronghold_base_health: base,
            stronghold_current_health: Math.max(0, displayedStrongholdHp(shown[tid], mode)) || base,
            stronghold_health: undefined,
          }
        : { is_stronghold: true, stronghold_base_health: base, stronghold_health: undefined },
    );
  };


  const saveEdits = async () => {
    if (!dirty || !draft || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onSave(draft);
      setDraft(null);
      setDiscardOpen(false);
      setConfirmOpen(false);
      setConfirmText('');
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed');
      setConfirmOpen(false);
    } finally {
      setSaving(false);
    }
  };

  const openUnitsEditor = () => {
    setUnitsText(JSON.stringify(units, null, 2));
    setUnitsErr(null);
    setUnitsConfirm('');
    setUnitsOpen(true);
  };

  const saveUnits = async () => {
    if (!onSaveUnits || !saveConfirmPhrase || unitsConfirm !== saveConfirmPhrase || unitsSaving) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(unitsText);
    } catch (e) {
      setUnitsErr(e instanceof Error ? e.message : 'Invalid JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setUnitsErr('Units JSON must be a single object.');
      return;
    }
    setUnitsSaving(true);
    setUnitsErr(null);
    try {
      await onSaveUnits(parsed as Dict);
      setUnitsOpen(false);
      setUnitsConfirm('');
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Save failed';
      setUnitsErr(message.replace(/; /g, '\n'));
    } finally {
      setUnitsSaving(false);
    }
  };

  const requestSave = () => {
    if (!dirty || !draft || saving) return;
    if (saveConfirmPhrase) {
      setConfirmText('');
      setConfirmOpen(true);
      setSaveError(null);
      return;
    }
    void saveEdits();
  };

  const onMapSelect = (tid: string | null) => {
    if (editing) {
      if (tid) setEditId(toKey(tid));
      return;
    }
    setSelected(tid ? toKey(tid) : null);
  };

  const selectedOpening = useMemo(() => {
    if (view !== 'openings' || !openings || openings.key !== openingRequest.key) return null;
    return openings.factions.find((faction) => faction.factionId === openingFactionId) ?? openings.factions[0] ?? null;
  }, [view, openings, openingRequest.key, openingFactionId]);

  const openingBoard = selectedOpening?.board ?? null;
  const mapOwners = openingBoard ? openingBoard.owners : owners;
  const mapUnits = openingBoard ? openingBoard.units : startingUnits;
  const mapTerritoryData = useMemo(() => {
    if (!openingBoard) return territoryData;
    const out: typeof territoryData = {};
    for (const [tid, row] of Object.entries(territoryData)) {
      const hp = openingBoard.strongholdHp[tid];
      out[tid] = {
        ...row,
        owner: openingBoard.owners[tid] || undefined,
        ...(hp != null && row.stronghold ? { stronghold_current_health: hp } : {}),
      };
    }
    return out;
  }, [territoryData, openingBoard]);
  const mapGameState = useMemo(() => {
    if (!selectedOpening) return gameState;
    return { ...gameState, current_faction: selectedOpening.factionId };
  }, [gameState, selectedOpening]);

  const infoText = (key: string) => {
    const def = shown[key];
    const ownerId = mapOwners[key];
    const parts = [
      territoryLabel(key, def),
      `Owner: ${ownerId ? ownerLabel(ownerId) : 'none'}`,
      `Power: ${territoryPower(def)}`,
    ];
    if (def?.is_stronghold === true) {
      const projectedHp = openingBoard?.strongholdHp[key];
      parts.push(`Stronghold ${projectedHp != null ? projectedHp : displayedStrongholdHp(def, mode)} HP`);
    }
    if (hasSiteAt(cur.camps, key)) parts.push('Camp');
    if (hasSiteAt(cur.ports, key)) parts.push('Port');
    const stacks = stacksForTerritory(mapUnits, key);
    const unitWord = mode === 'game' ? 'Units' : 'Starting units';
    parts.push(`${unitWord}: ${stacks.map((s) => `${s.count}× ${unitLabel(s.unit_id)}`).join(', ') || 'none'}`);
    return parts.join(' · ');
  };

  const setupEditingCopy = mode === 'game'
    ? 'Choose a territory from the list or the map, then set its owner, power, camp, port, stronghold, and units.'
    : 'Choose a territory from the list or the map, then set its owner, power, camp, port, stronghold, and starting units.';
  const setupViewCopy = mode === 'game'
    ? 'The board as it is now. Click a territory for its details. Scroll to zoom, drag to pan.'
    : 'The map as it starts on turn 1. Click a territory for its details. Scroll to zoom, drag to pan.';
  const description = [
    contextLine,
    view === 'openings'
      ? 'Opening moves in turn order, chosen by the same MCTS search the AI uses. Each later faction is shown after the earlier factions have fought those battles. A pill is the combat sim’s chance the attack wins; Take is an empty territory.'
      : editing
        ? setupEditingCopy
        : setupViewCopy,
    mode === 'game' && editing ? `Saving asks you to type ${saveConfirmPhrase}.` : '',
  ].filter(Boolean).join(' ');

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
              {title}
            </h2>
            <div className="admin-mapview__toggle" role="group" aria-label="Map view mode">
              {([
                ['setup', mode === 'game' ? 'Board' : 'Setup'],
                ...(mode === 'game' ? [] : [['openings', 'Openings']]),
              ] as ['setup' | 'openings', string][]).map(([viewMode, label]) => (
                <button
                  key={viewMode}
                  type="button"
                  className={`admin-mapview__toggle-btn${view === viewMode ? ' admin-mapview__toggle-btn--active' : ''}`}
                  aria-pressed={view === viewMode}
                  onClick={() => setView(viewMode)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {editing ? (
            <div className="admin-graph__toolbar">
              {editKey ? (
                <div className="admin-graph__props">
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
                            min={mode === 'game' ? 0 : 1}
                            step={1}
                            className="admin-form__input admin-graph__power"
                            value={String(displayedStrongholdHp(editDef, mode))}
                            onChange={(e) => {
                              const raw = e.target.value.trim();
                              if (raw === '' || raw === '-') return;
                              const n = Number(raw);
                              if (!Number.isFinite(n)) return;
                              const hp = Math.max(mode === 'game' ? 0 : 1, Math.trunc(n));
                              patchTerritory(
                                editKey,
                                mode === 'game'
                                  ? { stronghold_current_health: hp }
                                  : { stronghold_base_health: hp, stronghold_health: undefined },
                              );
                            }}
                            onBlur={(e) => {
                              const n = Number(e.target.value);
                              const floor = mode === 'game' ? 0 : 1;
                              if (!Number.isFinite(n) || n < floor) {
                                patchTerritory(
                                  editKey,
                                  mode === 'game'
                                    ? { stronghold_current_health: floor }
                                    : { stronghold_base_health: 1, stronghold_health: undefined },
                                );
                              }
                            }}
                          />
                        </label>
                      ) : null}
                    </>
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
            {mode === 'game' && onSaveUnits ? (
              <button type="button" className="admin-page__btn" onClick={openUnitsEditor} disabled={saving || unitsSaving}>
                Edit Units
              </button>
            ) : null}
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
                if (editing) requestSave();
                else {
                  setEditing(true);
                  setDraft(null);
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
        </div>
        {saveError ? <p className="admin-page__error">{saveError}</p> : null}
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
              <div className="admin-mapview__game">
                {view === 'openings' && openings && openings.key === openingRequest.key && openings.factions.length ? (
                  <div className="admin-openings__factions" role="tablist" aria-label="Opening faction">
                    {openings.factions.map((faction) => {
                      const art = factionData[faction.factionId];
                      const selected = faction.factionId === selectedOpening?.factionId;
                      return (
                        <button
                          key={faction.factionId}
                          type="button"
                          role="tab"
                          aria-selected={selected}
                          className={`admin-openings__faction${selected ? ' admin-openings__faction--selected' : ''}`}
                          title={faction.factionName}
                          onClick={() => setOpeningFactionId(faction.factionId)}
                        >
                          <span
                            className="faction-seal"
                            style={art?.color ? { borderColor: art.color } : undefined}
                          >
                            {art?.icon ? <img src={art.icon} alt="" /> : null}
                          </span>
                          <span className="admin-openings__faction-name">{faction.factionName}</span>
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <GameMap
                  gameState={mapGameState}
                  selectedTerritory={selectedKey}
                  selectedUnit={null}
                  territoryData={mapTerritoryData}
                  territoryUnits={mapUnits}
                  rings={rings}
                  unitDefs={unitDefs}
                  unitStats={unitStats}
                  factionData={factionData}
                  mapOverrides={mapOverrides}
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
                  openingAttacks={view === 'openings' && selectedOpening ? selectedOpening.arrows : []}
                />
              </div>
          </div>
        {view === 'openings' ? (
          <p className="admin-graph__status">
            {openingError
              ? openingError
              : !selectedOpening || openingLoading
                ? 'Simulating opening attacks…'
                : openingStatus(selectedOpening, openings?.factions ?? [], cur.territories)}
          </p>
        ) : null}
        {selectedKey && shown[selectedKey] ? (
          <p className="admin-graph__status admin-graph__status--info">{infoText(selectedKey)}</p>
        ) : (
          <p className="admin-graph__status">{editing ? 'Choose a territory' : 'No territory selected'}</p>
        )}
        {unitsOpen && saveConfirmPhrase ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation">
            <div
              className="admin-modal admin-modal--unit-json"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-edit-units-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-edit-units-title" className="admin-modal__title">
                Edit units
              </h2>
              <p className="admin-form__micro">
                Unit definitions for this game. Type <strong>{saveConfirmPhrase}</strong> to save.
              </p>
              <textarea
                className="admin-page__editor"
                spellCheck={false}
                aria-label="Units JSON"
                value={unitsText}
                onChange={(e) => {
                  setUnitsText(e.target.value);
                  setUnitsErr(null);
                }}
              />
              <div className="admin-form__row">
                <label className="admin-form__label" htmlFor="admin-edit-units-confirm">
                  Confirmation
                </label>
                <input
                  id="admin-edit-units-confirm"
                  className="admin-form__input"
                  value={unitsConfirm}
                  onChange={(e) => setUnitsConfirm(e.target.value)}
                  placeholder={saveConfirmPhrase}
                />
              </div>
              {unitsErr ? <p className="admin-page__error admin-page__error--pre">{unitsErr}</p> : null}
              <div className="admin-modal__actions">
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={() => setUnitsOpen(false)}
                  disabled={unitsSaving}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  onClick={() => void saveUnits()}
                  disabled={unitsConfirm !== saveConfirmPhrase || unitsSaving}
                >
                  {unitsSaving ? 'Saving…' : 'Save units'}
                </button>
              </div>
            </div>
          </div>
        ) : null}
        {confirmOpen && saveConfirmPhrase ? (
          <div className="admin-modal-overlay admin-graph__confirm" role="presentation">
            <div
              className="admin-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="admin-graph-confirm-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="admin-graph-confirm-title" className="admin-modal__title">
                Save this game?
              </h2>
              <p className="admin-form__micro">
                This writes the board of the game in progress. Type <strong>{saveConfirmPhrase}</strong> to confirm.
              </p>
              <div className="admin-form__row">
                <label className="admin-form__label" htmlFor="admin-edit-game-confirm">
                  Confirmation
                </label>
                <input
                  id="admin-edit-game-confirm"
                  className="admin-form__input"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={saveConfirmPhrase}
                  autoFocus
                />
              </div>
              <div className="admin-modal__actions">
                <button
                  type="button"
                  className="admin-page__btn"
                  onClick={() => setConfirmOpen(false)}
                  disabled={saving}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="admin-page__btn admin-page__btn--danger"
                  onClick={() => void saveEdits()}
                  disabled={confirmText !== saveConfirmPhrase || saving}
                >
                  {saving ? 'Saving…' : 'Save game'}
                </button>
              </div>
            </div>
          </div>
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
