import type { GameState, GamePhase, GameEvent, DeclaredBattle } from '../types/game';
import type { PendingMoveConfirm } from './GameMap';
import type { PendingMobilization } from '../App';
import './Sidebar.css';

interface SidebarProps {
  /** When false (e.g. not your turn in multiplayer), the Actions panel is hidden; territory select and event log still show. */
  canAct?: boolean;
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
    isCapital?: boolean;
  }>;
  territoryUnits: Record<string, { unit_id: string; count: number }[]>;
  /** When non-combat move and a territory is selected: stacks grouped by (unit_id, remaining_movement) for that territory */
  territoryUnitStacksWithMovement?: { unit_id: string; remaining_movement: number; count: number }[] | null;
  unitDefs: Record<string, { name: string; icon: string }>;
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
  onCancelPendingMove?: (moveId: string) => void;
  pendingMobilization?: PendingMobilization | null;
  onUpdateMobilizationCount?: (count: number) => void;
  onConfirmMobilization?: () => void;
  onCancelMobilization?: () => void;
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
  return phase
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function Sidebar({
  canAct = true,
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
  onCancelPendingMove,
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
}: SidebarProps) {
  const territory = selectedTerritory ? territoryData[selectedTerritory] : null;
  const units = selectedTerritory ? territoryUnits[selectedTerritory] || [] : [];
  const phaseConfig = PHASE_CONFIG[gameState.phase] || { buttons: [] };
  const owner = territory?.owner;
  const ownerData = owner ? factionData[owner] : null;

  // Purchase is disabled when current faction's capital is captured
  const currentFactionCapital = factionData[gameState.current_faction]?.capital;
  const capitalOwner = currentFactionCapital ? territoryData[currentFactionCapital]?.owner : undefined;
  const capitalCaptured = !!currentFactionCapital && capitalOwner !== gameState.current_faction;

  return (
    <aside className="sidebar">
      {/* Actions Panel — only when it's this player's turn (canAct) */}
      {canAct && (
      <div className="panel actions-panel">
        <h2>Actions</h2>
        <div className="phase-actions">
          {gameState.phase === 'purchase' && capitalCaptured ? (
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

          {/* Pending move confirmation with +/- controls */}
          {pendingMoveConfirm && (
            <div className="move-confirm">
              <h3>{gameState.phase === 'combat_move' ? 'Confirm Attack' : 'Confirm Move'}</h3>
              <p className="move-details">
                <span className="unit-name">{pendingMoveConfirm.unitDef?.name || pendingMoveConfirm.unitId}</span>
                <br />
                <span className="move-route">
                  {territoryData[pendingMoveConfirm.fromTerritory]?.name} → {territoryData[pendingMoveConfirm.toTerritory]?.name}
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
              <p className="max-hint">Max: {pendingMoveConfirm.maxCount}</p>
              <div className="move-confirm-buttons">
                <button
                  className={gameState.phase === 'combat_move' ? 'attack-btn' : 'confirm-move-btn'}
                  onClick={onConfirmMove}
                >
                  {gameState.phase === 'combat_move' ? 'Attack' : 'Move'}
                </button>
                <button className="cancel-move-btn" onClick={onCancelMove}>Cancel</button>
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
                      return (
                        <div key={move.id} className="pending-move-item">
                          <span className="move-info">
                            {unitDef?.name || move.unitType} ({move.count}) from {fromName}
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
                      return (
                        <div key={move.id} className="pending-move-item">
                          <span className="move-info">
                            {unitDef?.name || move.unitType} ({move.count}) from {fromName}
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

          {/* Show pending mobilizations during mobilize phase */}
          {gameState.phase === 'mobilize' && pendingMobilizations.length > 0 && (
            <div className="pending-moves">
              <h3>Planned Mobilizations</h3>
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
              {gameState.declared_battles.map((battle, index) => {
                const territoryName = territoryData[battle.territory]?.name || battle.territory;
                return (
                  <button
                    key={index}
                    className="battle-btn"
                    onClick={() => onInitiateCombat?.(battle)}
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

          {/* Show message when no contested battles remain: "No battles declared" only if user made 0 combat moves; else "All battles completed" (includes all uncontested). */}
          {gameState.phase === 'combat' && gameState.declared_battles.length === 0 && !pendingRetreat && (
            <p className="empty-state">
              {combatMovesDeclaredThisPhase > 0 || battlesCompletedThisPhase > 0
                ? 'All battles completed.'
                : 'No battles declared. Skipping combat.'}
            </p>
          )}
        </div>

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

        {!pendingEndPhaseConfirm && (
          <>
            {gameState.phase === 'combat_move' && !pendingMoveConfirm && (gameState.pending_moves || []).filter(m => m.phase === 'combat_move').length === 0 && (
              <p className="phase-instruction">Drag units into territories on the map.</p>
            )}
            {gameState.phase === 'non_combat_move' && !pendingMoveConfirm && (gameState.pending_moves || []).filter(m => m.phase === 'non_combat_move').length === 0 && (
              <p className="phase-instruction">Drag units into territories on the map.</p>
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

      {/* Territory Panel */}
      <div className="panel territory-panel">
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
              <span className="territory-name">
                {territory.name}{' '}
                <span className="power-production">({territory.produces}P)</span>
              </span>
            ) : (
              'Select Territory'
            )}
          </span>
          {territory?.terrain && (
            <span className="terrain-type">{territory.terrain}</span>
          )}
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

            {gameState.phase === 'non_combat_move' && territoryUnitStacksWithMovement && territoryUnitStacksWithMovement.length > 0 ? (
              <div className="units-by-movement">
                {territoryUnitStacksWithMovement.map((row, index) => {
                  const unitDef = unitDefs[row.unit_id];
                  return (
                    <div key={`${row.unit_id}-${row.remaining_movement}-${index}`} className="unit-movement-row">
                      {unitDef?.name || row.unit_id} with {row.remaining_movement}M ({row.count})
                    </div>
                  );
                })}
              </div>
            ) : units.length > 0 && (
              <div className="units-inline">
                {units.map(({ unit_id, count }, index) => {
                  const unitDef = unitDefs[unit_id];
                  return (
                    <span key={unit_id}>
                      {unitDef?.name || unit_id} ({count})
                      {index < units.length - 1 && ', '}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Event Log */}
      <div className="panel event-log">
        <h2>Event Log</h2>
        <div className="log-entries">
          {eventLog.length === 0 ? (
            <p className="empty-state">No events yet</p>
          ) : (
            eventLog.map(event => (
              <div key={event.id} className={`log-entry ${event.type}`}>
                {event.message}
              </div>
            ))
          )}
        </div>
      </div>
    </aside>
  );
}

export default Sidebar;
