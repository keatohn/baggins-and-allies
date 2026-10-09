import { useState, type ChangeEvent, type FocusEvent, type InputHTMLAttributes } from 'react';

type NumberFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange' | 'inputMode'> & {
  value: string | number;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
};

/**
 * Numeric admin field that keeps the typed text while focused.
 * `type="number"` on a phone snaps an emptied field back to its minimum (often 1),
 * so the keypad is text with a numeric keyboard and the draft is local until blur.
 */
export function NumberField({ value, onChange, onFocus, onBlur, min: _min, max: _max, step: _step, ...rest }: NumberFieldProps) {
  const valueStr = value == null ? '' : String(value);
  const [draft, setDraft] = useState(valueStr);
  const [focused, setFocused] = useState(false);
  const [seen, setSeen] = useState(valueStr);
  if (!focused && seen !== valueStr) {
    setSeen(valueStr);
    setDraft(valueStr);
  }

  return (
    <input
      {...rest}
      type="text"
      inputMode="numeric"
      autoComplete="off"
      autoCorrect="off"
      spellCheck={false}
      enterKeyHint="done"
      value={focused ? draft : valueStr}
      onFocus={(event: FocusEvent<HTMLInputElement>) => {
        setFocused(true);
        setDraft(valueStr);
        setSeen(valueStr);
        onFocus?.(event);
      }}
      onChange={(event: ChangeEvent<HTMLInputElement>) => {
        const cleaned = event.target.value.replace(/[^\d-]/g, '').replace(/(?!^)-/g, '');
        setDraft(cleaned);
        if (event.target.value !== cleaned) event.target.value = cleaned;
        onChange(event);
      }}
      onBlur={(event: FocusEvent<HTMLInputElement>) => {
        setFocused(false);
        onBlur?.(event);
      }}
    />
  );
}
