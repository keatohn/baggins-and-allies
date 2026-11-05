"""
Hash-seed determinism scenario for test/test_combat_determinism.py.

Not collected by pytest (no test_ prefix). Run directly:

    PYTHONHASHSEED=0 python test/determinism_scenario.py

Prints a JSON dump of every event from a fixed combat sequence (siegeworks
bomb-pair self-destruct, archer prefire wounds, round-1 wounds, combat end).
Outputs under different PYTHONHASHSEED values must be byte-identical.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.engine.actions import continue_combat, initiate_combat
from backend.engine.definitions import UnitDefinition, load_static_definitions, load_starting_setup
from backend.engine.reducer import apply_action
from backend.engine.state import Unit
from backend.engine.utils import initialize_game_state

SETUP = "wotr_exp_1.0"
TERRITORY = "gap_of_rohan"


def run_scenario() -> str:
    unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(
        setup_id=SETUP
    )
    unit_defs["tankx"] = UnitDefinition(
        id="tankx", display_name="Tank X", faction="isengard", archetype="infantry",
        tags=["land"], attack=5, defense=3, movement=1, health=2, cost={"power": 1},
        dice=1, specials=[],
    )
    unit_defs["deftank"] = UnitDefinition(
        id="deftank", display_name="Def Tank", faction="rohan", archetype="infantry",
        tags=["land"], attack=1, defense=4, movement=1, health=2, cost={"power": 2},
        dice=1, specials=[],
    )
    starting = load_starting_setup(setup_id=SETUP)
    state = initialize_game_state(
        faction_defs, territory_defs, unit_defs,
        starting_setup=starting,
        camp_defs=camp_defs,
    )
    t = state.territories[TERRITORY]
    t.units.clear()
    t.owner = "rohan"

    def make(faction: str, unit_id: str) -> Unit:
        ud = unit_defs[unit_id]
        return Unit(
            instance_id=state.generate_unit_instance_id(faction, unit_id),
            unit_id=unit_id,
            remaining_movement=ud.movement,
            remaining_health=ud.health,
            base_movement=ud.movement,
            base_health=ud.health,
        )

    t.units.extend([
        make("isengard", "bomb"),
        make("isengard", "berserker"),
        make("isengard", "tankx"),
        make("isengard", "tankx"),
        make("rohan", "rohirrim_archer"),
        make("rohan", "rohirrim_archer"),
        make("rohan", "deftank"),
        make("rohan", "deftank"),
    ])
    state.current_faction = "isengard"
    state.phase = "combat"

    dump = []

    def record(events):
        for e in events:
            dump.append({"type": e.type, "payload": e.payload})

    # Round 0: siegeworks. Rolls of 10 miss every defender (hit iff roll <= stat);
    # the fused bomb+berserker pair still self-destructs, and the unit_destroyed
    # event order comes from a set union.
    state, events = apply_action(
        state,
        initiate_combat("isengard", TERRITORY, {"attacker": [10] * 10, "defender": []}),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    record(events)

    # Continue: defender archer prefire wounds both multi-health attacker tanks.
    state, events = apply_action(
        state,
        continue_combat("isengard", {"attacker": [], "defender": [1, 1]}),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    record(events)

    # Continue: round 1 — both multi-health defender units are wounded, then the
    # attackers are eliminated and combat ends.
    state, events = apply_action(
        state,
        continue_combat("isengard", {"attacker": [1, 1], "defender": [1, 1, 1, 1]}),
        unit_defs, territory_defs, faction_defs, camp_defs, port_defs,
    )
    record(events)

    return json.dumps(dump, sort_keys=True, default=str)


if __name__ == "__main__":
    print(run_scenario())
