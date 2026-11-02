import { useState } from 'react';
import { COLOR_PALETTE } from './avatarColors';
import './AvatarColor.css';

export default function AvatarColorPicker({
  color,
  onChange,
  label = 'Background color',
  disabled = false,
}: {
  color: string;
  onChange: (color: string) => void;
  label?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="profile-color">
      <button
        type="button"
        className="profile-color-trigger"
        style={{ ['--swatch' as string]: color }}
        aria-expanded={open}
        aria-label="Choose background color"
        disabled={disabled}
        onClick={() => setOpen((isOpen) => !isOpen)}
      >
        <span className="profile-color-trigger__chip" aria-hidden />
        {label}
      </button>
      {open && (
        <div className="profile-color-grid" role="listbox" aria-label="Background colors">
          {COLOR_PALETTE.map((swatch) => {
            const selected = swatch.toLowerCase() === color.toLowerCase();
            return (
              <button
                key={swatch}
                type="button"
                role="option"
                aria-selected={selected}
                aria-label={swatch}
                className={`profile-color-swatch${selected ? ' profile-color-swatch--selected' : ''}`}
                style={{ ['--swatch' as string]: swatch }}
                onClick={() => {
                  onChange(swatch);
                  setOpen(false);
                }}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
