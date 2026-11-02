/**
 * Pick readable text color for a background hex.
 * Returns near-black ink for light backgrounds, white only when the color is
 * genuinely dark (WCAG relative luminance threshold L <= 0.179, i.e. white
 * yields at least as much contrast against the color as black does).
 */
export function contrastTextOn(hex: string): string {
  const ink = '#14110d';
  const trimmed = (hex || '').trim().replace(/^#/, '');
  const expanded =
    trimmed.length === 3
      ? trimmed.split('').map((c) => c + c).join('')
      : trimmed;
  if (!/^[0-9a-fA-F]{6}$/.test(expanded)) return ink;
  const r = parseInt(expanded.slice(0, 2), 16) / 255;
  const g = parseInt(expanded.slice(2, 4), 16) / 255;
  const b = parseInt(expanded.slice(4, 6), 16) / 255;
  const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const luminance = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  return luminance <= 0.179 ? '#ffffff' : ink;
}
