"""Sequential opening-combat projections using the same search as the Openings tab."""
from copy import deepcopy
from statistics import mean

from backend.balance.starting_strength import compute_starting_strength
from backend.engine.definitions import definitions_from_snapshot
from backend.engine.openings import compute_opening_attacks

SEEDS = (8, 29, 71)
METRICS = ("starting_power_score", "starting_power_share", "unit_power", "power_production", "strongholds")


def compute_opening_projection(bundle: dict, *, rings: bool | None = None) -> dict:
    working = deepcopy(bundle)
    # Match the balance toggle without disabling required rules.
    if rings is False:
        working["manifest"]["special_rules"] = [
            rule for rule in working["manifest"].get("special_rules", [])
            if not (isinstance(rule, dict) and rule.get("type") == "rings_of_power" and rule.get("is_optional") is True)
        ]
    else:
        for rule in working["manifest"].get("special_rules", []):
            if isinstance(rule, dict) and rule.get("type") == "rings_of_power":
                rule["is_optional"] = False
    snapshot = {
        name: {key: {**row, "id": row.get("id", key)} for key, row in working.get(name, {}).items()}
        for name in ("units", "territories", "factions", "camps", "ports")
    }
    units, territories, factions, camps, _ports = definitions_from_snapshot(snapshot)
    baseline = compute_starting_strength(bundle, rings=rings)
    runs = []
    for seed in SEEDS:
        result = compute_opening_attacks(
            units, territories, factions, camps, working["starting_setup"],
            manifest=working["manifest"], seed=seed, n_trials=2000,
        )
        projected = deepcopy(working)
        projected["starting_setup"]["territory_owners"] = result["final_board"]["owners"]
        projected["starting_setup"]["starting_units"] = result["final_board"]["units"]
        runs.append(compute_starting_strength(projected, rings=rings))
    rows = []
    for original in baseline["alliances"]:
        samples = [next(row for row in run["alliances"] if row["id"] == original["id"]) for run in runs]
        metrics = {}
        for metric in METRICS:
            values = [row[metric] for row in samples if row.get(metric) is not None]
            if not values:
                metrics[metric] = None
                continue
            average = mean(values)
            metrics[metric] = {"mean": round(average, 3), "min": min(values), "max": max(values),
                               "change": round(average - (original.get(metric) or 0), 3)}
        rows.append({"id": original["id"], "display_name": original["display_name"], "metrics": metrics})
    return {"runs": len(SEEDS), "seeds": list(SEEDS), "alliances": rows}
