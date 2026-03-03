import { useState, useEffect, useCallback, useMemo } from 'react';
import './CombatDisplay.css';

interface CombatUnit {
  id: string;
  unitType: string;
  name: string;
  icon: string;
  attack: number;
  defense: number;
  /** When set (e.g. terrain bonus), used for shelf grouping so unit and rolls stay on same row. */
  effectiveAttack?: number;
  /** When set (e.g. terrain bonus), used for shelf grouping so unit and rolls stay on same row. */
  effectiveDefense?: number;
  /** True if unit is archer (archetype or tag); used to show only archers during prefire. */
  isArcher?: boolean;
  health: number;
  remainingHealth: number;
  /** Defender units only: border color for this unit's faction (not territory owner). */
  factionColor?: string;
  /** Attack only: unit has Terror. */
  hasTerror?: boolean;
  /** Unit is receiving mountain terrain bonus. */
  terrainMountain?: boolean;
  /** Unit is receiving forest terrain bonus. */
  terrainForest?: boolean;
  /** Unit is receiving captain +1 (not the captain himself). */
  hasCaptainBonus?: boolean;
  /** Unit is receiving anti-cavalry (pikes) +1. */
  hasAntiCavalry?: boolean;
}

interface DiceRoll {
  value: number;
  target: number;
  isHit: boolean;
}

/** Full round result from backend (combat_round_resolved). Single source of truth for in-round display. */
export interface CombatRound {
  roundNumber: number;
  attackerRolls: Record<number, DiceRoll[]>;
  defenderRolls: Record<number, DiceRoll[]>;
  attackerHits: number;
  defenderHits: number;
  attackerCasualties: string[];
  defenderCasualties: string[];
  attackerWounded?: string[];
  defenderWounded?: string[];
  attackerHitsByUnitType?: Record<string, number>;
  defenderHitsByUnitType?: Record<string, number>;
  isArcherPrefire?: boolean;
  terrorApplied?: boolean;
  /** Units at round start (from backend). Always present; use this for round display, not state. */
  attackerUnitsAtStart: CombatUnit[];
  defenderUnitsAtStart: CombatUnit[];
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
  onStartRound: () => Promise<{
    round: CombatRound;
    combatOver: boolean;
    attackerWon: boolean;
    terrorReroll?: {
      applied: boolean;
      instance_ids?: string[];
      initial_rolls_by_instance?: Record<string, number[]>;
      defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>;
      defender_rerolled_indices_by_stat?: Record<string, number[]>;
    };
  } | null>;
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
// isRerolled: show red X overlay (Terror re-roll indicator).
function Die({
  value,
  isHit,
  isLanding,
  isVisible,
  hitColor,
  isRerolled,
}: {
  value: number;
  isHit: boolean;
  isLanding: boolean;
  isVisible: boolean;
  hitColor?: string;
  isRerolled?: boolean;
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
      className={`die ${isHit ? 'hit' : 'miss'} ${isLanding ? 'landing' : ''} ${isRerolled ? 'rerolled' : ''}`}
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
      {isRerolled && <span className="die-reroll-x" aria-hidden>×</span>}
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
  showCasualtyBadges,
  isAttacker,
  revealedRows,
  currentRowKey,
  isLanding,
  hitColor,
  rerolledIndices,
  rerolledDice,
  showRerollX,
}: {
  statValue: number;
  units: CombatUnit[];
  rolls: DiceRoll[];
  countCasualties: string[];
  badgeHitsByUnitType: Record<string, number>;
  onlyShowBadgeForHpGreaterThanOne: boolean;
  showCasualtyBadges: boolean;
  isAttacker: boolean;
  revealedRows: Set<string>;
  currentRowKey: string | null;
  isLanding: boolean;
  hitColor?: string;
  /** Indices in this row's rolls that were re-rolled by Terror (show red X). */
  rerolledIndices?: number[];
  /** Re-rolled dice to show next to original (Terror); shown after pause. */
  rerolledDice?: DiceRoll[];
  /** When false, red X is hidden (used to delay showing X until after dice are visible). */
  showRerollX?: boolean;
}) {
  const rowKey = `${isAttacker ? 'attacker' : 'defender'}_${statValue}`;
  const isRevealed = revealedRows.has(rowKey);
  const isCurrentlyLanding = currentRowKey === rowKey && isLanding;
  const rerolledSet = new Set(rerolledIndices ?? []);
  const hasRerolledDice = (rerolledDice?.length ?? 0) > 0;
  const showX = showRerollX === true;

  const countCasualtySet = new Set(countCasualties);

  const unitGroups: {
    unitType: string;
    name: string;
    icon: string;
    health: number;
    total: number;
    countCasualties: number;
    factionColor?: string;
    hasTerror?: boolean;
    terrainMountain?: boolean;
    terrainForest?: boolean;
    hasCaptainBonus?: boolean;
    hasAntiCavalry?: boolean;
  }[] = [];
  const groupMap = new Map<string, {
    name: string;
    icon: string;
    health: number;
    total: number;
    countCasualties: number;
    factionColor?: string;
    hasTerror?: boolean;
    terrainMountain?: boolean;
    terrainForest?: boolean;
    hasCaptainBonus?: boolean;
    hasAntiCavalry?: boolean;
  }>();

  units.forEach(unit => {
    const existing = groupMap.get(unit.unitType);
    const inCountCasualties = countCasualtySet.has(unit.id);
    if (existing) {
      existing.total++;
      if (inCountCasualties) existing.countCasualties++;
      if (unit.hasTerror) existing.hasTerror = true;
      if (unit.terrainMountain) existing.terrainMountain = true;
      if (unit.terrainForest) existing.terrainForest = true;
      if (unit.hasCaptainBonus) existing.hasCaptainBonus = true;
      if (unit.hasAntiCavalry) existing.hasAntiCavalry = true;
    } else {
      groupMap.set(unit.unitType, {
        name: unit.name,
        icon: unit.icon,
        health: unit.health,
        total: 1,
        countCasualties: inCountCasualties ? 1 : 0,
        factionColor: unit.factionColor,
        hasTerror: unit.hasTerror,
        terrainMountain: unit.terrainMountain,
        terrainForest: unit.terrainForest,
        hasCaptainBonus: unit.hasCaptainBonus,
        hasAntiCavalry: unit.hasAntiCavalry,
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
          const aliveCount = group.total - group.countCasualties;
          const eliminated = aliveCount === 0;
          // Delay red X until after dice and hits are revealed so stacks don't "disappear" mid-animation
          const showEliminated = eliminated && showCasualtyBadges;
          // Only show hit badge after all dice are revealed (showCasualtyBadges), then apply HP filter
          const showBadge = hits > 0 && showCasualtyBadges && (!onlyShowBadgeForHpGreaterThanOne || group.health > 1);
          const specials: string[] = [];
          if (group.hasTerror) specials.push('T');
          if (group.terrainMountain) specials.push('M');
          if (group.terrainForest) specials.push('F');
          if (group.hasCaptainBonus) specials.push('C');
          if (group.hasAntiCavalry) specials.push('P');
          return (
            <div
              key={group.unitType}
              className={`combat-unit-group ${showEliminated ? 'eliminated' : ''}`}
              title={group.name}
              style={(group.factionColor ?? hitColor) ? { borderColor: group.factionColor ?? hitColor } : undefined}
            >
              {specials.length > 0 && (
                <span className="unit-specials-badge" title="T=Terror, M=Mountain, F=Forest, C=Captain, P=Pikes (anti-cavalry)">
                  {specials.join('')}
                </span>
              )}
              <img src={group.icon} alt={group.name} />
              {showEliminated ? (
                <span className="unit-stack-eliminated-x" aria-hidden title="Stack eliminated">×</span>
              ) : (
                <span className="unit-count">{aliveCount > 0 ? aliveCount : group.total}</span>
              )}
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
            isRerolled={showX && rerolledSet.has(i)}
          />
        ))}
        {hasRerolledDice && (
          <>
            <span className="dice-stack-reroll-label">Re-roll</span>
            {(rerolledDice ?? []).map((roll, i) => (
              <Die
                key={`reroll-${i}`}
                value={roll.value}
                isHit={roll.isHit}
                isLanding={false}
                isVisible={isRevealed}
                hitColor={hitColor}
                isRerolled={false}
              />
            ))}
          </>
        )}
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
  badgeHitsPerShelf,
  onlyShowBadgeForHpGreaterThanOne,
  showCasualtyBadges,
  isAttacker,
  isArcherPrefire,
  revealedRows,
  currentRowKey,
  isLanding,
  showHits,
  defenderRerolledIndicesByStat,
  defenderRerolledDiceByStat,
  showRerollX,
}: {
  title: string;
  factionIcon: string;
  factionColor: string;
  units: CombatUnit[];
  rolls: Record<number, DiceRoll[]>;
  hits: number;
  countCasualties: string[];
  badgeHitsByUnitType: Record<string, number>;
  /** When set (defender), hit badges are per-shelf so they match backend loss order. */
  badgeHitsPerShelf?: Record<number, Record<string, number>>;
  onlyShowBadgeForHpGreaterThanOne: boolean;
  showCasualtyBadges: boolean;
  isAttacker: boolean;
  isArcherPrefire?: boolean;
  revealedRows: Set<string>;
  currentRowKey: string | null;
  isLanding: boolean;
  showHits: boolean;
  /** When Terror re-roll: stat -> indices in that row's rolls that were re-rolled (show red X). Defender only. */
  defenderRerolledIndicesByStat?: Record<string, number[]>;
  /** When Terror re-roll: stat -> re-rolled dice to show on shelf next to original. Defender only. */
  defenderRerolledDiceByStat?: Record<string, DiceRoll[]>;
  /** When false, red X on re-rolled dice is hidden (delay until after dice visible). Defender only. */
  showRerollX?: boolean;
}) {
  // Shelf/row is always by effective stat (base + terrain, captain, anti-cavalry, etc.) when provided by parent
  const unitsByValue: Record<number, CombatUnit[]> = {};
  units.forEach(unit => {
    const baseAtk = unit.attack;
    const baseDef = unit.defense;
    const effAtk = unit.effectiveAttack ?? baseAtk;
    const effDef = unit.effectiveDefense ?? baseDef;
    let value = isAttacker ? effAtk : effDef;
    if (isArcherPrefire && !isAttacker) value = effDef - 1;
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
            rolls={rolls[value] ?? (rolls as Record<string, DiceRoll[]>)[String(value)] ?? []}
            countCasualties={countCasualties}
            badgeHitsByUnitType={badgeHitsPerShelf ? (badgeHitsPerShelf[value] ?? {}) : badgeHitsByUnitType}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={isAttacker}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            hitColor={unitsByValue[value]?.[0]?.factionColor ?? factionColor}
            rerolledIndices={!isAttacker ? (defenderRerolledIndicesByStat?.[String(value)] ?? []) : undefined}
            rerolledDice={!isAttacker ? (defenderRerolledDiceByStat?.[String(value)] ?? []) : undefined}
            showRerollX={!isAttacker ? showRerollX : undefined}
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
  /** When terror re-roll: 'initial' = showing pre-reroll dice (don't show Terror badge yet); 'rerolled' = showing final dice. */
  const [terrorRerollPhase, setTerrorRerollPhase] = useState<'initial' | 'rerolled' | null>(null);
  /** Which defender dice indices (per stat) were re-rolled by Terror; used to show red X on original dice. */
  const [terrorRerolledIndicesByStat, setTerrorRerolledIndicesByStat] = useState<Record<string, number[]>>({});
  /** Final round after Terror (for hit count and to extract re-rolled dice); we never replace displayed defender dice with it. */
  const [terrorFinalRound, setTerrorFinalRound] = useState<CombatRound | null>(null);
  /** When true, show red X on defender dice that were re-rolled (delayed so dice appear first). */
  const [showTerrorRerollX, setShowTerrorRerollX] = useState(false);

  // Track casualties from PREVIOUS rounds (these units don't show)
  const [previousAttackerCasualties, setPreviousAttackerCasualties] = useState<string[]>([]);
  const [previousDefenderCasualties, setPreviousDefenderCasualties] = useState<string[]>([]);

  // Track if combat is actually over (computed after round)
  const [isCombatOver, setIsCombatOver] = useState(false);
  const [attackerWon, setAttackerWon] = useState(false);
  const [defenderWon, setDefenderWon] = useState(false);

  // Reset combat UI state when modal opens
  useEffect(() => {
    if (isOpen) {
      setCombatPhase('ready');
      setCurrentRound(null);
      setRoundNumber(0);
      setRevealedRows(new Set());
      setCurrentRowKey(null);
      setShowHits(false);
      setShowCasualtyBadges(false);
      setTerrorRerollPhase(null);
      setTerrorRerolledIndicesByStat({});
      setTerrorFinalRound(null);
      setShowTerrorRerollX(false);
      setPreviousAttackerCasualties([]);
      setPreviousDefenderCasualties([]);
      setIsCombatOver(false);
      setAttackerWon(false);
      setDefenderWon(false);
    }
  }, [isOpen]);

  // Clear territory highlights when closing retreat selection
  useEffect(() => {
    if (combatPhase !== 'selecting_retreat' && onHighlightTerritories) {
      onHighlightTerritories([]);
    }
  }, [combatPhase, onHighlightTerritories]);

  // Single source of truth: when we have a round result, the backend payload defines who was on each shelf.
  // Before any round runs (ready phase), we use props built from state.
  const attackerUnitsThisRound = currentRound ? currentRound.attackerUnitsAtStart : attacker.units;
  const defenderUnitsThisRound = currentRound ? currentRound.defenderUnitsAtStart : defender.units;

  // Only show units that are rolling this round: prefire = defender archers only; round 1+ = everyone
  const attackerUnitsToShow = useMemo(() => {
    if (currentRound?.isArcherPrefire) return [];
    return attackerUnitsThisRound;
  }, [currentRound?.isArcherPrefire, attackerUnitsThisRound]);
  const defenderUnitsToShow = useMemo(() => {
    if (currentRound?.isArcherPrefire)
      return defenderUnitsThisRound.filter(u => u.isArcher === true);
    return defenderUnitsThisRound;
  }, [currentRound?.isArcherPrefire, defenderUnitsThisRound]);

  // Cumulative casualties (for red badge and surviving count)
  const allAttackerCasualties = [...previousAttackerCasualties, ...(currentRound?.attackerCasualties || [])];
  const allDefenderCasualties = [...previousDefenderCasualties, ...(currentRound?.defenderCasualties || [])];
  const attackerUnitsAlive = attacker.units.filter(u => !allAttackerCasualties.includes(u.id));
  const _defenderUnitsAlive = defender.units.filter(u => !allDefenderCasualties.includes(u.id));
  void _defenderUnitsAlive; // Suppress unused warning - used in animation callback

  // Instance -> unit type and health for computing hits per stack (use round-start units when available)
  const instanceToAttacker = useMemo(() => {
    const map: Record<string, { unitType: string; health: number }> = {};
    attackerUnitsThisRound.forEach(u => { map[u.id] = { unitType: u.unitType, health: u.health }; });
    return map;
  }, [attackerUnitsThisRound]);
  const instanceToDefender = useMemo(() => {
    const map: Record<string, { unitType: string; health: number }> = {};
    defenderUnitsThisRound.forEach(u => { map[u.id] = { unitType: u.unitType, health: u.health }; });
    return map;
  }, [defenderUnitsThisRound]);

  // Defender instance -> effective defense (for grouping casualties by shelf so hit badges match backend)
  const defenderEffectiveDefenseByInstance = useMemo(() => {
    const map: Record<string, number> = {};
    defenderUnitsThisRound.forEach(u => {
      map[u.id] = u.effectiveDefense ?? u.defense;
    });
    return map;
  }, [defenderUnitsThisRound]);

  // Hit badge: at round start = hits on living units (HP>1 only); after round = hits that stack received this round.
  // Defender hits are per-shelf (by effective defense) so badges and dice counts stay in sync with backend.
  const { badgeHitsAttacker, badgeHitsDefender, badgeHitsDefenderPerShelf, onlyShowBadgeForHpGreaterThanOne } = useMemo(() => {
    const onlyHpGreaterThanOne = !showCasualtyBadges; // round start = true, after round = false
    const attackerHits: Record<string, number> = {};
    const defenderHits: Record<string, number> = {};
    const defenderHitsPerShelf: Record<number, Record<string, number>> = {};

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
      // Defender: derive from casualties + wounded and group by effective defense (shelf) so each row shows only its hits
      const defenderRound = (terrorRerollPhase === 'rerolled' && terrorFinalRound) ? terrorFinalRound : currentRound;
      const addDefenderHit = (id: string, hitValue: number) => {
        const info = instanceToDefender[id];
        const effDef = defenderEffectiveDefenseByInstance[id];
        if (info != null && effDef != null) {
          defenderHits[info.unitType] = (defenderHits[info.unitType] ?? 0) + hitValue;
          if (!defenderHitsPerShelf[effDef]) defenderHitsPerShelf[effDef] = {};
          defenderHitsPerShelf[effDef][info.unitType] = (defenderHitsPerShelf[effDef][info.unitType] ?? 0) + hitValue;
        }
      };
      defenderRound.defenderCasualties.forEach(id => {
        const info = instanceToDefender[id];
        addDefenderHit(id, info?.health ?? 1);
      });
      (defenderRound.defenderWounded ?? []).forEach(id => addDefenderHit(id, 1));
    } else {
      // Round start: hits on living units (only count for HP>1 so badge only shows for multi-HP stacks)
      attackerUnitsThisRound.forEach(u => {
        if (u.health > 1 && u.remainingHealth < u.health) {
          const d = u.health - u.remainingHealth;
          attackerHits[u.unitType] = (attackerHits[u.unitType] ?? 0) + d;
        }
      });
      defenderUnitsThisRound.forEach(u => {
        if (u.health > 1 && u.remainingHealth < u.health) {
          const d = u.health - u.remainingHealth;
          defenderHits[u.unitType] = (defenderHits[u.unitType] ?? 0) + d;
          const effDef = u.effectiveDefense ?? u.defense;
          if (!defenderHitsPerShelf[effDef]) defenderHitsPerShelf[effDef] = {};
          defenderHitsPerShelf[effDef][u.unitType] = (defenderHitsPerShelf[effDef][u.unitType] ?? 0) + d;
        }
      });
    }

    return {
      badgeHitsAttacker: attackerHits,
      badgeHitsDefender: defenderHits,
      badgeHitsDefenderPerShelf: defenderHitsPerShelf,
      onlyShowBadgeForHpGreaterThanOne: onlyHpGreaterThanOne,
    };
  }, [showCasualtyBadges, currentRound, terrorRerollPhase, terrorFinalRound, instanceToAttacker, instanceToDefender, defenderEffectiveDefenseByInstance, attackerUnitsThisRound, defenderUnitsThisRound]);

  // Calculate row animation order (include -1 for archer prefire)
  const getRowOrder = useCallback((round: CombatRound) => {
    const order: string[] = [];

    for (let i = 1; i <= 10; i++) {
      if (round.attackerRolls[i]?.length > 0) {
        order.push(`attacker_${i}`);
      }
    }
    if (round.defenderRolls[-1]?.length > 0) {
      order.push('defender_-1');
    }
    for (let i = 1; i <= 10; i++) {
      if (round.defenderRolls[i]?.length > 0) {
        order.push(`defender_${i}`);
      }
    }

    return order;
  }, []);

  // Convert grouped dice (stat -> { rolls, hits }) to round defenderRolls format
  const groupedToDefenderRolls = useCallback((
    grouped: Record<string, { rolls: number[]; hits: number }>
  ): Record<number, DiceRoll[]> => {
    const out: Record<number, DiceRoll[]> = {};
    for (const [statStr, data] of Object.entries(grouped || {})) {
      const stat = Number(statStr);
      out[stat] = (data.rolls || []).map((value: number) => ({
        value,
        target: stat,
        isHit: value <= stat,
      }));
    }
    return out;
  }, []);

  // Animate dice reveals. When terror: show initial defender dice (and initial hits), then after pause show "Terror" and swap to re-rolled dice/hits.
  const animateDiceReveals = useCallback((
    round: CombatRound,
    currentPrevAttackerCasualties: string[],
    currentPrevDefenderCasualties: string[],
    combatOverResult?: { combatOver: boolean; attackerWon: boolean },
    terrorReroll?: { applied: boolean; finalRound?: CombatRound },
    onTerrorRerollShown?: () => void
  ) => {
    const rowOrder = getRowOrder(round);
    let index = 0;
    const DELAY_BETWEEN_ROWS = 1050;
    const LANDING_DURATION = 280;
    const revealed = new Set<string>();
    const TERROR_REROLL_PAUSE_MS = 900;

    const revealNextRow = () => {
      if (index >= rowOrder.length) {
        setCurrentRowKey(null);
        const scheduleResultOrDecision = (delayMs: number) => {
          setTimeout(() => {
            const unitListA = round.attackerUnitsAtStart ?? attackerUnitsThisRound;
            const unitListD = round.defenderUnitsAtStart ?? defenderUnitsThisRound;
            const combatEnded = combatOverResult
              ? combatOverResult.combatOver
              : (() => {
                const totalAttackerCasualties = [...currentPrevAttackerCasualties, ...round.attackerCasualties];
                const totalDefenderCasualties = [...currentPrevDefenderCasualties, ...round.defenderCasualties];
                const attackersAlive = unitListA.filter(u => !totalAttackerCasualties.includes(u.id));
                const defendersAlive = unitListD.filter(u => !totalDefenderCasualties.includes(u.id));
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
                const attackersAlive = unitListA.filter(u => !totalAttackerCasualties.includes(u.id));
                const defendersAlive = unitListD.filter(u => !totalDefenderCasualties.includes(u.id));
                setAttackerWon(defendersAlive.length === 0 && attackersAlive.length > 0);
                setDefenderWon(attackersAlive.length === 0 && defendersAlive.length > 0);
              }
              setTimeout(() => {
                setShowCasualtyBadges(true);
                setShowHits(true);
                setCombatPhase('showing_result');
                setTimeout(() => setCombatPhase('complete'), 1500);
              }, 400);
            } else {
              setCombatPhase('awaiting_decision');
            }
          }, delayMs);
        };

        if (terrorReroll?.applied && terrorReroll.finalRound) {
          setTimeout(() => {
            setTerrorRerollPhase('rerolled');
            setTerrorFinalRound(terrorReroll.finalRound!);
            onTerrorRerollShown?.();
          }, TERROR_REROLL_PAUSE_MS);
          setTimeout(() => setShowHits(true), TERROR_REROLL_PAUSE_MS + 400);
          setTimeout(() => setShowCasualtyBadges(true), TERROR_REROLL_PAUSE_MS + 600);
          scheduleResultOrDecision(750 + TERROR_REROLL_PAUSE_MS);
        } else {
          setTimeout(() => setShowHits(true), 280);
          setTimeout(() => setShowCasualtyBadges(true), 480);
          scheduleResultOrDecision(750);
        }
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
  }, [getRowOrder, attackerUnitsThisRound, defenderUnitsThisRound]);

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
    setTerrorFinalRound(null);

    const result = await onStartRound();
    if (!result) {
      setCombatPhase(roundNumber === 0 ? 'ready' : 'awaiting_decision');
      return;
    }

    const newRoundNumber = result.round.roundNumber;
    setRoundNumber(newRoundNumber);

    const prevA = overridePrevAttacker ?? previousAttackerCasualties;
    const prevD = overridePrevDefender ?? previousDefenderCasualties;

    const terrorApplied = result.terrorReroll?.applied ?? result.round.terrorApplied ?? false;
    const hasInitialGrouped = terrorApplied && result.terrorReroll?.defender_dice_initial_grouped &&
      Object.keys(result.terrorReroll.defender_dice_initial_grouped).length > 0;

    if (hasInitialGrouped) {
      const initialGrouped = result.terrorReroll!.defender_dice_initial_grouped!;
      const initialDefenderHits = Object.values(initialGrouped).reduce((s, g) => s + (g.hits ?? 0), 0);
      setTerrorRerollPhase('initial');
      setShowTerrorRerollX(false);
      // Only use backend's defender_rerolled_indices_by_stat (cap is 3 dice; do not infer all hits as re-rolled)
      setTerrorRerolledIndicesByStat(result.terrorReroll?.defender_rerolled_indices_by_stat ?? {});
      setCurrentRound({
        ...result.round,
        defenderRolls: groupedToDefenderRolls(initialGrouped),
        defenderHits: initialDefenderHits,
      });
      // Show red X on re-rolled dice only after 600ms so user sees the dice first (schedule here so effect cleanup can't cancel it)
      setTimeout(() => setShowTerrorRerollX(true), 600);
    } else {
      setTerrorRerollPhase(null);
      setTerrorRerolledIndicesByStat({});
      setTerrorFinalRound(null);
      setCurrentRound(result.round);
    }

    animateDiceReveals(
      result.round,
      prevA,
      prevD,
      result.combatOver ? { combatOver: true, attackerWon: result.attackerWon } : undefined,
      hasInitialGrouped ? { applied: true, finalRound: result.round } : terrorApplied ? { applied: true } : undefined
    );
  }, [onStartRound, animateDiceReveals, previousAttackerCasualties, previousDefenderCasualties, roundNumber, groupedToDefenderRolls]);

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

  // Hit counts must match the displayed dice. Derive from rolls so the bottom number never disagrees with the dice.
  const effectiveAttackerHits = currentRound?.attackerRolls
    ? Object.values(currentRound.attackerRolls).flat().filter((r) => r.isHit).length
    : (currentRound?.attackerHits ?? 0);
  // After Terror re-roll, defender count comes from final round; otherwise derive from displayed defender rolls
  const effectiveDefenderHits = (terrorRerollPhase === 'rerolled' && terrorFinalRound)
    ? terrorFinalRound.defenderHits
    : currentRound?.defenderRolls
      ? Object.values(currentRound.defenderRolls).flat().filter((r) => r.isHit).length
      : (currentRound?.defenderHits ?? 0);

  // Re-rolled dice per stat (from final round at rerolled indices) to show on shelf next to original dice
  const terrorRerolledDiceByStat = useMemo((): Record<string, DiceRoll[]> => {
    if (!terrorFinalRound || !terrorRerolledIndicesByStat || Object.keys(terrorRerolledIndicesByStat).length === 0)
      return {};
    const out: Record<string, DiceRoll[]> = {};
    for (const [statStr, indices] of Object.entries(terrorRerolledIndicesByStat)) {
      const rollsForStat = terrorFinalRound.defenderRolls[Number(statStr)] ?? terrorFinalRound.defenderRolls[statStr as unknown as number];
      if (!rollsForStat || indices.length === 0) continue;
      const rerolled = indices.map(i => rollsForStat[i]).filter(Boolean);
      if (rerolled.length > 0) out[statStr] = rerolled;
    }
    return out;
  }, [terrorFinalRound, terrorRerolledIndicesByStat]);

  return (
    <div className="modal-overlay">
      <div className="modal combat-modal">
        <header className="modal-header">
          <h2>Battle for {territoryName}</h2>
          <div className="combat-header-indicators">
            {currentRound?.isArcherPrefire && (
              <span className="round-indicator">Archers</span>
            )}
            {roundNumber > 0 && !currentRound?.isArcherPrefire && (
              <span className="round-indicator">Round {roundNumber}</span>
            )}
          </div>
        </header>

        <div className="combat-arena">
          <CombatSide
            title={attacker.factionName}
            factionIcon={attacker.factionIcon}
            factionColor={attacker.factionColor}
            units={attackerUnitsToShow}
            rolls={currentRound?.attackerRolls || {}}
            hits={effectiveAttackerHits}
            countCasualties={allAttackerCasualties}
            badgeHitsByUnitType={badgeHitsAttacker}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={true}
            isArcherPrefire={currentRound?.isArcherPrefire}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            showHits={showHits}
          />

          <div className="vs-divider">
            {currentRound?.terrorApplied && (
              <span className="vs-divider-terror">Terror</span>
            )}
            <span>VS</span>
            {showRollingIndicator && (
              <div className="rolling-indicator">Fighting...</div>
            )}
          </div>

          <CombatSide
            title={defender.factionName}
            factionIcon={defender.factionIcon}
            factionColor={defender.factionColor}
            units={defenderUnitsToShow}
            rolls={currentRound?.defenderRolls || {}}
            hits={effectiveDefenderHits}
            countCasualties={allDefenderCasualties}
            badgeHitsByUnitType={badgeHitsDefender}
            badgeHitsPerShelf={badgeHitsDefenderPerShelf}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={false}
            isArcherPrefire={currentRound?.isArcherPrefire}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            showHits={showHits}
            defenderRerolledIndicesByStat={(terrorRerollPhase === 'initial' || terrorRerollPhase === 'rerolled') ? terrorRerolledIndicesByStat : undefined}
            defenderRerolledDiceByStat={terrorRerollPhase === 'rerolled' ? terrorRerolledDiceByStat : undefined}
            showRerollX={showTerrorRerollX}
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
