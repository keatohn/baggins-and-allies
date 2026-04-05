# Casualty order (full tiebreak order)

Hits are applied one at a time. After each hit we re-sort to pick the next casualty. The unit that sorts first (lowest sort key) takes the hit.

## Defender order (land, with stronghold territory)

When the defender is in a **stronghold territory** (`territory_def.is_stronghold`):

1. **is_stronghold** (desc for normal hits, asc for ladder hits):
   - **Normal attacker hits**: stronghold takes first hits (is_stronghold **desc** — units in stronghold soak first).
   - **Hits from ladder attackers**: assign to non-stronghold first (is_stronghold **asc** — ladder infantry bypass stronghold; their hits go to defender units that are not stronghold first). If all defenders are in the stronghold, this is a no-op for ordering.
2. **remaining_health** (desc): healthiest unit takes the hit first (soak with high HP).
3. **Casualty order mode** (battle config): best_unit or best_attack/best_defense (cost vs effective stat).
4. **(Naval only) cargo_value** (asc): boats with less valuable cargo sink first.
5. **num_specials** (asc): fewer specials first.
6. **remaining_movement** (asc): immobile units first.
7. **instance_id**: deterministic tiebreaker.

When the defender is **not** in a stronghold territory, steps 1–2 collapse (no stronghold key; remaining_health is first).

## Attacker order

1. **remaining_health** (desc)
2. **Casualty order mode**: best_unit or best_attack (cost vs effective attack)
3. **(Naval only) cargo_value** (asc)
4. **num_specials** (asc), **remaining_movement** (asc), **instance_id**

## Attacker: Best Unit vs Best Attack

- **best_unit**: (cost asc, then effective attack asc) — lose cheap units first, then by attack.
- **best_attack**: (effective attack asc, then cost asc) — lose low-attack units first, then by cost.

## Defender: Best Unit vs Best Defense

- **best_unit**: (cost asc, then effective defense asc) — lose cheap units first, then by defense.
- **best_defense**: (effective defense asc, then cost asc) — lose low-defense units first, then by cost.

Effective stat = base stat + terrain/captain/anti-cavalry modifiers for this combat.

## must_conquer (attacker only)

Not a sort rule. If the *normal* casualty for this hit would be the **last ground unit** (this hit would kill it), that hit is assigned to an aerial instead (best aerial by the same tiebreak order). So aerials do not always go first—only when the hit would otherwise eliminate the last ground unit, so a ground unit can conquer.
