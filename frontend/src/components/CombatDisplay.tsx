import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  playArcherPrefireCommenceSound,
  playCombatDiceShelfRevealSound,
  playSiegeworksRoundCommenceSound,
  stopSiegeworksRoundCommenceSound,
} from '../audio/gameAudio';
import './CombatDisplay.css';

/**
 * Live territory state drops dead instance ids; standard combat shelves must still show every stack
 * that existed at battle open, with red X when fully dead. Merge snapshot + live survivors + casualty ids.
 */
function mergeBattleSnapshotWithLiveAndCasualties(
  snapshot: CombatUnit[] | null,
  liveUnits: CombatUnit[],
  casualtyIds: Set<string>,
): CombatUnit[] | null {
  if (!snapshot?.length) return null;
  const liveMap = new Map(liveUnits.map(u => [u.id, u]));
  return snapshot.map(su => {
    const live = liveMap.get(su.id);
    if (live) return live;
    if (casualtyIds.has(su.id)) return { ...su, remainingHealth: 0 };
    return su;
  });
}

/**
 * While viewing a resolved round, shelf units + badges must match that round's backend snapshot
 * (captain boost, terrain, effective stats). API `combat_specials` after the round is recomputed on
 * survivors only, so we must not rebuild shelves from live state for the current round.
 * Append stacks eliminated in *prior* rounds (not in round snapshot) from the battle-open snapshot.
 */
function augmentRoundStartWithPriorEliminated(
  roundUnits: CombatUnit[],
  snapshot: CombatUnit[] | null,
  previousCasualties: string[],
): CombatUnit[] {
  if (!snapshot?.length || !previousCasualties.length) return roundUnits;
  const have = new Set(roundUnits.map(u => u.id));
  const prevDead = new Set(previousCasualties);
  const extra: CombatUnit[] = [];
  for (const su of snapshot) {
    if (have.has(su.id)) continue;
    if (prevDead.has(su.id)) {
      extra.push({ ...su, remainingHealth: 0 });
    }
  }
  return extra.length ? [...roundUnits, ...extra] : roundUnits;
}

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
  /** True if unit has archer special (tag or specials list); used to show only archers during prefire. */
  isArcher?: boolean;
  health: number;
  remainingHealth: number;
  /** Defender units only: border color for this unit's faction (not territory owner). */
  factionColor?: string;
  /** Unit's owning faction id (from definitions); used for result-banner plurality tinting. */
  factionId?: string;
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
  /** Attacker only: unit has Sea Raider and is in a sea raid (+1 attack). */
  hasSeaRaider?: boolean;
  /** Defender only: unit is archer (prefire). */
  hasArcher?: boolean;
  /** Attacker only: unit has Stealth (prefire when all attackers have it). */
  hasStealth?: boolean;
  /** Attacker only: unit has Bombikazi (self-destruct with bomb). */
  hasBombikazi?: boolean;
  /** Defender only: unit has Fearless (immune to terror), when attackers have terror. */
  hasFearless?: boolean;
  /** Defender only: unit has Hope (cancels 1 terror), when attackers have terror. */
  hasHope?: boolean;
  /** Attacker only: unit has ram special (from backend round snapshot / combat_specials). */
  hasRam?: boolean;
  /** Siegework archetype (from backend); used to shelf non-rolling engines in standard combat. */
  siegeworkArchetype?: boolean;
  /** Embarked units on this naval unit (map/modal parity). */
  passengerCount?: number;
}

/** Cumulative eliminated stack (all units in this group died in a prior round). Shown with red X, no rolls. */
interface EliminatedStack {
  unitType: string;
  unitKey?: string;
  name: string;
  icon: string;
  health: number;
  statValue: number;
  count: number;
  factionColor?: string;
  factionId?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasAntiCavalry?: boolean;
  hasSeaRaider?: boolean;
  hasArcher?: boolean;
  hasStealth?: boolean;
  hasBombikazi?: boolean;
  hasFearless?: boolean;
  hasHope?: boolean;
  hasRam?: boolean;
}

export interface DiceRoll {
  value: number;
  target: number;
  isHit: boolean;
}

export type AttackerDiceAtStat =
  | { mode: 'flat'; rolls: DiceRoll[] }
  | {
      mode: 'ladder';
      segments: Array<{
        rolls: DiceRoll[];
        onLadder: boolean;
        unitType: string;
        unitCount: number;
      }>;
    }
  | {
      mode: 'siegework_ram_flex';
      ram: { rolls: DiceRoll[] };
      flex: { rolls: DiceRoll[] };
    };

function isAttackerLadderDice(d: AttackerDiceAtStat | undefined): d is Extract<AttackerDiceAtStat, { mode: 'ladder' }> {
  return !!d && typeof d === 'object' && 'mode' in d && d.mode === 'ladder';
}

function isAttackerSiegeworkRamFlexDice(
  d: AttackerDiceAtStat | undefined
): d is Extract<AttackerDiceAtStat, { mode: 'siegework_ram_flex' }> {
  return !!d && typeof d === 'object' && 'mode' in d && d.mode === 'siegework_ram_flex';
}

function attackerStatRowHasRolls(ar: AttackerDiceAtStat | undefined): boolean {
  if (!ar) return false;
  if (ar.mode === 'ladder') return ar.segments.some(s => s.rolls.length > 0);
  if (ar.mode === 'siegework_ram_flex') {
    return (ar.ram.rolls?.length ?? 0) > 0 || (ar.flex.rolls?.length ?? 0) > 0;
  }
  return (ar.rolls?.length ?? 0) > 0;
}

/** True when this stat shelf has dice this round (reveal order applies). Ghost-only rows from prior rounds have no dice → show red X, not a fake count. */
function rowShelfHasDiceRolls(
  rolls: DiceRoll[],
  isAttacker: boolean,
  attackerLadderDice: AttackerDiceAtStat | undefined,
  siegeworkRamFlexDice: AttackerDiceAtStat | undefined,
): boolean {
  if (rolls.length > 0) return true;
  if (isAttacker && attackerLadderDice) return attackerStatRowHasRolls(attackerLadderDice);
  if (isAttacker && siegeworkRamFlexDice) return attackerStatRowHasRolls(siegeworkRamFlexDice);
  return false;
}

function sumAttackerDiceHits(rolls: Record<number, AttackerDiceAtStat>): number {
  let n = 0;
  for (const ar of Object.values(rolls)) {
    if (ar.mode === 'ladder') {
      for (const seg of ar.segments) n += seg.rolls.filter(r => r.isHit).length;
    } else if (ar.mode === 'siegework_ram_flex') {
      n += ar.ram.rolls.filter(r => r.isHit).length;
      n += ar.flex.rolls.filter(r => r.isHit).length;
    } else {
      n += ar.rolls.filter(r => r.isHit).length;
    }
  }
  return n;
}

/** Full round result from backend (combat_round_resolved). Single source of truth for in-round display. */
export interface CombatRound {
  roundNumber: number;
  attackerRolls: Record<number, AttackerDiceAtStat>;
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
  isStealthPrefire?: boolean;
  isSiegeworksRound?: boolean;
  terrorApplied?: boolean;
  /** Defender dice re-rolled due to terror (round 1); label shows count, e.g. 2 Terror. */
  terrorRerollCount?: number;
  /** Units at round start (from backend). Always present; use this for round display, not state. */
  attackerUnitsAtStart: CombatUnit[];
  defenderUnitsAtStart: CombatUnit[];
  /**
   * Attacker infantry on ladders for this round (from combat_round_resolved).
   * When set, use for ladder dice shelves; live active_combat ladder list can change after the round.
   */
  ladderInfantryInstanceIds?: string[];
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
  /** True when active combat is a sea raid (attackers staged from sea); retreat is never allowed. */
  seaRaidCombat?: boolean;
  /** True when the next round to run is the dedicated siegeworks round (only siegework units roll). */
  siegeworksPending?: boolean;
  /** True when the next round is defender archer prefire (after siegeworks if both apply). */
  archerPrefirePending?: boolean;
  /** When siegeworks is the next/pending round: only these attacker instance ids appear on shelves (matches backend). */
  siegeworksAttackerInstanceIds?: string[];
  siegeworksDefenderInstanceIds?: string[];
  /** Current attacker casualty priority from backend (best_unit | best_attack). */
  casualtyPriorityAttacker?: string;
  /** Current defender (territory) casualty priority from backend (best_unit | best_defense). */
  casualtyPriorityDefender?: string;
  /** Current must_conquer from backend. */
  mustConquer?: boolean;
  onStartRound: (casualtyOrder?: string, mustConquer?: boolean, fuseBomb?: boolean) => Promise<{
    round: CombatRound;
    combatOver: boolean;
    attackerWon: boolean;
    defenderWon: boolean;
    terrorReroll?: {
      applied: boolean;
      instance_ids?: string[];
      initial_rolls_by_instance?: Record<string, number[]>;
      defender_dice_initial_grouped?: Record<string, { rolls: number[]; hits: number }>;
      defender_rerolled_indices_by_stat?: Record<string, number[]>;
      terror_reroll_count?: number;
    };
  } | null>;
  onRetreat: (territoryId: string) => void;
  onClose: (outcome: { attackerWon: boolean; defenderWon: boolean }, survivingAttackers: CombatUnit[]) => void;
  onCancel?: () => void;
  onHighlightTerritories?: (territoryIds: string[]) => void;
  /** Special definitions from setup (for display codes in unit badges: T, M, FR, etc.). */
  specials?: Record<string, { name?: string; display_code?: string }>;
  /** When true (spectator view): no actions, only a close button to dismiss. */
  readOnly?: boolean;
  /** Backend active_combat.combat_log for spectator sync (rounds from poll). */
  combatLog?: unknown[];
  /** When combat ended while spectating: show result then auto-close after 3s. */
  combatEndResult?: { attackerWon: boolean; defenderWon: boolean } | null;
  /** Cumulative hits received by attacker for the whole battle (from backend). */
  cumulativeHitsReceivedByAttacker?: number;
  /** Cumulative hits received by defender for the whole battle (from backend). */
  cumulativeHitsReceivedByDefender?: number;
  /** When defending a stronghold: current and base HP (stronghold soaks hits first; show on defender side). */
  defenderStrongholdHp?: { current: number; base: number };
  /** Attacker instance IDs on ladders (capacity = ladder units' transport_capacity sum). */
  ladderInfantryInstanceIds?: string[];
  /** Number of ladder siege pieces (for ×N); from backend when capacity varies per piece. */
  ladderEquipmentCount?: number;
  /** Unit type ids with ram special (attacker): show R badge only during siegeworks vs stronghold. */
  attackerRamUnitTypes?: string[];
  /** Unit defs (archetype, specials) for siegework shelf split (ladder vs other in siegeworks round). */
  combatUnitDefs?: Record<string, { archetype?: string; specials?: string[] }>;
  /** When true, show Fuse bomb Yes/No before Start (attacker has bomb + bombikazi pairing possible). */
  attackerHasFuseBombOption?: boolean;
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

type UnitRowGroup = {
  unitKey: string;
  unitType: string;
  name: string;
  icon: string;
  health: number;
  total: number;
  countCasualties: number;
  /** Full-stack deaths for red X (delayed until dice reveal like countCasualties when fullCasualtyIds tracks that). */
  countFullCasualties: number;
  factionColor?: string;
  factionId?: string;
  hasTerror?: boolean;
  terrainMountain?: boolean;
  terrainForest?: boolean;
  hasCaptainBonus?: boolean;
  hasAntiCavalry?: boolean;
  hasSeaRaider?: boolean;
  hasArcher?: boolean;
  hasStealth?: boolean;
  hasBombikazi?: boolean;
  hasFearless?: boolean;
  hasHope?: boolean;
  hasRam?: boolean;
  passengerCount?: number;
  isCumulativeEliminated?: boolean;
  groupKey?: string;
};

function buildUnitRowGroupList(
  units: CombatUnit[],
  eliminatedGroups: EliminatedStack[],
  countCasualties: string[],
  fullCasualtyIds?: string[],
): UnitRowGroup[] {
  const countCasualtySet = new Set(countCasualties);
  const fullCasualtySet = fullCasualtyIds !== undefined ? new Set(fullCasualtyIds) : countCasualtySet;
  const groupMap = new Map<string, {
    name: string;
    icon: string;
    health: number;
    total: number;
    countCasualties: number;
    countFullCasualties: number;
    factionColor?: string;
    hasTerror?: boolean;
    terrainMountain?: boolean;
    terrainForest?: boolean;
    hasCaptainBonus?: boolean;
    hasAntiCavalry?: boolean;
    hasSeaRaider?: boolean;
    hasArcher?: boolean;
    hasStealth?: boolean;
    hasBombikazi?: boolean;
    hasFearless?: boolean;
    hasHope?: boolean;
    hasRam?: boolean;
    passengerCount?: number;
  }>();

  units.forEach(unit => {
    const unitGroupKey = `${unit.unitType}::${unit.factionId ?? unit.factionColor ?? ''}::${unit.health}`;
    const existing = groupMap.get(unitGroupKey);
    const inCountCasualties = countCasualtySet.has(unit.id);
    const inFullCasualties = fullCasualtySet.has(unit.id);
    if (existing) {
      existing.total++;
      if (inCountCasualties) existing.countCasualties++;
      if (inFullCasualties) existing.countFullCasualties++;
      if (unit.hasTerror) existing.hasTerror = true;
      if (unit.terrainMountain) existing.terrainMountain = true;
      if (unit.terrainForest) existing.terrainForest = true;
      if (unit.hasCaptainBonus) existing.hasCaptainBonus = true;
      if (unit.hasAntiCavalry) existing.hasAntiCavalry = true;
      if (unit.hasSeaRaider) existing.hasSeaRaider = true;
      if (unit.hasArcher) existing.hasArcher = true;
      if (unit.hasStealth) existing.hasStealth = true;
      if (unit.hasBombikazi) existing.hasBombikazi = true;
      if (unit.hasFearless) existing.hasFearless = true;
      if (unit.hasHope) existing.hasHope = true;
      existing.passengerCount = (existing.passengerCount ?? 0) + (unit.passengerCount ?? 0);
    } else {
      groupMap.set(unitGroupKey, {
        name: unit.name,
        icon: unit.icon,
        health: unit.health,
        total: 1,
        countCasualties: inCountCasualties ? 1 : 0,
        countFullCasualties: inFullCasualties ? 1 : 0,
        factionColor: unit.factionColor,
        hasTerror: unit.hasTerror,
        terrainMountain: unit.terrainMountain,
        terrainForest: unit.terrainForest,
        hasCaptainBonus: unit.hasCaptainBonus,
        hasAntiCavalry: unit.hasAntiCavalry,
        hasSeaRaider: unit.hasSeaRaider,
        hasArcher: unit.hasArcher,
        hasStealth: unit.hasStealth,
        hasBombikazi: unit.hasBombikazi,
        hasFearless: unit.hasFearless,
        hasHope: unit.hasHope,
        hasRam: unit.hasRam,
        passengerCount: unit.passengerCount ?? 0,
      });
    }
  });

  const unitGroups: UnitRowGroup[] = [];
  groupMap.forEach((value, key) => {
    const unitType = key.split('::')[0] ?? key;
    unitGroups.push({ unitKey: key, unitType, ...value });
  });

  eliminatedGroups.forEach((es, idx) => {
    const eliminatedKey = es.unitKey ?? `${es.unitType}::${es.factionId ?? es.factionColor ?? ''}::${es.health}`;
    if (groupMap.has(eliminatedKey)) return;
    unitGroups.push({
      unitKey: eliminatedKey,
      unitType: es.unitType,
      name: es.name,
      icon: es.icon,
      health: es.health,
      total: es.count,
      countCasualties: es.count,
      countFullCasualties: es.count,
      factionColor: es.factionColor,
      factionId: es.factionId,
      hasTerror: es.hasTerror,
      terrainMountain: es.terrainMountain,
      terrainForest: es.terrainForest,
      hasCaptainBonus: es.hasCaptainBonus,
      hasAntiCavalry: es.hasAntiCavalry,
      hasSeaRaider: es.hasSeaRaider,
      hasArcher: es.hasArcher,
      hasStealth: es.hasStealth,
      hasBombikazi: es.hasBombikazi,
      hasFearless: es.hasFearless,
      hasHope: es.hasHope,
      hasRam: es.hasRam,
      isCumulativeEliminated: true,
      groupKey: `elim-${es.unitType}-${idx}`,
    });
  });

  return unitGroups;
}

// Unit row: countCasualties = delayed (prior deaths only until round reveal ends) so unrevealed shelves keep full counts.
// fullCasualtyIds = same delay as countCasualties unless omitted — red X on eliminated stacks only after reveal, then kept for later rounds via previous* casualties.
// badgeHitsByUnitType = hits to show per stack; onlyShowBadgeForHpGreaterThanOne = true at round start (HP=1 stacks never show a wound badge then).
function UnitRow({
  statValue,
  units,
  eliminatedGroups = [],
  rolls,
  countCasualties,
  fullCasualtyIds,
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
  specials: specialsDefs,
  showRamSiegeworkForAttacker = false,
  ramAttackerUnitTypeSet,
  hideStatLabel = false,
  /** When hideStatLabel: text in the stat column (default em dash so the row isn’t blank). */
  bottomRowStatLabel = '—',
  attackerLadderDice,
  ladderInstanceIds,
  ladderSegmentHits,
  /** Siegework bottom row: show empty die outlines (ladder / inactive ram) next to units. */
  siegeworkBottomBlankDice = false,
  siegeworkRamFlexDice,
}: {
  statValue: number;
  units: CombatUnit[];
  /** Cumulative stacks eliminated in prior rounds (show with red X, no rolls). */
  eliminatedGroups?: EliminatedStack[];
  rolls: DiceRoll[];
  countCasualties: string[];
  /** When set, stack is fully eliminated if all its units appear here (typically delayed until dice reveal completes). */
  fullCasualtyIds?: string[];
  badgeHitsByUnitType: Record<string, number>;
  onlyShowBadgeForHpGreaterThanOne: boolean;
  showCasualtyBadges: boolean;
  isAttacker: boolean;
  revealedRows: Set<string>;
  currentRowKey: string | null;
  isLanding: boolean;
  hitColor?: string;
  rerolledIndices?: number[];
  rerolledDice?: DiceRoll[];
  showRerollX?: boolean;
  specials?: Record<string, { name?: string; display_code?: string }>;
  showRamSiegeworkForAttacker?: boolean;
  ramAttackerUnitTypeSet?: Set<string>;
  hideStatLabel?: boolean;
  bottomRowStatLabel?: string;
  attackerLadderDice?: AttackerDiceAtStat;
  ladderInstanceIds?: Set<string>;
  /** Per-segment hit badges (aligned with ladder segments). */
  ladderSegmentHits?: number[];
  siegeworkBottomBlankDice?: boolean;
  /** Dedicated siegework round: ram vs flexible dice on separate sub-rows (same stat shelf). */
  siegeworkRamFlexDice?: AttackerDiceAtStat;
}) {
  // Some siegework UI flags are threaded through from callers; keep this variable "used"
  // until ram/siegework-specific rendering is wired up in this component.
  void showRamSiegeworkForAttacker;
  const code = (id: string, fallback: string) => (specialsDefs?.[id]?.display_code ?? fallback);
  const ladderTitle = specialsDefs?.ladder?.name
    ? `${code('ladder', 'L')}=${specialsDefs.ladder.name}`
    : 'L=Ladder (on wall; hits bypass stronghold)';
  const badgeTitle = [
    specialsDefs?.ladder?.name && ladderTitle,
    specialsDefs?.ram?.name && `${code('ram', 'R')}=${specialsDefs.ram.name}`,
    specialsDefs?.terror?.name && `${code('terror', 'T')}=${specialsDefs.terror.name}`,
    specialsDefs?.mountain?.name && `${code('mountain', 'M')}=${specialsDefs.mountain.name}`,
    specialsDefs?.forest?.name && `${code('forest', 'FR')}=${specialsDefs.forest.name}`,
    specialsDefs?.captain?.name && `${code('captain', 'C')}=${specialsDefs.captain.name}`,
    specialsDefs?.anti_cavalry?.name && `${code('anti_cavalry', 'AC')}=${specialsDefs.anti_cavalry.name}`,
    specialsDefs?.sea_raider?.name && `${code('sea_raider', 'SR')}=${specialsDefs.sea_raider.name}`,
    specialsDefs?.archer?.name && `${code('archer', 'AR')}=${specialsDefs.archer.name}`,
    specialsDefs?.stealth?.name && `${code('stealth', 'ST')}=${specialsDefs.stealth.name}`,
    specialsDefs?.bombikazi?.name && `${code('bombikazi', 'B')}=${specialsDefs.bombikazi.name}`,
    specialsDefs?.fearless?.name && `${code('fearless', 'FL')}=${specialsDefs.fearless.name}`,
    specialsDefs?.hope?.name && `${code('hope', 'HP')}=${specialsDefs.hope.name}`,
  ].filter(Boolean).join(', ') || 'L=Ladder, R=Ram (siegeworks), T=Terror, M=Mountain, FR=Forest, C=Captain, AC=Anti-cavalry, SR=Sea Raider, AR=Archer, ST=Stealth, B=Bombikazi, FL=Fearless, HP=Hope';

  const rowKey = hideStatLabel
    ? `${isAttacker ? 'attacker' : 'defender'}_siegework_noroll`
    : `${isAttacker ? 'attacker' : 'defender'}_${statValue}`;
  /* Non-rolling siegework row has no dice step; keep shelf visible (not dimmed). */
  const isRevealed = hideStatLabel || revealedRows.has(rowKey);
  const isCurrentlyLanding = currentRowKey === rowKey && isLanding;
  const rerolledSet = new Set(rerolledIndices ?? []);
  const hasRerolledDice = (rerolledDice?.length ?? 0) > 0;
  const showX = showRerollX === true;

  const countCasualtySet = new Set(countCasualties);
  const fullCasualtySet = fullCasualtyIds !== undefined ? new Set(fullCasualtyIds) : countCasualtySet;
  const ladderSet = ladderInstanceIds ?? new Set<string>();

  /** Attacker row: ladder dice are shown in two rows (Ladder dice above, non-ladder below). */
  if (isAttacker && isAttackerLadderDice(attackerLadderDice)) {
    const segs = attackerLadderDice.segments;
    const ladderSegs = segs.filter(s => s.onLadder);
    const offSegs = segs.filter(s => !s.onLadder);
    const ladderDice = ladderSegs.flatMap(s => s.rolls);
    const offDice = offSegs.flatMap(s => s.rolls);

    const renderDice = (dice: Array<{ value: number; target: number; isHit: boolean }>) =>
      dice.map((roll, i) => (
        <Die
          key={i}
          value={roll.value}
          isHit={roll.isHit}
          isLanding={isCurrentlyLanding}
          isVisible={isRevealed}
          hitColor={hitColor}
          isRerolled={false}
        />
      ));

    return (
      <div className={`unit-row-shelf ${isRevealed ? 'revealed' : ''} unit-row-shelf--attacker-ladder`}>
        <div className={`stat-label ${hideStatLabel ? 'stat-label--plain' : ''}`} aria-hidden={hideStatLabel}>
          {hideStatLabel ? '\u00a0' : statValue}
        </div>

        <div className="attacker-ladder-grid">
          {/* Units column spans both dice rows so icons don't vertically shift. */}
          <div className="unit-stack attacker-ladder-units">
            {segs.map((seg, segIdx) => {
              const pool = units.filter(
                u => u.unitType === seg.unitType && ladderSet.has(u.id) === seg.onLadder
              );
              const u0 = pool[0];
              const total = pool.length || seg.unitCount;
              const countCasInPoolDelayed = pool.filter(u => countCasualtySet.has(u.id)).length;
              const countCasInPoolFull = pool.filter(u => fullCasualtySet.has(u.id)).length;
              const aliveCount = total - countCasInPoolDelayed;
              const eliminatedFull = total > 0 && countCasInPoolFull === total;
              const hasDiceInSeg = seg.rolls.length > 0;
              const showEliminated = eliminatedFull && (!hasDiceInSeg || isRevealed);
              const segHits = ladderSegmentHits?.[segIdx] ?? 0;
              const showBadge =
                segHits > 0 &&
                showCasualtyBadges &&
                (!onlyShowBadgeForHpGreaterThanOne || (u0?.health ?? 1) > 1);
              const codes: string[] = [];
              if (u0?.hasTerror) codes.push(code('terror', 'T'));
              if (u0?.terrainMountain) codes.push(code('mountain', 'M'));
              if (u0?.terrainForest) codes.push(code('forest', 'FR'));
              if (u0?.hasCaptainBonus) codes.push(code('captain', 'C'));
              if (u0?.hasAntiCavalry) codes.push(code('anti_cavalry', 'AC'));
              if (u0?.hasSeaRaider) codes.push(code('sea_raider', 'SR'));
              if (seg.onLadder) codes.push(code('ladder', 'L'));
              const paxLadder = pool.reduce((s, u) => s + (u.passengerCount ?? 0), 0);

              return (
                <div
                  key={`ladder-unit-${segIdx}-${seg.onLadder}-${seg.unitType}`}
                  className={`combat-unit-group ${showEliminated ? 'eliminated' : ''}`}
                  title={u0?.name ?? seg.unitType}
                  style={(u0?.factionColor ?? hitColor) ? { borderColor: u0?.factionColor ?? hitColor } : undefined}
                >
                  {codes.length > 0 && (
                    <span className="unit-specials-badge" title={badgeTitle}>
                      {codes.join('')}
                    </span>
                  )}
                  {u0 && <img src={u0.icon} alt={u0.name} />}
                  {showEliminated ? (
                    <span className="unit-stack-eliminated-x" aria-hidden title="Stack eliminated">×</span>
                  ) : (
                    <span className="unit-count">{aliveCount > 0 ? aliveCount : eliminatedFull ? '' : total}</span>
                  )}
                  {!showEliminated && paxLadder > 0 && (
                    <span className="combat-unit-passenger-badge" title={`${paxLadder} aboard`}>{paxLadder}</span>
                  )}
                  {showBadge && (
                    <span className="hits-badge" title="Hits received">{segHits}</span>
                  )}
                </div>
              );
            })}
          </div>

          {/* Dice rows column (label left of dice, like ram shelf) */}
          <div className="attacker-ladder-dice-row attacker-ladder-dice-row--ladder">
            {ladderDice.length > 0 ? (
              <>
                <span className="siegework-split-row-label" title={ladderTitle}>L</span>
                <div className="dice-stack attacker-ladder-dice-stack">{renderDice(ladderDice)}</div>
              </>
            ) : (
              <>
                <span className="siegework-split-row-label siegework-split-row-label--spacer" aria-hidden />
                <div className="dice-stack attacker-ladder-dice-stack" />
              </>
            )}
          </div>

          <div className="attacker-ladder-dice-row attacker-ladder-dice-row--off">
            {offDice.length > 0 ? (
              <>
                <span className="siegework-split-row-label siegework-split-row-label--spacer" aria-hidden />
                <div className="dice-stack attacker-ladder-dice-stack">{renderDice(offDice)}</div>
              </>
            ) : (
              <>
                <span className="siegework-split-row-label siegework-split-row-label--spacer" aria-hidden />
                <div className="dice-stack attacker-ladder-dice-stack" />
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  /** No dice yet (ready / siegework): split climbers on same attack shelf */
  if (
    isAttacker &&
    ladderSet.size > 0 &&
    rolls.length === 0 &&
    units.some(u => ladderSet.has(u.id)) &&
    !attackerLadderDice
  ) {
    const off = units.filter(u => !ladderSet.has(u.id));
    const on = units.filter(u => ladderSet.has(u.id));
    const stacks: { pool: CombatUnit[]; onLadder: boolean }[] = [];
    if (off.length) stacks.push({ pool: off, onLadder: false });
    if (on.length) stacks.push({ pool: on, onLadder: true });
    if (stacks.length >= 1) {
      return (
        <div className={`unit-row-shelf ${isRevealed ? 'revealed' : ''} unit-row-shelf--attacker-ladder`}>
          <div className={`stat-label ${hideStatLabel ? 'stat-label--plain' : ''}`} aria-hidden={hideStatLabel}>
            {hideStatLabel ? '\u00a0' : statValue}
          </div>
          <div className="attacker-ladder-segments attacker-ladder-segments--nodice">
            {stacks.map(({ pool, onLadder }, idx) => {
              const byType = new Map<string, CombatUnit[]>();
              pool.forEach(u => {
                if (!byType.has(u.unitType)) byType.set(u.unitType, []);
                byType.get(u.unitType)!.push(u);
              });
              return (
                <div key={idx} className="attacker-ladder-segment">
                  {Array.from(byType.entries()).map(([ut, plist]) => {
                    const uu = plist[0];
                    const total = plist.length;
                    const ccDelayed = plist.filter(u => countCasualtySet.has(u.id)).length;
                    const ccFull = plist.filter(u => fullCasualtySet.has(u.id)).length;
                    const alive = total - ccDelayed;
                    const paxSeg = plist.reduce((s, u) => s + (u.passengerCount ?? 0), 0);
                    const codes: string[] = [];
                    if (uu.hasTerror) codes.push(code('terror', 'T'));
                    if (uu.terrainMountain) codes.push(code('mountain', 'M'));
                    if (uu.terrainForest) codes.push(code('forest', 'FR'));
                    if (uu.hasCaptainBonus) codes.push(code('captain', 'C'));
                    if (uu.hasAntiCavalry) codes.push(code('anti_cavalry', 'AC'));
                    if (uu.hasSeaRaider) codes.push(code('sea_raider', 'SR'));
                    if (uu.hasRam) codes.push(code('ram', 'R'));
                    if (onLadder) codes.push(code('ladder', 'L'));
                    const eliminatedAllFull = total > 0 && ccFull === total;
                    /** No dice on this shelf yet: prior-round ghosts show red X, not a stale count. */
                    const showEliminatedNodice = eliminatedAllFull;
                    return (
                      <div key={ut + String(onLadder)} className="unit-stack unit-stack--segment">
                        <div
                          className={`combat-unit-group ${showEliminatedNodice ? 'eliminated' : ''}`}
                          title={uu.name}
                          style={(uu.factionColor ?? hitColor) ? { borderColor: uu.factionColor ?? hitColor } : undefined}
                        >
                          {codes.length > 0 && (
                            <span className="unit-specials-badge" title={badgeTitle}>
                              {codes.join('')}
                            </span>
                          )}
                          <img src={uu.icon} alt={uu.name} />
                          {showEliminatedNodice ? (
                            <span className="unit-stack-eliminated-x" aria-hidden>×</span>
                          ) : (
                            <span className="unit-count">{alive > 0 ? alive : eliminatedAllFull ? '' : total}</span>
                          )}
                          {!showEliminatedNodice && paxSeg > 0 && (
                            <span className="combat-unit-passenger-badge" title={`${paxSeg} aboard`}>{paxSeg}</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      );
    }
  }

  if (isAttacker && isAttackerSiegeworkRamFlexDice(siegeworkRamFlexDice)) {
    const sf = siegeworkRamFlexDice;
    const ramSet = ramAttackerUnitTypeSet ?? new Set<string>();
    const ramUnits = units.filter(u => ramSet.has(u.unitType));
    const flexUnits = units.filter(u => !ramSet.has(u.unitType));
    const ramElim = (eliminatedGroups ?? []).filter(es => ramSet.has(es.unitType));
    const flexElim = (eliminatedGroups ?? []).filter(es => !ramSet.has(es.unitType));
    const ramGroups = buildUnitRowGroupList(ramUnits, ramElim, countCasualties, fullCasualtyIds);
    const flexGroups = buildUnitRowGroupList(flexUnits, flexElim, countCasualties, fullCasualtyIds);
    const hasRamRow = ramUnits.length > 0 || sf.ram.rolls.length > 0;
    const hasFlexRow = flexUnits.length > 0 || sf.flex.rolls.length > 0;

    const renderOneGroup = (group: UnitRowGroup, hasSubRowDice: boolean) => {
      const hits = badgeHitsByUnitType[group.unitType] ?? 0;
      const aliveCount = group.total - group.countCasualties;
      const eliminatedFull = group.total > 0 && group.countFullCasualties >= group.total;
      const showEliminated =
        group.isCumulativeEliminated ||
        (eliminatedFull && (!hasSubRowDice || isRevealed));
      const showBadge = hits > 0 && showCasualtyBadges && (!onlyShowBadgeForHpGreaterThanOne || group.health > 1);
      const specialCodes: string[] = [];
      if (group.hasTerror) specialCodes.push(code('terror', 'T'));
      if (group.terrainMountain) specialCodes.push(code('mountain', 'M'));
      if (group.terrainForest) specialCodes.push(code('forest', 'FR'));
      if (group.hasCaptainBonus) specialCodes.push(code('captain', 'C'));
      if (group.hasAntiCavalry) specialCodes.push(code('anti_cavalry', 'AC'));
      if (group.hasSeaRaider) specialCodes.push(code('sea_raider', 'SR'));
      if (group.hasArcher) specialCodes.push(code('archer', 'AR'));
      if (group.hasStealth) specialCodes.push(code('stealth', 'ST'));
      if (group.hasBombikazi) specialCodes.push(code('bombikazi', 'B'));
      if (group.hasFearless) specialCodes.push(code('fearless', 'FL'));
      if (group.hasHope) specialCodes.push(code('hope', 'HP'));
      if (group.hasRam) specialCodes.push(code('ram', 'R'));
      const paxG = group.passengerCount ?? 0;
      return (
        <div
          key={group.groupKey ?? group.unitKey}
          className={`combat-unit-group ${showEliminated ? 'eliminated' : ''}`}
          title={group.name}
          style={(group.factionColor ?? hitColor) ? { borderColor: group.factionColor ?? hitColor } : undefined}
        >
          {specialCodes.length > 0 && (
            <span className="unit-specials-badge" title={badgeTitle}>
              {specialCodes.join('')}
            </span>
          )}
          <img src={group.icon} alt={group.name} />
          {showEliminated ? (
            <span className="unit-stack-eliminated-x" aria-hidden title="Stack eliminated">×</span>
          ) : (
            <span className="unit-count">{aliveCount > 0 ? aliveCount : eliminatedFull ? '' : group.total}</span>
          )}
          {!showEliminated && paxG > 0 && (
            <span className="combat-unit-passenger-badge" title={`${paxG} aboard`}>{paxG}</span>
          )}
          {showBadge && (
            <span className="hits-badge" title="Hits received">{hits}</span>
          )}
        </div>
      );
    };

    const renderDice = (chunk: DiceRoll[]) =>
      chunk.map((roll, i) => (
        <Die
          key={i}
          value={roll.value}
          isHit={roll.isHit}
          isLanding={isCurrentlyLanding}
          isVisible={isRevealed}
          hitColor={hitColor}
          isRerolled={false}
        />
      ));

    return (
      <div className={`unit-row-shelf ${isRevealed ? 'revealed' : ''} unit-row-shelf--siegework-split`}>
        <div className={`stat-label ${hideStatLabel ? 'stat-label--plain' : ''}`} aria-hidden={hideStatLabel}>
          {hideStatLabel ? bottomRowStatLabel : statValue}
        </div>
        <div className="siegework-split-rows">
          {hasRamRow && (
              <div className={`siegework-split-row ${hasFlexRow ? 'siegework-split-row--with-label' : ''}`}>
              <span className="siegework-split-row-label">R</span>
              <div className="siegework-split-row-units-dice">
                <div className="unit-stack">{ramGroups.map(g => renderOneGroup(g, (sf.ram.rolls?.length ?? 0) > 0))}</div>
                <div className="dice-stack">{renderDice(sf.ram.rolls)}</div>
              </div>
            </div>
          )}
          {hasFlexRow && (
            <div
              className={`siegework-split-row ${hasRamRow ? 'siegework-split-row--with-label' : 'siegework-split-row--flex-only'}`}
            >
              {hasRamRow ? (
                <span className="siegework-split-row-label siegework-split-row-label--spacer" aria-hidden>
                  R
                </span>
              ) : null}
              <div className="siegework-split-row-units-dice">
                <div className="unit-stack">{flexGroups.map(g => renderOneGroup(g, (sf.flex.rolls?.length ?? 0) > 0))}</div>
                <div className="dice-stack">{renderDice(sf.flex.rolls)}</div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  const unitGroups = buildUnitRowGroupList(units, eliminatedGroups, countCasualties, fullCasualtyIds);
  const hasDiceInRow = rowShelfHasDiceRolls(rolls, isAttacker, attackerLadderDice, siegeworkRamFlexDice);

  return (
    <div className={`unit-row-shelf ${isRevealed ? 'revealed' : ''}`}>
        <div className={`stat-label ${hideStatLabel ? 'stat-label--plain' : ''}`} aria-hidden={hideStatLabel}>
          {hideStatLabel ? bottomRowStatLabel : statValue}
        </div>
        <div className="unit-stack">
        {unitGroups.map((group) => {
          const hits = badgeHitsByUnitType[group.unitType] ?? 0;
          const aliveCount = group.total - group.countCasualties;
          const eliminatedFull = group.total > 0 && group.countFullCasualties >= group.total;
          /** Cumulative: always X. Ghost shelf (no dice): X when fully dead. With dice: red X once this shelf has been revealed (full casualty list), not only after round-end badges. */
          const showEliminated =
            group.isCumulativeEliminated ||
            (eliminatedFull && (!hasDiceInRow || isRevealed));
          // Only show hit badge after all dice are revealed (showCasualtyBadges), then apply HP filter
          const showBadge = hits > 0 && showCasualtyBadges && (!onlyShowBadgeForHpGreaterThanOne || group.health > 1);
          const specialCodes: string[] = [];
          if (group.hasTerror) specialCodes.push(code('terror', 'T'));
          if (group.terrainMountain) specialCodes.push(code('mountain', 'M'));
          if (group.terrainForest) specialCodes.push(code('forest', 'FR'));
          if (group.hasCaptainBonus) specialCodes.push(code('captain', 'C'));
          if (group.hasAntiCavalry) specialCodes.push(code('anti_cavalry', 'AC'));
          if (group.hasSeaRaider) specialCodes.push(code('sea_raider', 'SR'));
          if (group.hasArcher) specialCodes.push(code('archer', 'AR'));
          if (group.hasStealth) specialCodes.push(code('stealth', 'ST'));
          if (group.hasBombikazi) specialCodes.push(code('bombikazi', 'B'));
          if (group.hasFearless) specialCodes.push(code('fearless', 'FL'));
          if (group.hasHope) specialCodes.push(code('hope', 'HP'));
          if (group.hasRam) specialCodes.push(code('ram', 'R'));
          const paxRow = group.passengerCount ?? 0;
          return (
            <div
              key={group.groupKey ?? group.unitKey}
              className={`combat-unit-group ${showEliminated ? 'eliminated' : ''}`}
              title={group.name}
              style={(group.factionColor ?? hitColor) ? { borderColor: group.factionColor ?? hitColor } : undefined}
            >
              {specialCodes.length > 0 && (
                <span className="unit-specials-badge" title={badgeTitle}>
                  {specialCodes.join('')}
                </span>
              )}
              <img src={group.icon} alt={group.name} />
              {showEliminated ? (
                <span className="unit-stack-eliminated-x" aria-hidden title="Stack eliminated">×</span>
              ) : (
                <span className="unit-count">{aliveCount > 0 ? aliveCount : eliminatedFull ? '' : group.total}</span>
              )}
              {!showEliminated && paxRow > 0 && (
                <span className="combat-unit-passenger-badge" title={`${paxRow} aboard`}>{paxRow}</span>
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
                isLanding={isCurrentlyLanding}
                isVisible={isRevealed}
                hitColor={hitColor}
                isRerolled={false}
              />
            ))}
          </>
        )}
        {siegeworkBottomBlankDice && rolls.length === 0 && unitGroups.length > 0 && (
          Array.from({ length: unitGroups.length }, (_, i) => (
            <div key={`sw-blank-${i}`} className="die die-blank-slot" aria-hidden />
          ))
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
  fullCasualtyIds,
  badgeHitsByUnitType,
  badgeHitsPerShelf,
  onlyShowBadgeForHpGreaterThanOne,
  showCasualtyBadges,
  isAttacker,
  isArcherPrefire: _isArcherPrefire,
  revealedRows,
  currentRowKey,
  isLanding,
  showHits,
  defenderRerolledIndicesByStat,
  defenderRerolledDiceByStat,
  showRerollX,
  specials: specialsForBadges,
  casualtyPriorityLabel,
  eliminatedStacks,
  cumulativeHitsReceived,
  showCumulativeHits = false,
  showRamSiegeworkForAttacker = false,
  ramAttackerUnitTypeSet,
  siegeworkShelfMode = 'none',
  combatUnitDefs = {},
  siegeworkRoundActive = false,
  defenderStrongholdHp,
  ladderInstanceIds: ladderInstanceIdSet,
  attackerRoundCasualties = [],
  attackerRoundWounded = [],
}: {
  title: string;
  factionIcon: string;
  factionColor: string;
  units: CombatUnit[];
  rolls: Record<number, AttackerDiceAtStat | DiceRoll[]>;
  hits: number;
  countCasualties: string[];
  /** All instance ids dead this battle including current round (red X timing). Defaults to countCasualties if omitted. */
  fullCasualtyIds?: string[];
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
  ladderInstanceIds?: Set<string>;
  attackerRoundCasualties?: string[];
  attackerRoundWounded?: string[];
  /** When Terror re-roll: stat -> indices in that row's rolls that were re-rolled (show red X). Defender only. */
  defenderRerolledIndicesByStat?: Record<string, number[]>;
  /** When Terror re-roll: stat -> re-rolled dice to show on shelf next to original. Defender only. */
  defenderRerolledDiceByStat?: Record<string, DiceRoll[]>;
  /** When false, red X on re-rolled dice is hidden (delay until after dice visible). Defender only. */
  showRerollX?: boolean;
  /** Special definitions from setup (display_code for unit badges). */
  specials?: Record<string, { name?: string; display_code?: string }>;
  /** Very small label under unit boxes: e.g. "Casualty Priority: Best Unit" or "Casualty Priority: Best Defense". */
  casualtyPriorityLabel?: string;
  /** Cumulative stacks fully eliminated in prior rounds (show with red X, no rolls). */
  eliminatedStacks?: EliminatedStack[];
  /** Cumulative hits this side has received for the whole battle (show with red Xs under casualty priority). */
  cumulativeHitsReceived?: number;
  /** When true, show cumulative hits (typically after dice for the round are revealed). */
  showCumulativeHits?: boolean;
  /** Attacker only: show R on ram units during siegeworks vs stronghold. */
  showRamSiegeworkForAttacker?: boolean;
  ramAttackerUnitTypeSet?: Set<string>;
  siegeworkShelfMode?: 'none' | 'sw_bottom' | 'ladder_bottom';
  combatUnitDefs?: Record<string, { archetype?: string; specials?: string[] }>;
  /** True during dedicated siegework round (or ready when siegework is next). */
  siegeworkRoundActive?: boolean;
  /** Only demote ram to the bottom (no-stat) row when walls are actually breached. */
  defenderStrongholdHp?: { current: number; base: number };
}) {
  const defs = combatUnitDefs ?? {};
  const inactiveRamToBottom =
    isAttacker &&
    !siegeworkRoundActive &&
    defenderStrongholdHp != null &&
    defenderStrongholdHp.base > 0 &&
    (defenderStrongholdHp.current ?? 0) <= 0;
  const unitToSiegeworkBottom = (u: { unitType: string; siegeworkArchetype?: boolean }): boolean => {
    if (siegeworkShelfMode === 'none') return false;
    const spec = defs[u.unitType]?.specials ?? [];
    const isSw = u.siegeworkArchetype === true || defs[u.unitType]?.archetype === 'siegework';
    if (siegeworkShelfMode === 'sw_bottom') return isSw;
    if (siegeworkShelfMode === 'ladder_bottom') {
      if (isSw && spec.includes('ladder')) return true;
      if (inactiveRamToBottom && spec.includes('ram')) return true;
      return false;
    }
    return false;
  };

  const mainUnits = units.filter(u => !unitToSiegeworkBottom(u));
  const bottomSiegeworkUnits = units.filter(unitToSiegeworkBottom);

  const eliminatedByValue: Record<number, EliminatedStack[]> = {};
  const bottomEliminated: EliminatedStack[] = [];
  (eliminatedStacks ?? []).forEach(es => {
    if (unitToSiegeworkBottom({ unitType: es.unitType })) bottomEliminated.push(es);
    else {
      if (!eliminatedByValue[es.statValue]) eliminatedByValue[es.statValue] = [];
      eliminatedByValue[es.statValue].push(es);
    }
  });

  const unitsByValue: Record<number, CombatUnit[]> = {};
  mainUnits.forEach(unit => {
    const baseAtk = unit.attack;
    const baseDef = unit.defense;
    const effAtk = unit.effectiveAttack ?? baseAtk;
    const effDef = unit.effectiveDefense ?? baseDef;
    const value = isAttacker ? effAtk : effDef;
    if (!unitsByValue[value]) unitsByValue[value] = [];
    unitsByValue[value].push(unit);
  });

  const ladderSetCombat = ladderInstanceIdSet ?? new Set<string>();
  const casThis = new Set(attackerRoundCasualties);
  const woundThis = new Set(attackerRoundWounded);

  const rowHasDiceForRow = (value: number): boolean => {
    const raw = rolls[value];
    if (raw == null) return false;
    if (isAttacker && typeof raw === 'object' && !Array.isArray(raw) && 'mode' in raw) {
      const a = raw as AttackerDiceAtStat;
      if (a.mode === 'ladder') return a.segments.some(s => s.rolls.length > 0);
      if (a.mode === 'siegework_ram_flex') {
        return (a.ram.rolls?.length ?? 0) > 0 || (a.flex.rolls?.length ?? 0) > 0;
      }
      return (a.rolls?.length ?? 0) > 0;
    }
    return Array.isArray(raw) && raw.length > 0;
  };

  const allValues = new Set([
    ...Object.keys(unitsByValue).map(Number),
    ...Object.keys(rolls).map(Number),
    ...Object.keys(eliminatedByValue).map(Number),
  ]);
  const sortedValues = Array.from(allValues)
    .filter((value) => {
      const hasUnits = (unitsByValue[value]?.length ?? 0) > 0;
      const hasRolls = rowHasDiceForRow(value);
      const hasEliminated = (eliminatedByValue[value]?.length ?? 0) > 0;
      return hasUnits || hasRolls || hasEliminated;
    })
    .sort((a, b) => a - b);

  const showBottomRow =
    siegeworkShelfMode !== 'none' &&
    (bottomSiegeworkUnits.length > 0 || bottomEliminated.length > 0);

  return (
    <div
      className={`combat-side ${isAttacker ? 'attacker' : 'defender'}`}
      style={factionColor ? { borderColor: factionColor } : undefined}
    >
      <div className="side-header">
        {factionIcon ? (
          <img src={factionIcon} alt={title} className="faction-icon" />
        ) : null}
        <span className="side-title">{title}</span>
        <div className="side-header-trailing">
          {!isAttacker &&
            defenderStrongholdHp != null &&
            defenderStrongholdHp.base > 0 && (
            <div
              className="combat-stronghold-hp combat-stronghold-hp--header"
              title={`Stronghold walls: ${defenderStrongholdHp.current} / ${defenderStrongholdHp.base} HP (attacker hits soak here first)`}
            >
              <span className="combat-stronghold-hp-icon" aria-hidden>🏰</span>
              <span className="combat-stronghold-hp-value">
                {defenderStrongholdHp.current} / {defenderStrongholdHp.base} HP
              </span>
            </div>
          )}
          <span className="side-role-badge" title={isAttacker ? 'Attacking' : 'Defending'}>
            {isAttacker ? 'A' : 'D'}
          </span>
        </div>
      </div>

      <div className="units-shelves">
        {sortedValues.map(value => {
          const rawRoll = rolls[value];
          const atkDice = isAttacker && rawRoll && typeof rawRoll === 'object' && 'mode' in rawRoll
            ? (rawRoll as AttackerDiceAtStat)
            : undefined;
          const flatRolls: DiceRoll[] = !isAttacker
            ? ((rawRoll as DiceRoll[]) ?? [])
            : isAttackerLadderDice(atkDice)
              ? []
              : atkDice?.mode === 'flat'
                ? atkDice.rolls
                : [];
          const rowUnits = unitsByValue[value] || [];
          if (isAttacker && isAttackerSiegeworkRamFlexDice(atkDice)) {
            return (
          <UnitRow
            key={value}
            statValue={value}
            units={rowUnits}
            eliminatedGroups={eliminatedByValue[value] ?? []}
            rolls={[]}
            countCasualties={countCasualties}
            fullCasualtyIds={fullCasualtyIds}
            badgeHitsByUnitType={badgeHitsPerShelf ? (badgeHitsPerShelf[value] ?? {}) : badgeHitsByUnitType}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={isAttacker}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            hitColor={rowUnits[0]?.factionColor ?? factionColor}
            specials={specialsForBadges}
            showRamSiegeworkForAttacker={isAttacker ? showRamSiegeworkForAttacker : false}
            ramAttackerUnitTypeSet={isAttacker ? ramAttackerUnitTypeSet : undefined}
            siegeworkRamFlexDice={atkDice}
          />
            );
          }
          let segHits: number[] | undefined;
          if (isAttacker && isAttackerLadderDice(atkDice)) {
            segHits = atkDice.segments.map(seg =>
              rowUnits
                .filter(u => u.unitType === seg.unitType && ladderSetCombat.has(u.id) === seg.onLadder)
                .reduce((s, u) => {
                  if (casThis.has(u.id)) return s + u.health;
                  if (woundThis.has(u.id)) return s + 1;
                  return s;
                }, 0)
            );
          }
          return (
          <UnitRow
            key={value}
            statValue={value}
            units={rowUnits}
            eliminatedGroups={eliminatedByValue[value] ?? []}
            rolls={flatRolls}
            countCasualties={countCasualties}
            fullCasualtyIds={fullCasualtyIds}
            badgeHitsByUnitType={badgeHitsPerShelf ? (badgeHitsPerShelf[value] ?? {}) : badgeHitsByUnitType}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={isAttacker}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            hitColor={rowUnits[0]?.factionColor ?? factionColor}
            rerolledIndices={!isAttacker ? (defenderRerolledIndicesByStat?.[String(value)] ?? []) : undefined}
            rerolledDice={!isAttacker ? (defenderRerolledDiceByStat?.[String(value)] ?? []) : undefined}
            showRerollX={!isAttacker ? showRerollX : undefined}
            specials={specialsForBadges}
            showRamSiegeworkForAttacker={isAttacker ? showRamSiegeworkForAttacker : false}
            ramAttackerUnitTypeSet={isAttacker ? ramAttackerUnitTypeSet : undefined}
            attackerLadderDice={atkDice}
            ladderInstanceIds={isAttacker ? ladderSetCombat : undefined}
            ladderSegmentHits={segHits}
          />
          );
        })}
        {showBottomRow && (
          <UnitRow
            key="siegework-noroll"
            statValue={0}
            hideStatLabel
            bottomRowStatLabel="—"
            units={bottomSiegeworkUnits}
            eliminatedGroups={bottomEliminated}
            rolls={[]}
            countCasualties={countCasualties}
            fullCasualtyIds={fullCasualtyIds}
            badgeHitsByUnitType={badgeHitsByUnitType}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={isAttacker}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            hitColor={bottomSiegeworkUnits[0]?.factionColor ?? factionColor}
            specials={specialsForBadges}
            showRamSiegeworkForAttacker={false}
            siegeworkBottomBlankDice={isAttacker && siegeworkShelfMode === 'ladder_bottom'}
          />
        )}
      </div>

      <div className="combat-side-meta">
        <div className={`hits-display ${showHits ? 'visible' : ''}`}>
          <span className="hits-count">{hits} Hit{hits !== 1 ? 's' : ''}</span>
        </div>
        {casualtyPriorityLabel && (
          <div className="combat-side-casualty-order" title={casualtyPriorityLabel}>
            {casualtyPriorityLabel}
          </div>
        )}
        {showCumulativeHits &&
          typeof cumulativeHitsReceived === 'number' &&
          cumulativeHitsReceived >= 0 && (
          <div className="combat-side-cumulative-hits" title="Cumulative hits taken this battle (includes damage to stronghold)">
            <span className="combat-side-cumulative-hits-text">Hits taken: {cumulativeHitsReceived}</span>
            {cumulativeHitsReceived > 0 && (
              <div className="combat-side-cumulative-hits-markers" aria-hidden>
                {Array.from({ length: Math.min(cumulativeHitsReceived, 12) }, (_, i) => (
                  <span key={i} className="combat-side-cumulative-hit-x">
                    ×
                  </span>
                ))}
                {cumulativeHitsReceived > 12 && (
                  <span className="combat-side-cumulative-hits-more">+{cumulativeHitsReceived - 12}</span>
                )}
              </div>
            )}
          </div>
        )}
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
  seaRaidCombat = false,
  siegeworksPending = false,
  archerPrefirePending = false,
  siegeworksAttackerInstanceIds,
  siegeworksDefenderInstanceIds,
  onStartRound,
  onRetreat,
  onClose,
  onCancel,
  onHighlightTerritories,
  specials,
  casualtyPriorityAttacker = 'best_unit',
  casualtyPriorityDefender = 'best_unit',
  mustConquer: mustConquerProp = false,
  readOnly = false,
  combatLog,
  combatEndResult,
  cumulativeHitsReceivedByAttacker = 0,
  cumulativeHitsReceivedByDefender = 0,
  defenderStrongholdHp,
  ladderInfantryInstanceIds = [],
  ladderEquipmentCount: _ladderEquipmentCount = 0,
  attackerRamUnitTypes = [],
  combatUnitDefs = {},
  attackerHasFuseBombOption = false,
}: CombatDisplayProps) {
  const [casualtyPriorityPill, setCasualtyPriorityPill] = useState<'best_unit' | 'best_attack'>(casualtyPriorityAttacker as 'best_unit' | 'best_attack');
  const [mustConquerPill, setMustConquerPill] = useState(mustConquerProp);
  const [fuseBombPill, setFuseBombPill] = useState(true);
  useEffect(() => {
    setCasualtyPriorityPill((casualtyPriorityAttacker === 'best_attack' ? 'best_attack' : 'best_unit'));
    setMustConquerPill(mustConquerProp);
  }, [casualtyPriorityAttacker, mustConquerProp]);
  useEffect(() => {
    if (isOpen && attackerHasFuseBombOption) {
      setFuseBombPill(true);
    }
  }, [isOpen, attackerHasFuseBombOption]);
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

  const ramAttackerTypeSet = useMemo(() => new Set(attackerRamUnitTypes), [attackerRamUnitTypes]);
  /** Walls still up: treat missing current as full (start of round) so ram stays on numbered shelves with R badge. */
  const strongholdWallsUp =
    !!defenderStrongholdHp?.base &&
    defenderStrongholdHp.base > 0 &&
    (defenderStrongholdHp.current ?? defenderStrongholdHp.base) > 0;
  const showRamSiegeworkAttacker =
    strongholdWallsUp &&
    (currentRound?.isSiegeworksRound || (roundNumber === 0 && siegeworksPending));

  const attackerSiegeworkShelfMode = useMemo((): 'none' | 'sw_bottom' | 'ladder_bottom' => {
    const r = currentRound;
    if (!r || r.isArcherPrefire || r.isStealthPrefire) return 'none';
    if (r.isSiegeworksRound) return 'ladder_bottom';
    return 'sw_bottom';
  }, [currentRound]);

  const defenderSiegeworkShelfMode = useMemo((): 'none' | 'sw_bottom' | 'ladder_bottom' => {
    const r = currentRound;
    if (!r || r.isArcherPrefire || r.isStealthPrefire) return 'none';
    // Match attacker: ladder equipment (0 def, does not roll) belongs on the bottom siegework row, not a "0" stat shelf.
    if (r.isSiegeworksRound) return 'ladder_bottom';
    return 'sw_bottom';
  }, [currentRound]);

  // Track casualties from PREVIOUS rounds (these units don't show)
  const [previousAttackerCasualties, setPreviousAttackerCasualties] = useState<string[]>([]);
  const [previousDefenderCasualties, setPreviousDefenderCasualties] = useState<string[]>([]);

  // Cumulative eliminated stacks (fully dead in a prior round); shown with red X and no rolls in later rounds
  const [previousEliminatedAttackerStacks, setPreviousEliminatedAttackerStacks] = useState<EliminatedStack[]>([]);
  const [previousEliminatedDefenderStacks, setPreviousEliminatedDefenderStacks] = useState<EliminatedStack[]>([]);

  // Track if combat is actually over (computed after round)
  const [isCombatOver, setIsCombatOver] = useState(false);
  const [attackerWon, setAttackerWon] = useState(false);
  const [defenderWon, setDefenderWon] = useState(false);

  /** Max cumulative hits seen from props this session (backend clears active_combat after battle; keeps UI from snapping to 0). */
  const [peakCumulativeAtt, setPeakCumulativeAtt] = useState(0);
  const [peakCumulativeDef, setPeakCumulativeDef] = useState(0);

  /** Full roster at first open (instance ids never shrink like live territory props do when units die). */
  const [battleStartAttackerSnapshot, setBattleStartAttackerSnapshot] = useState<CombatUnit[] | null>(null);
  const [battleStartDefenderSnapshot, setBattleStartDefenderSnapshot] = useState<CombatUnit[] | null>(null);
  const battleSnapshotOpenRef = useRef(false);

  const displayCumulativeAtt = Math.max(cumulativeHitsReceivedByAttacker ?? 0, peakCumulativeAtt);
  const displayCumulativeDef = Math.max(cumulativeHitsReceivedByDefender ?? 0, peakCumulativeDef);

  // Reset combat UI state when modal opens (must run before peak accumulation effect on the same open).
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
      setPreviousEliminatedAttackerStacks([]);
      setPreviousEliminatedDefenderStacks([]);
      setIsCombatOver(false);
      setAttackerWon(false);
      setDefenderWon(false);
      setPeakCumulativeAtt(0);
      setPeakCumulativeDef(0);
    }
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) {
      battleSnapshotOpenRef.current = false;
      setBattleStartAttackerSnapshot(null);
      setBattleStartDefenderSnapshot(null);
      return;
    }
    const snapEmpty = (battleStartAttackerSnapshot?.length ?? 0) === 0 && (battleStartDefenderSnapshot?.length ?? 0) === 0;
    if (!battleSnapshotOpenRef.current || (snapEmpty && (attacker.units.length > 0 || defender.units.length > 0))) {
      setBattleStartAttackerSnapshot([...attacker.units]);
      setBattleStartDefenderSnapshot([...defender.units]);
      battleSnapshotOpenRef.current = true;
    }
  }, [isOpen, attacker.units, defender.units, battleStartAttackerSnapshot?.length, battleStartDefenderSnapshot?.length]);

  useEffect(() => {
    if (!isOpen) {
      stopSiegeworksRoundCommenceSound();
    }
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const a = cumulativeHitsReceivedByAttacker ?? 0;
    const d = cumulativeHitsReceivedByDefender ?? 0;
    setPeakCumulativeAtt(x => Math.max(x, a));
    setPeakCumulativeDef(x => Math.max(x, d));
  }, [isOpen, cumulativeHitsReceivedByAttacker, cumulativeHitsReceivedByDefender]);

  // Clear territory highlights when closing retreat selection
  useEffect(() => {
    if (combatPhase !== 'selecting_retreat' && onHighlightTerritories) {
      onHighlightTerritories([]);
    }
  }, [combatPhase, onHighlightTerritories]);

  // Spectator: sync round state from backend combat_log when it updates (e.g. from polling)
  useEffect(() => {
    if (!readOnly || !combatLog?.length || !attacker.units.length || !defender.units.length) return;
    const last = combatLog[combatLog.length - 1] as {
      round_number?: number;
      attacker_rolls?: number[];
      defender_rolls?: number[];
      attacker_hits?: number;
      defender_hits?: number;
      attacker_casualties?: string[];
      defender_casualties?: string[];
      is_archer_prefire?: boolean;
      is_stealth_prefire?: boolean;
    };
    const rn = last.round_number ?? 0;
    const aRolls = last.attacker_rolls ?? [];
    const dRolls = last.defender_rolls ?? [];
    const aHits = last.attacker_hits ?? 0;
    const dHits = last.defender_hits ?? 0;
    const toAttackerFlat = (
      rolls: number[],
      hitCount: number
    ): Record<number, AttackerDiceAtStat> => {
      const arr = rolls.map((value, i) => ({ value, target: 10, isHit: i < hitCount }));
      return arr.length ? { 1: { mode: 'flat', rolls: arr } } : {};
    };
    const toDefenderSpectator = (rolls: number[], hitCount: number): Record<number, DiceRoll[]> => {
      const arr = rolls.map((value, i) => ({ value, target: 10, isHit: i < hitCount }));
      return arr.length ? { 1: arr } : {};
    };
    let prevA: string[] = [];
    let prevD: string[] = [];
    for (let i = 0; i < combatLog.length - 1; i++) {
      const e = combatLog[i] as { attacker_casualties?: string[]; defender_casualties?: string[] };
      prevA = prevA.concat(e.attacker_casualties ?? []);
      prevD = prevD.concat(e.defender_casualties ?? []);
    }
    const aAtStart = attacker.units.filter(u => !prevA.includes(u.id));
    const dAtStart = defender.units.filter(u => !prevD.includes(u.id));
    const round: CombatRound = {
      roundNumber: rn,
      attackerRolls: toAttackerFlat(aRolls, aHits),
      defenderRolls: toDefenderSpectator(dRolls, dHits),
      attackerHits: aHits,
      defenderHits: dHits,
      attackerCasualties: last.attacker_casualties ?? [],
      defenderCasualties: last.defender_casualties ?? [],
      isArcherPrefire: last.is_archer_prefire ?? false,
      isStealthPrefire: last.is_stealth_prefire ?? false,
      terrorApplied: false,
      attackerUnitsAtStart: aAtStart,
      defenderUnitsAtStart: dAtStart,
    };
    setRoundNumber(rn);
    setPreviousAttackerCasualties(prevA);
    setPreviousDefenderCasualties(prevD);
    setCurrentRound(round);
    setShowHits(true);
    setShowCasualtyBadges(true);
    setCombatPhase('awaiting_decision'); // Show round + hits; result banner only when combatEndResult is set
  }, [readOnly, combatLog, attacker.units, defender.units]);

  // Spectator: when combat ended, show result banner then auto-close after 3s
  useEffect(() => {
    if (!readOnly || !combatEndResult) return;
    setAttackerWon(combatEndResult.attackerWon);
    setDefenderWon(combatEndResult.defenderWon);
    setCombatPhase('complete');
    const t = setTimeout(() => {
      onClose(
        { attackerWon: combatEndResult.attackerWon, defenderWon: combatEndResult.defenderWon },
        []
      );
    }, 3000);
    return () => clearTimeout(t);
  }, [readOnly, combatEndResult, onClose]);

  // Cumulative casualties (full truth for close / badges / backend sync) — needed before shelf merge for standard rounds
  const allAttackerCasualties = [...previousAttackerCasualties, ...(currentRound?.attackerCasualties || [])];
  const allDefenderCasualties = [...previousDefenderCasualties, ...(currentRound?.defenderCasualties || [])];

  /** Prefire / dedicated siegework round: shelves use backend subset only. Standard rounds: full battle roster with red X on dead stacks. */
  const isRestrictedShelfRound = useMemo(
    () =>
      currentRound?.isArcherPrefire === true ||
      currentRound?.isStealthPrefire === true ||
      currentRound?.isSiegeworksRound === true ||
      (roundNumber === 0 && siegeworksPending && !currentRound) ||
      (roundNumber === 0 && archerPrefirePending && !currentRound),
    [currentRound, roundNumber, siegeworksPending, archerPrefirePending],
  );

  const casualtyAttSet = useMemo(() => new Set(allAttackerCasualties), [allAttackerCasualties]);
  const casualtyDefSet = useMemo(() => new Set(allDefenderCasualties), [allDefenderCasualties]);

  const mergedAttackerForStandardShelving = useMemo(
    () => mergeBattleSnapshotWithLiveAndCasualties(battleStartAttackerSnapshot, attacker.units, casualtyAttSet),
    [battleStartAttackerSnapshot, attacker.units, casualtyAttSet],
  );
  const mergedDefenderForStandardShelving = useMemo(
    () => mergeBattleSnapshotWithLiveAndCasualties(battleStartDefenderSnapshot, defender.units, casualtyDefSet),
    [battleStartDefenderSnapshot, defender.units, casualtyDefSet],
  );

  const attackerUnitsThisRound = useMemo(() => {
    if (isRestrictedShelfRound) {
      return currentRound ? currentRound.attackerUnitsAtStart : attacker.units;
    }
    if (currentRound) {
      return augmentRoundStartWithPriorEliminated(
        currentRound.attackerUnitsAtStart,
        battleStartAttackerSnapshot,
        previousAttackerCasualties,
      );
    }
    return mergedAttackerForStandardShelving ?? attacker.units;
  }, [
    isRestrictedShelfRound,
    currentRound,
    battleStartAttackerSnapshot,
    previousAttackerCasualties,
    mergedAttackerForStandardShelving,
    attacker.units,
  ]);

  const defenderUnitsThisRound = useMemo(() => {
    if (isRestrictedShelfRound) {
      return currentRound ? currentRound.defenderUnitsAtStart : defender.units;
    }
    if (currentRound) {
      return augmentRoundStartWithPriorEliminated(
        currentRound.defenderUnitsAtStart,
        battleStartDefenderSnapshot,
        previousDefenderCasualties,
      );
    }
    return mergedDefenderForStandardShelving ?? defender.units;
  }, [
    isRestrictedShelfRound,
    currentRound,
    battleStartDefenderSnapshot,
    previousDefenderCasualties,
    mergedDefenderForStandardShelving,
    defender.units,
  ]);

  // Only show units that participate this round: archer prefire = defender archers only; siegeworks = siegework units (+ paired bombikazi) only
  const attackerUnitsToShow = useMemo(() => {
    if (currentRound?.isArcherPrefire) return []; // Defender archer prefire: only defenders roll
    const sw =
      currentRound?.isSiegeworksRound || (roundNumber === 0 && siegeworksPending);
    if (sw && Array.isArray(siegeworksAttackerInstanceIds)) {
      const idSet = new Set(siegeworksAttackerInstanceIds);
      return attackerUnitsThisRound.filter(u => idSet.has(u.id));
    }
    return attackerUnitsThisRound;
  }, [
    currentRound?.isArcherPrefire,
    currentRound?.isSiegeworksRound,
    roundNumber,
    siegeworksPending,
    siegeworksAttackerInstanceIds,
    attackerUnitsThisRound,
  ]);
  const defenderUnitsToShow = useMemo(() => {
    if (currentRound?.isArcherPrefire)
      return defenderUnitsThisRound.filter(u => u.isArcher === true);
    const sw =
      currentRound?.isSiegeworksRound || (roundNumber === 0 && siegeworksPending);
    if (sw && Array.isArray(siegeworksDefenderInstanceIds)) {
      const idSet = new Set(siegeworksDefenderInstanceIds);
      return defenderUnitsThisRound.filter(u => idSet.has(u.id));
    }
    return defenderUnitsThisRound;
  }, [
    currentRound?.isArcherPrefire,
    currentRound?.isSiegeworksRound,
    roundNumber,
    siegeworksPending,
    siegeworksDefenderInstanceIds,
    defenderUnitsThisRound,
  ]);

  const ladderInstanceIdSet = useMemo(() => {
    const fromRound = currentRound?.ladderInfantryInstanceIds;
    if (fromRound !== undefined) return new Set(fromRound);
    return new Set(ladderInfantryInstanceIds ?? []);
  }, [currentRound?.ladderInfantryInstanceIds, ladderInfantryInstanceIds]);
  /** Stack counts in shelves: omit current-round casualties until dice reveal completes (showCasualtyBadges). Spectators always see resolved counts. */
  const stackDisplayAttackerCasualties =
    readOnly || showCasualtyBadges ? allAttackerCasualties : previousAttackerCasualties;
  const stackDisplayDefenderCasualties =
    readOnly || showCasualtyBadges ? allDefenderCasualties : previousDefenderCasualties;
  const attackerUnitsAlive = attacker.units.filter(u => !allAttackerCasualties.includes(u.id));

  /** Banner tint always uses the attacking faction’s color (Victory / Defeat / Mutual destruction). */
  const resultBannerBackgroundColor = useMemo(() => {
    const c = attacker.factionColor;
    if (!c) return undefined;
    return `${c}60`;
  }, [attacker.factionColor]);

  // Defender instance -> effective defense (for grouping casualties by shelf so hit badges match backend)
  const defenderEffectiveDefenseByInstance = useMemo(() => {
    const map: Record<string, number> = {};
    defenderUnitsThisRound.forEach(u => {
      map[u.id] = u.effectiveDefense ?? u.defense;
    });
    return map;
  }, [defenderUnitsThisRound]);

  // Hit badge: at round start = prior damage on living units (HP>1 only); after round = cumulative damage
  // (max HP − current remaining from live game state, includes pre-rounds and all prior rounds).
  // Defender hits are per-shelf (by effective defense).
  const { badgeHitsAttacker, badgeHitsDefender, badgeHitsDefenderPerShelf, onlyShowBadgeForHpGreaterThanOne } = useMemo(() => {
    const onlyHpGreaterThanOne = !showCasualtyBadges; // round start = true, after round = false
    const attackerHits: Record<string, number> = {};
    const defenderHits: Record<string, number> = {};
    const defenderHitsPerShelf: Record<number, Record<string, number>> = {};

    if (showCasualtyBadges && currentRound) {
      const liveAttMap = new Map(attacker.units.map(u => [u.id, u]));
      const liveDefMap = new Map(defender.units.map(u => [u.id, u]));
      const casualtyAtt = new Set(allAttackerCasualties);
      const casualtyDef = new Set(allDefenderCasualties);

      attackerUnitsThisRound.forEach(u => {
        if (casualtyAtt.has(u.id)) return;
        const live = liveAttMap.get(u.id);
        const rh = live?.remainingHealth ?? u.remainingHealth;
        const dmg = Math.max(0, u.health - rh);
        if (dmg > 0) {
          attackerHits[u.unitType] = (attackerHits[u.unitType] ?? 0) + dmg;
        }
      });
      defenderUnitsThisRound.forEach(u => {
        if (casualtyDef.has(u.id)) return;
        const live = liveDefMap.get(u.id);
        const rh = live?.remainingHealth ?? u.remainingHealth;
        const dmg = Math.max(0, u.health - rh);
        if (dmg > 0) {
          defenderHits[u.unitType] = (defenderHits[u.unitType] ?? 0) + dmg;
          const effDef = defenderEffectiveDefenseByInstance[u.id] ?? u.effectiveDefense ?? u.defense;
          if (!defenderHitsPerShelf[effDef]) defenderHitsPerShelf[effDef] = {};
          defenderHitsPerShelf[effDef][u.unitType] = (defenderHitsPerShelf[effDef][u.unitType] ?? 0) + dmg;
        }
      });
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
  }, [
    showCasualtyBadges,
    currentRound,
    defenderEffectiveDefenseByInstance,
    attackerUnitsThisRound,
    defenderUnitsThisRound,
    attacker.units,
    defender.units,
    allAttackerCasualties,
    allDefenderCasualties,
  ]);

  // Calculate row animation order (include -1 for archer prefire)
  const getRowOrder = useCallback((round: CombatRound) => {
    const order: string[] = [];

    for (let i = 1; i <= 10; i++) {
      if (attackerStatRowHasRolls(round.attackerRolls[i])) {
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
    combatOverResult?: { combatOver: boolean; attackerWon: boolean; defenderWon: boolean },
    terrorReroll?: {
      applied: boolean;
      finalRound?: CombatRound;
      defenderRerolledIndicesByStat?: Record<string, number[]>;
    },
    onTerrorRerollShown?: () => void,
    /** Merged into previousEliminated* when casualties become visible (same time as setShowCasualtyBadges). */
    eliminatedStacksThisRound?: { attacker: EliminatedStack[]; defender: EliminatedStack[] }
  ) => {
    const elimA = eliminatedStacksThisRound?.attacker ?? [];
    const elimD = eliminatedStacksThisRound?.defender ?? [];
    let eliminatedStacksAppended = false;
    const appendEliminatedStacksForRound = () => {
      if (eliminatedStacksAppended) return;
      eliminatedStacksAppended = true;
      if (elimA.length > 0) {
        setPreviousEliminatedAttackerStacks(prev => [...prev, ...elimA]);
      }
      if (elimD.length > 0) {
        setPreviousEliminatedDefenderStacks(prev => [...prev, ...elimD]);
      }
    };
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
            // Prefire/siegeworks rounds: don't decide combat end from the locally displayed shelf units.
            // The backend can keep combat active even when the displayed unit subsets are empty.
            const isPrefireRound =
              round.isArcherPrefire === true
              || round.isStealthPrefire === true
              || round.isSiegeworksRound === true;
            const combatEnded = combatOverResult
              ? combatOverResult.combatOver
              : isPrefireRound
                ? false
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
                setDefenderWon(combatOverResult.defenderWon);
              } else {
                const totalAttackerCasualties = [...currentPrevAttackerCasualties, ...round.attackerCasualties];
                const totalDefenderCasualties = [...currentPrevDefenderCasualties, ...round.defenderCasualties];
                const attackersAlive = unitListA.filter(u => !totalAttackerCasualties.includes(u.id));
                const defendersAlive = unitListD.filter(u => !totalDefenderCasualties.includes(u.id));
                setAttackerWon(defendersAlive.length === 0 && attackersAlive.length > 0);
                setDefenderWon(attackersAlive.length === 0 && defendersAlive.length > 0);
              }
              setTimeout(() => {
                appendEliminatedStacksForRound();
                setShowCasualtyBadges(true);
                setShowHits(true);
                setCombatPhase('showing_result');
                setTimeout(() => setCombatPhase('complete'), 600);
              }, 400);
            } else {
              setCombatPhase('awaiting_decision');
            }
          }, delayMs);
        };

        if (terrorReroll?.applied && terrorReroll.finalRound) {
          const idxMap = terrorReroll.defenderRerolledIndicesByStat;
          const statsWithRerolls =
            idxMap != null
              ? Object.entries(idxMap)
                .filter(([, idx]) => (idx?.length ?? 0) > 0)
                .map(([k]) => Number(k))
                .filter(n => Number.isFinite(n))
              : [];
          const rerollRowKeys: string[] = [];
          if (statsWithRerolls.includes(-1)) rerollRowKeys.push('defender_-1');
          for (let s = 1; s <= 10; s++) {
            if (statsWithRerolls.includes(s)) rerollRowKeys.push(`defender_${s}`);
          }
          const rerollStaggerMs =
            rerollRowKeys.length > 0 ? (rerollRowKeys.length - 1) * DELAY_BETWEEN_ROWS : 0;
          // Badges / eliminated stacks must not appear until all terror re-roll rows have revealed (higher stat shelves last).
          const terrorCasualtyBadgeDelay =
            rerollRowKeys.length > 0
              ? TERROR_REROLL_PAUSE_MS + rerollStaggerMs + LANDING_DURATION + 200
              : TERROR_REROLL_PAUSE_MS + 600;

          setTimeout(() => {
            setTerrorRerollPhase('rerolled');
            setTerrorFinalRound(terrorReroll.finalRound!);
            onTerrorRerollShown?.();
          }, TERROR_REROLL_PAUSE_MS);
          setTimeout(() => setShowHits(true), TERROR_REROLL_PAUSE_MS + 400);
          setTimeout(() => {
            appendEliminatedStacksForRound();
            setShowCasualtyBadges(true);
          }, terrorCasualtyBadgeDelay);
          rerollRowKeys.forEach((rowKey, i) => {
            setTimeout(() => {
              playCombatDiceShelfRevealSound();
              setCurrentRowKey(rowKey);
              setIsLanding(true);
              setTimeout(() => setIsLanding(false), LANDING_DURATION);
            }, TERROR_REROLL_PAUSE_MS + i * DELAY_BETWEEN_ROWS);
          });
          if (rerollRowKeys.length > 0) {
            const clearAt =
              TERROR_REROLL_PAUSE_MS + (rerollRowKeys.length - 1) * DELAY_BETWEEN_ROWS + LANDING_DURATION;
            setTimeout(() => setCurrentRowKey(null), clearAt);
          }
          scheduleResultOrDecision(750 + TERROR_REROLL_PAUSE_MS + rerollStaggerMs);
        } else {
          setTimeout(() => setShowHits(true), 280);
          setTimeout(() => {
            appendEliminatedStacksForRound();
            setShowCasualtyBadges(true);
          }, 480);
          scheduleResultOrDecision(750);
        }
        return;
      }

      const rowKey = rowOrder[index];
      revealed.add(rowKey);
      setRevealedRows(new Set(revealed));
      setCurrentRowKey(rowKey);
      setIsLanding(true);
      playCombatDiceShelfRevealSound();

      setTimeout(() => setIsLanding(false), LANDING_DURATION);

      index++;
      setTimeout(revealNextRow, DELAY_BETWEEN_ROWS);
    };

    revealNextRow();
  }, [getRowOrder, attackerUnitsThisRound, defenderUnitsThisRound]);

  // Compute fully eliminated stacks from a round (whole group died) for cumulative display
  const computeEliminatedStacksFromRound = useCallback((round: CombatRound): { attacker: EliminatedStack[]; defender: EliminatedStack[] } => {
    const casualtySetA = new Set(round.attackerCasualties);
    const casualtySetD = new Set(round.defenderCasualties);
    const groupByKey = (units: CombatUnit[], getStat: (u: CombatUnit) => number) => {
      const map = new Map<string, CombatUnit[]>();
      units.forEach(u => {
        const stat = getStat(u);
        const key = `${stat}_${u.unitType}_${u.factionId ?? u.factionColor ?? ''}`;
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push(u);
      });
      return map;
    };
    const getAttackerStat = (u: CombatUnit) => u.effectiveAttack ?? u.attack;
    const getDefenderStat = (u: CombatUnit) => u.effectiveDefense ?? u.defense;
    const toEliminated = (units: CombatUnit[], casualtySet: Set<string>, getStat: (u: CombatUnit) => number): EliminatedStack[] => {
      const groups = groupByKey(units, getStat);
      const out: EliminatedStack[] = [];
      groups.forEach((group) => {
        const dead = group.filter(u => casualtySet.has(u.id));
        if (dead.length === group.length && dead.length > 0) {
          const u = group[0];
          out.push({
            unitType: u.unitType,
            unitKey: `${u.unitType}::${u.factionId ?? u.factionColor ?? ''}::${u.health}`,
            name: u.name,
            icon: u.icon,
            health: u.health,
            statValue: getStat(u),
            count: group.length,
            factionColor: u.factionColor,
            factionId: u.factionId,
            hasTerror: u.hasTerror,
            terrainMountain: u.terrainMountain,
            terrainForest: u.terrainForest,
            hasCaptainBonus: u.hasCaptainBonus,
            hasAntiCavalry: u.hasAntiCavalry,
            hasSeaRaider: u.hasSeaRaider,
            hasArcher: u.hasArcher,
            hasStealth: u.hasStealth,
            hasBombikazi: u.hasBombikazi,
            hasFearless: u.hasFearless,
            hasHope: u.hasHope,
            hasRam: u.hasRam,
          });
        }
      });
      return out;
    };
    return {
      attacker: toEliminated(round.attackerUnitsAtStart, casualtySetA, getAttackerStat),
      defender: toEliminated(round.defenderUnitsAtStart, casualtySetD, getDefenderStat),
    };
  }, []);

  // Start battle (round 1) or continue to next round - call backend then animate
  const handleRoll = useCallback(async (
    overridePrevAttacker?: string[],
    overridePrevDefender?: string[]
  ) => {
    const casualtyOrderForRequest = casualtyPriorityPill;
    const mustConquerForRequest = mustConquerPill;
    const fuseBombForRequest = fuseBombPill;
    setCombatPhase('rolling');
    setShowHits(false);
    setShowCasualtyBadges(false);
    setRevealedRows(new Set());
    setCurrentRowKey(null);
    setTerrorFinalRound(null);

    const result = await onStartRound(casualtyOrderForRequest, mustConquerForRequest, fuseBombForRequest);
    if (!result) {
      setCombatPhase(roundNumber === 0 ? 'ready' : 'awaiting_decision');
      return;
    }

    const newRoundNumber = result.round.roundNumber;
    setRoundNumber(newRoundNumber);

    // Fully eliminated stacks: appended when dice reveal finishes (see animateDiceReveals appendEliminatedStacksForRound)
    const { attacker: newElimA, defender: newElimD } = computeEliminatedStacksFromRound(result.round);

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
        terrorRerollCount:
          result.round.terrorRerollCount ?? result.terrorReroll?.terror_reroll_count,
      });
      // Show red X on re-rolled dice only after 600ms so user sees the dice first (schedule here so effect cleanup can't cancel it)
      setTimeout(() => setShowTerrorRerollX(true), 600);
    } else {
      setTerrorRerollPhase(null);
      setTerrorRerolledIndicesByStat({});
      setTerrorFinalRound(null);
      setCurrentRound(result.round);
    }

    if (result.round.isArcherPrefire) {
      playArcherPrefireCommenceSound();
    }
    if (result.round.isSiegeworksRound) {
      playSiegeworksRoundCommenceSound();
    }

    animateDiceReveals(
      result.round,
      prevA,
      prevD,
      result.combatOver
        ? { combatOver: true, attackerWon: result.attackerWon, defenderWon: result.defenderWon }
        : undefined,
      hasInitialGrouped
        ? {
            applied: true,
            finalRound: result.round,
            defenderRerolledIndicesByStat: result.terrorReroll?.defender_rerolled_indices_by_stat ?? {},
          }
        : terrorApplied
          ? { applied: true }
          : undefined,
      undefined,
      { attacker: newElimA, defender: newElimD }
    );
  }, [onStartRound, casualtyPriorityPill, mustConquerPill, fuseBombPill, animateDiceReveals, previousAttackerCasualties, previousDefenderCasualties, roundNumber, groupedToDefenderRolls, computeEliminatedStacksFromRound]);

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
    onClose({ attackerWon, defenderWon }, attackerUnitsAlive);
  }, [onClose, attackerWon, defenderWon, attackerUnitsAlive]);

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
  /** Casualty / must-conquer apply to the next API round; lock during roll, reveal, and non-decision phases so they cannot change after dice are in play for this round. */
  const attackerCombatOptionsLocked =
    !readOnly &&
    (combatPhase === 'rolling' ||
      combatPhase === 'showing_result' ||
      combatPhase === 'confirming_retreat' ||
      combatPhase === 'selecting_retreat' ||
      combatPhase === 'complete' ||
      (currentRound != null && !showCasualtyBadges));

  // Hit counts must match the displayed dice. Derive from rolls so the bottom number never disagrees with the dice.
  const effectiveAttackerHits =
    currentRound?.attackerHits != null
      ? currentRound.attackerHits
      : currentRound?.attackerRolls
        ? sumAttackerDiceHits(currentRound.attackerRolls)
        : 0;
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
            {(currentRound?.isArcherPrefire || (roundNumber === 0 && archerPrefirePending)) && (
              <span className="round-indicator">Archers</span>
            )}
            {currentRound?.isStealthPrefire && (
              <span className="round-indicator">Stealth</span>
            )}
            {(currentRound?.isSiegeworksRound || (roundNumber === 0 && siegeworksPending)) && (
              <span className="round-indicator">Siegeworks</span>
            )}
            {roundNumber > 0 && !currentRound?.isArcherPrefire && !currentRound?.isStealthPrefire && !currentRound?.isSiegeworksRound && (
              <span className="round-indicator">Round {roundNumber}</span>
            )}
          </div>
          {readOnly && (
            <button
              type="button"
              className="combat-modal-close-x"
              onClick={handleClose}
              title="Close"
              aria-label="Close"
            >
              ×
            </button>
          )}
        </header>

        <div className="combat-arena">
          <div className="combat-arena-sides">
          <CombatSide
            title={attacker.factionName}
            factionIcon={attacker.factionIcon}
            factionColor={attacker.factionColor}
            units={attackerUnitsToShow}
            rolls={currentRound?.attackerRolls || {}}
            hits={effectiveAttackerHits}
            countCasualties={stackDisplayAttackerCasualties}
            fullCasualtyIds={stackDisplayAttackerCasualties}
            badgeHitsByUnitType={badgeHitsAttacker}
            onlyShowBadgeForHpGreaterThanOne={onlyShowBadgeForHpGreaterThanOne}
            showCasualtyBadges={showCasualtyBadges}
            isAttacker={true}
            isArcherPrefire={currentRound?.isArcherPrefire}
            revealedRows={revealedRows}
            currentRowKey={currentRowKey}
            isLanding={isLanding}
            showHits={showHits}
            specials={specials}
            casualtyPriorityLabel={casualtyPriorityPill === 'best_attack' ? 'Casualty Priority: Best Attack' : 'Casualty Priority: Best Unit'}
            eliminatedStacks={previousEliminatedAttackerStacks}
            cumulativeHitsReceived={displayCumulativeAtt}
            showCumulativeHits={showHits || isCombatOver}
            showRamSiegeworkForAttacker={showRamSiegeworkAttacker}
            ramAttackerUnitTypeSet={ramAttackerTypeSet}
            siegeworkShelfMode={attackerSiegeworkShelfMode}
            combatUnitDefs={combatUnitDefs}
            siegeworkRoundActive={
              currentRound?.isSiegeworksRound === true || (roundNumber === 0 && siegeworksPending)
            }
            defenderStrongholdHp={defenderStrongholdHp}
            ladderInstanceIds={ladderInstanceIdSet}
            attackerRoundCasualties={currentRound?.attackerCasualties ?? []}
            attackerRoundWounded={currentRound?.attackerWounded ?? []}
          />

          <div className="vs-divider">
            {currentRound?.terrorApplied && (
              <span className="vs-divider-terror">
                {typeof currentRound.terrorRerollCount === 'number' && currentRound.terrorRerollCount > 0
                  ? `${currentRound.terrorRerollCount} Terror`
                  : 'Terror'}
              </span>
            )}
            <span>VS</span>
            {showRollingIndicator && (
              <div className="rolling-indicator">Fighting...</div>
            )}
          </div>

          <div className="combat-defender-column">
            <CombatSide
              title={defender.factionName}
              factionIcon={defender.factionIcon}
              factionColor={defender.factionColor}
              units={defenderUnitsToShow}
              rolls={currentRound?.defenderRolls || {}}
              hits={effectiveDefenderHits}
              countCasualties={stackDisplayDefenderCasualties}
              fullCasualtyIds={stackDisplayDefenderCasualties}
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
              specials={specials}
              casualtyPriorityLabel={casualtyPriorityDefender === 'best_defense' ? 'Casualty Priority: Best Defense' : 'Casualty Priority: Best Unit'}
              eliminatedStacks={previousEliminatedDefenderStacks}
              cumulativeHitsReceived={displayCumulativeDef}
              showCumulativeHits={showHits || isCombatOver}
              siegeworkShelfMode={defenderSiegeworkShelfMode}
              combatUnitDefs={combatUnitDefs}
              defenderStrongholdHp={defenderStrongholdHp}
            />
          </div>
          </div>

          {/* Overlay: does not affect layout; shelves stay same size */}
          {showResultBanner && isCombatOver && (
            <div className="combat-result-overlay" aria-live="polite">
              <div
                className={`combat-result ${attackerWon ? 'victory' : defenderWon ? 'defeat' : 'draw'}`}
                style={{
                  backgroundColor: resultBannerBackgroundColor,
                }}
              >
                <span className="result-text">
                  {attackerWon && 'Victory!'}
                  {defenderWon && 'Defeat!'}
                  {!attackerWon && !defenderWon && 'Mutual Destruction!'}
                </span>
              </div>
            </div>
          )}
        </div>

        <footer className="modal-footer">
          <div className="combat-actions">
            {/* Read-only (spectator): no action buttons */}
            {readOnly && (
              <span className="combat-spectator-label">Spectating</span>
            )}
            {/* Initial: Close (tan) + Start (red) */}
            {!readOnly && showInitialButtons && (
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
            {!readOnly && showDecisionButtons && (
              <>
                <button
                  type="button"
                  className={`combat-btn-retreat${canRetreat ? '' : ' disabled'}`}
                  onClick={canRetreat ? handleRetreatClick : undefined}
                  disabled={!canRetreat}
                  title={
                    !canRetreat
                      ? seaRaidCombat
                        ? 'Retreat is not available during a sea raid'
                        : 'Retreat unavailable (click Continue first after archer prefire, or no adjacent allied/neutral territory)'
                      : undefined
                  }
                >
                  Retreat
                </button>
                <button type="button" className="combat-btn-continue" onClick={handleContinue}>
                  Continue
                </button>
              </>
            )}

            {/* Attacker options: fuse (when applicable) leftmost, then casualty + must conquer */}
            {!readOnly && (showInitialButtons || showDecisionButtons) && (
              <div className="combat-options-pills">
                {attackerHasFuseBombOption && showInitialButtons && (
                  <div className="combat-options-row">
                    <div className="combat-pill-group">
                      <button
                        type="button"
                        className={`combat-pill combat-pill--segmented-left${fuseBombPill ? ' combat-pill--active' : ''}`}
                        onClick={() => setFuseBombPill(true)}
                        title="Bomb detonates in the siegeworks round when applicable; paired bomb units are removed after"
                      >
                        Yes
                      </button>
                      <button
                        type="button"
                        className={`combat-pill combat-pill--segmented-right${!fuseBombPill ? ' combat-pill--active' : ''}`}
                        onClick={() => setFuseBombPill(false)}
                        title="Bomb skips siegeworks; no detonation there — bomb stays as non-rolling siegework in standard combat"
                      >
                        No
                      </button>
                    </div>
                    <span className="combat-options-field-label">Fuse bomb</span>
                  </div>
                )}
                <div className="combat-options-row">
                  <div className="combat-pill-group">
                    <button
                      type="button"
                      className={`combat-pill combat-pill--segmented-left${casualtyPriorityPill === 'best_unit' ? ' combat-pill--active' : ''}`}
                      onClick={() => setCasualtyPriorityPill('best_unit')}
                      disabled={attackerCombatOptionsLocked}
                      title="Lose cheap/weak units first (cost then attack)"
                    >
                      Best Unit
                    </button>
                    <button
                      type="button"
                      className={`combat-pill combat-pill--segmented-right${casualtyPriorityPill === 'best_attack' ? ' combat-pill--active' : ''}`}
                      onClick={() => setCasualtyPriorityPill('best_attack')}
                      disabled={attackerCombatOptionsLocked}
                      title="Prioritize attack value (lose low attack first)"
                    >
                      Best Attack
                    </button>
                  </div>
                  <span className="combat-options-field-label">Casualty Priority</span>
                </div>
                <div className="combat-options-row">
                  <div className="combat-pill-group">
                    <button
                      type="button"
                      className={`combat-pill combat-pill--segmented-left${!mustConquerPill ? ' combat-pill--active' : ''}`}
                      onClick={() => setMustConquerPill(false)}
                      disabled={attackerCombatOptionsLocked}
                      title="Aerial may survive last"
                    >
                      Off
                    </button>
                    <button
                      type="button"
                      className={`combat-pill combat-pill--segmented-right${mustConquerPill ? ' combat-pill--active' : ''}`}
                      onClick={() => setMustConquerPill(true)}
                      disabled={attackerCombatOptionsLocked}
                      title="Kill aerial before last ground unit so a ground unit can conquer"
                    >
                      On
                    </button>
                  </div>
                  <span className="combat-options-field-label">Must conquer</span>
                </div>
              </div>
            )}

            {/* Retreat confirmation */}
            {!readOnly && showRetreatConfirm && (
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
            {!readOnly && showRetreatSelection && (
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
            {!readOnly && showCloseButton && (
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
