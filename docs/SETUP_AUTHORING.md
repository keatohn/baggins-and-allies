# Setup JSON

Each scenario has one editable document: `backend/data/setups/<id>/setup.json`.
There are no YAML sources, scenario inheritance, or separately editable compiled setup files.
The admin forms, JSON tabs, downloads, file-mode gameplay and initial database seed all use this format.

```json
{
  "manifest": { "id": "example", "display_name": "Example", "map_asset": "world", "is_active": false },
  "units": {
    "guard": { "seed": "guard" },
    "ally_guard": { "seed": "guard", "overrides": { "attack": 5 } }
  },
  "factions": { "good": { "seed": "good", "overrides": { "capital": "shire" } } },
  "territories": { "shire": { "seed": "shire", "overrides": { "produces": { "power": 4 } } } },
  "camps": {},
  "ports": {},
  "starting_setup": {
    "turn_order": ["good"],
    "territory_owners": { "shire": "good" },
    "starting_units": { "shire": [{ "unit_id": "guard", "count": 2 }] }
  }
}
```

Only selected entries appear. A seed reference with no overrides inherits all defaults.
Overrides merge objects recursively; lists, scalars and null replace their defaults.
`{"$delete": true}` removes an optional inherited field. Explicit overrides equal to their
current default remain pinned. Unit aliases can reference the same seed under different IDs.
Custom units/factions omit `seed` and supply their fields in `overrides`.
Territory IDs must belong to the selected map; their seed IDs match their region IDs.

Shared unit, faction and territory metadata lives in `backend/data/scenario_library.json`.
Map membership and adjacency live in `backend/data/maps/<map>/territories.json` and
the map editor's overrides. Territory metadata comes from matching shared seeds.
Terrain inherits from the territory seed and may be explicitly overridden by a setup. Adjacency always comes from the map.
The engine consumes resolved data; resolution is a one-way operation, not another editing workflow.

## Admin editing

Use **Setups → Units / Factions / Territories** to select seeds and edit optional overrides.
JSON mode on those tabs shows the compact seed references, rather than expanded definitions.
Manifest, camps, ports and starting positions are edited in the other tabs.
**Download setup JSON** downloads the current pending setup using the same format as Save.
There is no separate scenario-source editor or validation/preview step.
Saving an individual setup validates its resolved data and reports errors normally.

Changing **Map asset** requires confirmation. It replaces the territory selection, terrain
and adjacency together. Matching territory IDs retain metadata overrides, ownership and
unit placements. Removed IDs are pruned from ownership, unit deployments, camps and ports;
removed capitals are cleared. New territories have no owner and no starting units.
The change remains pending until Save; canceling the confirmation leaves the setup untouched.
The backend performs the same normalization before validating a map change.

Saving a map graph edit updates every setup using that map. Editing seed terrain affects every map and inheriting setup; explicit setup terrain overrides remain intact. Adjacency is inherited,
so graph changes do not require rewriting adjacency lists in setup JSON. Files mode updates
shared map files; DB mode updates shared map data and generated gameplay snapshots in one transaction. Landscape shows seed terrain as read-only; edit it in Seeds → Territories or the Builder assignment popup.
Existing games retain their own snapshots.

Use **Seeds → Review / Save and Rebuild** to edit shared defaults. Validation messages are
warnings and do not block seed saves. Invalid scenarios retain their last valid generated
DB snapshots until repaired; valid scenarios rebuild. Malformed records or missing seed
references still block saving. **Export** downloads `{library, setups, catalog}` as JSON.

## Files and database

`SETUP_SOURCE=files` (local development only) reads canonical files directly. Admin setup and
seed saves in this mode write those canonical files; they do not create legacy document copies.
Use `npm run dev:files` for local authoring. Never enable files mode in production.

In default DB mode, the canonical library and setup definitions are stored in the existing
`app_settings` record. `Setup` row columns are generated runtime snapshots, not a competing
source to edit. Fresh databases seed from the canonical files. Existing databases migrate
stored authoring JSON in place, flattening legacy `extends` once and preserving metadata,
selected seeds, explicit overrides, visibility and starting positions. Legacy geometry
patches stop overriding the selected map. Existing games are not rewritten.
There is no table migration, DB reset or import of repository examples into a live database.

The checked-in scenarios are historical examples, not an export of production.
Always export/back up production data before offline edits. Historical validation problems
remain visible and are not an invitation to overwrite live data with repository examples.

## CLI

```sh
# Resolve and validate canonical JSON without a DB or persistent writes
.venv/bin/python -m backend.tools.scenario build backend/data/setups/motw_1.0/setup.json --check

# Optional resolved snapshot for debugging (not a second editable setup)
.venv/bin/python -m backend.tools.scenario build backend/data/setups/motw_1.0/setup.json --output /tmp/resolved.json

# Unpack an admin export into canonical JSON and shared seed/map files
.venv/bin/python -m backend.tools.scenario unpack /path/to/export.json --output-root /tmp/setups

# Inspect a unit's inherited and overridden values
.venv/bin/python -m backend.tools.scenario explain gondor_archer backend/data/setups/motw_1.0/setup.json
```

`extract` can import old resolved JSON folders into this format. `unpack` also accepts old
JSON authoring exports as a one-way migration. YAML input and `--write-setup` are removed.
The CLI reports historical validation errors and exits nonzero; exporting does not erase them.

## API

- `GET /admin/setups/{id}` returns the canonical `setup`, shared `library`, and resolved fields for controls/previews.
- `PUT /admin/setups/{id}` accepts canonical setup JSON and returns `{ok, id, setup, bundle}`.
- `POST /admin/setups` accepts `{id, setup: JSON_OBJECT_OR_STRING}` for an inactive import; copy/legacy bundle imports remain supported.
- `POST /admin/setups/validate` validates canonical setup JSON without saving.
- `GET /admin/scenarios/export` returns `{library, setups, catalog}`.
- `/admin/setups/{id}/source` is removed.

Frontend calls use `/api` in development; backend routes have no `/api` prefix.

## Map builder selections

In **Maps → Builder → Select**, drag a box to select nodes and the edges between them.
Shift-click nodes/edges or Shift-drag to extend a selection. Use **Copy selection** /
**Paste selection**, or Ctrl/Cmd+C and Ctrl/Cmd+V. The builder clipboard also works
between drafts while the builder stays open. Pasted geometry has independent node
and edge IDs and starts slightly offset; drag it to its destination. Selecting a
region's nodes also carries its labels. Across maps their seed IDs are retained. Within the same map duplicate assignments are left unnamed, so choose another seed or create one for the copied region.

Drag the selection rectangle's side handles to scale horizontally or vertically;
corner handles preserve aspect ratio and anchor the opposite corner. Shapes remain
within the map bounds. Handles can also be focused and resized with arrow keys
(Shift = 10 px). Escape or an interrupted pointer cancels a resize. Each paste or
resize gesture is one undo/redo step. Shared nodes still reshape connected neighbors.

## Territory identity and terrain

A map region is assigned to a shared territory seed ID; the Builder document stores only
`labels: {"seed_id": {"x": ..., "y": ...}}`. Its name, terrain, image, production and stronghold
properties come from that seed. There is no independent map territory name or second ID.
The inherited display name is shown when a territory is selected; its ID remains an internal reference.

Use **Builder → Label → Assign territory** to pick an existing seed from the dropdown or
click the **Create new territory seed** button below the dropdown. The popup includes the same metadata fields as Seeds →
Territories. **Create and assign** creates the seed immediately and assigns its ID in the
current draft; save the map to publish the assignment. A seed can only be assigned once per
map. Unassigned regions can remain while drawing. Imported unknown IDs must be assigned to
existing seeds or created before saving the map. **Edit shared seed** updates its properties
everywhere; IDs remain immutable.

Changing a region's seed identity requires confirmation. On map save, affected setup
territory lists replace retired IDs with newly assigned ones, preserving matching records,
removing old ownership/units/camps/ports and clearing removed capitals. New IDs start unowned
and empty. Existing games retain snapshots. Graph edges referencing retired IDs are removed.

The terrain migration chooses a deterministic shared default for legacy IDs and preserves
different existing map terrain as explicit setup overrides. Checked-in scenario terrain is
verified against a baseline from the pre-migration files. Old map-level terrain API payloads
remain compatible by updating shared seeds; new UI edits use the seed endpoints.

In **Maps → Graph**, **Select** changes the focused territory without changing connections.
Choose **Land**, **Aerial**, or **Ford** to toggle edges from that focus. Alt-click another
territory to change focus while retaining the active edge tool. Sidebar selections also
change focus without editing the graph.
