"""
AI habits and tuning constants.
Objective is to buy the right units (defend + attack capability), not to hit a spend ratio.
Decisions use formulas that balance variables (e.g. expected gain vs expected cost); see
docs/AI_PLAYER_DESIGN.md "Formulas to define (your input needed)".
"""

# ----- Purchase -----
# No TARGET_SPEND_RATIO: we maximize "right units" subject to power and mobilization caps.
MIN_POWER_RESERVE: int = 0

# Base defense vs offense blend for purchase scoring; blended further with
# StrategicTurnContext.purchase_defense_priority and strict-reach mode.
PURCHASE_DEFENSE_WEIGHT_DEFAULT: float = 0.5

# Diversity: nudge purchase away from over-represented types (based on total active units on map + purchased).
# If a unit type is already this fraction of our army, apply a penalty to buying more.
PURCHASE_DIVERSITY_OVER_RATIO: float = 0.6
PURCHASE_DIVERSITY_OVER_PENALTY: float = -0.85
# If a unit type is under-represented (below this fraction), small bonus.
PURCHASE_DIVERSITY_UNDER_RATIO: float = 0.2
PURCHASE_DIVERSITY_UNDER_BONUS: float = 0.2
# When filling slots one-by-one, penalty per unit already chosen in this batch (spreads mix).
PURCHASE_BATCH_DIVERSITY_PENALTY: float = 0.2
# Prefer cheapest line infantry (archetype infantry, no specials) over medium pieces unless
# formulas strongly favor specialists (e.g. anti-cavalry when enemy cav can reach us).
PURCHASE_CHEAPEST_INFANTRY_BONUS: float = 0.95
# cost_range_bonus penalizes costs below the roster band; cheap line sits there on purpose.
PURCHASE_CHEAP_LINE_COST_BAND_PAD: float = 0.45
# When army is already soldier-heavy, only lightly apply diversity over-penalty to cheap line.
PURCHASE_CHEAP_LINE_DIVERSITY_OVER_MULT: float = 0.18
# Cap total active siegework units (on map + purchased this round). Stops AI over-buying siege; value saturates.
MAX_ACTIVE_SIEGEWORK: int = 3

# Purchase: lighter sim trial count for marginal-defense scoring (many unit×territory checks).
PURCHASE_DEFENSE_SIM_TRIALS: int = 48
# How much marginal ΔP(hold) × territory_loss_cost (per power) feeds into purchase score alongside formulas.
PURCHASE_SIM_DEFENSE_VALUE_SCALE: float = 0.42
# Max threatened territories to evaluate for purchase sim (by expected loss).
PURCHASE_DEFENSE_INTEREST_MAX: int = 12
# Multi-front phantom: higher = more spread of hypothetical buys across threatened tiles (diminishing weight per power already placed on a tile).
PURCHASE_PHANTOM_SPREAD_COEFF: float = 0.32
# Naval purchases: extra score when strategic context flags coastal/sea pressure (max bonus capped).
NAVAL_PURCHASE_COAST_PRESSURE_SCALE: float = 0.11
NAVAL_PURCHASE_COAST_PRESSURE_MAX: float = 14.0

# Retreat: prefer safer adjacent hexes (lower next-turn reach threat), penalize stopping on a frontline.
RETREAT_SCORE_THREAT_WEIGHT: float = 2.6
RETREAT_SCORE_FRONTLINE_PENALTY: float = 4.2

# Mobilization: bonus for placing closer to high expected-loss territories (sim-based need).
MOBILIZATION_DEFENSE_SIM_TRIALS: int = 48
MOBILIZATION_NEED_PROXIMITY_SCALE: float = 2.8
MOBILIZATION_NEED_MAP_MAX_TERRITORIES: int = 22
# Weight for ally-owned threatened tiles in need_by_territory (our tiles use full expected loss).
MOBILIZATION_ALLY_EXPECTED_LOSS_SCALE: float = 0.5
# Extra score: sim ΔP(hold) from adding one pooled land unit directly onto this destination (only when
# enemies can reach and tile is in need map or is stronghold/capital).
MOBILIZATION_MARGINAL_HOLD_SCALE: float = 14.0

# ----- Combat (attack decisions; use sim) -----
# Battle decisions use formula-based balance: expected gain vs expected cost (sim outputs
# attacker_casualty_cost_mean, defender_casualty_cost_mean, win rate). No fixed "min win prob"
# or "necessary" constants; see docs "Formulas to define" -> battle_net_value, battle_gain_if_win,
# battle_expected_cost.
# Archer on defense: multiplier applied to archer contribution in defense_value_per_power.
DEFENSE_ARCHER_MULTIPLIER: float = 0.9
# Anti-cavalry: multiplier for defense/attack value when unit has anti_cavalry and enemy has cavalry.
ANTICAV_VS_CAVALRY_MULTIPLIER: float = 1.2
# Attack/defense value formulas: terror bonus, aerial bonus, movement term floor.
TERROR_ATTACK_BONUS: float = 3.0
AERIAL_VALUE_BONUS: float = 2.0
MOVEMENT_MIN_FOR_ATTACK: int = 1  # max(1, movement-1) in attack value formula

# Expected net gain (attack decision): stronghold/capital/power multipliers; unit gain = def_cas - att_cas.
EXPECTED_STRONGHOLD_VALUE: float = 5.0
EXPECTED_CAPITAL_VALUE: float = 2.5
EXPECTED_CAMP_VALUE: float = 2.0
EXPECTED_PORT_VALUE: float = 2.0
EXPECTED_HOME_VALUE: float = 1.0
EXPECTED_POWER_MULTIPLIER: float = 2.5

# Number of sim trials per candidate battle (fast; some variability).
COMBAT_SIM_TRIALS: int = 100
# Initiate / continue combat: compare best_attack vs best_unit sims and pick higher expected net.
# When True, each order uses half the trials (sum = COMBAT_SIM_TRIALS), same total RNG budget as one full sim.
COMBAT_COMPARE_CASUALTY_ORDERS: bool = True

# When multiple contested battles exist, initiate order: penalize high attacker-casualty variance.
COMBAT_INITIATE_MODERATE_VARIANCE_PENALTY: float = 0.28
COMBAT_INITIATE_UNPREDICTABLE_VARIANCE_PENALTY: float = 0.72
# Same scale subtracted from expected_net_gain when deciding continue vs retreat (risk-averse).
COMBAT_CONTINUE_MODERATE_VARIANCE_PENALTY: float = 0.22
COMBAT_CONTINUE_UNPREDICTABLE_VARIANCE_PENALTY: float = 0.55

# Retreat: re-evaluate each round. If expected net gain drops below this, retreat unless high-value target justifies risk.
COMBAT_RETREAT_NET_THRESHOLD: float = 0
# When attacking a stronghold, allow continuing if win_rate * gain_if_win >= this (worth the risk).
COMBAT_STRONGHOLD_RISK_MIN_EV: float = 4.0

# Combat move: balance units across multiple attacks. Once an attack's sim win rate reaches this
# threshold we consider it "confident" (very likely to win). Marginal saturation (below) applies
# only when there are at least two defended destinations this phase — never for a lone attack.
COMBAT_MOVE_CONFIDENT_WIN_RATE: float = 0.85
# Do not make attacks with win rate below this; prefer end_phase over terrible odds.
COMBAT_MOVE_MIN_WIN_RATE: float = 0.18
# Tie-breaking among similar combat-move scores: pick_from_score_band + AI_SCORE_BAND_TOLERANCE.
AI_SCORE_BAND_TOLERANCE: float = 0.05
# Bonus for empty (0 defenders) conquest so the AI actually chooses to send a charging unit into valuable open space when that's best (score includes destination + charge_through conquests only; friendly pass-through does not count).
COMBAT_MOVE_EMPTY_TERRITORY_BONUS_ADDED: float = 2.0
COMBAT_MOVE_EMPTY_TERRITORY_BONUS_MULTIPLIER: float = 3.0
# Turns of movement to look ahead when valuing charge/open-space chains from the destination (after this combat move). One turn ≈ one full movement budget for that unit type.
COMBAT_MOVE_CHARGE_LOOKAHEAD_TURNS: int = 3
# Weight for multi-turn charge lookahead: best-path value of open space reachable over those turns from this move's destination.
COMBAT_MOVE_FUTURE_CHARGE_WEIGHT: float = 0.6
# Marginal gain (only if n_defended_destinations >= 2): small win/net bump from extra bodies —
# down-score so movers can serve a second defended front. Empty / single-front attacks never use this.
COMBAT_MOVE_MARGINAL_WIN_SATURATION_THRESHOLD: float = 0.05
# Also treat as saturated when marginal expected net gain is below this (gain flattens).
COMBAT_MOVE_MARGINAL_NET_SATURATION_THRESHOLD: float = 0.4
# Score multiplier when saturated (win or net); 1.0 = no penalty.
COMBAT_MOVE_SATURATION_SCORE_FACTOR: float = 0.22
# Extra pull toward empty conquests when another attack is already confident (saturated on win rate).
COMBAT_MOVE_EMPTY_WHEN_CONFIDENT_BONUS: float = 14.0
# When any committed attack is already very likely to win, nudge harder toward empty/open conquests.
COMBAT_MOVE_EMPTY_WHEN_HIGH_COMMIT_WIN_BONUS: float = 10.0
COMBAT_MOVE_HIGH_COMMIT_WIN_RATE_FLOOR: float = 0.88
# Bonus when the charge path is entirely through our/allied territory (into open space beyond).
COMBAT_MOVE_FRIENDLY_CHARGE_CORRIDOR_BONUS: float = 6.0
# Empty defender (open space): at most this many cavalry/charging units into the same destination
# hex over the whole combat_move phase (counts pending + new move). Second charger in one move only
# when multi-turn charge lookahead crosses COMBAT_MOVE_OPEN_SPACE_SECOND_CHARGE_MIN_FUTURE_GAIN.
COMBAT_MOVE_OPEN_SPACE_MAX_CHARGE_UNITS: int = 2
COMBAT_MOVE_OPEN_SPACE_SECOND_CHARGE_MIN_FUTURE_GAIN: float = 4.0
# When the same origin can reach multiple empty hexes, boost targets we have not yet opened from
# that origin this phase (so the AI "sees" several directions and spreads before reinforcing one).
COMBAT_MOVE_OPEN_SPACE_NEW_DIRECTION_BONUS: float = 4.0
# Saturated combat moves: penalize over-committing attackers vs defender size + looming enemies
# that can reach the origin. "Elite" units for meat-shield / undermanned heuristics = power cost >= this.
AI_ELITE_UNIT_MIN_POWER_COST: int = 10
COMBAT_MOVE_MEAT_SHIELD_OVERSTACK_PENALTY: float = 4.5
# Expected gain decays with graph distance (steps beyond an adjacent attack). Tuned so far stronghold
# snipes lose to local fights / adjacent targets.
COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_PER_STEP: float = 5.5
COMBAT_MOVE_ATTACK_DISTANCE_PENALTY_EXPONENT: float = 1.4
# When this frontline hex is already next-turn outnumbered (ours <= enemy reach count) and we still
# move land away: extra quadratic penalty vs distance so cavalry does not abandon a collapsing line.
COMBAT_MOVE_ABANDON_PRESSURED_FRONTLINE_PENALTY: float = 16.0
# When any frontline is weak (faction-wide) but this origin is not locally outnumbered: still
# discourage long marches from the border (linear add-on to distance decay).
COMBAT_MOVE_FRONTLINE_CRISIS_LONG_MARCH_EXTRA: float = 5.0
# Penalty per step when attacking a distant enemy stronghold from the frontline while a nearer enemy stronghold exists for this blob (avoid abandoning local front for far strongholds).
COMBAT_MOVE_DISTANT_STRONGHOLD_FROM_FRONTLINE_PENALTY: float = 3.0
# Counterattack into adjacent threat: if worst-case P(hold) on origin exceeds P(win) this attack by more than margin, do not relax garrison and down-score the move.
COMBAT_MOVE_HOLD_VS_COUNTERATTACK_MARGIN: float = 0.06
COMBAT_MOVE_HOLD_PREFERRED_OVER_COUNTERATTACK_PENALTY: float = 22.0

# ----- Mobilization -----
# Home territories: AI mobilizes matching unit types to home *before* camp/port (see mobilization.py).
# When multiple land units are purchased and both capital and non-capital slots exist, optionally
# cap one forward batch size so scoring can rotate; all picks still use _score_land_destination.
MOBILIZATION_SPLIT_MIN_TOTAL_LAND: int = 2
MOBILIZATION_FORWARD_SPLIT_MAX: int = 5

# ----- Strategic context (ally coordination) -----
# Scales for ally-tile bonuses in strategic_context (non_combat / combat_move purchase pressure).
STRATEGIC_ALLY_NON_COMBAT_SH_MULT: float = 14.0
STRATEGIC_ALLY_NON_COMBAT_LAND_MULT: float = 4.5
# Allied tile must show local border heat or this much reach pressure before non_combat bonus.
STRATEGIC_ALLY_NON_COMBAT_MIN_PRESSURE: float = 0.1
STRATEGIC_ALLY_COMBAT_MOVE_ADJACENT_SCALE: float = 6.0
STRATEGIC_ALLY_PURCHASE_DEFENSE_PRIORITY_MULT: float = 0.72
STRATEGIC_ALLY_COMBAT_MOVE_PRESSURE_FLOOR: float = 0.04

# ----- Movement -----
# Base weight for reinforce vs attack-setup when scoring non_combat_move; overridden by crisis
# stronghold mode and blended with StrategicTurnContext (purchase_defense_priority).
DEFEND_VS_ATTACK_WEIGHT: float = 0.5
# Penalize moving from a forward/staging hex to a safer rear hex when no enemy can reach the origin (wasted mobility).
NON_COMBAT_UNNECESSARY_RETREAT_PENALTY: float = 4.0
# Defensive sim: absolute P(hold) tiers before saturation. A hex can be capital + stronghold; we use
# max(applicable tier) so the highest defensive bar wins (typically capital >= stronghold >= default).
# Single consumer: defense_hold_saturation_threshold() in defense_sim.py (do not duplicate values elsewhere).
NON_COMBAT_DEFENSE_SATURATION_HOLD_STRONGHOLD: float = 0.96
NON_COMBAT_DEFENSE_SATURATION_HOLD_CAPITAL: float = 0.98
NON_COMBAT_DEFENSE_SATURATION_HOLD_DEFAULT: float = 0.90
# If ΔP(hold) from this candidate move is below this, marginal benefit is treated as negligible.
NON_COMBAT_DEFENSE_MARGINAL_HOLD_EPSILON: float = 0.012
# Scales marginal ΔP into the same ballpark as other move scores (tunable).
NON_COMBAT_DEFENSE_MARGINAL_HOLD_SCORE_SCALE: float = 120.0
# Reinforce_value: raw "reach" counts every enemy that could path here next turn — can be huge and
# blocks saturation. Blend with adjacent enemy unit count; reach beyond local can only add this much
# to effective pressure for need/excess math (stops piling 13 on a SH "because 30 enemies exist somewhere").
NON_COMBAT_REINFORCE_REACH_BEYOND_LOCAL_CAP: int = 8
# Moving more bodies onto a stronghold that already exceeds effective pressure by this cushion.
NON_COMBAT_STRONGHOLD_OVERSTACK_CUSHION: int = 3
NON_COMBAT_STRONGHOLD_OVERSTACK_MOVE_PENALTY: float = 12.0
# Any owned stronghold/capital is under reach threat and below hold threshold (or under-manned):
# bias non_combat heavily toward reinforcing those tiles, not marching toward distant enemy SH.
NON_COMBAT_CRISIS_W_DEF: float = 0.9
NON_COMBAT_STRONGHOLD_CRISIS_REINFORCE_BONUS: float = 26.0
NON_COMBAT_CRISIS_PUSH_TOWARD_ENEMY_MULT: float = 0.14
# Non-combat move onto an allied faction's land: bonus per enemy that can reach that tile (capped).
NON_COMBAT_ALLY_REINFORCE_BONUS_PER_THREAT: float = 1.15
NON_COMBAT_ALLY_REINFORCE_THREAT_CAP: int = 8
# Empty ownable neutral (no enemy units on tile): allow expensive pieces to stage forward instead
# of getting zeroed by the elite undermanned-reach penalty meant for doomed defended hexes.
NON_COMBAT_ELITE_UNDERMANNED_NEUTRAL_MULT: float = 0.22
NON_COMBAT_EMPTY_NEUTRAL_STAGING_BONUS: float = 6.5
# Sea → land: discourage parking on an ally's beach when combat_move could take empty enemy/neutral
# coast instead (non_combat still allows allied staging for crisis — see non_combat_move).
NON_COMBAT_SEA_OFFLOAD_ALLIED_BEACH_PENALTY: float = 7.5

# ----- Lines / geography -----
# Prefer moving/mobilizing to frontline (our territories adjacent to enemy). Added to destination score.
FRONTLINE_BONUS: float = 3.5
# Prefer destinations that are closer to this blob's nearest enemy stronghold (push direction). Per-step bonus.
PUSH_TOWARD_STRONGHOLD_PER_STEP_BONUS: float = 1.0
