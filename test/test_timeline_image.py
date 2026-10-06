"""Create-game timeline image filenames on scenario menu rows."""

from backend.engine.definitions import scenario_menu_entry, timeline_image_filename


def test_timeline_image_filename_accepts_scenario_assets():
    assert timeline_image_filename({"timeline_image": "last_alliance.png"}) == "last_alliance.png"
    assert timeline_image_filename({"timeline_image": "  scene.webp "}) == "scene.webp"
    assert timeline_image_filename({"timeline_image": "portrait.jpeg"}) == "portrait.jpeg"


def test_timeline_image_filename_rejects_paths_and_other_files():
    assert timeline_image_filename({}) is None
    assert timeline_image_filename({"timeline_image": ""}) is None
    assert timeline_image_filename({"timeline_image": "../units/sauron.png"}) is None
    assert timeline_image_filename({"timeline_image": "notes.txt"}) is None
    assert timeline_image_filename({"timeline_image": "folder/scene.png"}) is None


def test_scenario_menu_entry_includes_timeline_image():
    entry = scenario_menu_entry(
        {
            "id": "wotla_1.0",
            "display_name": "War of the Last Alliance",
            "map_asset": "wotla_map_1.0",
            "context": {"year": "SA 3434"},
            "timeline_image": "alliance.png",
        },
        "wotla_1.0",
    )
    assert entry["timeline_image"] == "alliance.png"
    assert entry["id"] == "wotla_1.0"


def test_scenario_menu_entry_omits_invalid_timeline_image():
    entry = scenario_menu_entry(
        {
            "id": "x",
            "display_name": "X",
            "map_asset": "m",
            "context": {"year": "TA 3019"},
            "timeline_image": "../secret.png",
        },
        "x",
    )
    assert "timeline_image" not in entry
