# M1 fix — split into parts; Part 1 drafted, verification pending

## Context
Issue: Bug Audit — Combat, finding **M1**: prefire rounds hardcode `casualty_order="best_unit"`.
M1 splits into two independent parts:

- **Part 1 — stealth prefire ignores `state.territory_defender_casualty_order`** (the real mismatch; stealth victims are *defenders*, so they must follow the territory's configured order, default `best_defense` for strongholds/capitals/camps/ports per `state.py:796`). ← do this first
- **Part 2 — archer prefire ignores attacker `casualty_order`** (minor; victim is the *attacker*; `combat.casualty_order_attacker` set from the round-0 continue payload at `reducer.py:3752` before the prefire branch, but prefire hardcodes `best_unit`). Follow-up PR.

## Part 1 changes (already drafted in working tree)

1. `backend/engine/combat.py` — `resolve_stealth_prefire` gains keyword-only `casualty_order_defender: str = "best_unit"`, forwarded to `_apply_hits` (was hardcoded).
2. `backend/engine/reducer.py` — in `_handle_initiate_combat`, compute `defender_casualty_order` *before* the stealth branch (moved up from line ~2934) and pass it to `resolve_stealth_prefire`; removed the now-duplicate later assignment.
3. `backend/engine/combat_sim.py` — sim stealth prefire passes `defender_casualty_order` (from `opts.casualty_order_defender`), matching live combat.
4. `test/test_stealth_prefire_casualty_order.py` (new) — 4 tests:
   - direct: default order loses cheap-first (`guardx` dies); `best_defense` loses low-defense-first (`tankx` dies);
   - integration via `initiate_combat` on `gap_of_rohan` with synthetic unit defs: territory order `best_defense` → low-defense defender removed; `best_unit` → cheap defender removed.

No API/frontend changes: defender order is state, not payload; roll generation unaffected.

## Remaining steps

1. Run `.venv/bin/pytest test/test_stealth_prefire_casualty_order.py`, then the full suite `.venv/bin/pytest test/`; fix any failures caused by the change.
2. Review diff for unrelated changes.
3. Commit on the feature branch, push, open PR linked to the issue (automatic via opencode infrastructure).
4. Later (separate PR): Part 2 — pass `casualty_order=combat.casualty_order_attacker` at the continue-side `resolve_archer_prefire` call (`reducer.py:3993`) and `opts.casualty_order_attacker` in `combat_sim.py`.
