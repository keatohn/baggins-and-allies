# Casualty order (full tiebreak order)

Hits are applied one at a time. After each hit we re-sort to pick the next casualty. The unit that sorts first (lowest sort key) takes the hit.

## Global order (same for both sides)

1. **remaining_health** (desc): the **least wounded** (healthiest) unit takes the hit first—high HP units soak for others (Axis & Allies style).
2. **Casualty order mode** (see below): cost and effective stat.
3. **num_specials** (asc): fewer specials (unit_def.specials) first.
4. **remaining_movement** (asc): immobile units first.
5. **instance_id**: deterministic tiebreaker.

**must_conquer** (attacker only): Not a sort rule. If the *normal* casualty for this hit would be the **last ground unit** (this hit would kill it), that hit is assigned to an aerial instead (best aerial by the same tiebreak order). So aerials do not always go first—only when the hit would otherwise eliminate the last ground unit, so a ground unit can conquer.

## Attacker: Best Unit vs Best Attack

- **best_unit**: (cost asc, then effective attack asc) — lose cheap units first, then by attack.
- **best_attack**: (effective attack asc, then cost asc) — lose low-attack units first, then by cost.

## Defender: Best Unit vs Best Defense

- **best_unit**: (cost asc, then effective defense asc) — lose cheap units first, then by defense.
- **best_defense**: (effective defense asc, then cost asc) — lose low-defense units first, then by cost.

Effective stat = base stat + terrain/captain/anti-cavalry modifiers for this combat.
