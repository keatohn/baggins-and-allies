import type { SignalAppliesTo, SignalPreset } from '../../territorySignals';

const APPLIES: { id: SignalAppliesTo; label: string }[] = [
  { id: 'enemy', label: 'Enemy territories' },
  { id: 'allied', label: 'Allied territories' },
  { id: 'neutral', label: 'Neutral territories' },
  { id: 'any', label: 'Any territory' },
];

const ICON_CHOICES = ['⚔️', '🛡️', '⚑', '👁️', '⚠️', '⭐', '🔥', '🏹'];

function newPreset(): SignalPreset {
  return {
    id: `signal_${Date.now().toString(36)}`,
    label: 'New signal',
    icon: '⚑',
    applies_to: 'any',
    color: '#6b5b4b',
  };
}

export function SignalsPanel({
  presets,
  onChange,
}: {
  presets: SignalPreset[];
  onChange: (next: SignalPreset[]) => void;
}) {
  const update = (id: string, patch: Partial<SignalPreset>) => {
    onChange(presets.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  };

  return (
    <div className="admin-form">
      <p className="admin-form__micro">
        Preset callouts players can pin on a territory. Only factions on the same alliance see a pin.
        Enemy, allied, and neutral signals show up only on matching territories. This catalog is shared
        by every setup. The flag on the map uses this signal’s color. Pins already on a map keep the
        label, icon, and flag color they were placed with.
      </p>
      <div className="admin-signal-list">
        {presets.map((preset) => (
          <div key={preset.id} className="admin-signal-row">
            <div className="admin-signal-icon-line">
              <label className="admin-form__label">
                Icon
                <input
                  className="admin-form__input admin-signal-icon-input"
                  value={preset.icon}
                  maxLength={16}
                  aria-label={`Icon for ${preset.label || 'signal'}`}
                  onChange={(e) => update(preset.id, { icon: e.target.value })}
                />
              </label>
              <div className="admin-signal-icon-choices" role="group" aria-label={`Suggested icons for ${preset.label || 'signal'}`}>
                {ICON_CHOICES.map((icon) => (
                  <button
                    key={icon}
                    type="button"
                    className={`admin-signal-icon-choice${preset.icon === icon ? ' admin-signal-icon-choice--active' : ''}`}
                    onClick={() => update(preset.id, { icon })}
                  >
                    {icon}
                  </button>
                ))}
              </div>
            </div>
            <div className="admin-signal-message-line">
              <label className="admin-form__label admin-signal-label">
                Message
                <input
                  className="admin-form__input"
                  value={preset.label}
                  maxLength={48}
                  onChange={(e) => update(preset.id, { label: e.target.value })}
                />
              </label>
              <label className="admin-form__label">
                Flag
                <input
                  type="color"
                  className="admin-signal-color"
                  value={/^#[0-9a-fA-F]{6}$/.test(preset.color) ? preset.color : '#6b5b4b'}
                  aria-label={`Flag color for ${preset.label || 'signal'}`}
                  onChange={(e) => update(preset.id, { color: e.target.value })}
                />
              </label>
            </div>
            <label className="admin-form__label">
              Shows on
              <select
                className="admin-form__input"
                value={preset.applies_to}
                onChange={(e) => update(preset.id, { applies_to: e.target.value as SignalAppliesTo })}
              >
                {APPLIES.map((opt) => (
                  <option key={opt.id} value={opt.id}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="admin-page__btn admin-page__btn--danger admin-signal-remove"
              onClick={() => onChange(presets.filter((p) => p.id !== preset.id))}
            >
              Remove
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="admin-page__btn"
        onClick={() => onChange([...presets, newPreset()])}
        disabled={presets.length >= 24}
      >
        Add signal
      </button>
    </div>
  );
}
