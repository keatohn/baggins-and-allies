"""Parchment grain strength stays inside 0–1."""

from backend.map_texture import DEFAULT_MAP_TEXTURE_OPACITY, normalize_map_texture_opacity


def test_opacity_clamps_and_rounds():
    assert normalize_map_texture_opacity(0.555) == 0.56
    assert normalize_map_texture_opacity(2) == 1.0
    assert normalize_map_texture_opacity(-1) == 0.0


def test_opacity_falls_back_when_missing():
    assert normalize_map_texture_opacity(None) == DEFAULT_MAP_TEXTURE_OPACITY
    assert normalize_map_texture_opacity("nope") == DEFAULT_MAP_TEXTURE_OPACITY
    assert normalize_map_texture_opacity(True) == DEFAULT_MAP_TEXTURE_OPACITY
