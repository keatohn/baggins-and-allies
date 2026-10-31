"""
Self-play smoke harness: drive decide() through validate_action + apply_action
in-process (no API/DB).

Building blocks:
- 3.2b: full-turn driver across all five phases via the real decide() dispatcher
  with AI_ALGORITHM=mcts, plus the lazy ctx.strategic build check.
- 3.2c: two-faction matchups (MCTS vs heuristic / MCTS vs MCTS) with per-turn
  timing assertions and a win-rate printout, plus test/selfplay_winrate.py for
  longer manual sweeps (CI stays at short capped games).
"""

import contextlib
import json
import os
import pathlib
import random
import time

import pytest

from backend.engine.actions import end_phase
from backend.engine.movement import _is_water_zone, water_transport_relation
from backend.engine.queries import (
    _is_water_boat,
    get_aerial_units_must_move,
    get_contested_territories,
    get_faction_stats,
    get_mobilization_capacity,
    get_mobilization_river_zones,
    get_mobilization_sea_zones,
    get_mobilization_territories,
    get_purchasable_units,
    get_purchased_units,
    get_unit_faction,
    validate_action,
)
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.subfaction_rules import unit_destination_spec

from backend.ai.context import AIContext
from backend.ai.decide import decide
from backend.ai.mcts.action_key import HashableAction
from backend.ai.mcts.dice import sample_dice_for_continue, sample_dice_for_initiate

SETUP_ID = "motw_1.0"

# Generous per-action wall cap (seconds) — catches pathological blowups, not slow CI.
PER_ACTION_CAP_S = 25.0
# Per-faction-turn wall cap for matchups (a full five-phase turn is ~10-55s
# locally; the generous cap only catches runaway turns, not slow CI).
PER_TURN_CAP_S = 300.0
# Hard step cap per phase (anti-stuck safety net for the test itself).
MAX_STEPS_PER_PHASE = 30


def _load_setup(setup_id: str = SETUP_ID):
    from backend.engine.definitions import load_static_definitions, load_starting_setup
    from backend.engine.utils import initialize_game_state

    ud, td, fd, cd, port_d = load_static_definitions(setup_id=setup_id)
    setup = load_starting_setup(setup_id=setup_id)
    manifest = json.loads(
        (pathlib.Path("backend/data/setups") / setup_id / "manifest.json").read_text()
    )
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup,
        camp_defs=cd,
        victory_criteria=manifest.get("victory_criteria"),
        camp_cost=manifest.get("camp_cost"),
        stronghold_repair_cost=manifest.get("stronghold_repair_cost"),
    )
    return state, ud, td, fd, cd, port_d


def _available_actions(state, faction_id, phase, ud, td, fd, cd, port_d) -> dict:
    """Compact available-actions dict for the in-process driver.

    The MCTS Problems compute their own legality from state; the keys here cover
    what decide() itself reads (combat_territories, can_end_phase) plus enough
    for heuristic fallbacks to not crash if MCTS returns nothing. can_end_phase
    uses the reducer's own end-phase guard as ground truth.
    """
    actions: dict = {
        "faction": faction_id,
        "phase": phase,
        "can_end_phase": validate_action(
            state, end_phase(faction_id), ud, td, fd, cd, port_d
        ).valid,
    }
    if phase == "purchase":
        purchasable = get_purchasable_units(state, faction_id, ud, fd)
        capacity_info = get_mobilization_capacity(state, faction_id, td, cd, port_d, ud, fd)
        territories_list = capacity_info.get("territories", [])
        land_cap = sum(t.get("power", 0) for t in territories_list) + sum(
            1 for t in territories_list if t.get("home_unit_capacity")
        )
        sea_cap = sum(z.get("power", 0) for z in capacity_info.get("sea_zones", []))
        actions["purchasable_units"] = purchasable
        actions["mobilization_capacity"] = land_cap + sea_cap
        actions["mobilization_land_capacity"] = land_cap
        actions["purchased_units_count"] = sum(
            s.count for s in (state.faction_purchased_units or {}).get(faction_id, [])
        )
    elif phase == "combat":
        actions["combat_territories"] = get_contested_territories(
            state, faction_id, fd, ud, td
        )
    elif phase == "combat_move":
        # Mirror the API's must-attack boat guidance (read by the heuristic's
        # last-resort path); MCTS computes its own end-phase legality.
        state_after = get_state_after_pending_moves(state, "combat_move", ud, td, fd)
        loaded_boat_ids = set(
            getattr(state_after, "loaded_naval_must_attack_instance_ids", []) or []
        )
        declared_attack: set[str] = set()
        for pm in (state.pending_moves or []):
            if getattr(pm, "phase", None) != "combat_move":
                continue
            from_id = getattr(pm, "from_territory", "")
            to_id = getattr(pm, "to_territory", "")
            if not _is_water_zone(td.get(from_id)):
                continue
            to_land = not _is_water_zone(td.get(to_id))
            to_territory = state.territories.get(to_id) if to_id else None
            to_enemy_sea = (
                water_transport_relation(td.get(from_id), td.get(to_id)) == "sail"
                and to_territory
                and any(
                    get_unit_faction(u, ud) != faction_id
                    and (
                        not fd.get(faction_id)
                        or not fd.get(get_unit_faction(u, ud))
                        or fd.get(get_unit_faction(u, ud)).alliance
                        != fd.get(faction_id).alliance
                    )
                    for u in to_territory.units
                )
            )
            if to_land or to_enemy_sea:
                from_territory = state.territories.get(from_id)
                if from_territory:
                    units_by_iid = {u.instance_id: u for u in from_territory.units}
                    for iid in getattr(pm, "unit_instance_ids", []) or []:
                        u = units_by_iid.get(iid)
                        if u and _is_water_boat(ud.get(u.unit_id)):
                            declared_attack.add(iid)
        actions["loaded_naval_must_attack_instance_ids"] = sorted(
            loaded_boat_ids - declared_attack
        )
    elif phase == "non_combat_move":
        # Aerials still on unfriendly land block end_phase (heuristic rescue path).
        state_after = get_state_after_pending_moves(state, "non_combat_move", ud, td, fd)
        actions["aerial_units_must_move"] = get_aerial_units_must_move(
            state_after, ud, td, fd, faction_id
        )
    elif phase == "mobilization":
        purchased = get_purchased_units(state, faction_id)
        unit_destinations = {}
        for stack in purchased:
            spec = unit_destination_spec(
                state, faction_id, ud.get(stack.get("unit_id")), fd, td, cd, port_d
            )
            if spec:
                unit_destinations[stack["unit_id"]] = spec
        actions["mobilize_options"] = {
            "territories": get_mobilization_territories(state, faction_id, td, cd, port_d, ud, fd),
            "sea_zones": get_mobilization_sea_zones(state, faction_id, td, port_d),
            "river_zones": get_mobilization_river_zones(state, faction_id, td),
            "capacity": get_mobilization_capacity(state, faction_id, td, cd, port_d, ud, fd),
            "pending_units": purchased,
        }
        if unit_destinations:
            actions["mobilize_options"]["unit_destinations"] = unit_destinations
    return actions


def _ctx_for(state, ud, td, fd, cd, port_d) -> AIContext:
    faction_id = state.current_faction or ""
    phase = state.phase or "purchase"
    return AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=_available_actions(state, faction_id, phase, ud, td, fd, cd, port_d),
    )


def _fill_combat_dice(state, action, ud, td, fd, rng) -> None:
    """Fill empty dice on combat actions, mirroring the API's server-side generation."""
    if action.type == "initiate_combat":
        pl = action.payload
        dr = pl.get("dice_rolls") or {}
        if not dr.get("attacker") and not dr.get("defender"):
            pl["dice_rolls"] = sample_dice_for_initiate(
                state,
                pl.get("territory_id"),
                pl.get("sea_zone_id"),
                action.faction,
                ud,
                td,
                fd,
                rng=rng,
            )
    elif action.type == "continue_combat" and state.active_combat:
        dr = action.payload.get("dice_rolls") or {}
        if not dr.get("attacker") and not dr.get("defender"):
            action.payload["dice_rolls"] = sample_dice_for_continue(
                state, ud, td, fd, rng=rng
            )


def run_phase_actions(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str,
    *,
    max_steps: int = MAX_STEPS_PER_PHASE,
    per_action_cap_s: float = PER_ACTION_CAP_S,
    rng: random.Random | None = None,
    decide_times: list[float] | None = None,
) -> tuple[list[str], object]:
    """Drive decide -> validate -> apply until the phase advances or the turn ends.

    Mirrors the /ai-step API loop in-process: fresh AIContext per action, dice
    filled for combat actions. Returns (applied action types, latest state).
    When `decide_times` is given, each decide() wall time is appended to it.
    """
    rng = rng if rng is not None else random.Random(1234)
    start_phase = state.phase or "purchase"
    action_types: list[str] = []
    for _step in range(max_steps):
        if state.winner or state.current_faction != faction_id:
            break
        if (state.phase or "purchase") != start_phase:
            break
        ctx = _ctx_for(state, ud, td, fd, cd, port_d)
        t0 = time.perf_counter()
        action = decide(ctx)
        elapsed = time.perf_counter() - t0
        if decide_times is not None:
            decide_times.append(elapsed)
        assert elapsed <= per_action_cap_s, (
            f"decide() in {start_phase} took {elapsed:.2f}s (cap {per_action_cap_s}s)"
        )
        if action is None:
            # Anti-stuck: same fallback shape as the API loop.
            action = end_phase(faction_id)
        _fill_combat_dice(state, action, ud, td, fd, rng)
        v = validate_action(state, action, ud, td, fd, cd, port_d)
        assert v.valid, f"invalid action {action.type} {action.payload}: {v.error}"
        state, _events = apply_action(state, action, ud, td, fd, cd, port_d)
        action_types.append(action.type)
    else:
        raise AssertionError(
            f"phase {start_phase} hit step cap {max_steps} without advancing (anti-stuck broken?)"
        )
    return action_types, state


def run_faction_turn(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    faction_id: str,
    *,
    per_action_cap_s: float = PER_ACTION_CAP_S,
    rng: random.Random | None = None,
    decide_times: list[float] | None = None,
) -> tuple[list[str], object]:
    """Drive all five phases of one faction's turn. Returns (action types, latest state)."""
    rng = rng if rng is not None else random.Random(1234)
    all_types: list[str] = []
    for expected_phase in ("purchase", "combat_move", "combat", "non_combat_move", "mobilization"):
        if state.winner or state.current_faction != faction_id:
            break
        assert (state.phase or "purchase") == expected_phase, (
            f"expected phase {expected_phase}, got {state.phase}"
        )
        types, state = run_phase_actions(
            state, ud, td, fd, cd, port_d, faction_id,
            per_action_cap_s=per_action_cap_s, rng=rng, decide_times=decide_times,
        )
        all_types.extend(types)
    return all_types, state


# ---------------------------------------------------------------------------
# 3.2c: two-faction matchups, timing assertions, win-rate printout
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _algorithm_env(algorithm: str):
    """Temporarily set AI_ALGORITHM for one faction's turn; restores ambient value."""
    previous = os.environ.get("AI_ALGORITHM")
    os.environ["AI_ALGORITHM"] = algorithm
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AI_ALGORITHM", None)
        else:
            os.environ["AI_ALGORITHM"] = previous


def run_matchup(
    state,
    ud,
    td,
    fd,
    cd,
    port_d,
    algorithms: dict[str, str],
    *,
    default_algorithm: str = "heuristic",
    max_faction_turns: int | None = None,
    max_cycles: int | None = 1,
    setup_id: str = SETUP_ID,
    per_action_cap_s: float = PER_ACTION_CAP_S,
    per_turn_cap_s: float = PER_TURN_CAP_S,
    seed: int | None = None,
) -> dict:
    """Drive a game where each faction decides via its own algorithm.

    `algorithms` maps faction id -> "mcts" | "heuristic"; factions missing from
    the map use `default_algorithm`. Caps (whichever hits first):
    `max_faction_turns` limits driven faction turns; `max_cycles` limits full
    turn-order wraps (the reducer increments state.turn_number at each wrap).
    Defaults to 1 cycle when neither cap is given so a call can never run
    unbounded. Per-action and per-turn wall caps are asserted inside the loop.
    `seed` (if given) seeds both the module RNG (heuristic tie-breaking) and the
    dice stream so games are reproducible.
    """
    if max_faction_turns is None and max_cycles is None:
        max_cycles = 1
    if seed is not None:
        random.seed(seed)
    rng = random.Random(seed) if seed is not None else None

    start_turn_number = state.turn_number
    assigned = {
        fid: algorithms.get(fid, default_algorithm)
        for fid in (state.turn_order or list(fd))
    }
    turn_records: list[dict] = []
    wall_start = time.perf_counter()
    while not state.winner:
        if max_faction_turns is not None and len(turn_records) >= max_faction_turns:
            break
        if max_cycles is not None and state.turn_number - start_turn_number >= max_cycles:
            break
        fid = state.current_faction or ""
        assert fid, "matchup state has no current_faction"
        turn_number_before = state.turn_number
        algorithm = assigned.get(fid, default_algorithm)
        decide_times: list[float] = []
        t0 = time.perf_counter()
        with _algorithm_env(algorithm):
            action_types, state = run_faction_turn(
                state, ud, td, fd, cd, port_d, fid,
                per_action_cap_s=per_action_cap_s, rng=rng, decide_times=decide_times,
            )
        wall = time.perf_counter() - t0
        assert wall <= per_turn_cap_s, (
            f"{fid} ({algorithm}) turn took {wall:.1f}s (cap {per_turn_cap_s}s)"
        )
        progressed = (
            state.winner is not None
            or state.current_faction != fid
            or state.turn_number != turn_number_before
        )
        assert progressed, f"{fid} completed a turn with no handoff/wrap (soft-lock)"
        turn_records.append({
            "faction": fid,
            "algorithm": algorithm,
            "actions": len(action_types),
            "action_types": action_types,
            "decide_times": decide_times,
            "wall_time": wall,
        })

    stats = get_faction_stats(state, td, fd, ud)
    factions = {
        fid: {k: int(row.get(k, 0) or 0) for k in ("territories", "strongholds", "power")}
        for fid, row in stats.get("factions", {}).items()
    }
    alliances = {
        name: {
            "strongholds": int(row.get("strongholds", 0) or 0),
            "territories": int(row.get("territories", 0) or 0),
        }
        for name, row in stats.get("alliances", {}).items()
    }
    sh_counts = {name: row["strongholds"] for name, row in alliances.items()}
    leader: str | None = None
    if sh_counts:
        top = max(sh_counts.values())
        top_names = sorted(n for n, v in sh_counts.items() if v == top)
        leader = top_names[0] if len(top_names) == 1 else "tie"

    return {
        "setup_id": setup_id,
        "algorithms": assigned,
        "winner": state.winner,
        "turns_driven": len(turn_records),
        "turn_records": turn_records,
        "cycles_completed": int(state.turn_number - start_turn_number),
        "turn_number": int(state.turn_number),
        "standings": {
            "factions": factions,
            "alliances": alliances,
            "neutral_strongholds": int(stats.get("neutral_strongholds", 0) or 0),
        },
        "leader": leader,
        "wall_time": time.perf_counter() - wall_start,
    }


def format_matchup_report(record: dict) -> str:
    """Human-readable matchup summary: result, per-turn timing, standings."""
    lines = [
        f"=== matchup {record['setup_id']} ===",
        (
            f"result: {record['winner'] or 'no winner (cap reached)'} | "
            f"faction turns: {record['turns_driven']} | "
            f"cycles: {record['cycles_completed']} | "
            f"wall: {record['wall_time']:.1f}s"
        ),
        "turn | faction | algorithm | actions | decide avg/max | turn wall",
    ]
    for i, turn in enumerate(record["turn_records"], 1):
        times = turn["decide_times"]
        avg = sum(times) / len(times) if times else 0.0
        worst = max(times) if times else 0.0
        lines.append(
            f"{i} | {turn['faction']} | {turn['algorithm']} | {turn['actions']} | "
            f"{avg:.2f}/{worst:.2f} | {turn['wall_time']:.1f}"
        )
    standings = record["standings"]
    lines.append(f"standings (leader: {record['leader'] or 'none'}):")
    lines.append("faction | territories | strongholds | power")
    for fid, row in standings["factions"].items():
        lines.append(
            f"{fid} | {row['territories']} | {row['strongholds']} | {row['power']}"
        )
    lines.append(
        "alliances: "
        + ", ".join(
            f"{name} {row['strongholds']}sh/{row['territories']}terr"
            for name, row in sorted(standings["alliances"].items())
        )
        + f" | neutral strongholds: {standings['neutral_strongholds']}"
    )
    return "\n".join(lines)


def summarize_winrate(records: list[dict]) -> str:
    """Aggregate tally across matchup records: decisive wins + leader-at-cutoff."""
    games = len(records)
    decisive = [r for r in records if r["winner"]]
    lines = [
        f"games: {games} | decisive: {len(decisive)} | capped: {games - len(decisive)}"
    ]
    if games == 0:
        return lines[0]
    wins: dict[str, int] = {}
    for record in decisive:
        wins[record["winner"]] = wins.get(record["winner"], 0) + 1
    if wins:
        for winner, n in sorted(wins.items()):
            lines.append(f"win rate {winner}: {n}/{games} ({100.0 * n / games:.0f}%)")
    else:
        lines.append("win rates: none within cap")
    leaders: dict[str, int] = {}
    for record in records:
        key = record["leader"] or "none"
        leaders[key] = leaders.get(key, 0) + 1
    lines.append(
        "leader at cutoff: "
        + ", ".join(f"{name} {n}" for name, n in sorted(leaders.items()))
    )
    for i, record in enumerate(records, 1):
        outcome = record["winner"] or f"capped (leader: {record['leader'] or 'none'})"
        algs = "/".join(sorted(set(record["algorithms"].values())))
        lines.append(
            f"  game {i}: {record['setup_id']} [{algs}] {outcome} | "
            f"turns {record['turns_driven']} | wall {record['wall_time']:.0f}s"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3.2b: full-turn smoke via the real decide() dispatcher
# ---------------------------------------------------------------------------


def test_full_turn_smoke_mcts(monkeypatch):
    """One complete faction turn (all five phases) with AI_ALGORITHM=mcts.

    Asserts the structural smoke contract: every action passes validate_action,
    no per-action wall-time blowup, the loop terminates, and the turn hands off
    to the next faction (or the game ends).
    """
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    faction_id = state.current_faction or ""
    assert state.phase == "purchase"

    action_types, final_state = run_faction_turn(
        state, ud, td, fd, cd, port_d, faction_id
    )

    assert action_types, "expected at least one applied action"
    assert "end_phase" in action_types, "turn must advance via end_phase"
    assert final_state.current_faction != faction_id or final_state.winner is not None, (
        "faction turn must hand off (or game must end)"
    )


def test_decide_skips_strategic_build_when_mcts_succeeds(monkeypatch):
    """MCTS paths must not pay the ctx.strategic build; heuristic paths still do."""
    import importlib

    decide_mod = importlib.import_module("backend.ai.decide")

    calls = {"n": 0}
    original = decide_mod.build_strategic_turn_context

    def spy(ctx):
        calls["n"] += 1
        return original(ctx)

    # decide() imports the symbol by name, so patch the name it bound.
    monkeypatch.setattr(decide_mod, "build_strategic_turn_context", spy)

    # MCTS path: purchase decides via MCTS -> strategic never built.
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources.setdefault(fid, {})["power"] = 20
    ctx = _ctx_for(state, ud, td, fd, cd, port_d)
    action = decide(ctx)
    assert action is not None
    assert calls["n"] == 0, "MCTS path built ctx.strategic unnecessarily"
    assert ctx.strategic is None

    # Heuristic path: strategic must be built before the purchase policy runs.
    monkeypatch.setenv("AI_ALGORITHM", "heuristic")
    state2, ud2, td2, fd2, cd2, port2 = _load_setup()
    fid2 = state2.current_faction or ""
    state2.faction_resources.setdefault(fid2, {})["power"] = 15
    ctx2 = _ctx_for(state2, ud2, td2, fd2, cd2, port2)
    action2 = decide(ctx2)
    assert action2 is not None
    assert calls["n"] == 1
    assert ctx2.strategic is not None


# ---------------------------------------------------------------------------
# Recovered 3.2a coverage: purchase-phase smoke + determinism + anti-stuck
# ---------------------------------------------------------------------------


def test_purchase_phase_smoke_buys_then_ends(monkeypatch):
    """MCTS purchase phase: buys (power permitting) then ends the phase cleanly."""
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources.setdefault(fid, {})["power"] = 25
    action_types, final_state = run_phase_actions(
        state, ud, td, fd, cd, port_d, fid
    )
    assert action_types[-1] == "end_phase"
    assert "purchase_units" in action_types, "expected MCTS to spend available power"
    assert (final_state.phase or "") != "purchase" or final_state.current_faction != fid


def test_purchase_phase_zero_power_anti_stuck(monkeypatch):
    """Zero power: purchase must terminate with a single end_phase (no soft-lock)."""
    monkeypatch.setenv("AI_ALGORITHM", "mcts")
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    fid = state.current_faction or ""
    state.faction_resources[fid] = {"power": 0}
    action_types, final_state = run_phase_actions(
        state, ud, td, fd, cd, port_d, fid
    )
    assert action_types == ["end_phase"]
    assert (final_state.phase or "") != "purchase" or final_state.current_faction != fid


def test_purchase_mcts_seed_determinism(monkeypatch):
    """Same seeded PurchaseProblem search -> same action-type sequence."""
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    from backend.ai.mcts.budget import get_budget
    from backend.ai.mcts.purchase import PurchaseProblem
    from backend.ai.mcts.search import run_mcts

    def purchase_seq(seed: int) -> list[str]:
        random.seed(seed)  # PurchaseProblem takes no rng; seed module-level entropy.
        state, ud, td, fd, cd, port_d = _load_setup()
        fid = state.current_faction or ""
        state.faction_resources.setdefault(fid, {})["power"] = 25
        ctx = _ctx_for(state, ud, td, fd, cd, port_d)
        problem = PurchaseProblem(ctx)
        budget = get_budget("easy")
        seq: list[str] = []
        s = ctx.state
        for _ in range(8):
            a = run_mcts(problem, s, budget)
            if a is None:
                break
            raw = a.unwrap() if isinstance(a, HashableAction) else a
            seq.append(raw.type)
            if raw.type == "end_phase":
                break
            s = problem.apply(s, a)
        return seq

    seq1 = purchase_seq(7)
    seq2 = purchase_seq(7)
    assert seq1, "expected a non-empty purchase sequence"
    assert seq1 == seq2


# ---------------------------------------------------------------------------
# 3.2c: matchups + baseline win-rate printout
# ---------------------------------------------------------------------------


def _alliance_algorithms(
    fd, faction_ids, by_alliance: dict[str, str], default: str = "heuristic"
) -> dict[str, str]:
    return {
        fid: by_alliance.get(getattr(fd.get(fid), "alliance", ""), default)
        for fid in faction_ids
    }


def test_matchup_mcts_vs_mcts(monkeypatch):
    """Matchup smoke: two MCTS factions alternate turns within timing caps."""
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    state, ud, td, fd, cd, port_d = _load_setup()
    result = run_matchup(
        state, ud, td, fd, cd, port_d,
        {fid: "mcts" for fid in state.turn_order},
        max_faction_turns=2,
        seed=7,
    )
    print(format_matchup_report(result))

    assert result["turns_driven"] == 2
    assert len({t["faction"] for t in result["turn_records"]}) == 2, (
        "the two driven turns must go to different factions"
    )
    for turn in result["turn_records"]:
        assert turn["algorithm"] == "mcts"
        assert turn["actions"] >= 1
        assert "end_phase" in turn["action_types"], "faction turn must end via end_phase"
        assert turn["decide_times"], "decide timings must be recorded"
        assert max(turn["decide_times"]) <= PER_ACTION_CAP_S
        assert turn["wall_time"] <= PER_TURN_CAP_S
    assert result["winner"] is None or result["winner"] in set(result["standings"]["alliances"])


def test_matchup_mcts_vs_heuristic(monkeypatch):
    """Mixed matchup: each side decides with its own algorithm in-process.

    Also checks the lazy strategic build stays per-side: the MCTS faction never
    builds ctx.strategic, the heuristic faction always does.
    """
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    import importlib

    decide_mod = importlib.import_module("backend.ai.decide")
    builds: dict[str, int] = {}
    original = decide_mod.build_strategic_turn_context

    def spy(ctx):
        fid = ctx.state.current_faction or ""
        builds[fid] = builds.get(fid, 0) + 1
        return original(ctx)

    monkeypatch.setattr(decide_mod, "build_strategic_turn_context", spy)

    state, ud, td, fd, cd, port_d = _load_setup()
    algorithms = _alliance_algorithms(
        fd, state.turn_order, {"good": "mcts", "evil": "heuristic"}
    )
    result = run_matchup(
        state, ud, td, fd, cd, port_d, algorithms, max_faction_turns=2, seed=13
    )
    print(format_matchup_report(result))

    assert result["turns_driven"] == 2
    assert {t["algorithm"] for t in result["turn_records"]} == {"mcts", "heuristic"}, (
        "first two factions must span both algorithms (setup changed?)"
    )
    for turn in result["turn_records"]:
        if turn["algorithm"] == "mcts":
            assert builds.get(turn["faction"], 0) == 0, (
                f"{turn['faction']} MCTS turn built ctx.strategic"
            )
        else:
            assert builds.get(turn["faction"], 0) >= 1, (
                f"{turn['faction']} heuristic turn never built ctx.strategic"
            )
        assert max(turn["decide_times"]) <= PER_ACTION_CAP_S
        assert turn["wall_time"] <= PER_TURN_CAP_S


def test_matchup_baseline_winrate_printout(monkeypatch):
    """Baseline win-rate printout: aggregate tally across short capped games.

    CI games are intentionally capped far below victory, so the tally mostly
    shows capped games + leader-at-cutoff; real win rates come from
    test/selfplay_winrate.py run manually.
    """
    monkeypatch.setenv("AI_MCTS_DIFFICULTY", "easy")
    records = []
    for seed, mode in ((11, "mcts"), (22, "mixed")):
        state, ud, td, fd, cd, port_d = _load_setup()
        if mode == "mcts":
            algorithms = {fid: "mcts" for fid in state.turn_order}
        else:
            algorithms = _alliance_algorithms(
                fd, state.turn_order, {"good": "mcts", "evil": "heuristic"}
            )
        records.append(
            run_matchup(
                state, ud, td, fd, cd, port_d, algorithms,
                max_faction_turns=1, seed=seed,
            )
        )
    summary = summarize_winrate(records)
    print(summary)

    assert len(records) == 2
    assert all(r["turns_driven"] == 1 for r in records)
    decisive = sum(1 for r in records if r["winner"])
    assert f"games: 2 | decisive: {decisive} | capped: {2 - decisive}" in summary
    assert "game 1:" in summary and "game 2:" in summary
    assert "leader at cutoff:" in summary
    for record in records:
        assert record["leader"] is None or isinstance(record["leader"], str)
