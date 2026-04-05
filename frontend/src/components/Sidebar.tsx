import { useState, useMemo } from 'react';
import type { GameState, GamePhase, GameEvent, DeclaredBattle } from '../types/game';
import type { PendingMoveConfirm } from './GameMap';
import type { BulkMoveConfirmState, PendingMobilization, BulkMobilizeConfirmState } from '../App';
import './Sidebar.css';

interface SidebarProps {
  /** When false (e.g. not your turn in multiplayer), the Actions panel is hidden; territory select and event log still show. */
  canAct?: boolean;
  /** When true, phase action buttons (Purchase Units, End Phase) are hidden. */
  gameOver?: boolean;
  gameState: GameState;
  selectedTerritory: string | null;
  territoryData: Record<string, {
    name: string;
    owner?: string;
    terrain: string;
    stronghold: boolean;
    produces: number;
    adjacent: string[];
    hasCamp?: boolean;
    hasPort?: boolean;
    isCapital?: boolean;
    ownable?: boolean;
    stronghold_base_health?: number;
    stronghold_current_health?: number;
  }>;
  territoryUnits: Record<string, { unit_id: string; count: number }[]>;
  /** When non-combat move and a territory is selected: stacks grouped by (unit_id, remaining_movement) for that territory */
  territoryUnitStacksWithMovement?: { unit_id: string; remaining_movement: number; count: number }[] | null;
  unitDefs: Record<string, {
    name: string;
    icon: string;
    faction?: string;
    home_territory_ids?: string[];
    cost?: number;
    archetype?: string;
    tags?: string[];
  }>;
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string; capital?: string }>;
  eventLog: GameEvent[];
  onEndPhase: () => void;
  onOpenPurchase: () => void;
  onInitiateCombat?: (battle: DeclaredBattle) => void;
  pendingEndPhaseConfirm?: string | null;
  hasPurchaseCart?: boolean;
  /** Combat phase: cannot end while battles remain */
  endPhaseDisabled?: boolean;
  endPhaseDisabledReason?: string;
  onConfirmEndPhase?: () => void;
  onCancelEndPhase?: () => void;
  pendingMoveConfirm?: PendingMoveConfirm | null;
  onUpdateMoveCount?: (count: number) => void;
  onConfirmMove?: () => void;
  onCancelMove?: () => void;
  onChooseChargePath?: (path: string[]) => void;
  /** When sea raid and land is adjacent to multiple sea zones: pick which sea zone to conduct the raid from (after Confirm Sea Raid). */
  onChooseSeaRaidSeaZone?: (seaZoneId: string) => void;
  /** When user clicks Confirm Sea Raid but multiple sea zones exist: show zone picker (do not submit yet). */
  onRequestSeaRaidZoneChoice?: () => void;
  /** When backend returns need_offload_sea_choice: user must pick which sea zone to sail to. */
  pendingOffloadSeaChoice?: { from: string; to: string; unitInstanceIds: string[]; validSeaZones: string[] } | null;
  onChooseOffloadSeaZone?: (seaZoneId: string) => void;
  onCancelOffloadSeaChoice?: () => void;
  /** Bulk "All" drag: summary confirm (no per-stack +/-); submit adds one pending move per stack. */
  bulkMoveConfirm?: BulkMoveConfirmState | null;
  onConfirmBulkMove?: () => void;
  onCancelBulkMove?: () => void;
  onCancelPendingMove?: (moveId: string) => void;
  pendingMobilization?: PendingMobilization | null;
  onUpdateMobilizationCount?: (count: number) => void;
  onConfirmMobilization?: () => void;
  onCancelMobilization?: () => void;
  bulkMobilizeConfirm?: BulkMobilizeConfirmState | null;
  onConfirmBulkMobilize?: () => void;
  onCancelBulkMobilize?: () => void;
  battlesCompletedThisPhase?: number;
  /** Number of combat moves declared when entering combat phase (so we can distinguish "no battles" vs "all uncontested"). */
  combatMovesDeclaredThisPhase?: number;
  pendingRetreat?: { territory: string } | null;
  validRetreatDestinations?: string[];
  onConfirmRetreat?: (destinationId: string) => void;
  onCancelRetreat?: () => void;
  /** Mobilization: true when there are purchased units not yet deployed (used to show phase instruction). */
  hasUnmobilizedPurchases?: boolean;
  /** Pending mobilizations this phase; show list with cancel X. */
  pendingMobilizations?: GameState['pending_mobilizations'];
  onCancelPendingMobilization?: (mobilizationIndex: number) => void;
  /** Pending camp placement (drag or click); confirm adds to queue. */
  pendingCampPlacement?: { campIndex: number; territoryId: string } | null;
  onConfirmCampPlacement?: () => void;
  onCancelCampPlacement?: () => void;
  /** Queued camp placements (from backend; applied at end of phase). */
  pendingCampPlacements?: { camp_index: number; territory_id: string }[];
  onCancelQueuedCampPlacement?: (placementIndex: number) => void;
  /** Non-combat move: aerial units that must move to friendly territory before phase can end. */
  aerialUnitsMustMove?: { territory_id: string; unit_id: string; instance_id: string }[];
  /** Defender casualty order per territory (from backend). Shown when selected territory is owned by current faction. */
  territoryDefenderCasualtyOrder?: Record<string, string>;
  /** Set defender casualty order for a territory (owner only). */
  onSetTerritoryDefenderCasualtyOrder?: (territoryId: string, casualtyOrder: 'best_unit' | 'best_defense') => void;
  /** When !canAct and phase is combat: which battle is currently in progress (territory_id; optional sea_zone_id for sea raids). */
  activeCombatTerritoryId?: string | null;
  /** Optional sea_zone_id of active combat (for sea raids). */
  activeCombatSeaZoneId?: string | null;
  /** Spectator clicks a battle to view it (only active battle is openable). */
  onSpectateBattle?: (battle: DeclaredBattle) => void;
  /** When true, current faction is AI; hide spectate battles panel during combat (no need to pick which AI battle to view). */
  isCurrentFactionAI?: boolean;
  /** Combat move: sea zones with a mobilization naval standoff (fight next phase or sail away). */
  forcedNavalStandoffSeaZoneIds?: string[];
  /** Current faction color: tints Actions / Territory / Event log panel borders only. */
  turnAccentColor?: string;
}

// Phase-specific action configurations
// Note: collect_income happens automatically at end of turn, not a visible UI phase
const PHASE_CONFIG: Record<GamePhase, { buttons: { id: string; label: string }[] }> = {
  purchase: {
    buttons: [{ id: 'btn-purchase', label: 'Purchase Units' }],
  },
  combat_move: {
    buttons: [],
  },
  combat: {
    buttons: [],
  },
  non_combat_move: {
    buttons: [],
  },
  mobilize: {
    buttons: [],
  },
};

function formatPhase(phase: string): string {
  if (phase === 'non_combat_move') return 'Non-Combat Move';
  if (phase === 'mobilization' || phase === 'mobilize') return 'Mobilization';
  return phase
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/** Backend / state phase ids; order matches game flow (never alphabetical). */
const EVENT_LOG_PHASE_ORDER = [
  'purchase',
  'combat_move',
  'combat',
  'non_combat_move',
  'mobilization',
] as const;

/** Single canonical id per phase for filters (avoids mobilize vs mobilization duplicates and stray spacing). */
function canonicalEventLogPhase(phase: string): string {
  let s = String(phase).trim().toLowerCase().replace(/[\s-]+/g, '_');
  if (s === 'mobilize') s = 'mobilization';
  return s;
}

/** "Isengard Purchase 2: …" — uses event payload when present, else current game state. */
function formatEventLogEntryLine(
  event: GameEvent,
  gameState: GameState,
  factionData: Record<string, { name: string }>,
): string {
  const p = event.payload;
  const factionId =
    typeof p?.faction === 'string' && p.faction.trim()
      ? p.faction.trim()
      : gameState.current_faction;
  const phaseKey =
    typeof p?.phase === 'string' && p.phase.trim()
      ? canonicalEventLogPhase(p.phase)
      : canonicalEventLogPhase(gameState.phase);
  const turn =
    typeof p?.turn_number === 'number' && Number.isFinite(p.turn_number)
      ? p.turn_number
      : gameState.turn_number;

  const factionName = (factionId && factionData[factionId]?.name) || factionId || '—';
  const phaseLabel = formatPhase(phaseKey);
  return `${factionName} ${phaseLabel} ${turn}: ${event.message}`;
}

/** Phases present in the log, listed in turn order — not sorted by string (so combat_move never appears before purchase). */
function phasesForEventLogFilter(eventLog: GameEvent[]): string[] {
  const fromLog = new Set<string>();
  eventLog.forEach((e) => {
    const x = e.payload?.phase;
    if (typeof x === 'string' && x.trim()) fromLog.add(canonicalEventLogPhase(x));
  });
  const ordered = EVENT_LOG_PHASE_ORDER.filter((p) => fromLog.has(p));
  const unknown = [...fromLog].filter(
    (p) => !EVENT_LOG_PHASE_ORDER.includes(p as (typeof EVENT_LOG_PHASE_ORDER)[number])
  );
  unknown.sort((a, b) => a.localeCompare(b));
  return [...ordered, ...unknown];
}

function sortEventLogFactions(factionIds: string[], turnOrder: string[] | undefined): string[] {
  const idx = (id: string) => {
    const i = turnOrder?.indexOf(id) ?? -1;
    return i === -1 ? 999 : i;
  };
  return [...factionIds].sort((a, b) => idx(a) - idx(b) || a.localeCompare(b));
}

/** True if this sea hex has enemy-alliance naval units (combat_move sea→sea = naval attack, not sail). */
function destinationSeaHasHostileEnemyNaval(
  seaTerritoryId: string,
  currentFaction: string,
  territoryUnits: Record<string, { unit_id: string; count: number }[]>,
  unitDefs: Record<string, { faction?: string; archetype?: string; tags?: string[] }>,
  factionData: Record<string, { alliance?: string }>,
): boolean {
  const stacks = territoryUnits[seaTerritoryId] ?? [];
  const ourAlliance = factionData[currentFaction]?.alliance ?? '';
  for (const s of stacks) {
    if ((s.count ?? 0) <= 0) continue;
    const def = unitDefs[s.unit_id];
    if (!def) continue;
    const naval =
      def.archetype === 'naval' ||
      (Array.isArray(def.tags) && def.tags.includes('naval'));
    if (!naval) continue;
    const uf = def.faction;
    if (!uf || uf === currentFaction) continue;
    const theirAlliance = factionData[uf]?.alliance;
    if (theirAlliance == null || theirAlliance === '') return true;
    if (theirAlliance !== ourAlliance) return true;
  }
  return false;
}

/** Aerial land→sea in combat is always attacking enemy naval, never embark (transportable land units still "Load" in combat for sea raids). */
function unitIsAerial(
  unitId: string,
  unitDefs: Record<string, { archetype?: string; tags?: string[] } | undefined>,
): boolean {
  const d = unitDefs[unitId];
  return d?.archetype === 'aerial' || (d != null && Array.isArray(d.tags) && d.tags.includes('aerial'));
}

/** Planned Attacks list: never show "Load" for aerial into sea (backend may still send move_type load on older pending rows). */
function plannedCombatMoveTypeLabel(
  move: { move_type?: string | null; from: string; to: string; unitType: string },
  territoryData: Record<string, { terrain?: string } | undefined>,
  unitDefs: Record<string, { archetype?: string; tags?: string[] } | undefined>,
): string | null {
  const fromSea =
    territoryData[move.from]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.from);
  const toSea = territoryData[move.to]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.to);
  if (fromSea && !toSea) {
    if (unitIsAerial(move.unitType, unitDefs)) {
      const mtEarly = (move.move_type ?? '').trim();
      return mtEarly === 'aerial' ? 'Attack' : 'Move';
    }
    return 'Sea Raid';
  }
  const mt = (move.move_type ?? '').trim();
  if (!mt) return null;
  if (mt === 'aerial' || (mt === 'load' && !fromSea && toSea && unitIsAerial(move.unitType, unitDefs))) {
    return 'Attack';
  }
  return mt.charAt(0).toUpperCase() + mt.slice(1);
}

/** Planned Moves (NCM): never label aerial sea→land as Offload (backend may have stored offload before fix). */
function plannedNonCombatMoveTypeLabel(
  move: { move_type?: string | null; from: string; to: string; unitType: string },
  territoryData: Record<string, { terrain?: string } | undefined>,
  unitDefs: Record<string, { archetype?: string; tags?: string[] } | undefined>,
): string | null {
  const fromSea =
    territoryData[move.from]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.from);
  const toSea = territoryData[move.to]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.to);
  if (fromSea && !toSea && unitIsAerial(move.unitType, unitDefs)) {
    return 'Move';
  }
  const mt = (move.move_type ?? '').trim();
  if (!mt) return null;
  if (mt === 'aerial') return 'Move';
  return mt.charAt(0).toUpperCase() + mt.slice(1);
}

function Sidebar({
  canAct = true,
  gameOver = false,
  gameState,
  selectedTerritory,
  territoryData,
  territoryUnits,
  territoryUnitStacksWithMovement = null,
  unitDefs,
  factionData,
  eventLog,
  onEndPhase,
  onOpenPurchase,
  onInitiateCombat,
  pendingEndPhaseConfirm,
  hasPurchaseCart,
  endPhaseDisabled,
  endPhaseDisabledReason,
  onConfirmEndPhase,
  onCancelEndPhase,
  pendingMoveConfirm,
  onUpdateMoveCount,
  onConfirmMove,
  onCancelMove,
  onChooseChargePath,
  onChooseSeaRaidSeaZone,
  onRequestSeaRaidZoneChoice: _onRequestSeaRaidZoneChoice,
  pendingOffloadSeaChoice,
  onChooseOffloadSeaZone,
  onCancelOffloadSeaChoice,
  bulkMoveConfirm,
  onConfirmBulkMove,
  onCancelBulkMove,
  onCancelPendingMove,
  bulkMobilizeConfirm,
  onConfirmBulkMobilize,
  onCancelBulkMobilize,
  pendingMobilization,
  onUpdateMobilizationCount,
  onConfirmMobilization,
  onCancelMobilization,
  battlesCompletedThisPhase = 0,
  combatMovesDeclaredThisPhase = 0,
  pendingRetreat,
  validRetreatDestinations = [],
  onConfirmRetreat,
  onCancelRetreat,
  hasUnmobilizedPurchases = false,
  pendingMobilizations = [],
  onCancelPendingMobilization,
  pendingCampPlacement = null,
  onConfirmCampPlacement,
  onCancelCampPlacement,
  pendingCampPlacements = [],
  onCancelQueuedCampPlacement,
  aerialUnitsMustMove = [],
  territoryDefenderCasualtyOrder = {},
  onSetTerritoryDefenderCasualtyOrder,
  activeCombatTerritoryId = null,
  activeCombatSeaZoneId = null,
  onSpectateBattle,
  isCurrentFactionAI = false,
  forcedNavalStandoffSeaZoneIds = [],
  turnAccentColor,
}: SidebarProps) {
  const territory = selectedTerritory ? territoryData[selectedTerritory] : null;
  const units = selectedTerritory ? territoryUnits[selectedTerritory] || [] : [];
  const phaseConfig = PHASE_CONFIG[gameState.phase] || { buttons: [] };
  const owner = territory?.owner;
  const ownerData = owner ? factionData[owner] : null;

  /** Naval battle in a sea zone must resolve before a sea raid that stages from that same zone. */
  const { sortedDeclaredBattles, isSeaRaidBlockedByPendingNaval } = useMemo(() => {
    const battles = gameState.declared_battles || [];
    const isSea = (tid: string) => {
      const t = territoryData[tid];
      return t?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(tid);
    };
    const navalSeaZones = new Set<string>();
    for (const b of battles) {
      if (!b.sea_zone_id && isSea(b.territory)) {
        navalSeaZones.add(b.territory);
      }
    }
    const blocked = (b: DeclaredBattle) =>
      Boolean(b.sea_zone_id && navalSeaZones.has(b.sea_zone_id));
    const priority = (b: DeclaredBattle): number => {
      if (!b.sea_zone_id && isSea(b.territory)) {
        return 0;
      }
      return blocked(b) ? 2 : 1;
    };
    const sorted = [...battles].sort(
      (a, b) => priority(a) - priority(b) || a.territory.localeCompare(b.territory),
    );
    return { sortedDeclaredBattles: sorted, isSeaRaidBlockedByPendingNaval: blocked };
  }, [gameState.declared_battles, territoryData]);

  const [eventLogTurn, setEventLogTurn] = useState<number | ''>('');
  const [eventLogFaction, setEventLogFaction] = useState<string>('');
  const [eventLogPhase, setEventLogPhase] = useState<string>('');
  const eventLogTurns = useMemo(() => {
    const turns = new Set<number>();
    eventLog.forEach(e => { const n = e.payload?.turn_number; if (typeof n === 'number') turns.add(n); });
    return Array.from(turns).sort((a, b) => a - b);
  }, [eventLog]);
  const eventLogFactions = useMemo(() => {
    const f = new Set<string>();
    eventLog.forEach(e => { const x = e.payload?.faction; if (typeof x === 'string' && x) f.add(x); });
    return sortEventLogFactions(Array.from(f), gameState.turn_order);
  }, [eventLog, gameState.turn_order]);
  const eventLogPhases = useMemo(() => phasesForEventLogFilter(eventLog), [eventLog]);
  const filteredEventLog = useMemo(() => {
    return eventLog.filter(e => {
      if (e.payload?.debug_only) return false;
      if (eventLogTurn !== '' && (e.payload?.turn_number ?? null) !== eventLogTurn) return false;
      if (eventLogFaction !== '' && (e.payload?.faction ?? '') !== eventLogFaction) return false;
      if (eventLogPhase !== '' && canonicalEventLogPhase(String(e.payload?.phase ?? '')) !== eventLogPhase) return false;
      return true;
    });
  }, [eventLog, eventLogTurn, eventLogFaction, eventLogPhase]);

  // Purchase is disabled when current faction's capital is captured
  const currentFactionCapital = factionData[gameState.current_faction]?.capital;
  const capitalOwner = currentFactionCapital ? territoryData[currentFactionCapital]?.owner : undefined;
  const capitalCaptured = !!currentFactionCapital && capitalOwner !== gameState.current_faction;

  return (
    <aside
      className={`sidebar${turnAccentColor ? ' sidebar--turn-accent' : ''}`}
      style={turnAccentColor ? { ['--sidebar-panel-accent' as string]: turnAccentColor } : undefined}
    >
      {/* Actions Panel — only when it's this player's turn (canAct) */}
      {canAct && (
        <div className="panel actions-panel">
          <h2>Actions</h2>
          <div className="phase-actions">
            {gameOver ? (
              <p className="phase-instruction">Game over.</p>
            ) : gameState.phase === 'purchase' && capitalCaptured ? (
              <p className="phase-instruction">Cannot purchase units until capital is liberated.</p>
            ) : (
              phaseConfig.buttons.map(btn => (
                <button
                  key={btn.id}
                  id={btn.id}
                  onClick={btn.id === 'btn-purchase' ? onOpenPurchase : undefined}
                >
                  {btn.label}
                </button>
              ))
            )}

            {/* Backend asked for offload sea zone choice (multiple valid sea zones) */}
            {pendingOffloadSeaChoice && pendingOffloadSeaChoice.validSeaZones.length > 0 && (
              <div className="move-confirm">
                <h3>{gameState.phase === 'non_combat_move' ? 'Offload' : 'Sea Raid'}</h3>
                <p className="move-details">
                  <span className="move-route">
                    {territoryData[pendingOffloadSeaChoice.from]?.name || pendingOffloadSeaChoice.from} → {territoryData[pendingOffloadSeaChoice.to]?.name || pendingOffloadSeaChoice.to}
                  </span>
                </p>
                <p className="charge-path-prompt">Choose which sea zone to sail to (then offload):</p>
                <div className="charge-path-options">
                  {pendingOffloadSeaChoice.validSeaZones.map((seaZoneId) => (
                    <button
                      key={seaZoneId}
                      type="button"
                      className="charge-path-btn"
                      onClick={() => onChooseOffloadSeaZone?.(seaZoneId)}
                    >
                      {territoryData[seaZoneId]?.name || seaZoneId}
                    </button>
                  ))}
                </div>
                <button type="button" className="cancel-move-btn" onClick={onCancelOffloadSeaChoice}>Cancel</button>
              </div>
            )}

            {/* Bulk "All": same chrome as single-unit confirm (no per-stack list) */}
            {bulkMoveConfirm && !pendingOffloadSeaChoice && (
              <div className="move-confirm">
                {(() => {
                  const isAttack = gameState.phase === 'combat_move';
                  const confirmTitle = isAttack ? 'Confirm Attack' : 'Confirm Move';
                  const buttonLabel = isAttack ? 'Attack' : 'Move';
                  const confirmBtnClass = isAttack ? 'confirm-move-btn attack-btn' : 'confirm-move-btn';
                  return (
                    <>
                      <h3>{confirmTitle}</h3>
                      <p className="move-details">
                        <span className="unit-name">All units</span>
                        <br />
                        <span className="move-route">
                          {territoryData[bulkMoveConfirm.fromTerritory]?.name || bulkMoveConfirm.fromTerritory}
                          {' → '}
                          {territoryData[bulkMoveConfirm.toTerritory]?.name || bulkMoveConfirm.toTerritory}
                        </span>
                      </p>
                      <div className="move-confirm-buttons">
                        <button
                          type="button"
                          className={confirmBtnClass}
                          onClick={() => onConfirmBulkMove?.()}
                        >
                          {buttonLabel}
                        </button>
                        <button type="button" className="cancel-move-btn" onClick={() => onCancelBulkMove?.()}>
                          Cancel
                        </button>
                      </div>
                    </>
                  );
                })()}
              </div>
            )}

            {/* Pending move confirmation with +/- controls */}
            {pendingMoveConfirm && !pendingOffloadSeaChoice && !bulkMoveConfirm && (
              <div className="move-confirm">
                {pendingMoveConfirm.chargePathOptions && pendingMoveConfirm.chargePathOptions.length > 1 ? (
                  <>
                    <h3>Charge Through</h3>
                    <p className="move-details">
                      <span className="unit-name">{pendingMoveConfirm.unitDef?.name || pendingMoveConfirm.unitId}</span>
                    </p>
                    <p className="charge-path-prompt">Choose route:</p>
                    <div className="charge-path-options">
                      {(() => {
                        const toId = pendingMoveConfirm!.toTerritory;
                        const fromId = pendingMoveConfirm!.fromTerritory;
                        const fromAdjacent = territoryData[fromId]?.adjacent?.includes(toId);
                        const seen = new Set<string>();
                        return pendingMoveConfirm.chargePathOptions!
                          .map((path) => path.filter((tid) => tid !== toId))
                          .filter((path) => {
                            if (path.length > 0) return true;
                            return !!fromAdjacent;
                          })
                          .filter((path) => {
                            const key = JSON.stringify(path);
                            if (seen.has(key)) return false;
                            seen.add(key);
                            return true;
                          })
                          .map((path, idx) => (
                            <button
                              key={idx}
                              type="button"
                              className="charge-path-btn"
                              onClick={() => onChooseChargePath?.(path)}
                            >
                              {path.length === 0
                                ? 'Direct'
                                : `Via ${path.map(tid => territoryData[tid]?.name || tid).join(', ')}`}
                            </button>
                          ));
                      })()}
                    </div>
                    <button className="cancel-move-btn" onClick={onCancelMove}>Cancel</button>
                  </>
                ) : (() => {
                  const fromTerrain = territoryData[pendingMoveConfirm.fromTerritory]?.terrain;
                  const toTerrain = territoryData[pendingMoveConfirm.toTerritory]?.terrain;
                  const fromSea = fromTerrain === 'sea' || /^sea_zone_?\d+$/i.test(pendingMoveConfirm.fromTerritory);
                  const toSea = toTerrain === 'sea' || /^sea_zone_?\d+$/i.test(pendingMoveConfirm.toTerritory);
                  const isAerialUnit = unitIsAerial(pendingMoveConfirm.unitId, unitDefs);
                  const isLoad = !fromSea && toSea && !isAerialUnit;
                  const isOffload =
                    fromSea && !toSea && gameState.phase === 'non_combat_move' && !isAerialUnit;
                  const toIdRaw =
                    typeof pendingMoveConfirm.toTerritory === 'string'
                      ? pendingMoveConfirm.toTerritory.trim()
                      : '';
                  const combatNavalAttack =
                    gameState.phase === 'combat_move' &&
                    fromSea &&
                    toSea &&
                    Boolean(toIdRaw) &&
                    destinationSeaHasHostileEnemyNaval(
                      toIdRaw,
                      gameState.current_faction,
                      territoryUnits,
                      unitDefs,
                      factionData,
                    );
                  const isSail = fromSea && toSea && !combatNavalAttack;
                  const isSeaRaid = fromSea && !toSea && gameState.phase === 'combat_move' && !isAerialUnit;
                  const multipleSeaZones = (isSeaRaid || isOffload) && (pendingMoveConfirm.seaRaidSeaZoneOptions?.length ?? 0) > 1;
                  return multipleSeaZones ? (
                    <>
                      <h3>{gameState.phase === 'non_combat_move' ? 'Offload' : 'Sea Raid'}</h3>
                      <p className="move-details">
                        <span className="unit-name">{pendingMoveConfirm.unitDef?.name || pendingMoveConfirm.unitId}</span>
                        <br />
                        <span className="move-route">
                          → {territoryData[pendingMoveConfirm.toTerritory]?.name || pendingMoveConfirm.toTerritory}
                        </span>
                      </p>
                      <p className="charge-path-prompt">
                        {gameState.phase === 'non_combat_move' ? 'Choose which sea zone to sail to (then offload):' : 'Choose which sea zone to conduct the raid from:'}
                      </p>
                      <div className="charge-path-options">
                        {pendingMoveConfirm.seaRaidSeaZoneOptions!.map((seaZoneId) => (
                          <button
                            key={seaZoneId}
                            type="button"
                            className="charge-path-btn"
                            onClick={() => onChooseSeaRaidSeaZone?.(seaZoneId)}
                          >
                            {territoryData[seaZoneId]?.name || seaZoneId}
                          </button>
                        ))}
                      </div>
                      <button className="cancel-move-btn" onClick={onCancelMove}>Cancel</button>
                    </>
                  ) : (
                    <>
                      {(() => {
                        const isAttack =
                          gameState.phase === 'combat_move' &&
                          !isLoad &&
                          ((!fromSea && !toSea) ||
                            (fromSea && !toSea) ||
                            (!fromSea && toSea) ||
                            combatNavalAttack);
                        const isNavalMove = isLoad || isOffload || isSail;
                        const confirmTitle = isLoad ? 'Confirm Load' : isOffload ? 'Confirm Offload' : isSail ? 'Confirm Sail' : isSeaRaid ? 'Confirm Sea Raid' : isAttack ? 'Confirm Attack' : 'Confirm Move';
                        const buttonLabel = isLoad ? 'Load' : isOffload ? 'Offload' : isSail ? 'Sail' : isSeaRaid ? 'Sea Raid' : isAttack ? 'Attack' : 'Move';
                        const confirmBtnClass = isAttack
                          ? 'confirm-move-btn attack-btn'
                          : isNavalMove
                            ? 'confirm-move-btn naval-move-btn'
                            : 'confirm-move-btn';
                        const chosenZoneName = (isSeaRaid || isOffload) && pendingMoveConfirm.chosenSeaZoneId
                          ? territoryData[pendingMoveConfirm.chosenSeaZoneId]?.name || pendingMoveConfirm.chosenSeaZoneId
                          : null;
                        return (
                          <>
                            <h3>{confirmTitle}</h3>
                            <p className="move-details">
                              <span className="unit-name">{pendingMoveConfirm.unitDef?.name || pendingMoveConfirm.unitId}</span>
                              <br />
                              <span className="move-route">
                                {territoryData[pendingMoveConfirm.fromTerritory]?.name}
                                {chosenZoneName ? ` (via ${chosenZoneName})` : ''} → {territoryData[pendingMoveConfirm.toTerritory]?.name}
                              </span>
                            </p>
                            <div className="count-controls">
                              <button
                                className="count-btn minus"
                                onClick={() => onUpdateMoveCount?.(Math.max(1, pendingMoveConfirm.count - 1))}
                                disabled={pendingMoveConfirm.count <= 1}
                              >
                                −
                              </button>
                              <span className="count-value">{pendingMoveConfirm.count}</span>
                              <button
                                className="count-btn plus"
                                onClick={() => onUpdateMoveCount?.(Math.min(pendingMoveConfirm.maxCount, pendingMoveConfirm.count + 1))}
                                disabled={pendingMoveConfirm.count >= pendingMoveConfirm.maxCount}
                              >
                                +
                              </button>
                            </div>
                            <p className="max-hint">
                              Max: {pendingMoveConfirm.maxCount}
                              {pendingMoveConfirm.navalBoatStacks && pendingMoveConfirm.navalBoatStacks.length > 1
                                ? ' ships (each with its passengers)'
                                : ''}
                            </p>
                            <div className="move-confirm-buttons">
                              <button
                                className={confirmBtnClass}
                                onClick={() => onConfirmMove?.()}
                              >
                                {buttonLabel}
                              </button>
                              <button className="cancel-move-btn" onClick={onCancelMove}>Cancel</button>
                            </div>
                          </>
                        );
                      })()}
                    </>
                  );
                })()}
              </div>
            )}

            {/* Bulk mobilization confirmation ("All") */}
            {bulkMobilizeConfirm && !pendingMobilization && (
              <div className="move-confirm mobilization-confirm">
                <h3>Mobilize All</h3>
                <p className="move-details">
                  <span className="unit-name">All units</span>
                  <br />
                  <span className="move-route">
                    → {territoryData[bulkMobilizeConfirm.toTerritory]?.name || bulkMobilizeConfirm.toTerritory}
                  </span>
                </p>
                <div className="mobilization-all-stacks">
                  {bulkMobilizeConfirm.units.map(u => (
                    <div key={u.unitId} className="mobilization-all-stack">
                      {u.unitName}: {u.count}
                    </div>
                  ))}
                </div>
                <div className="move-confirm-buttons">
                  <button className="confirm-move-btn mobilize-btn" onClick={onConfirmBulkMobilize}>
                    Mobilize
                  </button>
                  <button className="cancel-move-btn" onClick={onCancelBulkMobilize}>Cancel</button>
                </div>
              </div>
            )}

            {/* Pending mobilization confirmation with +/- controls */}
            {pendingMobilization && (
              <div className="move-confirm mobilization-confirm">
                <h3>Deploy Units</h3>
                <p className="move-details">
                  <span className="unit-name">{pendingMobilization.unitName}</span>
                  <br />
                  <span className="move-route">
                    → {territoryData[pendingMobilization.toTerritory]?.name}
                  </span>
                </p>
                <div className="count-controls">
                  <button
                    className="count-btn minus"
                    onClick={() => onUpdateMobilizationCount?.(Math.max(1, pendingMobilization.count - 1))}
                    disabled={pendingMobilization.count <= 1}
                  >
                    −
                  </button>
                  <span className="count-value">{pendingMobilization.count}</span>
                  <button
                    className="count-btn plus"
                    onClick={() => onUpdateMobilizationCount?.(Math.min(pendingMobilization.maxCount, pendingMobilization.count + 1))}
                    disabled={pendingMobilization.count >= pendingMobilization.maxCount}
                  >
                    +
                  </button>
                </div>
                <p className="max-hint">Max: {pendingMobilization.maxCount}</p>
                <div className="move-confirm-buttons">
                  <button className="confirm-move-btn mobilize-btn" onClick={onConfirmMobilization}>Mobilize</button>
                  <button className="cancel-move-btn" onClick={onCancelMobilization}>Cancel</button>
                </div>
              </div>
            )}

            {/* Pending camp placement confirmation (applied at end of phase) */}
            {gameState.phase === 'mobilize' && pendingCampPlacement && (
              <div className="move-confirm mobilization-confirm">
                <p className="move-details">
                  <span className="unit-name">Camp</span>
                  <br />
                  <span className="move-route">
                    → {territoryData[pendingCampPlacement.territoryId]?.name ?? pendingCampPlacement.territoryId}
                  </span>
                </p>
                <p className="max-hint">Placed when you end the mobilization phase.</p>
                <div className="move-confirm-buttons">
                  <button className="confirm-move-btn mobilize-btn" onClick={onConfirmCampPlacement}>Confirm</button>
                  <button className="cancel-move-btn" onClick={onCancelCampPlacement}>Cancel</button>
                </div>
              </div>
            )}

            {/* Mobilization naval standoff: enemy fleet appeared in your sea — fight in combat phase or sail away */}
            {gameState.phase === 'combat_move' && forcedNavalStandoffSeaZoneIds.length > 0 && (
              <div className="pending-moves naval-standoff-notice">
                <h3>Naval standoff</h3>
                <p className="naval-standoff-notice__text">
                  Conduct the naval combat in this sea zone or avoid it by sailing to an adjacent sea zone.
                </p>
                <ul className="naval-standoff-notice__list">
                  {forcedNavalStandoffSeaZoneIds.map((zid) => (
                    <li key={zid}>{territoryData[zid]?.name || zid}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* Show pending attacks during combat_move phase */}
            {gameState.phase === 'combat_move' && (() => {
              const combatMoves = gameState.pending_moves.filter(m => m.phase === 'combat_move');
              if (combatMoves.length === 0) return null;
              return (
                <div className="pending-moves">
                  <h3>Planned Attacks</h3>
                  {Object.entries(
                    combatMoves.reduce((acc, move) => {
                      const destName = territoryData[move.to]?.name || move.to;
                      if (!acc[destName]) {
                        acc[destName] = [];
                      }
                      acc[destName].push(move);
                      return acc;
                    }, {} as Record<string, typeof combatMoves>)
                  ).map(([destName, moves]) => (
                    <div key={destName} className="move-group">
                      <div className="move-group-header">→ {destName}</div>
                      {moves.map(move => {
                        const unitDef = unitDefs[move.unitType];
                        const fromName = territoryData[move.from]?.name || move.from;
                        const fromSea = territoryData[move.from]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.from);
                        const toSea = territoryData[move.to]?.terrain === 'sea' || /^sea_zone_?\d+$/i.test(move.to);
                        const isSeaRaidMove = fromSea && !toSea;
                        const moveTypeLabel = plannedCombatMoveTypeLabel(move, territoryData, unitDefs);
                        return (
                          <div key={move.id} className="pending-move-item">
                            <span className="move-info">
                              {unitDef?.name || move.unitType} ({move.count}) from {fromName}
                              {moveTypeLabel && (
                                <span
                                  className="move-type-label"
                                  title={
                                    isSeaRaidMove
                                      ? 'Sea raid (sea unit attacking land)'
                                      : moveTypeLabel === 'Attack'
                                        ? 'Combat move into enemy naval hex'
                                        : `Move type: ${move.move_type ?? ''}`
                                  }
                                >
                                  {' '}
                                  — {moveTypeLabel}
                                </span>
                              )}
                            </span>
                            <button
                              className="cancel-move-x"
                              onClick={() => onCancelPendingMove?.(move.id)}
                              title="Cancel this attack"
                            >
                              ×
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>
              );
            })()}

            {/* Show pending moves during non_combat_move phase */}
            {gameState.phase === 'non_combat_move' && (() => {
              const nonCombatMoves = gameState.pending_moves.filter(m => m.phase === 'non_combat_move');
              if (nonCombatMoves.length === 0) return null;
              return (
                <div className="pending-moves">
                  <h3>Planned Moves</h3>
                  {Object.entries(
                    nonCombatMoves.reduce((acc, move) => {
                      const destName = territoryData[move.to]?.name || move.to;
                      if (!acc[destName]) {
                        acc[destName] = [];
                      }
                      acc[destName].push(move);
                      return acc;
                    }, {} as Record<string, typeof nonCombatMoves>)
                  ).map(([destName, moves]) => (
                    <div key={destName} className="move-group">
                      <div className="move-group-header">→ {destName}</div>
                      {moves.map(move => {
                        const unitDef = unitDefs[move.unitType];
                        const fromName = territoryData[move.from]?.name || move.from;
                        const ncmTypeLabel = plannedNonCombatMoveTypeLabel(move, territoryData, unitDefs);
                        return (
                          <div key={move.id} className="pending-move-item">
                            <span className="move-info">
                              {unitDef?.name || move.unitType} ({move.count}) from {fromName}
                              {ncmTypeLabel && (
                                <span
                                  className="move-type-label"
                                  title={`Move type: ${move.move_type ?? ''}`}
                                >
                                  {' '}
                                  — {ncmTypeLabel}
                                </span>
                              )}
                            </span>
                            <button
                              className="cancel-move-x"
                              onClick={() => onCancelPendingMove?.(move.id)}
                              title="Cancel this move"
                            >
                              ×
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>
              );
            })()}

            {/* Show pending mobilizations and camp placements during mobilize phase */}
            {gameState.phase === 'mobilize' && (pendingMobilizations.length > 0 || pendingCampPlacements.length > 0) && (
              <div className="pending-moves">
                <h3>Planned Mobilizations</h3>
                {pendingCampPlacements.map((p, index) => {
                  const destName = territoryData[p.territory_id]?.name || p.territory_id;
                  return (
                    <div key={`camp-${index}`} className="pending-move-item">
                      <span className="move-info">
                        Camp → {destName}
                      </span>
                      <button
                        className="cancel-move-x"
                        onClick={() => onCancelQueuedCampPlacement?.(index)}
                        title="Cancel this camp placement"
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
                {pendingMobilizations.map((mob, index) => {
                  const destName = territoryData[mob.destination]?.name || mob.destination;
                  const unitSummary = mob.units
                    .map(u => `${unitDefs[u.unit_id]?.name || u.unit_id} (${u.count})`)
                    .join(', ');
                  return (
                    <div key={mob.id} className="pending-move-item">
                      <span className="move-info">
                        → {destName}: {unitSummary}
                      </span>
                      <button
                        className="cancel-move-x"
                        onClick={() => onCancelPendingMobilization?.(index)}
                        title="Cancel this mobilization"
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Show pending battles during combat phase */}
            {gameState.phase === 'combat' && gameState.declared_battles.length > 0 && (
              <div className="pending-battles">
                <h3>Battles</h3>
                {sortedDeclaredBattles.map((battle) => {
                  const territoryName = territoryData[battle.territory]?.name || battle.territory;
                  const blocked = isSeaRaidBlockedByPendingNaval(battle);
                  return (
                    <button
                      key={battle.sea_zone_id ? `sea_${battle.sea_zone_id}_${battle.territory}` : battle.territory}
                      type="button"
                      className={`battle-btn${blocked ? ' battle-btn--blocked' : ''}`}
                      disabled={blocked}
                      title={
                        blocked
                          ? 'Fight the naval battle in this sea zone first; then the sea raid unlocks (or disappears if you lose the fleet).'
                          : undefined
                      }
                      onClick={() => !blocked && onInitiateCombat?.(battle)}
                    >
                      {territoryName}
                    </button>
                  );
                })}
              </div>
            )}

            {/* Retreat destination selection */}
            {pendingRetreat && (
              <div className="retreat-selection">
                <h3>Select Retreat Destination</h3>
                <p className="retreat-from">
                  Retreating from {territoryData[pendingRetreat.territory]?.name || pendingRetreat.territory}
                </p>
                <div className="retreat-options">
                  {validRetreatDestinations.map(destId => {
                    const dest = territoryData[destId];
                    return (
                      <button
                        key={destId}
                        className="retreat-option"
                        onClick={() => onConfirmRetreat?.(destId)}
                      >
                        {dest?.name || destId}
                      </button>
                    );
                  })}
                </div>
                {validRetreatDestinations.length === 0 && (
                  <p className="no-retreat">No valid retreat destinations!</p>
                )}
                <button className="cancel-retreat" onClick={onCancelRetreat}>
                  Cancel (Continue Fighting)
                </button>
              </div>
            )}

          </div>

          {gameState.phase === 'combat' && gameState.declared_battles.length === 0 && !pendingRetreat && (
            <p className="phase-instruction combat-phase-idle">
              {combatMovesDeclaredThisPhase > 0 || battlesCompletedThisPhase > 0
                ? 'All battles completed.'
                : 'No battles declared.'}
            </p>
          )}

          {/* Confirmation dialog */}
          {pendingEndPhaseConfirm && (
            <div className="confirm-dialog">
              <p>
                {pendingEndPhaseConfirm === 'purchase' &&
                  (hasPurchaseCart
                    ? 'End purchase phase? Your purchases will be applied.'
                    : 'Are you sure you would like to end the purchase phase without making any purchases?')}
                {pendingEndPhaseConfirm === 'combat_move' &&
                  'Are you sure you would like to end the combat move phase without making any combat moves?'}
                {pendingEndPhaseConfirm === 'non_combat_move' &&
                  'Are you sure you would like to end the non-combat move phase without making any moves?'}
              </p>
              <div className="confirm-buttons">
                <button className="confirm-yes" onClick={onConfirmEndPhase}>Yes, End Phase</button>
                <button className="confirm-no" onClick={onCancelEndPhase}>Cancel</button>
              </div>
            </div>
          )}

          {!pendingEndPhaseConfirm && !gameOver && (
            <>
              {gameState.phase === 'combat_move' && !pendingMoveConfirm && !bulkMoveConfirm && (gameState.pending_moves || []).filter(m => m.phase === 'combat_move').length === 0 && (
                <p className="phase-instruction">Drag units into territories on the map.</p>
              )}
              {gameState.phase === 'non_combat_move' && !pendingMoveConfirm && !bulkMoveConfirm && (gameState.pending_moves || []).filter(m => m.phase === 'non_combat_move').length === 0 && (
                <>
                  <p className="phase-instruction">Drag units into territories on the map.</p>
                  {aerialUnitsMustMove.length > 0 && (
                    <p className="phase-instruction aerial-must-move-msg">
                      ⚠️ Move aerial unit{aerialUnitsMustMove.length !== 1 ? 's' : ''} to friendly territory before ending phase.
                      {(() => {
                        const byTerritory = new Map<string, number>();
                        for (const u of aerialUnitsMustMove) {
                          byTerritory.set(u.territory_id, (byTerritory.get(u.territory_id) ?? 0) + 1);
                        }
                        const names = Array.from(byTerritory.keys())
                          .map(tid => territoryData[tid]?.name ?? tid)
                          .slice(0, 5);
                        if (names.length > 0) {
                          return (
                            <span className="aerial-must-move-where">
                              {' '}In: {names.join(', ')}{names.length < byTerritory.size ? '…' : ''}
                            </span>
                          );
                        }
                        return null;
                      })()}
                    </p>
                  )}
                </>
              )}
              {gameState.phase === 'mobilize' && !pendingMobilization && hasUnmobilizedPurchases && (
                <p className="phase-instruction">Drag unit stacks to map to mobilize.</p>
              )}
              <button
                className="primary"
                onClick={onEndPhase}
                disabled={endPhaseDisabled}
                title={endPhaseDisabled ? endPhaseDisabledReason : undefined}
              >
                End {formatPhase(gameState.phase)} Phase
              </button>
            </>
          )}
        </div>
      )}

      {/* Spectate Battles: when not our turn, phase is combat, and there are battles; hide when current faction is AI */}
      {!canAct && gameState.phase === 'combat' && gameState.declared_battles.length > 0 && !isCurrentFactionAI && (
        <div className="panel spectate-battles-panel">
          <h2>Spectate Battles</h2>
          <div className="spectate-battles-list">
            {sortedDeclaredBattles.map((battle) => {
              const territoryName = territoryData[battle.territory]?.name || battle.territory;
              const isActive =
                activeCombatTerritoryId === battle.territory &&
                (battle.sea_zone_id == null ? true : activeCombatSeaZoneId === battle.sea_zone_id);
              const key = battle.sea_zone_id ? `sea_${battle.sea_zone_id}_${battle.territory}` : battle.territory;
              const blocked = isSeaRaidBlockedByPendingNaval(battle);
              return (
                <button
                  key={key}
                  type="button"
                  className={`spectate-battle-btn${isActive ? ' spectate-battle-btn--active' : ''}${blocked ? ' spectate-battle-btn--blocked' : ''}`}
                  disabled={blocked}
                  onClick={() => !blocked && onSpectateBattle?.(battle)}
                  title={
                    blocked
                      ? 'Naval battle in this sea zone must finish first.'
                      : isActive
                        ? 'View this battle (in progress)'
                        : 'View units in this battle'
                  }
                >
                  {territoryName}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Territory Panel */}
      <div className={`panel territory-panel${territory ? ' has-territory' : ''}`}>
        {selectedTerritory && (
          <img
            src={`/assets/territories/${selectedTerritory}.png`}
            alt=""
            className="territory-panel-bg-image"
            aria-hidden
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
          />
        )}
        <h2
          className="territory-panel-header"
          style={
            ownerData
              ? {
                backgroundColor: `${ownerData.color}59`,
                borderLeft: `4px solid ${ownerData.color}`,
              }
              : undefined
          }
        >
          {ownerData && (
            <img
              className="faction-icon"
              src={ownerData.icon}
              alt={ownerData.name}
            />
          )}
          <span className="territory-title-content">
            {territory ? (
              <>
                <span className="territory-name">{territory.name}</span>
                <span className="power-terrain">
                  {territory.ownable === false
                    ? (territory.terrain ? territory.terrain.charAt(0).toUpperCase() + territory.terrain.slice(1) : '')
                    : (() => {
                      const powerPart = `${(Number(territory.produces) || 0)}P`;
                      const terrainPart = territory.terrain ? territory.terrain.charAt(0).toUpperCase() + territory.terrain.slice(1) : '';
                      const baseHp = territory.stronghold_base_health ?? 0;
                      const isStrongholdWithHp = territory.stronghold && baseHp > 0;
                      if (isStrongholdWithHp) {
                        const currentHp = territory.stronghold_current_health ?? baseHp;
                        const parts = [`${currentHp}/${baseHp} HP`, powerPart, terrainPart].filter(Boolean);
                        return parts.join(' | ');
                      }
                      const parts = [powerPart, terrainPart].filter(Boolean);
                      return parts.join(' | ');
                    })()}
                </span>
              </>
            ) : (
              'Select Territory'
            )}
          </span>
        </h2>

        {territory && (
          <div className="territory-info">
            {territory.isCapital && (
              <div className="capital-badge">Capital</div>
            )}
            {territory.stronghold && !territory.isCapital && (
              <div className="stronghold-badge">Stronghold</div>
            )}
            {territory.hasCamp && !territory.isCapital && !(selectedTerritory && territory.owner && factionData[territory.owner]?.capital === selectedTerritory) && (
              <div className="camp-badge">Camp</div>
            )}
            {territory.hasPort && (
              <div className="camp-badge">Port</div>
            )}
            {selectedTerritory &&
              territory.owner &&
              (() => {
                const homeUnitNames = Object.entries(unitDefs)
                  .filter(([, def]) => {
                    if (def.faction !== territory.owner) return false;
                    const ids = def.home_territory_ids ?? [];
                    return ids.includes(selectedTerritory);
                  })
                  .map(([, def]) => def.name)
                  .sort();
                if (homeUnitNames.length === 0) return null;
                return (
                  <div className="home-to-badge" title="Home territory: can deploy 1 of these unit types here without a camp">
                    Home to {homeUnitNames.join(', ')}
                  </div>
                );
              })()}

            {gameState.phase === 'non_combat_move' && territoryUnitStacksWithMovement && territoryUnitStacksWithMovement.length > 0 ? (
              <div className="territory-units-list">
                {[...territoryUnitStacksWithMovement]
                  .sort((a, b) => {
                    if (b.count !== a.count) return b.count - a.count;
                    const costA = unitDefs[a.unit_id]?.cost ?? 0;
                    const costB = unitDefs[b.unit_id]?.cost ?? 0;
                    if (costB !== costA) return costB - costA;
                    if (a.unit_id !== b.unit_id) return a.unit_id.localeCompare(b.unit_id);
                    return (b.remaining_movement ?? 0) - (a.remaining_movement ?? 0);
                  })
                  .map((row, index) => {
                    const unitDef = unitDefs[row.unit_id];
                    const icon = unitDef?.icon;
                    const factionColor = unitDef?.faction ? factionData[unitDef.faction]?.color : undefined;
                    return (
                      <div key={`${row.unit_id}-${row.remaining_movement}-${index}`} className="territory-unit-row">
                        {icon && (
                          <span
                            className="territory-unit-icon-wrap"
                            style={factionColor ? { ['--faction-border' as string]: factionColor } : undefined}
                          >
                            <img src={icon} alt="" className="territory-unit-icon" />
                          </span>
                        )}
                        <span className="territory-unit-label">
                          {unitDef?.name || row.unit_id} with {row.remaining_movement}M
                        </span>
                        <span className="territory-unit-count-badge">{row.count}</span>
                      </div>
                    );
                  })}
              </div>
            ) : units.length > 0 && (
              <div className="territory-units-list">
                {[...units]
                  .sort((a, b) => {
                    if (b.count !== a.count) return b.count - a.count;
                    const costA = unitDefs[a.unit_id]?.cost ?? 0;
                    const costB = unitDefs[b.unit_id]?.cost ?? 0;
                    if (costB !== costA) return costB - costA;
                    return a.unit_id.localeCompare(b.unit_id);
                  })
                  .map(({ unit_id, count }, index) => {
                    const unitDef = unitDefs[unit_id];
                    const icon = unitDef?.icon;
                    const factionColor = unitDef?.faction ? factionData[unitDef.faction]?.color : undefined;
                    return (
                      <div key={`${unit_id}-${index}`} className="territory-unit-row">
                        {icon && (
                          <span
                            className="territory-unit-icon-wrap"
                            style={factionColor ? { ['--faction-border' as string]: factionColor } : undefined}
                          >
                            <img src={icon} alt="" className="territory-unit-icon" />
                          </span>
                        )}
                        <span className="territory-unit-label">
                          {unitDef?.name || unit_id}
                        </span>
                        <span className="territory-unit-count-badge">{count}</span>
                      </div>
                    );
                  })}
              </div>
            )}

            {/* Defensive casualty priority: show for any selected territory; editable only when owned by current faction */}
            {selectedTerritory && territory && (
              <div className="defender-casualty-order">
                <span className="defender-casualty-order-label">Defensive Casualty Priority</span>
                {territory.owner === gameState.current_faction && canAct && onSetTerritoryDefenderCasualtyOrder ? (
                  <div className="defender-casualty-order-pills">
                    <button
                      type="button"
                      className={`defender-pill${(territoryDefenderCasualtyOrder[selectedTerritory] ?? 'best_unit') === 'best_unit' ? ' defender-pill--active' : ''}`}
                      onClick={() => onSetTerritoryDefenderCasualtyOrder(selectedTerritory, 'best_unit')}
                      title="Lose cheap/weak units first (cost then defense)"
                    >
                      Best Unit
                    </button>
                    <button
                      type="button"
                      className={`defender-pill${(territoryDefenderCasualtyOrder[selectedTerritory] ?? 'best_unit') === 'best_defense' ? ' defender-pill--active' : ''}`}
                      onClick={() => onSetTerritoryDefenderCasualtyOrder(selectedTerritory, 'best_defense')}
                      title="Prioritize defense value (lose low defense first)"
                    >
                      Best Defense
                    </button>
                  </div>
                ) : (
                  <div className="defender-casualty-order-pills">
                    <span
                      className="defender-pill defender-pill--readonly defender-pill--active"
                      title="Defensive casualty priority (set by territory owner)"
                    >
                      {(territoryDefenderCasualtyOrder[selectedTerritory] ?? 'best_unit') === 'best_defense' ? 'Best Defense' : 'Best Unit'}
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Event Log */}
      <div className="panel event-log">
        <h2>Event Log</h2>
        {(eventLogTurns.length > 0 || eventLogFactions.length > 0 || eventLogPhases.length > 0) && (
          <div className="event-log-filters">
            <label className="event-log-filter">
              <span className="event-log-filter-label">Turn</span>
              <select
                className="event-log-filter-select"
                value={eventLogTurn === '' ? '' : String(eventLogTurn)}
                onChange={e => setEventLogTurn(e.target.value === '' ? '' : Number(e.target.value))}
                aria-label="Filter by turn"
              >
                <option value="">All</option>
                {eventLogTurns.map(t => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </label>
            <label className="event-log-filter">
              <span className="event-log-filter-label">Faction</span>
              <select
                className="event-log-filter-select"
                value={eventLogFaction}
                onChange={e => setEventLogFaction(e.target.value)}
                aria-label="Filter by faction"
              >
                <option value="">All</option>
                {eventLogFactions.map(fid => (
                  <option key={fid} value={fid}>{factionData[fid]?.name ?? fid}</option>
                ))}
              </select>
            </label>
            <label className="event-log-filter">
              <span className="event-log-filter-label">Phase</span>
              <select
                className="event-log-filter-select"
                value={eventLogPhase}
                onChange={e => setEventLogPhase(e.target.value)}
                aria-label="Filter by phase"
              >
                <option value="">All</option>
                {eventLogPhases.map(p => (
                  <option key={p} value={p}>{formatPhase(p)}</option>
                ))}
              </select>
            </label>
          </div>
        )}
        <div className="log-entries">
          {filteredEventLog.length === 0 ? (
            <p className="empty-state">
              {eventLog.length === 0 ? 'No events yet' : 'No events match the selected filters'}
            </p>
          ) : (
            filteredEventLog.map(event => (
              <div key={event.id} className={`log-entry ${event.type}`}>
                {formatEventLogEntryLine(event, gameState, factionData)}
              </div>
            ))
          )}
        </div>
      </div>
    </aside>
  );
}

export default Sidebar;
