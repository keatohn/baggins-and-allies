# Editing setups

Read `docs/SETUP_AUTHORING.md`. Edit `backend/data/setups/<id>/setup.json` or the equivalent
canonical setup from a live admin export. Use selected seed references and optional overrides;
do not recreate YAML, `extends`, or expanded unit/faction/territory document copies.
Map membership and geometry belong to shared map data. Keep ownership and deployments
consistent with the selected territory IDs. Use the admin map-change confirmation to normalize
those dependent records automatically.

For production work, use a live export rather than checked-in historical examples. Preserve
publication status and existing game snapshots. Validate the resolved JSON with the CLI and
run the full pytest suite; report historical errors without replacing live values.
