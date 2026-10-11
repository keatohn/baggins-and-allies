"""Create-game menu visibility: public, hidden, and admin-only."""

import json

from backend.engine import definitions
from backend.engine.definitions import (
    list_setups,
    menu_visibility,
    scenario_menu_entry,
    shown_on_create_menu,
)
from backend.setup_validation import validate_setup_payload


def test_menu_visibility_values():
    assert menu_visibility(True) == "yes"
    assert menu_visibility(False) == "no"
    assert menu_visibility(None) == "no"
    assert menu_visibility("admin") == "admin"
    assert menu_visibility(" Admin ") == "admin"
    assert menu_visibility("yes") == "no"


def test_shown_on_create_menu_includes_admin_only_for_admins():
    public = {"is_active": True}
    hidden = {"is_active": False}
    admin = {"is_active": "admin"}
    assert shown_on_create_menu(public, include_admin=False) is True
    assert shown_on_create_menu(hidden, include_admin=True) is False
    assert shown_on_create_menu(admin, include_admin=False) is False
    assert shown_on_create_menu(admin, include_admin=True) is True


def test_scenario_menu_entry_marks_admin_only():
    entry = scenario_menu_entry(
        {"id": "draft", "display_name": "Draft", "is_active": "admin", "context": {"year": "TA 3019"}},
        "draft",
    )
    assert entry["admin_only"] is True
    public = scenario_menu_entry(
        {"id": "live", "display_name": "Live", "is_active": True, "context": {"year": "TA 3019"}},
        "live",
    )
    assert "admin_only" not in public


def test_list_setups_hides_admin_only_unless_requested(tmp_path, monkeypatch):
    def write(name, active):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "starting_setup.json").write_text("{}")
        (folder / "manifest.json").write_text(json.dumps({
            "id": name,
            "display_name": name,
            "is_active": active,
            "context": {"year": "TA 3019"},
        }))

    write("public_one", True)
    write("hidden_one", False)
    write("admin_one", "admin")
    monkeypatch.setattr(definitions, "SETUPS_DIR", tmp_path)

    public_ids = [row["id"] for row in list_setups()]
    admin_ids = [row["id"] for row in list_setups(include_admin=True)]
    assert public_ids == ["public_one"]
    assert admin_ids == ["admin_one", "public_one"]
    admin_row = next(row for row in list_setups(include_admin=True) if row["id"] == "admin_one")
    assert admin_row["admin_only"] is True


def test_admin_only_scenario_still_needs_context():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent / "backend" / "data" / "setups" / "woteas_1.0"
    payload = {}
    for key in ("manifest", "units", "territories", "factions", "camps", "ports", "starting_setup"):
        path = root / f"{key}.json"
        payload[key] = json.loads(path.read_text()) if path.exists() else {}
    payload["manifest"]["is_active"] = "admin"
    payload["manifest"]["context"] = {}
    errors = validate_setup_payload(payload)
    assert "manifest.context must be a non-empty object when the scenario is shown in the create-game menu" in errors

    payload["manifest"]["is_active"] = "sometimes"
    errors = validate_setup_payload(payload)
    assert 'manifest.is_active must be true, false, or "admin"' in errors
