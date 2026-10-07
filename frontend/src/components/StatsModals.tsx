import React from 'react';
import type { ApiFactionStats, FactionStatEntry } from '../services/api';
import { ringIconSrc } from '../ringsDisplay';
import './Header.css';

export interface UnitForStats {
  id: string;
  name: string;
  icon: string;
  cost: number;
  attack: number;
  defense: number;
  dice: number;
  movement: number;
  health: number;
  specials: string[];
}

export type StatsFactionData = Record<
  string,
  { name: string; icon: string; color: string; alliance: string; parent?: string }
>;

function sortByTurnOrder(factionIds: string[], turnOrder: string[]): string[] {
  return [...factionIds].sort((a, b) => {
    const ia = turnOrder.indexOf(a);
    const ib = turnOrder.indexOf(b);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

function unitStatsFactionOrder(
  unitsByFaction: Record<string, UnitForStats[]>,
  factionData: StatsFactionData,
  turnOrder: string[],
): string[] {
  const withUnits = (fid: string) => (unitsByFaction[fid]?.length ?? 0) > 0;
  const allianceOrder = ['good', 'evil'].filter((a) =>
    Object.values(factionData).some((fd) => fd.alliance === a),
  );
  if (allianceOrder.length > 0) {
    const ordered = allianceOrder.flatMap((a) =>
      sortByTurnOrder(
        Object.keys(factionData).filter((fid) => factionData[fid]?.alliance === a && withUnits(fid)),
        turnOrder,
      ),
    );
    const remaining = Object.keys(factionData).filter((fid) => withUnits(fid) && !ordered.includes(fid));
    const extra = Object.keys(unitsByFaction).filter((fid) => withUnits(fid) && !ordered.includes(fid) && !remaining.includes(fid));
    return [...ordered, ...sortByTurnOrder(remaining, turnOrder), ...sortByTurnOrder(extra, turnOrder)];
  }
  const fromData = Object.keys(factionData).filter(withUnits);
  const extra = Object.keys(unitsByFaction).filter((fid) => withUnits(fid) && !fromData.includes(fid));
  return [...sortByTurnOrder(fromData, turnOrder), ...sortByTurnOrder(extra, turnOrder)];
}

export type StatsRingMark = { id: string; name: string };

export function GameStatsModal({
  factionStats,
  factionData,
  turnOrder = [],
  ringsByFaction,
  toolbar,
  onClose,
}: {
  factionStats: ApiFactionStats | null | undefined;
  factionData: StatsFactionData;
  turnOrder?: string[];
  /** Ring icons for each faction row, already assigned by who holds them. */
  ringsByFaction?: Record<string, StatsRingMark[]>;
  toolbar?: React.ReactNode;
  onClose: () => void;
}) {
  const alliances = factionStats?.alliances ?? {};
  const factionStatEntries = factionStats?.factions ?? {};
  const subfactionStatEntries = factionStats?.subfactions ?? {};
  const allianceOrder = ['good', 'evil'].filter((a) => a in alliances);

  const ringsByRow: Record<string, StatsRingMark[]> = {};
  for (const [fid, rings] of Object.entries(ringsByFaction ?? {})) {
    const parent = factionData[fid]?.parent;
    const row = parent && !subfactionStatEntries[fid] ? parent : fid;
    (ringsByRow[row] ??= []).push(...rings);
  }

  const renderRow = (fid: string, st: FactionStatEntry, isSub: boolean) => {
    const fd = factionData[fid];
    const rings = ringsByRow[fid] ?? [];
    return (
      <tr key={fid} className={isSub ? 'stats-faction-row stats-subfaction-row' : 'stats-faction-row'}>
        <td className="stats-col-faction">
          <span className="stats-faction-cell-inner">
            {fd?.icon && <img className="stats-faction-icon" src={fd.icon} alt="" aria-hidden />}
            <span>{fd?.name ?? fid}</span>
            {rings.length > 0 && (
              <span className="stats-faction-rings">
                {rings.map((ring) => (
                  <img key={ring.id} className="stats-faction-ring" src={ringIconSrc(ring.id)} alt="" title={ring.name} />
                ))}
              </span>
            )}
          </span>
        </td>
        <td className="stats-col-num">{st.strongholds}</td>
        <td className="stats-col-num">{st.territories}</td>
        <td className="stats-col-num">{st.power_per_turn}</td>
        <td className="stats-col-num">{isSub ? '–' : st.power}</td>
        <td className="stats-col-num">{st.units ?? 0}</td>
        <td className="stats-col-num">{st.unit_power ?? 0}</td>
      </tr>
    );
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal stats-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Game Stats</h2>
          <button type="button" className="close-btn" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="stats-modal-body">
          {toolbar && <div className="stats-modal-toolbar">{toolbar}</div>}
          {allianceOrder.length > 0 ? (
            <table className="header-stats-table header-stats-table--game">
              <colgroup>
                <col className="stats-game-col-faction" />
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <col key={i} className="stats-game-col-num" />
                ))}
              </colgroup>
              <thead>
                <tr>
                  <th className="stats-col-faction">Faction</th>
                  <th className="stats-col-num">S</th>
                  <th className="stats-col-num">T</th>
                  <th className="stats-col-num">PP</th>
                  <th className="stats-col-num">P</th>
                  <th className="stats-col-num">U</th>
                  <th className="stats-col-num">UP</th>
                </tr>
              </thead>
              <tbody>
                {allianceOrder.map((allianceKey) => {
                  const tot = alliances[allianceKey];
                  if (!tot) return null;
                  const allianceLabel = allianceKey === 'good' ? 'Good' : 'Evil';
                  const factionIds = sortByTurnOrder(
                    Object.keys(factionData).filter(
                      (fid) => factionData[fid]?.alliance === allianceKey && !factionData[fid]?.parent,
                    ),
                    turnOrder,
                  );
                  return (
                    <React.Fragment key={allianceKey}>
                      <tr className="stats-alliance-row">
                        <td className="stats-alliance-cell">{allianceLabel}</td>
                        <td className="stats-col-num">{tot.strongholds}</td>
                        <td className="stats-col-num">{tot.territories}</td>
                        <td className="stats-col-num">{tot.power_per_turn}</td>
                        <td className="stats-col-num">{tot.power}</td>
                        <td className="stats-col-num">{tot.units ?? 0}</td>
                        <td className="stats-col-num">{tot.unit_power ?? 0}</td>
                      </tr>
                      {factionIds.map((fid) => {
                        const st = factionStatEntries[fid];
                        if (!st) return null;
                        const subIds = Object.keys(factionData).filter(
                          (sid) => factionData[sid]?.parent === fid && subfactionStatEntries[sid],
                        );
                        return (
                          <React.Fragment key={fid}>
                            {renderRow(fid, st, false)}
                            {subIds.map((sid) => renderRow(sid, subfactionStatEntries[sid], true))}
                          </React.Fragment>
                        );
                      })}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p className="stats-placeholder">No stats available.</p>
          )}
        </div>
        <p className="stats-modal-key">
          S = Strongholds | T = Territories | PP = Power production | P = Power | U = Units | UP = Unit power
        </p>
      </div>
    </div>
  );
}

export function UnitStatsModal({
  unitsByFaction,
  factionData,
  turnOrder = [],
  onClose,
}: {
  unitsByFaction: Record<string, UnitForStats[]>;
  factionData: StatsFactionData;
  turnOrder?: string[];
  onClose: () => void;
}) {
  const order = unitStatsFactionOrder(unitsByFaction, factionData, turnOrder);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal unit-stats-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Unit Stats</h2>
          <button type="button" className="close-btn" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="unit-stats-modal-body">
          {Object.keys(unitsByFaction).length > 0 ? (
            <table className="header-stats-table header-stats-table--units">
              <colgroup>
                <col className="stats-units-col-name" />
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <col key={i} className="stats-units-col-num" />
                ))}
                <col className="stats-units-col-specials" />
              </colgroup>
              <thead>
                <tr>
                  <th className="stats-col-unit">Unit</th>
                  <th className="stats-col-num">P</th>
                  <th className="stats-col-num">A</th>
                  <th className="stats-col-num">D</th>
                  <th className="stats-col-num">R</th>
                  <th className="stats-col-num">M</th>
                  <th className="stats-col-num">HP</th>
                  <th className="stats-col-num stats-col-specials">SP</th>
                </tr>
              </thead>
              <tbody>
                {order.flatMap((fid) => {
                  const units = unitsByFaction[fid] ?? [];
                  const fd = factionData[fid];
                  return [
                    <tr key={`faction-${fid}`} className="unit-stats-faction-row">
                      <td colSpan={8} className="stats-col-unit">
                        <div className="unit-stats-name-cell">
                          {fd?.icon && (fd?.alliance === 'good' || fd?.alliance === 'evil') && (
                            <img className="unit-stats-faction-icon" src={fd.icon} alt="" aria-hidden />
                          )}
                          <span>{fd?.name ?? fid}</span>
                        </div>
                      </td>
                    </tr>,
                    ...units.map((u) => (
                      <tr key={u.id} className="unit-stats-unit-row">
                        <td className="stats-col-unit">
                          <div className="unit-stats-name-cell">
                            <img src={u.icon} alt="" className="unit-stats-unit-icon" aria-hidden />
                            <span className="unit-name-text">{u.name}</span>
                          </div>
                        </td>
                        <td className="stats-col-num">{u.cost}</td>
                        <td className="stats-col-num">{u.attack}</td>
                        <td className="stats-col-num">{u.defense}</td>
                        <td className="stats-col-num">{u.dice}</td>
                        <td className="stats-col-num">{u.movement}</td>
                        <td className="stats-col-num">{u.health}</td>
                        <td className="stats-col-num stats-col-specials">
                          {u.specials?.length ? u.specials.join(', ') : ''}
                        </td>
                      </tr>
                    )),
                  ];
                })}
              </tbody>
            </table>
          ) : (
            <p className="stats-placeholder">No unit definitions available.</p>
          )}
        </div>
        <p className="unit-stats-modal-key">
          P = Power cost | A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
        </p>
      </div>
    </div>
  );
}
