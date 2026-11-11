"""Inspect, validate and export canonical setup JSON. No database access."""
from __future__ import annotations
import argparse
import difflib
import json
from pathlib import Path
import sys
from backend.engine.definitions import DATA_DIR
from backend.scenario_builder import (
    BUNDLE_KEYS, canonical_setup, compile_setup, extract_library, load_library,
    migrate_legacy_setups, migrate_seed_terrain, parse_setup_json, read_setup_bundle, setup_from_bundle, write_library, write_setup,
)
from backend.setup_validation import validate_setup_payload


def read_bundle(path: Path) -> dict:
    return read_setup_bundle(path) if path.is_dir() else json.loads(path.read_text())


def update_setup_sections(folder: Path, sections: dict) -> None:
    """CSV compatibility: import changed records into the one canonical file."""
    library = load_library(folder.parent.parent)
    previous = canonical_setup(parse_setup_json((folder / "setup.json").read_text()))
    bundle = read_setup_bundle(folder)
    bundle.update(sections)
    map_id = bundle["manifest"].get("map_asset") or bundle["manifest"]["id"]
    if bundle["territories"].keys() - library["maps"].get(map_id, {}).keys():
        raise ValueError("CSV includes territories outside the selected map; edit the map first")
    write_setup(folder, setup_from_bundle(bundle, library, previous))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage canonical setup JSON")
    parser.add_argument("--data-root", type=Path, default=DATA_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "validate"):
        command = commands.add_parser(name, help="Resolve seed references and validate setup.json")
        command.add_argument("source", type=Path)
        if name == "build":
            command.add_argument("--check", action="store_true")
            command.add_argument("--output", type=Path, help="Optional resolved runtime snapshot")
    unpack = commands.add_parser("unpack", help="Write an admin JSON export as canonical setup files")
    unpack.add_argument("snapshot", type=Path)
    unpack.add_argument("--output-root", type=Path, required=True)
    extract = commands.add_parser("extract", help="Import legacy resolved folders into canonical setup JSON")
    extract.add_argument("--setups", type=Path, default=DATA_DIR / "setups")
    extract.add_argument("--output-root", type=Path, required=True)
    explain = commands.add_parser("explain")
    explain.add_argument("unit")
    explain.add_argument("source", type=Path)
    diff = commands.add_parser("diff")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    printed = lambda value: json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    try:
        if args.command in ("unpack", "extract"):
            if args.command == "unpack":
                snapshot = json.loads(args.snapshot.read_text())
                state = migrate_legacy_setups(snapshot)
                migrate_seed_terrain(state)
                catalog = snapshot.get("catalog")
            else:
                bundles = {folder.name: read_setup_bundle(folder) for folder in sorted(args.setups.iterdir())
                           if (folder / "setup.json").is_file() or (folder / "manifest.json").is_file()}
                if not bundles:
                    raise ValueError("No setup folders found")
                library = extract_library(bundles)
                state = {"library": library, "setups": {key: setup_from_bundle(bundle, library) for key, bundle in bundles.items()}}
                catalog = json.loads((args.data_root / "catalog.json").read_text())
            from backend.setup_ids import validate_setup_id
            for key in state["setups"]:
                if error := validate_setup_id(key):
                    raise ValueError(error)
            write_library(args.output_root, state["library"])
            if catalog is not None:
                (args.output_root / "catalog.json").write_text(printed(catalog))
            for key, setup in state["setups"].items():
                write_setup(args.output_root / "setups" / key, setup)
            print(f"Wrote {len(state['setups'])} canonical setup JSON files.")
            return 0
        library = load_library(args.data_root)
        def resolve(path):
            return read_setup_bundle(path) if path.is_dir() else compile_setup(parse_setup_json(path.read_text()), library)
        if args.command == "diff":
            print("".join(difflib.unified_diff(printed(resolve(args.before)).splitlines(True),
                printed(resolve(args.after)).splitlines(True), fromfile=str(args.before), tofile=str(args.after))), end="")
            return 0
        if args.command == "explain":
            setup = canonical_setup(parse_setup_json(args.source.read_text()))
            entry = setup["units"][args.unit]
            print(printed({"seed": entry.get("seed"), "overrides": entry.get("overrides", {}),
                           "resolved": compile_setup(setup, library)["units"][args.unit]}), end="")
            return 0
        bundle = resolve(args.source)
        errors = validate_setup_payload(bundle, json.loads((args.data_root / "catalog.json").read_text()))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        if args.command == "build":
            if args.check and args.output:
                raise ValueError("--check cannot be combined with --output")
            if args.output:
                args.output.write_text(printed(bundle))
            elif not args.check:
                print(printed(bundle), end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
