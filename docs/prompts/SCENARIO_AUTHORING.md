# Author a scenario

Read `docs/SETUP_AUTHORING.md` before editing. Use the supplied live authoring export when targeting production; checked-in setup JSON is historical and must not overwrite live values.

Given the requested scenario:

1. Unpack the export to a scratch data root, or use `backend/data` for repository-only work. Inspect the available map, unit/faction seeds, and catalog. Select a parent only when it has no `extends` of its own.
2. Write a version-1 declarative source containing the scenario's intent: map, seed-based roster/factions, territory deltas, manifest rules/victory, turn order, ownership, deployment, camps, and ports. Use explicit structural overrides for faction remaps. Do not duplicate inherited records or invent unknown seed/special/territory ids.
3. Compile with `python -m backend.tools.scenario --data-root DATA_ROOT build SOURCE --check`. Correct every reported error. Do not expand the historical waiver file to make a new scenario pass.
4. Run `explain UNIT SOURCE` for changed unit stats and `diff` against the base scenario. Check default changes for effects on every inheriting scenario.
5. Produce a validated bundle with `build SOURCE --output BUNDLE_JSON`, retaining the YAML source as the authoring artifact. For repository work, intentionally rebuild committed compiled files and run the full pytest suite.
6. Present the source, compiled differences, validation result, and remaining assumptions. Import as an inactive draft via Admin, review starting positions and stats, then publish only when instructed.

Do not modify engine rules, production DB credentials, or the historical example data merely to fit a new scenario. Keep scalars/lists replacement, recursive object merging, explicit null, and `{$delete: true}` removal semantics intact.
