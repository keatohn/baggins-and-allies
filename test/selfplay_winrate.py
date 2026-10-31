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

Run from the repo root:
    PYTHONPATH=. .venv/bin/python test/selfplay_winrate.py \
        --matchup mcts-vs-mcts --games 4 --max-cycles 20 --difficulty easy
"""

import argparse
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_ai_mcts_selfplay import (  # noqa: E402
    _load_setup,
    format_matchup_report,
    run_matchup,
    summarize_winrate,
)

MATCHUPS = {
    "mcts-vs-mcts": {"good": "mcts", "evil": "mcts"},
    "mcts-vs-heuristic": {"good": "mcts", "evil": "heuristic"},
    "heuristic-vs-mcts": {"good": "heuristic", "evil": "mcts"},
    "heuristic-vs-heuristic": {"good": "heuristic", "evil": "heuristic"},
}


def main() -> int:
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
        algorithms = {
            fid: by_alliance.get(getattr(fd.get(fid), "alliance", ""), "heuristic")
            for fid in (state.turn_order or list(fd))
        }
        print(
            f"game {game_idx + 1}/{args.games}: {args.matchup} on {args.setup} "
            f"({args.difficulty}, seed {args.seed + game_idx})",
            flush=True,
        )
        record = run_matchup(
            state, ud, td, fd, cd, port_d, algorithms,
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
