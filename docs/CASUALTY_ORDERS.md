# Casualty order (full tiebreak order)

Hits are applied one at a time. After each hit we re-sort to pick the next casualty. The unit that sorts first (lowest sort key) takes the hit.

## Step 0: stronghold wall soak (not a unit order)

In a **stronghold territory** (`territory_def.is_stronghold`), the wall is a separate HP pool on the territory (`TerritoryState.stronghold_current_health`) — it is not a unit and is never part of the sort below.

- **Normal attacker hits** drain the wall first; only overflow lands on defender units.
- **Hits from ladder-climbing infantry** bypass the wall entirely and go straight to defender units.

Once hits reach defender units, ladder and non-ladder hits are assigned identically — there is no per-unit "stronghold" sort key (no unit has an `is_stronghold` flag; only territories do).

## Defender order (land)

1. **remaining_health** (desc): healthiest unit takes the hit first (soak with high HP).
2. **Casualty order mode** (battle config): best_unit or best_attack/best_defense (cost vs effective stat).
3. **(Naval only) cargo_value** (asc): boats with less valuable cargo sink first.
4. **num_specials** (asc): fewer specials first.
5. **remaining_movement** (asc): immobile units first.
6. **instance_id**: deterministic tiebreaker.

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
