import { useState } from 'react';

/** Commit exact values once, while allowing unfinished decimal input during typing. */
export function LandscapeScaleInput({ value, onChange, label }: {
  value: number;
  onChange: (value: number) => void;
  label: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  return <input
    className="mapov-scale-number"
    aria-label={label}
    title="Scale multiplier (0.25–4)"
    type="number" min={0.25} max={4} step="any"
    value={draft ?? String(value)}
    onPointerDown={(event) => event.stopPropagation()}
    onClick={(event) => event.stopPropagation()}
    onChange={(event) => setDraft(event.target.value)}
    onBlur={(event) => {
      if (draft !== null && draft.trim() && event.currentTarget.validity.valid) {
        const next = Number(draft);
        if (Number.isFinite(next) && next !== value) onChange(next);
      }
      setDraft(null);
    }}
    onKeyDown={(event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        if (event.currentTarget.reportValidity()) event.currentTarget.blur();
      } else if (event.key === 'Escape') {
        event.stopPropagation();
        setDraft(null);
      }
    }}
  />;
}
