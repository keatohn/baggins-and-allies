import { useState, type ChangeEvent, type FocusEvent } from 'react';
import type { ProgressionConfig, ProgressionFrame, ProgressionRules, ProgressionTier, TierGlow } from '../../services/api';
import { NumberField } from './NumberField';

const GLOWS: { id: TierGlow; label: string }[] = [
  { id: 'none', label: 'None' },
  { id: 'soft', label: 'Soft' },
  { id: 'pulse', label: 'Pulse' },
];

const INT_RULES: { key: keyof ProgressionRules; label: string }[] = [
  { key: 'game_completed', label: 'Finishing a game' },
  { key: 'game_won', label: 'Winning' },
  { key: 'stronghold_captured', label: 'Each stronghold captured' },
  { key: 'territory_captured', label: 'Each other territory captured' },
  { key: 'enemy_unit_destroyed', label: 'Each enemy unit destroyed' },
  { key: 'battle_won', label: 'Each battle won' },
  { key: 'stats_cap_per_game', label: 'Cap on stat XP per game' },
  { key: 'min_rounds_to_count', label: 'Minimum rounds for a game to count' },
  { key: 'forfeit_xp', label: 'XP for a player who forfeited' },
  { key: 'daily_cap', label: 'Daily XP cap (0 = no cap)' },
];

function hexOr(value: string, fallback: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : fallback;
}

/** Rank 1 is the lowest tier. Matches the order the server uses after a save. */
function rankedTiers(tiers: ProgressionTier[]): { tier: ProgressionTier; rank: number }[] {
  return [...tiers]
    .sort((a, b) => a.min_xp - b.min_xp || a.id.localeCompare(b.id))
    .map((tier, index) => ({ tier, rank: index + 1 }));
}

function newTier(tiers: ProgressionTier[]): ProgressionTier {
  const min = tiers.length ? Math.max(...tiers.map((t) => t.min_xp)) + 250 : 0;
  return {
    id: `tier_${Date.now().toString(36)}`,
    name: 'New tier',
    min_xp: min,
    color: '#8c6a3f',
    icon: '',
    description: '',
    visual: { frame_id: '', ring_color: '#8c6a3f', glow: 'none' },
  };
}

function newFrame(): ProgressionFrame {
  return {
    id: `frame_${Date.now().toString(36)}`,
    label: 'New frame',
    src: '/avatar-frames/frame.png',
  };
}

/** Decimal admin field. Keeps the typed text while focused so "0.5" can be entered. */
function DecimalField({
  value,
  onChange,
  ariaLabel,
}: {
  value: number;
  onChange: (next: number) => void;
  ariaLabel: string;
}) {
  const valueStr = String(value);
  const [draft, setDraft] = useState(valueStr);
  const [focused, setFocused] = useState(false);
  const [seen, setSeen] = useState(valueStr);
  if (!focused && seen !== valueStr) {
    setSeen(valueStr);
    setDraft(valueStr);
  }
  return (
    <input
      className="admin-form__input admin-form__input--narrow"
      inputMode="decimal"
      aria-label={ariaLabel}
      autoComplete="off"
      value={focused ? draft : valueStr}
      onFocus={(event: FocusEvent<HTMLInputElement>) => {
        setFocused(true);
        setDraft(valueStr);
        setSeen(valueStr);
        event.target.select();
      }}
      onBlur={() => setFocused(false)}
      onChange={(event: ChangeEvent<HTMLInputElement>) => {
        const cleaned = event.target.value.replace(/[^\d.]/g, '').replace(/(\..*)\./g, '$1');
        setDraft(cleaned);
        const n = Number(cleaned);
        if (cleaned !== '' && cleaned !== '.' && Number.isFinite(n)) onChange(n);
      }}
    />
  );
}

export function TierXpPanel({
  config,
  onChange,
  unitIcons,
}: {
  config: ProgressionConfig;
  onChange: (next: ProgressionConfig) => void;
  unitIcons: string[];
}) {
  const setTier = (id: string, patch: Partial<ProgressionTier>) => {
    onChange({
      ...config,
      tiers: config.tiers.map((t) => (t.id === id ? { ...t, ...patch } : t)),
    });
  };
  const setVisual = (id: string, patch: Partial<ProgressionTier['visual']>) => {
    onChange({
      ...config,
      tiers: config.tiers.map((t) => (t.id === id ? { ...t, visual: { ...t.visual, ...patch } } : t)),
    });
  };
  const setRule = (key: keyof ProgressionRules, value: number) => {
    onChange({ ...config, rules: { ...config.rules, [key]: value } });
  };
  const setFrame = (id: string, patch: Partial<ProgressionFrame>) => {
    onChange({
      ...config,
      frames: config.frames.map((f) => (f.id === id ? { ...f, ...patch } : f)),
    });
  };
  const ranked = rankedTiers(config.tiers);
  const rankById = new Map(ranked.map((row) => [row.tier.id, row.rank]));
  const lowestId = ranked[0]?.tier.id ?? '';
  const unlocks = config.icon_min_tier ?? {};
  const [iconQuery, setIconQuery] = useState('');
  const iconQueryNorm = iconQuery.trim().toLowerCase();
  const visibleIcons = unitIcons.filter((name) => name.toLowerCase().includes(iconQueryNorm));

  const setIconTier = (name: string, tierId: string) => {
    const next = { ...unlocks };
    if (!tierId || tierId === lowestId) delete next[name];
    else next[name] = tierId;
    onChange({ ...config, icon_min_tier: next });
  };

  return (
    <div className="admin-form admin-tier-form">
      <section>
        <h3 className="admin-form__subtitle">Tiers</h3>
        <p className="admin-form__micro">
          A player is in the highest tier whose minimum they have reached. The lowest tier must start at 0,
          and each threshold has to be higher than the one before it. Tier 1 is the lowest. Saving sorts
          tiers by that minimum. Raising a threshold can move players down. The ring color and glow wrap the profile circle.
          A frame is optional and only used when that image exists.
        </p>
        <label className="admin-form__label">
          XP label
          <input
            className="admin-form__input admin-form__input--narrow"
            value={config.currency_label}
            maxLength={24}
            onChange={(e) => onChange({ ...config, currency_label: e.target.value })}
          />
        </label>
        {config.tiers.map((tier) => (
          <div key={tier.id} className="admin-form__card admin-tier-card">
            <p className="admin-form__micro admin-tier-card__rank">Tier {rankById.get(tier.id) ?? ''}</p>
            <div className="admin-tier-card__line">
              <label className="admin-form__label">
                Name
                <input
                  className="admin-form__input"
                  value={tier.name}
                  maxLength={48}
                  onChange={(e) => setTier(tier.id, { name: e.target.value })}
                />
              </label>
              <label className="admin-form__label">
                Minimum XP
                <NumberField
                  className="admin-form__input admin-form__input--narrow"
                  aria-label={`Minimum XP for ${tier.name || 'tier'}`}
                  value={tier.min_xp}
                  onChange={(e) => setTier(tier.id, { min_xp: Number(e.target.value) || 0 })}
                />
              </label>
              <label className="admin-form__label">
                Badge
                <input
                  type="color"
                  className="admin-signal-color"
                  aria-label={`Badge color for ${tier.name || 'tier'}`}
                  value={hexOr(tier.color, '#8c6a3f')}
                  onChange={(e) => setTier(tier.id, { color: e.target.value })}
                />
              </label>
            </div>
            <div className="admin-tier-card__line">
              <label className="admin-form__label">
                Frame
                <select
                  className="admin-form__input"
                  value={tier.visual.frame_id}
                  onChange={(e) => setVisual(tier.id, { frame_id: e.target.value })}
                >
                  <option value="">None</option>
                  {config.frames.map((frame) => (
                    <option key={frame.id} value={frame.id}>
                      {frame.label || frame.id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="admin-form__label">
                Ring
                <input
                  type="color"
                  className="admin-signal-color"
                  aria-label={`Ring color for ${tier.name || 'tier'}`}
                  value={hexOr(tier.visual.ring_color, '#8c6a3f')}
                  onChange={(e) => setVisual(tier.id, { ring_color: e.target.value })}
                />
              </label>
              <label className="admin-form__label">
                Glow
                <select
                  className="admin-form__input"
                  value={tier.visual.glow}
                  onChange={(e) => setVisual(tier.id, { glow: e.target.value as TierGlow })}
                >
                  {GLOWS.map((glow) => (
                    <option key={glow.id} value={glow.id}>
                      {glow.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="admin-form__label">
              Description
              <input
                className="admin-form__input"
                value={tier.description}
                maxLength={160}
                onChange={(e) => setTier(tier.id, { description: e.target.value })}
              />
            </label>
            <button
              type="button"
              className="admin-page__btn admin-page__btn--danger"
              onClick={() => {
                const nextUnlocks = { ...(config.icon_min_tier ?? {}) };
                for (const [icon, tierId] of Object.entries(nextUnlocks)) {
                  if (tierId === tier.id) delete nextUnlocks[icon];
                }
                onChange({
                  ...config,
                  tiers: config.tiers.filter((t) => t.id !== tier.id),
                  icon_min_tier: nextUnlocks,
                });
              }}
              disabled={config.tiers.length <= 1}
            >
              Remove tier
            </button>
          </div>
        ))}
        <button
          type="button"
          className="admin-page__btn"
          onClick={() => onChange({ ...config, tiers: [...config.tiers, newTier(config.tiers)] })}
          disabled={config.tiers.length >= 30}
        >
          Add tier
        </button>
      </section>

      <section>
        <h3 className="admin-form__subtitle">Frames</h3>
        <p className="admin-form__micro">
          Circle frames for the profile image. Point each one at a file under /avatar-frames/ (for example
          /avatar-frames/bronze_ring.png) or an https URL. No circle frames are in the game yet, so this
          list starts empty. Removing a frame clears it from any tier that used it.
        </p>
        {config.frames.length === 0 ? <p className="admin-form__micro">No frames yet.</p> : null}
        {config.frames.map((frame) => {
          const showPreview = frame.src.startsWith('/avatar-frames/') || frame.src.startsWith('https://');
          return (
            <div key={frame.id} className="admin-form__card admin-tier-card">
              <div className="admin-tier-card__line">
                {showPreview ? (
                  <img className="admin-tier-frame-preview" src={frame.src} alt="" />
                ) : null}
                <label className="admin-form__label">
                  Label
                  <input
                    className="admin-form__input"
                    value={frame.label}
                    maxLength={48}
                    onChange={(e) => setFrame(frame.id, { label: e.target.value })}
                  />
                </label>
                <label className="admin-form__label admin-tier-frame-src">
                  Image path
                  <input
                    className="admin-form__input"
                    value={frame.src}
                    maxLength={200}
                    spellCheck={false}
                    onChange={(e) => setFrame(frame.id, { src: e.target.value })}
                  />
                </label>
              </div>
              <button
                type="button"
                className="admin-page__btn admin-page__btn--danger"
                onClick={() =>
                  onChange({
                    ...config,
                    frames: config.frames.filter((f) => f.id !== frame.id),
                    tiers: config.tiers.map((t) =>
                      t.visual.frame_id === frame.id ? { ...t, visual: { ...t.visual, frame_id: '' } } : t,
                    ),
                  })
                }
              >
                Remove frame
              </button>
            </div>
          );
        })}
        <button
          type="button"
          className="admin-page__btn"
          onClick={() => onChange({ ...config, frames: [...config.frames, newFrame()] })}
          disabled={config.frames.length >= 40}
        >
          Add frame
        </button>
      </section>

      <section>
        <h3 className="admin-form__subtitle">Profile icons</h3>
        <p className="admin-form__micro">
          Icons are grouped by the lowest tier that can use them. Tier 1 is at the top, and each step down
          is a higher tier. The arrows move an icon into the group above or below. Players only see icons
          at or below their own tier. Icons that unlock at the player’s current tier, other than tier 1,
          get a mark in the picker.
        </p>
        <label className="admin-form__label">
          Find an icon
          <input
            className="admin-form__input"
            value={iconQuery}
            placeholder="archer, hobbit…"
            onChange={(e) => setIconQuery(e.target.value)}
          />
        </label>
        {unitIcons.length === 0 ? <p className="admin-form__micro">Loading icons…</p> : null}
        {unitIcons.length > 0 && visibleIcons.length === 0 ? (
          <p className="admin-form__micro">No icons match.</p>
        ) : null}
        <div className="admin-icon-unlocks">
          {ranked.map(({ tier, rank }, tierIndex) => {
            const icons = visibleIcons
              .filter((name) => {
                const assigned = unlocks[name] && rankById.has(unlocks[name]) ? unlocks[name] : lowestId;
                return assigned === tier.id;
              })
              .sort((a, b) => a.localeCompare(b));
            if (iconQueryNorm && icons.length === 0) return null;
            return (
              <section key={tier.id} className="admin-icon-group" aria-label={`Tier ${rank}`}>
                <h4 className="admin-icon-group__title">
                  Tier {rank}
                  {tier.name ? ` · ${tier.name}` : ''}
                </h4>
                {icons.length === 0 ? <p className="admin-form__micro">No icons</p> : null}
                {icons.map((name) => {
                  const label = name.replace(/\.png$/i, '').replace(/_/g, ' ');
                  return (
                    <div key={name} className="admin-icon-unlock">
                      <img src={`/assets/units/${name}`} alt="" />
                      <span>{label}</span>
                      <span className="admin-icon-unlock__arrows">
                        <button
                          type="button"
                          className="admin-icon-unlock__arrow"
                          aria-label={`Move ${label} up`}
                          disabled={tierIndex === 0}
                          onClick={() => {
                            const above = ranked[tierIndex - 1];
                            if (above) setIconTier(name, above.tier.id);
                          }}
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="admin-icon-unlock__arrow"
                          aria-label={`Move ${label} down`}
                          disabled={tierIndex === ranked.length - 1}
                          onClick={() => {
                            const below = ranked[tierIndex + 1];
                            if (below) setIconTier(name, below.tier.id);
                          }}
                        >
                          ↓
                        </button>
                      </span>
                    </div>
                  );
                })}
              </section>
            );
          })}
        </div>
      </section>

      <section>
        <h3 className="admin-form__subtitle">XP rules</h3>
        <p className="admin-form__micro">
          XP is awarded once, when a game finishes. Stat XP is added up, cut off at the per-game cap, then
          multiplied. A game counts as against the computer when only one human is playing. Rounds use the
          game’s round counter: it starts at 1 and goes up after every faction has taken a turn. Forfeiting
          players get only the forfeit amount. The daily cap uses the day the game finished, and 0 turns it off.
        </p>
        <div className="admin-tier-rules">
          {INT_RULES.map((rule) => (
            <label key={rule.key} className="admin-form__label">
              {rule.label}
              <NumberField
                className="admin-form__input admin-form__input--narrow"
                aria-label={rule.label}
                value={config.rules[rule.key]}
                onChange={(e) => setRule(rule.key, Number(e.target.value) || 0)}
              />
            </label>
          ))}
          <label className="admin-form__label">
            Multiplier against other people
            <DecimalField
              ariaLabel="Multiplier against other people"
              value={config.rules.multiplayer_multiplier}
              onChange={(n) => setRule('multiplayer_multiplier', n)}
            />
          </label>
          <label className="admin-form__label">
            Multiplier against the computer
            <DecimalField
              ariaLabel="Multiplier against the computer"
              value={config.rules.vs_ai_multiplier}
              onChange={(n) => setRule('vs_ai_multiplier', n)}
            />
          </label>
        </div>
      </section>
    </div>
  );
}
