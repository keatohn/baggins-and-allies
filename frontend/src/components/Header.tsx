import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import type { GameState } from '../types/game';
import type { ApiFactionStats, SpecialDefinition } from '../services/api';
import StrongholdAllianceBar from './StrongholdAllianceBar';
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
  /** Setup / scenario display name from manifest (left of game name on the subtitle row). */
  setupDisplayName?: string | null;
  /** Special ability definitions from setup (backend). Key = special id. */
  specials?: Record<string, SpecialDefinition>;
  /** Display order for specials (from setup). */
  specialsOrder?: string[];
  /** Per-special list of units (for Specials modal). Key = special id. */
  unitsBySpecial?: Record<string, Array<{ unitId: string; name: string; faction: string; factionDisplayName: string; cost: number; homeTerritoryDisplayNames?: string[] }>>;
  /** Open Combat Simulator modal (button shown between Menu and Specials when set). */
  onOpenCombatSim?: () => void;
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

function Header({ gameState, turnOrderForTicker, factionData, effectivePower, factionStats, unitsByFaction = {}, gameName = null, setupDisplayName = null, specials = {}, specialsOrder: _specialsOrder = [], unitsBySpecial = {}, onOpenCombatSim }: HeaderProps) {
  const [statsOpen, setStatsOpen] = useState(false);
  const [specialsOpen, setSpecialsOpen] = useState(false);
  const [unitStatsOpen, setUnitStatsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
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
        className={`header${factionColor ? ' header--faction-turn' : ''}`}
        style={
          factionColor ? { ['--header-faction-accent' as string]: factionColor } : undefined
        }
      >
        <Link to="/" className="header-menu-btn" title="Main menu" aria-label="Main menu">
          Menu
        </Link>
        <button
          type="button"
          className="header-help-btn"
          onClick={() => setHelpOpen(true)}
          title="Help"
          aria-label="Open help"
        >
          <svg className="header-help-icon" viewBox="0 0 24 24" aria-hidden>
            <path
              d="M10.5 8.2c0-1.4 1.1-2.5 2.5-2.5s2.5 1.1 2.5 2.5c0 1.2-.8 1.9-1.6 2.6l-.4.3c-.6.5-1 1-1 1.8v.6"
              fill="none"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="12" cy="17" r="2.4" fill="currentColor" />
          </svg>
        </button>
        {onOpenCombatSim && (
          <button
            type="button"
            className="header-combat-sim-btn"
            onClick={onOpenCombatSim}
            title="Combat Simulator"
            aria-label="Open Combat Simulator"
          >
            <span className="header-combat-sim-icon-wrap" aria-hidden>
              <span className="header-combat-sim-emoji">⚔️</span>
              <svg className="header-combat-sim-die" viewBox="0 0 24 24" aria-hidden>
                <rect x="4" y="4" width="20" height="20" rx="3" />
                <circle cx="9" cy="9" r="2.2" />
                <circle cx="19" cy="9" r="2.2" />
                <circle cx="14" cy="14" r="2.2" />
                <circle cx="9" cy="19" r="2.2" />
                <circle cx="19" cy="19" r="2.2" />
              </svg>
            </span>
          </button>
        )}
        <button
          type="button"
          className="header-specials-btn"
          onClick={() => setSpecialsOpen(true)}
          title="Specials"
          aria-label="Open special abilities"
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

        {/* Stronghold bar: Good (white) | Neutral (gray) | Evil (black); victory threshold markers from setup */}
        {allianceOrder.length > 0 && factionStats && (
          <div className="header-stronghold-bar-wrap">
            <StrongholdAllianceBar factionStats={factionStats} variant="header" />
          </div>
        )}

        {/* Title to the right of stronghold bar (left-aligned group) so it doesn’t overlap faction logos on narrow screens */}
        <div className="header-center-title">
          <span className="header-center-title-brand">Baggins & Allies</span>
          {(setupDisplayName || gameName) && (
            <div className="header-center-title-subrow">
              {setupDisplayName && (
                <span className="header-center-title-setup" title={setupDisplayName}>
                  {setupDisplayName}
                </span>
              )}
              {setupDisplayName && gameName && (
                <span className="header-center-title-subsep" aria-hidden>
                  ·
                </span>
              )}
              {gameName && (
                <span className="header-center-title-game" title={gameName}>
                  {gameName}
                </span>
              )}
            </div>
          )}
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
                                  <span className="stats-faction-cell-inner">
                                    {fd?.icon && (
                                      <img className="stats-faction-icon" src={fd.icon} alt="" aria-hidden />
                                    )}
                                    <span>{name}</span>
                                  </span>
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

      {helpOpen && (
        <div className="modal-overlay" onClick={() => setHelpOpen(false)}>
          <div className="modal help-modal" onClick={e => e.stopPropagation()}>
            <header className="modal-header">
              <h2>Help</h2>
              <button type="button" className="close-btn" onClick={() => setHelpOpen(false)}>×</button>
            </header>
            <div className="help-modal-body">
              <section className="help-section">
                <h3>New to Axis & Allies?</h3>
                <p>We recommend reading up on the basic rules using a free online Axis &amp; Allies rulebook or YouTube tutorial.</p>
              </section>
              <section className="help-section">
                <h3>Familiar with Axis & Allies?</h3>
                <p>Baggins & Allies has the same game mechanics, but with maps from Middle-earth, unique Lord of the Rings units per faction, and a few additional twists:</p>
                <ul className="help-icon-list">
                  <li>Combat uses <strong>10-sided dice.</strong></li>
                  <li>Neutral units like cave trolls and goblins spawn in unowned territories. They can only defend against attacks.</li>
                  <li>Unit specials modify combat. Reference the <strong>Specials</strong> button (★ SP) for details on unit abilities and combat modifiers.</li>
                  <li>Terrain types can impact combat. Mountains and rivers block ground movement. Bridges allow ground units to cross over a river. Aerial units can fly over mountains and rivers.</li>
                </ul>
              </section>
              <section className="help-section">
                <h3>Victory criteria</h3>
                <p>
                  The first alliance to control the required number of strongholds at the conclusion of a full turn cycle wins the game. The last faction in the turn order must complete their turn.
                </p>
              </section>
              <section className="help-section">
                <h3>Terminology</h3>
                <ul className="help-icon-list">
                  <li><strong>Strongholds</strong> = Victory cities</li>
                  <li><strong>Camps</strong> = Land industrial complexes</li>
                  <li><strong>Ports</strong> = Naval industrial complexes</li>
                  <li><strong>Power</strong> = IPCs</li>
                  <li><strong>Charging</strong> = Blitzing</li>
                  <li><strong>Sea Raid</strong> = Amphibious Assault</li>
                </ul>
              </section>
              <section className="help-section">
                <h3>Map Icons</h3>
                <ul className="help-icon-list">
                  <li><span className="help-icon" aria-hidden>⛺</span> <strong>Camp</strong> — mobilize new land units here.</li>
                  <li><span className="help-icon" aria-hidden>⚓</span> <strong>Port</strong> — mobilize new ships in adjacent sea zones.</li>
                  <li><span className="help-icon" aria-hidden><img src="/ford.png" alt="" width={18} height={18} style={{ verticalAlign: 'text-bottom' }} /></span> <strong>Ford</strong> — shallow river crossing for certain units, along with up to 2 transport passengers (see "Ford Crosser" in the Specials button).</li>
                  <li><span className="help-icon" aria-hidden>🏠</span> <strong>Home</strong> — deploy 1 of certain units to their home territory per turn without a camp (see "Home" in the Specials button).</li>
                  <li><span className="help-icon" aria-hidden>🌲</span><span className="help-icon" aria-hidden>⛰️</span> Terrain types: Forest and Mountains — certain units receive terrain bonuses during combat (see "Forest" and "Mountain" in the Specials button).</li>
                  <li>Strongholds show a faction logo. Capitals show a larger faction logo.</li>
                </ul>
              </section>
              <section className="help-section">
                <h3>Phase Order</h3>
                <p><strong>Purchase</strong> → <strong>Combat move</strong> → <strong>Combat</strong> → <strong>Non-combat move</strong> → <strong>Mobilization</strong></p>
                <ul className="help-icon-list">
                  <li><strong>Purchase</strong> — buy units with power to mobilize at the end of the turn.</li>
                  <li><strong>Combat move</strong> — declare attacks by moving units into enemy or neutral territories.</li>
                  <li><strong>Combat</strong> — resolve declared combat moves: roll attack vs defense, apply hits, remove casualties, repeat or retreat until a side is defeated. Some units have specials that can alter combat flow and attack/defense values. See the <strong>Specials</strong> modal (★ SP) for details.</li>
                  <li><strong>Non-combat move</strong> — move units to friendly territories with remaining movement.</li>
                  <li><strong>Mobilization</strong> — place purchased units: land units in territories with your camp (or home territory for units with the &quot;home&quot; special), ships in sea zones adjacent to your port. Place any camps you bought in eligible territories.</li>
                </ul>
              </section>
              <section className="help-section">
                <h3>Combat Order</h3>
                <p>Stealth or Siegeworks Round (if applicable) → Archer Round (if applicable) → Standard Combat Rounds (until retreat or completion)</p>
              </section>
              <section className="help-section">
                <h3>Stronghold HP</h3>
                <p>
                  Stronghold territories have hit points that soak the first hits of an attack. Once hit, strongholds can be repaired during the purchase phase of its owner.
                </p>
              </section>
              <section className="help-section">
                <h3>Unit Types</h3>
                <ul className="help-icon-list">
                  <li><strong>Infantry</strong> — base ground units.</li>
                  <li><strong>Archer</strong> — ground units that can fire before combat rounds on defense.</li>
                  <li><strong>Cavalry</strong> — ground units that can conquer empty territories in its combat movement path.</li>
                  <li><strong>Siegeworks</strong> — ground units that only roll during the siegeworks combat round.</li>
                  <ul className="help-icon-list">
                    <li><strong>Ram</strong> — hits can only be directed at strongholds, not units.</li>
                    <li><strong>Ladder</strong> — instead of rolling for hits, allows up to 2 infantry to bypass the defender's stronghold HP and allocate their hits directly at defending units.</li>
                  </ul>
                  <li><strong>Aerial</strong> — aerial units can ignore terrain obstacles (mountains, rivers, sea zones, etc.) and attack naval units before returning to land. They cannot conquer a territory without a ground unit.</li>
                  <li><strong>Naval</strong> — naval units are limited to sea zones only.</li>
                </ul>
              </section>
            </div>
          </div>
        </div >
      )
      }

      {
        specialsOpen && (
          <div className="modal-overlay" onClick={() => setSpecialsOpen(false)}>
            <div className="modal specials-modal" onClick={e => e.stopPropagation()}>
              <header className="modal-header">
                <h2>Specials</h2>
                <button type="button" className="close-btn" onClick={() => setSpecialsOpen(false)}>×</button>
              </header>
              <div className="specials-modal-body">
                <dl className="specials-list">
                  {Object.keys(specials)
                    .filter(k => specials[k])
                    .sort((a, b) => (specials[a]?.name ?? a).localeCompare(specials[b]?.name ?? b))
                    .map(key => {
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
        )
      }

      {
        unitStatsOpen && (
          <div className="modal-overlay" onClick={() => setUnitStatsOpen(false)}>
            <div className="modal unit-stats-modal" onClick={e => e.stopPropagation()}>
              <header className="modal-header">
                <h2>Unit Stats</h2>
                <button type="button" className="close-btn" onClick={() => setUnitStatsOpen(false)}>×</button>
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
                      {unitStatsFactionOrder.flatMap(fid => {
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
                          ...units.map(u => (
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
                P = Power cost | A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
              </p>
            </div>
          </div>
        )
      }
    </>
  );
}

export default Header;
