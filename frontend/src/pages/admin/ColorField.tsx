function parseHexColor(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const s = value.trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return `#${s.slice(1).toLowerCase()}`;
  return null;
}

/** Swatch plus hex field used for faction colors in the setup editor. */
export function ColorField({
  value,
  onApply,
  ariaLabel = 'Faction color palette',
  disabled = false,
}: {
  value: unknown;
  onApply: (c: string) => void;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  const raw = typeof value === 'string' ? value : '';
  const hex = parseHexColor(value);
  return (
    <div className="admin-color-picker">
      <input
        type="color"
        className="admin-color-picker__swatch"
        value={hex ?? '#888888'}
        disabled={disabled}
        onChange={(e) => onApply(e.target.value.toLowerCase())}
        aria-label={ariaLabel}
      />
      <input
        type="text"
        className="admin-form__input admin-color-picker__hex"
        spellCheck={false}
        value={raw}
        disabled={disabled}
        onChange={(e) => onApply(e.target.value)}
      />
    </div>
  );
}
