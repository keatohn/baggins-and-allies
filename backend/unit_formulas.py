"""
Global unit cost formulas for the admin Unit Stats view.

Two linear formulas predict a unit's power cost from its features:
- regression: weights fit by ridge regression on purchasable units from chosen setups
- custom: weights typed in by the admin

Feature keys: attack, defense, movement, dice, health, transport_capacity, is_naval, is_hero,
and "special:<id>" (1 when the unit has that special). The frontend computes the same features
in frontend/src/pages/admin/unitFormulas.ts.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from backend.engine.definitions import DATA_DIR
from backend.setup_data import is_files_setup_source

UNIT_FORMULAS_PATH = DATA_DIR / "unit_formulas.json"
UNIT_FORMULAS_SETTING_KEY = "unit_formulas"

CORE_FEATURES = ("attack", "defense", "movement", "dice", "health", "transport_capacity")
FLAG_FEATURES = ("is_naval", "is_hero")
SPECIAL_PREFIX = "special:"
MAX_RIDGE = 1000.0


class UnitFormulasError(ValueError):
    pass


def is_feature_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if key in CORE_FEATURES or key in FLAG_FEATURES:
        return True
    return key.startswith(SPECIAL_PREFIX) and len(key) > len(SPECIAL_PREFIX)


def _number(raw: Any, default: float = 0.0) -> float:
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def _label(raw: Any, default: str) -> str:
    s = raw.strip() if isinstance(raw, str) else ""
    return s[:24] or default


def _feature_list(raw: Any) -> list[str]:
    out: list[str] = []
    for key in raw if isinstance(raw, list) else []:
        if is_feature_key(key) and key not in out:
            out.append(key)
    return out


def _weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {k: _number(v) for k, v in raw.items() if is_feature_key(k) and _number(v) != 0}


def _string_list(raw: Any) -> list[str]:
    out: list[str] = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, str) and s.strip() and s.strip() not in out:
            out.append(s.strip())
    return out


def _optional_number(raw: Any) -> float | None:
    n = _number(raw, float("nan"))
    return None if math.isnan(n) else n


def normalize_formulas(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    training = data.get("training") if isinstance(data.get("training"), dict) else {}
    reg = data.get("regression") if isinstance(data.get("regression"), dict) else {}
    stats = reg.get("stats") if isinstance(reg.get("stats"), dict) else {}
    custom = data.get("custom") if isinstance(data.get("custom"), dict) else {}
    return {
        "features": _feature_list(data.get("features", list(CORE_FEATURES))),
        "training": {
            "setup_ids": _string_list(training.get("setup_ids")),
            "include_heroes": training.get("include_heroes") is not False,
            "ridge": min(MAX_RIDGE, max(0.0, _number(training.get("ridge"), 1.0))),
        },
        "regression": {
            "label": _label(reg.get("label"), "Fit"),
            "show": reg.get("show") is True,
            "intercept": _number(reg.get("intercept")),
            "weights": _weights(reg.get("weights")),
            "stats": {
                "n": max(0, int(_number(stats.get("n")))),
                "r2": _optional_number(stats.get("r2")),
                "rmse": _optional_number(stats.get("rmse")),
                "mae": _optional_number(stats.get("mae")),
                "trained_at": stats.get("trained_at") if isinstance(stats.get("trained_at"), str) else None,
            },
        },
        "custom": {
            "label": _label(custom.get("label"), "Custom"),
            "show": custom.get("show") is True,
            "intercept": _number(custom.get("intercept")),
            "weights": _weights(custom.get("weights")),
        },
        "diff_label": _label(data.get("diff_label"), "Δ"),
    }


def unit_power_cost(unit: dict[str, Any]) -> float:
    cost = unit.get("cost")
    if isinstance(cost, dict):
        return _number(cost.get("power"))
    return _number(cost)


def unit_features(unit: dict[str, Any]) -> dict[str, float]:
    """Every feature value for one unit definition. Missing dice count as 1, like the game."""
    tags = unit.get("tags") if isinstance(unit.get("tags"), list) else []
    specials = unit.get("specials") if isinstance(unit.get("specials"), list) else []
    hero_id = unit.get("hero_id")
    out = {
        "attack": _number(unit.get("attack")),
        "defense": _number(unit.get("defense")),
        "movement": _number(unit.get("movement")),
        "dice": _number(unit.get("dice"), 1.0),
        "health": _number(unit.get("health")),
        "transport_capacity": _number(unit.get("transport_capacity")),
        "is_naval": 1.0 if unit.get("archetype") == "naval" or "naval" in tags else 0.0,
        "is_hero": 1.0 if isinstance(hero_id, str) and hero_id.strip() else 0.0,
    }
    for sid in specials:
        if isinstance(sid, str) and sid:
            out[SPECIAL_PREFIX + sid] = 1.0
    return out


def is_training_unit(unit: dict[str, Any], include_heroes: bool) -> bool:
    if unit.get("purchasable") is False or unit_power_cost(unit) <= 0:
        return False
    return include_heroes or unit_features(unit)["is_hero"] == 0


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None when the system is singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-9:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_ridge(
    rows: list[dict[str, float]],
    targets: list[float],
    features: list[str],
    ridge: float,
) -> dict[str, Any]:
    """
    Fit cost ≈ intercept + Σ weight·feature. Features are standardized before the ridge penalty so
    one strength treats A and a 0/1 special alike; weights come back in raw units. The intercept is
    not penalized. Features that never vary in the data get weight 0.
    """
    n = len(rows)
    if n == 0:
        raise UnitFormulasError("No purchasable units to train on. Pick at least one setup.")
    y_mean = sum(targets) / n
    used: list[str] = []
    means: list[float] = []
    scales: list[float] = []
    for key in features:
        col = [r.get(key, 0.0) for r in rows]
        mean = sum(col) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in col) / n)
        if sd > 1e-12:
            used.append(key)
            means.append(mean)
            scales.append(sd)
    z = [[(r.get(k, 0.0) - means[j]) / scales[j] for j, k in enumerate(used)] for r in rows]
    yc = [t - y_mean for t in targets]
    p = len(used)
    xtx = [[sum(z[i][a] * z[i][b] for i in range(n)) + (ridge if a == b else 0.0) for b in range(p)] for a in range(p)]
    xty = [sum(z[i][a] * yc[i] for i in range(n)) for a in range(p)]
    beta = _solve(xtx, xty) if p else []
    if beta is None:
        raise UnitFormulasError(
            "Some chosen features move together exactly, so plain least squares has no single answer. "
            "Raise the ridge strength above 0 or untick one of them."
        )
    weights = {k: beta[j] / scales[j] for j, k in enumerate(used)}
    intercept = y_mean - sum(weights[k] * means[j] for j, k in enumerate(used))
    preds = [intercept + sum(w * r.get(k, 0.0) for k, w in weights.items()) for r in rows]
    sse = sum((t - q) ** 2 for t, q in zip(targets, preds))
    sst = sum((t - y_mean) ** 2 for t in targets)
    return {
        "intercept": intercept,
        "weights": {k: w for k, w in weights.items() if w != 0},
        "stats": {
            "n": n,
            "r2": 1 - sse / sst if sst > 0 else None,
            "rmse": math.sqrt(sse / n),
            "mae": sum(abs(t - q) for t, q in zip(targets, preds)) / n,
        },
    }


def training_rows(
    unit_sets: Iterable[dict[str, Any]],
    include_heroes: bool,
) -> tuple[list[dict[str, float]], list[float]]:
    """Training rows from several setups' unit dicts. The same unit id with the same stats and cost counts once."""
    seen: set[str] = set()
    rows: list[dict[str, float]] = []
    targets: list[float] = []
    for units in unit_sets:
        for uid, unit in (units or {}).items():
            if not isinstance(unit, dict) or not is_training_unit(unit, include_heroes):
                continue
            feats = unit_features(unit)
            cost = unit_power_cost(unit)
            key = json.dumps([uid, cost, sorted(feats.items())])
            if key in seen:
                continue
            seen.add(key)
            rows.append(feats)
            targets.append(cost)
    return rows, targets


def train_regression(
    unit_sets: Iterable[dict[str, Any]],
    features: list[str],
    include_heroes: bool,
    ridge: float,
) -> dict[str, Any]:
    features = _feature_list(features)
    if not features:
        raise UnitFormulasError("Tick at least one feature to train on.")
    rows, targets = training_rows(unit_sets, include_heroes)
    result = fit_ridge(rows, targets, features, min(MAX_RIDGE, max(0.0, ridge)))
    result["stats"]["trained_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _read_file() -> dict[str, Any]:
    if not UNIT_FORMULAS_PATH.is_file():
        return normalize_formulas({})
    try:
        with open(UNIT_FORMULAS_PATH, encoding="utf-8") as f:
            return normalize_formulas(json.load(f))
    except (json.JSONDecodeError, OSError):
        return normalize_formulas({})


def _write_file(data: dict[str, Any]) -> None:
    UNIT_FORMULAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(UNIT_FORMULAS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_unit_formulas(db: Session | None) -> dict[str, Any]:
    if is_files_setup_source() or db is None:
        return _read_file()
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == UNIT_FORMULAS_SETTING_KEY).first()
    if not row:
        return _read_file()
    try:
        return normalize_formulas(json.loads(row.value_json))
    except (json.JSONDecodeError, TypeError):
        return _read_file()


def save_unit_formulas(db: Session | None, raw: Any) -> dict[str, Any]:
    data = normalize_formulas(raw)
    if is_files_setup_source() or db is None:
        _write_file(data)
        return data
    from backend.api.models import AppSetting

    payload = json.dumps(data, ensure_ascii=False)
    row = db.query(AppSetting).filter(AppSetting.key == UNIT_FORMULAS_SETTING_KEY).first()
    if row:
        row.value_json = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=UNIT_FORMULAS_SETTING_KEY, value_json=payload))
    db.commit()
    return data


def seed_unit_formulas_if_empty(db: Session) -> None:
    if is_files_setup_source():
        return
    from backend.api.models import AppSetting

    if db.query(AppSetting).filter(AppSetting.key == UNIT_FORMULAS_SETTING_KEY).first():
        return
    db.add(AppSetting(key=UNIT_FORMULAS_SETTING_KEY, value_json=json.dumps(_read_file(), ensure_ascii=False)))
    db.commit()
