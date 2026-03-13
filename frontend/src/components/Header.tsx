import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import type { GameState } from '../types/game';
import type { ApiFactionStats, SpecialDefinition } from '../services/api';
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

interface HeaderProps {
  gameState: GameState;
  /** Faction IDs in display order for ticker (from create response or backend). Overrides gameState.turn_order when provided. */
  turnOrderForTicker?: string[];
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string }>;
  effectivePower?: number;
  factionStats?: ApiFactionStats | null;
  unitsByFaction?: Record<string, UnitForStats[]>;
  /** Current game name (created/loaded), shown under "Baggins & Allies" in the center */
  gameName?: string | null;
  /** Special ability definitions from setup (backend). Key = special id. */
  specials?: Record<string, SpecialDefinition>;
  /** Display order for specials (from setup). */
  specialsOrder?: string[];
  /** Per-special list of units (for Specials modal). Key = special id. */
  unitsBySpecial?: Record<string, Array<{ unitId: string; name: string; faction: string; factionDisplayName: string; cost: number; homeTerritoryDisplayNames?: string[] }>>;
}

const PHASE_ORDER: string[] = ['purchase', 'combat_move', 'combat', 'non_combat_move', 'mobilization'];

function formatPhase(phase: string): string {
  if (phase === 'non_combat_move') return 'Non-Combat Move';
  if (phase === 'mobilization') return 'Mobilization';
  return phase
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function phaseLabel(phase: string): string {
  const phaseKey = phase === 'mobilize' ? 'mobilization' : phase;
  const idx = PHASE_ORDER.indexOf(phaseKey);
  const n = PHASE_ORDER.length;
  const current = idx >= 0 ? idx + 1 : 1;
  return `${formatPhase(phase)} (${current}/${n})`;
}

function Header({ gameState, turnOrderForTicker, factionData, effectivePower, factionStats, unitsByFaction = {}, gameName = null, specials = {}, specialsOrder = [], unitsBySpecial = {} }: HeaderProps) {
  const [statsOpen, setStatsOpen] = useState(false);
  const [specialsOpen, setSpecialsOpen] = useState(false);
  const [unitStatsOpen, setUnitStatsOpen] = useState(false);
  const faction = factionData[gameState.current_faction];
  const resources = gameState.faction_resources[gameState.current_faction];
  const power = effectivePower ?? resources?.power ?? 0;
  const factionColor = faction?.color;

  const alliances = factionStats?.alliances ?? {};
  const factionStatEntries = factionStats?.factions ?? {};
  const allianceOrder = ['good', 'evil'].filter(a => a in alliances);
  const turnOrder = (turnOrderForTicker?.length ? turnOrderForTicker : gameState.turn_order) ?? [];
  const sortByTurnOrder = (factionIds: string[]) =>
    [...factionIds].sort((a, b) => {
      const ia = turnOrder.indexOf(a);
      const ib = turnOrder.indexOf(b);
      if (ia === -1 && ib === -1) return 0;
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
  // Unit stats: factions with units, grouped by alliance (good then evil), then neutral/other at bottom
  const unitStatsFactionOrder = (() => {
    const withUnits = (fid: string) => (unitsByFaction[fid]?.length ?? 0) > 0;
    if (allianceOrder.length > 0) {
      const ordered = allianceOrder.flatMap(a =>
        sortByTurnOrder(
          Object.keys(factionData).filter(
            fid => factionData[fid]?.alliance === a && withUnits(fid)
          )
        )
      );
      const neutralAndOther = Object.keys(factionData).filter(
        fid => withUnits(fid) && !ordered.includes(fid)
      );
      return [...ordered, ...sortByTurnOrder(neutralAndOther)];
    }
    return sortByTurnOrder(Object.keys(factionData).filter(withUnits));
  })();

  return (
    <>
      <header
        className="header"
        style={
          factionColor
            ? { borderBottomColor: factionColor, borderBottomWidth: 3 }
            : undefined
        }
      >
        <Link to="/" className="header-menu-btn" title="Main menu" aria-label="Main menu">
          Menu
        </Link>
        <button
          type="button"
          className="header-specials-btn"
          onClick={() => setSpecialsOpen(true)}
          title="Specials"
          aria-label="Open specials definitions"
        >
          <span className="header-specials-star" aria-hidden>★</span>
          <span className="header-specials-sp">SP</span>
        </button>
        <button
          type="button"
          className="header-unit-stats-btn"
          onClick={() => setUnitStatsOpen(true)}
          title="Unit stats"
          aria-label="Open unit stats"
        >
          <img src="/assets/units/gondor_soldier.png" alt="" className="header-unit-stats-icon" aria-hidden />
        </button>
        <button
          type="button"
          className="header-stats-btn"
          onClick={() => setStatsOpen(true)}
          title="Game stats"
          aria-label="Open game stats"
        >
          <svg className="stats-icon" viewBox="0 0 24 24" aria-hidden>
            <rect x="3" y="14" width="4" height="6" rx="1" />
            <rect x="10" y="10" width="4" height="10" rx="1" />
            <rect x="17" y="4" width="4" height="16" rx="1" />
          </svg>
        </button>

        {/* Stronghold bar: Good (white) | Neutral (gray) | Evil (black) — no label for neutral */}
        {allianceOrder.length > 0 && (() => {
          const good = alliances['good']?.strongholds ?? 0;
          const evil = alliances['evil']?.strongholds ?? 0;
          const neutral = factionStats?.neutral_strongholds ?? 0;
          const total = good + neutral + evil || 1;
          const goodPct = (good / total) * 100;
          const neutralPct = (neutral / total) * 100;
          const evilPct = (evil / total) * 100;
          return (
            <div className="header-stronghold-bar-wrap">
              <div className="header-stronghold-bar">
                <div
                  className="header-stronghold-bar-good"
                  style={{ width: `${goodPct}%` }}
                />
                <div
                  className="header-stronghold-bar-neutral"
                  style={{ width: `${neutralPct}%` }}
                />
                <div
                  className="header-stronghold-bar-evil"
                  style={{ width: `${evilPct}%` }}
                />
              </div>
              <span className="header-stronghold-bar-label">Good {good} · Evil {evil}</span>
            </div>
          );
        })()}

        {/* Title to the right of stronghold bar (left-aligned group) so it doesn’t overlap faction logos on narrow screens */}
        <div className="header-center-title">
          <span className="header-center-title-brand">Baggins & Allies</span>
          {gameName && <span className="header-center-title-game">{gameName}</span>}
        </div>

        <div className="header-spacer" />

        {/* Turn order ticker: faction logos in turn order (from setup or turnOrderForTicker), gold ring around current */}
        <div className="header-turn-ticker" aria-label="Turn order" style={factionColor ? { borderColor: factionColor } : undefined}>
          {(() => {
            const displayOrder = turnOrder.filter((f) => factionData[f]);
            const order = displayOrder.length > 0 ? displayOrder : Object.keys(factionData).sort();
            return order.map((fid) => {
              const fd = factionData[fid];
              const isCurrent = fid === gameState.current_faction;
              return (
                <div
                  key={fid}
                  className={`header-turn-ticker-slot ${isCurrent ? 'header-turn-ticker-slot--current' : ''}`}
                  title={isCurrent ? `${fd?.name ?? fid} (current turn)` : fd?.name ?? fid}
                >
                  {fd?.icon && (
                    <img src={fd.icon} alt="" className="header-turn-ticker-icon" aria-hidden />
                  )}
                </div>
              );
            });
          })()}
        </div>

        <div className="faction-header" style={factionColor ? { borderColor: factionColor } : undefined}>
          <span className="faction-title">{faction?.name}</span>
        </div>

        <div className="turn-status">
          <span className="turn-number">Turn {gameState.turn_number}</span>
          <span className="phase-divider">|</span>
          <span className="current-phase">{phaseLabel(gameState.phase)}</span>
          <span className="phase-divider">|</span>
          <span className="current-power">{power}P</span>
        </div>
      </header>

      {statsOpen && (
        <div className="modal-overlay" onClick={() => setStatsOpen(false)}>
          <div className="modal stats-modal" onClick={e => e.stopPropagation()}>
            <header className="modal-header">
              <h2>Game Stats</h2>
              <button type="button" className="close-btn" onClick={() => setStatsOpen(false)}>×</button>
            </header>
            <div className="stats-modal-body">
              {allianceOrder.length > 0 ? (
                <table className="header-stats-table">
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
                      {allianceOrder.map(allianceKey => {
                        const tot = alliances[allianceKey];
                        if (!tot) return null;
                        const allianceLabel = allianceKey === 'good' ? 'Good' : 'Evil';
                        const factionIds = sortByTurnOrder(
                          Object.keys(factionData).filter(
                            fid => factionData[fid]?.alliance === allianceKey
                          )
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
                            {factionIds.map(fid => {
                              const st = factionStatEntries[fid];
                              if (!st) return null;
                              const fd = factionData[fid];
                              const name = fd?.name ?? fid;
                              return (
                                <tr key={fid} className="stats-faction-row">
                                  <td className="stats-col-faction">
                                    {fd?.icon && (
                                      <img className="stats-faction-icon" src={fd.icon} alt="" aria-hidden />
                                    )}
                                    <span>{name}</span>
                                  </td>
                                  <td className="stats-col-num">{st.strongholds}</td>
                                  <td className="stats-col-num">{st.territories}</td>
                                  <td className="stats-col-num">{st.power_per_turn}</td>
                                  <td className="stats-col-num">{st.power}</td>
                                  <td className="stats-col-num">{st.units ?? 0}</td>
                                  <td className="stats-col-num">{st.unit_power ?? 0}</td>
                                </tr>
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
      )}

      {specialsOpen && (
        <div className="modal-overlay" onClick={() => setSpecialsOpen(false)}>
          <div className="modal specials-modal" onClick={e => e.stopPropagation()}>
            <header className="modal-header">
              <h2>Specials</h2>
              <button type="button" className="close-btn" onClick={() => setSpecialsOpen(false)}>×</button>
            </header>
            <div className="specials-modal-body">
              <dl className="specials-list">
                {(specialsOrder.length > 0 ? specialsOrder : Object.keys(specials).sort()).filter(k => specials[k]).map(key => {
                  const def = specials[key];
                  const termLabel = (def.display_code != null && String(def.display_code).trim() !== '') ? `${def.name} (${def.display_code})` : (def.name ?? key);
                  const unitList = unitsBySpecial[key] ?? [];
                  return (
                    <React.Fragment key={key}>
                      <dt className="specials-term">{termLabel}</dt>
                      <dd className="specials-desc">
                        {def.description}
                        {unitList.length > 0 && (
                          <ul className="specials-unit-list" aria-label={`Units with ${def.name}`}>
                            {unitList.map(({ unitId, name, factionDisplayName, homeTerritoryDisplayNames }) => (
                              <li key={unitId}>
                                {name} ({factionDisplayName})
                                {key === 'home' && homeTerritoryDisplayNames?.length
                                  ? `: ${homeTerritoryDisplayNames.join(', ')}`
                                  : ''}
                              </li>
                            ))}
                          </ul>
                        )}
                      </dd>
                    </React.Fragment>
                  );
                })}
              </dl>
            </div>
          </div>
        </div>
      )}

      {unitStatsOpen && (
        <div className="modal-overlay" onClick={() => setUnitStatsOpen(false)}>
          <div className="modal unit-stats-modal" onClick={e => e.stopPropagation()}>
            <header className="modal-header">
              <h2>Unit Stats</h2>
              <button type="button" className="close-btn" onClick={() => setUnitStatsOpen(false)}>×</button>
            </header>
            <div className="unit-stats-modal-body">
              {Object.keys(unitsByFaction).length > 0 ? (
                <table className="header-stats-table">
                  <thead>
                    <tr>
                      <th className="stats-col-unit">Unit</th>
                      <th className="stats-col-num">C</th>
                      <th className="stats-col-num">A</th>
                      <th className="stats-col-num">D</th>
                      <th className="stats-col-num">R</th>
                      <th className="stats-col-num">M</th>
                      <th className="stats-col-num">HP</th>
                      <th className="stats-col-num stats-col-specials">SP</th>
                    </tr>
                  </thead>
                  <tbody>
                    {unitStatsFactionOrder.flatMap(fid => {
                      const units = unitsByFaction[fid] ?? [];
                      const fd = factionData[fid];
                      return [
                        <tr key={`faction-${fid}`} className="unit-stats-faction-row">
                          <td colSpan={8} className="stats-col-unit">
                            {fd?.icon && (fd?.alliance === 'good' || fd?.alliance === 'evil') && (
                              <img className="unit-stats-faction-icon" src={fd.icon} alt="" aria-hidden />
                            )}
                            <span>{fd?.name ?? fid}</span>
                          </td>
                        </tr>,
                        ...units.map(u => (
                          <tr key={u.id} className="unit-stats-unit-row">
                            <td className="stats-col-unit">
                              <img src={u.icon} alt="" className="unit-stats-unit-icon" aria-hidden />
                              <span className="unit-name-text">{u.name}</span>
                            </td>
                            <td className="stats-col-num">{u.cost}</td>
                            <td className="stats-col-num">{u.attack}</td>
                            <td className="stats-col-num">{u.defense}</td>
                            <td className="stats-col-num">{u.dice}</td>
                            <td className="stats-col-num">{u.movement}</td>
                            <td className="stats-col-num">{u.health}</td>
                            <td className="stats-col-num stats-col-specials">{u.specials?.length ? u.specials.join(', ') : ''}</td>
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
              C = Cost | A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
            </p>
          </div>
        </div>
      )}
    </>
  );
}

export default Header;
