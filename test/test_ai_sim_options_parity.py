"""AI sims must score battles with the same wall HP and prefire flag as live combat.

Covers issue #51 batch 3:
- Sim M1: AI call sites omit `stronghold_initial_hp` (damaged/breached walls were
  simulated at full base HP).
- Sim M5: AI call sites never pass `prefire_penalty`, so 5 of 6 shipped setups
  (manifest `prefire_penalty: false`) were simulated with the wrong prefire stat.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

from backend.ai.combat import _casualty_order_pair_options
from backend.ai.defense_sim import (
    _sim_options_for_territory,
    worst_case_defender_hold_probability,
)
from backend.engine.combat_sim import sim_prefire_penalty, sim_stronghold_initial_hp
from backend.engine.definitions import load_static_definitions

REPO_ROOT = Path(__file__).resolve().parents[1]
AI_DIR = REPO_ROOT / "backend" / "ai"


def _defs():
    ud, td, *_ = load_static_definitions(setup_id="wotr_exp_1.0")
    return ud, td


def _state(*, walls: int | None, prefire_penalty: bool = True, tid: str = "minas_tirith"):
    """Minimal state stand-in: only the fields the sim-option helpers read."""
    territory = SimpleNamespace(stronghold_current_health=walls)
    return SimpleNamespace(
        territories={tid: territory},
        prefire_penalty=prefire_penalty,
    )


# --- M1 helpers -------------------------------------------------------------

def test_stronghold_hp_falls_back_to_base_when_state_has_no_value():
    _, td = _defs()
    tdef = td["minas_tirith"]  # base 4
    assert sim_stronghold_initial_hp(_state(walls=None), "minas_tirith", tdef) == 4
    assert sim_stronghold_initial_hp(SimpleNamespace(), "minas_tirith", tdef) == 4


def test_stronghold_hp_uses_current_value_when_state_tracks_it():
    _, td = _defs()
    tdef = td["minas_tirith"]
    assert sim_stronghold_initial_hp(_state(walls=2), "minas_tirith", tdef) == 2
    # Walls already breached: an explicit 0 must reach the sim (no soak), not base HP.
    assert sim_stronghold_initial_hp(_state(walls=0), "minas_tirith", tdef) == 0


def test_stronghold_hp_none_for_non_stronghold_or_zero_base():
    _, td = _defs()
    gap = td["gap_of_rohan"]  # not a stronghold
    assert sim_stronghold_initial_hp(_state(walls=None), "gap_of_rohan", gap) is None
    zero_base = SimpleNamespace(is_stronghold=True, stronghold_base_health=0)
    assert sim_stronghold_initial_hp(_state(walls=None), "minas_tirith", zero_base) is None
    assert sim_stronghold_initial_hp(_state(walls=None), "minas_tirith", None) is None


def test_prefire_penalty_reads_state_flag():
    assert sim_prefire_penalty(SimpleNamespace(prefire_penalty=False)) is False
    assert sim_prefire_penalty(SimpleNamespace(prefire_penalty=True)) is True
    assert sim_prefire_penalty(SimpleNamespace()) is True  # GameState default


# --- M1/M5: option builders the AI call sites use ---------------------------

def test_defense_sim_options_carry_wall_hp_and_prefire_flag():
    _, td = _defs()
    tdef = td["minas_tirith"]

    walls_down = _sim_options_for_territory(_state(walls=0, prefire_penalty=False), "minas_tirith", tdef)
    assert walls_down.stronghold_initial_hp == 0
    assert walls_down.prefire_penalty is False
    assert walls_down.must_conquer is True
    assert walls_down.casualty_order_attacker == "best_attack"

    full_walls = _sim_options_for_territory(_state(walls=None), "minas_tirith", tdef)
    assert full_walls.stronghold_initial_hp == 4
    assert full_walls.prefire_penalty is True

    open_ground = _sim_options_for_territory(_state(walls=None), "gap_of_rohan", td["gap_of_rohan"])
    assert open_ground.stronghold_initial_hp is None
    assert open_ground.must_conquer is False
    assert open_ground.casualty_order_attacker == "best_unit"
    assert open_ground.prefire_penalty is True


def test_casualty_order_pair_options_differ_only_in_order():
    _, td = _defs()
    tdef = td["edoras"]  # base 1
    state = _state(walls=0, prefire_penalty=False, tid="edoras")

    best_attack, best_unit = _casualty_order_pair_options(state, "edoras", tdef, True)
    assert best_attack.casualty_order_attacker == "best_attack"
    assert best_unit.casualty_order_attacker == "best_unit"
    for opt in (best_attack, best_unit):
        assert opt.stronghold_initial_hp == 0
        assert opt.prefire_penalty is False
        assert opt.must_conquer is True

    atk, unit = _casualty_order_pair_options(_state(walls=None), "gap_of_rohan", td["gap_of_rohan"], False)
    for opt in (atk, unit):
        assert opt.stronghold_initial_hp is None
        assert opt.prefire_penalty is True
        assert opt.must_conquer is False


# --- M1/M5: options actually reach run_simulation from an AI entry point ----

def test_worst_case_hold_sim_passes_wall_hp_and_prefire_flag(monkeypatch):
    """defense_sim's hold-probability sim must forward state-derived options to the sim."""
    ud, td = _defs()
    captured = {}

    def fake_run_simulation(att, def_stacks, territory_id, unit_defs, territory_defs,
                            n_trials=1000, options=None, seed=None, return_outcomes=False):
        captured["options"] = options
        return SimpleNamespace(p_attacker_win=0.5)

    monkeypatch.setattr("backend.ai.defense_sim.run_simulation", fake_run_simulation)

    defender = SimpleNamespace(unit_id="morannon_orc")
    attacker = SimpleNamespace(unit_id="urukhai_warrior")
    state = _state(walls=0, prefire_penalty=False)

    p = worst_case_defender_hold_probability(
        [{"unit_id": "morannon_orc", "count": 2}],
        "minas_tirith",
        {"isengard": [attacker]},
        ud,
        td,
        state=state,
        n_trials=1,
    )

    assert p is not None
    opts = captured["options"]
    assert opts.stronghold_initial_hp == 0
    assert opts.prefire_penalty is False
    # Without state the same call would fall back to base walls + prefire penalty on.
    assert opts.stronghold_initial_hp != 4


# --- M5 guard: no AI SimOptions may drift from the live rules ---------------

def test_every_ai_simoptions_passes_wall_hp_and_prefire_flag():
    """Every `SimOptions(...)` built under backend/ai/ names both live-parity fields.

    Defaults are wrong for AI scoring: `stronghold_initial_hp=None` simulates full
    base walls (M1) and `prefire_penalty=True` contradicts the shipped manifests (M5).
    """
    offenders: list[str] = []
    checked = 0
    for path in sorted(AI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "SimOptions"):
                continue
            checked += 1
            names = {kw.arg for kw in node.keywords}
            missing = {"stronghold_initial_hp", "prefire_penalty"} - names
            if missing:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno} missing {sorted(missing)}")
    assert checked >= 10, f"expected the known AI SimOptions call sites, saw {checked}"
    assert not offenders, "AI SimOptions must pass live-parity options:\n" + "\n".join(offenders)
