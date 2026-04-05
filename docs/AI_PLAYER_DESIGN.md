# AI Player – Design & Scope

## Goals

- **Single-player and multiplayer**: AI can fill one or more faction slots. In single-player, unclaimed factions (or explicitly marked factions) are AI. In multiplayer, empty slots can be filled by AI.
- **Rules-compliant**: AI only chooses from valid actions. All validation uses the existing engine (`validate_action`); the AI never bypasses rules.
- **Read-only engine**: The AI code does not modify the game engine. It reads state and definitions, proposes actions, and the existing API applies them via `apply_action` + `save_game`.
- **Goal-aware**: AI understands victory (stronghold count), power, and territory control. It works toward short-term objectives (conquer, defend strongholds, move toward enemy capitals). The **end goal is not “spend X% of power”**—it’s **buy the right units** so it can defend its territories and attack where it should next turn; that may leave 0P or 2P remaining.
- **Beginner-level**: No deep opening book or pro meta; solid fundamentals (right purchases, defend, push toward strongholds) are enough for a first version.

---

## How Board-Game AIs Are Usually Implemented

1. **Heuristic + action space**
   - **Action space**: Enumerate or generate valid actions (from engine/API). Filter by phase (e.g. only `purchase_units`, `end_phase` in purchase).
   - **Evaluation**: Score game state or state-after-action with a heuristic (e.g. stronghold count, power income, unit count, threat to own strongholds). No search tree.
   - **Choice**: Greedy pick (best single action) or softmax over scores. Fast, easy to tune with “habits” (e.g. bias toward spending).

2. **Minimax / MCTS**
   - Used when we need to look ahead (opponent responses, combat outcomes). Heavier and more complex.
   - **Not in v1**: We can add a shallow lookahead or combat sim later; first version is heuristic-only per phase.

3. **Scripted / finite-state**
   - Per phase: “in purchase do X; in combat_move do Y”. Easy to reason about and debug.
   - We use this: one policy per phase (purchase, combat_move, combat, non_combat_move, mobilization), each returning a single valid action or “end_phase” / “end_turn”.

4. **Constraints**
   - Always get valid actions from the same layer the human uses (e.g. `_build_available_actions` or equivalent). Propose an action → `validate_action` → if valid, `apply_action`. Never write custom rules in the AI that duplicate or bypass the engine.

---

## Architecture

### Backend

- **`backend/ai/`** – standalone package. Imports only from `backend.engine` and `backend.api` (or DB) in a read-only way: read state, read definitions, read available actions. It **returns** an `Action`; the **API** (or a dedicated runner) calls `validate_action`, `apply_action`, `save_game`.

- **Context**: The AI receives a **context** object: current `GameState`, unit/territory/faction/camp/port definitions, and the **available-actions** dict (same shape as the API’s `getAvailableActions`). No direct DB access inside the AI.

- **Policies (habits / tendencies)**:
  - **Purchase**: **Objective = buy the right units** to maximize the likelihood of defending own territories and attacking where it should next turn. **Constraint = available power and mobilization capacity.** Remaining power (0P, 2P, etc.) is not a target—it’s an outcome of “best basket for defend/attack.” Unit mix should be driven by defensive needs (e.g. reinforce threatened strongholds) and offensive needs (e.g. units that can push toward enemy strongholds). Optionally buy a camp when power and strategy justify it.
  - **Combat (attack decisions)**: Before committing to an attack, the AI runs the **combat sim** (e.g. ~100 trials per candidate battle) so it can estimate win probability. It should not attack when chances are very low unless necessary (e.g. last resort to prevent loss). Sim runs can be sent in the same call (e.g. 100 runs) so they run fast with some variability; the AI uses the resulting win rate (and optionally casualty expectations) to decide whether to attack, continue, or retreat.
  - **Defense**: In movement/placement, prefer reinforcing own strongholds and high-value territories when they are under threat (adjacent enemy units).
  - **Offense**: Prefer moving toward enemy strongholds and capitals; prefer conquering territory to gain power.
  - **Victory**: Score states by stronghold count (and optionally power, territory count). Prefer actions that improve these.

- **Executor**: `ai.decide(context) -> Action | None`:
  - Returns one action for the current phase (e.g. `purchase_units(...)` or `end_phase(...)`).
  - `None` can mean “no preference” (caller can choose `end_phase`); or we always return an explicit action including `end_phase` / `skip_turn` / `end_turn`.

- **API**:
  - **Game config**: A way to mark which factions are AI (e.g. `config["ai_factions"] = ["mordor", "isengard"]` or “unclaimed factions in single-player are AI”). The API uses this to decide when to allow `POST /games/{id}/ai-step`.
  - **`POST /games/{game_id}/ai-step`** (or `ai-turn`):
    - If game is over or current faction is not an AI faction → 4xx or no-op.
    - Else: load state + definitions + available actions, call `ai.decide(context)`, validate the returned action, apply it, save game, return new state (and optionally events). One action per request to keep latency and logic simple; the frontend can poll or call repeatedly until it’s the human’s turn again.
  - **Authentication**: Either no auth (internal only), or require the same auth as the game (e.g. any player in the game can trigger the AI step so the frontend can “run AI turn” when it’s the AI’s turn).

### Frontend

- **Single-player**: When creating a game, the user picks their faction; other factions are AI (or we have an explicit “vs AI” toggle and mark `ai_factions` in config).
- **Multiplayer**: Optional “fill empty slots with AI” when starting; those factions get `ai_factions`.
- **During game**: When `current_faction` is an AI faction and the client gets state (e.g. from polling):
  - Show “AI is thinking…” (or “Computer’s turn”).
  - Call `POST /games/{id}/ai-step` in a loop; after each response, **apply a short delay** before applying the next state update or before the next ai-step call so the human can **track what the AI is doing** (e.g. see each purchase, each move, each combat resolution). Delays are per action or per phase (e.g. 300–800 ms) and are a UX choice so the human can follow along rather than seeing the whole turn jump at once.
- **No “Watson”**: No chat or complex UI; just a label and possibly a short log of “AI purchased X”, “AI moved to Y”.

---

## Short-Term Objective Functions (Heuristics)

- **Victory**: `score = f(stronghold_count, victory_criteria)`. Prefer actions that increase our stronghold control or deny the enemy’s.
- **Power**: `power_per_turn` from faction_stats; prefer conquering territories that produce power.
- **Threat**: For each own stronghold/capital, count adjacent enemy units; prefer reinforcing or defending when threat is high.
- **Distance to enemy strongholds/capitals**: Prefer moves that shorten path to enemy strongholds/capitals (e.g. BFS distance).
- **Combat**: Run the **combat sim** (e.g. 100 trials) per candidate battle. The sim returns **win rate**, **attacker_casualty_cost_mean**, **defender_casualty_cost_mean** (and per-unit casualty means). Decisions should **balance expected gain vs expected cost** (see **Formulas to define** below)—no fixed “min win prob” or “necessary” constants; use formulas that combine gain and cost.

These can be combined into a single scalar per action (e.g. weighted sum) and the AI picks the action with the highest score, or we use a small set of rules (e.g. “if under threat at stronghold, prefer defend; else prefer attack toward stronghold”).

---

## Combat sim for AI attack decisions

- When the AI is deciding whether to attack (or continue/retreat), it calls the **existing combat sim** with the current attacker/defender stacks and terrain (same API as the human combat simulator).
- **Volume**: e.g. **~100 sim runs per candidate battle**; results are fast and retain variability.
- **Sim outputs** (from the engine): `attacker_casualty_cost_mean`, `defender_casualty_cost_mean` (mean power cost of casualties across trials), win rate (attacker wins %), and optionally per-unit casualty means. Use these in **gain-vs-cost formulas** (see **Formulas to define**), not target constants.

---

## Formulas to define (your input needed)

**Principle:** Prefer **formulas that balance variables** (e.g. expected gain vs expected cost, defense need vs offense need) over **target constants** (e.g. “min attack win prob”, “necessary”). The AI should compare options using the same formula and pick the best balance, rather than hard gates.

Below are the formulas we need to implement the AI. **Please fill in or adjust the right-hand sides and any coefficients** so we can code them as-is. If you prefer a different structure (e.g. ratio vs difference, or extra terms), rewrite the formula and we’ll use it.

**Sim engine outputs available:** For each candidate battle the sim returns (among other things):

- `win_rate` = P(attacker wins) over trials  
- `attacker_casualty_cost_mean` = mean power cost of attacker casualties across trials  
- `defender_casualty_cost_mean` = mean power cost of defender casualties across trials  

Use these in the battle formulas below.

---

### 1. Battle: expected gain from winning

**What we need:** A single number for “how much we gain if we win this battle” (territory, stronghold, capital, power, strategic value).

- **Proposed (you edit):**  
  `battle_gain_if_win(target) = ???`  
  e.g. `(is_stronghold ? A : 1) + (is_capital ? B : 0) + power_production * C + (denies_enemy_stronghold ? D : 0)` with A, B, C, D numbers you choose.
  A = 15
  B = 10
  C = 3
I don't know what D means in the context of Axis & Allies
---

### 2. Battle: expected cost of the battle

**What we need:** A single number for “expected cost to us” of fighting this battle, using sim outputs. Should combine our expected casualties (power cost) and the chance we lose (and any cost of losing).

- **Proposed (you edit):**  
  `battle_expected_cost(win_rate, attacker_casualty_cost_mean, defender_casualty_cost_mean) = ???`  
  e.g. `attacker_casualty_cost_mean + (1 - win_rate) * cost_if_we_lose` where `cost_if_we_lose` might be our committed units’ power cost, or a fixed penalty for losing the territory. Or a different combination (e.g. include defender cost as “value destroyed” to us).

  I'm confused by this formula. battle expected cost should just be a simple attacker_casualty_cost_mean - defender_casualty_cost_mean
---

### 3. Battle: net value of attacking

**What we need:** A single “net value” or “expected net” so we can compare battles and choose to attack when this is positive (or when it’s the best among options).

- **Proposed (you edit):**  
  `battle_net_value = ???`  
  e.g. `win_rate * battle_gain_if_win(target) - battle_expected_cost(...)`  
  or `win_rate * gain - (1 - win_rate) * loss - attacker_casualty_cost_mean`  
  or a risk-adjusted form (e.g. net / variance, or net * win_rate).  
  **Attack** when `battle_net_value > 0` (or when it’s the best among candidate battles). **Continue vs retreat** can use the same formula re-run after the round (updated stacks and sim outputs).

(win_rate*battle_gain_if_win) - battle_expected_cost
---

### 4. Purchase: defense need vs offense need (weights)

**What we need:** How to weight “defense value” vs “offense value” when scoring unit types and territories. These should be **variables** (e.g. from threat level, stronghold count), not fixed constants.

- **Proposed (you edit):**  
  `defense_weight(state, faction) = ???`  
  e.g. `f(our_strongholds_under_threat, total_threat_level)`  
  `offense_weight(state, faction) = ???`  
  e.g. `g(strongholds_we_need, distance_to_enemy_strongholds)`  
  Then e.g. `w_def = defense_weight(...)`, `w_off = offense_weight(...)`, and unit score = `w_def * defense_value_per_power(unit) + w_off * attack_value_per_power(unit)`.

defense need evaluate battle sims of potential attacks from enemy territories and weigh that with the cost of reinforcing that place (on a unit level)
vulnerability = max(win_rate) of the potential attack that sends all enemy units (of one faction) that can reach that territory
territory loss cost would be the same as the battle gain if win, but higher is bad as it would cost that to lose that territory that that AI owns
expected loss would be territory loss cost * vulnerability. so for example, if no attacks can be made against their capital, then vulnerability would be 0, so expected loss would be 0, and there's no need to protect it as there's no threat. but AI may still need to deploy units there for the reason below.
- AI should be looking to reinforcement areas to keep expected loss down. it can't always deploy to those territories directly tho, so it should calc the closest valid deployment territory to the place that it wants to reduce the expected loss on.
- AI should evaluate which units would be the best to purchase for both next turn's attacks and for next turn's defensive reinforcements
- AI should consider how long it would take for these units to actually be able to participate in the attack or defense as well. if the mobilization capacity restrictions, for example, cause them to have to be deployed out of reach, that should impact their attack/defense value, which is below at turns_to_reach in the formulas that follow. turns_to_reach should be the turns that it would take that unit to participate in / reach the territory for reinforcement or attacking (keep in mind that territories can be attacked from multiple territories, so for attack they just have to reach any territory that they can attack the target territory from)
---

### 5. Purchase: defense value and attack value per unit (per power)

**What we need:** Two scores per unit type (for stacking into the weighted sum above).

- **Proposed (you edit):**  
  `defense_value_per_power(unit) = ???`  
  e.g. `(defense + health) / power_cost`  
  `attack_value_per_power(unit) = ???`  
  e.g. `(attack + movement) / power_cost`  
  Add or change terms (e.g. dice, specials) as you like.

defense value = ((defense * rolls * health) + (is_archer * (defense-1) / 2) - turns_to_reach) / power_cost
attack_value = ((attack + (movement-1) * rolls * health) + transport_capacity + len(specials) - turns_to_reach) / power_cost

so that the AI doesn't only ever buy one unit that's technically the most cost-effective, let's find a way to incentivize keeping some balance in the ranks. i have a crazy way of doing that. take the unit types that the faction has available (gonna be 6-8 units), take the mean cost between the 2nd and 3rd cheapest unit, treat that as a lower bound. treat the median as an upper bound (not median active unit, but median unit type within the available unit types to purchase). AI should try to keep their total active unit mean cost (across all their territories) within that range, but not absolutely have to.
---

### 6. Movement: value of reinforcing vs value of attacking

**What we need:** How to score “reinforce territory T” vs “attack target T” so we can rank moves. Again, balance variables (e.g. threat, distance to stronghold), not fixed constants.

- **Proposed (you edit):**  
  `reinforce_value(territory, state) = ???`  
  e.g. `(is_stronghold ? 2 : 1) * (1 + count_adjacent_enemy_units)`  
  `attack_value(destination, state) = ???`  
  e.g. `(is_stronghold ? 3 : 1) + (is_capital ? 2 : 0) + power_production * 0.5 - distance_penalty`  
  Then combine (e.g. one score per move: reinforce_value for defensive moves, attack_value for offensive moves) so we can order moves.

  this seems similar to the one about territory loss cost and expected loss? maybe not. the idea would be that the AI re-calculates the sims for each additional unit that it decides to reinforce with during purchase, causing the expected loss to go down and then re-evaluate, and maybe find that it needs a second unit, and so on. basically choosing to dedicate a unit to the most important initiative (attack or defense) based on the formulas above, choosing one unit for that, re-calcing, and doing it again. so each unit has a particular purpose when purchased, which of course will later be changed as the game unfolds.

---

### 7. Optional: risk or variance

If you want the AI to be risk-averse or risk-seeking, we can add a term that depends on variance (e.g. sim variance of casualty cost or win rate). If you’re happy with expected values only, we skip this.

- **Proposed (you edit):**  
  `risk_adjustment(...) = ???` or “use expected values only”.

yes i'm fine with the AI being slightly risk-averse, so it should account for some variance.
---

Once these formulas are filled in (or replaced with your versions), we will implement them in code and avoid hard-coded “min win prob” or “necessary” gates; all battle and purchase decisions will come from comparing these balanced quantities.

---

## UI delays when the AI is acting

- **Goal**: Let the human **track what the AI is doing** instead of the whole turn updating in one jump.
- **Mechanism**: After each `POST /games/{id}/ai-step` response (or after each state refresh that reflects an AI action), the frontend introduces a **configurable delay** (e.g. 300–800 ms) before:
  - Applying the next state/UI update, or
  - Calling the next `ai-step`.
- Delays can be per **action** (e.g. each purchase, each move, each combat) or per **phase** (e.g. one delay after all purchase actions, then one after all combat moves). Exact values and per-action vs per-phase are UX choices to tune so the human can follow along without the turn feeling sluggish.

---

## Questions and high-level choices (for you to refine)

High-level questions that inform how we use the **Formulas to define** section above. The actual math lives in that section; here we only list policy questions.

### Purchase

1. **Unit mix**: Prefer scoring unit types by defense/offense value per power and weighting by threat (see formula section), or a simpler rule (e.g. “prefer infantry until N, then cavalry”)?
2. **Camps**: Under what conditions should the AI buy a camp? (e.g. only when a formula “camp_value(state)” exceeds “cost”, or when power and fronts exceed X?)
3. **Remaining power**: Should the AI ever leave power unspent on purpose (e.g. “saving for next turn”), or always choose the best basket subject to capacity and let remaining P be whatever it is?

### Combat

4. **Continue vs retreat**: Use the same **battle_net_value** formula re-run after the round (with updated sim), and retreat when net value turns negative (or below some balance)?
5. **Casualty order**: Fixed rule (e.g. best_defense / best_attack) or try both in sim and pick the order that gives better expected outcome?

### Movement

6. **Priority**: Rank moves by **reinforce_value** vs **attack_value** from the formula section; any extra criteria (e.g. “never leave stronghold X empty if adjacent to enemy”)?
7. **Sea vs land**: Any special rules for naval moves (e.g. only when we have a concrete attack target)?

### Long-term

8. **Stronghold targeting**: Prefer moves that get closer to the next stronghold we need, or sometimes “clean up” weaker territories first?
9. **Alliance play**: For v1, ignore or add simple “don’t attack ally” / “support ally front”?

Once the **Formulas to define** section is filled in and you’ve answered these, we codify them in code and per-phase policies.

---

## Sim-based purchase and mobilization (implemented)

**Flow in code:**

1. **Territories of interest (purchase)**  
   `purchase_defense_interest_territories` ranks **our** land under next-turn combat reach by expected loss (hold sim × `territory_loss_cost`). Ally tiles are excluded here because mobilization only places onto **our** camps/ports/home.

2. **Purchase**  
   Land slots use **marginal ΔP(hold)** from `marginal_hold_delta_add_land_unit` (worst-case enemy-faction reach, optional phantom defenders for multi-slot spread). Naval scoring gets extra weight from `StrategicTurnContext.naval_sea_zone_bonus` when coastal pressure is high. Phase weights blend with `purchase_defense_priority` (includes ally pressure at reduced weight).

3. **Mobilization**  
   Proximity to high **expected loss** uses `defense_expected_loss_by_territory`, which includes **allied** threatened land at `MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE` so placement can bias toward helping the alliance without ignoring our own fronts. Stronghold/capital destinations use sim hold + marginal ΔP when enemies can reach.

4. **Strategic turn context** (`strategic_context.py`)  
   Shared blob mode (defend / push / consolidate), pressure, non-combat and combat-move bonuses, ally-adjacent combat nudges, and purchase defense blend—built once per `decide()`.

---

## Implementation status

| Area | Implemented | Tested | Notes |
|------|-------------|--------|------|
| Formulas (formulas.py) | Yes | Via AI tests | battle_gain_if_win, expected_net_gain, defense/attack_value_per_power, purchase_cost_bounds, cost_range_bonus. |
| Purchase (purchase.py) | Yes | Yes | Formulas + **marginal hold sim** (phantom defenders, multi-front spread), defense interest list, naval coastal pressure from strategic context, strict-reach defensive mode. |
| Combat continue/retreat + initiate (combat.py) | Yes | Yes | Sim + expected net; **casualty order** compare (`best_attack` vs `best_unit`) when `COMBAT_COMPARE_CASUALTY_ORDERS`; split trials so total ≈ `COMBAT_SIM_TRIALS` per compared pair. |
| Decide dispatch (decide.py) | Yes | Yes | All phases; `build_strategic_turn_context` each tick. |
| API | Yes | Integration / manual | `ai_factions`, POST ai-step, server fills empty dice for AI initiate/continue. |
| Mobilization (mobilization.py) | Yes | Yes | Stronghold/power, distance-to-enemy, **expected-loss proximity** (own + scaled ally need), marginal hold where applicable. |
| Combat move (combat_move.py) | Yes | Yes | Multi-attack balance; strategic blob mult / strip penalty / **ally-adjacent attack bonus**. |
| Non-combat move (non_combat_move.py) | Yes | Yes | Marginal hold sims with **coalition garrison** on allied destinations; ally reinforce scoring; crisis stronghold bias. |
| Defense sim (defense_sim.py) | Yes | Yes | Worst-case enemy-faction reach, hold prob, **`defense_hold_saturation_threshold`** (single helper; thresholds from habits only). |
| Alliance coordination | Yes | Yes | Coalition defenders in relevant hold sims; strategic bonuses; `MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE`. |

**Tests:** `test/test_ai.py` (phase smoke + validation), **`test/test_ai_coalition_strategic.py`** (alliance hold / need map / strategic context / purchase interest scope). Run full `test/` for regression.

---

## Incremental Implementation Plan

1. **Phase 1 – Purchase only** (Implemented. Tested.)
   - Add `backend/ai/` with `context`, `habits` (config: e.g. `defend/attack capability (no spend-ratio)`), and `purchase.py` that, given context, returns a `purchase_units` action (or `end_phase` if nothing to buy).
   - Use engine only to read: `get_purchasable_units`, `get_mobilization_capacity`, `state.faction_resources`, unit costs. Build a valid `purchases` dict that respects capacity and resource constraints and uses power to maximize defend/attack capability (no spend-ratio target).
   - API: add `ai_factions` to game config (e.g. from create-game request or default for single-player). Add `POST /games/{id}/ai-step` that, if current faction is AI, calls `ai.decide`, validates, applies, saves, returns state.
   - Frontend: when `current_faction` is in `ai_factions`, show “Computer’s turn” and call `ai-step` in a loop with delays between steps so the human can track what the AI is doing.

2. **Phase 2 – End phase / end turn** (Implemented. Tested.)
   - AI can return `end_phase` when it has nothing else to do in purchase (or after one purchase), and `end_turn` in mobilization when done. This gets the AI through a full turn with only purchase + end_phase (and later mobilization + end_turn).

3. **Phase 3 – Mobilization** (Implemented. Tested.)
   - Policy: place purchased units into territories (camps, home, ports). Prefer strongholds and high-power territories; prefer reinforcing weak borders. Use `mobilize_options` from available actions to get valid (territory, unit, count) and pick by heuristic.

4. **Phase 4 – Combat move** (Implemented. Tested.)
   - Balance units across multiple attacks; once an attack reaches ~90% win rate (confident), dedicate units to a second attack. Units sent is input to sim; recalc per candidate move. See `COMBAT_MOVE_CONFIDENT_WIN_RATE` in habits.

5. **Phase 5 – Combat (resolve battles)** (Implemented. Tested.)
   - **Initiate:** Sim-scores each contested battle (same casualty-order compare as continue when enabled); picks from top score band. **Continue/retreat:** Re-sim each round; retreat destination scored (value − threat); casualty order passed when `best_attack` wins the compare. Server still generates dice when AI sends empty rolls.

6. **Phase 6 – Non-combat move** (Implemented. Tested.)
   - Setup for next turn: reinforce (defense) + forward position (attack); DEFEND_VS_ATTACK_WEIGHT.

7. **Later / optional**
   - Deeper lookahead (multi-step combat_move or purchase lookahead).
   - MCTS or rollouts for high-stakes battles.
   - More automated tuning of `habits.py` from replay data.

---

## Frontend Integration (Summary)

- **Game meta**: Backend returns `ai_factions: string[]` in `GET /games/{id}/meta`. Frontend `GameMeta` includes `ai_factions?: string[]`.
- **When it's the AI's turn**: If `gameMeta.ai_factions` includes `backendState.current_faction`, the frontend shows **"Computer's turn"** in the header (instead of the faction name) and runs an AI step loop: call `POST /games/{id}/ai-step`, apply the returned state and events, then wait **AI_STEP_DELAY_MS** (e.g. 2.5 s) before refreshing and repeating. This gives the human time to see each action (purchase, move, combat, etc.) instead of the turn flying by.
- **Delay**: `AI_STEP_DELAY_MS = 2500` in `App.tsx`; configurable. One delay between each AI action.
- **Create game**: For single-player vs-AI, the backend defaults `ai_factions` to all factions except the first in turn order (human plays first faction). The client can override by sending `ai_factions` in the create request. When the game loads, meta includes `ai_factions` and the AI loop runs on their turn.
- **Polling**: For single-player vs AI, polling is off (human only acts on their turn; AI steps are triggered by the client loop). Polling remains for multiplayer.

---

## Files to Add (Backend)

- `backend/ai/__init__.py` – expose `decide(context) -> Action | None`.
- `backend/ai/context.py` – dataclass or type for context (state, defs, available_actions).
- `backend/ai/habits.py` – constants (e.g. `COMBAT_SIM_TRIALS`, `PURCHASE_DEFENSE_WEIGHT_DEFAULT`); battle decisions use formula-based gain vs cost, not min win prob (see **Formulas to define**).
- `backend/ai/purchase.py` – `decide_purchase(context) -> Action`.
- `backend/ai/combat.py` – `decide_combat(context)` – continue vs retreat using sim + battle_net_value.
- `backend/ai/mobilization.py` – `decide_mobilization(context)` – place purchased units (prefer strongholds, then power).
- `backend/ai/formulas.py` – battle gain/cost/net, unit value per power, cost-range bonus.
- `backend/ai/combat_move.py` – `decide_combat_move(context)` – balance attacks; 90% confident then second attack.
- `backend/ai/non_combat_move.py` – `decide_non_combat_move(context)` – setup for next turn (reinforce + attack position).
- `backend/ai/decide.py` – `decide(context) -> Action | None` by phase (purchase, combat_move, non_combat_move, combat, mobilization, end_phase).
- Tests: `test/test_ai.py` (phase smoke), `test/test_ai_coalition_strategic.py` (alliance + strategic), plus combat/movement tests as needed.

API changes (in `backend/api/main.py`):

- Create game: accept `ai_factions: list[str]` or infer (e.g. single-player ⇒ all factions not chosen by human are AI).
- `POST /games/{game_id}/ai-step`: if `current_faction` in `ai_factions`, run `ai.decide`, validate, apply, save, return.

---

## Rules & Constraints Summary

- **All actions** come from the engine’s action constructors (`purchase_units`, `move_units`, `end_phase`, etc.).
- **Validity** is determined only by `validate_action(state, action, ...)`. The AI never invents new action types or payloads.
- **State changes** only via `apply_action` and `save_game` (by the API). The AI never writes to the DB or mutates state directly.
- **Definitions and available actions** come from the same code path as the human client (`get_game`, `get_game_definitions`, `_build_available_actions`). The AI sees the same rules and options as the UI.

This keeps the AI correct by construction: if the engine allows an action, the AI can choose it; if the engine forbids it, the AI cannot apply it.
