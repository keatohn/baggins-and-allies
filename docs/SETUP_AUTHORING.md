# Scenario authoring

A scenario source (`setup.yaml`, or equivalent JSON) references shared **unit and faction seeds** and **map territory defaults**, then declares overrides. Compilation produces the existing seven documents: `manifest`, `units`, `territories`, `factions`, `camps`, `ports`, and `starting_setup`. Gameplay, saved games, and DB seeding continue to use those compiled documents. There is no inheritance resolution inside the engine.

## Production data comes first

The checked-in setups are historical examples, **not an export of production**. The first startup with this feature builds an authoring library by majority vote over the current **database rows**, and extracts lossless sources from their effective admin bundles (including existing map terrain and graph data). It stores these in the existing `app_settings` table under `scenario_authoring_v1`. It does not replace setup rows, import historical seeds into production, change publication status, or touch existing games.

Defaults are voted per field, recursively for objects. Lists vote as whole values. Missing fields do not vote; an extracted removal override preserves their absence. Equal vote counts are resolved by lexicographic canonical JSON, independent of setup ordering. Every extracted source must compile to its original bundle. Later initialization preserves the stored library.

Use **Admin → Seeds → Export live authoring data** to download the current library, scenario sources, and global catalog before authoring offline. Keep production backups outside git. To unpack the export:

```sh
.venv/bin/python -m backend.tools.scenario unpack /path/to/scenario-authoring.json --output-root /tmp/live-scenarios
.venv/bin/python -m backend.tools.scenario --data-root /tmp/live-scenarios build /tmp/live-scenarios/setups/SCENARIO/setup.yaml --check
```

`unpack` preserves historical content even when it has validation errors; `build` and `validate` report those errors and return a failing exit status. Never replace live setups with the repository examples to resolve an authoring validation failure.

## Admin flow

1. **Seeds:** edit shared unit or faction records with the existing field editors. **Review changes** shows affected scenarios and their compiled differences. **Save defaults and rebuild** validates first and updates the library and affected compiled setups in one transaction. Existing explicit overrides remain pinned, including overrides equal to the previous default. Existing games retain their snapshots.
2. **Maps → Territory defaults:** edit territory name, image, default power production, stronghold, and ownable values. Add new map territory defaults here. Terrain stays in **Landscape** and adjacency stays in **Graph**, shared by setups as before. Existing map assets, positions, and decorations retain their editors.
3. **New setup:** create an empty draft, copy an existing setup, import a master bundle, or import declarative YAML/JSON (paste or select a file). Declarative imports always start inactive. Imported legacy bundles acquire a lossless source without replacing their values with seed defaults.
4. **Setups → Units / Factions / Territories:** choose a record. Unchecked fields inherit the visible default; checking a field records an override. The inherited value remains visible and is dimmed when overridden. Uncheck to return to inheritance. Additional optional fields, seed aliases, custom records, and record removal are supported. Lists and complex optional values use JSON inputs. Raw JSON remains available for advanced editing.
5. Edit manifest rules/victory, camps, ports, and starting ownership/deployment in their existing panels. Those edits are reconciled into the source when saved. Saving through the materialized forms flattens an inherited scenario; use the source editor to retain `extends`.
6. **Scenario source:** edit/download the source as YAML or JSON. **Validate & preview** shows the timeline, starting ownership/units, and compiled master JSON without saving. Save only after successful validation. Set `manifest.is_active: false` for a draft, `"admin"` for admin visibility, or `true` to publish. A visible scenario requires nonempty `manifest.context`.

Changing a shared default affects scenarios that inherit it when the default is saved. Review the affected-scenario list before applying it. New validation errors block the entire save. Historical validation errors may remain unchanged during a default update; saving an individual scenario still requires full validation. A source edit also rebuilds direct `extends` children and validates them. A parent cannot be deleted while children inherit it.

## Source schema (version 1)

| Field | Type | Meaning |
|---|---|---|
| `version` | integer, `1` | Source format version |
| `extends` | optional setup id | One parent source; the parent cannot itself extend another source |
| `map` | map id | Map defaults to compile against |
| `manifest` | object | Existing manifest: immutable setup `id`, display name, visibility, context, victory criteria, special rules, music, menu order, and costs |
| `units` | id → seed entry | Explicit roster; omitted seeds are not included |
| `factions` | id → seed entry | Explicit factions, including any subfaction definitions |
| `territory_ids` | optional list of ids | Map territory subset; omit to use the whole map |
| `territory_overrides` | territory id → patch | Name, PP, ownable, stronghold, and other scenario-specific territory fields |
| `camps` | object | Existing camp records keyed by camp id |
| `ports` | object | Existing port records keyed by port id |
| `starting_setup` | object | Existing `turn_order`, `territory_owners`, `starting_units`, and any optional starting rules |

A **seed entry** is `{seed: ID, overrides: PATCH}`. `seed` is optional for custom records; `overrides` defaults to `{}`. A roster key may differ from its seed id; the compiler assigns the aliased record's `id` to the roster key unless explicitly overridden. Faction, subfaction, `specials`, `home_territory_ids`, and downgrade references are structural fields and can be overridden along with statistics.

Unit seed records use the existing unit schema: `id`, `display_name`, `faction`, catalog `archetype`, integer `attack`, `defense`, `movement`, `health`, optional `cost: {power: N}`, `specials: [ID, ...]`, icon, hero/downgrade/home references, and other engine-supported unit attributes. Faction seeds use the existing faction schema: `id`, display name, alliance, capital, color, icon/banner/music, and optional subfactions. Defaults need not be repeated in a scenario.

Map records use the existing territory schema: `id`, `display_name`, `terrain_type`, adjacency lists (`adjacent`, `aerial_adjacent`, `ford_adjacent`), `produces: {power: N}`, `ownable`, `is_stronghold`, and optional image or territory attributes. Positions and landscape art remain in map `overrides.json` / map-builder data. In the deployed admin flow, map terrain and connections remain authoritative: edit them in Maps rather than scenario overrides.

### Merge rules

- Scalars replace, including booleans and explicit `null`. `null` stays JSON null; it is distinct from an absent field.
- Lists replace wholesale, never concatenate. `specials: []` clears all inherited specials.
- Objects merge recursively. `produces: {power: 4}` changes PP without replacing other production properties.
- `{$delete: true}` removes a field. This explicit marker distinguishes removal from null and makes lossless extraction possible. The same marker removes an inherited roster entry under `extends`.
- Under `extends`, omitted values inherit; objects merge and lists replace. To replace ownership/roster dictionaries entirely, explicitly remove unwanted inherited keys. Compilation flattens all references.
- Unknown top-level source keys and unknown map/seed/territory references are errors. YAML duplicate keys, aliases, non-string object keys, object constructors, non-JSON values, files larger than 2 MB, and excessive nesting are rejected.

Example variant of a local or exported scenario (use a parent available in the same source collection):

```yaml
version: 1
extends: wotr_1.1
manifest:
  id: my_war_of_the_ring
  display_name: My War of the Ring
  is_active: false
units:
  hobbit:
    overrides:
      attack: 6
      specials: []
      home_territory_ids: null
territory_overrides:
  the_shire:
    produces:
      power: 4
```

`map` selects the compiled `manifest.map_asset`. Extracted empty drafts retain their empty `map_asset` and use the setup id as the map reference, matching the existing loader.

Extracted legacy null map references are retained too; an explicit `manifest.map_asset: {$delete: true}` preserves an originally absent field. New sources may omit `manifest.map_asset` and let `map` fill it in.

## Files and commands

Install `requirements.txt` in `.venv` (includes PyYAML). These commands do not connect to the DB:

```sh
# Compile, validate against the catalog, and write nothing.
.venv/bin/python -m backend.tools.scenario build backend/data/setups/wotr_1.1/setup.yaml --check

# Produce an importable master bundle, only if validation succeeds.
.venv/bin/python -m backend.tools.scenario build backend/data/setups/wotr_1.1/setup.yaml --output /tmp/bundle.json

# Rebuild the seven committed files intentionally after source/default changes.
.venv/bin/python -m backend.tools.scenario build backend/data/setups/wotr_1.1/setup.yaml --write-setup backend/data/setups/wotr_1.1

# Validate a bundle or an existing setup folder.
.venv/bin/python -m backend.tools.scenario validate /tmp/bundle.json
.venv/bin/python -m backend.tools.scenario validate backend/data/setups/wotr_1.1

# Explain seed → override → effective unit, or compare compiled scenarios.
.venv/bin/python -m backend.tools.scenario explain hobbit backend/data/setups/wotr_1.1/setup.yaml
.venv/bin/python -m backend.tools.scenario diff backend/data/setups/wotr_1.1/setup.yaml backend/data/setups/wotr_exp_1.0/setup.yaml

# Mechanically extract defaults/sources from a directory of setup JSON folders.
.venv/bin/python -m backend.tools.scenario extract --setups backend/data/setups --output-root /tmp/extracted-scenarios
```

The library stores units/factions in `backend/data/scenario_library.json`; map territory defaults live in `backend/data/maps/<map>/territories.json`. The setup source lives alongside committed compiled JSON. `--data-root DIR` selects a different library, map/default collection, catalog, and parent-source lookup. It must appear before the verb. Extraction never rewrites input JSON.

Tests compare all six sources against their existing compiled JSON. Historical errors are explicitly recorded in `test/fixtures/scenario_validation_baseline.json`: 31 for `wotla_1.0`, 3 for `wotr_exp_1.0`, 2 for `wotr_exp_1.1`. The other three scenarios validate cleanly. These waivers preserve historical data; CLI validation still reports and fails on them. Do not expand the baseline to hide newly introduced errors.

The pure frontend override model and map node-selection regressions also run with Node 24:

```sh
node --experimental-strip-types --test test/scenario-overrides.test.mjs test/map_builder_selection.test.mjs test/landscape_border.test.mjs
```

In the map builder, deleting an intermediate node reconnects its two surviving neighbors. Deleting consecutive nodes simplifies the whole chain; duplicate edges and self loops are avoided. Junctions with more than two neighbors have no unambiguous replacement edge, so their incident edges are removed. Selecting and deleting an edge still removes that edge directly.

In Maps → Landscape, Border mode targets the shared border between two territories automatically. Select mountain art and click near the shared edge to fill just that border. Painting locks to the shared border where the stroke starts, preventing wide brushes from spilling onto other edges or coasts. Repeated fills do not stack duplicate mountains, including when approached from the other territory. There is no separate border selector or additional placement mode.

## HTTP contract

All endpoints require the existing admin authentication. Frontend requests retain `/api`; backend routes have no prefix.

| Method / backend path | Body / result |
|---|---|
| `GET /admin/scenario-library` | Current DB-derived units, factions, and maps |
| `POST /admin/scenario-library/preview` | Library object → `changed_setups`, per-setup compiled `changes`; no write |
| `PUT /admin/scenario-library` | Same library object → validated atomic rebuild and impact report |
| `GET /admin/scenarios/export` | Library, original sources, and catalog for offline work |
| `GET /admin/setups/{id}/source` | Original source, flattened `resolved_source`, library |
| `PUT /admin/setups/{id}/source` | `{source: YAML_STRING_OR_OBJECT}` → compile, validate, save source + compiled rows |
| `POST /admin/setups/validate` | Seven-key bundle or `{source: YAML_STRING_OR_OBJECT}` → `{valid, errors, bundle}`; no write |
| `POST /admin/setups` | Existing creation inputs, or `{id, source: YAML_STRING_OR_OBJECT}` → inactive compiled scenario |

Existing bundle save/import APIs remain compatible. The form may supply optional `authoring_source` metadata to preserve explicit override choices while saving the materialized bundle. The server reconciles and verifies it rather than trusting client-side compilation.

## Publish checklist

- Start from current exported DB data for production work.
- Confirm the map, roster, faction/subfaction mappings, capitals and home/downgrade references.
- Keep adjacency symmetric, with no references to missing territories; edit shared topology in Maps.
- Use catalog terrain/archetype/special ids. Validate ownership, deployment and turn order references.
- Inspect `explain`, compiled diffs, timeline, starting positions, and the existing stats/map previews.
- Save a draft first; use admin visibility for review; enable public visibility deliberately.
- For repository changes, rebuild compiled files and run `.venv/bin/pytest test/`, frontend build, and configured lint. Never deploy historical JSON over live DB content.

The reusable agent prompt is [prompts/SCENARIO_AUTHORING.md](prompts/SCENARIO_AUTHORING.md). In-app hosted LLM calls are outside this change; they require a separate API-key and cost decision.
