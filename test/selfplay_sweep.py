#!/usr/bin/env python3
"""Standalone eval-weight sweep driver (manual; not collected by pytest).

MCTS 3.6c: compare weight variants against a baseline control via capped
self-play. Each variant runs both alignments (variant weights on good, then
on evil) across the same seeds so side-order luck cancels out; the aggregate
score rate (decisive wins + leader-at-cutoff points) decides whether a
variant beats baseline.

Variants are short-name weight overrides ({"material": "2.0"}) resolved to
AI_MCTS_W_* env vars per faction turn via run_matchup's env_overrides. The
default grid covers the material/territory axis (0.5, 2.0) and the
purchase-defense axis (0.0, 1.0); pass --variant to run a subset.

Run from the repo root:
    PYTHONPATH=. .venv/bin/python test/selfplay_sweep.py \
        --games-per-side 1 --seeds 1 --max-cycles 5 --difficulty easy
    PYTHONPATH=. .venv/bin/python test/selfplay_sweep.py \
        --variant baseline --variant material_2.0 \
        --games-per-side 2 --seeds 1 2 --max-cycles 10
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test.selfplay_winrate import MATCHUPS  # noqa: E402
from test.test_ai_mcts_selfplay import (  # noqa: E402
    _load_setup,
    resolve_weight_env,
    run_matchup,
    summarize_winrate,
)

SIDES = ("good", "evil")


@dataclasses.dataclass(frozen=True)
class SweepVariant:
    """One weight configuration under test."""

    name: str
    weights: dict[str, str]  # short names (or AI_MCTS_W_* keys) -> string values

    def __post_init__(self) -> None:
        # Fail fast on unknown keys / bad values before any game runs.
        resolve_weight_env(self.weights)


# Default grid (MCTS 3.6c): baseline control plus the material/territory
# axis and the purchase-defense axis. Values are strings so they pass through
# os.environ unmodified (eval_weights validates ranges itself).
DEFAULT_SWEEP_GRID: list[SweepVariant] = [
    SweepVariant("baseline", {}),
    SweepVariant("material_0.5", {"material": "0.5"}),
    SweepVariant("material_2.0", {"material": "2.0"}),
    SweepVariant("territory_0.5", {"territory": "0.5"}),
    SweepVariant("territory_2.0", {"territory": "2.0"}),
    SweepVariant("pdefense_0.0", {"purchase_defense": "0.0"}),
    SweepVariant("pdefense_1.0", {"purchase_defense": "1.0"}),
]


def variant_by_name(name: str) -> SweepVariant:
    for variant in DEFAULT_SWEEP_GRID:
        if variant.name == name:
            return variant
    raise KeyError(
        f"unknown variant {name!r}: choose from {[v.name for v in DEFAULT_SWEEP_GRID]}"
    )


def side_overrides(
    variant: SweepVariant,
    fd,
    faction_ids,
    alliance: str,
) -> dict[str, dict[str, str]]:
    """Per-faction env overrides putting the variant's weights on one alliance."""
    resolved = resolve_weight_env(variant.weights)
    if not resolved:
        return {}
    return {
        fid: dict(resolved)
        for fid in faction_ids
        if getattr(fd.get(fid), "alliance", "") == alliance
    }


def run_sweep(
    variants: list[SweepVariant],
    *,
    setup_id: str = "motw_1.0",
    matchup: str = "mcts-vs-mcts",
    seeds: list[int],
    games_per_side: int = 1,
    max_cycles: int | None = 20,
    max_faction_turns: int | None = None,
    difficulty: str = "easy",
) -> list[dict]:
    """Play every variant x seed x alignment and collect sweep records.

    For each variant and seed both alignments are played (variant weights on
    good, then on evil) so side-order luck cancels out. `games_per_side`
    repeats each alignment with seeds seed+0 .. seed+games_per_side-1.
    """
    if matchup not in MATCHUPS:
        raise ValueError(f"unknown matchup {matchup!r}: choose from {sorted(MATCHUPS)}")
    if games_per_side < 1:
        raise ValueError("games_per_side must be >= 1")
    if not seeds:
        raise ValueError("seeds must be non-empty")
    if max_cycles is None and max_faction_turns is None:
        raise ValueError("need a cap: max_cycles > 0 or max_faction_turns > 0")

    os.environ["AI_MCTS_DIFFICULTY"] = difficulty
    by_alliance = MATCHUPS[matchup]
    records: list[dict] = []

    for variant in variants:
        for base_seed in seeds:
            for alignment in SIDES:
                for game_idx in range(games_per_side):
                    seed = base_seed + game_idx
                    state, ud, td, fd, cd, port_d = _load_setup(setup_id)
                    faction_ids = state.turn_order or list(fd)
                    algorithms = {
                        fid: by_alliance.get(
                            getattr(fd.get(fid), "alliance", ""), "heuristic"
                        )
                        for fid in faction_ids
                    }
                    env_overrides = side_overrides(variant, fd, faction_ids, alignment)
                    record = run_matchup(
                        state,
                        ud,
                        td,
                        fd,
                        cd,
                        port_d,
                        algorithms,
                        env_overrides=env_overrides,
                        max_cycles=max_cycles,
                        max_faction_turns=max_faction_turns,
                        setup_id=setup_id,
                        seed=seed,
                    )
                    record = dict(record)
                    record["variant"] = variant.name
                    record["alignment"] = alignment
                    record["seed"] = seed
                    records.append(record)
                    outcome = (
                        record["winner"]
                        or f"capped (leader: {record['leader'] or 'none'})"
                    )
                    print(
                        f"[{variant.name}] {alignment} seed {seed}: {outcome} "
                        f"after {record['turns_driven']} turns "
                        f"({record['wall_time']:.0f}s)",
                        flush=True,
                    )
    return records


def aggregate_sweep(records: list[dict]) -> list[dict]:
    """Per-variant totals: decisive wins/losses, leader-at-cutoff points, score rate.

    Score = decisive wins + leader-at-cutoff points (capped games where the
    variant's alliance held the stronghold lead). Score rate = score / games;
    the variant with the higher rate than baseline is the adoption candidate.
    """
    by_variant: dict[str, dict] = {}
    for rec in records:
        name = rec["variant"]
        row = by_variant.setdefault(
            name,
            {
                "variant": name,
                "games": 0,
                "wins": 0,
                "losses": 0,
                "leader_pts": 0,
                "leader_none": 0,
                "wall_time": 0.0,
            },
        )
        row["games"] += 1
        row["wall_time"] += float(rec.get("wall_time", 0.0) or 0.0)
        variant_side = rec["alignment"]
        winner = rec.get("winner")
        if winner == variant_side:
            row["wins"] += 1
        elif winner in SIDES:
            row["losses"] += 1
        else:
            leader = rec.get("leader")
            if leader == variant_side:
                row["leader_pts"] += 1
            else:
                row["leader_none"] += 1
    rows: list[dict] = []
    for row in by_variant.values():
        games = row["games"]
        row["score"] = row["wins"] + row["leader_pts"]
        row["score_rate"] = row["score"] / games if games else 0.0
        row["win_rate"] = row["wins"] / games if games else 0.0
        rows.append(row)
    rows.sort(key=lambda r: (-r["score_rate"], r["variant"]))
    return rows


def format_sweep_table(rows: list[dict]) -> str:
    """Aligned text table of per-variant sweep results."""
    header = (
        f"{'variant':<16} {'games':>5} {'wins':>4} {'loss':>4} "
        f"{'ldpt':>4} {'score':>5} {'rate':>5} {'wall':>7}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['variant']:<16} {r['games']:>5} {r['wins']:>4} {r['losses']:>4} "
            f"{r['leader_pts']:>4} {r['score']:>5} {r['score_rate']:>5.2f} "
            f"{r['wall_time']:>6.0f}s"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--setup", default="motw_1.0", help="setup id")
    parser.add_argument(
        "--matchup",
        choices=sorted(MATCHUPS),
        default="mcts-vs-mcts",
        help="per-alliance algorithm pairing",
    )
    parser.add_argument(
        "--variant",
        action="append",
        dest="variants",
        metavar="NAME",
        help="variant to run (repeatable; default: full DEFAULT_SWEEP_GRID)",
    )
    parser.add_argument(
        "--games-per-side",
        type=int,
        default=1,
        help="games per alignment per seed (default 1)",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[1],
        help="base seeds (game i uses seed+i)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=20,
        help="full turn-order wraps per game (>=1)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=0,
        help="faction-turn cap per game (0 = no turn cap)",
    )
    parser.add_argument(
        "--difficulty",
        choices=["easy", "normal", "hard"],
        default="easy",
        help="AI_MCTS_DIFFICULTY budget for MCTS sides",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the full per-game matchup report too",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.games_per_side < 1:
        parser.error("--games-per-side must be >= 1")
    if args.max_cycles < 1:
        parser.error("--max-cycles must be >= 1 (use --max-turns for a turn cap)")
    max_turns = args.max_turns or None

    if args.variants:
        try:
            variants = [variant_by_name(name) for name in args.variants]
        except KeyError as exc:
            parser.error(str(exc))
    else:
        variants = list(DEFAULT_SWEEP_GRID)

    records = run_sweep(
        variants,
        setup_id=args.setup,
        matchup=args.matchup,
        seeds=list(args.seeds),
        games_per_side=args.games_per_side,
        max_cycles=args.max_cycles,
        max_faction_turns=max_turns,
        difficulty=args.difficulty,
    )

    if args.report:
        for rec in records:
            print(
                f"\n=== [{rec['variant']}] {rec['alignment']} seed {rec['seed']} ===",
                flush=True,
            )
            print(summarize_winrate([rec]), flush=True)

    print()
    rows = aggregate_sweep(records)
    print(format_sweep_table(rows))
    baseline = next((r for r in rows if r["variant"] == "baseline"), None)
    if baseline is not None:
        print()
        for r in rows:
            if r["variant"] == "baseline":
                continue
            delta = r["score_rate"] - baseline["score_rate"]
            verdict = "beats baseline" if delta > 0 else (
                "ties baseline" if delta == 0 else "loses to baseline"
            )
            print(f"{r['variant']}: {delta:+.2f} vs baseline ({verdict})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
