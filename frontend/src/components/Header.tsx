import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { GameState } from '../types/game';
import type { ApiFactionStats, ApiSubfactionRule, SpecialDefinition } from '../services/api';
import StrongholdAllianceBar from './StrongholdAllianceBar';
import AccountMenu from './AccountMenu';
import SeatPlayerModal, { type SeatPortrait } from './account/SeatPlayerModal';
import { tierRingProps } from './account/tierRing';
import { useMenuScroll } from './MenuScrollContext';
import { GameStatsModal, RingsOfPowerModal, UnitStatsModal, type RingsOfPowerData, type StatsRingMark, type UnitForStats } from './StatsModals';
import './Header.css';

export type { UnitForStats };

interface HeaderProps {
  gameState: GameState;
  /** Faction IDs in display order for ticker (from create response or backend). Overrides gameState.turn_order when provided. */
  turnOrderForTicker?: string[];
  factionData: Record<string, { name: string; icon: string; color: string; alliance: string; parent?: string }>;
  factionStats?: ApiFactionStats | null;
  /** Rings held by each faction, shown beside the name in Game Stats. */
  ringsByFaction?: Record<string, StatsRingMark[]>;
  /** Rings of Power game: rings + display names. Shows the Rings of Power modal button when set. */
  ringsOfPower?: RingsOfPowerData | null;
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
  /** Scenario subfaction rules, keyed by subfaction id. */
  subfactionRules?: Record<string, ApiSubfactionRule>;
  /** Unit id → display name, for recruitment lines. */
  unitNames?: Record<string, string>;
  /** Special rules that apply to this game. */
  specialRules?: { type?: string; name?: string; description?: string }[];
  /** Catalog names and descriptions, used when a scenario leaves the description blank. */
  catalogRules?: { id: string; name: string; description: string }[];
  /** Human seat under each turn-order faction. Omitted for single-player. */
  seatPortraits?: Record<string, SeatPortrait>;
  /** Computer factions in the turn order. `dots` is the difficulty rank, easiest = 1. */
  computerSeats?: Record<string, { dots: number; difficulty: string }>;
  /** Logged-in player, so their own portrait opens the editable profile. */
  viewerId?: string | null;
}

type FactionMark = { id: string; name: string; icon: string; color: string };

/** Subfactions that have their own icon. A blank icon inherits the parent and is not badged. */
function subfactionMarks(
  parentId: string,
  factionData: Record<string, { name: string; icon: string; color: string; parent?: string }>,
): FactionMark[] {
  const parentIcon = factionData[parentId]?.icon;
  const marks: FactionMark[] = [];
  for (const [id, fd] of Object.entries(factionData)) {
    if (fd.parent !== parentId || !fd.icon || fd.icon === parentIcon) continue;
    marks.push({ id, name: fd.name, icon: fd.icon, color: fd.color });
  }
  return marks;
}

type HelpSubfaction = { id: string; name: string; parentName: string; rules: string[] };
type HelpSpecialRule = { key: string; name: string; description: string };

const SMALL_RULE_WORDS = new Set(['of', 'the', 'and', 'a']);

function specialRuleTitle(
  rule: { type?: string; name?: string },
  catalogName?: string,
): string {
  const own = rule.name?.trim();
  if (own) return own;
  const fromCatalog = catalogName?.trim();
  if (fromCatalog) return fromCatalog;
  const words = (rule.type ?? '').split('_').filter(Boolean);
  if (words.length === 0) return 'Special rule';
  return words
    .map((word, index) => (index > 0 && SMALL_RULE_WORDS.has(word) ? word : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(' ');
}

function helpSpecialRules(
  rules: { type?: string; name?: string; description?: string }[] | undefined,
  catalogRules: { id: string; name: string; description: string }[],
): HelpSpecialRule[] {
  const catalog = new Map(catalogRules.map((entry) => [entry.id, entry]));
  const out: HelpSpecialRule[] = [];
  (rules ?? []).forEach((rule, index) => {
    const type = typeof rule.type === 'string' ? rule.type.trim() : '';
    if (!type) return;
    const entry = catalog.get(type);
    const own = typeof rule.description === 'string' ? rule.description.trim() : '';
    const description = own || entry?.description?.trim() || '';
    out.push({
      key: `${type}-${index}`,
      name: specialRuleTitle(rule, entry?.name),
      description,
    });
  });
  return out;
}

function recruitmentLine(
  recruitment: ApiSubfactionRule['recruitment'],
  parentName: string,
  unitNames: Record<string, string>,
): string | null {
  if (!recruitment || typeof recruitment !== 'object') return null;
  const unitId = typeof recruitment.unit_id === 'string' ? recruitment.unit_id.trim() : '';
  const count = recruitment.count;
  if (!unitId || typeof count !== 'number' || !Number.isInteger(count) || count < 1) return null;
  const unitName = unitNames[unitId] || unitId;
  if (recruitment.mode === 'fixed') {
    return `Each turn, ${parentName} receives ${count} ${unitName} to mobilize.`;
  }
  if (recruitment.mode === 'per_territory') {
    const every = count === 1
      ? 'each territory this subfaction held at the start of the turn'
      : `every ${count} territories this subfaction held at the start of the turn`;
    return `Each turn, ${parentName} receives 1 ${unitName} for ${every}.`;
  }
  return null;
}

function subfactionRuleLines(
  parentName: string,
  rule: ApiSubfactionRule | undefined,
  unitNames: Record<string, string>,
): string[] {
  const economy = rule?.economy === 'pool' ? 'pool' : 'none';
  const purchasable = rule?.purchasable_by_parent === true;
  const mobilization = rule?.mobilization === 'any_home' ? 'any_home' : 'camps';
  const capture = rule?.capture === 'unit_faction' ? 'unit_faction' : 'liberate_else_parent';
  const movement = rule?.movement === 'home_only' ? 'home_only' : 'with_parent';
  const lines = [
    economy === 'pool'
      ? `Power from its territories is added to ${parentName}'s income.`
      : 'Its territories produce no power.',
    purchasable
      ? `${parentName} can purchase its units.`
      : `${parentName} cannot purchase its units.`,
  ];
  const grant = recruitmentLine(rule?.recruitment, parentName, unitNames);
  if (grant) lines.push(grant);
  lines.push(
    mobilization === 'any_home'
      ? 'Its units mobilize on land it owns, or in sea and river zones beside that land, without using camp or port capacity.'
      : `Its units mobilize at camps and ports, including ${parentName}'s, and count toward mobilization capacity.`,
    movement === 'home_only'
      ? 'Its units may only enter territories that originally belong to this subfaction.'
      : `Its units move with ${parentName} and may enter the same territories.`,
    capture === 'unit_faction'
      ? `Territory taken by its units alone belongs to this subfaction. A force mixed with other units gives the land to ${parentName}. Allied original owners are still liberated.`
      : `Territory it captures belongs to ${parentName}. If the land originally belonged to an ally, including this subfaction, it is liberated instead.`,
  );
  return lines;
}

function Header({ gameState, turnOrderForTicker, factionData, factionStats, ringsByFaction, ringsOfPower = null, unitsByFaction = {}, gameName = null, setupDisplayName = null, specials = {}, specialsOrder: _specialsOrder = [], unitsBySpecial = {}, onOpenCombatSim, subfactionRules = {}, unitNames = {}, specialRules = [], catalogRules = [], seatPortraits, computerSeats, viewerId = null }: HeaderProps) {
  const { navigateWithScroll } = useMenuScroll();
  const [statsOpen, setStatsOpen] = useState(false);
  const [ringsOpen, setRingsOpen] = useState(false);
  const [specialsOpen, setSpecialsOpen] = useState(false);
  const [unitStatsOpen, setUnitStatsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [portraitsOn, setPortraitsOn] = useState(false);
  const [openSeat, setOpenSeat] = useState<SeatPortrait | null>(null);
  const [seatEdits, setSeatEdits] = useState<Record<string, SeatPortrait>>({});
  const faction = factionData[gameState.current_faction];
  const factionColor = faction?.color;

  const alliances = factionStats?.alliances ?? {};
  const allianceOrder = ['good', 'evil'].filter(a => a in alliances);
  const turnOrder = (turnOrderForTicker?.length ? turnOrderForTicker : gameState.turn_order) ?? [];
  const showSeatToggle = Boolean(
    seatPortraits && (
      Object.values(seatPortraits).some((seat) => seat.playerId !== viewerId)
      || (computerSeats && Object.keys(computerSeats).length > 0)
    ),
  );
  const facesOn = portraitsOn && showSeatToggle;
  const seatFor = (factionId: string): SeatPortrait | undefined => {
    const parentId = factionData[factionId]?.parent;
    const raw = seatPortraits?.[factionId] ?? (parentId ? seatPortraits?.[parentId] : undefined);
    if (!raw) return undefined;
    const edited = seatEdits[raw.playerId];
    if (!edited || edited.relation === raw.relation) return raw;
    return edited;
  };
  const computerFor = (factionId: string) => {
    const parentId = factionData[factionId]?.parent;
    return computerSeats?.[factionId] ?? (parentId ? computerSeats?.[parentId] : undefined);
  };
  const helpSubfactions = useMemo((): HelpSubfaction[] => {
    const entries = Object.entries(factionData).filter(([, fd]) => fd.parent);
    if (entries.length === 0) return [];
    const rank = (parent?: string) => {
      const index = parent ? turnOrder.indexOf(parent) : -1;
      return index < 0 ? turnOrder.length + 1 : index;
    };
    entries.sort((a, b) => rank(a[1].parent) - rank(b[1].parent));
    return entries.map(([id, fd]) => {
      const parentName = (fd.parent && factionData[fd.parent]?.name) || fd.parent || 'its parent';
      return {
        id,
        name: fd.name || id,
        parentName,
        rules: subfactionRuleLines(parentName, subfactionRules[id], unitNames),
      };
    });
  }, [factionData, subfactionRules, turnOrder, unitNames]);
  const helpRules = useMemo(() => helpSpecialRules(specialRules, catalogRules), [specialRules, catalogRules]);
  return (
    <>
      <header
        className={`header${factionColor ? ' header--faction-turn' : ''}`}
        style={
          factionColor ? { ['--header-faction-accent' as string]: factionColor } : undefined
        }
      >
        <div className="header-scroll">
        <Link
          to="/"
          className="header-menu-btn"
          title="Main menu"
          aria-label="Main menu"
          onClick={(e) => {
            e.preventDefault();
            navigateWithScroll('/');
          }}
        >
          Menu
        </Link>
        <AccountMenu variant="header" />
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
        {ringsOfPower && (
          <button
            type="button"
            className="header-rings-btn"
            onClick={() => setRingsOpen(true)}
            title="Rings of Power"
            aria-label="Open Rings of Power"
          >
            <img src="/assets/rings/the_one.png" alt="" className="header-rings-icon" aria-hidden />
          </button>
        )}

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
        <div className="header-turn-cluster">
          {showSeatToggle && (
            <button
              type="button"
              className={`header-seats-btn${facesOn ? ' header-seats-btn--on' : ''}`}
              aria-pressed={facesOn}
              aria-label={facesOn ? 'Hide player portraits' : 'Show player portraits'}
              title={facesOn ? 'Hide player portraits' : 'Show player portraits'}
              onClick={() => setPortraitsOn((on) => !on)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="8" r="3.2" fill="currentColor" />
                <path d="M5.2 19.4c.9-3.4 3.4-5 6.8-5s5.9 1.6 6.8 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          )}
          <div
            className={`header-turn-ticker${facesOn ? ' header-turn-ticker--seats' : ''}`}
            aria-label="Turn order"
            style={factionColor ? { borderColor: factionColor } : undefined}
          >
            {(() => {
              const displayOrder = turnOrder.filter((f) => factionData[f]);
              const order = displayOrder.length > 0
                ? displayOrder
                : Object.keys(factionData).filter((fid) => !factionData[fid]?.parent).sort();
              return order.map((fid) => {
                const fd = factionData[fid];
                const isCurrent = fid === gameState.current_faction;
                const marks = subfactionMarks(fid, factionData);
                const badgeMode = marks.length === 2 ? 'two' : marks.length >= 3 ? 'row' : 'one';
                const baseName = fd?.name ?? fid;
                const markNames = marks.map((mark) => mark.name).filter(Boolean).join(', ');
                const seat = seatFor(fid);
                const title = markNames
                  ? `${baseName}${isCurrent ? ' (current turn)' : ''} — ${markNames}`
                  : isCurrent
                    ? `${baseName} (current turn)`
                    : baseName;
                const band = !isCurrent && fd?.color ? { borderColor: fd.color } : undefined;
                const face = seat?.avatar ?? { image: 'hobbit.png', color: '#2d5a27' };
                const ring = tierRingProps(seat?.tier);
                const computer = seat ? undefined : computerFor(fid);
                return (
                  <div
                    key={fid}
                    className={`header-turn-ticker-slot ${isCurrent ? 'header-turn-ticker-slot--current' : ''}`}
                    title={title}
                    /* Non-current: band matches the faction color. Current uses border:none + gold ring (CSS). */
                    style={facesOn ? undefined : band}
                  >
                    <div className="header-turn-ticker-logo" style={facesOn ? band : undefined}>
                      {fd?.icon && (
                        <img src={fd.icon} alt="" className="header-turn-ticker-icon" aria-hidden />
                      )}
                      {marks.length > 0 && (
                        <div
                          className={`header-turn-ticker-badges header-turn-ticker-badges--${badgeMode}`}
                          style={badgeMode === 'row' ? { ['--sub-count' as string]: String(marks.length) } : undefined}
                          aria-hidden
                        >
                          {marks.map((mark) => (
                            <span
                              key={mark.id}
                              className="header-turn-ticker-badge"
                              style={{ background: mark.color }}
                            >
                              <img src={mark.icon} alt="" />
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    {facesOn && (
                      seat ? (
                        <button
                          type="button"
                          className={`header-turn-ticker-portrait${ring.className}`}
                          style={{ background: face.color, ...ring.style }}
                          aria-label={`View ${seat.username}'s profile`}
                          title={seat.username}
                          onClick={() => setOpenSeat(seat)}
                        >
                          <img src={`/assets/units/${face.image}`} alt="" />
                        </button>
                      ) : computer ? (
                        <span
                          className="header-turn-ticker-portrait header-turn-ticker-portrait--computer"
                          title={`Computer · ${computer.difficulty}`}
                          aria-label={`Computer, ${computer.difficulty}, difficulty ${computer.dots}`}
                        >
                          <svg className="header-turn-ticker-robot" viewBox="0 0 24 24" aria-hidden="true">
                            <rect x="10.25" y="1.2" width="3.5" height="2.4" rx="1" fill="currentColor" />
                            <circle cx="12" cy="1.5" r="1.15" fill="currentColor" />
                            <rect x="3.5" y="5.2" width="17" height="13.2" rx="3.2" fill="currentColor" />
                            <circle cx="8.7" cy="10.6" r="1.7" fill="#2a3138" />
                            <circle cx="15.3" cy="10.6" r="1.7" fill="#2a3138" />
                            <rect x="8.2" y="14.1" width="7.6" height="1.5" rx="0.75" fill="#2a3138" />
                          </svg>
                          <span className="header-turn-ticker-robot-dots" aria-hidden="true">
                            {Array.from({ length: computer.dots }, (_, index) => (
                              <span key={index} />
                            ))}
                          </span>
                        </span>
                      ) : (
                        <span className="header-turn-ticker-portrait header-turn-ticker-portrait--empty" aria-hidden />
                      )
                    )}
                  </div>
                );
              });
            })()}
          </div>
        </div>

        <div className="faction-header" style={factionColor ? { borderColor: factionColor } : undefined}>
          <span className="faction-title">{faction?.name}</span>
        </div>
        </div>
      </header>

      {openSeat && (
        <SeatPlayerModal
          seat={openSeat}
          onClose={() => setOpenSeat(null)}
          onSeatChange={(next) => {
            setSeatEdits((prev) => ({ ...prev, [next.playerId]: next }));
            setOpenSeat(next);
          }}
        />
      )}

      {statsOpen && (
        <GameStatsModal
          factionStats={factionStats}
          factionData={factionData}
          turnOrder={turnOrder}
          ringsByFaction={ringsByFaction}
          onClose={() => setStatsOpen(false)}
        />
      )}

      {ringsOpen && ringsOfPower && (
        <RingsOfPowerModal
          rings={ringsOfPower.rings}
          territoryNames={ringsOfPower.territoryNames}
          heroNames={ringsOfPower.heroNames}
          onClose={() => setRingsOpen(false)}
        />
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
                <h3>What&apos;s Unique About Baggins & Allies?</h3>
                <p>Baggins & Allies has the same game mechanics, but with maps from Middle-earth, unique Lord of the Rings units per faction, and a few additional twists:</p>
                <ul className="help-icon-list">
                  <li>Combat uses <strong>10-sided dice.</strong></li>
                  <li>Neutral units like cave trolls and goblins spawn in unowned territories. They can only defend against attacks.</li>
                  <li>Unit specials modify combat. Reference the <strong>Specials</strong> button (★ SP) for details on unit abilities and combat modifiers.</li>
                  <li>Terrain types can impact combat. Mountains and rivers block ground movement. Bridges allow ground units to cross over a river. Aerial units can fly over mountains and rivers.</li>
                  <li>River zones are like sea zones but can be traversed by rowboats, not ships. Rowboats can be mobilized to any territory adjacent to your own, with a limit based on the adjacent power production.</li>
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
                  <li>
                    <span className="help-map-key-badge help-map-key-badge--power" aria-hidden>1</span>{' '}
                    <strong>Power Production</strong> — the amount of power generated per turn for its territory owner.
                  </li>
                  <li>
                    <span className="help-map-key-badge help-map-key-badge--sea" aria-hidden>1</span>{' '}
                    <strong>Sea Zone</strong> — labels a sea zone.
                  </li>
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
              {helpRules.length > 0 && (
                <section className="help-section">
                  <h3>Special Rules</h3>
                  {helpRules.map((rule) => (
                    <div key={rule.key} className="help-special-rule">
                      <h4>{rule.name}</h4>
                      {rule.description ? <p>{rule.description}</p> : null}
                    </div>
                  ))}
                </section>
              )}
              {helpSubfactions.length > 0 && (
                <section className="help-section">
                  <h3>Subfactions</h3>
                  <p>
                    A subfaction fights as part of a parent faction and does not take its own turn. The parent commands its units. Its territories keep the subfaction&apos;s colors on the map.
                  </p>
                  <p className="help-subfaction-note">
                    If the parent loses its capital, it no longer collects that subfaction&apos;s pooled power, gains its free recruits, or places its units.
                  </p>
                  {helpSubfactions.map((sub) => (
                    <div key={sub.id} className="help-subfaction">
                      <h4>
                        {sub.name} <span className="help-subfaction-parent">({sub.parentName})</span>
                      </h4>
                      <ul className="help-icon-list">
                        {sub.rules.map((rule) => (
                          <li key={rule}>{rule}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </section>
              )}
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
                      const descriptionText =
                        key === 'archer'
                          ? 'Defending archers pre-fire before round 1 of combat'
                          : def.description;
                      const unitList = unitsBySpecial[key] ?? [];
                      return (
                        <React.Fragment key={key}>
                          <dt className="specials-term">{termLabel}</dt>
                          <dd className="specials-desc">
                            {descriptionText}
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

      {unitStatsOpen && (
        <UnitStatsModal
          unitsByFaction={unitsByFaction}
          factionData={factionData}
          turnOrder={turnOrder}
          onClose={() => setUnitStatsOpen(false)}
        />
      )}
    </>
  );
}

export default Header;
