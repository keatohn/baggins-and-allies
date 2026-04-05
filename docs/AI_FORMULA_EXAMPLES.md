# AI Formula Examples (Dummy I/O for Gut-Checking)

These are example inputs and corresponding outputs for the main AI decision formulas, so you can verify the math and tune constants.

---

## 1. `expected_net_gain(win_rate, territory_id, territory_defs, faction_defs, defender_cas_mean, attacker_cas_mean)`

**Formula:**  
`expected_stronghold_gain = win_rate * (stronghold_val + capital_val)`  
`expected_power_gain = win_rate * power_production * EXPECTED_POWER_MULTIPLIER` (3.0)  
`expected_unit_gain = defender_cas_mean - attacker_cas_mean` (not scaled by win_rate)  
`expected_net_gain = expected_stronghold_gain + expected_power_gain + expected_unit_gain`

**Constants (from habits):**  
`EXPECTED_STRONGHOLD_VALUE = 10.0`, `EXPECTED_CAPITAL_VALUE = 5.0`, `EXPECTED_POWER_MULTIPLIER = 3.0`.

### Example 1: Empty territory (100% win, 0 casualties)

- Territory: plain, 1 power, not stronghold, not capital.  
  So: stronghold_val=0, capital_val=0, power_production=1.
- `win_rate = 1.0`, `defender_cas_mean = 0`, `attacker_cas_mean = 0`.

**Computation:**  
- expected_stronghold_gain = 1.0 * (0 + 0) = **0**  
- expected_power_gain = 1.0 * 1 * 3 = **3**  
- expected_unit_gain = 0 - 0 = **0**  
- **expected_net_gain = 0 + 3 + 0 = 3**

So a 1-power empty territory gives net **3**. With `COMBAT_MOVE_EMPTY_TERRITORY_BONUS = 18`, score = 3 + 18 = **21** (strongly above any weak battle).

### Example 2: Stronghold, 40% win rate, costly fight

- Territory: stronghold, 0 power, not capital.  
  stronghold_val=10, capital_val=0, power_production=0.
- `win_rate = 0.4`, `defender_cas_mean = 2`, `attacker_cas_mean = 5`.

**Computation:**  
- expected_stronghold_gain = 0.4 * 10 = **4**  
- expected_power_gain = 0.4 * 0 * 3 = **0**  
- expected_unit_gain = 2 - 5 = **-3**  
- **expected_net_gain = 4 + 0 - 3 = 1**

Net is still positive (1), so “continue” is chosen. If we had attacker_cas_mean = 8:  
expected_unit_gain = 2 - 8 = -6 → net = 4 - 6 = **-2**. Then we’d retreat unless stronghold risk rule applies.

### Example 3: Stronghold risk rule (retreat logic)

- Same stronghold (gain_if_win = 15 from battle_gain_if_win: 15 + 0 + 0).  
- `win_rate = 0.4`, `defender_cas_mean = 2`, `attacker_cas_mean = 8` → net = **-2**.  
- `COMBAT_RETREAT_NET_THRESHOLD = -2.0` → net is not above threshold.  
- `win_rate * gain_if_win = 0.4 * 15 = 6`.  
- `COMBAT_STRONGHOLD_RISK_MIN_EV = 4.0` → 6 >= 4, so we **continue anyway** (worth the risk).

If win_rate = 0.2: 0.2 * 15 = 3 < 4 → **retreat**.

---

## 2. `battle_gain_if_win(territory_id, territory_defs, faction_defs)`

**Formula:**  
`gain = (is_stronghold ? 15 : 0) + (is_capital ? 10 : 0) + power_production * 3`

**Examples:**

| Territory        | Stronghold | Capital | Power | gain_if_win |
|-----------------|------------|---------|-------|-------------|
| Plain 1 power   | No         | No      | 1     | 3           |
| Stronghold 0 p  | Yes        | No      | 0     | 15          |
| Capital 2 power  | No         | Yes     | 2     | 10 + 6 = 16 |
| Stronghold cap 2| Yes        | Yes     | 2     | 15 + 10 + 6 = 31 |

---

## 3. `battle_net_value(win_rate, gain_if_win, expected_cost)` and `battle_net_value_expected(...)`

**battle_net_value:**  
`(win_rate * gain_if_win) - expected_cost`.  
Interpretation: simple “expected gain minus cost”; attack when > 0.

**Example:**  
- win_rate = 0.6, gain_if_win = 10, expected_cost = 4 (we pay 4 on average).  
- **battle_net_value = 0.6 * 10 - 4 = 6 - 4 = 2** → attack.

**battle_net_value_expected:**  
`win_rate * gain_if_win - expected_loss * (1 - win_rate)`.  
Expected value of the gamble.

**Example:**  
- win_rate = 0.6, gain_if_win = 10, expected_loss = 5.  
- **battle_net_value_expected = 0.6 * 10 - 5 * 0.4 = 6 - 2 = 4** → attack.

---

## 4. Combat move: empty vs weak battle

- **Empty 1-power:** expected_net_gain = 3, score = 3 + 18 = **21**.  
- **Weak battle:** win_rate = 0.25, net = -1 (expected_net_gain), score ≈ -1 * (1 + 0.6) + noise ≈ -1.6.  
So empty at **21** is chosen over the weak battle.  
If we lowered `COMBAT_MOVE_EMPTY_TERRITORY_BONUS` to 5, empty would be 3 + 5 = 8, still above -1.6 but closer; 18 keeps free gains clearly dominant.

---

## 5. Retreat decision (round-by-round)

Each round we compute:

1. `net = expected_net_gain(win_rate, territory_id, ...)` from current survivors.
2. `gain_if_win = battle_gain_if_win(territory_id, ...)`.
3. **Continue** if:  
   `net > COMBAT_RETREAT_NET_THRESHOLD` (-2) **or**  
   `(is_stronghold and (win_rate * gain_if_win) >= COMBAT_STRONGHOLD_RISK_MIN_EV` (4)).
4. Otherwise **retreat** to first valid destination.

So we retreat when the fight is both negative expected value (net ≤ -2) and not worth the stronghold risk (either not a stronghold or win_rate * gain_if_win < 4).
