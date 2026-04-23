#!/usr/bin/env python3
"""
Build a single "master" JSON for admin **New setup → Import master JSON**.

The object has the same keys as the per-file setup JSONs (no ``.json`` in the key names):
  manifest, units, territories, factions, camps, ports, starting_setup, specials

Run from the repository root (so ``import backend`` works)::

  python3 backend/scripts/export_setup_master_json.py motw_1.0
  python3 backend/scripts/export_setup_master_json.py motw_1.0 -o /tmp/motw_master.json

Or::

  python3 -m backend.scripts.export_setup_master_json motw_1.0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo root when executed as backend/scripts/export_setup_master_json.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.engine.definitions import SETUPS_DIR  # noqa: E402
from backend.setup_data import import_setup_folder_to_dicts  # noqa: E402

_MASTER_KEYS = (
    "manifest",
    "units",
    "territories",
    "factions",
    "camps",
    "ports",
    "starting_setup",
    "specials",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export one master JSON object from backend/data/setups/<setup_id>/",
    )
    parser.add_argument(
        "setup_id",
        help="Folder name under backend/data/setups (e.g. motw_1.0)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Write to this file instead of stdout",
    )
    args = parser.parse_args()

    folder = SETUPS_DIR / args.setup_id.strip()
    bundle = import_setup_folder_to_dicts(folder)
    if not bundle:
        print(f"ERROR: Incomplete or missing setup folder: {folder}", file=sys.stderr)
        sys.exit(1)

    out_obj = {k: bundle[k] for k in _MASTER_KEYS}
    text = json.dumps(out_obj, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
