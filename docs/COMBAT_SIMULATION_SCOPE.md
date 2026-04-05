# Combat Simulation Engine — Scope

## Goal

A **simulation engine** that runs many combat trials with the **same rules** as real combat (dice, casualties, prefires, terrain, must_conquer, etc.) and returns **aggregate statistics**: P(attacker wins), P(conquer), casualty distributions per side, round distribution. No changes to actual combat code; the sim **calls** existing combat resolution.

**Future frontend**: Right sidebar (e.g. under "Select Territory") where the user can input attacker/defender unit counts and see simulation stats. Scoped here but not required for the first backend-only slice.

---

## What the Real Combat Flow Does (Sim Must Mirror)

1. **Setup**: Attacker units, defender units, territory (for terrain, stronghold, defender casualty order), optional sea_zone_id (sea raid).
2. **Prefires** (order matters):
   - **Stealth prefire**: If *all* attackers have stealth → attackers roll at attack−1, hits apply to defenders only. Can end combat before round 1.
   - **Archer prefire**: If *any* defender is archer → defender archers roll (defense−1, or +0 if stronghold/fortress), hits apply to attackers only. Can end combat before round 1.
3. **Round loop**: Each round:
   - Generate dice (per unit, from `generate_combat_rolls_for_units`).
   - Round 1 only: **Terror** can force defender re-rolls (up to cap); sim needs to model this with random re-rolls per trial.
   - **Bombikazi**: Paired bombikazi+bomb get effective dice and self-destruct after round; `get_attacker_effective_dice_and_bombikazi_self_destruct` already handles this.
   - **Terrain / anti-cavalry / captain**: Modifiers from `compute_terrain_stat_modifiers`, `compute_anti_cavalry_stat_modifiers`, and captain logic (reducer).
   - `resolve_combat_round(...)` with casualty_order_attacker, casualty_order_defender, must_conquer, is_naval_combat_*.
   - Remove casualties; if one side eliminated → end.
4. **Outcome**: Attacker wins = defenders eliminated and at least one attacker alive; **conquer** = attacker wins and at least one surviving attacker is a land unit and territory is ownable.

Existing entry points the sim will **reuse** (no duplication of rules):

- `combat.resolve_combat_round`, `resolve_archer_prefire`, `resolve_stealth_prefire`
- `utils.generate_combat_rolls_for_units`
- `combat.compute_terrain_stat_modifiers`, `compute_anti_cavalry_stat_modifiers`
- `combat.get_attacker_effective_dice_and_bombikazi_self_destruct`
- Definitions: `unit_defs`, `territory_defs` from setup (same as game)

---

## Inputs (Sim Scenario)

| Input | Description |
|-------|-------------|
| `setup_id` | Which game setup (unit_defs, territory_defs). |
| `attacker_stacks` | `[{ "unit_id": string, "count": int }, ...]` (or `Record<unit_id, count>`). |
| `defender_stacks` | Same shape. |
| `territory_id` | For terrain, stronghold, defender casualty order lookup. |
| `casualty_order_attacker` | `"best_unit"` \| `"best_attack"`. |
| `casualty_order_defender` | `"best_unit"` \| `"best_defense"`. |
| `must_conquer` | bool. |
| `options` (optional) | e.g. `max_rounds`, `is_sea_raid`, `retreat_when_attacker_units_le` (int \| None: retreat when attacker count ≤ N after a round). |

Prefires and terror are **derived** from unit_defs (archer tag, stealth tag, terror tag) so the sim doesn’t need separate flags; it uses the same logic as the reducer.

---

## Outputs (Per Trial and Aggregated)

**Per trial:**

- `winner`: `"attacker"` \| `"defender"`
- `retreat`: bool (true if ended by `retreat_when_attacker_units_le` threshold)
- `conquered`: bool (attacker won and had a living ground unit and territory ownable)
- `rounds`: int
- `attacker_casualties`: e.g. `{ unit_id: count }`
- `defender_casualties`: same

**Aggregated (e.g. over 1k–10k trials):**

- P(attacker wins), P(defender wins)
- P(conquer)
- Mean / median / percentiles (e.g. 90th) for attacker and defender casualties **by unit_id**
- Round distribution (mean, histogram or percentiles)
- Optional: variance, confidence intervals

---

## Implementation Phases

### Phase 1: Backend simulation module (core)

**Effort: small–medium (roughly 1–2 days)**

- **New module**: `backend/engine/combat_sim.py` (or `backend/simulation/combat_sim.py`).
- **Helpers**:
  - Build lists of `Unit` from stacks (synthetic `instance_id`s, e.g. `att_0`, `att_1`, `def_0`; full health, no `loaded_onto` unless we ever sim naval with passengers).
  - Load `unit_defs` and `territory_defs` by `setup_id` (reuse existing loader from definitions.py / API).
- **Single battle**: `run_one_battle(attacker_stacks, defender_stacks, territory_id, setup_id, options) -> BattleOutcome`.
  - Build attacker/defender `Unit` lists (copies).
  - Determine prefires from unit_defs (all attackers stealth → stealth prefire; any defender archer → archer prefire).
  - Run stealth prefire if applicable (generate rolls, `resolve_stealth_prefire`, apply casualties); if defenders eliminated → return.
  - Run archer prefire if applicable (generate rolls, `resolve_archer_prefire`, apply casualties); if attackers eliminated → return.
  - Loop: generate rolls (round 1: apply terror re-rolls in sim), get terrain/anticav/captain modifiers, get bombikazi effective_dice + self_destruct, `resolve_combat_round`; apply casualties; break when one side eliminated or `max_rounds` hit.
  - Compute winner and conquered (same rules as `_resolve_combat_end`: ground unit + ownable).
- **Monte Carlo**: `run_simulation(..., n_trials=1000, seed=None) -> SimResult`.
  - Run `run_one_battle` n_trials times (different RNG each time; optional seed for reproducibility).
  - Aggregate: win rates, conquer rate, casualty stats per unit type, round stats.

**Terror**: For round 1, simulate re-rolls (e.g. up to terror_cap defender dice that hit get new rolls); use same RNG as the rest of the trial.

**No game state**: Sim never touches `GameState` or the database; it only uses definitions and pure combat functions.

---

### Phase 2: API endpoint

**Effort: small (roughly 0.5 day)**

- **Endpoint**: e.g. `POST /simulate-combat` or `POST /games/{game_id}/simulate-combat` (game_id optional; if provided, can use game’s setup_id and optionally pre-fill from current combat).
- **Request body**: scenario (attacker_stacks, defender_stacks, territory_id, casualty orders, must_conquer, options), `n_trials` (default 1000), optional `setup_id` (or from game).
- **Response**: JSON with SimResult (win rates, P(conquer), casualty aggregates, round stats).
- **Auth**: Can be unauthenticated for dev or require same as rest of API; no state mutation.

---

### Phase 3: Frontend — minimal (later)

**Effort: medium (roughly 1–2 days)**

- **Placement**: Right sidebar, e.g. collapsible section “Combat Simulator” below “Select Territory”.
- **Initial version** (no per-unit inputs yet):
  - If there is an **active combat**, pre-fill attacker/defender from current combat; user clicks “Simulate” and sees stats.
  - Or: user selects a territory (defender side could be current units there), attacker side from another source or manual (future).
- **UI**: Button “Run simulation (1k trials)”, loading state, then display: P(attacker wins), P(conquer), mean rounds, mean casualties (e.g. by unit type or total). Simple table or cards.

---

### Phase 4: Frontend — full (future state)

**Effort: medium (roughly 1–2 days)**

- **Inputs**: Grid or list for attacker and defender: for each unit type, input count (and optionally preset “current combat” / “selected territory” to fill from game state).
- **Options**: Territory dropdown (for terrain/stronghold), casualty order toggles, must_conquer checkbox, n_trials (e.g. 1k / 5k / 10k).
- **Output**: Same as Phase 3 plus: casualty distribution per unit type (e.g. mean ± std or percentiles), round distribution (histogram or table). Optional export (e.g. CSV).

---

## Risks and edge cases

- **Retreat**: Optional input `retreat_when_attacker_units_left: int | None`. If set, after each round the sim checks whether the attacker's remaining unit count is ≤ that value; if so, the battle ends as **retreat** (defender holds, no conquest). If unset, sim runs to elimination (or max_rounds) with no retreat. No “retreat” outcome unless we add a separate rule (e.g. “if rounds > N, count as defender win”).
- **Terror**: Must implement round-1 re-roll logic in sim (mirror reducer/combat terror handling) so probabilities match.
- **Naval / sea raid**: Use same `is_naval_combat_attacker` / `is_naval_combat_defender` as reducer (from territory and sea_zone_id); sim already has territory_id and optional is_sea_raid in options.
- **Captain / anti-cavalry**: Recomputed each round in reducer; sim should call the same modifier helpers each round so results stay aligned with real combat.

---

## Summary: how much work?

| Phase | Scope | Effort (rough) |
|-------|--------|-----------------|
| **1. Backend sim** | `combat_sim.py`: build units from stacks, run prefires + round loop using existing combat/utils, aggregate over n_trials | **1–2 days** |
| **2. API** | Single POST endpoint, request/response JSON, no state | **~0.5 day** |
| **3. Frontend minimal** | Sidebar section, “Simulate” from current combat (or selected territory), show win/conquer/rounds/casualties | **1–2 days** |
| **4. Frontend full** | Per-unit count inputs, territory/options, full stats + distributions | **1–2 days** |

**Total for “backend + minimal UI that uses current combat”**: ~2.5–4.5 days.  
**Total for “full UI with custom stacks and full stats”**: ~4–7 days.

The heavy part is Phase 1 (wiring prefires, terror, modifiers, and bombikazi into a single `run_one_battle` that matches reducer behavior); Phases 2–4 are straightforward once Phase 1 is correct.
