"""Admin unit cost formulas: features, ridge fit, storage, and the train endpoint."""
import pytest

from backend import unit_formulas
from backend.unit_formulas import (
    UnitFormulasError,
    fit_ridge,
    normalize_formulas,
    train_regression,
    training_rows,
    unit_features,
)


def _unit(attack, defense, cost, **extra):
    return {
        "attack": attack,
        "defense": defense,
        "dice": 1,
        "movement": 1,
        "health": 1,
        "transport_capacity": 0,
        "cost": {"power": cost},
        "purchasable": True,
        "specials": [],
        "tags": [],
        **extra,
    }


def test_unit_features_include_flags_and_specials():
    feats = unit_features(
        {
            "attack": 3,
            "defense": 2,
            "health": 2,
            "archetype": "naval",
            "hero_id": "aragorn",
            "specials": ["archer", "fearless"],
        }
    )
    assert feats["attack"] == 3
    assert feats["dice"] == 1
    assert feats["is_naval"] == 1
    assert feats["is_hero"] == 1
    assert feats["special:archer"] == 1
    assert "special:charging" not in feats


def test_plain_least_squares_recovers_exact_weights():
    units = {
        f"u{i}": _unit(a, d, 1 + 2 * a + 0.5 * d)
        for i, (a, d) in enumerate([(1, 1), (2, 1), (3, 2), (1, 4), (4, 3), (2, 2)])
    }
    rows, targets = training_rows([units], include_heroes=True)
    fit = fit_ridge(rows, targets, ["attack", "defense"], ridge=0)
    assert fit["intercept"] == pytest.approx(1)
    assert fit["weights"]["attack"] == pytest.approx(2)
    assert fit["weights"]["defense"] == pytest.approx(0.5)
    assert fit["stats"]["r2"] == pytest.approx(1)
    assert fit["stats"]["mae"] == pytest.approx(0, abs=1e-9)


def test_ridge_shrinks_weights_and_constant_features_get_none():
    units = {
        f"u{i}": _unit(a, d, 1 + 2 * a + 0.5 * d)
        for i, (a, d) in enumerate([(1, 1), (2, 1), (3, 2), (1, 4), (4, 3), (2, 2)])
    }
    rows, targets = training_rows([units], include_heroes=True)
    fit = fit_ridge(rows, targets, ["attack", "defense", "health"], ridge=5)
    assert 0 < fit["weights"]["attack"] < 2
    assert "health" not in fit["weights"]


def test_collinear_features_need_ridge():
    units = {f"u{i}": _unit(a, a, 2 * a) for i, a in enumerate([1, 2, 3, 4])}
    rows, targets = training_rows([units], include_heroes=True)
    with pytest.raises(UnitFormulasError):
        fit_ridge(rows, targets, ["attack", "defense"], ridge=0)
    fit = fit_ridge(rows, targets, ["attack", "defense"], ridge=0.1)
    assert fit["weights"]["attack"] == pytest.approx(fit["weights"]["defense"])


def test_training_rows_skip_unbuyable_free_heroes_and_duplicates():
    a = {
        "grunt": _unit(1, 1, 3),
        "free": _unit(1, 1, 0),
        "locked": _unit(1, 1, 5, purchasable=False),
        "hero": _unit(3, 3, 8, hero_id="h"),
    }
    b = {"grunt": _unit(1, 1, 3), "other": _unit(2, 2, 6)}
    rows, targets = training_rows([a, b], include_heroes=False)
    assert sorted(targets) == [3, 6]
    rows, targets = training_rows([a, b], include_heroes=True)
    assert sorted(targets) == [3, 6, 8]


def test_train_requires_features_and_units():
    with pytest.raises(UnitFormulasError):
        train_regression([{"x": _unit(1, 1, 3)}], [], True, 1)
    with pytest.raises(UnitFormulasError):
        train_regression([{}], ["attack"], True, 1)


def test_normalize_drops_bad_keys_and_fills_defaults():
    data = normalize_formulas(
        {
            "features": ["attack", "bogus", "special:archer", "attack"],
            "custom": {"show": True, "label": "  Mine ", "weights": {"attack": 2, "nope": 1, "defense": 0}},
            "training": {"ridge": -3},
        }
    )
    assert data["features"] == ["attack", "special:archer"]
    assert data["custom"] == {"label": "Mine", "show": True, "intercept": 0.0, "weights": {"attack": 2.0}}
    assert data["training"]["ridge"] == 0
    assert data["regression"]["label"] == "Fit"
    assert data["diff_label"] == "Δ"


def test_formula_endpoints_round_trip(tmp_path, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from backend.api import main
    from backend.api.auth import get_current_admin
    from backend.api.main import app
    from backend.engine.definitions import SETUPS_DIR
    from backend.setup_data import import_setup_folder_to_dicts

    def bundle_from_folder(_db, setup_id):
        folder = Path(SETUPS_DIR) / setup_id
        return import_setup_folder_to_dicts(folder) if folder.is_dir() else None

    monkeypatch.setattr(main, "get_admin_setup_bundle", bundle_from_folder)
    monkeypatch.setattr(unit_formulas, "UNIT_FORMULAS_PATH", tmp_path / "unit_formulas.json")
    monkeypatch.setattr(unit_formulas, "is_files_setup_source", lambda: True)
    app.dependency_overrides[get_current_admin] = lambda: object()
    try:
        client = TestClient(app)
        trained = client.post(
            "/admin/formulas/train",
            json={"setup_ids": ["wotr_1.1"], "features": ["attack", "defense", "health"], "ridge": 1},
        )
        assert trained.status_code == 200
        fit = trained.json()
        assert fit["stats"]["n"] > 10
        saved = client.put(
            "/admin/formulas",
            json={"features": ["attack"], "regression": {"show": True, **fit}, "diff_label": "Off by"},
        )
        assert saved.status_code == 200
        loaded = client.get("/admin/formulas").json()
        assert loaded["regression"]["weights"] == pytest.approx(fit["weights"])
        assert loaded["diff_label"] == "Off by"
        missing = client.post("/admin/formulas/train", json={"setup_ids": ["nope"], "features": ["attack"]})
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
