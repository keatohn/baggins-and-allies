# Deployment checklist

## Before release: remove Skip Turn button only

The **Skip Turn** button is for development only (to quickly advance through factions when testing). Remove only the UI before deployment:

- **Frontend:** Remove the "SKIP TURN" button, `onSkipTurn` prop, `handleSkipTurn`, and `.skip-turn-btn-temp` styles. You may remove the `api.skipTurn` client method if no longer used, or leave it (the endpoint is still used by the forfeit flow).

**Do not remove the backend:** The `POST /games/{game_id}/skip-turn` endpoint (and the `skip_turn` action, `_handle_skip_turn`, and phase/validation entries) must stay—they are used when a player forfeits on their turn to advance to the next faction.

**Do not remove:** Factions with **no capital and 0 units** must still be skipped automatically. That behavior lives in `_handle_end_turn` in `backend/engine/reducer.py`: when the turn advances, a `while` loop applies pending income and then skips any faction that does not own its capital and has zero units (emitting `turn_skipped` and advancing to the next faction). That logic is the real “skip turn” and must remain.
