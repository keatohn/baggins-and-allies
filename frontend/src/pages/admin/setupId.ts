/** Mirrors backend.setup_ids.SETUP_ID_RE */
export const SETUP_ID_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,126}$/;

export function isValidSetupId(id: string): boolean {
  return SETUP_ID_PATTERN.test(id.trim());
}
