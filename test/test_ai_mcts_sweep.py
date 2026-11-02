"""Tests for the 3.6c-i eval-weight sweep runner (test/selfplay_sweep.py)."""

import types

import pytest

from test.selfplay_sweep import (
    DEFAULT_SWEEP_GRID,
    SweepVariant,
    aggregate_sweep,
    build_parser,
    format_sweep_table,
    run_sweep,
    side_overrides,
    variant_by_name,
)


def _fake_fd(alliances: dict[str, str]):
    return {
        fid: types.SimpleNamespace(alliance=alliance)
        for fid, alliance in alliances.items()
    }


def test_default_grid_names_and_axes():
    names = [v.name for v in DEFAULT_SWEEP_GRID]
    assert names[0] == "baseline"
    assert "material_0.5" in names and "material_2.0" in names
    assert "territory_0.5" in names and "territory_2.0" in names
    assert "pdefense_0.0" in names and "pdefense_1.0" in names
    baseline = DEFAULT_SWEEP_GRID[0]
    assert baseline.weights == {}


def test_variant_rejects_unknown_weight_keys():
    with pytest.raises(ValueError, match="unknown weight override"):
        SweepVariant("bad", {"nope": "1.0"})
    with pytest.raises(ValueError, match="unknown weight override"):
        SweepVariant("bad", {"wibble": "AI_MCTS_W_MATERIAL"})


def test_variant_by_name_known_and_unknown():
    v = variant_by_name("material_2.0")
    assert v.weights == {"material": "2.0"}
    with pytest.raises(KeyError, match="unknown variant"):
        variant_by_name("does_not_exist")


def test_side_overrides_targets_only_requested_alliance():
    fd = _fake_fd({
        "gondor": "good",
        "rohan": "good",
        "mordor": "evil",
        "isengard": "evil",
    })
    variant = SweepVariant("material_2.0", {"material": "2.0"})
    good = side_overrides(variant, fd, list(fd), "good")
    assert set(good) == {"gondor", "rohan"}
    assert good["gondor"] == {"AI_MCTS_W_MATERIAL": "2.0"}
    evil = side_overrides(variant, fd, list(fd), "evil")
    assert set(evil) == {"mordor", "isengard"}


def test_side_overrides_baseline_is_empty():
    fd = _fake_fd({"gondor": "good", "mordor": "evil"})
    baseline = variant_by_name("baseline")
    assert side_overrides(baseline, fd, list(fd), "good") == {}
    assert side_overrides(baseline, fd, list(fd), "evil") == {}


def test_aggregate_sweep_scores_wins_and_leader_points():
    records = [
        # decisive win for the variant alliance
        {"variant": "v", "alignment": "good", "winner": "good", "leader": None, "wall_time": 10},
        # decisive loss
        {"variant": "v", "alignment": "evil", "winner": "good", "leader": None, "wall_time": 10},
        # capped, variant alliance leads
        {"variant": "v", "alignment": "good", "winner": None, "leader": "good", "wall_time": 5},
        # capped, no leader
        {"variant": "v", "alignment": "evil", "winner": None, "leader": None, "wall_time": 5},
        # another variant for sort-order coverage
        {"variant": "w", "alignment": "good", "winner": "good", "leader": None, "wall_time": 1},
    ]
    rows = aggregate_sweep(records)
    by_name = {r["variant"]: r for r in rows}
    v = by_name["v"]
    assert v["games"] == 4
    assert v["wins"] == 1
    assert v["losses"] == 1
    assert v["leader_pts"] == 1
    assert v["leader_none"] == 1
    assert v["score"] == 2
    assert v["score_rate"] == pytest.approx(0.5)
    w = by_name["w"]
    assert w["score_rate"] == 1.0
    assert rows[0]["variant"] == "w"  # sorted by descending score_rate


def test_aggregate_sweep_empty_input():
    assert aggregate_sweep([]) == []


def test_format_sweep_table_lists_variants():
    rows = aggregate_sweep([
        {"variant": "baseline", "alignment": "good", "winner": None, "leader": "good", "wall_time": 3},
        {"variant": "material_2.0", "alignment": "good", "winner": "good", "leader": None, "wall_time": 4},
    ])
    table = format_sweep_table(rows)
    assert "baseline" in table
    assert "material_2.0" in table
    assert "rate" in table.splitlines()[0]


def test_run_sweep_dispatches_variants_alignments_and_seeds(monkeypatch):
    calls = []

    def fake_run_matchup(state, ud, td, fd, cd, port_d, algorithms, **kwargs):
        calls.append({
            "algorithms": dict(algorithms),
            "env_overrides": kwargs.get("env_overrides"),
            "seed": kwargs.get("seed"),
            "max_cycles": kwargs.get("max_cycles"),
        })
        alliance = kwargs.get("env_overrides") or {}
        # winner: pretend the weighted alliance always wins
        weighted_side = "good" if any(
            getattr(fd.get(fid), "alliance", "") == "good" for fid in alliance
        ) else "evil"
        return {
            "setup_id": kwargs.get("setup_id", "motw_1.0"),
            "algorithms": dict(algorithms),
            "env_overrides": dict(alliance),
            "winner": weighted_side if alliance else "good",
            "turns_driven": 1,
            "turn_records": [],
            "cycles_completed": 1,
            "turn_number": 1,
            "standings": {"factions": {}, "alliances": {}, "neutral_strongholds": 0},
            "leader": None,
            "wall_time": 0.01,
        }

    monkeypatch.setattr("test.selfplay_sweep.run_matchup", fake_run_matchup)

    class _FakeFaction:
        def __init__(self, alliance):
            self.alliance = alliance

    class _FakeState:
        turn_order = ["gondor", "rohan", "mordor", "isengard"]

    fd = {
        "gondor": _FakeFaction("good"),
        "rohan": _FakeFaction("good"),
        "mordor": _FakeFaction("evil"),
        "isengard": _FakeFaction("evil"),
    }
    monkeypatch.setattr(
        "test.selfplay_sweep._load_setup",
        lambda setup_id: (_FakeState(), {}, {}, fd, {}, {}),
    )

    variant = SweepVariant("material_2.0", {"material": "2.0"})
    records = run_sweep(
        [variant],
        matchup="mcts-vs-mcts",
        seeds=[7],
        games_per_side=1,
        max_cycles=1,
    )
    # both alignments, one seed, one game each
    assert len(records) == 2
    assert {r["alignment"] for r in records} == {"good", "evil"}
    assert all(r["seed"] == 7 for r in records)
    assert all(r["variant"] == "material_2.0" for r in records)
    # env_overrides present on the variant's side only
    good_rec = next(r for r in records if r["alignment"] == "good")
    evil_rec = next(r for r in records if r["alignment"] == "evil")
    assert good_rec["env_overrides"] == {
        "gondor": {"AI_MCTS_W_MATERIAL": "2.0"},
        "rohan": {"AI_MCTS_W_MATERIAL": "2.0"},
    }
    assert evil_rec["env_overrides"] == {
        "mordor": {"AI_MCTS_W_MATERIAL": "2.0"},
        "isengard": {"AI_MCTS_W_MATERIAL": "2.0"},
    }
    assert len(calls) == 2


def test_run_sweep_rejects_bad_args():
    with pytest.raises(ValueError, match="unknown matchup"):
        run_sweep([], matchup="nope", seeds=[1], max_cycles=1)
    with pytest.raises(ValueError, match="games_per_side"):
        run_sweep([], matchup="mcts-vs-mcts", seeds=[1], games_per_side=0, max_cycles=1)
    with pytest.raises(ValueError, match="seeds"):
        run_sweep([], matchup="mcts-vs-mcts", seeds=[], max_cycles=1)
    with pytest.raises(ValueError, match="cap"):
        run_sweep([], matchup="mcts-vs-mcts", seeds=[1], max_cycles=None)


def test_cli_rejects_bad_caps_and_unknown_variant(monkeypatch):
    parser = build_parser()
    # argparse-level: choices are enforced
    with pytest.raises(SystemExit):
        parser.parse_args(["--matchup", "not-a-matchup"])

    monkeypatch.setattr(
        "sys.argv", ["selfplay_sweep", "--max-cycles", "0"]
    )
    from test.selfplay_sweep import main as sweep_main

    with pytest.raises(SystemExit):
        sweep_main()

    monkeypatch.setattr(
        "sys.argv", ["selfplay_sweep", "--games-per-side", "0"]
    )
    with pytest.raises(SystemExit):
        sweep_main()

    monkeypatch.setattr(
        "sys.argv", ["selfplay_sweep", "--variant", "no_such_variant"]
    )
    with pytest.raises(SystemExit):
        sweep_main()


def test_smoke_heuristic_vs_heuristic_one_faction_turn(monkeypatch):
    """Tiny end-to-end smoke: one capped faction turn via run_sweep."""
    records = run_sweep(
        [variant_by_name("baseline")],
        matchup="heuristic-vs-heuristic",
        seeds=[1],
        games_per_side=1,
        max_cycles=1,
        max_faction_turns=1,
        difficulty="easy",
    )
    assert len(records) == 2  # both alignments
    assert {r["alignment"] for r in records} == {"good", "evil"}
    for rec in records:
        assert rec["variant"] == "baseline"
        assert rec["turns_driven"] == 1
