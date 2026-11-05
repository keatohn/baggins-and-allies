"""
M5: combat event output must not depend on Python hash randomization.

Set-backed id lists (wounded ids, bomb-pair self-destruct casualties) were
returned via list(set) / set-union, so their order — and therefore event payload
order — varied per process. All such lists are now sorted.

Run: .venv/bin/pytest test/test_combat_determinism.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from backend.engine.combat import _apply_hits

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "test" / "determinism_scenario.py"


def _unit(iid: str, unit_id: str):
    return SimpleNamespace(
        instance_id=iid, unit_id=unit_id, health=3, remaining_health=3, remaining_movement=0,
    )


def test_apply_hits_wounded_ids_sorted():
    """_apply_hits returns wounded instance ids in sorted order (not set order)."""
    defs = {
        "tank": SimpleNamespace(
            attack=1, defense=3, dice=1, health=3, cost={"power": 1},
            archetype="infantry", specials=[],
        ),
    }
    # Ids chosen so their hash order differs from lexicographic order under most seeds.
    units = [_unit("zz_tank", "tank"), _unit("aa_tank", "tank"), _unit("mm_tank", "tank")]
    destroyed, wounded = _apply_hits(units, 3, defs, is_attacker=False, casualty_order="best_unit")
    assert destroyed == []
    assert len(wounded) == 3
    assert wounded == sorted(wounded)


def _run_scenario(hash_seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    proc = subprocess.run(
        [sys.executable, str(SCENARIO)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"scenario failed:\n{proc.stderr}"
    return proc.stdout


def test_events_deterministic_across_hash_seeds():
    """The full combat sequence produces byte-identical events under two hash seeds."""
    out_a = _run_scenario("0")
    out_b = _run_scenario("42")
    assert out_a == out_b

    events = json.loads(out_a)
    # Sanity: the scenario really exercises the set-order sites.
    prefires = [e for e in events if e["payload"].get("is_archer_prefire")]
    assert len(prefires) == 1
    assert len(prefires[0]["payload"]["attacker_wounded"]) == 2
    destroyed = [e for e in events if e["type"] == "unit_destroyed"]
    # 4: the siegeworks bomb-pair self-destruct (set-union order) + 2 round-1 kills.
    assert len(destroyed) == 4
    pair_ids = {e["payload"]["unit_id"] for e in destroyed[:2]}
    assert any("bomb" in i for i in pair_ids)
    assert any("berserker" in i for i in pair_ids)
