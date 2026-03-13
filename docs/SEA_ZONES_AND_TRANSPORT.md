# Sea Zones and Transport – Scope and Plan

## Summary

Introduce **~10 sea zones** so boats can move at sea and **transport** land units. Land units never move in sea on their own; only naval units (and optionally aerial) can enter sea zones. Boats use existing `transport_capacity` to carry land units when moving.

**Recommended approach: model sea zones as territories** with a distinct type, and extend movement/validation to support “driver + passengers” when the destination is sea (or the path includes sea).

---

## 1. Model: Sea Zones as Territories (Recommended)

- **Same structure**: Sea zones are entries in `TerritoryDefinition` and `state.territories`, like land. No new top-level concept (e.g. no separate `sea_zones` dict).
- **Why**: One graph, one `state.territories`, one movement/reachability pipeline. Combat, retreat, and UI already key off territory IDs.
- **How to distinguish**: Use **`terrain_type: "sea"`** (or add an explicit `is_sea_zone: bool` if you prefer). Existing code already branches on `terrain_type` (e.g. mountain, forest) in a few places.

**Sea zone definition shape (in `territories.json`):**

- `terrain_type`: `"sea"`
- `adjacent`: land territories (coasts/ports) + other sea zones
- `produces`: `{}` (no power)
- `ownable`: `false` (sea is not conquered; optional design)
- `is_stronghold`: `false`
- No camps (camps live on land only)

**State:** Each sea zone gets a `TerritoryState` in `state.territories` with `owner=None` (or never set), `units=[]` initially. No need to put sea in `territory_owners` in starting_setup.

**Initialization:** In `initialize_game_state`, every `territory_def` (including sea) already gets a `TerritoryState`. Ensure `starting_units` only places units in territories that exist; boats can start in a port (land) or in a sea zone if you define it that way.

---

## 2. Who Can Enter Sea Zones

- **Land units (infantry, cavalry, etc.):** **Never** enter a sea zone under their own movement. They only appear in sea when carried by a naval unit.
- **Naval units (boats):** New unit capability. Need a clear marker: **archetype `"naval"`** or tag **`"naval"`** (or both). Only these units can have sea zones in their reachable set and can use `transport_capacity`.
- **Aerial:** Design choice. Either (a) aerial can fly over/into sea like any other territory, or (b) aerial cannot end movement in sea. Recommendation: **(a)** for simplicity and to match “fly over obstacles” behavior.

So:

- **Reachability:** Land = never add sea to reachable set. Naval = add sea (and land, for ports). Aerial = add sea if we allow it (same as land for adjacency).

---

## 3. Movement Logic Changes

### 3.1 Reachability (backend)

**File:** `backend/engine/movement.py` – `get_reachable_territories_for_unit`.

- After resolving `adjacent_id` (and `_adjacent_ids` for aerial), get `adj_def = territory_defs.get(adjacent_id)`.
- **Land unit** (not aerial, not naval): if `adj_def` is sea (e.g. `terrain_type == "sea"` or `is_sea_zone`), do **not** enqueue that neighbor and do not add to `reachable`. So land units never see sea in their BFS.
- **Naval unit:** treat sea like any other territory (enqueue, add to reachable). Optionally restrict naval to only enter land territories that are “ports” (e.g. a tag or list on territory def) if you want; otherwise naval can enter any adjacent land (coast).
- **Aerial:** no extra filter if we allow aerial in sea; otherwise same as land for sea (skip sea).

Helper suggestion: `_is_sea_zone(territory_def)` and `_can_unit_enter_sea(unit_def)` (True for naval, and optionally aerial).

### 3.2 Transport: “Driver + Passengers” Moves

Today, **every** unit in a move must individually reach the destination. So “boat + infantry” to a sea zone would fail because infantry can’t reach sea.

**New rule for moves that involve sea:**

- If the **destination** is a sea zone (or the **path** from origin to destination includes a sea zone), the move is allowed if:
  1. At least one unit in the stack can legally reach the destination on its own (the “driver”) – e.g. a naval unit (and optionally aerial).
  2. Every other unit in the stack is a “passenger”: must be land (or a non-driver that can’t reach the destination).
  3. Total number of passengers ≤ sum of `transport_capacity` of all **driver** units in the stack (only naval units with capacity count as drivers for this).

So:

- **Backend – move validation** (e.g. in `queries.py` where move is validated, and in reducer where move is declared):  
  - If `to_id` is sea (or path includes sea):  
    - Require at least one naval (or allowed) unit in the stack that can reach `to_id`.  
    - Require every other unit to be land (or otherwise non-sea-capable).  
    - Require `count(passengers) <= sum(transport_capacity of drivers in stack)`.  
  - Else (destination is land): keep current rule – every unit must be able to reach `to_id` (no transport needed).

- **Backend – apply move** (reducer): No change to “move these instance_ids from A to B”; the same list of units moves together. Only validation changes.

- **Path/distance:** Use the driver’s reachability (and path cost) for the whole stack when the move is a transport move. So `get_reachable_territories_for_unit(boat, ...)` defines valid destinations; land units in the stack don’t need to have that destination in their reachable set.

**Edge cases to define:**

- Move from **land → sea**: driver = naval (and optionally aerial); passengers = land, within capacity.
- Move from **sea → land**: same idea; driver must be able to reach the land territory (naval can if it’s adjacent coast).
- Move from **sea → sea**: only naval (and optionally aerial); no “passengers” in the sense of land units (they’re already in sea only because they were carried there).
- Move from **land → land** through sea: path must be valid for the driver; if path includes sea, again passengers ≤ capacity.

So the reducer/validation needs to:

1. Classify destination (and optionally path) as involving sea or not.
2. If involving sea: require one driver (naval/aerial) that can reach destination; treat others as passengers; enforce capacity.
3. If not involving sea: keep current “all units must reach destination” rule.

---

## 4. Unit Definitions

- **Naval unit (boat):**  
  - `archetype`: `"naval"` (or tag `"naval"`).  
  - `transport_capacity`: e.g. 2 (number of land units it can carry).  
  - Movement, attack, defense, etc. as you want for combat (if you add sea combat later).

- **Land units:** No change; they already don’t have naval archetype/tag, so they’ll be excluded from entering sea in reachability.

`transport_capacity` already exists on `UnitDefinition`; it’s currently unused. Use it only for naval (or transport) units.

---

## 5. Combat and Retreat (Scope for Later)

- **Combat in sea:** If naval (and possibly aerial) can end up in the same sea zone as enemies, you’ll want combat there. That implies: initiate_combat allowed when attackers in a sea zone (or moving into a sea zone with defenders). Same `resolve_combat_round` can apply; only participants would be naval (+ aerial?). Land units in boats: either they don’t fight in sea combat (only boats fight) or they do at reduced strength – design choice. **Recommendation:** Phase 1 = movement + transport only; sea combat as a follow-up.
- **Retreat from sea:** If combat can happen in sea, retreat options for the loser: adjacent territories (sea zones + coasts). Retreat to “allied” might mean allied coast only (since sea is unowned). Define clearly when you add sea combat.

---

## 6. Frontend

- **Map:** Render sea zones like territories but with a distinct style (e.g. blue, different border). Same `territories` and `territoryData`; use `terrain_type === 'sea'` (or `is_sea_zone`) to style.
- **Movement highlights:**  
  - Land units: only land destinations (current logic once backend excludes sea from reachable).  
  - Naval: show sea + adjacent land (ports/coasts).  
  - When selecting a stack that includes both boats and land units, valid destinations can come from the “driver” (boat) and show transport as allowed (e.g. tooltip “2 infantry carried”).
- **Transport UX:** When user selects a mixed stack (boat + infantry) and drops on a sea zone, the move is “boat moves and carries infantry”. Backend already validates; frontend only needs to show valid drop targets (from boat’s reachability when transport is used) and optionally show capacity (e.g. “1/2 transport”).
- **Unit stats / specials:** Expose “Naval” and “Transport (2)” (or similar) in the unit stats modal so players see who can enter sea and who can carry.

---

## 7. Implementation Order (Suggested)

1. **Data and types**  
   - Add ~10 sea zones to `territories.json` (terrain_type `"sea"`, adjacent, ownable false, produces {}).  
   - Optionally add `is_sea_zone: bool` to `TerritoryDefinition` and set it from JSON for clarity.

2. **Reachability**  
   - In `get_reachable_territories_for_unit`, skip sea neighbors for land units; allow them for naval (and optionally aerial). Add `_is_sea_zone()` and `_can_unit_enter_sea()` (or equivalent).

3. **Naval archetype/tag**  
   - Add at least one boat unit with `archetype: "naval"` (or tag `"naval"`) and `transport_capacity > 0`.

4. **Move validation and reducer**  
   - When destination (or path) involves sea: switch to “driver + passengers” rule; require at least one driver that can reach destination; enforce passenger count ≤ driver transport capacity.  
   - Keep “every unit must reach destination” for land-to-land moves.

5. **Path/cost for transport moves**  
   - Use driver’s `get_reachable_territories_for_unit` (and path/cost) for the whole stack when the move is a transport move. Ensure `get_shortest_path` / `movement_cost_along_path` work for naval (sea links are just normal edges for them).

6. **Frontend**  
   - Style sea zones on the map; use backend reachability so land never sees sea, naval sees sea + coasts; show transport capacity when relevant.

7. **Later**  
   - Sea combat (who fights, who can initiate, retreat from sea).  
   - Optional: “ports” only for naval landing (restrict which land territories naval can enter).

---

## 8. Alternative: Separate Sea Graph

Instead of sea-as-territories, you could have `state.sea_zones` and a separate adjacency for sea. That would require:

- Every movement and combat path to know about two graphs (land vs sea) and transitions (port ↔ sea).
- More branching in reducer, queries, and frontend.

**Recommendation:** Stick with sea zones as territories and a single graph; it keeps the model simple and reuses all existing territory machinery.

---

## 9. Checklist (Quick Reference)

- [ ] Sea zones in `territories.json` (terrain_type `"sea"`, ownable false, produces {}, adjacencies).
- [ ] `_is_sea_zone(territory_def)` (and optionally `is_sea_zone` in def).
- [ ] Reachability: land units never get sea in reachable set; naval (and optionally aerial) do.
- [ ] Naval unit(s): archetype or tag `"naval"`, `transport_capacity > 0`.
- [ ] Move validation: “driver + passengers” when destination (or path) involves sea; capacity check.
- [ ] Reducer: apply move unchanged; validation and reachability handle transport.
- [ ] Frontend: style sea zones; valid targets from backend (transport-aware); show transport in UI.
- [ ] (Later) Sea combat and retreat from sea.

This gives you a clear path to add ~10 sea zones and boat transport without changing the core idea that “territories” are the single place for both land and sea, and without overhauling combat or state shape.
