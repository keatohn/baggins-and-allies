#!/usr/bin/env python3
"""Standalone self-play win-rate sweep (manual; not collected by pytest).

Drives full two-sided matchups via test_ai_mcts_selfplay.run_matchup until a
winner or a cycle/turn cap, printing per-game lines and an aggregate win-rate
table. CI only runs short capped matchups (see test_ai_mcts_selfplay.py, which
prints a baseline tally from those); use this script for the longer sweeps that
produce real win rates (e.g. before tuning evaluation weights in MCTS 3.6).

Matchups are by alliance (alliance names come from the setup's faction defs):
good and evil each get one algorithm. Caps are "whichever comes first";
--max-cycles 0 disables the cycle cap (then --max-turns must be > 0).

Eval-weight knobs (MCTS 3.6b): --w-<name> sets an AI_MCTS_W_* var for both
sides; --good-w-<name> / --evil-w-<name> set it for one alliance only (the
per-side value wins). Names: material, territory, stronghold, power_prod,
board_scale, purchase_defense. Weight sweeps compare e.g.
`--good-w-material 2.0` (variant good vs default evil) against the same
weight on evil, across seeds.

Run from the repo root:
    PYTHONPATH=. .venv/bin/python test/selfplay_winrate.py \
        --matchup mcts-vs-mcts --games 4 --max-cycles 20 --difficulty easy
    PYTHONPATH=. .venv/bin/python test/selfplay_winrate.py \
        --matchup mcts-vs-mcts --games 8 --max-cycles 20 \
        --good-w-material 2.0 --seed 1
"""

import argparse
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test.test_ai_mcts_selfplay import (  # noqa: E402
    WEIGHT_ENV_KEYS,
    _load_setup,
    format_matchup_report,
    resolve_weight_env,
    run_matchup,
    summarize_winrate,
)

MATCHUPS = {
    "mcts-vs-mcts": {"good": "mcts", "evil": "mcts"},
    "mcts-vs-heuristic": {"good": "mcts", "evil": "heuristic"},
    "heuristic-vs-mcts": {"good": "heuristic", "evil": "mcts"},
    "heuristic-vs-heuristic": {"good": "heuristic", "evil": "heuristic"},
}

SIDES = ("good", "evil")


def _add_weight_flags(parser: argparse.ArgumentParser) -> None:
    """Add --w-<name>, --good-w-<name>, --evil-w-<name> for each weight knob."""
    for name in WEIGHT_ENV_KEYS:
        flag = name.replace("_", "-")
        parser.add_argument(
            f"--w-{flag}",
            dest=f"w_{name}",
            default=None,
            metavar="V",
            help=f"AI_MCTS_W_{name.upper()} for both sides",
        )
        for side in SIDES:
            parser.add_argument(
                f"--{side}-w-{flag}",
                dest=f"{side}_w_{name}",
                default=None,
                metavar="V",
                help=f"AI_MCTS_W_{name.upper()} for the {side} alliance (wins over --w-{flag})",
            )


def _side_weight_env(args: argparse.Namespace, side: str) -> dict[str, str]:
    """Resolve one alliance's short-name weight overrides from parsed args."""
    shorts: dict[str, str] = {}
    for name in WEIGHT_ENV_KEYS:
        value = getattr(args, f"{side}_w_{name}", None)
        if value is None:
            value = getattr(args, f"w_{name}", None)
        if value is not None:
            shorts[name] = value
    return resolve_weight_env(shorts)


def _alliance_env_overrides(fd, faction_ids, args: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Per-faction resolved weight env overrides from per-side CLI flags."""
    per_side = {side: _side_weight_env(args, side) for side in SIDES}
    overrides: dict[str, dict[str, str]] = {}
    for fid in faction_ids:
        alliance = getattr(fd.get(fid), "alliance", "")
        side_env = per_side.get(alliance, {})
        if side_env:
            overrides[fid] = dict(side_env)
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--setup", default="motw_1.0", help="setup id")
    parser.add_argument(
        "--matchup", choices=sorted(MATCHUPS), default="mcts-vs-mcts",
        help="per-alliance algorithm pairing",
    )
    parser.add_argument("--games", type=int, default=2, help="games to play")
    parser.add_argument(
        "--max-cycles", type=int, default=10,
        help="full turn-order wraps per game (0 = no cycle cap)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=0,
        help="faction-turn cap per game (0 = no turn cap)",
    )
    parser.add_argument(
        "--difficulty", choices=["easy", "normal", "hard"], default="easy",
        help="AI_MCTS_DIFFICULTY budget for MCTS sides",
    )
    parser.add_argument("--seed", type=int, default=1, help="base seed (game i uses seed+i)")
    parser.add_argument("--report", action="store_true", help="print the full per-game report")
    _add_weight_flags(parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    max_cycles = args.max_cycles or None
    max_turns = args.max_turns or None
    if max_cycles is None and max_turns is None:
        parser.error("need a cap: --max-cycles > 0 or --max-turns > 0")
    if args.games < 1:
        parser.error("--games must be >= 1")

    os.environ["AI_MCTS_DIFFICULTY"] = args.difficulty
    by_alliance = MATCHUPS[args.matchup]

    records = []
    for game_idx in range(args.games):
        state, ud, td, fd, cd, port_d = _load_setup(args.setup)
        faction_ids = state.turn_order or list(fd)
        algorithms = {
            fid: by_alliance.get(getattr(fd.get(fid), "alliance", ""), "heuristic")
            for fid in faction_ids
        }
        env_overrides = _alliance_env_overrides(fd, faction_ids, args)
        weight_note = ""
        if env_overrides:
            sides = sorted({
                getattr(fd.get(fid), "alliance", "") for fid in env_overrides
            })
            weight_note = f" | weights on {'/'.join(sides)}"
        print(
            f"game {game_idx + 1}/{args.games}: {args.matchup} on {args.setup} "
            f"({args.difficulty}, seed {args.seed + game_idx}){weight_note}",
            flush=True,
        )
        record = run_matchup(
            state, ud, td, fd, cd, port_d, algorithms,
            env_overrides=env_overrides,
            max_cycles=max_cycles,
            max_faction_turns=max_turns,
            setup_id=args.setup,
            seed=args.seed + game_idx,
        )
        records.append(record)
        outcome = record["winner"] or f"capped (leader: {record['leader'] or 'none'})"
        print(
            f"  -> {outcome} after {record['turns_driven']} turns "
            f"({record['cycles_completed']} cycles), {record['wall_time']:.0f}s",
            flush=True,
        )
        if args.report:
            print(format_matchup_report(record), flush=True)

    print()
    print(summarize_winrate(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
