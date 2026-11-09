"""Run with ``python -m backend.tools.scenario --help``. No database access."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys

import yaml

from backend.engine.definitions import DATA_DIR
from backend.scenario_builder import (
    BUNDLE_KEYS, compile_scenario, extract_library, extract_source,
    load_library, parse_source, resolve_source, upgrade_territory_sources, write_library,
)
from backend.setup_validation import validate_setup_payload


def read_bundle(path: Path) -> dict:
    if path.is_dir():
        return {key: json.loads((path / f"{key}.json").read_text()) for key in BUNDLE_KEYS}
    return json.loads(path.read_text())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compile declarative scenarios without changing the database")
    parser.add_argument("--data-root", type=Path, default=DATA_DIR, help="Library, maps and setups directory")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="Extract majority defaults and lossless sources from JSON folders")
    extract.add_argument("--setups", type=Path, default=DATA_DIR / "setups")
    extract.add_argument("--output-root", type=Path, required=True, help="Destination data root (use a scratch directory for DB exports)")
    unpack = commands.add_parser("unpack", help="Write a live admin export to an offline data root")
    unpack.add_argument("snapshot", type=Path)
    unpack.add_argument("--output-root", type=Path, required=True)
    build = commands.add_parser("build", help="Compile and validate a setup.yaml")
    build.add_argument("source", type=Path)
    build.add_argument("--check", action="store_true", help="Dry run; emit errors only")
    build.add_argument("--output", type=Path, help="Write a seven-key master JSON bundle")
    build.add_argument("--write-setup", type=Path, help="Write compiled JSON documents to this folder")
    validate = commands.add_parser("validate", help="Validate master JSON, compiled setup directory, or setup.yaml")
    validate.add_argument("path", type=Path)
    explain = commands.add_parser("explain", help="Show seed, explicit overrides, and resolved unit")
    explain.add_argument("unit")
    explain.add_argument("source", type=Path)
    diff = commands.add_parser("diff", help="Diff compiled bundles (JSON, folder, or YAML)")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    args = parser.parse_args(argv)

    def lookup(setup_id):
        from backend.setup_ids import validate_setup_id
        if error := validate_setup_id(setup_id):
            raise ValueError(error)
        return parse_source((args.data_root / "setups" / setup_id / "setup.yaml").read_text())

    def compile_path(path):
        if path.suffix.lower() in (".yaml", ".yml"):
            return compile_scenario(parse_source(path.read_text()), load_library(args.data_root), lookup)
        return read_bundle(path)

    def printed(bundle):
        return json.dumps(bundle, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    try:
        if args.command == "unpack":
            from backend.setup_ids import validate_setup_id
            state = json.loads(args.snapshot.read_text())
            upgrade_territory_sources(state)
            library, sources = state["library"], state["sources"]
            bundles = {key: compile_scenario(source, library, sources.get) for key, source in sources.items()}
            for key in sources:
                if error := validate_setup_id(key):
                    raise ValueError(error)
            write_library(args.output_root, library)
            (args.output_root / "catalog.json").write_text(printed(state["catalog"]))
            for key, source in sources.items():
                folder = args.output_root / "setups" / key
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "setup.yaml").write_text(yaml.safe_dump(source, sort_keys=False, allow_unicode=True))
                for name in BUNDLE_KEYS:
                    (folder / f"{name}.json").write_text(printed(bundles[key][name]))
            print(f"Unpacked {len(sources)} live scenarios. Run validate to inspect historical errors.")
            return 0
        if args.command == "extract":
            bundles = {p.name: read_bundle(p) for p in sorted(args.setups.iterdir()) if (p / "manifest.json").is_file()}
            if not bundles:
                raise ValueError("No setup folders found")
            library = extract_library(bundles)
            sources = {key: extract_source(bundle, library) for key, bundle in bundles.items()}
            for key in bundles:
                if compile_scenario(sources[key], library) != bundles[key]:
                    raise ValueError(f"Lossless extraction failed for {key}")
            write_library(args.output_root, library)
            for key, source in sources.items():
                folder = args.output_root / "setups" / key
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "setup.yaml").write_text(yaml.safe_dump(source, sort_keys=False, allow_unicode=True))
            print(f"Extracted {len(sources)} scenarios; all compile identically to their input bundles.")
            return 0
        if args.command == "explain":
            library = load_library(args.data_root)
            source = resolve_source(parse_source(args.source.read_text()), lookup)
            entry = source.get("units", {}).get(args.unit)
            if entry is None:
                raise ValueError(f"Unknown unit: {args.unit}")
            print(printed({"seed": entry.get("seed"), "default": library["units"].get(entry.get("seed"), {}),
                           "overrides": entry.get("overrides", {}),
                           "resolved": compile_scenario(source, library)["units"][args.unit]}), end="")
            return 0
        if args.command == "diff":
            print("".join(difflib.unified_diff(printed(compile_path(args.before)).splitlines(True),
                                              printed(compile_path(args.after)).splitlines(True),
                                              fromfile=str(args.before), tofile=str(args.after))), end="")
            return 0
        path = args.source if args.command == "build" else args.path
        bundle = compile_path(path)
        catalog = json.loads((args.data_root / "catalog.json").read_text())
        errors = validate_setup_payload(bundle, catalog)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        if args.command == "build":
            if args.check and (args.output or args.write_setup):
                raise ValueError("--check cannot be combined with write options")
            if args.output:
                args.output.write_text(printed(bundle))
            if args.write_setup:
                args.write_setup.mkdir(parents=True, exist_ok=True)
                for key in BUNDLE_KEYS:
                    (args.write_setup / f"{key}.json").write_text(printed(bundle[key]))
            if not args.check and not args.output and not args.write_setup:
                print(printed(bundle), end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
