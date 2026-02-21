import { useState, useEffect, useCallback, useMemo } from 'react';
import './CombatDisplay.css';

interface CombatUnit {
  id: string;
  unitType: string;
  name: string;
  icon: string;
  attack: number;
  defense: number;
  health: number;
  remainingHealth: number;
}

interface DiceRoll {
  value: number;
  target: number;
  isHit: boolean;
}

interface CombatRound {
  roundNumber: number;
  attackerRolls: Record<number, DiceRoll[]>;
  defenderRolls: Record<number, DiceRoll[]>;
  attackerHits: number;
  defenderHits: number;
  attackerCasualties: string[];
  defenderCasualties: string[];
  attackerWounded?: string[];
  defenderWounded?: string[];
  /** Hits per unit type this round (from backend); used for after-round hit badges. */
  attackerHitsByUnitType?: Record<string, number>;
  defenderHitsByUnitType?: Record<string, number>;
  /** True when this round is defender archer prefire (before round 1). */
  isArcherPrefire?: boolean;
}

interface RetreatOption {
  territoryId: string;
  territoryName: string;
}

interface CombatDisplayProps {
  isOpen: boolean;
  territoryName: string;
  attacker: {
    faction: string;
    factionName: string;
    factionIcon: string;
    factionColor: string;
    units: CombatUnit[];
  };
  defender: {
    faction: string;
    factionName: string;
    factionIcon: string;
    factionColor: string;
    units: CombatUnit[];
  };
  retreatOptions: RetreatOption[];
  /** False after archer prefire until round 1 is run; retreat button is disabled. */
  canRetreat?: boolean;
  onStartRound: () => Promise<{ round: CombatRound; combatOver: boolean; attackerWon: boolean } | null>;
  onRetreat: (territoryId: string) => void;
  onClose: (attackerWon: boolean, survivingAttackers: CombatUnit[]) => void;
  onCancel?: () => void;
  onHighlightTerritories?: (territoryIds: string[]) => void;
}

// Combat phase states
type CombatPhase = 'ready' | 'rolling' | 'showing_result' | 'awaiting_decision' | 'confirming_retreat' | 'selecting_retreat' | 'complete';

// Dice dot patterns for D10
const DICE_PATTERNS: Record<number, number[]> = {
  1: [4],
  2: [0, 8],
  3: [0, 4, 8],
  4: [0, 2, 6, 8],
  5: [0, 2, 4, 6, 8],
  6: [0, 2, 3, 5, 6, 8],
  7: [0, 2, 3, 4, 5, 6, 8],
  8: [0, 1, 2, 3, 5, 6, 7, 8],
  9: [0, 1, 2, 3, 4, 5, 6, 7, 8],
  10: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
};

// Dice face component with landing animation. Hits use faction color when hitColor is provided.
function Die({
  value,
  isHit,
  isLanding,
  isVisible,
  hitColor,
}: {
  value: number;
  isHit: boolean;
  isLanding: boolean;
  isVisible: boolean;
  hitColor?: string;
}) {
  const pattern = DICE_PATTERNS[value] || DICE_PATTERNS[1];
  const hasTenthDot = pattern.includes(9);

  if (!isVisible) {
    return <div className="die placeholder" />;
  }

  const hitStyle = isHit && hitColor
    ? {
        background: hitColor,
        borderColor: hitColor,
        boxShadow: `0 0 8px ${hitColor}80`,
      }
    : undefined;

  return (
    <div
      className={`die ${isHit ? 'hit' : 'miss'} ${isLanding ? 'landing' : ''}`}
      style={hitStyle}
    >
      {hasTenthDot && <span className="dot tenth-dot" />}
      <div className="die-grid">
        {[0, 1, 2, 3, 4, 5, 6, 7, 8].map(pos => (
          <span
            key={pos}
            className={`dot ${pattern.includes(pos) ? 'visible' : ''}`}
          />
        ))}
      </div>
    </div>
  );
}

// Unit row: countCasualties = previous rounds only (count = start-of-round).
// badgeHitsByUnitType = hits to show per stack; onlyShowBadgeForHpGreaterThanOne = true at round start (HP=1 stacks never show a wound badge then).
function UnitRow({
  statValue,
  units,
  rolls,
  countCasualties,
  badgeHitsByUnitType,
  onlyShowBadgeForHpGreaterThanOne,
  isAttacker,
  revealedRows,
  currentRowKey,
  isLanding,
  hitColor,
}: {
  statValue: number;
  units: CombatUnit[];
  rolls: DiceRoll[];
  countCasualties: string[];
  badgeHitsByUnitType: Record<string, number>;
  onlyShowBadgeForHpGreaterThanOne: boolean;
  isAttacker: boolean;
  revealedRows: Set<string>;
  currentRowKey: string | null;
  isLanding: boolean;
  hitColor?: string;
}) {
  const rowKey = `${isAttacker ? 'attacker' : 'defender'}_${statValue}`;
  const isRevealed = revealedRows.has(rowKey);
  const isCurrentlyLanding = currentRowKey === rowKey && isLanding;

  const countCasualtySet = new Set(countCasualties);

  const unitGroups: { unitType: string; name: string; icon: string; health: number; total: number; countCasualties: number }[] = [];
  const groupMap = new Map<string, { name: string; icon: string; health: number; total: number; countCasualties: number }>();

  units.forEach(unit => {
    const existing = groupMap.get(unit.unitType);
    const inCountCasualties = countCasualtySet.has(unit.id);
    if (existing) {
      existing.total++;
      if (inCountCasualties) existing.countCasualties++;
    } else {
      groupMap.set(unit.unitType, {
        name: unit.name,
        icon: unit.icon,
        health: unit.health,
        total: 1,
        countCasualties: inCountCasualties ? 1 : 0,
      });
    }
  });

  groupMap.forEach((value, key) => {
    unitGroups.push({ unitType: key, ...value });
  });

  return (
    <div className={`unit-row-shelf ${isRevealed ? 'revealed' : ''}`}>
      <div className="stat-label">{statValue}</div>
      <div className="unit-stack">
        {unitGroups.map(group => {
          const hits = badgeHitsByUnitType[group.unitType] ?? 0;
          const showBadge = hits > 0 && (!onlyShowBadgeForHpGreaterThanOne || group.health > 1);
          return (
            <div
              key={group.unitType}
              className="combat-unit-group"
              title={group.name}
            >
              <img src={group.icon} alt={group.name} />
              <span className="unit-count">{group.total - group.countCasualties}</span>
              {showBadge && (
                <span className="hits-badge" title="Hits received">{hits}</span>
              )}
            </div>
          );
        })}
      </div>
      <div className="dice-stack">
        {rolls.map((roll, i) => (
          <Die
            key={i}
            value={roll.value}
            isHit={roll.isHit}
            isLanding={isCurrentlyLanding}
            isVisible={isRevealed}
            hitColor={hitColor}
          />
        ))}
      </div>
    </div>
  );
}

// Combat side display grouped by stat value
function CombatSide({
  title,
  factionIcon,
  factionColor,
  units,
  rolls,
  hits,
  countCasualties,
  badgeHitsByUnitType,
  onlyShowBadgeForHpGreaterThanOne,
  isAttacker,
  revealedRows,
  currentRowKey,
  isLanding,
  showHits,
}: {
  title: string;
  factionIcon: string;
  factionColor: string;
  units: CombatUnit[];
  rolls: Record<number, DiceRoll[]>;
  hits: number;
  countCasualties: string[];
  badgeHitsByUnitType: Record<string, number>;
  onlyShowBadgeForHpGreaterThanOne: boolean;
  isAttacker: boolean;
  revealedRows: Set<string>;
  currentRowKey: string | null;
  isLanding: boolean;
  showHits: boolean;
}) {
  const unitsByValue: Record<number, CombatUnit[]> = {};
  units.forEach(unit => {
    const value = isAttacker ? unit.attack : unit.defense;
    if (!unitsByValue[value]) unitsByValue[value] = [];
    unitsByValue[value].push(unit);
  });

  const allValues = new Set([
    ...Object.keys(unitsByValue).map(Number),
    ...Object.keys(rolls).map(Number),
  ]);
  const sortedValues = Array.from(allValues).sort((a, b) => a - b);

  return (
    <div
      className={`combat-side ${isAttacker ? 'attacker' : 'defender'}`}
      style={{ borderColor: factionColor }}
    >
      <div className="side-header">
        <img src={factionIcon} alt={title} className="faction-icon" />
        <span className="side-title">{title}</span>
        <span className="side-role-badge" title={isAttacker ? 'Attacking' : 'Defending'}>
          {isAttacker ? 'A' : 'D'}
        </span>
      </div>

      <div className="units-shelves">
        {sortedValues.map(value => (
          <UnitRow
            key={value}
            statValue={value}
            units={unitsByValue[value] || []}
            rolls={rolls[value] || []}
            countCasualties={countCasualties}
            badgeHitsByUnitType={badgeHitsByUnitType}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            isAttacker={isAttacker}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            hitColor={factionColor}
          />
        ))}
      </div>

      <div className={`hits-display ${showHits ? 'visible' : ''}`}>
        <span className="hits-count">{hits} Hit{hits !== 1 ? 's' : ''}</span>
      </div>
    </div>
  );
}

function CombatDisplay({
  isOpen,
  territoryName,
  attacker,
  defender,
  retreatOptions,
  canRetreat = true,
  onStartRound,
  onRetreat,
  onClose,
  onCancel,
  onHighlightTerritories,
}: CombatDisplayProps) {
  const [combatPhase, setCombatPhase] = useState<CombatPhase>('ready');
  const [currentRound, setCurrentRound] = useState<CombatRound | null>(null);
  const [roundNumber, setRoundNumber] = useState(0);
  const [revealedRows, setRevealedRows] = useState<Set<string>>(new Set());
  const [currentRowKey, setCurrentRowKey] = useState<string | null>(null);
  const [isLanding, setIsLanding] = useState(false);
  const [showHits, setShowHits] = useState(false);
  const [showCasualtyBadges, setShowCasualtyBadges] = useState(false);

  // Track casualties from PREVIOUS rounds (these units don't show)
  const [previousAttackerCasualties, setPreviousAttackerCasualties] = useState<string[]>([]);
  const [previousDefenderCasualties, setPreviousDefenderCasualties] = useState<string[]>([]);

  // Track if combat is actually over (computed after round)
  const [isCombatOver, setIsCombatOver] = useState(false);
  const [attackerWon, setAttackerWon] = useState(false);
  const [defenderWon, setDefenderWon] = useState(false);

  // Snapshot all units when combat opens so they stay visible (don't disappear when state refreshes)
  const [displayedAttackerUnits, setDisplayedAttackerUnits] = useState<CombatUnit[]>([]);
  const [displayedDefenderUnits, setDisplayedDefenderUnits] = useState<CombatUnit[]>([]);

  // Reset state when opened and snapshot units so icons never disappear
  useEffect(() => {
    if (isOpen) {
      setCombatPhase('ready');
      setCurrentRound(null);
      setRoundNumber(0);
      setRevealedRows(new Set());
      setCurrentRowKey(null);
      setShowHits(false);
      setShowCasualtyBadges(false);
      setPreviousAttackerCasualties([]);
      setPreviousDefenderCasualties([]);
      setIsCombatOver(false);
      setAttackerWon(false);
      setDefenderWon(false);
      setDisplayedAttackerUnits(attacker.units);
      setDisplayedDefenderUnits(defender.units);
    }
  }, [isOpen]); // Snapshot units only when modal opens; ignore later prop updates so icons don't disappear

  // Clear territory highlights when closing retreat selection
  useEffect(() => {
    if (combatPhase !== 'selecting_retreat' && onHighlightTerritories) {
      onHighlightTerritories([]);
    }
  }, [combatPhase, onHighlightTerritories]);

  // Show all units that were in the combat (snapshotted when opened); never remove icons
  const attackerUnitsThisRound = displayedAttackerUnits.length > 0 ? displayedAttackerUnits : attacker.units;
  const defenderUnitsThisRound = displayedDefenderUnits.length > 0 ? displayedDefenderUnits : defender.units;

  // Cumulative casualties (for red badge and surviving count)
  const allAttackerCasualties = [...previousAttackerCasualties, ...(currentRound?.attackerCasualties || [])];
  const allDefenderCasualties = [...previousDefenderCasualties, ...(currentRound?.defenderCasualties || [])];
  const attackerUnitsAlive = attacker.units.filter(u => !allAttackerCasualties.includes(u.id));
  const _defenderUnitsAlive = defender.units.filter(u => !allDefenderCasualties.includes(u.id));
  void _defenderUnitsAlive; // Suppress unused warning - used in animation callback

  // Instance -> unit type and health (from snapshot when combat opened) for computing hits per stack
  const instanceToAttacker = useMemo(() => {
    const map: Record<string, { unitType: string; health: number }> = {};
    const list = displayedAttackerUnits.length > 0 ? displayedAttackerUnits : attacker.units;
    list.forEach(u => { map[u.id] = { unitType: u.unitType, health: u.health }; });
    return map;
  }, [displayedAttackerUnits, attacker.units]);
  const instanceToDefender = useMemo(() => {
    const map: Record<string, { unitType: string; health: number }> = {};
    const list = displayedDefenderUnits.length > 0 ? displayedDefenderUnits : defender.units;
    list.forEach(u => { map[u.id] = { unitType: u.unitType, health: u.health }; });
    return map;
  }, [displayedDefenderUnits, defender.units]);

  // Hit badge: at round start = hits on living units (HP>1 only); after round = hits that stack received this round
  const { badgeHitsAttacker, badgeHitsDefender, onlyShowBadgeForHpGreaterThanOne } = useMemo(() => {
    const onlyHpGreaterThanOne = !showCasualtyBadges; // round start = true, after round = false
    const attackerHits: Record<string, number> = {};
    const defenderHits: Record<string, number> = {};

    if (showCasualtyBadges && currentRound) {
      // After round: use backend payload when present, else derive from casualties + wounded
      if (currentRound.attackerHitsByUnitType != null) {
        Object.assign(attackerHits, currentRound.attackerHitsByUnitType);
      } else {
        currentRound.attackerCasualties.forEach(id => {
          const info = instanceToAttacker[id];
          if (info) { attackerHits[info.unitType] = (attackerHits[info.unitType] ?? 0) + info.health; }
        });
        (currentRound.attackerWounded ?? []).forEach(id => {
          const info = instanceToAttacker[id];
          if (info) { attackerHits[info.unitType] = (attackerHits[info.unitType] ?? 0) + 1; }
        });
      }
      if (currentRound.defenderHitsByUnitType != null) {
        Object.assign(defenderHits, currentRound.defenderHitsByUnitType);
      } else {
        currentRound.defenderCasualties.forEach(id => {
          const info = instanceToDefender[id];
          if (info) { defenderHits[info.unitType] = (defenderHits[info.unitType] ?? 0) + info.health; }
        });
        (currentRound.defenderWounded ?? []).forEach(id => {
          const info = instanceToDefender[id];
          if (info) { defenderHits[info.unitType] = (defenderHits[info.unitType] ?? 0) + 1; }
        });
      }
    } else {
      // Round start: hits on living units (only count for HP>1 so badge only shows for multi-HP stacks)
      attacker.units.forEach(u => {
        if (u.health > 1 && u.remainingHealth < u.health) {
          const d = u.health - u.remainingHealth;
          attackerHits[u.unitType] = (attackerHits[u.unitType] ?? 0) + d;
        }
      });
      defender.units.forEach(u => {
        if (u.health > 1 && u.remainingHealth < u.health) {
          const d = u.health - u.remainingHealth;
          defenderHits[u.unitType] = (defenderHits[u.unitType] ?? 0) + d;
        }
      });
    }

    return {
      badgeHitsAttacker: attackerHits,
      badgeHitsDefender: defenderHits,
      onlyShowBadgeForHpGreaterThanOne: onlyHpGreaterThanOne,
    };
  }, [showCasualtyBadges, currentRound, instanceToAttacker, instanceToDefender, attacker.units, defender.units]);

  // Calculate row animation order
  const getRowOrder = useCallback((round: CombatRound) => {
    const order: string[] = [];

    // Attacker rows ascending (1-10)
    for (let i = 1; i <= 10; i++) {
      if (round.attackerRolls[i]?.length > 0) {
        order.push(`attacker_${i}`);
      }
    }

    // Defender rows ascending (1-10)
    for (let i = 1; i <= 10; i++) {
      if (round.defenderRolls[i]?.length > 0) {
        order.push(`defender_${i}`);
      }
    }

    return order;
  }, []);

  // Animate dice reveals sequentially. If combatOverResult is provided (from backend), use it instead of computing from units.
  const animateDiceReveals = useCallback((
    round: CombatRound,
    currentPrevAttackerCasualties: string[],
    currentPrevDefenderCasualties: string[],
    combatOverResult?: { combatOver: boolean; attackerWon: boolean }
  ) => {
    const rowOrder = getRowOrder(round);
    let index = 0;
    const DELAY_BETWEEN_ROWS = 1050;
    const LANDING_DURATION = 280;
    const revealed = new Set<string>();

    const revealNextRow = () => {
      if (index >= rowOrder.length) {
        setCurrentRowKey(null);
        // 1) Show hits count below shelves (faster: ~half previous delay)
        setTimeout(() => setShowHits(true), 280);
        // 2) Then show hit badges on unit icons (short delay)
        setTimeout(() => setShowCasualtyBadges(true), 480);
        // 3) Then show result or decision buttons
        setTimeout(() => {
          const combatEnded = combatOverResult
            ? combatOverResult.combatOver
            : (() => {
              const totalAttackerCasualties = [...currentPrevAttackerCasualties, ...round.attackerCasualties];
              const totalDefenderCasualties = [...currentPrevDefenderCasualties, ...round.defenderCasualties];
              const attackersAlive = attacker.units.filter(u => !totalAttackerCasualties.includes(u.id));
              const defendersAlive = defender.units.filter(u => !totalDefenderCasualties.includes(u.id));
              return attackersAlive.length === 0 || defendersAlive.length === 0;
            })();

          if (combatEnded) {
            setIsCombatOver(true);
            if (combatOverResult) {
              setAttackerWon(combatOverResult.attackerWon);
              setDefenderWon(!combatOverResult.attackerWon);
            } else {
              const totalAttackerCasualties = [...currentPrevAttackerCasualties, ...round.attackerCasualties];
              const totalDefenderCasualties = [...currentPrevDefenderCasualties, ...round.defenderCasualties];
              const attackersAlive = attacker.units.filter(u => !totalAttackerCasualties.includes(u.id));
              const defendersAlive = defender.units.filter(u => !totalDefenderCasualties.includes(u.id));
              setAttackerWon(defendersAlive.length === 0 && attackersAlive.length > 0);
              setDefenderWon(attackersAlive.length === 0 && defendersAlive.length > 0);
            }
            setTimeout(() => {
              setCombatPhase('showing_result');
              setTimeout(() => setCombatPhase('complete'), 1500);
            }, 400);
          } else {
            setCombatPhase('awaiting_decision');
          }
        }, 750);
        return;
      }

      const rowKey = rowOrder[index];
      revealed.add(rowKey);
      setRevealedRows(new Set(revealed));
      setCurrentRowKey(rowKey);
      setIsLanding(true);

      setTimeout(() => setIsLanding(false), LANDING_DURATION);

      index++;
      setTimeout(revealNextRow, DELAY_BETWEEN_ROWS);
    };

    revealNextRow();
  }, [getRowOrder, attacker.units, defender.units]);

  // Start battle (round 1) or continue to next round - call backend then animate
  const handleRoll = useCallback(async (
    overridePrevAttacker?: string[],
    overridePrevDefender?: string[]
  ) => {
    setCombatPhase('rolling');
    setShowHits(false);
    setShowCasualtyBadges(false);
    setRevealedRows(new Set());
    setCurrentRowKey(null);

    const result = await onStartRound();
    if (!result) {
      setCombatPhase(roundNumber === 0 ? 'ready' : 'awaiting_decision');
      return;
    }

    const newRoundNumber = result.round.roundNumber;
    setRoundNumber(newRoundNumber);
    setCurrentRound(result.round);

    const prevA = overridePrevAttacker ?? previousAttackerCasualties;
    const prevD = overridePrevDefender ?? previousDefenderCasualties;

    animateDiceReveals(
      result.round,
      prevA,
      prevD,
      result.combatOver ? { combatOver: true, attackerWon: result.attackerWon } : undefined
    );
  }, [onStartRound, animateDiceReveals, previousAttackerCasualties, previousDefenderCasualties, roundNumber]);

  // Continue = start next round (merge this round's casualties then roll)
  const handleContinue = useCallback(() => {
    const nextPrevA = [...previousAttackerCasualties, ...(currentRound?.attackerCasualties || [])];
    const nextPrevD = [...previousDefenderCasualties, ...(currentRound?.defenderCasualties || [])];
    setPreviousAttackerCasualties(nextPrevA);
    setPreviousDefenderCasualties(nextPrevD);
    setCurrentRound(null);
    setShowHits(false);
    setShowCasualtyBadges(false);
    setRevealedRows(new Set());
    setCurrentRowKey(null);
    void handleRoll(nextPrevA, nextPrevD);
  }, [currentRound, previousAttackerCasualties, previousDefenderCasualties, handleRoll]);

  // Handle Retreat button click - show confirmation
  const handleRetreatClick = useCallback(() => {
    setCombatPhase('confirming_retreat');
  }, []);

  // Handle retreat confirmation - show territory selection
  const handleConfirmRetreat = useCallback(() => {
    setCombatPhase('selecting_retreat');
    // Highlight valid retreat territories on the map
    if (onHighlightTerritories) {
      onHighlightTerritories(retreatOptions.map(r => r.territoryId));
    }
  }, [retreatOptions, onHighlightTerritories]);

  // Handle retreat cancellation
  const handleCancelRetreat = useCallback(() => {
    setCombatPhase('awaiting_decision');
    if (onHighlightTerritories) {
      onHighlightTerritories([]);
    }
  }, [onHighlightTerritories]);

  // Handle retreat territory selection
  const handleSelectRetreatTerritory = useCallback((territoryId: string) => {
    if (onHighlightTerritories) {
      onHighlightTerritories([]);
    }
    onRetreat(territoryId);
  }, [onRetreat, onHighlightTerritories]);

  const handleClose = useCallback(() => {
    onClose(attackerWon, attackerUnitsAlive);
  }, [onClose, attackerWon, attackerUnitsAlive]);

  const handleCancel = useCallback(() => {
    onCancel?.();
  }, [onCancel]);

  if (!isOpen) return null;

  const showRollingIndicator = combatPhase === 'rolling';
  const showDecisionButtons = combatPhase === 'awaiting_decision';
  const showRetreatConfirm = combatPhase === 'confirming_retreat';
  const showRetreatSelection = combatPhase === 'selecting_retreat';
  const showCloseButton = combatPhase === 'complete';
  const showResultBanner = combatPhase === 'showing_result' || combatPhase === 'complete';
  const showInitialButtons = combatPhase === 'ready' && roundNumber === 0;

  return (
    <div className="modal-overlay">
      <div className="modal combat-modal">
        <header className="modal-header">
          <h2>Battle for {territoryName}</h2>
          {currentRound?.isArcherPrefire && (
            <span className="round-indicator">Archers</span>
          )}
          {roundNumber > 0 && !currentRound?.isArcherPrefire && (
            <span className="round-indicator">Round {roundNumber}</span>
          )}
        </header>

        <div className="combat-arena">
          <CombatSide
            title={attacker.factionName}
            factionIcon={attacker.factionIcon}
            factionColor={attacker.factionColor}
            units={attackerUnitsThisRound}
            rolls={currentRound?.attackerRolls || {}}
            hits={currentRound?.attackerHits || 0}
            countCasualties={previousAttackerCasualties}
            badgeHitsByUnitType={badgeHitsAttacker}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            isAttacker={true}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            showHits={showHits}
          />

          <div className="vs-divider">
            <span>VS</span>
            {showRollingIndicator && (
              <div className="rolling-indicator">Fighting...</div>
            )}
          </div>

          <CombatSide
            title={defender.factionName}
            factionIcon={defender.factionIcon}
            factionColor={defender.factionColor}
            units={defenderUnitsThisRound}
            rolls={currentRound?.defenderRolls || {}}
            hits={currentRound?.defenderHits || 0}
            countCasualties={previousDefenderCasualties}
            badgeHitsByUnitType={badgeHitsDefender}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            isAttacker={false}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            showHits={showHits}
          />
        </div>

        {/* Victory/Defeat banner - only show after animations complete */}
        {showResultBanner && isCombatOver && (
          <div
            className={`combat-result ${attackerWon ? 'victory' : defenderWon ? 'defeat' : 'draw'}`}
            style={{
              backgroundColor: attackerWon
                ? `${attacker.factionColor}60`
                : defenderWon
                  ? `${defender.factionColor}60`
                  : undefined
            }}
          >
            <span className="result-text">
              {attackerWon && 'Victory!'}
              {defenderWon && 'Defeat!'}
              {!attackerWon && !defenderWon && 'Mutual Destruction!'}
            </span>
          </div>
        )}

        <footer className="modal-footer">
          <div className="combat-actions">
            {/* Initial: Close (tan) + Start (red) */}
            {showInitialButtons && (
              <>
                <button type="button" className="combat-btn-close" onClick={handleCancel}>
                  Close
                </button>
                <button type="button" className="combat-btn-start" onClick={() => handleRoll()}>
                  Start
                </button>
              </>
            )}

            {/* After round: Retreat (red, disabled until attackers have rolled) + Continue (tan) */}
            {showDecisionButtons && (
              <>
                <button
                  type="button"
                  className={`combat-btn-retreat${canRetreat ? '' : ' disabled'}`}
                  onClick={canRetreat ? handleRetreatClick : undefined}
                  disabled={!canRetreat}
                  title={!canRetreat ? 'Retreat unavailable (click Continue first after archer prefire, or no adjacent allied/neutral territory)' : undefined}
                >
                  Retreat
                </button>
                <button type="button" className="combat-btn-continue" onClick={handleContinue}>
                  Continue
                </button>
              </>
            )}

            {/* Retreat confirmation */}
            {showRetreatConfirm && (
              <div className="retreat-confirm">
                <p className="confirm-message">Are you sure you want to retreat? Your surviving units will flee the battle.</p>
                <div className="confirm-buttons">
                  <button onClick={handleCancelRetreat}>Cancel</button>
                  <button className="danger" onClick={handleConfirmRetreat}>
                    Yes, Retreat
                  </button>
                </div>
              </div>
            )}

            {/* Retreat territory selection */}
            {showRetreatSelection && (
              <div className="retreat-selection">
                <p className="selection-message">Select a territory to retreat to:</p>
                <div className="retreat-options">
                  {retreatOptions.map(option => (
                    <button
                      key={option.territoryId}
                      className="retreat-option"
                      onClick={() => handleSelectRetreatTerritory(option.territoryId)}
                    >
                      {option.territoryName}
                    </button>
                  ))}
                </div>
                <button className="cancel-retreat" onClick={handleCancelRetreat}>
                  Cancel
                </button>
              </div>
            )}

            {/* Combat over - Close */}
            {showCloseButton && (
              <button type="button" className="combat-btn-close" onClick={handleClose}>
                Close
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}

export default CombatDisplay;
