# Issue #92 — Sea raid must wait for forced navy battle (scope B + any enemy ship)

## Objective
Fix GitHub issue #92: a sea raid declared into a hostile sea zone must force the naval battle first. Approved scope (keatohn): **B — ordering + deferred offload**, hostile = **any enemy ship (factionless included)** via `_sea_zone_has_hostile_enemy_boats` (`backend/engine/movement.py:319`).

Raiders stay embarked during the forced navy battle; boat sinking kills them via the existing passenger-elimination path (`_remove_casualties`, `reducer.py:4372`); only survivors disembark and raid. Backend rejects raid initiation; frontend grays out the raid until the staging zone's naval battle is resolved.

Branch: `opencode/issue92-20261104221819`.

## Design decisions
1. **Block predicate**: staging sea zone is hostile for the staging faction per `_sea_zone_has_hostile_enemy_boats` (any different-alliance unit; factionless `uf is None` → hostile). Softlock guard: only *enforce* the block when the zone would actually list a naval battle for the attacker (at least one non-embarked enemy unit with `participates_in_sea_hex_naval_combat`); factionless-only zones have no listable defenders today (`queries.py:2698` requires `unit_faction is not None`). Verified 0 factionless units in setups — guard is defensive only.
2. **Deferred offload**: at combat_move apply, if `from_id` is a hostile staging zone, record staging (tsrf / faction / passenger ids) but do **not** move passengers ashore, do **not** clear `loaded_onto`, do **not** set `pending_captures`. Still mark boats as having attacked (clears `loaded_naval_must_attack`). Movement cost for the skipped land hop is not charged (cosmetic in combat phase).
3. **Listed blocked raid**: while staging is deferred and the land has enemy defenders, `get_contested_territories` synthesizes a raid entry (`attacker_unit_ids` = staged passengers still in the sea zone, `sea_zone_id` set, `defender_unit_ids` = land enemies) so the existing frontend graying (`isSeaRaidBlockedByPendingNaval`, `Sidebar.tsx:629`) works with no frontend logic change. Empty-land raids stay unlisted (no battle); capture is deferred instead.
4. **Naval outcomes**:
   - **Win** → post-naval-win offload of surviving staged passengers onto each staged land (`territory_sea_raid_passenger_instance_ids`), clear staging for that sea zone; empty land gets `pending_captures`.
   - **Loss** (`attackers_eliminated`) → existing `_purge_sea_raid_staging_after_lost_naval` + passenger death with boats.
   - **Retreat** → boats+passengers already travel with surviving boats; clear all tsrf entries whose **value** == retreated sea zone (fixes no-op `pop(combat.territory_id)` at `reducer.py:4321`).
5. **AI**: both decide paths skip entries whose initiate would be rejected (blocked raids), including the `random.choice` fallback.
6. **Issue #50** does not mention this bug — proceed with #92.

## Implementation steps

### 1. Backend — deferred offload (`backend/engine/reducer.py`, `_apply_pending_moves` ~1946–2222)
- In the combat_move passenger-offload branch (after staging records at 1959–1966): compute `defer_offload = phase == "combat_move" and from_sea and _sea_zone_has_hostile_enemy_boats(state, from_id, faction_id, ...)` with the softlock guard above.
- If `defer_offload`: skip the physical move loop for those passengers (2039–2051), skip `loaded_onto` clear (2117–2121), skip `force_can_conquer`/`pending_captures` (2171–2206). Keep staging records, boat-attacked clearing (2145–2167), and event emission.
- Do not defer aerial offloads (`all_movers_aerial`) or non-combat_move phases.

### 2. Backend — synthesized blocked raid entry (`backend/engine/queries.py`, `get_contested_territories` ~2658)
- After the normal land loop: for each `(land_tid, sea_tid)` in `territory_sea_raid_from` where faction matches and staging is still deferred/hostile, and land has enemy defenders and no normal entry was produced (attackers not on land): append entry with `attacker_unit_ids` = staged passengers still alive in `sea_tid`, `defender_unit_ids` = land enemies, `sea_zone_id = sea_tid`.
- Helper: shared “is staging zone still hostile / block raid?” predicate (reuse in validate + AI).

### 3. Backend — reject initiation (`backend/engine/queries.py`, `_validate_initiate_combat` sea-raid branch ~1394)
- After resolving `sea_zone_id`, if staging zone is hostile-with-listable-naval → `ValidationResult(False, "... naval battle in {sea} must be resolved before sea raid ...")`.

### 4. Backend — naval outcome handling (`backend/engine/reducer.py`)
- `_resolve_combat_end` (~4754–4771): when `is_sea_combat` and attackers **won** (not `attackers_eliminated`), run new `_offload_sea_raid_survivors_after_naval_win(state, sea_territory_id, ...)` before the existing else-pop: move surviving staged passengers from sea to each staged land, clear `loaded_onto`, set `pending_captures` for empty land, clear tsrf/faction/passenger entries for that sea zone. Loss path unchanged (purge).
- `_handle_retreat` (~4318–4324): replace `pop(combat.territory_id)` with filter `v != combat.territory_id` on tsrf; drop matching faction + passenger-map keys.

### 5. Backend — AI skip (`backend/ai/combat.py`)
- `decide_initiate_combat` (~143): drop entries failing the block predicate before scoring; fallback `random.choice` only over remaining valid entries (return `None` / `end_phase` if none — phase-end already blocked while contested).
- `decide_mcts_combat_policy` / `_declared_battles` (~407–458): same filter.
- `backend/ai/mcts/combat.py` `legal_actions` (~91): rely on `validate_action` rejecting blocked raids (already validates each initiate) — confirm no fallback picks a rejected entry.

### 6. Frontend
- No logic change required: existing `isSeaRaidBlockedByPendingNaval` grays raids whose `sea_zone_id` matches a listed naval battle. Verify with a quick manual/`npm run build` pass; only touch `Sidebar.tsx` if synthesized entries need a display tweak.

### 7. Tests (`test/test_sea_transport_combat.py`)
- Deferred offload: apply combat_move sail+offload into hostile zone → passengers remain embarked (`loaded_onto` set), still in sea territory, no `pending_captures`, staging records set.
- `get_contested_territories` lists naval battle + synthesized raid with `sea_zone_id`; raid initiation rejected by `_validate_initiate_combat`.
- Naval win → survivors disembark onto land; empty land captured via `pending_captures`; staging cleared.
- Naval loss → passengers dead with boats; staging purged (existing tests still pass).
- Naval retreat → tsrf entries for that sea zone cleared (regression for no-op pop).
- Unhostile staging zone → offload still immediate (no behavior change).

## Verification
1. `.venv/bin/pytest test/test_sea_transport_combat.py` then full `.venv/bin/pytest test/`
2. `npm run lint` and `npm run build`
3. Review diff for unrelated changes

## Relevant files
- `backend/engine/reducer.py` — `_apply_pending_moves`, `_resolve_combat_end`, `_handle_retreat`, `_purge_sea_raid_staging_after_lost_naval`, `_remove_casualties`, `_sea_raid_attacker_units_from_board`
- `backend/engine/queries.py` — `get_contested_territories`, `_validate_initiate_combat`, `participates_in_sea_hex_naval_combat`
- `backend/engine/movement.py` — `_sea_zone_has_hostile_enemy_boats`
- `backend/ai/combat.py`, `backend/ai/mcts/combat.py`
- `frontend/src/components/Sidebar.tsx` — existing graying
- `test/test_sea_transport_combat.py`
- `docs/SEA_ZONES_AND_TRANSPORT.md` — update if behavior description changes
